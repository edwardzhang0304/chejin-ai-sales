"""Standard workflow service for curation/import/release/eval lifecycle."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from apps.wechat_ai_customer_service.knowledge_paths import (
    active_tenant_id,
    default_admin_knowledge_base_root,
    tenant_context,
    tenant_root,
    tenant_runtime_root,
)
from apps.wechat_ai_customer_service.workflows.knowledge_intake import evaluate_intake_item
from apps.wechat_ai_customer_service.workflows.knowledge_runtime import KnowledgeRuntime

from .knowledge_base_store import KnowledgeBaseStore
from .knowledge_compiler import KnowledgeCompiler
from .knowledge_registry import KnowledgeRegistry
from .knowledge_schema_manager import KnowledgeSchemaManager
from .version_store import VersionStore


APP_ROOT = Path(__file__).resolve().parents[2]
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
PHONE_RE = re.compile(r"(?<!\d)(1[3-9]\d[\s-]?\d{4}[\s-]?\d{4})(?!\d)")
NAMED_PERSON_RE_COLON = re.compile(r"((?:姓名|联系人|销售|客服|顾问|经理|老师|先生|女士|车主)\s*[:：]\s*)([\u4e00-\u9fff]{2,4})")
NAMED_PERSON_RE_INLINE = re.compile(r"((?:联系人|销售|客服|经理|老师)\s*)([\u4e00-\u9fff]{2,4})(?=[，,。；;\s]|$)")
METADATA_POLLUTION_TERMS = (
    "schema_version",
    "risk_level",
    "requires_handoff",
    "allow_auto_reply",
    "category_id",
    "source.batch_token",
)
NOISE_PATTERNS = (
    "系统消息",
    "撤回了一条消息",
    "以上是打招呼",
    "点击链接",
    "邀请你加入群聊",
    "现在开始聊天吧",
)
FORBIDDEN_TERM_CLAUSE_SEPARATORS = ("。", "！", "？", "；", ";", "，", ",", "\n")
FORBIDDEN_TERM_NEGATION_TOKENS = (
    "不能",
    "不可",
    "不会",
    "无法",
    "不得",
    "不予",
    "不要",
    "不应",
    "不做",
    "不承诺",
    "不保证",
    "不敢",
    "并非",
    "不是",
    "禁止",
    "严禁",
    "拒绝",
    "避免",
    "杜绝",
    "请勿",
    "勿",
    "别",
    "未",
    "无",
    "没",
    "不",
)
FORBIDDEN_TERM_NEGATION_BRIDGES = ("承诺", "保证", "做", "给", "说", "答应")
DEFAULT_METRICS_GATE = {
    "factual_consistency_min": 0.95,
    "violation_rate_max": 0.01,
    "handoff_precision_min": 0.90,
    "continue_chat_rate_min": 0.70,
}
MATCH_SCORE_MIN = 0.2
REAL_CHAT_FORMAL_SOURCE_TYPES = {
    "cleaned_real_chat_pack",
    "real_chat",
    "wechat_raw_message",
    "raw_wechat_private",
    "raw_wechat_group",
    "raw_wechat_file_transfer",
}
REAL_CHAT_BATCH_MARKERS = ("realchat", "real_chat", "实盘聊天", "真实聊天", "微信聊天")
REAL_CHAT_ID_PREFIXES = ("chejin_real_",)


class WorkflowService:
    def __init__(self, *, tenant_id: str | None = None) -> None:
        self.tenant_id = active_tenant_id(tenant_id)
        self.registry = None
        self.schema_manager = None
        self.base_store = None
        self.version_store = VersionStore()
        self.compiler = None

    def _scoped_components(self, tenant_id: str) -> tuple[KnowledgeSchemaManager, KnowledgeBaseStore, KnowledgeCompiler]:
        registry = KnowledgeRegistry(root=default_admin_knowledge_base_root(tenant_id))
        schema_manager = KnowledgeSchemaManager(registry)
        base_store = KnowledgeBaseStore(registry, schema_manager)
        compiler = KnowledgeCompiler(runtime=KnowledgeRuntime(tenant_id=tenant_id))
        return schema_manager, base_store, compiler

    # -------------------------------
    # Chapter 1: data curation
    # -------------------------------

    def create_curation_job(
        self,
        *,
        tenant_id: str,
        industry_id: str,
        batch_id: str,
        source_files: list[str],
        strict_mode: bool = True,
    ) -> dict[str, Any]:
        requested_tenant = active_tenant_id(tenant_id or self.tenant_id)
        normalized_batch = sanitize_batch_id(batch_id)
        job_id = "curate_job_" + digest(f"{requested_tenant}:{normalized_batch}:{now_iso()}", length=12)
        started_at = now_iso()

        with tenant_context(requested_tenant):
            schema_manager, _base_store, _compiler = self._scoped_components(requested_tenant)
            schema = schema_manager.load_schema("chats")
            rows: list[dict[str, Any]] = []
            reject_reasons: dict[str, int] = {}
            source_missing: list[str] = []
            accepted_signatures: set[str] = set()
            sanitized_phone_count = 0
            sanitized_name_count = 0
            total_candidates = 0

            for source_file in source_files:
                source_path = Path(str(source_file or "")).expanduser()
                if not source_path.exists():
                    source_missing.append(str(source_path))
                    bump(reject_reasons, "source_file_missing")
                    continue
                for raw_record in iter_source_records(source_path):
                    for candidate in extract_dialogue_candidates(raw_record):
                        total_candidates += 1
                        normalized = normalize_candidate(candidate)
                        if normalized is None:
                            bump(reject_reasons, "missing_customer_or_reply")
                            continue
                        if looks_like_noise(normalized["customer_message"], normalized["service_reply"]):
                            bump(reject_reasons, "noise_or_system_text")
                            continue
                        if contains_metadata_pollution(normalized["service_reply"]):
                            bump(reject_reasons, "metadata_pollution")
                            continue

                        clean_customer, customer_phone_hits, customer_name_hits = sanitize_text(normalized["customer_message"])
                        clean_reply, reply_phone_hits, reply_name_hits = sanitize_text(normalized["service_reply"])
                        sanitized_phone_count += customer_phone_hits + reply_phone_hits
                        sanitized_name_count += customer_name_hits + reply_name_hits
                        if strict_mode and (has_phone(clean_customer) or has_phone(clean_reply)):
                            bump(reject_reasons, "phone_leak_after_sanitize")
                            continue

                        signature = digest(f"{clean_customer}||{clean_reply}", length=20)
                        if signature in accepted_signatures:
                            bump(reject_reasons, "duplicate_dialogue")
                            continue
                        accepted_signatures.add(signature)

                        template_item = build_template_item(
                            category_id="chats",
                            customer_message=clean_customer,
                            service_reply=clean_reply,
                            batch_token=normalized_batch,
                            industry_id=industry_id,
                            hint_id=normalized.get("id", ""),
                            intent_tags=normalized.get("intent_tags", []),
                            tone_tags=normalized.get("tone_tags", []),
                            scenario=normalized.get("scenario", ""),
                            hit_count=normalized.get("hit_count", 1),
                            source_samples=normalized.get("source_samples", []),
                        )
                        evaluated = evaluate_intake_item(
                            category_id="chats",
                            schema=schema,
                            item=template_item,
                            raw_text=f"{clean_customer}\n{clean_reply}",
                            confidence=0.88,
                            source_label=str(candidate.get("source_label") or "实盘聊天"),
                        )
                        intake = evaluated.get("intake") if isinstance(evaluated.get("intake"), dict) else {}
                        normalized_item = evaluated.get("item") if isinstance(evaluated.get("item"), dict) else template_item
                        if not intake.get("ok"):
                            bump(reject_reasons, "schema_needs_more_info")
                            continue
                        rows.append(normalized_item)

            curated_path = self.curated_templates_root() / f"templates_{normalized_batch}.jsonl"
            write_jsonl(curated_path, rows)
            summary = {
                "total_candidates": total_candidates,
                "accepted": len(rows),
                "rejected": max(0, total_candidates - len(rows)),
                "source_missing_count": len(source_missing),
                "sanitized_phone_count": sanitized_phone_count,
                "sanitized_name_count": sanitized_name_count,
                "reject_reasons": reject_reasons,
                "schema_pass_rate": 1.0 if rows else 0.0,
                "sensitive_leak_count": 0 if strict_mode else sanitized_phone_count + sanitized_name_count,
            }
            report = {
                "ok": True,
                "job_id": job_id,
                "tenant_id": requested_tenant,
                "industry_id": str(industry_id or "").strip(),
                "batch_id": normalized_batch,
                "strict_mode": bool(strict_mode),
                "source_files": [str(Path(item).expanduser()) for item in source_files],
                "source_missing": source_missing,
                "curated_file": str(curated_path),
                "summary": summary,
                "created_at": started_at,
                "completed_at": now_iso(),
                "status": "completed",
            }
            write_json(self.curation_jobs_root() / f"{job_id}.json", report)
            write_json(self.curation_reports_root() / f"curation_report_{job_id}.json", report)
            write_json(
                self.curation_reports_root() / f"acceptance_report_{job_id}.json",
                {
                    "job_id": job_id,
                    "tenant_id": requested_tenant,
                    "batch_id": normalized_batch,
                    "schema_pass_rate": summary["schema_pass_rate"],
                    "sensitive_leak_count": summary["sensitive_leak_count"],
                    "accepted": summary["accepted"],
                    "rejected": summary["rejected"],
                    "ok": summary["schema_pass_rate"] >= 1.0 and summary["sensitive_leak_count"] == 0,
                    "generated_at": now_iso(),
                },
            )
            return report

    def get_curation_job(self, job_id: str) -> dict[str, Any] | None:
        return read_json(self.curation_jobs_root() / f"{job_id}.json")

    def list_curation_jobs(self, *, limit: int = 20) -> list[dict[str, Any]]:
        return list_json_files(self.curation_jobs_root(), limit=limit)

    # -------------------------------
    # Chapter 2: import + version
    # -------------------------------

    def dry_run_import(
        self,
        *,
        tenant_id: str,
        industry_id: str,
        input_file: str,
    ) -> dict[str, Any]:
        requested_tenant = active_tenant_id(tenant_id or self.tenant_id)
        with tenant_context(requested_tenant):
            schema_manager, base_store, _compiler = self._scoped_components(requested_tenant)
            source_path = Path(str(input_file or "")).expanduser()
            if not source_path.exists():
                return {"ok": False, "message": f"input file not found: {source_path}"}

            schema = schema_manager.load_schema("chats")
            existing_items = {
                str(item.get("id") or ""): item
                for item in base_store.list_items("chats", include_archived=True)
                if isinstance(item, dict) and str(item.get("id") or "")
            }
            seen_ids: set[str] = set()
            new_items: list[dict[str, Any]] = []
            updated_items: list[dict[str, Any]] = []
            skipped_items: list[dict[str, Any]] = []
            blocked_items: list[dict[str, Any]] = []
            conflicts: list[dict[str, Any]] = []
            ready_items: list[dict[str, Any]] = []

            for line_no, item in iter_jsonl(source_path):
                normalized = normalize_template_payload(item, default_category="chats")
                item_id = str(normalized.get("id") or "")
                if not item_id:
                    blocked_items.append({"line": line_no, "reason": "missing_id"})
                    continue
                if item_id in seen_ids:
                    blocked_items.append({"line": line_no, "id": item_id, "reason": "duplicate_id_in_batch"})
                    continue
                seen_ids.add(item_id)
                if has_phone(str((normalized.get("data") or {}).get("service_reply") or "")):
                    blocked_items.append({"line": line_no, "id": item_id, "reason": "phone_leak"})
                    continue
                real_chat_reason = real_chat_formal_import_block_reason(normalized)
                if real_chat_reason:
                    blocked_items.append(
                        {
                            "line": line_no,
                            "id": item_id,
                            "reason": "real_chat_requires_rag_first",
                            "message": real_chat_reason,
                            "source_type": str((normalized.get("source") or {}).get("type") or ""),
                            "recommended_target": "rag_experience_and_style_memory",
                        }
                    )
                    continue
                evaluated = evaluate_intake_item(
                    category_id="chats",
                    schema=schema,
                    item=normalized,
                    raw_text=str((normalized.get("data") or {}).get("service_reply") or ""),
                    confidence=0.9,
                    source_label="workflow_import",
                )
                intake = evaluated.get("intake") if isinstance(evaluated.get("intake"), dict) else {}
                normalized_item = evaluated.get("item") if isinstance(evaluated.get("item"), dict) else normalized
                if not intake.get("ok"):
                    blocked_items.append(
                        {
                            "line": line_no,
                            "id": item_id,
                            "reason": "schema_needs_more_info",
                            "missing_fields": intake.get("missing_fields", []),
                        }
                    )
                    continue
                ready_items.append(normalized_item)

                existing = existing_items.get(item_id)
                if existing is None:
                    new_items.append({"id": item_id})
                    continue
                if item_signature(existing) == item_signature(normalized_item):
                    skipped_items.append({"id": item_id, "reason": "same_content"})
                    continue
                updated_items.append({"id": item_id})
                conflicts.append({"id": item_id, "reason": "existing_item_will_be_updated"})

            job_id = "import_job_" + digest(f"dry_run:{requested_tenant}:{source_path}:{now_iso()}", length=12)
            ready_items_path = self.import_jobs_root() / f"{job_id}_ready_items.jsonl"
            write_jsonl(ready_items_path, ready_items)
            summary = {
                "total": len(seen_ids),
                "new_items": len(new_items),
                "updated_items": len(updated_items),
                "skipped_items": len(skipped_items),
                "blocked_items": len(blocked_items),
            }
            job = {
                "ok": True,
                "job_id": job_id,
                "tenant_id": requested_tenant,
                "industry_id": str(industry_id or "").strip(),
                "mode": "dry_run",
                "status": "completed",
                "input_file": str(source_path),
                "ready_items_file": str(ready_items_path),
                "summary": summary,
                "new_items": new_items,
                "updated_items": updated_items,
                "skipped_items": skipped_items,
                "blocked_items": blocked_items,
                "conflicts": conflicts,
                "created_at": now_iso(),
            }
            write_json(self.import_jobs_root() / f"{job_id}.json", job)
            write_json(self.import_reports_root() / f"import_dry_run_report_{job_id}.json", job)
            return job

    def apply_import(
        self,
        *,
        tenant_id: str,
        industry_id: str,
        dry_run_job_id: str = "",
        input_file: str = "",
        release_version: str = "",
    ) -> dict[str, Any]:
        requested_tenant = active_tenant_id(tenant_id or self.tenant_id)
        with tenant_context(requested_tenant):
            _schema_manager, base_store, compiler = self._scoped_components(requested_tenant)
            dry_job: dict[str, Any]
            if dry_run_job_id:
                dry_job = self.get_import_job(dry_run_job_id) or {}
                if not dry_job:
                    return {"ok": False, "message": f"dry run job not found: {dry_run_job_id}"}
                if str(dry_job.get("mode") or "") != "dry_run":
                    return {"ok": False, "message": f"job is not dry_run: {dry_run_job_id}"}
            else:
                if not input_file:
                    return {"ok": False, "message": "input_file is required when dry_run_job_id is empty"}
                dry_job = self.dry_run_import(tenant_id=requested_tenant, industry_id=industry_id, input_file=input_file)
                if not dry_job.get("ok"):
                    return dry_job

            if int((dry_job.get("summary") or {}).get("blocked_items", 0) or 0) > 0:
                return {
                    "ok": False,
                    "message": "dry-run has blocked items; apply is refused",
                    "dry_run_job_id": dry_job.get("job_id"),
                    "blocked_items": dry_job.get("blocked_items", []),
                }

            ready_items_path = Path(str(dry_job.get("ready_items_file") or ""))
            if not ready_items_path.exists():
                return {"ok": False, "message": f"ready_items_file not found: {ready_items_path}"}

            ready_items = [item for _, item in iter_jsonl(ready_items_path)]
            real_chat_blocks = [
                {
                    "id": str(item.get("id") or ""),
                    "reason": "real_chat_requires_rag_first",
                    "message": real_chat_formal_import_block_reason(item),
                    "recommended_target": "rag_experience_and_style_memory",
                }
                for item in ready_items
                if real_chat_formal_import_block_reason(item)
            ]
            if real_chat_blocks:
                return {
                    "ok": False,
                    "message": "real-chat learning material cannot be applied directly to formal knowledge; send it to AI experience pool item/style memory first",
                    "dry_run_job_id": dry_job.get("job_id"),
                    "blocked_items": real_chat_blocks,
                }
            snapshot = self.version_store.create_snapshot(
                reason="workflow template import apply",
                metadata={
                    "tenant_id": requested_tenant,
                    "industry_id": str(industry_id or "").strip(),
                    "dry_run_job_id": dry_job.get("job_id"),
                    "release_version": str(release_version or ""),
                    "source": "workflow_service",
                },
            )
            applied_ids: list[str] = []
            failed_ids: list[dict[str, Any]] = []
            for item in ready_items:
                result = base_store.save_item("chats", item)
                if result.get("ok"):
                    applied_ids.append(str((result.get("item") or {}).get("id") or ""))
                else:
                    failed_ids.append({"id": str(item.get("id") or ""), "reason": result.get("problems") or result.get("message") or "save_failed"})
            if applied_ids:
                compiler.compile_to_disk()

            summary = {
                "total": len(ready_items),
                "applied_items": len(applied_ids),
                "failed_items": len(failed_ids),
                "new_items": int((dry_job.get("summary") or {}).get("new_items", 0) or 0),
                "updated_items": int((dry_job.get("summary") or {}).get("updated_items", 0) or 0),
                "skipped_items": int((dry_job.get("summary") or {}).get("skipped_items", 0) or 0),
                "blocked_items": 0,
            }
            import_job_id = "import_job_" + digest(f"apply:{requested_tenant}:{dry_job.get('job_id')}:{now_iso()}", length=12)
            apply_job = {
                "ok": len(failed_ids) == 0,
                "job_id": import_job_id,
                "tenant_id": requested_tenant,
                "industry_id": str(industry_id or "").strip(),
                "mode": "apply",
                "status": "completed" if len(failed_ids) == 0 else "partial_failed",
                "release_version": str(release_version or ""),
                "source_dry_run_job_id": dry_job.get("job_id"),
                "ready_items_file": str(ready_items_path),
                "summary": summary,
                "applied_item_ids": applied_ids,
                "failed_items": failed_ids,
                "rollback_to": str(snapshot.get("version_id") or ""),
                "backup_snapshot": snapshot,
                "created_at": now_iso(),
            }
            write_json(self.import_jobs_root() / f"{import_job_id}.json", apply_job)
            write_json(self.import_reports_root() / f"import_apply_report_{import_job_id}.json", apply_job)
            return apply_job

    def get_import_job(self, job_id: str) -> dict[str, Any] | None:
        return read_json(self.import_jobs_root() / f"{job_id}.json")

    def list_import_jobs(self, *, limit: int = 20) -> list[dict[str, Any]]:
        return list_json_files(self.import_jobs_root(), limit=limit)

    # -------------------------------
    # Chapter 5/6: eval + release
    # -------------------------------

    def run_replay_eval(
        self,
        *,
        tenant_id: str,
        release_version: str,
        suite_id: str = "",
        suite_file: str = "",
        metrics_gate: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        requested_tenant = active_tenant_id(tenant_id or self.tenant_id)
        with tenant_context(requested_tenant):
            _schema_manager, base_store, _compiler = self._scoped_components(requested_tenant)
            suite_payload = self._load_eval_suite(suite_id=suite_id, suite_file=suite_file)
            if not suite_payload.get("ok"):
                return suite_payload
            cases = suite_payload.get("cases", [])
            chats_items = base_store.list_items("chats", include_archived=False)
            case_results = [evaluate_case(case, chats_items) for case in cases]
            metrics = compute_metrics(case_results)
            gate = normalize_metrics_gate(metrics_gate or DEFAULT_METRICS_GATE)
            gate_result = {
                "factual_consistency_pass": metrics["factual_consistency"] >= gate["factual_consistency_min"],
                "violation_rate_pass": metrics["violation_rate"] <= gate["violation_rate_max"],
                "handoff_precision_pass": metrics["handoff_precision"] >= gate["handoff_precision_min"],
                "continue_chat_rate_pass": metrics["continue_chat_rate"] >= gate["continue_chat_rate_min"],
            }
            gate_pass = all(gate_result.values())

            eval_job_id = "eval_job_" + digest(f"{requested_tenant}:{release_version}:{suite_payload.get('suite_id')}:{now_iso()}", length=12)
            report = {
                "ok": True,
                "report_id": eval_job_id,
                "eval_job_id": eval_job_id,
                "status": "completed",
                "tenant_id": requested_tenant,
                "release_version": str(release_version or "").strip(),
                "suite_id": suite_payload.get("suite_id"),
                "suite_file": suite_payload.get("suite_file"),
                "summary": {
                    "total_cases": len(case_results),
                    "passed_cases": sum(1 for item in case_results if item.get("passed")),
                    "failed_cases": sum(1 for item in case_results if not item.get("passed")),
                },
                "metrics": metrics,
                "metrics_gate": gate,
                "gate_result": gate_result,
                "gate_pass": gate_pass,
                "failed_cases": [item for item in case_results if not item.get("passed")],
                "results": case_results,
                "generated_at": now_iso(),
            }
            write_json(self.replay_eval_root() / f"{eval_job_id}.json", report)
            write_json(self.replay_eval_root() / f"eval_report_{release_version or eval_job_id}.json", report)
            return report

    def get_replay_eval_job(self, eval_job_id: str) -> dict[str, Any] | None:
        return read_json(self.replay_eval_root() / f"{eval_job_id}.json")

    def list_replay_eval_jobs(self, *, limit: int = 20) -> list[dict[str, Any]]:
        return list_json_files(self.replay_eval_root(), limit=limit)

    def create_release(
        self,
        *,
        tenant_id: str,
        release_version: str,
        import_job_ids: list[str],
        feature_flags: dict[str, Any] | None = None,
        industry_id: str = "",
        metrics_gate: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        requested_tenant = active_tenant_id(tenant_id or self.tenant_id)
        with tenant_context(requested_tenant):
            normalized_import_job_ids = list(dict.fromkeys([str(item or "").strip() for item in import_job_ids if str(item or "").strip()]))
            if not normalized_import_job_ids:
                return {"ok": False, "message": "import_job_ids is required"}
            missing_jobs: list[str] = []
            invalid_jobs: list[dict[str, str]] = []
            apply_jobs: list[dict[str, Any]] = []
            for job_id in normalized_import_job_ids:
                job = self.get_import_job(job_id)
                if not job:
                    missing_jobs.append(job_id)
                    continue
                if str(job.get("tenant_id") or requested_tenant) != requested_tenant:
                    invalid_jobs.append({"job_id": job_id, "reason": "tenant_mismatch"})
                    continue
                mode = str(job.get("mode") or "")
                status = str(job.get("status") or "")
                if mode != "apply":
                    invalid_jobs.append({"job_id": job_id, "reason": f"mode_not_apply:{mode or 'unknown'}"})
                    continue
                if status != "completed" or not bool(job.get("ok")):
                    invalid_jobs.append({"job_id": job_id, "reason": f"apply_not_completed:{status or 'unknown'}"})
                    continue
                apply_jobs.append(job)
            if missing_jobs or invalid_jobs:
                return {
                    "ok": False,
                    "message": "import jobs are invalid",
                    "missing_import_jobs": missing_jobs,
                    "invalid_import_jobs": invalid_jobs,
                }
            normalized_release_version = str(release_version or "").strip() or "wf_" + datetime.now().strftime("%Y%m%d_%H%M%S")
            release_id = "release_" + digest(f"{requested_tenant}:{normalized_release_version}:{now_iso()}", length=12)
            rollback_to = ""
            apply_jobs.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
            for job in apply_jobs:
                rollback_to = str(job.get("rollback_to") or "").strip()
                if rollback_to:
                    break
            if not rollback_to:
                return {"ok": False, "message": "rollback snapshot missing in apply import jobs"}
            payload = {
                "ok": True,
                "release_id": release_id,
                "status": "created",
                "tenant_id": requested_tenant,
                "industry_id": str(industry_id or "").strip(),
                "release_version": normalized_release_version,
                "import_job_ids": normalized_import_job_ids,
                "feature_flags": feature_flags or {},
                "metrics_gate": normalize_metrics_gate(metrics_gate or DEFAULT_METRICS_GATE),
                "rollback_to": rollback_to,
                "approved_by": "",
                "approved_at": "",
                "approval_note": "",
                "created_at": now_iso(),
                "updated_at": now_iso(),
            }
            write_json(self.release_versions_root() / f"{release_id}.json", payload)
            return payload

    def get_release(self, release_id: str) -> dict[str, Any] | None:
        return read_json(self.release_versions_root() / f"{release_id}.json")

    def list_releases(self, *, limit: int = 20) -> list[dict[str, Any]]:
        return list_json_files(self.release_versions_root(), limit=limit)

    def approve_release(self, *, release_id: str, approved_by: str, approval_note: str = "") -> dict[str, Any]:
        release = self.get_release(release_id)
        if not release:
            return {"ok": False, "message": f"release not found: {release_id}"}
        if str(release.get("status") or "") not in {"created", "failed_gate"}:
            return {"ok": False, "message": f"release cannot be approved in status: {release.get('status')}"}
        release_version = str(release.get("release_version") or "")
        release_created_at = str(release.get("created_at") or "")
        eval_report = self.find_latest_eval_report(release_version=release_version, generated_after=release_created_at)
        if not eval_report:
            eval_report = self.find_latest_eval_report(release_version=release_version)
        if not eval_report:
            release["status"] = "failed_gate"
            release["updated_at"] = now_iso()
            write_json(self.release_versions_root() / f"{release_id}.json", release)
            return {"ok": False, "message": "missing replay eval report for release version", "release": release}
        eval_total_cases = safe_int(((eval_report.get("summary") or {}).get("total_cases")), default=0)
        if eval_total_cases <= 0:
            release["status"] = "failed_gate"
            release["updated_at"] = now_iso()
            write_json(self.release_versions_root() / f"{release_id}.json", release)
            return {"ok": False, "message": "replay eval suite has no effective cases", "release": release, "eval_report_id": eval_report.get("eval_job_id")}
        if not bool(eval_report.get("gate_pass")):
            release["status"] = "failed_gate"
            release["updated_at"] = now_iso()
            write_json(self.release_versions_root() / f"{release_id}.json", release)
            return {"ok": False, "message": "replay eval gate not passed", "release": release, "eval_report_id": eval_report.get("eval_job_id")}

        release["status"] = "approved"
        release["approved_by"] = str(approved_by or "admin")
        release["approved_at"] = now_iso()
        release["approval_note"] = str(approval_note or "")
        release["updated_at"] = now_iso()
        release["eval_report_id"] = eval_report.get("eval_job_id")
        write_json(self.release_versions_root() / f"{release_id}.json", release)
        return {"ok": True, "release": release}

    def rollback_release(self, *, release_id: str, rollback_to_version: str = "", reason: str = "") -> dict[str, Any]:
        release = self.get_release(release_id)
        if not release:
            return {"ok": False, "message": f"release not found: {release_id}"}
        target_version = str(rollback_to_version or release.get("rollback_to") or "").strip()
        if not target_version:
            return {"ok": False, "message": "rollback_to_version is required"}
        with tenant_context(str(release.get("tenant_id") or self.tenant_id)):
            result = self.version_store.rollback(target_version)
        if not result.get("ok"):
            return {"ok": False, "message": "version rollback failed", "detail": result}
        release["status"] = "rolled_back"
        release["rolled_back_to"] = target_version
        release["rollback_reason"] = str(reason or "")
        release["rollback_at"] = now_iso()
        release["updated_at"] = now_iso()
        write_json(self.release_versions_root() / f"{release_id}.json", release)
        return {"ok": True, "release": release, "version_rollback": result}

    def find_latest_eval_report(self, *, release_version: str, generated_after: str = "") -> dict[str, Any] | None:
        reports = self.list_replay_eval_jobs(limit=100)
        matched = [item for item in reports if str(item.get("release_version") or "") == release_version]
        if generated_after:
            matched = [
                item
                for item in matched
                if str(item.get("generated_at") or item.get("created_at") or "") >= generated_after
            ]
        if not matched:
            return None
        matched.sort(key=lambda item: str(item.get("generated_at") or item.get("created_at") or ""), reverse=True)
        return matched[0]

    # -------------------------------
    # shared filesystem layout
    # -------------------------------

    def learning_pack_root(self) -> Path:
        return tenant_root(self.tenant_id) / "learning_packs"

    def curated_templates_root(self) -> Path:
        return self.learning_pack_root() / "curated_templates"

    def curation_jobs_root(self) -> Path:
        return self.learning_pack_root() / "curation_jobs"

    def curation_reports_root(self) -> Path:
        return self.learning_pack_root() / "curation_reports"

    def import_jobs_root(self) -> Path:
        return self.learning_pack_root() / "import_jobs"

    def import_reports_root(self) -> Path:
        return self.learning_pack_root() / "import_reports"

    def release_versions_root(self) -> Path:
        return tenant_runtime_root(self.tenant_id) / "release_versions"

    def replay_eval_root(self) -> Path:
        return tenant_runtime_root(self.tenant_id) / "replay_eval"

    def _load_eval_suite(self, *, suite_id: str = "", suite_file: str = "") -> dict[str, Any]:
        if suite_file:
            path = Path(str(suite_file or "")).expanduser()
        else:
            normalized_suite = str(suite_id or "default_usedcar_suite").strip()
            path = APP_ROOT / "data" / "learning_suites" / f"{normalized_suite}.json"
        if not path.exists():
            return {"ok": False, "message": f"eval suite not found: {path}"}
        payload = read_json(path)
        if not isinstance(payload, dict):
            return {"ok": False, "message": f"invalid eval suite payload: {path}"}
        cases = payload.get("cases")
        if not isinstance(cases, list):
            return {"ok": False, "message": f"eval suite cases must be list: {path}"}
        return {
            "ok": True,
            "suite_id": str(payload.get("suite_id") or path.stem),
            "suite_file": str(path),
            "cases": [item for item in cases if isinstance(item, dict)],
        }


def normalize_candidate(candidate: dict[str, Any]) -> dict[str, Any] | None:
    customer = str(candidate.get("customer_message") or "").strip()
    reply = str(candidate.get("service_reply") or "").strip()
    if not customer or not reply:
        return None
    if short_low_value(customer) or short_low_value(reply):
        return None
    normalized = dict(candidate)
    normalized["customer_message"] = compact_text(customer)
    normalized["service_reply"] = compact_text(reply)
    normalized["intent_tags"] = normalize_string_list(candidate.get("intent_tags"))
    normalized["tone_tags"] = normalize_string_list(candidate.get("tone_tags"))
    normalized["source_samples"] = normalize_string_list(candidate.get("source_samples"))
    normalized["scenario"] = str(candidate.get("scenario") or "").strip()
    normalized["hit_count"] = safe_int(candidate.get("hit_count"), default=1)
    return normalized


def extract_dialogue_candidates(record: dict[str, Any]) -> list[dict[str, Any]]:
    pairs: list[dict[str, Any]] = []
    direct_customer = pick_text(record, ("customer_message", "customer", "question", "query", "ask"))
    direct_reply = pick_text(record, ("service_reply", "reply", "answer", "assistant_reply"))
    if direct_customer and direct_reply:
        pairs.append(build_candidate_from_record(record, direct_customer, direct_reply))
        return pairs

    messages_value = record.get("messages")
    if isinstance(messages_value, list):
        pairs.extend(pairs_from_messages(messages_value, record))
        if pairs:
            return pairs
    dialogue_value = record.get("dialogue")
    if isinstance(dialogue_value, list):
        pairs.extend(pairs_from_messages(dialogue_value, record))
        if pairs:
            return pairs

    fragments = normalize_string_list(record.get("source_samples"))
    if len(fragments) >= 2:
        pairs.append(build_candidate_from_record(record, fragments[0], fragments[1]))
    return pairs


def pairs_from_messages(messages: list[Any], record: dict[str, Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    pending_customer = ""
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or message.get("sender_role") or message.get("sender") or "").strip().lower()
        content = pick_text(message, ("content", "text", "message"))
        if not content:
            continue
        if role in {"user", "customer", "client", "buyer", "other"}:
            pending_customer = content
            continue
        if role in {"assistant", "service", "agent", "seller", "bot", "ai"} and pending_customer:
            result.append(build_candidate_from_record(record, pending_customer, content))
            pending_customer = ""
    return result


def build_candidate_from_record(record: dict[str, Any], customer: str, reply: str) -> dict[str, Any]:
    return {
        "id": str(record.get("id") or record.get("template_id") or "").strip(),
        "customer_message": customer,
        "service_reply": reply,
        "intent_tags": normalize_string_list(record.get("intent_tags") or record.get("tags")),
        "tone_tags": normalize_string_list(record.get("tone_tags")),
        "scenario": str(record.get("scenario") or record.get("scene") or "").strip(),
        "hit_count": safe_int(record.get("hit_count"), default=1),
        "source_samples": normalize_string_list(record.get("source_samples")),
        "source_label": str(record.get("source_label") or record.get("source") or "实盘聊天").strip(),
    }


def build_template_item(
    *,
    category_id: str,
    customer_message: str,
    service_reply: str,
    batch_token: str,
    industry_id: str,
    hint_id: str,
    intent_tags: list[str],
    tone_tags: list[str],
    scenario: str,
    hit_count: int,
    source_samples: list[str],
) -> dict[str, Any]:
    item_id = sanitize_item_id(hint_id)
    if not item_id:
        scope = sanitize_item_id(industry_id) or "generic"
        item_id = f"{scope}_tpl_{digest(customer_message + '||' + service_reply, length=12)}"
    requires_handoff = needs_handoff(service_reply)
    risk_level = "high" if requires_handoff else "normal"
    now_text = now_iso()
    return {
        "schema_version": 1,
        "category_id": category_id,
        "id": item_id,
        "status": "active",
        "source": {
            "type": "cleaned_real_chat_pack",
            "batch_token": batch_token,
        },
        "data": {
            "customer_message": customer_message,
            "service_reply": service_reply,
            "intent_tags": intent_tags,
            "tone_tags": tone_tags,
            "linked_categories": ["products", "policies"],
            "linked_item_ids": [],
            "applicability_scope": "industry",
            "industry_id": str(industry_id or "").strip(),
            "usable_as_template": True,
            "additional_details": {
                "scenario": scenario,
                "hit_count": max(1, int(hit_count or 1)),
                "source_samples": source_samples[:5],
            },
        },
        "runtime": {
            "allow_auto_reply": not requires_handoff,
            "requires_handoff": requires_handoff,
            "risk_level": risk_level,
        },
        "metadata": {
            "created_at": now_text,
            "updated_at": now_text,
            "created_by": "workflow_curation",
            "updated_by": "workflow_curation",
        },
    }


def normalize_template_payload(item: dict[str, Any], *, default_category: str) -> dict[str, Any]:
    payload = dict(item)
    payload["schema_version"] = int(payload.get("schema_version") or 1)
    payload["category_id"] = str(payload.get("category_id") or default_category)
    payload["id"] = sanitize_item_id(str(payload.get("id") or "")) or f"tpl_{digest(json.dumps(payload, ensure_ascii=False, sort_keys=True), length=12)}"
    source = payload.get("source") if isinstance(payload.get("source"), dict) else {}
    source.setdefault("type", "workflow_import")
    payload["source"] = source

    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    customer_message = compact_text(str(data.get("customer_message") or payload.get("customer_message") or ""))
    service_reply = compact_text(str(data.get("service_reply") or payload.get("service_reply") or ""))
    data["customer_message"] = customer_message
    data["service_reply"] = service_reply
    data["intent_tags"] = normalize_string_list(data.get("intent_tags"))
    data["tone_tags"] = normalize_string_list(data.get("tone_tags"))
    details = data.get("additional_details") if isinstance(data.get("additional_details"), dict) else {}
    details["source_samples"] = normalize_string_list(details.get("source_samples"))
    data["additional_details"] = details
    payload["data"] = data

    runtime = payload.get("runtime") if isinstance(payload.get("runtime"), dict) else {}
    runtime.setdefault("allow_auto_reply", True)
    runtime.setdefault("requires_handoff", False)
    runtime.setdefault("risk_level", "normal")
    payload["runtime"] = runtime

    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    now_text = now_iso()
    metadata.setdefault("created_at", now_text)
    metadata["updated_at"] = now_text
    metadata.setdefault("created_by", "workflow_import")
    metadata.setdefault("updated_by", "workflow_import")
    payload["metadata"] = metadata
    return payload


def real_chat_formal_import_block_reason(item: dict[str, Any]) -> str:
    """Return a human-readable reason when an item must stay out of formal knowledge."""
    source = item.get("source") if isinstance(item.get("source"), dict) else {}
    data = item.get("data") if isinstance(item.get("data"), dict) else {}
    details = data.get("additional_details") if isinstance(data.get("additional_details"), dict) else {}
    values = {
        str(source.get("type") or ""),
        str(source.get("original_type") or ""),
        str(source.get("source_type") or ""),
        str(source.get("candidate_source_type") or ""),
        str(details.get("source_type") or ""),
    }
    source_type_hits = sorted(value for value in values if value in REAL_CHAT_FORMAL_SOURCE_TYPES)
    if source_type_hits:
        return (
            "检测到实盘/微信聊天来源 "
            + ", ".join(source_type_hits)
            + "；这类材料只能进入AI经验池和实盘话术风格层，不能直接写入正式知识库。"
        )

    item_id = str(item.get("id") or "")
    if any(item_id.startswith(prefix) for prefix in REAL_CHAT_ID_PREFIXES):
        return "检测到实盘聊天批次ID；请进入AI经验池/话术风格层，不能直接写入正式知识库。"

    batch_token = " ".join(
        str(value or "")
        for value in (
            source.get("batch_token"),
            details.get("batch_token"),
            details.get("source_hint_id"),
            details.get("cleaning_kind"),
        )
    ).lower()
    if any(marker.lower() in batch_token for marker in REAL_CHAT_BATCH_MARKERS):
        return "检测到实盘聊天批次标记；请进入AI经验池/话术风格层，不能直接写入正式知识库。"
    return ""


def evaluate_case(case: dict[str, Any], chats_items: list[dict[str, Any]]) -> dict[str, Any]:
    case_id = str(case.get("case_id") or f"case_{digest(json.dumps(case, ensure_ascii=False, sort_keys=True), length=8)}")
    query = case_query(case)
    best = find_best_chat_item(query, chats_items)
    best_item = best.get("item") if isinstance(best.get("item"), dict) else {}
    match_score = round(float(best.get("score") or 0.0), 4)
    reply = str(((best_item.get("data") or {}).get("service_reply")) or "")
    predicted_handoff = bool(((best_item.get("runtime") or {}).get("requires_handoff")) or needs_handoff(reply))
    constraints = case.get("expected_constraints") if isinstance(case.get("expected_constraints"), dict) else {}

    required_terms = normalize_string_list(constraints.get("required_terms"))
    forbidden_terms = normalize_string_list(constraints.get("forbidden_terms"))
    require_handoff = constraints.get("require_handoff") if isinstance(constraints.get("require_handoff"), bool) else None
    factual_reference_terms = normalize_string_list(constraints.get("factual_reference_terms"))

    reasons: list[str] = []
    matched_item_id = str(best_item.get("id") or "")
    if not matched_item_id or (query and match_score < MATCH_SCORE_MIN):
        reasons.append("no_matching_template")
    for term in required_terms:
        if term and term not in reply:
            reasons.append(f"missing_required_term:{term}")
    for term in factual_reference_terms:
        if term and term not in reply:
            reasons.append(f"missing_factual_term:{term}")
    for term in forbidden_terms:
        if term and forbidden_term_hit(reply, term):
            reasons.append(f"forbidden_term_hit:{term}")
    if require_handoff is True and not predicted_handoff:
        reasons.append("handoff_expected_but_not_triggered")
    if require_handoff is False and predicted_handoff:
        reasons.append("handoff_not_expected_but_triggered")

    passed = len(reasons) == 0
    violated = any(reason.startswith("forbidden_term_hit:") for reason in reasons) or "handoff_expected_but_not_triggered" in reasons
    return {
        "case_id": case_id,
        "query": query,
        "matched_item_id": matched_item_id,
        "match_score": match_score,
        "predicted_reply": reply,
        "predicted_requires_handoff": predicted_handoff,
        "expected_constraints": constraints,
        "passed": passed,
        "violated": violated,
        "reasons": reasons,
    }


def compute_metrics(case_results: list[dict[str, Any]]) -> dict[str, float]:
    total = max(1, len(case_results))
    passed = sum(1 for item in case_results if item.get("passed"))
    violations = sum(1 for item in case_results if item.get("violated"))
    continue_chat_eligible = 0
    continue_chat_pass = 0

    expected_handoff = 0
    predicted_handoff = 0
    true_positive = 0
    for item in case_results:
        expected = item.get("expected_constraints") if isinstance(item.get("expected_constraints"), dict) else {}
        expected_value = expected.get("require_handoff")
        predicted_value = bool(item.get("predicted_requires_handoff"))
        if not (isinstance(expected_value, bool) and expected_value):
            continue_chat_eligible += 1
            if str(item.get("predicted_reply") or "").strip() and not predicted_value:
                continue_chat_pass += 1
        if isinstance(expected_value, bool) and expected_value:
            expected_handoff += 1
        if predicted_value:
            predicted_handoff += 1
        if isinstance(expected_value, bool) and expected_value and predicted_value:
            true_positive += 1

    if predicted_handoff == 0:
        handoff_precision = 1.0 if expected_handoff == 0 else 0.0
    else:
        handoff_precision = true_positive / float(predicted_handoff)

    continue_chat_rate = 1.0 if continue_chat_eligible == 0 else continue_chat_pass / float(continue_chat_eligible)

    return {
        "factual_consistency": round(passed / float(total), 4),
        "violation_rate": round(violations / float(total), 4),
        "handoff_precision": round(handoff_precision, 4),
        "continue_chat_rate": round(continue_chat_rate, 4),
    }


def forbidden_term_hit(reply: str, term: str) -> bool:
    text = str(reply or "")
    target = str(term or "").strip()
    if not target or target not in text:
        return False
    for start in find_all_occurrences(text, target):
        if not forbidden_term_is_negated(text, start):
            return True
    return False


def forbidden_term_is_negated(text: str, hit_start: int) -> bool:
    clause_start = 0
    for separator in FORBIDDEN_TERM_CLAUSE_SEPARATORS:
        index = text.rfind(separator, 0, hit_start)
        if index >= 0 and index + 1 > clause_start:
            clause_start = index + 1
    prefix = re.sub(r"\s+", "", text[clause_start:hit_start])
    if not prefix:
        return False
    for token in FORBIDDEN_TERM_NEGATION_TOKENS:
        if prefix.endswith(token):
            return True
        for bridge in FORBIDDEN_TERM_NEGATION_BRIDGES:
            if prefix.endswith(token + bridge):
                return True
    return False


def find_all_occurrences(text: str, target: str) -> Iterable[int]:
    offset = 0
    while True:
        index = text.find(target, offset)
        if index < 0:
            return
        yield index
        offset = index + max(1, len(target))


def case_query(case: dict[str, Any]) -> str:
    input_messages = case.get("input_messages")
    if isinstance(input_messages, list):
        best = ""
        for message in input_messages:
            if isinstance(message, str) and message.strip():
                best = message.strip()
                continue
            if isinstance(message, dict):
                role = str(message.get("role") or "").strip().lower()
                content = pick_text(message, ("content", "text", "message"))
                if not content:
                    continue
                if role in {"user", "customer", "client", "buyer"}:
                    best = content
                elif not best:
                    best = content
        if best:
            return compact_text(best, limit=200)
    if isinstance(case.get("query"), str) and case.get("query").strip():
        return compact_text(str(case.get("query")).strip(), limit=200)
    return ""


def find_best_chat_item(query: str, items: list[dict[str, Any]]) -> dict[str, Any]:
    query_text = compact_text(query, limit=200)
    query_tokens = token_set(query_text)
    best_item: dict[str, Any] | None = None
    best_score = 0.0
    for item in items:
        data = item.get("data") if isinstance(item.get("data"), dict) else {}
        customer_message = compact_text(str(data.get("customer_message") or ""), limit=200)
        if query_text and customer_message and query_text in customer_message:
            return {"item": item, "score": 1.0}
        if query_text and customer_message and customer_message in query_text:
            score = 0.98
        else:
            item_tokens = token_set(customer_message)
            overlap = len(query_tokens & item_tokens)
            query_overlap = overlap / float(len(query_tokens)) if query_tokens else 0.0
            score = (jaccard(query_tokens, item_tokens) * 0.6) + (query_overlap * 0.4)
        if score > best_score:
            best_score = score
            best_item = item
    return {"item": best_item or {}, "score": best_score}


def normalize_metrics_gate(payload: dict[str, Any]) -> dict[str, float]:
    gate = dict(DEFAULT_METRICS_GATE)
    for key in gate:
        if key in payload:
            try:
                gate[key] = float(payload[key])
            except (TypeError, ValueError):
                continue
    return gate


def sanitize_text(value: str) -> tuple[str, int, int]:
    text = compact_text(value)
    phone_hits = len(PHONE_RE.findall(text))
    text = PHONE_RE.sub("[联系方式已脱敏]", text)

    name_hits = 0

    def replace_name(match: re.Match[str]) -> str:
        nonlocal name_hits
        name_hits += 1
        return f"{match.group(1)}[姓名已脱敏]"

    text = NAMED_PERSON_RE_COLON.sub(replace_name, text)
    text = NAMED_PERSON_RE_INLINE.sub(replace_name, text)
    return text, phone_hits, name_hits


def has_phone(value: str) -> bool:
    return bool(PHONE_RE.search(str(value or "")))


def contains_metadata_pollution(value: str) -> bool:
    text = str(value or "")
    return any(term in text for term in METADATA_POLLUTION_TERMS)


def looks_like_noise(customer_message: str, service_reply: str) -> bool:
    joined = f"{customer_message}\n{service_reply}"
    return any(pattern in joined for pattern in NOISE_PATTERNS)


def needs_handoff(reply: str) -> bool:
    text = str(reply or "")
    return any(
        keyword in text
        for keyword in (
            "转人工",
            "人工跟进",
            "请人工",
            "请联系顾问",
            "人工核实",
            "人工顾问",
            "一对一核实",
        )
    )


def short_low_value(value: str) -> bool:
    text = compact_text(value)
    if len(text) <= 1:
        return True
    no_word = re.sub(r"[\u4e00-\u9fffA-Za-z0-9]+", "", text)
    return len(text) <= 3 and len(no_word) >= len(text) - 1


def normalize_string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        result = []
        for item in value:
            text = str(item or "").strip()
            if text:
                result.append(text)
        return result
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        if "、" in text:
            return [item.strip() for item in text.split("、") if item.strip()]
        if "," in text:
            return [item.strip() for item in text.split(",") if item.strip()]
        return [text]
    return []


def normalize_sort_key(payload: dict[str, Any]) -> str:
    return str(payload.get("created_at") or payload.get("generated_at") or payload.get("updated_at") or "")


def list_json_files(root: Path, *, limit: int) -> list[dict[str, Any]]:
    if not root.exists():
        return []
    items: list[dict[str, Any]] = []
    for path in root.glob("*.json"):
        payload = read_json(path)
        if isinstance(payload, dict):
            items.append(payload)
    items.sort(key=normalize_sort_key, reverse=True)
    return items[: max(1, int(limit or 20))]


def item_signature(item: dict[str, Any]) -> str:
    data = item.get("data") if isinstance(item.get("data"), dict) else {}
    runtime = item.get("runtime") if isinstance(item.get("runtime"), dict) else {}
    payload = {"data": data, "runtime": runtime}
    return digest(json.dumps(payload, ensure_ascii=False, sort_keys=True), length=20)


def sanitize_item_id(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    cleaned = re.sub(r"\s+", "_", text)
    cleaned = re.sub(r"[^A-Za-z0-9_.-]", "_", cleaned)
    cleaned = cleaned.strip("._-")
    if not cleaned:
        return ""
    if not SAFE_ID_RE.fullmatch(cleaned):
        return ""
    return cleaned


def sanitize_batch_id(value: str) -> str:
    text = sanitize_item_id(value)
    if text:
        return text
    return "batch_" + datetime.now().strftime("%Y%m%d_%H%M%S")


def compact_text(value: str, *, limit: int = 1000) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text[:limit]


def token_set(value: str) -> set[str]:
    text = str(value or "").strip().lower()
    if not text:
        return set()
    tokens = re.findall(r"[a-z0-9]+|[\u4e00-\u9fff]", text)
    return {token for token in tokens if token}


def jaccard(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    union = left | right
    if not union:
        return 0.0
    return len(left & right) / float(len(union))


def pick_text(payload: dict[str, Any], keys: Iterable[str]) -> str:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def safe_int(value: Any, *, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def bump(counter: dict[str, int], key: str) -> None:
    counter[key] = int(counter.get(key, 0) or 0) + 1


def digest(text: str, *, length: int = 16) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[: max(4, int(length or 16))]


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def iter_jsonl(path: Path) -> Iterable[tuple[int, dict[str, Any]]]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return
    for index, line in enumerate(text.splitlines(), start=1):
        raw = line.strip()
        if not raw:
            continue
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            yield index, payload


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(row, ensure_ascii=False) for row in rows]
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def iter_source_records(path: Path) -> Iterable[dict[str, Any]]:
    suffix = path.suffix.lower()
    if suffix in {".jsonl", ".json"}:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return
        if suffix == ".jsonl":
            for line in text.splitlines():
                raw = line.strip()
                if not raw:
                    continue
                try:
                    payload = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if isinstance(payload, dict):
                    yield payload
            return
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return
        if isinstance(payload, list):
            for item in payload:
                if isinstance(item, dict):
                    yield item
            return
        if isinstance(payload, dict):
            nested = payload.get("items")
            if isinstance(nested, list):
                for item in nested:
                    if isinstance(item, dict):
                        yield item
                return
            yield payload
            return
    # fallback plain text: split by blank lines and infer customer/reply pairs
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return
    blocks = [block.strip() for block in re.split(r"\n\s*\n", text) if block.strip()]
    for block in blocks:
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        if len(lines) < 2:
            continue
        yield {"customer_message": lines[0], "service_reply": lines[1], "source_samples": lines[:4]}
