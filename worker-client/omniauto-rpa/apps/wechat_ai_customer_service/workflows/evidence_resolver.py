"""Build classified evidence packs for the WeChat customer-service runtime."""

from __future__ import annotations

import re
from typing import Any

try:  # pragma: no cover - supports both package and script imports.
    from .knowledge_index import KnowledgeHit, KnowledgeIndex, business_intents, detect_intent_tags, infer_intent_tags_from_hits
    from .knowledge_runtime import KnowledgeRuntime
except ImportError:  # pragma: no cover
    from knowledge_index import KnowledgeHit, KnowledgeIndex, business_intents, detect_intent_tags, infer_intent_tags_from_hits
    from knowledge_runtime import KnowledgeRuntime
from apps.wechat_ai_customer_service.platform_understanding_rules import map_of_lists


HIGH_RISK_LEVELS = {"high"}
PRODUCT_SCOPED_CATEGORY_IDS = {"product_faq", "product_rules", "product_explanations"}


class EvidenceResolver:
    """Resolve a customer message into a compact category evidence pack."""

    def __init__(self, runtime: KnowledgeRuntime | None = None, index: KnowledgeIndex | None = None) -> None:
        self.runtime = runtime or KnowledgeRuntime()
        self.index = index or KnowledgeIndex(self.runtime)

    def resolve(self, text: str, *, context: dict[str, Any] | None = None, limit: int = 12) -> dict[str, Any]:
        context = context or {}
        intent_tags = detect_intent_tags(text)
        hits = self.index.search(text, context=context, intent_tags=intent_tags, limit=limit)
        intent_tags = infer_intent_tags_from_hits(intent_tags, hits)
        evidence_items = [build_evidence_item(hit, intent_tags) for hit in hits]
        safety = build_safety_summary(intent_tags, evidence_items, input_text=text)
        return {
            "schema_version": 1,
            "input_text": text,
            "intent_tags": intent_tags,
            "matched_categories": matched_categories(evidence_items),
            "evidence_items": evidence_items,
            "safety": safety,
            "context": sanitize_context(context),
        }


def build_evidence_item(hit: KnowledgeHit, intent_tags: list[str]) -> dict[str, Any]:
    item = hit.item
    data = item.get("data", {}) or {}
    runtime = item.get("runtime", {}) or {}
    risk_level = str(runtime.get("risk_level") or data.get("risk_level") or "normal")
    data_has_auto_reply = "allow_auto_reply" in data
    data_has_handoff = "requires_handoff" in data
    explicit_auto_reply = data.get("allow_auto_reply") if data_has_auto_reply else runtime.get("allow_auto_reply", True)
    explicit_requires_handoff = data.get("requires_handoff") if data_has_handoff else runtime.get("requires_handoff", False)
    runtime_requires_handoff = bool(runtime.get("requires_handoff", False)) and not data_has_handoff
    shared_risk_control = hit.category_id == "risk_control"
    # Privacy minimum collection is a guideline, not a handoff trigger, when the customer is providing data.
    if (
        shared_risk_control
        and "customer_data" in intent_tags
        and str(hit.item_id or "") == "shared_global_privacy_minimum_collection"
    ):
        shared_risk_control = False
    requires_handoff = bool(
        runtime_requires_handoff
        or explicit_requires_handoff
        or risk_level in HIGH_RISK_LEVELS
        or explicit_auto_reply is False
    )
    runtime_allows_auto_reply = runtime.get("allow_auto_reply", True) is not False or data_has_auto_reply
    allow_auto_reply = bool(explicit_auto_reply is not False and runtime_allows_auto_reply and not requires_handoff)
    handoff_reason = str(
        data.get("handoff_reason")
        or ("auto_reply_disabled" if explicit_auto_reply is False else "")
        or ("high_risk_item" if risk_level in HIGH_RISK_LEVELS else "")
        or ("shared_risk_control" if shared_risk_control and requires_handoff else "")
    )
    return {
        "category_id": hit.category_id,
        "item_id": hit.item_id,
        "title": hit.title,
        "matched_fields": list(hit.matched_fields),
        "match_reason": hit.match_reason,
        "confidence": hit.confidence,
        "knowledge_layer": knowledge_layer(item),
        "knowledge_track": knowledge_track(hit.category_id),
        "reply_excerpt": reply_excerpt(hit, intent_tags),
        "allow_auto_reply": allow_auto_reply,
        "requires_handoff": requires_handoff,
        "risk_level": risk_level,
        "handoff_reason": handoff_reason,
        "advisory_only": bool(shared_risk_control and not requires_handoff),
    }


def reply_excerpt(hit: KnowledgeHit, intent_tags: list[str]) -> str:
    data = hit.item.get("data", {}) or {}
    if hit.category_id == "products":
        return product_excerpt(data, intent_tags)
    if hit.category_id == "policies":
        return clip(str(data.get("answer") or ""))
    if hit.category_id == "chats":
        return clip(str(data.get("service_reply") or ""))
    if hit.category_id == "global_guidelines":
        return clip(str(data.get("guideline_text") or data.get("service_reply") or ""))
    if hit.category_id in {"reply_style", "risk_control"}:
        return clip(str(data.get("guideline_text") or data.get("service_reply") or data.get("answer") or data.get("content") or ""))
    if hit.category_id in PRODUCT_SCOPED_CATEGORY_IDS:
        return clip(str(data.get("answer") or data.get("content") or ""))
    for field in hit.resolver.get("reply_fields", []) or []:
        value = data.get(field)
        if value not in (None, "", [], {}):
            return clip(value)
    return ""


def product_excerpt(data: dict[str, Any], intent_tags: list[str]) -> str:
    name = str(data.get("name") or "")
    unit = str(data.get("unit") or "")
    parts = []
    if name:
        parts.append(name)
    if "shipping" in intent_tags and data.get("shipping_policy"):
        parts.append(str(data.get("shipping_policy")))
    elif "warranty" in intent_tags and data.get("warranty_policy"):
        parts.append(str(data.get("warranty_policy")))
    elif {"quote", "discount"} & set(intent_tags):
        if data.get("price") not in (None, ""):
            parts.append(f"price: {data.get('price')} / {unit}".strip())
        if data.get("price_tiers"):
            parts.append(f"tiers: {data.get('price_tiers')}")
    elif data.get("specs"):
        parts.append(str(data.get("specs")))
    elif data.get("shipping_policy"):
        parts.append(str(data.get("shipping_policy")))
    return clip(" | ".join(part for part in parts if part))


def build_safety_summary(intent_tags: list[str], evidence_items: list[dict[str, Any]], *, input_text: str = "") -> dict[str, Any]:
    reasons: list[str] = []
    must_handoff = False
    if "handoff" in intent_tags:
        reasons.append("handoff_intent_detected")
        must_handoff = True
    has_business_evidence = has_authoritative_business_evidence(intent_tags, evidence_items, input_text=input_text)
    for item in evidence_items:
        if item.get("requires_handoff"):
            if item.get("category_id") == "chats" and has_business_evidence:
                continue
            must_handoff = True
            reason = str(item.get("handoff_reason") or "matched_item_requires_handoff")
            if reason and reason not in reasons:
                reasons.append(reason)
    has_chat_evidence = any(item.get("category_id") == "chats" for item in evidence_items)
    style_only = bool(set(intent_tags) <= {"greeting", "small_talk"})
    customer_data_only = "customer_data" in intent_tags and not (set(intent_tags) - {"customer_data"})
    if not has_business_evidence and not style_only and not customer_data_only:
        if "unknown" in intent_tags or not has_chat_evidence or (set(intent_tags) & evidence_required_intents()):
            reasons.append("no_relevant_business_evidence")
            must_handoff = True
    return {
        "allowed_auto_reply": not must_handoff,
        "must_handoff": must_handoff,
        "reasons": dedupe(reasons),
    }


def evidence_required_intents() -> set[str]:
    return set(business_intents()) | {"product", "scene_product", "spec", "warranty"}


def has_authoritative_business_evidence(intent_tags: list[str], evidence_items: list[dict[str, Any]], *, input_text: str = "") -> bool:
    if asks_about_merchant_personal_preference(input_text):
        return False
    tag_set = set(intent_tags)
    for item in evidence_items:
        category_id = str(item.get("category_id") or "")
        if category_id == "products" or category_id in PRODUCT_SCOPED_CATEGORY_IDS:
            return True
        if category_id == "policies" and policy_item_matches_intent(item, tag_set):
            return True
    return False


def asks_about_merchant_personal_preference(text: str) -> bool:
    """Personal merchant/staff preferences are not authorized by product hits."""

    normalized = str(text or "")
    return bool(
        re.search(
            r"(?:你们老板|你老板|老板娘|店长|你们(?:员工|同事|客服|销售)|员工|同事|客服|销售|老板)"
            r"[^，。！？,.!?\n]{0,8}"
            r"(?:喜欢什么|喜欢哪|喜欢啥|偏好|爱好|什么颜色)",
            normalized,
        )
    )


def policy_item_matches_intent(item: dict[str, Any], tag_set: set[str]) -> bool:
    key = (str(item.get("item_id") or "") + " " + str(item.get("title") or "")).strip().lower()
    matched_fields = {str(value).lower() for value in item.get("matched_fields", []) or []}
    if matched_fields & {"keywords", "title", "question", "customer_message"}:
        return True
    for configured_key, configured_tags in map_of_lists("policy_key_tags").items():
        fragments = {fragment for fragment in configured_key.split("_") if fragment}
        tags = set(configured_tags)
        if configured_key in key or any(fragment in key for fragment in fragments):
            return bool(tags & tag_set)
    if "policy_type" in matched_fields:
        return bool(tag_set & business_intents())
    return False


def matched_categories(evidence_items: list[dict[str, Any]]) -> list[str]:
    order = {"products": 10, "product_rules": 18, "product_faq": 19, "product_explanations": 20, "policies": 30, "chats": 40, "global_guidelines": 80}
    categories = {str(item.get("category_id") or "") for item in evidence_items if item.get("category_id")}
    return sorted(categories, key=lambda category_id: (order.get(category_id, 90), category_id))


def sanitize_context(context: dict[str, Any]) -> dict[str, Any]:
    allowed = {"last_product_id", "last_product_name", "last_quantity", "last_shipping_city", "last_unit_price", "last_total"}
    return {key: context.get(key) for key in allowed if context.get(key) not in (None, "")}


def clip(value: Any, limit: int = 240) -> str:
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "..."


def dedupe(values: list[str]) -> list[str]:
    seen = set()
    result = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def knowledge_layer(item: dict[str, Any]) -> str:
    metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
    return str(item.get("_knowledge_layer") or metadata.get("knowledge_layer") or "tenant")


def knowledge_track(category_id: str) -> str:
    text = str(category_id or "")
    if text in {"products", "policies", "product_faq", "product_rules", "product_explanations"}:
        return "factual"
    if text in {"chats", "reply_style", "global_guidelines"}:
        return "style"
    if text in {"rag_experience"}:
        return "experience"
    if text in {"risk_control"}:
        return "factual"
    return "factual"
