"""Conversation-based AI knowledge generator for the admin console."""

from __future__ import annotations

import hashlib
import json
import re
import urllib.error
import urllib.request
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from .audit_log import append_audit
from .knowledge_base_store import KnowledgeBaseStore, product_scoped_category_records
from .knowledge_compiler import KnowledgeCompiler
from .rag_experience_auto_review import auto_review_rag_experience
from .knowledge_registry import KnowledgeRegistry
from .knowledge_schema_manager import KnowledgeSchemaManager
from apps.wechat_ai_customer_service.knowledge_paths import tenant_runtime_root
from apps.wechat_ai_customer_service.llm_config import apply_llm_reasoning_effort, llm_urlopen, read_secret, resolve_deepseek_base_url, resolve_deepseek_max_tokens, resolve_deepseek_tier_model, resolve_deepseek_timeout
from apps.wechat_ai_customer_service.platform_understanding_rules import intent_keywords, product_keywords, risk_keywords
from apps.wechat_ai_customer_service.workflows.knowledge_intake import evaluate_intake_item
from apps.wechat_ai_customer_service.workflows.rag_experience_store import RagExperienceStore
from apps.wechat_ai_customer_service.workflows.rag_layer import RagService
from apps.wechat_ai_customer_service.workflows.knowledge_runtime import PRODUCT_SCOPED_SCHEMAS
from apps.wechat_ai_customer_service.product_master import PRODUCT_MASTER_CATEGORY_ID, product_master_category_record


APP_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = APP_ROOT.parents[1]
SESSIONS_ROOT = PROJECT_ROOT / "runtime" / "apps" / "wechat_ai_customer_service" / "admin" / "generator_sessions"
LLM_ASSIST_POLICY_VERSION = "knowledge_llm_assist_v1"
FRIENDLY_FIELD_LABELS = {
    "price_tiers": "批量价格",
    "reply_templates": "客服回复内容",
    "risk_rules": "风险提醒",
    "policy_type": "规则类别",
    "allow_auto_reply": "允许自动回复",
    "requires_handoff": "需要人工确认",
    "handoff_reason": "人工确认原因",
    "operator_alert": "提醒人工客服",
    "fields": "字段内容",
}


class KnowledgeGenerator:
    def __init__(self) -> None:
        self.registry = KnowledgeRegistry()
        self.schema_manager = KnowledgeSchemaManager(self.registry)
        self.store = KnowledgeBaseStore(self.registry, self.schema_manager)
        self.compiler = KnowledgeCompiler()

    def create_session(
        self,
        message: str,
        *,
        preferred_category_id: str = "",
        use_llm: bool = True,
    ) -> dict[str, Any]:
        session = {
            "session_id": new_session_id(),
            "status": "collecting",
            "created_at": now(),
            "updated_at": now(),
            "history": [],
            "category_id": "",
            "category_name": "",
            "confidence": 0.0,
            "provider": "",
            "draft_item": {},
            "missing_fields": [],
            "question": "",
            "warnings": [],
            "summary_rows": [],
        }
        return self._advance(session, message, preferred_category_id=preferred_category_id, use_llm=use_llm)

    def continue_session(self, session_id: str, message: str, *, use_llm: bool = True) -> dict[str, Any]:
        session = self.require_session(session_id)
        if session.get("status") == "saved":
            raise ValueError("session already saved")
        return self._advance(session, message, preferred_category_id=str(session.get("category_id") or ""), use_llm=use_llm)

    def update_draft(self, session_id: str, data: dict[str, Any]) -> dict[str, Any]:
        session = self.require_session(session_id)
        if session.get("status") == "saved":
            raise ValueError("session already saved")
        category_id = normalize_category_id(session.get("category_id"), "", self.registry)
        schema = self.schema_manager.load_schema(category_id)
        category = knowledge_category_record(self.registry, category_id)
        existing_item = session.get("draft_item") if isinstance(session.get("draft_item"), dict) else {}
        normalized_data = normalize_data_for_schema(
            schema,
            postprocess_generated_data(category_id, data if isinstance(data, dict) else {}, "manual_admin_edit"),
        )
        item_id = safe_item_id(category_id, str(existing_item.get("id") or ""), normalized_data)
        runtime = normalize_runtime(category_id, normalized_data, session.get("warnings") or [], existing_item.get("runtime"))
        item = {
            "schema_version": 1,
            "category_id": category_id,
            "id": item_id,
            "status": str(existing_item.get("status") or "active"),
            "source": existing_item.get("source") if isinstance(existing_item.get("source"), dict) else {"type": "ai_generator", "session_id": session_id},
            "data": normalized_data,
            "runtime": runtime,
            "metadata": existing_item.get("metadata") if isinstance(existing_item.get("metadata"), dict) else {},
        }
        intake_result = evaluate_intake_item(
            category_id=category_id,
            schema=schema,
            item=item,
            raw_text="manual_admin_edit",
            confidence=float(session.get("confidence") or 0.7),
            source_label="manual_admin_edit",
        )
        item = intake_result["item"]
        validation = intake_result["intake"]
        session.update(
            {
                "status": "ready" if validation["ok"] else "collecting",
                "updated_at": now(),
                "category_id": category_id,
                "category_name": str(category.get("name") or schema.get("display_name") or category_id),
                "draft_item": item,
                "missing_fields": validation["missing_fields"],
                "question": "" if validation["ok"] else validation["question"],
                "summary_rows": build_summary_rows(schema, item),
                "intake": validation,
            }
        )
        self.write_session(session)
        return {"ok": True, "session": session}

    def confirm_session(self, session_id: str) -> dict[str, Any]:
        session = self.require_session(session_id)
        if session.get("status") != "ready":
            return {"ok": False, "message": "generator session is not ready to save", "session": session}
        category_id = str(session.get("category_id") or "")
        item = session.get("draft_item") if isinstance(session.get("draft_item"), dict) else {}
        item["id"] = safe_item_id(category_id, str(item.get("id") or ""), item.get("data") or {})
        item["id"] = unique_item_id(self.store, category_id, item["id"], session_id)
        validation = self._validate_generated_item(category_id, item)
        if not validation["ok"]:
            session.update(
                {
                    "status": "collecting",
                    "missing_fields": validation["missing_fields"],
                    "question": validation["question"],
                    "warnings": [*session.get("warnings", []), *validation["warnings"]],
                    "updated_at": now(),
                }
            )
            self.write_session(session)
            return {"ok": False, "message": "generated item still needs information", "session": session}

        saved = self.store.save_item(category_id, item)
        if not saved.get("ok"):
            return {"ok": False, "message": "knowledge validation failed", "validation": saved, "session": session}
        compile_result = self.compiler.compile_to_disk()
        session.update({"status": "saved", "draft_item": saved["item"], "updated_at": now(), "question": ""})
        self.write_session(session)
        append_audit("generator_item_saved", {"session_id": session_id, "category_id": category_id, "item_id": item["id"]})
        return {"ok": True, "session": session, "item": saved["item"], "compile": compile_result}

    def confirm_session_to_rag_experience(self, session_id: str, *, use_llm: bool = True) -> dict[str, Any]:
        session = self.require_session(session_id)
        if session.get("status") != "ready":
            return {"ok": False, "message": "generator session is not ready to become AI experience pool item", "session": session}
        category_id = str(session.get("category_id") or "")
        item = session.get("draft_item") if isinstance(session.get("draft_item"), dict) else {}
        evidence = json.dumps(
            {
                "category_id": category_id,
                "category_name": session.get("category_name"),
                "summary_rows": session.get("summary_rows") or [],
                "draft_item": item,
                "source": "manual_admin_entry",
            },
            ensure_ascii=False,
            indent=2,
        )
        root = tenant_runtime_root() / "manual_rag_entries"
        root.mkdir(parents=True, exist_ok=True)
        path = root / f"{session_id}.json"
        path.write_text(evidence + "\n", encoding="utf-8")
        rag = RagService()
        rag_ingest = rag.ingest_file(path, source_type="manual_admin_entry", category=category_id, rebuild_index=True)
        store = RagExperienceStore()
        experience = store.record_intake(
            source_type="manual_admin_entry",
            source_path=str(path),
            category=category_id,
            evidence_excerpt=evidence,
            rag_ingest=rag_ingest,
            candidate_ids=[],
            original_source={"type": "manual_admin_entry", "session_id": session_id, "category_id": category_id},
        )
        # Manual admin entries must stay pending for explicit human disposition.
        reviewed = auto_review_rag_experience(
            experience,
            store=store,
            use_llm=use_llm,
            apply_auto_triage=False,
        )
        experience_id = str(reviewed.get("experience_id") or experience.get("experience_id") or "")
        if experience_id:
            try:
                reviewed = store.update_metadata(
                    experience_id,
                    {
                        "experience_review": {
                            "status": "pending",
                            "pending_at": now(),
                            "pending_reason": "manual_admin_entry_requires_human_disposition",
                        },
                        "reviewed_by_user": False,
                    },
                    rebuild_index=True,
                )
            except KeyError:
                pass
        session.update(
            {
                "status": "sent_to_rag_experience",
                "rag_experience_id": experience.get("experience_id"),
                "updated_at": now(),
                "question": "",
            }
        )
        self.write_session(session)
        append_audit("generator_rag_experience_created", {"session_id": session_id, "category_id": category_id, "experience_id": experience.get("experience_id")})
        return {"ok": True, "session": session, "item": reviewed, "rag_ingest": rag_ingest}

    def _advance(
        self,
        session: dict[str, Any],
        message: str,
        *,
        preferred_category_id: str,
        use_llm: bool,
    ) -> dict[str, Any]:
        text = str(message or "").strip()
        if not text:
            raise ValueError("message is required")
        session.setdefault("history", []).append({"role": "user", "content": text, "created_at": now()})
        candidate = self._generate_candidate(session, text, preferred_category_id=preferred_category_id, use_llm=use_llm)
        category_id = normalize_category_id(candidate.get("category_id"), preferred_category_id, self.registry)
        schema = self.schema_manager.load_schema(category_id)
        category = knowledge_category_record(self.registry, category_id)

        existing_item = session.get("draft_item") if isinstance(session.get("draft_item"), dict) else {}
        existing_data = existing_item.get("data") if isinstance(existing_item.get("data"), dict) else {}
        merged_data = normalize_data_for_schema(
            schema,
            merge_generated_data(existing_data, candidate.get("data") if isinstance(candidate.get("data"), dict) else {}),
        )
        merged_data = postprocess_generated_data(category_id, merged_data, text)
        item_id = safe_item_id(category_id, str(candidate.get("item_id_hint") or existing_item.get("id") or ""), merged_data)
        runtime = normalize_runtime(category_id, merged_data, candidate.get("warnings") or [], existing_item.get("runtime"))
        item = {
            "schema_version": 1,
            "category_id": category_id,
            "id": item_id,
            "status": "active",
            "source": {"type": "ai_generator", "session_id": session["session_id"]},
            "data": merged_data,
            "runtime": runtime,
            "metadata": existing_item.get("metadata") if isinstance(existing_item.get("metadata"), dict) else {},
        }
        intake_result = evaluate_intake_item(
            category_id=category_id,
            schema=schema,
            item=item,
            raw_text=text,
            confidence=float(candidate.get("confidence") or 0.55),
            source_label="用户原始描述",
        )
        item = intake_result["item"]
        validation = intake_result["intake"]
        warnings = dedupe_strings([*session.get("warnings", []), *to_string_list(candidate.get("warnings")), *validation["warnings"]])
        status = "ready" if validation["ok"] else "collecting"
        question = "" if validation["ok"] else validation["question"]
        session.update(
            {
                "status": status,
                "updated_at": now(),
                "category_id": category_id,
                "category_name": str(category.get("name") or schema.get("display_name") or category_id),
                "confidence": float(candidate.get("confidence") or 0.55),
                "provider": str(candidate.get("provider") or "heuristic"),
                "draft_item": item,
                "missing_fields": validation["missing_fields"],
                "question": question,
                "warnings": warnings,
                "summary_rows": build_summary_rows(schema, item),
                "intake": validation,
                "llm_assist": candidate.get("llm_assist") if isinstance(candidate.get("llm_assist"), dict) else session.get("llm_assist", {}),
            }
        )
        self.write_session(session)
        return {"ok": True, "session": session}

    def _generate_candidate(
        self,
        session: dict[str, Any],
        message: str,
        *,
        preferred_category_id: str,
        use_llm: bool,
    ) -> dict[str, Any]:
        if use_llm:
            llm_result = self._call_deepseek(session, message, preferred_category_id=preferred_category_id)
            if llm_result.get("ok") and isinstance(llm_result.get("candidate"), dict):
                candidate = dict(llm_result["candidate"])
                heuristic = heuristic_candidate(
                    message,
                    preferred_category_id=str(candidate.get("category_id") or session.get("category_id") or preferred_category_id or ""),
                )
                if isinstance(candidate.get("data"), dict) and isinstance(heuristic.get("data"), dict):
                    candidate["data"] = merge_generated_data(candidate["data"], heuristic["data"])
                elif isinstance(heuristic.get("data"), dict):
                    candidate["data"] = heuristic["data"]
                if not candidate.get("category_id"):
                    candidate["category_id"] = heuristic.get("category_id")
                candidate["warnings"] = dedupe_strings(
                    [*to_string_list(candidate.get("warnings")), *to_string_list(heuristic.get("warnings"))]
                )
                candidate["provider"] = "deepseek"
                candidate["llm_assist"] = generator_llm_assist(
                    status="model_generated",
                    attempted=True,
                    provider="deepseek",
                    reason="deepseek_returned_structured_candidate",
                )
                return candidate
            llm_error = str(llm_result.get("error") or "llm_unavailable_or_invalid")
        candidate = heuristic_candidate(message, preferred_category_id=preferred_category_id)
        candidate["provider"] = "heuristic"
        candidate["llm_assist"] = generator_llm_assist(
            status="rule_fallback_after_llm" if use_llm else "rule_only_disabled_by_request",
            attempted=use_llm,
            provider="",
            reason=llm_error if use_llm else "llm_disabled_by_caller",
        )
        return candidate

    def _call_deepseek(self, session: dict[str, Any], message: str, *, preferred_category_id: str) -> dict[str, Any]:
        api_key = read_secret("DEEPSEEK_API_KEY")
        if not api_key:
            return {"ok": False, "error": "DEEPSEEK_API_KEY is not set"}
        base_url = resolve_deepseek_base_url(read_secret_fn=read_secret)
        model = resolve_deepseek_tier_model(tier="flash", read_secret_fn=read_secret)
        prompt_pack = self._build_prompt_pack(session, message, preferred_category_id=preferred_category_id)
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": prompt_pack["system"]},
                {"role": "user", "content": json.dumps(prompt_pack["user"], ensure_ascii=False)},
            ],
            "temperature": 0.2,
            "max_tokens": resolve_deepseek_max_tokens(2400, read_secret_fn=read_secret),
        }
        apply_llm_reasoning_effort(payload, tier="flash", read_secret_fn=read_secret)
        request = urllib.request.Request(
            url=base_url.rstrip("/") + "/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with llm_urlopen(request, timeout=resolve_deepseek_timeout(120, read_secret_fn=read_secret)) as response:
                data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            return {"ok": False, "error": body[:500], "status": exc.code}
        except Exception as exc:
            return {"ok": False, "error": repr(exc)}
        content = (
            data.get("choices", [{}])[0]
            .get("message", {})
            .get("content", "")
        )
        candidate = parse_json_object(str(content or ""))
        if not candidate:
            return {"ok": False, "error": "model_response_was_not_json_object", "raw": str(content)[:500]}
        return {"ok": True, "candidate": candidate, "usage": data.get("usage", {})}

    def _build_prompt_pack(self, session: dict[str, Any], message: str, *, preferred_category_id: str) -> dict[str, Any]:
        categories = []
        category_records = [*self.registry.list_categories(enabled_only=True), *product_scoped_category_records()]
        if preferred_category_id == PRODUCT_MASTER_CATEGORY_ID or str(session.get("category_id") or "") == PRODUCT_MASTER_CATEGORY_ID:
            category_records = [product_master_category_record(), *category_records]
        for category in category_records:
            category_id = str(category.get("id") or "")
            schema = self.schema_manager.load_schema(category_id)
            categories.append(
                {
                    "id": category_id,
                    "name": category.get("name") or schema.get("display_name") or category_id,
                    "description": schema.get("description") or "",
                    "fields": [
                        {
                            "id": field.get("id"),
                            "label": field.get("label"),
                            "type": field.get("type"),
                            "required": bool(field.get("required")),
                            "options": field.get("options", []),
                        }
                        for field in schema.get("fields", []) or []
                    ],
                }
            )
        return {
            "system": (
                "你是微信 AI 客服系统的知识库整理员。你只负责把用户的自然语言描述整理成结构化知识，"
                "不能编造不确定的信息，不能直接代表客服回复客户。必须只输出 JSON 对象。"
                "如果缺少关键信息，把字段名放入 missing_fields，并给出 followup_question。"
                "如果信息无法放入已有字段，必须放入 data.additional_details，不能丢弃。"
                "需要根据内容判断最合适的 category_id，不要盲从用户预设门类。"
                "如果内容只适用于某个具体商品，优先归入 product_faq、product_rules 或 product_explanations，并填写 data.product_id。"
                "高风险承诺如账期、赔偿、免单、虚开发票、最低价等必须写入 warnings。"
                "当 category_id=policies 时，data.answer 必须只写实际发给客户看的标准回复，"
                "不要把“如果用户问到...就回复...”这类规则说明整段抄进去；触发条件、适用场景、原始描述放入 data.additional_details。"
            ),
            "user": {
                "message": message,
                "preferred_category_id": preferred_category_id,
                "current_session": {
                    "category_id": session.get("category_id"),
                    "draft_data": (session.get("draft_item") or {}).get("data", {}),
                    "history": session.get("history", [])[-6:],
                },
                "categories": categories,
                "required_response_shape": {
                    "category_id": "products|policies|chats|erp_exports|product_faq|product_rules|product_explanations|custom category id",
                    "confidence": 0.0,
                    "item_id_hint": "safe english id if possible",
                    "data": {"additional_details": "object for details that do not fit existing fields"},
                    "missing_fields": [],
                    "followup_question": "",
                    "warnings": [],
                    "summary_rows": [],
                },
            },
        }

    def _validate_generated_item(self, category_id: str, item: dict[str, Any]) -> dict[str, Any]:
        schema = self.schema_manager.load_schema(category_id)
        report = evaluate_intake_item(category_id=category_id, schema=schema, item=item)["intake"]
        return {**report, "ok": report["status"] == "ready"}

    def write_session(self, session: dict[str, Any]) -> None:
        SESSIONS_ROOT.mkdir(parents=True, exist_ok=True)
        (SESSIONS_ROOT / f"{session['session_id']}.json").write_text(
            json.dumps(session, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def require_session(self, session_id: str) -> dict[str, Any]:
        if not re.fullmatch(r"gen_[0-9]{8}_[0-9]{6}_[a-f0-9]{8}", session_id):
            raise FileNotFoundError(session_id)
        path = SESSIONS_ROOT / f"{session_id}.json"
        if not path.exists():
            raise FileNotFoundError(session_id)
        return json.loads(path.read_text(encoding="utf-8"))


def heuristic_candidate(message: str, *, preferred_category_id: str = "") -> dict[str, Any]:
    text = normalize_text(message)
    category_id = preferred_category_id or infer_category(text)
    data: dict[str, Any]
    if category_id == "products":
        data = parse_product(text)
    elif category_id == "policies":
        data = parse_policy(text)
    elif category_id == "chats":
        data = parse_chat(text)
    elif category_id == "erp_exports":
        data = parse_erp(text)
    elif category_id in PRODUCT_SCOPED_SCHEMAS:
        data = parse_product_scoped(text, category_id)
    else:
        data = {"title": first_nonempty(extract_after_label(text, ["标题", "名称"]), short_title(text)), "content": message.strip()}
    return {"category_id": category_id, "confidence": 0.62, "data": data, "warnings": risk_warnings(text)}


def infer_category(text: str) -> str:
    if has_any(text, ["商品专属", "产品专属", "仅适用于", "针对商品", "针对产品"]):
        if has_any(text, ["说明", "解释", "原因", "参数解释"]):
            return "product_explanations"
        if has_any(text, ["问答", "faq", "客户问", "问题"]):
            return "product_faq"
        return "product_rules"
    if has_any(text, ["erp", "订单", "导出", "字段", "客户资料", "外部编号", "同步"]):
        return "erp_exports"
    if has_any(text, ["客户说", "客服说", "话术", "聊天", "回复风格", "怎么回"]):
        return "chats"
    product_terms = [
        *intent_keywords().get("product", []),
        *product_keywords("quote"),
        *product_keywords("stock"),
        *product_keywords("shipping"),
        *product_keywords("spec"),
        "sku",
    ]
    strong_product_markers = ["新增商品", "商品名称", "商品:", "商品：", "sku", "型号", "单价", "售价", "库存"]
    if has_any(text, strong_product_markers):
        has_numeric_fact = bool(re.search(r"\d+(?:\.\d+)?\s*(?:元|块|件|台|套|个|把|箱)", text))
        if has_numeric_fact or has_any(text, ["新增商品", "商品名称", "sku", "型号", "库存"]):
            return "products"
    policy_terms = [
        *intent_keywords().get("invoice", []),
        *intent_keywords().get("payment", []),
        *intent_keywords().get("after_sales", []),
        *intent_keywords().get("shipping", []),
        *intent_keywords().get("handoff", []),
        "规则",
    ]
    if has_any(text, product_terms) and not has_any(text, policy_terms):
        return "products"
    if has_any(text, policy_terms):
        return "policies"
    if has_any(text, product_terms):
        return "products"
    return "policies"


def parse_product(text: str) -> dict[str, Any]:
    name = extract_after_label(text, ["新增商品", "商品名称", "商品", "名称"])
    name = clean_name(name) or short_title(text)
    price = first_number_after(text, ["单价", "价格", "报价", "售价"])
    unit = extract_unit(text) or ("台" if "台" in text else "件")
    sku = extract_regex(text, r"(?:型号|sku|SKU)[:：\s]*([A-Za-z0-9_.-]+)")
    inventory = first_number_after(text, ["库存", "现货"], fallback_money=False)
    tiers = extract_price_tiers(text)
    data: dict[str, Any] = {
        "name": name,
        "sku": sku,
        "category": "",
        "aliases": [],
        "specs": extract_sentence(text, ["规格", "参数", "尺寸"]),
        "price": price,
        "unit": unit,
        "price_tiers": tiers,
        "inventory": inventory,
        "shipping_policy": extract_sentence(text, ["发货", "物流", "包邮"]),
        "warranty_policy": extract_sentence(text, ["售后", "保修", "质保"]),
        "reply_templates": {},
        "risk_rules": risk_warnings(text),
    }
    return {key: value for key, value in data.items() if value not in ("", [], {}, None)}


def parse_product_scoped(text: str, category_id: str) -> dict[str, Any]:
    product_id = extract_product_id_hint(text)
    title = extract_after_label(text, ["规则名称", "说明主题", "问题标题", "标题", "名称"]) or short_title(text)
    keywords = to_string_list(extract_after_label(text, ["触发关键词", "关键词"]))
    answer = extract_after_label(text, ["标准回复", "客户回复", "回复", "回答", "答案", "话术"])
    content = extract_after_label(text, ["说明内容", "解释内容", "内容"])
    question = extract_after_label(text, ["客户问题", "客户问", "问题", "问法"])
    if not answer and category_id != "product_explanations":
        answer = extract_customer_reply(text) or clean_multiline(text)
    if not content and category_id == "product_explanations":
        content = clean_multiline(text)
    common = {
        "product_id": product_id,
        "title": title,
        "keywords": keywords,
        "additional_details": {"user_original_description": text.strip()},
    }
    if category_id == "product_faq":
        return {key: value for key, value in {**common, "question": question, "answer": answer}.items() if not is_empty(value)}
    if category_id == "product_explanations":
        return {key: value for key, value in {**common, "content": content}.items() if not is_empty(value)}
    requires_handoff = has_any(text, ["人工确认", "转人工", "不能承诺", "需要确认", "请示"])
    return {
        key: value
        for key, value in {
            **common,
            "answer": answer,
            "allow_auto_reply": not requires_handoff,
            "requires_handoff": requires_handoff,
            "handoff_reason": extract_after_label(text, ["人工确认原因", "转人工原因"]) or ("需要人工确认" if requires_handoff else ""),
        }.items()
        if not is_empty(value)
    }


def extract_product_id_hint(text: str) -> str:
    labeled = extract_after_label(text, ["product_id", "商品ID", "产品ID", "商品编号", "产品编号", "知识归属商品", "归属商品"])
    candidate = labeled or extract_regex(text, r"(?:SKU|sku|型号)[:：\s]*([A-Za-z0-9_.-]{2,80})")
    if not candidate:
        match = re.search(r"\b([A-Za-z][A-Za-z0-9_.-]*\d[A-Za-z0-9_.-]*)\b", text)
        candidate = match.group(1) if match else ""
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", str(candidate or "").strip()).strip("_.-").lower()


def parse_policy(text: str) -> dict[str, Any]:
    policy_type = infer_policy_type(text)
    title = extract_after_label(text, ["规则名称", "政策名称", "标题"])
    if not title:
        title = {
            "invoice": "开票政策",
            "payment": "付款政策",
            "logistics": "物流政策",
            "after_sales": "售后政策",
            "manual_required": "转人工规则",
            "discount": "优惠政策",
            "contract": "合同政策",
        }.get(policy_type, short_title(text))
    answer = extract_customer_reply(text)
    trigger_conditions = extract_trigger_conditions(text)
    additional_details = {"user_original_description": text.strip()}
    if trigger_conditions:
        additional_details["trigger_conditions"] = trigger_conditions
    return {
        "title": title,
        "policy_type": policy_type,
        "keywords": keyword_tags(text),
        "answer": answer,
        "allow_auto_reply": not bool(risk_warnings(text)),
        "requires_handoff": bool(risk_warnings(text)),
        "handoff_reason": "高风险承诺需人工确认" if risk_warnings(text) else "",
        "operator_alert": bool(risk_warnings(text)),
        "risk_level": "high" if risk_warnings(text) else "normal",
        "additional_details": additional_details,
    }


def postprocess_generated_data(category_id: str, data: dict[str, Any], source_text: str) -> dict[str, Any]:
    if category_id != "policies":
        return data
    normalized = dict(data)
    answer = str(normalized.get("answer") or "")
    cleaned_answer = extract_customer_reply(answer)
    source_reply = extract_customer_reply(source_text)
    if answer and cleaned_answer != clean_multiline(answer):
        normalized["answer"] = cleaned_answer
    elif is_instructional_policy_text(answer) and source_reply:
        normalized["answer"] = source_reply
    elif not answer and source_reply:
        normalized["answer"] = source_reply
    details = normalized.get("additional_details") if isinstance(normalized.get("additional_details"), dict) else {}
    trigger_conditions = extract_trigger_conditions(answer) or extract_trigger_conditions(source_text)
    if trigger_conditions and "trigger_conditions" not in details:
        details["trigger_conditions"] = trigger_conditions
    if source_text and source_text != "manual_admin_edit" and "user_original_description" not in details:
        details["user_original_description"] = source_text.strip()
    if details:
        normalized["additional_details"] = details
    return normalized


def is_instructional_policy_text(text: str) -> bool:
    cleaned = clean_multiline(text)
    if not cleaned:
        return False
    instruction_markers = [
        "如果用户",
        "如果客户",
        "当用户",
        "当客户",
        "要明确",
        "需要回复",
        "要回复",
    ]
    reply_markers = ["回复", "标准回复", "话术", "答复", "回答"]
    return any(marker in cleaned for marker in instruction_markers) and any(marker in cleaned for marker in reply_markers)


def extract_customer_reply(text: str) -> str:
    cleaned = clean_multiline(text)
    if not cleaned:
        return ""
    labels = [
        "标准回复模板",
        "标准回复",
        "客户回复",
        "回复客户",
        "明确的回复",
        "明确回复",
        "答复",
        "回答",
        "话术",
        "回复",
    ]
    best_match: re.Match[str] | None = None
    for label in labels:
        matches = list(re.finditer(re.escape(label) + r"\s*[:：]\s*(.+)$", cleaned))
        if matches:
            match = matches[-1]
            if best_match is None or match.start() > best_match.start():
                best_match = match
    if best_match:
        return strip_reply_text(best_match.group(1))
    return strip_reply_text(cleaned)


def extract_trigger_conditions(text: str) -> str:
    cleaned = clean_multiline(text)
    if not cleaned:
        return ""
    labels = ["标准回复模板", "标准回复", "明确的回复", "明确回复", "答复", "回答", "话术", "回复"]
    positions = [cleaned.find(label) for label in labels if label in cleaned]
    positions = [position for position in positions if position > 0]
    if not positions:
        return ""
    prefix = cleaned[: min(positions)].strip(" ,，。；;:：")
    prefix = re.sub(r"(要|需要)?明确的?$", "", prefix).strip(" ,，。；;:：")
    prefix = re.sub(r"(要|需要|请|应)$", "", prefix).strip(" ,，。；;:：")
    if any(marker in prefix for marker in ("如果", "当", "若")):
        return prefix
    return ""


def strip_reply_text(text: str) -> str:
    reply = clean_multiline(text).strip(" \t\"'“”‘’：:")
    reply = re.sub(r"^(?:要|应|需要)?(?:明确)?(?:地|的)?(?:回复|答复|reply)\s*[:：]\s*", "", reply, flags=re.I)
    return reply.strip(" \t\"'“”‘’")


def parse_chat(text: str) -> dict[str, Any]:
    customer = extract_regex(text, r"客户[说:：\s]+(.+?)(?:客服[说:：\s]+|$)", flags=re.S)
    reply = extract_regex(text, r"客服[说:：\s]+(.+)$", flags=re.S)
    if not reply and has_any(text, ["回复", "话术"]):
        reply = text.strip()
    return {
        "customer_message": clean_multiline(customer),
        "service_reply": clean_multiline(reply),
        "intent_tags": keyword_tags(text),
        "tone_tags": ["自然", "客服"],
        "linked_categories": [],
        "linked_item_ids": [],
        "usable_as_template": True,
    }


def parse_erp(text: str) -> dict[str, Any]:
    record_type = "other"
    if "库存" in text:
        record_type = "inventory"
    elif "价格" in text:
        record_type = "price"
    elif "客户" in text:
        record_type = "customer"
    elif "订单" in text:
        record_type = "order"
    elif "商品" in text:
        record_type = "product"
    return {
        "source_system": extract_after_label(text, ["来源系统", "系统"]) or "ERP",
        "record_type": record_type,
        "external_id": extract_after_label(text, ["外部编号", "编号", "ID"]) or stable_hash(text)[:10],
        "fields": {"raw_description": text.strip()},
        "sync_status": "imported",
    }


def normalize_category_id(value: Any, preferred: str, registry: KnowledgeRegistry) -> str:
    category_id = str(value or preferred or "").strip()
    if category_id in PRODUCT_SCOPED_SCHEMAS:
        return category_id
    if category_id and registry.get_category(category_id):
        return category_id
    return "products" if not preferred else preferred


def knowledge_category_record(registry: KnowledgeRegistry, category_id: str) -> dict[str, Any]:
    if category_id == PRODUCT_MASTER_CATEGORY_ID:
        return product_master_category_record()
    for category in product_scoped_category_records():
        if category.get("id") == category_id:
            return category
    return registry.require_category(category_id)


def normalize_data_for_schema(schema: dict[str, Any], data: dict[str, Any]) -> dict[str, Any]:
    normalized: dict[str, Any] = {}
    field_ids = {str(field.get("id") or "") for field in schema.get("fields", []) or []}
    for field in schema.get("fields", []) or []:
        field_id = str(field.get("id") or "")
        field_type = str(field.get("type") or "short_text")
        value = data.get(field_id, field.get("default"))
        normalized[field_id] = normalize_field_value(value, field_type)
    if "additional_details" in field_ids:
        details = normalized.get("additional_details") if isinstance(normalized.get("additional_details"), dict) else {}
        for key, value in data.items():
            if str(key) not in field_ids and not is_empty(value):
                details[str(key)] = value
        if details:
            normalized["additional_details"] = details
    return normalized


def normalize_field_value(value: Any, field_type: str) -> Any:
    if field_type == "boolean":
        return bool(value)
    if field_type in {"number", "money"}:
        if value in (None, ""):
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
    if field_type == "tags":
        return to_string_list(value)
    if field_type == "table":
        return value if isinstance(value, list) else []
    if field_type == "object":
        return value if isinstance(value, dict) else {}
    return "" if value is None else str(value).strip()


def normalize_runtime(category_id: str, data: dict[str, Any], warnings: list[Any], existing: Any) -> dict[str, Any]:
    runtime = existing if isinstance(existing, dict) else {}
    risk = bool(warnings) or any(keyword in json.dumps(data, ensure_ascii=False) for keyword in risk_keywords("knowledge_generator"))
    hard_handoff = any(keyword in json.dumps(data, ensure_ascii=False) for keyword in ("禁止自动回复", "不可自动回复", "不能自动回复", "必须转人工"))
    explicit_auto_reply = data.get("allow_auto_reply")
    explicit_handoff = data.get("requires_handoff")
    requires_handoff = bool(hard_handoff or explicit_handoff is True or explicit_auto_reply is False or (risk and category_id in {"policies", "chats", "product_rules"}))
    allow_auto_reply = runtime.get("allow_auto_reply", explicit_auto_reply if explicit_auto_reply is not None else not requires_handoff)
    runtime_requires_handoff = runtime.get("requires_handoff", requires_handoff)
    if explicit_auto_reply is False:
        allow_auto_reply = False
        runtime_requires_handoff = True
    return {
        "allow_auto_reply": bool(allow_auto_reply),
        "requires_handoff": bool(runtime_requires_handoff),
        "risk_level": str(runtime.get("risk_level") or ("warning" if risk else "normal")),
    }


def generator_required_fields(category_id: str, schema: dict[str, Any]) -> list[str]:
    if category_id == "products":
        return ["name", "price", "unit"]
    if category_id == "policies":
        return ["title", "policy_type", "answer"]
    if category_id == "chats":
        return ["service_reply"]
    if category_id == "erp_exports":
        return ["source_system", "record_type", "external_id"]
    if category_id == "product_faq":
        return ["product_id", "title", "answer"]
    if category_id == "product_rules":
        return ["product_id", "title", "answer"]
    if category_id == "product_explanations":
        return ["product_id", "title", "content"]
    return [str(field.get("id")) for field in schema.get("fields", []) or [] if field.get("required")]


def build_followup_question(schema: dict[str, Any], missing: list[str]) -> str:
    if not missing:
        return ""
    labels = []
    fields = {str(field.get("id") or ""): field for field in schema.get("fields", []) or []}
    for field_id in missing:
        labels.append(str(fields.get(field_id, {}).get("label") or field_id))
    return "还需要补充：" + "、".join(labels) + "。请直接把这些信息发给我，我会继续整理。"


def build_summary_rows(schema: dict[str, Any], item: dict[str, Any]) -> list[dict[str, str]]:
    data = item.get("data") if isinstance(item.get("data"), dict) else {}
    rows = [{"label": "知识 ID", "value": str(item.get("id") or "")}]
    for field in schema.get("fields", []) or []:
        field_id = str(field.get("id") or "")
        value = data.get(field_id)
        if is_empty(value):
            continue
        rows.append({"label": friendly_field_label(field), "value": display_value(value)})
    return rows


def friendly_field_label(field: dict[str, Any]) -> str:
    field_id = str(field.get("id") or "")
    return FRIENDLY_FIELD_LABELS.get(field_id) or str(field.get("label") or field_id)


def display_value(value: Any) -> str:
    if isinstance(value, list):
        if value and all(isinstance(item, dict) for item in value):
            if all("min_quantity" in item and "unit_price" in item for item in value):
                return "；".join(f"{format_number(item.get('min_quantity'))} 起：{format_number(item.get('unit_price'))} 元" for item in value)
            return "；".join(", ".join(f"{key}:{inner}" for key, inner in item.items()) for item in value)
        return "、".join(str(item) for item in value)
    if isinstance(value, dict):
        return "；".join(f"{key}: {inner}" for key, inner in value.items())
    return str(value)


def format_number(value: Any) -> str:
    number = as_float(value)
    if number is None:
        return str(value)
    return str(int(number)) if number.is_integer() else str(number)


def validate_price_tiers(value: Any) -> str:
    if not value:
        return ""
    if not isinstance(value, list):
        return "阶梯价格格式不正确。"
    previous_quantity = 0.0
    previous_price = float("inf")
    for index, row in enumerate(value, start=1):
        if not isinstance(row, dict):
            return f"第 {index} 档阶梯价格格式不正确。"
        quantity = as_float(row.get("min_quantity"))
        price = as_float(row.get("unit_price"))
        if quantity is None or price is None:
            return f"第 {index} 档阶梯价格缺少数量或价格。"
        if quantity <= previous_quantity:
            return f"第 {index} 档数量必须高于上一档。"
        if price >= previous_price:
            return f"第 {index} 档价格必须低于上一档。"
        previous_quantity = quantity
        previous_price = price
    return ""


def normalize_price_tiers(tiers: list[dict[str, float]]) -> list[dict[str, float]]:
    by_quantity: dict[float, dict[str, float]] = {}
    for tier in tiers:
        quantity = as_float(tier.get("min_quantity"))
        price = as_float(tier.get("unit_price"))
        if quantity is None or price is None:
            continue
        by_quantity[quantity] = {"min_quantity": quantity, "unit_price": price}
    return [by_quantity[key] for key in sorted(by_quantity)]


def extract_price_tiers(text: str) -> list[dict[str, float]]:
    tiers: list[dict[str, float]] = []
    unit_words = r"(?:台|件|个|套|只|箱|张|把|条|份|组|批)?"
    price_words = r"(?:元|块|rmb|RMB)?"
    price_label_words = r"(?:订单价|批发价|阶梯价|优惠价|团购价|单价|每台|每件|每个|每套|每箱|每组)?"
    patterns = [
        rf"(\d+(?:\.\d+)?)\s*{unit_words}\s*(?:以上|起|起订|及以上|>=)\s*(?:[，,、:：=]|且|并且|为|是)?\s*{price_label_words}\s*(?:[:：=]|为|是)?\s*(\d+(?:\.\d+)?)\s*{price_words}(?:\s*/\s*{unit_words})?",
        rf"(?:第[一二三四五六七八九十\d]+档)\s*(\d+(?:\.\d+)?)\s*{unit_words}\s*(?:起订|起|以上|及以上)?\s*[，,、:：]?\s*(\d+(?:\.\d+)?)\s*{price_words}(?:\s*/\s*{unit_words})?",
        rf"(\d+(?:\.\d+)?)\s*{unit_words}\s*[，,、]\s*(\d+(?:\.\d+)?)\s*{price_words}",
    ]
    for pattern in patterns:
        for quantity, price in re.findall(pattern, text):
            tiers.append({"min_quantity": float(quantity), "unit_price": float(price)})
    return normalize_price_tiers(tiers)


def first_number_after(text: str, labels: list[str], *, fallback_money: bool = True) -> float | None:
    for label in labels:
        match = re.search(re.escape(label) + r"[:：\s]*(\d+(?:\.\d+)?)", text)
        if match:
            return float(match.group(1))
    if not fallback_money:
        return None
    match = re.search(r"(\d+(?:\.\d+)?)\s*(?:元|块|rmb|RMB)", text)
    return float(match.group(1)) if match else None


def extract_unit(text: str) -> str:
    match = re.search(r"(?:元|块)\s*/\s*([\u4e00-\u9fa5A-Za-z]+)", text)
    return match.group(1) if match else ""


def extract_sentence(text: str, labels: list[str]) -> str:
    for label in labels:
        match = re.search(r"([^。；;\n]*" + re.escape(label) + r"[^。；;\n]*)", text)
        if match:
            return match.group(1).strip(" ，,。；;")
    return ""


def extract_after_label(text: str, labels: list[str]) -> str:
    for label in labels:
        match = re.search(re.escape(label) + r"[:：\s]*([^，,。；;\n]+)", text)
        if match:
            return match.group(1).strip()
    return ""


def extract_regex(text: str, pattern: str, flags: int = 0) -> str:
    match = re.search(pattern, text, flags)
    return match.group(1).strip() if match else ""


def clean_name(value: str) -> str:
    value = re.split(r"(?:单价|价格|报价|库存|型号|发货|物流|售后|保修)", value or "")[0]
    return value.strip(" ：:,，。；;")


def clean_multiline(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def short_title(text: str) -> str:
    cleaned = re.sub(r"\s+", " ", text).strip(" ：:,，。；;")
    return cleaned[:40] or "未命名知识"


def infer_policy_type(text: str) -> str:
    mapping = [
        ("invoice", intent_keywords().get("invoice", [])),
        ("payment", intent_keywords().get("payment", [])),
        ("logistics", intent_keywords().get("shipping", [])),
        ("after_sales", intent_keywords().get("after_sales", [])),
        ("discount", intent_keywords().get("discount", [])),
        ("contract", ["合同", "协议"]),
        ("manual_required", intent_keywords().get("handoff", [])),
    ]
    for policy_type, keywords in mapping:
        if has_any(text, keywords):
            return policy_type
    return "other"


def keyword_tags(text: str) -> list[str]:
    tags = []
    for keyword_group in ("invoice", "payment", "shipping", "after_sales", "discount", "quote", "stock", "handoff", "small_talk"):
        for keyword in intent_keywords().get(keyword_group, []):
            if keyword in text:
                tags.append(keyword)
                break
    return tags


def risk_warnings(text: str) -> list[str]:
    return [f"包含高风险关键词：{keyword}" for keyword in risk_keywords("knowledge_generator") if keyword in text]


def safe_item_id(category_id: str, hint: str, data: dict[str, Any]) -> str:
    seed = hint or str(data.get("sku") or data.get("name") or data.get("title") or data.get("external_id") or data.get("customer_message") or category_id)
    ascii_seed = re.sub(r"[^A-Za-z0-9_.-]+", "_", seed).strip("_.-").lower()
    if not ascii_seed or not re.match(r"^[a-zA-Z0-9]", ascii_seed):
        ascii_seed = f"{category_id}_{stable_hash(json.dumps(data, ensure_ascii=False))[:10]}"
    return ascii_seed[:96]


def unique_item_id(store: KnowledgeBaseStore, category_id: str, item_id: str, session_id: str) -> str:
    existing = store.get_item(category_id, item_id)
    if not existing:
        return item_id
    source = existing.get("source") if isinstance(existing.get("source"), dict) else {}
    if source.get("session_id") == session_id:
        return item_id
    suffix = stable_hash(f"{item_id}:{session_id}")[:6]
    return f"{item_id[:88]}_{suffix}"


def stable_hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def new_session_id() -> str:
    return "gen_" + datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def parse_json_object(text: str) -> dict[str, Any] | None:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            payload = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return None
    return payload if isinstance(payload, dict) else None


def generator_llm_assist(*, status: str, attempted: bool, provider: str, reason: str) -> dict[str, Any]:
    return {
        "policy_version": LLM_ASSIST_POLICY_VERSION,
        "stage": "manual_description_to_draft_knowledge",
        "attempted": bool(attempted),
        "provider": provider,
        "status": status,
        "reason": reason,
        "fallback_allowed": True,
        "human_approval_required": True,
    }


def normalize_text(text: str) -> str:
    return str(text or "").replace("\r\n", "\n").replace("\r", "\n").strip()


def has_any(text: str, keywords: list[str] | tuple[str, ...]) -> bool:
    return any(keyword.lower() in text.lower() for keyword in keywords)


def first_nonempty(*values: str) -> str:
    for value in values:
        if value:
            return value
    return ""


def to_string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        return [item.strip() for item in re.split(r"[,，、\n]+", value) if item.strip()]
    return [str(value).strip()]


def merge_dicts(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    merged = dict(left)
    for key, value in right.items():
        if is_empty(value):
            continue
        merged[key] = value
    return merged


def merge_generated_data(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    """Merge a follow-up answer without letting short supplement text replace stable fields."""
    merged = dict(left)
    stable_fields = {"name", "sku", "category", "price", "unit", "source_system", "record_type", "external_id"}
    for key, value in right.items():
        if is_empty(value):
            continue
        existing = merged.get(key)
        if key == "price_tiers":
            merged[key] = normalize_price_tiers([*(existing or []), *value])
            continue
        if key == "reply_templates" and isinstance(existing, dict) and isinstance(value, dict):
            merged[key] = {**existing, **value}
            continue
        if key in stable_fields and not is_empty(existing):
            continue
        if isinstance(existing, list) and isinstance(value, list):
            merged[key] = dedupe_strings([*existing, *value])
            continue
        if isinstance(existing, dict) and isinstance(value, dict):
            merged[key] = {**existing, **value}
            continue
        merged[key] = value
    return merged


def dedupe_strings(values: list[Any]) -> list[str]:
    seen = set()
    result = []
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def is_empty(value: Any) -> bool:
    return value is None or value == "" or value == [] or value == {}


def as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
