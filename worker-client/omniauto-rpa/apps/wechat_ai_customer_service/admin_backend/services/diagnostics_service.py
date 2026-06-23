"""Knowledge validation and diagnostics helpers."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import uuid
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from .knowledge_base_store import KnowledgeBaseStore
from .knowledge_compiler import KnowledgeCompiler
from .knowledge_deduper import duplicate_text, normalize_price_tiers, normalized_fingerprint, semantic_key
from .knowledge_registry import KnowledgeRegistry
from .knowledge_schema_manager import KnowledgeSchemaManager
from .llm_knowledge_audit import audit_knowledge_and_rag, auto_dedup_rag_experiences, has_llm_config
from apps.wechat_ai_customer_service.knowledge_paths import active_tenant_id
from apps.wechat_ai_customer_service.platform_understanding_rules import risk_keywords
from apps.wechat_ai_customer_service.storage import get_postgres_store, load_storage_config


APP_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = APP_ROOT.parents[1]
DIAGNOSTICS_ROOT = PROJECT_ROOT / "runtime" / "apps" / "wechat_ai_customer_service" / "admin" / "diagnostics"
IGNORES_PATH = PROJECT_ROOT / "runtime" / "apps" / "wechat_ai_customer_service" / "admin" / "diagnostic_ignores.json"
TOKEN_BUDGET_NOTICE_THRESHOLD = 7000
KNOWLEDGE_DUPLICATE_SIMILARITY_THRESHOLD = 0.94


class DiagnosticsService:
    def run(
        self,
        mode: str = "quick",
        *,
        include_llm_probe: bool = False,
        include_wechat_live: bool = False,
        include_ignored: bool = False,
        include_llm_audit: bool = True,
        auto_dedup_rag: bool = False,
    ) -> dict[str, Any]:
        run_id = "diag_" + datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]

        # Auto-dedup AI experience pool items during full diagnostics (silent cleanup)
        auto_dedup_result: dict[str, Any] | None = None
        if mode == "full" or auto_dedup_rag:
            try:
                auto_dedup_result = auto_dedup_rag_experiences(active_tenant_id())
            except Exception:
                auto_dedup_result = None

        checks = [self.quick_check(recent_only=mode != "full", include_llm_audit=include_llm_audit)]
        if mode == "full":
            checks.extend(self.full_checks(include_llm_probe=include_llm_probe, include_wechat_live=include_wechat_live))
        issues = []
        for check in checks:
            issues.extend(check.get("issues", []) or [])
        issues = self.enrich_issues(issues)
        ignored = self.load_ignored()
        ignored_fingerprints = set(ignored)
        visible_ignored_fingerprints = {
            fingerprint
            for fingerprint, item in ignored.items()
            if not (isinstance(item, dict) and item.get("silent"))
        }
        token_budget_acknowledged = any(
            item.get("code") == "knowledge_token_budget_large" or item.get("source") == "diagnostic_auto_acknowledge"
            for item in ignored.values()
            if isinstance(item, dict)
        )
        token_budget_visible_acknowledged = any(
            (item.get("code") == "knowledge_token_budget_large" or item.get("source") == "diagnostic_auto_acknowledge")
            and not item.get("silent")
            for item in ignored.values()
            if isinstance(item, dict)
        )
        ignored_count = sum(
            1
            for item in issues
            if item.get("fingerprint") in visible_ignored_fingerprints
            or (token_budget_visible_acknowledged and is_token_budget_issue(item))
        )
        hidden_notice_count = sum(1 for item in ignored.values() if isinstance(item, dict) and item.get("silent"))
        if not include_ignored:
            issues = [
                item
                for item in issues
                if item.get("fingerprint") not in ignored_fingerprints
                and not (token_budget_acknowledged and is_token_budget_issue(item))
            ]
        status = (
            "error"
            if any(item.get("severity") == "error" for item in issues)
            else "warning"
            if any(item.get("severity") == "warning" for item in issues)
            else "ok"
        )
        report = {
            "ok": status != "error",
            "run_id": run_id,
            "mode": mode,
            "status": status,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "checks": checks,
            "issues": issues,
            "ignored_count": ignored_count,
            "summary": {
                "error_count": sum(1 for item in issues if item.get("severity") == "error"),
                "warning_count": sum(1 for item in issues if item.get("severity") == "warning"),
                "info_count": sum(1 for item in issues if item.get("severity") == "info"),
                "ignored_count": ignored_count,
                "hidden_notice_count": hidden_notice_count,
            },
            "auto_dedup_rag": auto_dedup_result,
        }
        self.write_report(report)
        return report

    def enrich_issues(self, issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
        enriched = []
        for issue in issues:
            item = dict(issue)
            item.setdefault("severity", "warning")
            item.setdefault("title", "diagnostic issue")
            item.setdefault("detail", "")
            item.setdefault("target_label", readable_target(item.get("target")))
            item.setdefault("repairable", bool(item.get("auto_repair")))
            if item.get("code") == "knowledge_token_budget_large":
                item["repairable"] = False
                item["target_label"] = "全局知识库"
            # Downgrade LLM audit severity so they don't block CI/error_count
            if str(item.get("code") or "").startswith("llm_"):
                current = item.get("severity", "warning")
                if current == "error":
                    item["severity"] = "warning"
                elif current == "warning":
                    item["severity"] = "info"
            # Preserve LLM audit fields
            for extra_key in ("action_type", "involved_targets", "llm_reasoning"):
                if extra_key in issue:
                    item[extra_key] = issue[extra_key]
            item["fingerprint"] = issue_fingerprint(item)
            item.setdefault("suggestions", default_suggestions(item))
            enriched.append(item)
        return enriched

    def load_ignored(self) -> dict[str, Any]:
        db = postgres_store()
        if db:
            payload = db.get_kv(active_tenant_id(), "diagnostics", "ignored_issues")
            if isinstance(payload, dict):
                return payload
        if not IGNORES_PATH.exists():
            return {}
        try:
            payload = json.loads(IGNORES_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def write_ignored(self, payload: dict[str, Any]) -> None:
        db = postgres_store()
        config = load_storage_config()
        if db:
            db.set_kv(active_tenant_id(), "diagnostics", "ignored_issues", payload)
            if not config.mirror_files:
                return
        IGNORES_PATH.parent.mkdir(parents=True, exist_ok=True)
        temp_path = IGNORES_PATH.with_suffix(".tmp")
        temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temp_path, IGNORES_PATH)

    def ignore_issue(self, fingerprint: str, reason: str = "") -> dict[str, Any]:
        if not re_fullmatch_fingerprint(fingerprint):
            return {"ok": False, "message": "invalid diagnostic fingerprint"}
        ignored = self.load_ignored()
        ignored[fingerprint] = {
            "fingerprint": fingerprint,
            "reason": reason or "ignored in admin console",
            "ignored_at": datetime.now().isoformat(timespec="seconds"),
        }
        self.write_ignored(ignored)
        return {"ok": True, "item": ignored[fingerprint], "items": list(ignored.values())}

    def list_ignored(self) -> list[dict[str, Any]]:
        return list(self.load_ignored().values())

    def clear_acknowledged_notices(self, code: str = "knowledge_token_budget_large") -> dict[str, Any]:
        ignored = self.load_ignored()
        current_token_issues = [
            item
            for item in self.enrich_issues(self.quick_check(recent_only=False).get("issues", []) or [])
            if is_token_budget_issue(item)
        ]
        current_token_fingerprints = {str(item.get("fingerprint") or "") for item in current_token_issues}
        changed = 0
        for fingerprint, item in list(ignored.items()):
            if not isinstance(item, dict):
                continue
            if item.get("code") == code or item.get("source") == "diagnostic_auto_acknowledge" or fingerprint in current_token_fingerprints:
                item["silent"] = True
                item["cleared_at"] = datetime.now().isoformat(timespec="seconds")
                item.setdefault("code", code)
                item.setdefault("source", "diagnostic_notice_suppressed")
                item["reason"] = "已彻底隐藏该容量提示。"
                ignored[fingerprint] = item
                changed += 1
        for issue in current_token_issues:
            fingerprint = str(issue.get("fingerprint") or issue_fingerprint(issue))
            if fingerprint not in ignored:
                ignored[fingerprint] = {
                    "fingerprint": fingerprint,
                    "code": code,
                    "reason": "已彻底隐藏该容量提示。",
                    "ignored_at": datetime.now().isoformat(timespec="seconds"),
                    "cleared_at": datetime.now().isoformat(timespec="seconds"),
                    "source": "diagnostic_notice_suppressed",
                    "silent": True,
                }
                changed += 1
        if changed:
            self.write_ignored(ignored)
        report = self.run(mode="quick")
        report["message"] = "已清除页面上的已处理提示记录。" if changed else "当前没有可清除的已处理提示记录。"
        report["cleared_count"] = changed
        return report

    def quick_check(self, *, recent_only: bool = True, include_llm_audit: bool = True) -> dict[str, Any]:
        checks = [
            self.validate_knowledge_bases(recent_only=recent_only),
        ]
        issues = []
        for check in checks:
            issues.extend(check.get("issues", []) or [])

        if include_llm_audit and has_llm_config():
            try:
                llm_issues = audit_knowledge_and_rag(active_tenant_id())
                issues.extend(llm_issues)
            except Exception:
                pass

        token_budget = self.estimate_token_budget()
        if token_budget > TOKEN_BUDGET_NOTICE_THRESHOLD:
            budget_detail = self.knowledge_budget_details(token_budget)
            issues.append({
                "code": "knowledge_token_budget_large",
                "severity": "info",
                "title": "知识体积较大",
                "detail": (
                    f"当前分类知识库总量估算约 {token_budget} token。"
                    "这不是知识格式故障，也没有某条知识需要立即删除；它提醒后续继续按客户问题检索相关知识，避免一次性加载全部知识。"
                ),
                "target_label": "全局知识库",
                "details": budget_detail["details"],
                "suggestions": budget_detail["suggestions"],
                "repairable": False,
            })
        return {
            "name": "quick_knowledge_validation",
            "ok": not any(item.get("severity") == "error" for item in issues),
            "issues": issues,
            "token_budget_estimate": token_budget,
            "recent_only": recent_only,
        }

    def full_checks(self, *, include_llm_probe: bool, include_wechat_live: bool) -> list[dict[str, Any]]:
        commands = [
            ("knowledge_runtime", ["uv", "run", "python", "apps/wechat_ai_customer_service/tests/run_knowledge_runtime_checks.py"]),
            ("knowledge_compiler", ["uv", "run", "python", "apps/wechat_ai_customer_service/tests/run_knowledge_compiler_checks.py"]),
            ("offline_regression", ["uv", "run", "python", "apps/wechat_ai_customer_service/tests/run_offline_regression.py"]),
            ("workflow_logic", ["uv", "run", "python", "apps/wechat_ai_customer_service/tests/run_workflow_logic_checks.py"]),
        ]
        if include_llm_probe:
            commands.append(("deepseek_probe", ["uv", "run", "python", "apps/wechat_ai_customer_service/tests/run_deepseek_boundary_probe.py"]))
        if include_wechat_live:
            commands.append(
                (
                    "wechat_live_regression",
                    [
                        "uv",
                        "run",
                        "python",
                        "apps/wechat_ai_customer_service/tests/run_file_transfer_live_regression.py",
                    ],
                )
            )
        return [self.run_command(name, command) for name, command in commands]

    def run_command(self, name: str, command: list[str]) -> dict[str, Any]:
        try:
            completed = subprocess.run(
                command,
                cwd=PROJECT_ROOT,
                text=True,
                capture_output=True,
                timeout=180,
            )
        except Exception as exc:
            return {
                "name": name,
                "ok": False,
                "issues": [{"severity": "error", "title": f"{name} 执行失败", "detail": repr(exc)}],
            }
        issues = []
        if completed.returncode != 0:
            issues.append(
                {
                    "severity": "error",
                    "title": f"{name} 未通过",
                    "detail": (completed.stdout + "\n" + completed.stderr)[-2000:],
                }
            )
        return {
            "name": name,
            "ok": completed.returncode == 0,
            "returncode": completed.returncode,
            "stdout_tail": completed.stdout[-1200:],
            "stderr_tail": completed.stderr[-1200:],
            "issues": issues,
        }

    def estimate_token_budget(self) -> int:
        total_chars = 0
        for path in (APP_ROOT / "data" / "knowledge_bases").rglob("*.json"):
            total_chars += len(path.read_text(encoding="utf-8"))
        return max(1, int(total_chars / 1.8))

    def knowledge_budget_details(self, token_budget: int) -> dict[str, Any]:
        root = APP_ROOT / "data" / "knowledge_bases"
        category_rows: list[dict[str, str]] = []
        total_files = 0
        for category_dir in sorted(path for path in root.iterdir() if path.is_dir()):
            chars = 0
            files = 0
            for path in category_dir.rglob("*.json"):
                files += 1
                total_files += 1
                chars += len(path.read_text(encoding="utf-8"))
            if files:
                category_rows.append({
                    "label": category_dir.name,
                    "value": f"约 {max(1, int(chars / 1.8))} token / {files} 个文件",
                    "level": "normal",
                })
        details = [
            {"label": "问题性质", "value": "容量提示，不是知识格式故障", "level": "normal"},
            {"label": "估算总量", "value": f"约 {token_budget} token", "level": "warning"},
            {"label": "检测阈值", "value": f"超过 {TOKEN_BUDGET_NOTICE_THRESHOLD} token 时提示", "level": "normal"},
            {"label": "文件数量", "value": f"{total_files} 个 JSON 文件", "level": "normal"},
            *category_rows,
        ]
        suggestions = [
            {
                "title": "不建议自动删除知识",
                "detail": "当前没有发现哪一条知识错误，系统不能为了降低体积擅自删内容。",
                "level": "danger",
            },
            {
                "title": "继续保持按门类和问题范围检索",
                "detail": "客服运行时应只取相关商品、政策、话术和上下文证据，不要把全库一次性塞给大模型。",
                "level": "normal",
            },
            {
                "title": "如果后续继续变大",
                "detail": "优先拆分过长政策、归档过期商品、把重复话术合并为模板，再重新检测。",
                "level": "warning",
            },
        ]
        return {"details": details, "suggestions": suggestions}

    def validate_knowledge_bases(self, *, recent_only: bool = True) -> dict[str, Any]:
        issues: list[dict[str, Any]] = []
        checked_items = 0
        try:
            registry = KnowledgeRegistry()
            schema_manager = KnowledgeSchemaManager(registry)
            store = KnowledgeBaseStore(registry, schema_manager)
            categories = registry.list_categories(enabled_only=True)
        except Exception as exc:
            return {"name": "classified_knowledge_validation", "ok": False, "issues": [{"severity": "error", "title": "knowledge_bases read failed", "detail": repr(exc)}]}
        for category in categories:
            category_id = str(category.get("id") or "")
            try:
                schema = schema_manager.load_schema(category_id)
                validation = schema_manager.validate_schema(category_id, schema)
                if not validation.get("ok"):
                    for problem in validation.get("problems", []) or []:
                        issues.append({"severity": "error", "title": "category schema invalid", "detail": str(problem), "target": category_id})
                all_items = store.list_items(category_id)
                focus_ids: set[str] | None = None
                if recent_only:
                    focus_ids = {
                        str(item.get("id") or "")
                        for item in all_items
                        if item_is_recent(store, category_id, str(item.get("id") or ""))
                    }
                for item in all_items:
                    if recent_only and not item_is_recent(store, category_id, str(item.get("id") or "")):
                        continue
                    checked_items += 1
                    item_validation = store.validate_item(category_id, item)
                    if not item_validation.get("ok"):
                        for problem in item_validation.get("problems", []) or []:
                            issues.append({"severity": "error", "title": "knowledge item invalid", "detail": str(problem), "target": f"{category_id}/{item.get('id')}"})
                issues.extend(self.detect_consistency_issues(category_id, all_items, focus_ids=focus_ids))
            except Exception as exc:
                issues.append({"severity": "error", "title": "category validation failed", "detail": repr(exc), "target": category_id})
        return {
            "name": "classified_knowledge_validation",
            "ok": not any(item.get("severity") == "error" for item in issues),
            "issues": issues,
            "category_count": len(categories),
            "checked_items": checked_items,
            "recent_only": recent_only,
        }

    def detect_consistency_issues(
        self,
        category_id: str,
        items: list[dict[str, Any]],
        *,
        focus_ids: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        issues: list[dict[str, Any]] = []
        grouped: dict[str, list[dict[str, Any]]] = {}
        for item in items:
            if item.get("status") == "archived":
                continue
            key = semantic_key(category_id, item)
            if not key:
                continue
            grouped.setdefault(key, []).append(item)
        for group in grouped.values():
            if len(group) < 2:
                continue
            for left_index, left in enumerate(group):
                left_id = str(left.get("id") or "")
                if focus_ids is not None and left_id not in focus_ids:
                    continue
                for right in group[left_index + 1 :]:
                    right_id = str(right.get("id") or "")
                    if left_id and right_id and left_id == right_id:
                        continue
                    left_fp = normalized_fingerprint(duplicate_text(category_id, left))
                    right_fp = normalized_fingerprint(duplicate_text(category_id, right))
                    similarity = SequenceMatcher(None, left_fp, right_fp).ratio() if left_fp and right_fp else 0.0
                    conflicts = conflicting_fields(category_id, left, right)
                    if conflicts:
                        issues.append(knowledge_consistency_issue(
                            code="knowledge_potential_conflict",
                            title="知识可能互相矛盾",
                            category_id=category_id,
                            left=left,
                            right=right,
                            detail=f"{category_id}/{left_id} 与 {category_id}/{right_id} 属于同一业务对象，但字段取值不一致：{', '.join(conflicts)}。",
                            similarity=similarity,
                            fields=conflicts,
                        ))
                    elif similarity >= KNOWLEDGE_DUPLICATE_SIMILARITY_THRESHOLD:
                        issues.append(knowledge_consistency_issue(
                            code="knowledge_potential_duplicate",
                            title="知识可能重复",
                            category_id=category_id,
                            left=left,
                            right=right,
                            detail=f"{category_id}/{left_id} 与 {category_id}/{right_id} 内容高度相似，建议人工确认是否合并或归档其中一条。",
                            similarity=similarity,
                            fields=[],
                        ))
                    if len(issues) >= 40:
                        return issues
        return issues

    def apply_suggestion(self, run_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        report = self.get_run(run_id)
        if not report:
            return {"ok": False, "run_id": run_id, "message": f"diagnostic run not found: {run_id}"}
        issues = report.get("issues", []) or []
        if not issues:
            return {"ok": True, "run_id": run_id, "message": "当前检测未发现需要修复的问题。"}
        acknowledged = []
        repairable = [issue for issue in issues if issue.get("repairable") or issue.get("auto_repair")]
        advisory = [issue for issue in issues if is_token_budget_issue(issue)]
        if advisory:
            ignored = self.load_ignored()
            for issue in advisory:
                fingerprint = str(issue.get("fingerprint") or issue_fingerprint(issue))
                ignored[fingerprint] = {
                    "fingerprint": fingerprint,
                    "code": "knowledge_token_budget_large",
                    "reason": "容量提示已确认：当前不是知识错误，后续按任务范围加载即可。",
                    "ignored_at": datetime.now().isoformat(timespec="seconds"),
                    "source": "diagnostic_auto_acknowledge",
                }
                acknowledged.append(fingerprint)
            self.write_ignored(ignored)

        repair_result = None
        if repairable:
            repair_result = KnowledgeCompiler().compile_to_disk()

        if not repairable and not acknowledged:
            result = dict(report)
            result.update({
                # Request succeeded, but there are no deterministic auto-repair actions.
                "ok": True,
                "auto_repair_applied": False,
                "message": "当前报告没有可自动修复的问题，请展开详情后按建议人工处理或标记忽略。",
                "payload": payload or {},
            })
            return result

        followup = self.run(mode=str(report.get("mode") or "quick"))
        actions = []
        if repair_result:
            actions.append("已重建兼容缓存")
        if acknowledged:
            actions.append("已将全局容量提示标记为已处理")
        followup["message"] = "；".join(actions) + "。"
        followup["repair"] = repair_result
        followup["acknowledged_fingerprints"] = acknowledged
        followup["payload"] = payload or {}
        return followup

    def write_report(self, report: dict[str, Any]) -> None:
        DIAGNOSTICS_ROOT.mkdir(parents=True, exist_ok=True)
        (DIAGNOSTICS_ROOT / f"{report['run_id']}.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def list_runs(self) -> list[dict[str, Any]]:
        if not DIAGNOSTICS_ROOT.exists():
            return []
        items = []
        for path in sorted(DIAGNOSTICS_ROOT.glob("*.json"), reverse=True):
            payload = json.loads(path.read_text(encoding="utf-8"))
            items.append(
                {
                    "run_id": payload.get("run_id"),
                    "mode": payload.get("mode"),
                    "status": payload.get("status"),
                    "created_at": payload.get("created_at"),
                    "summary": payload.get("summary", {}),
                }
            )
        return items

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        path = DIAGNOSTICS_ROOT / f"{run_id}.json"
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def validate_target_content(self, target_file: str, content: Any) -> dict[str, Any]:
        issues: list[dict[str, Any]] = []
        if not isinstance(content, dict):
            return {
                "ok": False,
                "issues": [
                    {"severity": "error", "title": "内容必须是 JSON 对象", "detail": f"{target_file} 不是对象。"}
                ],
            }

        if target_file == "product_knowledge":
            self._validate_product_knowledge(content, issues)
        elif target_file == "style_examples":
            self._validate_style_examples(content, issues)
        elif target_file == "manifest":
            self._validate_manifest(content, issues)
        else:
            issues.append({"severity": "error", "title": "未知目标文件", "detail": target_file})

        return {"ok": not any(item["severity"] == "error" for item in issues), "issues": issues}

    def validate_file(self, path: Path, target_file: str) -> dict[str, Any]:
        try:
            content = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            return {"ok": False, "issues": [{"severity": "error", "title": "JSON 读取失败", "detail": repr(exc)}]}
        return self.validate_target_content(target_file, content)

    def _validate_product_knowledge(self, content: dict[str, Any], issues: list[dict[str, Any]]) -> None:
        products = content.get("products")
        faqs = content.get("faq")
        if not isinstance(products, list):
            issues.append({"severity": "error", "title": "商品列表缺失", "detail": "products 必须是列表。"})
            products = []
        if not isinstance(faqs, list):
            issues.append({"severity": "error", "title": "FAQ 列表缺失", "detail": "faq 必须是列表。"})
            faqs = []

        ids = [str(item.get("id") or "") for item in products if isinstance(item, dict)]
        duplicate_ids = sorted({item for item in ids if item and ids.count(item) > 1})
        for product_id in duplicate_ids:
            issues.append({"severity": "error", "title": "商品 ID 重复", "detail": product_id, "target": product_id})

        alias_owner: dict[str, str] = {}
        for product in products:
            if not isinstance(product, dict):
                issues.append({"severity": "error", "title": "商品格式错误", "detail": "商品必须是对象。"})
                continue
            product_id = str(product.get("id") or "")
            for field in ("id", "name", "price", "unit"):
                if product.get(field) in (None, ""):
                    issues.append({"severity": "error", "title": "商品必填字段缺失", "detail": field, "target": product_id})
            aliases = [str(product.get("name") or ""), *[str(value) for value in product.get("aliases", []) or []]]
            for alias in [item.strip() for item in aliases if item.strip()]:
                previous = alias_owner.get(alias)
                if previous and previous != product_id:
                    issues.append({
                        "severity": "warning",
                        "title": "商品别名重复",
                        "detail": f"{alias}: {previous} / {product_id}",
                        "target": product_id,
                    })
                alias_owner[alias] = product_id

        intents = [str(item.get("intent") or "") for item in faqs if isinstance(item, dict)]
        duplicate_intents = sorted({item for item in intents if item and intents.count(item) > 1})
        for intent in duplicate_intents:
            issues.append({"severity": "warning", "title": "FAQ intent 重复", "detail": intent, "target": intent})
        for faq in faqs:
            if not isinstance(faq, dict):
                issues.append({"severity": "error", "title": "FAQ 格式错误", "detail": "FAQ 必须是对象。"})
                continue
            intent = str(faq.get("intent") or "")
            if not faq.get("answer"):
                issues.append({"severity": "error", "title": "FAQ 答案为空", "detail": intent, "target": intent})
            answer = str(faq.get("answer") or "")
            if any(keyword in answer for keyword in risk_keywords("diagnostics")) and not faq.get("needs_handoff"):
                issues.append({"severity": "warning", "title": "高风险 FAQ 未标记人工", "detail": intent, "target": intent})

    def _validate_style_examples(self, content: dict[str, Any], issues: list[dict[str, Any]]) -> None:
        examples = content.get("examples")
        if not isinstance(examples, list):
            issues.append({"severity": "error", "title": "话术列表缺失", "detail": "examples 必须是列表。"})
            return
        ids = [str(item.get("id") or "") for item in examples if isinstance(item, dict)]
        duplicate_ids = sorted({item for item in ids if item and ids.count(item) > 1})
        for style_id in duplicate_ids:
            issues.append({"severity": "error", "title": "话术 ID 重复", "detail": style_id, "target": style_id})
        for item in examples:
            if not isinstance(item, dict):
                issues.append({"severity": "error", "title": "话术格式错误", "detail": "话术必须是对象。"})
                continue
            if not item.get("id") or not item.get("message"):
                issues.append({"severity": "error", "title": "话术必填字段缺失", "detail": str(item), "target": item.get("id", "")})

    def _validate_manifest(self, content: dict[str, Any], issues: list[dict[str, Any]]) -> None:
        if not isinstance(content.get("items"), list):
            issues.append({"severity": "error", "title": "manifest items 缺失", "detail": "items 必须是列表。"})


def item_is_recent(store: KnowledgeBaseStore, category_id: str, item_id: str, *, days: int = 7) -> bool:
    try:
        path = store.item_path(category_id, item_id)
    except Exception:
        return True
    if not path.exists():
        return True
    modified_at = datetime.fromtimestamp(path.stat().st_mtime)
    return modified_at >= datetime.now() - timedelta(days=days)


def conflicting_fields(category_id: str, left: dict[str, Any], right: dict[str, Any]) -> list[str]:
    left_data = left.get("data") if isinstance(left.get("data"), dict) else {}
    right_data = right.get("data") if isinstance(right.get("data"), dict) else {}
    if category_id == "products":
        return changed_fields(
            left_data,
            right_data,
            ["price", "unit", "price_tiers", "inventory", "shipping_policy", "warranty_policy", "risk_rules"],
        )
    if category_id == "policies":
        fields = changed_fields(left_data, right_data, ["answer", "allow_auto_reply", "requires_handoff", "handoff_reason", "risk_level"])
        return fields if text_differs(left_data.get("answer"), right_data.get("answer")) or len(fields) > 1 else []
    if category_id == "chats":
        return changed_fields(left_data, right_data, ["service_reply", "intent_tags", "tone_tags"])
    if category_id == "erp_exports":
        return changed_fields(left_data, right_data, ["record_type", "fields"])
    return changed_fields(left_data, right_data, ["answer", "content", "price", "unit", "requires_handoff", "risk_level"])


def changed_fields(left_data: dict[str, Any], right_data: dict[str, Any], fields: list[str]) -> list[str]:
    changed = []
    for field in fields:
        left_value = left_data.get(field)
        right_value = right_data.get(field)
        if is_blank(left_value) or is_blank(right_value):
            continue
        if field == "price_tiers":
            if normalize_price_tiers(left_value) != normalize_price_tiers(right_value):
                changed.append(field)
            continue
        if normalized_value(left_value) != normalized_value(right_value):
            changed.append(field)
    return changed


def text_differs(left: Any, right: Any, *, threshold: float = 0.86) -> bool:
    left_text = normalized_fingerprint(str(left or ""))
    right_text = normalized_fingerprint(str(right or ""))
    if not left_text or not right_text:
        return False
    return SequenceMatcher(None, left_text, right_text).ratio() < threshold


def normalized_value(value: Any) -> str:
    return normalized_fingerprint(json.dumps(value, ensure_ascii=False, sort_keys=True) if isinstance(value, (dict, list)) else str(value or ""))


def is_blank(value: Any) -> bool:
    return value is None or value == "" or value == [] or value == {}


def knowledge_consistency_issue(
    *,
    code: str,
    title: str,
    category_id: str,
    left: dict[str, Any],
    right: dict[str, Any],
    detail: str,
    similarity: float,
    fields: list[str],
) -> dict[str, Any]:
    left_id = str(left.get("id") or "")
    right_id = str(right.get("id") or "")
    left_title = readable_item_title(left)
    right_title = readable_item_title(right)
    return {
        "code": code,
        "severity": "warning",
        "title": title,
        "detail": detail,
        "target": f"{category_id}/{left_id}",
        "target_label": f"{category_id}: {left_title} / {right_title}",
        "repairable": False,
        "details": [
            {"label": "知识 A", "value": f"{category_id}/{left_id} · {left_title}", "level": "warning"},
            {"label": "知识 B", "value": f"{category_id}/{right_id} · {right_title}", "level": "warning"},
            {"label": "相似度", "value": f"{similarity:.2f}", "level": "normal"},
            {"label": "争议字段", "value": "、".join(fields) if fields else "整体内容高度相似", "level": "danger" if fields else "warning"},
        ],
        "suggestions": [
            {
                "title": "请人工确认处理",
                "detail": "如果两条表达的是同一件事，建议保留信息更完整的一条，另一条归档；如果新资料更准确，可以手动合并后保存。",
                "level": "warning",
            },
            {
                "title": "不要自动覆盖",
                "detail": "系统只做风险提示，不会擅自删除或覆盖正式知识，避免误删客户真实规则。",
                "level": "normal",
            },
        ],
    }


def readable_item_title(item: dict[str, Any]) -> str:
    data = item.get("data") if isinstance(item.get("data"), dict) else {}
    for key in ("name", "title", "question", "customer_message", "external_id"):
        value = data.get(key)
        if value:
            return str(value)
    return str(item.get("id") or "未命名知识")


def readable_target(target: Any) -> str:
    text = str(target or "").strip()
    if not text:
        return "未指定位置"
    parts = text.split("/", 1)
    if len(parts) == 2:
        return f"知识门类：{parts[0]} / 条目：{parts[1]}"
    return f"知识门类：{text}"


def default_suggestions(issue: dict[str, Any]) -> list[dict[str, str]]:
    if issue.get("suggestions"):
        return issue["suggestions"]
    severity = str(issue.get("severity") or "")
    target = str(issue.get("target") or "")
    if severity == "error":
        return [
            {
                "title": "建议优先修复",
                "detail": "该问题可能影响知识入库、检索或客服回答。请点击查看位置进入对应知识，按提示补齐或修正字段。",
                "level": "danger",
            }
        ]
    if target:
        return [
            {
                "title": "建议人工确认",
                "detail": "该问题有明确知识位置，可以点查看位置进入条目详情后决定修改或标记忽略。",
                "level": "warning",
            }
        ]
    return [
        {
            "title": "建议展开详情",
            "detail": "该问题没有具体条目位置，先查看检测说明，再决定是否需要人工处理。",
            "level": "warning",
        }
    ]


def is_token_budget_issue(issue: dict[str, Any]) -> bool:
    title = str(issue.get("title") or "")
    detail = str(issue.get("detail") or "")
    return issue.get("code") == "knowledge_token_budget_large" or ("知识体积" in title and "token" in detail)


def issue_fingerprint(issue: dict[str, Any]) -> str:
    stable = {
        "code": issue.get("code"),
        "severity": issue.get("severity"),
        "title": issue.get("title"),
        "detail": issue.get("detail"),
        "target": issue.get("target"),
        "name": issue.get("name"),
    }
    return hashlib.sha1(json.dumps(stable, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:24]


def re_fullmatch_fingerprint(value: str) -> bool:
    return bool(value and len(value) <= 64 and all(char in "0123456789abcdef" for char in value.lower()))


def postgres_store():
    config = load_storage_config()
    if not config.use_postgres or not config.postgres_configured:
        return None
    store = get_postgres_store(config=config)
    return store if store.available() else None
