"""Client-side scanner for formal knowledge that may become shared public proposals.

This module is intentionally client-deliverable. It must not import VPS admin
modules because customer packages may ship without server source code.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from apps.wechat_ai_customer_service.knowledge_paths import DEFAULT_TENANT_ID, active_tenant_id, tenant_knowledge_base_root
from apps.wechat_ai_customer_service.workflows.generate_review_candidates import call_deepseek_json, compact_excerpt, stable_digest


UNIVERSAL_FORMAL_CATEGORIES = {"policies", "chats", "custom"}
SHARED_SCAN_TERMINAL_STATUSES = {
    "proposed",
    "uploaded",
    "not_recommended",
    "already_pending_or_reviewed",
    "already_in_shared_library",
    "duplicate",
    "accepted",
    "rejected",
    "void",
}
SHARED_CATEGORY_IDS = {"global_guidelines", "reply_style", "risk_control"}
PRODUCT_SPECIFIC_HINTS = {
    "product_id",
    "sku",
    "model",
    "inventory",
    "price",
    "vehicle",
    "car_model",
    "store",
    "city",
    "contact",
    "phone",
}
PRODUCT_SPECIFIC_TEXT_HINTS = {
    "商品",
    "SKU",
    "型号",
    "库存",
    "价格",
    "报价",
    "门店",
    "南京",
    "车辆",
    "二手车",
    "试驾",
    "过户",
    "检测报告",
    "车况",
}
TENANT_PRIVATE_FIELD_HINTS = {
    "tenant",
    "customer",
    "company",
    "store",
    "city",
    "contact",
    "phone",
    "wechat",
    "order",
    "contract",
}
TENANT_PRIVATE_TEXT_HINTS = {
    "门店",
    "手机号",
    "微信",
    "订单",
    "合同",
    "报价",
    "库存",
    "优惠",
    "江苏车金",
    "南京",
    "二手车",
    "试驾",
    "过户",
    "检测报告",
}
STRICT_UNIVERSAL_TOPIC_HINTS = {
    "人工",
    "转接",
    "礼貌",
    "抱歉",
    "稍等",
    "隐私",
    "风险",
    "无法确认",
    "不能承诺",
    "人工客服",
    "manual",
    "handoff",
    "privacy",
    "risk",
}
PRIVATE_DATA_PATTERNS = (
    re.compile(r"1[3-9]\d{9}"),
    re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
    re.compile(r"\b\d{15,18}[0-9Xx]?\b"),
)


def collect_universal_formal_entries(
    state: dict[str, Any],
    *,
    limit: int,
    tenant_id: str = "",
) -> list[dict[str, Any]]:
    tenants = [active_tenant_id(tenant_id)] if tenant_id else universal_scan_tenant_ids(state)
    entries: list[dict[str, Any]] = []
    for tenant in tenants:
        kb_root = tenant_knowledge_base_root(tenant)
        if not kb_root.exists():
            continue
        for category_dir in sorted(path for path in kb_root.iterdir() if path.is_dir()):
            category_id = category_dir.name
            if category_id not in UNIVERSAL_FORMAL_CATEGORIES:
                continue
            items_dir = category_dir / "items"
            if not items_dir.exists():
                continue
            for path in sorted(items_dir.glob("*.json")):
                payload = read_json_file(path, default={})
                if not isinstance(payload, dict):
                    continue
                entry = formal_item_entry(tenant, category_id, path, payload)
                if not is_universal_formal_entry(entry):
                    continue
                entries.append(entry)
                if len(entries) >= limit:
                    return entries
    return entries


def universal_scan_tenant_ids(state: dict[str, Any]) -> list[str]:
    tenants: set[str] = set()
    for user in state.get("users", {}).values():
        if not isinstance(user, dict):
            continue
        tenants.update(active_tenant_id(item) for item in user.get("tenant_ids", []) if str(item).strip())
    tenants.update(active_tenant_id(item) for item in state.get("tenants", {}).keys() if str(item).strip())
    return sorted(tenants)


def formal_item_entry(tenant_id: str, category_id: str, path: Path, payload: dict[str, Any]) -> dict[str, Any]:
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    title = str(data.get("title") or data.get("name") or payload.get("title") or payload.get("id") or path.stem)
    body = formal_item_body(data)
    keywords = normalize_text_list(data.get("keywords") or data.get("intent_tags") or data.get("tone_tags"))
    source_key = f"{tenant_id}:{category_id}:{payload.get('id') or path.stem}:{stable_digest(title + body, 16)}"
    return {
        "tenant_id": active_tenant_id(tenant_id),
        "category_id": category_id,
        "item_id": str(payload.get("id") or path.stem),
        "path": str(path),
        "status": str(payload.get("status") or data.get("status") or "active"),
        "title": title,
        "body": body,
        "keywords": keywords,
        "data": data,
        "source_key": source_key,
    }


def formal_item_body(data: dict[str, Any]) -> str:
    parts = []
    for key in ("answer", "service_reply", "guideline_text", "content", "body", "customer_message"):
        value = data.get(key)
        if value:
            parts.append(str(value))
    return "\n".join(parts)


def is_universal_formal_entry(entry: dict[str, Any]) -> bool:
    if str(entry.get("status") or "active") != "active":
        return False
    data = entry.get("data") if isinstance(entry.get("data"), dict) else {}
    if has_private_structured_fields(data):
        return False
    for key in PRODUCT_SPECIFIC_HINTS:
        if data.get(key):
            return False
    scope = str(data.get("applicability_scope") or data.get("scope") or "").strip().lower()
    if scope in {"product", "product_specific", "item", "sku", "category_specific", "specific_product", "product_category"}:
        return False
    text = f"{entry.get('title')}\n{entry.get('body')}\n{' '.join(entry.get('keywords') or [])}"
    if any(hint in text for hint in PRODUCT_SPECIFIC_TEXT_HINTS):
        return False
    if looks_tenant_private_or_industry_specific(text):
        return False
    if not str(entry.get("body") or "").strip():
        return False
    return True


def build_universal_shared_suggestions(entries: list[dict[str, Any]], *, use_llm: bool) -> list[dict[str, Any]]:
    if not entries:
        return []
    if use_llm:
        llm_suggestions = llm_universal_shared_suggestions(entries)
        if llm_suggestions is not None:
            return llm_suggestions
    return heuristic_universal_shared_suggestions(entries)


def llm_universal_shared_suggestions(entries: list[dict[str, Any]]) -> list[dict[str, Any]] | None:
    prompt = {
        "task": "从客户正式知识中筛选极少数适合提交为共享公共知识候选的内容。",
        "rules": [
            "共享公共知识必须跨客户、跨行业安全适用。",
            "禁止包含客户、公司、门店、城市、联系人、手机号、微信号、订单、商品、车型、价格、库存、优惠、合同和内部流程。",
            "二手车、试驾、过户、检测报告、门店看车等行业规则只能留在客户自己的正式知识库。",
            "不确定时不要输出候选。",
        ],
        "output_schema": {
            "suggestions": [
                {
                    "title": "标题",
                    "category_id": "global_guidelines|reply_style|risk_control",
                    "guideline_text": "公共原则",
                    "keywords": ["关键词"],
                    "applies_to": "适用范围",
                    "universal_reason": "为什么可共享",
                    "source_keys": ["正式知识 source_key"],
                    "universal_score": 0,
                }
            ]
        },
        "entries": [
            {
                "source_key": item["source_key"],
                "tenant_id": item["tenant_id"],
                "category_id": item["category_id"],
                "item_id": item["item_id"],
                "title": item["title"],
                "keywords": item["keywords"],
                "body": compact_excerpt(item["body"], 700),
            }
            for item in entries[:80]
        ],
    }
    result = call_deepseek_json(prompt)
    if not result:
        return None
    raw_suggestions = result.get("suggestions") if isinstance(result.get("suggestions"), list) else []
    by_key = {item["source_key"]: item for item in entries}
    suggestions = []
    for raw in raw_suggestions:
        if not isinstance(raw, dict):
            continue
        source_keys = [str(key) for key in raw.get("source_keys", []) if str(key).strip()]
        source_items = [by_key[key] for key in source_keys if key in by_key]
        if not source_items:
            source_key = str(raw.get("source_key") or "").strip()
            if source_key in by_key:
                source_items = [by_key[source_key]]
        if not source_items:
            continue
        suggestion = normalize_universal_suggestion(raw, source_items, provider="formal_knowledge_universal_llm", llm_used=True)
        if suggestion and is_strictly_shareable_suggestion(suggestion, source_items):
            suggestions.append(suggestion)
    return suggestions


def heuristic_universal_shared_suggestions(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    suggestions = []
    for entry in entries:
        if not entry_has_strict_universal_topic(entry):
            continue
        suggestion = normalize_universal_suggestion(
            {
                "title": entry["title"],
                "category_id": shared_category_for_formal_entry(entry),
                "guideline_text": entry["body"],
                "keywords": entry["keywords"],
                "applies_to": "所有客户都可能遇到的通用客服场景",
                "universal_reason": "该正式知识不绑定具体商品、库存、价格、门店或客户，可由管理员审核是否沉淀为共享公共知识。",
                "universal_score": 78,
            },
            [entry],
            provider="formal_knowledge_universal_heuristic",
            llm_used=False,
        )
        if suggestion and is_strictly_shareable_suggestion(suggestion, [entry]):
            suggestions.append(suggestion)
    return suggestions


def normalize_universal_suggestion(
    raw: dict[str, Any],
    source_items: list[dict[str, Any]],
    *,
    provider: str,
    llm_used: bool,
) -> dict[str, Any] | None:
    title = str(raw.get("title") or source_items[0].get("title") or "").strip()
    guideline = str(raw.get("guideline_text") or raw.get("content") or source_items[0].get("body") or "").strip()
    if not title or not guideline:
        return None
    source_keys = [str(item.get("source_key") or "") for item in source_items if str(item.get("source_key") or "").strip()]
    merged_key = stable_digest("|".join(source_keys) + title + guideline, 18)
    category_id = sanitize_shared_category(raw.get("category_id") or shared_category_for_formal_entry(source_items[0]))
    return {
        "id": f"shared_{merged_key}",
        "tenant_id": str(source_items[0].get("tenant_id") or DEFAULT_TENANT_ID),
        "category_id": category_id,
        "title": title,
        "guideline_text": compact_excerpt(guideline, 1200),
        "keywords": normalize_text_list(raw.get("keywords")) or merged_keywords(source_items),
        "applies_to": str(raw.get("applies_to") or "所有客户都可能遇到的通用客服场景"),
        "universal_reason": str(raw.get("universal_reason") or ""),
        "universal_score": safe_int(raw.get("universal_score"), default=78),
        "source_items": source_items,
        "source_key": source_keys[0] if len(source_keys) == 1 else stable_digest("|".join(source_keys), 18),
        "provider": provider,
        "llm_used": llm_used,
    }


def is_strictly_shareable_suggestion(suggestion: dict[str, Any], source_items: list[dict[str, Any]]) -> bool:
    shared_text = f"{suggestion.get('title')}\n{suggestion.get('guideline_text')}\n{suggestion.get('applies_to')}\n{' '.join(suggestion.get('keywords') or [])}"
    if looks_tenant_private_or_industry_specific(shared_text):
        return False
    if any(hint in shared_text for hint in PRODUCT_SPECIFIC_TEXT_HINTS):
        return False
    if suggestion.get("llm_used") and safe_int(suggestion.get("universal_score"), default=0) < 85:
        return False
    if not entry_has_strict_universal_topic({"title": suggestion.get("title"), "body": suggestion.get("guideline_text"), "keywords": suggestion.get("keywords") or []}):
        return False
    return all(is_universal_formal_entry(item) for item in source_items)


def build_shared_content_from_suggestion(suggestion: dict[str, Any]) -> dict[str, Any]:
    category_id = sanitize_shared_category(suggestion.get("category_id"))
    item_id = str(suggestion.get("id") or f"shared_{stable_digest(str(suggestion), 16)}")
    risk_control = category_id == "risk_control"
    data = {
        "title": str(suggestion.get("title") or item_id),
        "guideline_text": str(suggestion.get("guideline_text") or ""),
        "keywords": normalize_text_list(suggestion.get("keywords")),
        "applies_to": str(suggestion.get("applies_to") or ""),
    }
    if risk_control:
        data.update({"allow_auto_reply": False, "requires_handoff": True, "handoff_reason": "shared_risk_control"})
    return {
        "schema_version": 1,
        "id": item_id,
        "item_id": item_id,
        "category_id": category_id,
        "title": str(suggestion.get("title") or item_id),
        "status": "active",
        "keywords": normalize_text_list(suggestion.get("keywords")),
        "applies_to": str(suggestion.get("applies_to") or ""),
        "content": str(suggestion.get("guideline_text") or ""),
        "guideline_text": str(suggestion.get("guideline_text") or ""),
        "notes": f"候选理由：{suggestion.get('universal_reason') or ''}".strip(),
        "source": {
            "type": "formal_knowledge_universal_extraction",
            "provider": suggestion.get("provider"),
            "tenant_id": suggestion.get("tenant_id"),
            "source_items": suggestion.get("source_items") or [],
            "llm_used": bool(suggestion.get("llm_used")),
        },
        "data": data,
        "runtime": {
            "allow_auto_reply": not risk_control,
            "requires_handoff": risk_control,
            "risk_level": "high" if risk_control else "normal",
        },
    }


def formal_source_keys_for_suggestion(suggestion: dict[str, Any]) -> list[str]:
    keys: list[str] = []
    for item in suggestion.get("source_items") or []:
        if isinstance(item, dict):
            key = str(item.get("source_key") or "").strip()
            if key and key not in keys:
                keys.append(key)
    return keys


def has_private_structured_fields(data: dict[str, Any]) -> bool:
    if not isinstance(data, dict):
        return False
    for key, value in data.items():
        key_text = str(key or "").strip().lower()
        if any(hint in key_text for hint in TENANT_PRIVATE_FIELD_HINTS) and value not in (None, "", [], {}):
            return True
        if isinstance(value, dict) and has_private_structured_fields(value):
            return True
        if isinstance(value, list):
            for item in value:
                if isinstance(item, dict) and has_private_structured_fields(item):
                    return True
    return False


def looks_tenant_private_or_industry_specific(text: str) -> bool:
    value = str(text or "")
    if any(hint in value for hint in TENANT_PRIVATE_TEXT_HINTS):
        return True
    if any(pattern.search(value) for pattern in PRIVATE_DATA_PATTERNS):
        return True
    return False


def entry_has_strict_universal_topic(entry: dict[str, Any]) -> bool:
    text = f"{entry.get('title')}\n{entry.get('body')}\n{' '.join(entry.get('keywords') or [])}"
    if any(hint in text for hint in STRICT_UNIVERSAL_TOPIC_HINTS):
        return True
    data = entry.get("data") if isinstance(entry.get("data"), dict) else {}
    scope = str(data.get("applicability_scope") or data.get("scope") or "").strip().lower()
    return scope == "global" and str(entry.get("category_id") or "") in UNIVERSAL_FORMAL_CATEGORIES


def merged_keywords(source_items: list[dict[str, Any]]) -> list[str]:
    values: list[str] = []
    for item in source_items:
        values.extend(item.get("keywords") or [])
    return normalize_text_list(values)[:12]


def shared_category_for_formal_entry(entry: dict[str, Any]) -> str:
    data = entry.get("data") if isinstance(entry.get("data"), dict) else {}
    text = f"{entry.get('title')}\n{entry.get('body')}\n{' '.join(entry.get('keywords') or [])}".lower()
    policy_type = str(data.get("policy_type") or "").lower()
    if "risk" in text or "handoff" in text or "转人工" in text or "人工" in text or policy_type in {"manual_required", "risk_control"}:
        return "risk_control"
    if str(entry.get("category_id") or "") == "chats":
        return "reply_style"
    return "global_guidelines"


def sanitize_shared_category(value: Any) -> str:
    category = str(value or "").strip()
    return category if category in SHARED_CATEGORY_IDS else "global_guidelines"


def normalize_text_list(value: Any) -> list[str]:
    if value is None:
        return []
    values = value if isinstance(value, list) else str(value).replace("，", ",").split(",")
    result: list[str] = []
    for item in values:
        text = str(item or "").strip()
        if text and text not in result:
            result.append(text)
    return result


def read_json_file(path: Path, *, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def safe_int(value: Any, *, default: int) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default
