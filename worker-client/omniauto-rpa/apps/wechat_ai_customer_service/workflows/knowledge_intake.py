"""Shared quality gate for knowledge generated from text or uploaded files."""

from __future__ import annotations

import json
import re
from typing import Any

from apps.wechat_ai_customer_service.platform_understanding_rules import risk_keywords


BUSINESS_REQUIRED_FIELDS = {
    "products": ["name", "price", "unit"],
    "policies": ["title", "policy_type", "answer"],
    "chats": ["service_reply"],
    "erp_exports": ["source_system", "record_type", "external_id"],
    "product_faq": ["product_id", "title", "answer"],
    "product_rules": ["product_id", "title", "answer"],
    "product_explanations": ["product_id", "title", "content"],
}

HARD_HANDOFF_KEYWORDS = ("禁止自动回复", "不可自动回复", "不能自动回复", "全部转人工", "整单转人工", "必须转人工")
ADDITIONAL_DETAILS_FIELD = "additional_details"


def evaluate_intake_item(
    *,
    category_id: str,
    schema: dict[str, Any],
    item: dict[str, Any],
    raw_text: str = "",
    confidence: float = 0.55,
    source_label: str = "",
) -> dict[str, Any]:
    """Normalize one proposed knowledge item and report whether it is ready."""
    normalized_item = dict(item)
    data = normalized_item.get("data") if isinstance(normalized_item.get("data"), dict) else {}
    data, preserved_keys = preserve_additional_details(data, schema, raw_text=raw_text, source_label=source_label)
    data = normalize_business_fields(category_id, data)
    normalized_item["data"] = data

    missing_fields = missing_required_fields(category_id, schema, data)
    warnings = risk_warnings(data)
    runtime = normalized_item.get("runtime") if isinstance(normalized_item.get("runtime"), dict) else {}
    normalized_item["runtime"] = normalize_runtime(runtime, warnings)
    status = "ready" if not missing_fields else "needs_more_info"
    question = build_followup_question(schema, missing_fields)
    report = {
        "status": status,
        "ok": status == "ready",
        "confidence": round(float(confidence or 0.0), 3),
        "missing_fields": missing_fields,
        "missing_labels": field_labels(schema, missing_fields),
        "question": question,
        "warnings": warnings,
        "preserved_detail_keys": preserved_keys,
    }
    return {"item": normalized_item, "intake": report}


def normalize_business_fields(category_id: str, data: dict[str, Any]) -> dict[str, Any]:
    if category_id != "products":
        return data
    result = dict(data)
    tiers = result.get("price_tiers")
    if is_empty(result.get("price")):
        first_price = first_valid_tier_price(tiers)
        if first_price is not None:
            result["price"] = first_price
    return result


def preserve_additional_details(
    data: dict[str, Any],
    schema: dict[str, Any],
    *,
    raw_text: str = "",
    source_label: str = "",
) -> tuple[dict[str, Any], list[str]]:
    field_ids = {str(field.get("id") or "") for field in schema.get("fields", []) or []}
    result: dict[str, Any] = {}
    details: dict[str, Any] = {}
    existing_details = data.get(ADDITIONAL_DETAILS_FIELD)
    if isinstance(existing_details, dict):
        details.update({str(key): value for key, value in existing_details.items() if not is_empty(value)})

    extra_fields = data.get("extra_fields")
    if isinstance(extra_fields, dict):
        for key, value in extra_fields.items():
            if not is_empty(value):
                details[str(key)] = value

    for key, value in data.items():
        key_text = str(key)
        if key_text in {ADDITIONAL_DETAILS_FIELD, "extra_fields"}:
            continue
        if key_text in field_ids:
            result[key_text] = value
        elif not is_empty(value):
            details[key_text] = value

    clean_excerpt = compact_text(raw_text, limit=1600)
    if clean_excerpt:
        excerpt_key = source_label or "原始资料摘录"
        details.setdefault(excerpt_key, clean_excerpt)

    if ADDITIONAL_DETAILS_FIELD in field_ids and details:
        result[ADDITIONAL_DETAILS_FIELD] = details
    return result, sorted(details)


def missing_required_fields(category_id: str, schema: dict[str, Any], data: dict[str, Any]) -> list[str]:
    missing = []
    for field in schema.get("fields", []) or []:
        field_id = str(field.get("id") or "")
        if field.get("required") and is_empty(data.get(field_id)):
            missing.append(field_id)
    for field_id in BUSINESS_REQUIRED_FIELDS.get(category_id, []):
        if is_empty(data.get(field_id)):
            missing.append(field_id)
    if category_id == "products":
        tier_problem = validate_price_tiers(data.get("price_tiers"))
        if tier_problem and "price_tiers" not in missing:
            missing.append("price_tiers")
    return dedupe(missing)


def build_followup_question(schema: dict[str, Any], missing_fields: list[str]) -> str:
    if not missing_fields:
        return ""
    labels = field_labels(schema, missing_fields)
    return "还需要补充：" + "、".join(labels) + "。请补齐后再应用入库。"


def field_labels(schema: dict[str, Any], field_ids: list[str]) -> list[str]:
    fields = {str(field.get("id") or ""): field for field in schema.get("fields", []) or []}
    return [str(fields.get(field_id, {}).get("label") or field_id) for field_id in field_ids]


def risk_warnings(data: dict[str, Any]) -> list[str]:
    text = json.dumps(data, ensure_ascii=False)
    warnings = []
    for keyword in risk_keywords("knowledge_intake"):
        if keyword in text:
            warnings.append(f"包含高风险关键词：{keyword}")
    for keyword in HARD_HANDOFF_KEYWORDS:
        if keyword in text:
            warnings.append(f"明确要求人工确认：{keyword}")
    return dedupe(warnings)


def normalize_runtime(runtime: dict[str, Any], warnings: list[str]) -> dict[str, Any]:
    hard_handoff = any("明确要求人工确认" in warning for warning in warnings)
    existing_handoff = bool(runtime.get("requires_handoff", False))
    explicit_auto_reply = runtime.get("allow_auto_reply")
    requires_handoff = existing_handoff or hard_handoff or explicit_auto_reply is False
    risk_level = str(runtime.get("risk_level") or ("warning" if warnings else "normal"))
    if hard_handoff:
        risk_level = "high"
    return {
        "allow_auto_reply": bool(runtime.get("allow_auto_reply", not requires_handoff)),
        "requires_handoff": requires_handoff,
        "risk_level": risk_level,
    }


def validate_price_tiers(value: Any) -> str:
    if not value:
        return ""
    if not isinstance(value, list):
        return "阶梯价格格式不正确"
    previous_quantity = 0.0
    previous_price = float("inf")
    for index, row in enumerate(value, start=1):
        if not isinstance(row, dict):
            return f"第 {index} 档阶梯价格格式不正确"
        quantity = as_float(row.get("min_quantity"))
        price = as_float(row.get("unit_price"))
        if quantity is None or price is None:
            return f"第 {index} 档阶梯价格缺少数量或价格"
        if quantity <= previous_quantity:
            return f"第 {index} 档数量必须高于上一档"
        if price >= previous_price:
            return f"第 {index} 档价格必须低于上一档"
        previous_quantity = quantity
        previous_price = price
    return ""


def first_valid_tier_price(value: Any) -> float | None:
    if not isinstance(value, list):
        return None
    valid_rows: list[tuple[float, float]] = []
    for row in value:
        if not isinstance(row, dict):
            continue
        quantity = as_float(row.get("min_quantity"))
        price = as_float(row.get("unit_price"))
        if quantity is None or price is None:
            continue
        valid_rows.append((quantity, price))
    if not valid_rows:
        return None
    valid_rows.sort(key=lambda row: row[0])
    return valid_rows[0][1]


def compact_text(value: Any, *, limit: int) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text[:limit]


def is_empty(value: Any) -> bool:
    return value is None or value == "" or value == [] or value == {}


def as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def dedupe(values: list[str]) -> list[str]:
    seen = set()
    result = []
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        result.append(text)
        seen.add(text)
    return result
