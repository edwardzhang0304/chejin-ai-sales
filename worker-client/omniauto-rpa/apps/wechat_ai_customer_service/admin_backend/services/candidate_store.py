"""Review-candidate management and safe application."""

from __future__ import annotations

import json
import os
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from .audit_log import append_audit
from .candidate_badges import enrich_candidate
from .diagnostics_service import DiagnosticsService
from .draft_store import DraftStore
from .formal_review_state import mark_item_new
from .knowledge_base_store import KnowledgeBaseStore
from .knowledge_compiler import KnowledgeCompiler
from .knowledge_deduper import KnowledgeDeduper, normalize_key, normalize_price_tiers
from .source_authority_policy import evaluate_candidate_source_authority, mark_candidate_source_policy
from .version_store import VersionStore
from apps.wechat_ai_customer_service.knowledge_paths import active_tenant_id, tenant_review_candidates_root
from apps.wechat_ai_customer_service.platform_understanding_rules import intent_keywords
from apps.wechat_ai_customer_service.storage import get_postgres_store, load_storage_config
from apps.wechat_ai_customer_service.workflows.knowledge_intake import evaluate_intake_item


APP_ROOT = Path(__file__).resolve().parents[2]
STRUCTURED_ROOT = APP_ROOT / "data" / "structured"
TARGET_FILES = {
    "product_knowledge": STRUCTURED_ROOT / "product_knowledge.example.json",
    "style_examples": STRUCTURED_ROOT / "style_examples.json",
}
PRODUCT_SCOPED_TARGET_CATEGORIES = {"product_faq", "product_rules", "product_explanations"}
PRODUCT_MASTER_TARGET_CATEGORIES = {"products", "erp_exports"}


class CandidateStore:
    def __init__(self) -> None:
        self.diagnostics = DiagnosticsService()
        self.versions = VersionStore()
        self.compiler = KnowledgeCompiler()
        self.draft_store = DraftStore()
        self.base_store = KnowledgeBaseStore()
        self.deduper = KnowledgeDeduper(self.base_store)

    def list_candidates(self, status: str, *, compact: bool = False) -> list[dict[str, Any]]:
        db_items: list[dict[str, Any]] = []
        db = postgres_store()
        if db:
            db_items = db.list_candidates(active_tenant_id(), status=status)
            db_items = filter_candidates_by_status(db_items, status)
        root = self.status_root(status)
        items = [json.loads(path.read_text(encoding="utf-8")) for path in sorted(root.glob("*.json"), reverse=True)] if root.exists() else []
        items = filter_candidates_by_status(items, status)
        enriched = [enrich_candidate(item) for item in merge_candidates(db_items, items)]
        if compact:
            return [compact_candidate(item) for item in enriched]
        return enriched

    def get_candidate(self, candidate_id: str) -> dict[str, Any] | None:
        path = self.find_path(candidate_id)
        if not path:
            return None
        return enrich_candidate(json.loads(path.read_text(encoding="utf-8")))

    def update_candidate(self, candidate_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        path = self.require_path(candidate_id)
        candidate = json.loads(path.read_text(encoding="utf-8"))
        candidate.update(patch)
        candidate.setdefault("review", {})["updated_at"] = datetime.now().isoformat(timespec="seconds")
        path.write_text(json.dumps(candidate, ensure_ascii=False, indent=2), encoding="utf-8")
        upsert_candidate_to_db(candidate)
        append_audit("candidate_updated", {"candidate_id": candidate_id})
        return {"ok": True, "item": enrich_candidate(candidate)}

    def supplement_candidate(self, candidate_id: str, data: dict[str, Any]) -> dict[str, Any]:
        path = self.require_path(candidate_id)
        candidate = json.loads(path.read_text(encoding="utf-8"))
        patch = self.formal_patch(candidate)
        if not patch:
            return {"ok": False, "message": "candidate has no formal_patch"}
        target_category = str(patch.get("target_category") or "")
        if target_category in PRODUCT_MASTER_TARGET_CATEGORIES:
            return {"ok": False, "message": "商品资料属于权威主数据，禁止通过候选补充链路维护；请在商品库手动更新。"}
        item = patch.get("item")
        if not target_category or not isinstance(item, dict):
            return {"ok": False, "message": "native candidate item is required"}
        item_data = item.get("data") if isinstance(item.get("data"), dict) else {}
        item["data"] = merge_non_empty_dicts(item_data, data if isinstance(data, dict) else {})
        item = self.merge_existing_context(target_category, item)
        patch["item"] = item
        self.reassess_native_candidate(candidate, target_category, item)
        candidate.setdefault("review", {})["updated_at"] = datetime.now().isoformat(timespec="seconds")
        path.write_text(json.dumps(candidate, ensure_ascii=False, indent=2), encoding="utf-8")
        upsert_candidate_to_db(candidate)
        append_audit("candidate_supplemented", {"candidate_id": candidate_id, "target_category": target_category})
        return {"ok": True, "item": enrich_candidate(candidate)}

    def change_candidate_category(self, candidate_id: str, target_category: str) -> dict[str, Any]:
        path = self.require_path(candidate_id)
        candidate = json.loads(path.read_text(encoding="utf-8"))
        target_category = str(target_category or "").strip()
        if target_category in PRODUCT_MASTER_TARGET_CATEGORIES:
            return {"ok": False, "message": "商品资料属于权威主数据，不能通过候选分类改写；请在商品库手动导入/维护。"}
        schema = self.base_store.schema_manager.load_schema(target_category)
        patch = self.formal_patch(candidate)
        if not patch:
            return {"ok": False, "message": "candidate has no formal_patch"}
        source_item = patch.get("item") if isinstance(patch.get("item"), dict) else {}
        source_data = source_item.get("data") if isinstance(source_item.get("data"), dict) else {}
        evidence = str((candidate.get("source") or {}).get("evidence_excerpt") or "")
        transformed_data = transform_candidate_data_for_category(source_data, target_category, evidence, schema)
        if target_category in PRODUCT_SCOPED_TARGET_CATEGORIES:
            product_id = str(transformed_data.get("product_id") or "").strip()
            if not product_id or not self.base_store.get_item("products", product_id):
                return {
                    "ok": False,
                    "message": "商品专属知识必须先绑定到一个已存在商品。请从商品库的商品详情里维护专属问答、规则或解释。",
                }
        item_id = safe_candidate_item_id(
            str(
                transformed_data.get("sku")
                or transformed_data.get("name")
                or transformed_data.get("title")
                or transformed_data.get("external_id")
                or source_item.get("id")
                or candidate_id
            ),
            fallback_seed=f"{candidate_id}:{target_category}:{transformed_data}",
        )
        runtime = source_item.get("runtime") if isinstance(source_item.get("runtime"), dict) else {}
        item = {
            "schema_version": 1,
            "category_id": target_category,
            "id": item_id,
            "status": str(source_item.get("status") or "active"),
            "source": {**(source_item.get("source") or {}), "reclassified_from": str(patch.get("target_category") or "")},
            "data": transformed_data,
            "runtime": normalize_runtime_for_category(target_category, transformed_data, runtime),
        }
        patch.update({"target_category": target_category, "operation": "upsert_item", "item": item})
        proposal = candidate.setdefault("proposal", {})
        proposal["target_category"] = target_category
        proposal["change_type"] = f"manual_reclassify_to_{target_category}"
        proposal["summary"] = f"建议入库：{target_category} / {primary_candidate_title(transformed_data, item_id)}"
        self.reassess_native_candidate(candidate, target_category, item)
        candidate.setdefault("review", {})["updated_at"] = datetime.now().isoformat(timespec="seconds")
        path.write_text(json.dumps(candidate, ensure_ascii=False, indent=2), encoding="utf-8")
        upsert_candidate_to_db(candidate)
        append_audit("candidate_category_changed", {"candidate_id": candidate_id, "target_category": target_category})
        return {"ok": True, "item": enrich_candidate(candidate)}

    def reject(self, candidate_id: str, reason: str) -> dict[str, Any]:
        source = self.require_path(candidate_id)
        candidate = json.loads(source.read_text(encoding="utf-8"))
        candidate.setdefault("review", {}).update(
            {"status": "rejected", "rejected_at": datetime.now().isoformat(timespec="seconds"), "reason": reason}
        )
        target = self.status_root("rejected") / source.name
        self.move_candidate(source, target, candidate)
        append_audit("candidate_rejected", {"candidate_id": candidate_id, "reason": reason})
        return {"ok": True, "item": enrich_candidate(candidate)}

    def approve(self, candidate_id: str) -> dict[str, Any]:
        source = self.require_path(candidate_id)
        candidate = json.loads(source.read_text(encoding="utf-8"))
        candidate.setdefault("review", {}).update({"status": "approved", "approved_at": datetime.now().isoformat(timespec="seconds")})
        target = self.status_root("approved") / source.name
        self.move_candidate(source, target, candidate)
        append_audit("candidate_approved", {"candidate_id": candidate_id})
        return {"ok": True, "item": enrich_candidate(candidate)}

    def apply(self, candidate_id: str) -> dict[str, Any]:
        source = self.require_path(candidate_id)
        candidate = json.loads(source.read_text(encoding="utf-8"))
        patch = self.formal_patch(candidate)
        if not patch:
            return {"ok": False, "message": "candidate has no formal_patch"}
        target_category = str(patch.get("target_category") or "")
        if target_category:
            return self.apply_native_candidate(source, candidate, patch, target_category)
        target_file = str(patch.get("target_file") or "")
        if target_file not in TARGET_FILES:
            return {"ok": False, "message": f"unsupported target_file: {target_file}"}
        source_decision = existing_or_current_source_authority(candidate, legacy_target_category(target_file))
        mark_candidate_source_policy(candidate, source_decision)
        if not source_decision.get("allowed"):
            source.write_text(json.dumps(candidate, ensure_ascii=False, indent=2), encoding="utf-8")
            upsert_candidate_to_db(candidate)
            return {
                "ok": False,
                "message": str(source_decision.get("message") or source_decision.get("reason") or "candidate source is not authoritative"),
                "source_authority": source_decision,
            }
        content = self.current_target_content(target_file)
        apply_patch_to_content(content, patch)
        validation = self.diagnostics.validate_target_content(target_file, content)
        if not validation.get("ok"):
            return {"ok": False, "message": "validation failed", "validation": validation}
        snapshot = self.versions.create_snapshot("before candidate apply", {"candidate_id": candidate_id, "target_file": target_file})
        self.draft_store.apply_target_content(target_file, content)
        candidate.setdefault("review", {}).update(
            {
                "status": "approved",
                "applied": True,
                "applied_at": datetime.now().isoformat(timespec="seconds"),
                "version_snapshot": snapshot,
            }
        )
        target = self.status_root("approved") / source.name
        self.move_candidate(source, target, candidate)
        append_audit("candidate_applied", {"candidate_id": candidate_id, "target_file": target_file, "version_id": snapshot["version_id"]})
        return {"ok": True, "item": candidate, "snapshot": snapshot}

    def apply_native_candidate(
        self,
        source: Path,
        candidate: dict[str, Any],
        patch: dict[str, Any],
        target_category: str,
    ) -> dict[str, Any]:
        if target_category in PRODUCT_MASTER_TARGET_CATEGORIES:
            return {"ok": False, "message": "商品资料属于权威主数据，禁止通过候选知识链路写入；请在商品库手动导入/维护。"}
        source_decision = existing_or_current_source_authority(candidate, target_category)
        mark_candidate_source_policy(candidate, source_decision)
        if not source_decision.get("allowed"):
            source.write_text(json.dumps(candidate, ensure_ascii=False, indent=2), encoding="utf-8")
            upsert_candidate_to_db(candidate)
            return {
                "ok": False,
                "message": str(source_decision.get("message") or source_decision.get("reason") or "candidate source is not authoritative"),
                "source_authority": source_decision,
            }
        operation = str(patch.get("operation") or "")
        if operation != "upsert_item":
            return {"ok": False, "message": f"unsupported native operation: {operation}"}
        intake = candidate.get("intake") if isinstance(candidate.get("intake"), dict) else {}
        if intake.get("status") == "needs_more_info":
            return {
                "ok": False,
                "message": "candidate needs more information before apply",
                "intake": intake,
            }
        item = patch.get("item")
        if not isinstance(item, dict):
            return {"ok": False, "message": "native patch item is required"}
        item = self.merge_existing_context(target_category, item)
        patch["item"] = item
        duplicate = self.deduper.check_candidate(candidate)
        if duplicate.get("duplicate"):
            return {
                "ok": False,
                "message": "duplicate candidate",
                "duplicate": duplicate,
            }
        try:
            validation = self.base_store.validate_item(target_category, item)
        except (FileNotFoundError, ValueError) as exc:
            return {"ok": False, "message": str(exc)}
        if not validation.get("ok"):
            return {"ok": False, "message": "validation failed", "validation": validation}

        candidate_id = str(candidate.get("candidate_id") or "")
        item_id = str(item.get("id") or "")
        snapshot = self.versions.create_snapshot(
            "before candidate native apply",
            {"candidate_id": candidate_id, "target_category": target_category, "item_id": item_id},
        )
        item = mark_item_new(
            item,
            {
                "source_module": "candidate",
                "candidate_id": candidate_id,
                "target_category": target_category,
                "item_id": item_id,
            },
        )
        patch["item"] = item
        result = self.base_store.save_item(target_category, item)
        if not result.get("ok"):
            return {"ok": False, "message": "save failed", "validation": result}
        self.compiler.compile_to_disk()
        candidate.setdefault("review", {}).update(
            {
                "status": "approved",
                "applied": True,
                "applied_at": datetime.now().isoformat(timespec="seconds"),
                "version_snapshot": snapshot,
                "target_category": target_category,
                "item_id": item_id,
            }
        )
        target = self.status_root("approved") / source.name
        self.move_candidate(source, target, candidate)
        append_audit(
            "candidate_applied",
            {
                "candidate_id": candidate_id,
                "target_category": target_category,
                "item_id": item_id,
                "version_id": snapshot["version_id"],
            },
        )
        return {"ok": True, "item": enrich_candidate(candidate), "snapshot": snapshot, "saved_item": result.get("item")}

    def merge_existing_context(self, target_category: str, item: dict[str, Any]) -> dict[str, Any]:
        if target_category != "products":
            return item
        existing_item = self.match_existing_product(item)
        if not existing_item:
            return item
        merged_item = dict(existing_item)
        merged_item.update({key: value for key, value in item.items() if key != "data"})
        merged_item["id"] = str(existing_item.get("id") or item.get("id") or "")
        merged_item["category_id"] = "products"
        merged_item["data"] = merge_product_data(existing_item.get("data", {}) or {}, item.get("data", {}) or {})
        merged_item["runtime"] = merge_non_empty_dicts(existing_item.get("runtime", {}) or {}, item.get("runtime", {}) or {})
        return merged_item

    def match_existing_product(self, item: dict[str, Any]) -> dict[str, Any] | None:
        data = item.get("data") if isinstance(item.get("data"), dict) else {}
        sku = normalize_key(data.get("sku"))
        name = normalize_key(data.get("name"))
        for existing in self.base_store.list_items("products", include_archived=False):
            existing_data = existing.get("data") if isinstance(existing.get("data"), dict) else {}
            if sku and sku == normalize_key(existing_data.get("sku")):
                return existing
            if name and name == normalize_key(existing_data.get("name")):
                return existing
        return None

    def reassess_native_candidate(self, candidate: dict[str, Any], target_category: str, item: dict[str, Any]) -> None:
        schema = self.base_store.schema_manager.load_schema(target_category)
        evidence = str((candidate.get("source") or {}).get("evidence_excerpt") or "")
        intake_result = evaluate_intake_item(
            category_id=target_category,
            schema=schema,
            item=item,
            raw_text=evidence,
            confidence=float((candidate.get("intake") or {}).get("confidence") or 0.72),
            source_label="来源资料摘录",
        )
        normalized_item = intake_result["item"]
        intake = intake_result["intake"]
        proposal = candidate.setdefault("proposal", {})
        proposal.setdefault("formal_patch", {})["item"] = normalized_item
        proposal["suggested_fields"] = normalized_item.get("data", {})
        proposal["missing_fields"] = intake.get("missing_fields", [])
        proposal["warnings"] = intake.get("warnings", [])
        candidate["intake"] = intake
        candidate.setdefault("review", {}).update(
            {
                "completeness_status": intake.get("status"),
                "missing_fields": intake.get("missing_fields", []),
                "requires_human_approval": True,
                "allowed_auto_apply": False,
            }
        )

    def current_target_content(self, target_file: str) -> dict[str, Any]:
        compiled = self.compiler.compile()
        if target_file == "product_knowledge":
            return compiled["product_knowledge"]
        if target_file == "style_examples":
            return compiled["style_examples"]
        raise ValueError(f"unsupported target_file: {target_file}")

    def formal_patch(self, candidate: dict[str, Any]) -> dict[str, Any] | None:
        proposal = candidate.get("proposal", {}) or {}
        patch = proposal.get("formal_patch")
        return patch if isinstance(patch, dict) else None

    def status_root(self, status: str) -> Path:
        root = tenant_review_candidates_root() / status
        root.mkdir(parents=True, exist_ok=True)
        return root

    def find_path(self, candidate_id: str) -> Path | None:
        root = tenant_review_candidates_root()
        for status in ("pending", "approved", "rejected"):
            path = root / status / f"{candidate_id}.json"
            if path.exists():
                return path
        return None

    def require_path(self, candidate_id: str) -> Path:
        path = self.find_path(candidate_id)
        if not path:
            raise FileNotFoundError(candidate_id)
        return path

    def move_candidate(self, source: Path, target: Path, candidate: dict[str, Any]) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(candidate, ensure_ascii=False, indent=2), encoding="utf-8")
        upsert_candidate_to_db(candidate)
        if source.resolve() != target.resolve() and source.exists():
            source.unlink()


def merge_non_empty_dicts(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in patch.items():
        if is_empty(value):
            continue
        if isinstance(result.get(key), dict) and isinstance(value, dict):
            result[key] = merge_non_empty_dicts(result[key], value)
        else:
            result[key] = value
    return result


def merge_candidates(*groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for group in groups:
        for item in group:
            candidate_id = str(item.get("candidate_id") or item.get("id") or "")
            if not candidate_id:
                continue
            merged[candidate_id] = item
    return sorted(merged.values(), key=lambda item: str((item.get("review") or {}).get("updated_at") or item.get("created_at") or ""), reverse=True)


def filter_candidates_by_status(items: list[dict[str, Any]], status: str) -> list[dict[str, Any]]:
    expected = status or "pending"
    filtered = []
    for item in items:
        review = item.get("review") if isinstance(item.get("review"), dict) else {}
        item_status = str(review.get("status") or "pending")
        if item_status == expected:
            filtered.append(item)
    return filtered


def compact_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
    proposal = candidate.get("proposal") if isinstance(candidate.get("proposal"), dict) else {}
    formal_patch = proposal.get("formal_patch") if isinstance(proposal.get("formal_patch"), dict) else {}
    item = formal_patch.get("item") if isinstance(formal_patch.get("item"), dict) else {}
    data = item.get("data") if isinstance(item.get("data"), dict) else {}
    return {
        "candidate_id": candidate.get("candidate_id"),
        "generated_at": candidate.get("generated_at"),
        "review": compact_candidate_review(candidate.get("review")),
        "intake": compact_candidate_intake(candidate.get("intake")),
        "proposal": {
            "summary": proposal.get("summary") or "",
            "formal_patch": {
                "target_category": formal_patch.get("target_category") or proposal.get("target_category") or "",
                "operation": formal_patch.get("operation") or "",
                "item": {
                    "id": item.get("id") or "",
                    "data": compact_candidate_data(data),
                },
            },
        },
        "display_badges": candidate.get("display_badges") if isinstance(candidate.get("display_badges"), list) else [],
        "source_summary": candidate.get("source_summary") if isinstance(candidate.get("source_summary"), dict) else {},
        "primary_status": candidate.get("primary_status") or "",
        "can_promote": candidate.get("can_promote"),
    }


def compact_candidate_review(value: Any) -> dict[str, Any]:
    review = value if isinstance(value, dict) else {}
    keys = ["status", "completeness_status", "applied", "updated_at", "created_at"]
    return {key: review.get(key) for key in keys if review.get(key) not in (None, "", [], {})}


def compact_candidate_intake(value: Any) -> dict[str, Any]:
    intake = value if isinstance(value, dict) else {}
    keys = ["status"]
    return {key: intake.get(key) for key in keys if intake.get(key) not in (None, "", [], {})}


def compact_candidate_data(data: dict[str, Any]) -> dict[str, Any]:
    fields = [
        "name",
        "title",
        "sku",
        "category",
        "customer_message",
        "question",
        "answer",
        "service_reply",
        "policy_type",
        "price",
        "unit",
        "inventory",
        "product_id",
        "product_category",
        "applicability_scope",
    ]
    return {field: data.get(field) for field in fields if data.get(field) not in (None, "", [], {})}


def merge_product_data(existing_data: dict[str, Any], candidate_data: dict[str, Any]) -> dict[str, Any]:
    result = dict(existing_data)
    for key, value in candidate_data.items():
        if is_empty(value):
            continue
        if key == "price_tiers":
            result[key] = merge_price_tiers(result.get(key), value)
        elif key == "additional_details" and isinstance(value, dict):
            result[key] = merge_non_empty_dicts(result.get(key, {}) if isinstance(result.get(key), dict) else {}, value)
        else:
            result[key] = value
    return result


def merge_price_tiers(existing_value: Any, candidate_value: Any) -> list[dict[str, float]]:
    merged: dict[float, float] = {}
    for quantity, price in normalize_price_tiers(existing_value):
        merged[quantity] = price
    for quantity, price in normalize_price_tiers(candidate_value):
        merged[quantity] = price
    return [{"min_quantity": quantity, "unit_price": price} for quantity, price in sorted(merged.items())]


def is_empty(value: Any) -> bool:
    return value is None or value == "" or value == [] or value == {}


def transform_candidate_data_for_category(
    source_data: dict[str, Any],
    target_category: str,
    evidence: str,
    schema: dict[str, Any],
) -> dict[str, Any]:
    if target_category == "policies":
        return transform_to_policy_data(source_data, evidence)
    if target_category == "products":
        return transform_to_product_data(source_data)
    if target_category == "chats":
        return transform_to_chat_data(source_data, evidence)
    if target_category == "erp_exports":
        return transform_to_erp_data(source_data, evidence)
    if target_category in {"product_faq", "product_rules", "product_explanations"}:
        return transform_to_product_scoped_data(source_data, target_category, evidence)
    return transform_to_custom_data(source_data, evidence, schema)


def transform_to_policy_data(source_data: dict[str, Any], evidence: str) -> dict[str, Any]:
    details = source_data.get("additional_details") if isinstance(source_data.get("additional_details"), dict) else {}
    title = clean_text(source_data.get("title") or source_data.get("name") or details.get("公司名称") or "待确认规则")
    company_like = looks_like_company_data(source_data, evidence)
    policy_type = clean_text(source_data.get("policy_type")) or ("company" if company_like else "other")
    answer = clean_text(source_data.get("answer"))
    if not answer:
        answer = readable_policy_answer(source_data, evidence, company_like=company_like)
    answer = policy_customer_reply(answer)
    keywords = unique_strings(
        [
            *to_list(source_data.get("keywords")),
            *to_list(source_data.get("aliases")),
            title,
            source_data.get("category"),
            "公司名称" if company_like else "",
            "公司信息" if company_like else "",
            "主营范围" if company_like else "",
            "生产方" if company_like else "",
            "开票信息" if company_like else "",
        ]
    )
    return compact_business_dict(
        {
            "title": title,
            "policy_type": policy_type,
            "keywords": keywords,
            "applicability_scope": source_data.get("applicability_scope") or "global",
            "product_id": source_data.get("product_id") or details.get("product_id") or "",
            "product_category": source_data.get("product_category") or source_data.get("category") or details.get("商品类目") or "",
            "answer": answer,
            "allow_auto_reply": source_data.get("allow_auto_reply", True),
            "requires_handoff": source_data.get("requires_handoff", False),
            "handoff_reason": source_data.get("handoff_reason", ""),
            "operator_alert": source_data.get("operator_alert", False),
            "risk_level": source_data.get("risk_level") or "normal",
            "additional_details": merge_non_empty_dicts(details, remaining_details(source_data, {"title", "policy_type", "keywords", "applicability_scope", "product_id", "product_category", "category", "answer", "allow_auto_reply", "requires_handoff", "handoff_reason", "operator_alert", "risk_level"})),
        }
    )


def policy_customer_reply(text: str) -> str:
    cleaned = clean_text(text)
    if not cleaned:
        return ""
    labels = ["标准回复模板", "标准回复", "客户回复", "回复客户", "明确的回复", "明确回复", "答复", "回答", "话术", "回复"]
    best_match: re.Match[str] | None = None
    for label in labels:
        matches = list(re.finditer(re.escape(label) + r"\s*[:：]\s*(.+)$", cleaned))
        if matches:
            match = matches[-1]
            if best_match is None or match.start() > best_match.start():
                best_match = match
    if not best_match:
        return cleaned
    return clean_text(best_match.group(1)).strip(" \t\"'“”‘’：:")


def transform_to_product_data(source_data: dict[str, Any]) -> dict[str, Any]:
    details = source_data.get("additional_details") if isinstance(source_data.get("additional_details"), dict) else {}
    return compact_business_dict(
        {
            "name": source_data.get("name") or source_data.get("title"),
            "sku": source_data.get("sku"),
            "category": source_data.get("category"),
            "aliases": to_list(source_data.get("aliases") or source_data.get("keywords")),
            "specs": source_data.get("specs") or details.get("规格参数"),
            "price": source_data.get("price"),
            "unit": source_data.get("unit"),
            "price_tiers": source_data.get("price_tiers"),
            "inventory": source_data.get("inventory"),
            "shipping_policy": source_data.get("shipping_policy"),
            "warranty_policy": source_data.get("warranty_policy"),
            "reply_templates": source_data.get("reply_templates"),
            "risk_rules": source_data.get("risk_rules"),
            "additional_details": details or remaining_details(source_data, {"name", "title", "sku", "category", "aliases", "keywords", "specs", "price", "unit", "price_tiers", "inventory", "shipping_policy", "warranty_policy", "reply_templates", "risk_rules"}),
        }
    )


def transform_to_chat_data(source_data: dict[str, Any], evidence: str) -> dict[str, Any]:
    return compact_business_dict(
        {
            "customer_message": source_data.get("customer_message") or source_data.get("question") or "",
            "service_reply": source_data.get("service_reply") or source_data.get("answer") or readable_summary(source_data, evidence),
            "intent_tags": to_list(source_data.get("intent_tags") or source_data.get("keywords")) or ["general"],
            "tone_tags": to_list(source_data.get("tone_tags")) or ["人工整理"],
            "linked_categories": to_list(source_data.get("linked_categories")),
            "applicability_scope": source_data.get("applicability_scope") or "global",
            "product_id": source_data.get("product_id") or "",
            "product_category": source_data.get("product_category") or source_data.get("category") or "",
            "additional_details": remaining_details(source_data, {"customer_message", "question", "service_reply", "answer", "intent_tags", "keywords", "tone_tags", "linked_categories", "applicability_scope", "product_id", "product_category", "category"}),
        }
    )


def transform_to_erp_data(source_data: dict[str, Any], evidence: str) -> dict[str, Any]:
    external_id = clean_text(source_data.get("external_id") or source_data.get("sku") or source_data.get("name") or source_data.get("title") or "manual_record")
    fields = source_data.get("fields") if isinstance(source_data.get("fields"), dict) else source_data
    return compact_business_dict(
        {
            "source_system": source_data.get("source_system") or "admin_candidate",
            "record_type": source_data.get("record_type") or "other",
            "external_id": external_id,
            "fields": fields,
            "sync_status": source_data.get("sync_status") or "imported",
            "additional_details": {"来源资料摘录": evidence} if evidence else {},
        }
    )


def transform_to_product_scoped_data(source_data: dict[str, Any], target_category: str, evidence: str) -> dict[str, Any]:
    details = source_data.get("additional_details") if isinstance(source_data.get("additional_details"), dict) else {}
    product_id = safe_product_id(clean_text(source_data.get("product_id") or details.get("product_id") or details.get("归属商品") or source_data.get("sku")))
    title = clean_text(source_data.get("title") or source_data.get("name") or "商品专属知识")
    answer = policy_customer_reply(clean_text(source_data.get("answer") or source_data.get("service_reply") or readable_summary(source_data, evidence)))
    common = {
        "product_id": product_id,
        "title": title,
        "keywords": to_list(source_data.get("keywords") or source_data.get("aliases")),
        "additional_details": remaining_details(source_data, {"product_id", "sku", "title", "name", "keywords", "aliases", "answer", "service_reply", "content", "question"}),
    }
    if target_category == "product_faq":
        return compact_business_dict({**common, "question": source_data.get("question") or source_data.get("customer_message") or "", "answer": answer})
    if target_category == "product_explanations":
        return compact_business_dict({**common, "content": source_data.get("content") or source_data.get("description") or answer or evidence})
    return compact_business_dict(
        {
            **common,
            "answer": answer,
            "allow_auto_reply": source_data.get("allow_auto_reply", True),
            "requires_handoff": source_data.get("requires_handoff", False),
            "handoff_reason": source_data.get("handoff_reason", ""),
        }
    )


def transform_to_custom_data(source_data: dict[str, Any], evidence: str, schema: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for field in schema.get("fields", []) or []:
        field_id = str(field.get("id") or "")
        if not field_id:
            continue
        if field_id in source_data:
            result[field_id] = source_data[field_id]
        elif field_id == "title":
            result[field_id] = source_data.get("title") or source_data.get("name") or primary_candidate_title(source_data, "")
        elif field_id in {"content", "answer", "description"}:
            result[field_id] = source_data.get("answer") or source_data.get("description") or readable_summary(source_data, evidence)
    return compact_business_dict(result)


def normalize_runtime_for_category(target_category: str, data: dict[str, Any], existing: dict[str, Any]) -> dict[str, Any]:
    if target_category == "erp_exports":
        return {"allow_auto_reply": False, "requires_handoff": False, "risk_level": "normal"}
    explicit_auto_reply = data.get("allow_auto_reply", existing.get("allow_auto_reply", True))
    requires_handoff = bool(data.get("requires_handoff") or existing.get("requires_handoff") or explicit_auto_reply is False)
    allow_auto_reply = bool(explicit_auto_reply is not False and not requires_handoff)
    return {
        "allow_auto_reply": allow_auto_reply,
        "requires_handoff": requires_handoff,
        "risk_level": str(data.get("risk_level") or existing.get("risk_level") or ("warning" if requires_handoff else "normal")),
    }


def existing_or_current_source_authority(candidate: dict[str, Any], target_category: str = "") -> dict[str, Any]:
    if not target_category:
        return evaluate_candidate_source_authority(candidate)
    proposal = dict(candidate.get("proposal") if isinstance(candidate.get("proposal"), dict) else {})
    patch = dict(proposal.get("formal_patch") if isinstance(proposal.get("formal_patch"), dict) else {})
    patch["target_category"] = target_category
    proposal["target_category"] = target_category
    proposal["formal_patch"] = patch
    return evaluate_candidate_source_authority({**candidate, "proposal": proposal})


def legacy_target_category(target_file: str) -> str:
    if target_file == "style_examples":
        return "chats"
    if target_file == "product_knowledge":
        return "products"
    return ""


def safe_product_id(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "").strip()).strip("_.-").lower()


def looks_like_company_data(data: dict[str, Any], evidence: str = "") -> bool:
    payload = json.dumps(data, ensure_ascii=False) + "\n" + str(evidence or "")
    signals = intent_keywords().get("company", [])
    return any(signal in payload for signal in signals) or bool(re.search(r"[\u4e00-\u9fffA-Za-z0-9（）()]+(?:有限公司|有限责任公司|股份有限公司)", payload))


def readable_policy_answer(data: dict[str, Any], evidence: str, *, company_like: bool) -> str:
    details = data.get("additional_details") if isinstance(data.get("additional_details"), dict) else {}
    if company_like:
        parts = []
        name = clean_text(data.get("name") or details.get("公司名称"))
        scope = clean_text(details.get("主营范围") or details.get("主营业务") or data.get("category"))
        persona = clean_text(details.get("对外客服人设") or details.get("客服人设"))
        if name:
            parts.append(f"公司名称：{name}")
        if scope:
            parts.append(f"主营范围：{scope}")
        if persona:
            parts.append(f"对外客服风格：{persona}")
        for key, value in details.items():
            if key in {"公司名称", "主营范围", "主营业务", "对外客服人设", "客服人设"} or is_empty(value):
                continue
            parts.append(f"{key}：{value}")
        if parts:
            return "；".join(parts)
    return readable_summary(data, evidence)


def readable_summary(data: dict[str, Any], evidence: str) -> str:
    parts = []
    for key, value in data.items():
        if key in {"additional_details", "extra_fields"} or is_empty(value):
            continue
        parts.append(f"{key}：{value}")
    details = data.get("additional_details") if isinstance(data.get("additional_details"), dict) else {}
    for key, value in details.items():
        if not is_empty(value):
            parts.append(f"{key}：{value}")
    return "；".join(parts) or str(evidence or "")[:520]


def remaining_details(data: dict[str, Any], excluded: set[str]) -> dict[str, Any]:
    details = data.get("additional_details") if isinstance(data.get("additional_details"), dict) else {}
    result = dict(details)
    for key, value in data.items():
        if key in excluded or key == "additional_details" or is_empty(value):
            continue
        result.setdefault(str(key), value)
    return result


def compact_business_dict(data: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in data.items() if not is_empty(value)}


def to_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [clean_text(item) for item in value if clean_text(item)]
    text = clean_text(value)
    if not text:
        return []
    return [part.strip() for part in re.split(r"[,，、;；\n]+", text) if part.strip()]


def unique_strings(values: list[Any]) -> list[str]:
    result = []
    seen = set()
    for value in values:
        text = clean_text(value)
        if not text or text in seen:
            continue
        result.append(text)
        seen.add(text)
    return result


def clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def safe_candidate_item_id(value: str, *, fallback_seed: str) -> str:
    text = clean_text(value).lower()
    text = re.sub(r"[^a-z0-9_.-]+", "-", text)
    text = re.sub(r"-{2,}", "-", text).strip("-._")
    if not text or not re.match(r"^[a-z0-9]", text):
        import hashlib

        text = "item_" + hashlib.sha256(fallback_seed.encode("utf-8")).hexdigest()[:12]
    return text[:120]


def primary_candidate_title(data: dict[str, Any], fallback: str) -> str:
    return clean_text(data.get("name") or data.get("title") or data.get("external_id") or fallback or "未命名候选")


def apply_patch_to_content(content: dict[str, Any], patch: dict[str, Any]) -> None:
    operation = str(patch.get("operation") or "")
    item = patch.get("item") or {}
    if operation == "append_style":
        examples = content.setdefault("examples", [])
        replace_or_append(examples, "id", item)
        return
    if operation == "append_faq":
        faqs = content.setdefault("faq", [])
        replace_or_append(faqs, "intent", item)
        return
    if operation == "append_product":
        products = content.setdefault("products", [])
        replace_or_append(products, "id", item)
        return
    if operation == "update_product":
        products = content.setdefault("products", [])
        key = str(item.get("id") or "")
        for index, product in enumerate(products):
            if str(product.get("id") or "") == key:
                merged = {**product, **item}
                products[index] = merged
                return
        products.append(item)
        return
    raise ValueError(f"unsupported operation: {operation}")


def replace_or_append(items: list[dict[str, Any]], key: str, item: dict[str, Any]) -> None:
    item_key = str(item.get(key) or "")
    for index, existing in enumerate(items):
        if str(existing.get(key) or "") == item_key:
            items[index] = item
            return
    items.append(item)


def atomic_write_json(path: Path, content: Any) -> None:
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(json.dumps(content, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temp_path, path)


def postgres_store():
    config = load_storage_config()
    if not config.use_postgres or not config.postgres_configured:
        return None
    store = get_postgres_store(config=config)
    return store if store.available() else None


def upsert_candidate_to_db(candidate: dict[str, Any], *, tenant_id: str | None = None) -> None:
    db = postgres_store()
    if not db:
        return
    review = candidate.get("review") if isinstance(candidate.get("review"), dict) else {}
    db.upsert_candidate(active_tenant_id(tenant_id), candidate, status=str(review.get("status") or "pending"))
