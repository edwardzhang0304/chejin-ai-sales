"""Authority helpers for customer-service reply evidence.

The reply stack intentionally separates product facts, formal knowledge, current
conversation facts, the AI experience pool, and common-sense guidance. This
module keeps that ordering explicit so prompts and guards do not have to infer
authority from loose field names.
"""

from __future__ import annotations

from typing import Any, Iterable

from layer_contracts import layer_attribution


PRODUCT_MASTER = "product_master"
PRODUCT_SCOPED_FORMAL = "product_scoped_formal"
FORMAL_KNOWLEDGE = "formal_knowledge"
SHARED_PUBLIC_STRATEGY = "shared_public_strategy"
CANDIDATE_KNOWLEDGE = "candidate_knowledge"
RAG_EXPERIENCE = "rag_experience"
AI_EXPERIENCE_POOL = "ai_experience_pool"
LLM_COMMON_SENSE = "llm_common_sense"
STYLE_MEMORY = "style_memory"
CURRENT_CONVERSATION_FACT = "current_conversation_fact"
UNKNOWN = "unknown"

AUTHORITY_ORDER = [
    PRODUCT_MASTER,
    PRODUCT_SCOPED_FORMAL,
    FORMAL_KNOWLEDGE,
    CURRENT_CONVERSATION_FACT,
    SHARED_PUBLIC_STRATEGY,
    CANDIDATE_KNOWLEDGE,
    AI_EXPERIENCE_POOL,
    LLM_COMMON_SENSE,
    STYLE_MEMORY,
]

AUTHORITY_RANK = {name: len(AUTHORITY_ORDER) - index for index, name in enumerate(AUTHORITY_ORDER)}
CONTENT_BASIS_ALLOWED_LEVELS = {
    PRODUCT_MASTER,
    PRODUCT_SCOPED_FORMAL,
    FORMAL_KNOWLEDGE,
    CURRENT_CONVERSATION_FACT,
}
PRODUCT_MASTER_CATEGORY_ID = "products"
PRODUCT_SCOPED_CATEGORY_IDS = {"product_faq", "product_rules", "product_explanations"}
STYLE_CATEGORY_IDS = {"chats", "style_examples"}
FORMAL_CATEGORY_IDS = {
    "faq",
    "faqs",
    "policies",
    "policy",
    "erp_exports",
    "service_rules",
    "business_rules",
}
AI_EXPERIENCE_POOL_SOURCE_TYPES = {
    "rag_experience",
    "cleaned_real_chat_pack",
    "real_chat",
    "real_chat_style",
    "wechat_raw_message",
    "raw_wechat_private",
    "raw_wechat_group",
    "raw_wechat_file_transfer",
    "ai_recorder_chat",
    "chat_log",
    "upload",
}
STYLE_ONLY_SOURCE_TYPES = {
    "cleaned_real_chat_pack",
    "real_chat",
    "real_chat_style",
    "wechat_raw_message",
    "raw_wechat_private",
    "raw_wechat_group",
    "raw_wechat_file_transfer",
    "ai_recorder_chat",
}
PRODUCT_FACT_FIELDS = {
    "name",
    "sku",
    "category",
    "aliases",
    "specs",
    "price",
    "unit",
    "price_tiers",
    "inventory",
    "stock",
    "shipping_policy",
    "warranty_policy",
    "additional_details",
}


def authority_order_payload() -> list[dict[str, Any]]:
    """Return a prompt-friendly declaration of the evidence priority order."""

    return [
        {
            "level": PRODUCT_MASTER,
            "rank": authority_rank(PRODUCT_MASTER),
            "can_authorize_product_facts": True,
            "can_authorize_product_master_facts": True,
            "description": "商品事实唯一最高权威源：价格、库存、规格、状态、数量/使用记录、SKU、物流/售后等事实必须以此为准。",
        },
        {
            "level": PRODUCT_SCOPED_FORMAL,
            "rank": authority_rank(PRODUCT_SCOPED_FORMAL),
            "can_authorize_product_facts": False,
            "can_authorize_product_scoped_rules": True,
            "description": "商品专属 FAQ/规则/解释，可补充商品相关流程和禁用承诺，但不得覆盖商品主数据事实。",
        },
        {
            "level": FORMAL_KNOWLEDGE,
            "rank": authority_rank(FORMAL_KNOWLEDGE),
            "can_authorize_product_facts": False,
            "description": "正式知识库，回答门店流程、政策、服务边界和业务口径；与商品主数据冲突时必须让位。",
        },
        {
            "level": CURRENT_CONVERSATION_FACT,
            "rank": authority_rank(CURRENT_CONVERSATION_FACT),
            "can_authorize_product_facts": False,
            "description": "当前对话中客户刚刚明确提供的信息，可用于本轮上下文理解和资料登记，但不能反写商品库或正式知识。",
        },
        {
            "level": SHARED_PUBLIC_STRATEGY,
            "rank": authority_rank(SHARED_PUBLIC_STRATEGY),
            "can_authorize_product_facts": False,
            "description": "共享公共策略层，只提供跨账号客服策略、实体归一、闲聊处理和表达原则，不授权商品或政策事实。",
        },
        {
            "level": CANDIDATE_KNOWLEDGE,
            "rank": authority_rank(CANDIDATE_KNOWLEDGE),
            "can_authorize_product_facts": False,
            "description": "候选知识或待确认材料，只能辅助理解，不能作为事实承诺。",
        },
        {
            "level": AI_EXPERIENCE_POOL,
            "rank": authority_rank(AI_EXPERIENCE_POOL),
            "can_authorize_product_facts": False,
            "description": "AI经验池是原始资料治理和候选分发中枢，不直接作为客户回复内容依据。",
        },
        {
            "level": LLM_COMMON_SENSE,
            "rank": authority_rank(LLM_COMMON_SENSE),
            "can_authorize_product_facts": False,
            "description": "通用常识层只可做一般取舍分析，不得生成具体商品事实、价格、库存或检测结论。",
        },
    ]


def authority_rank(level: str | None) -> int:
    return AUTHORITY_RANK.get(str(level or UNKNOWN), 0)


def classify_evidence(
    item: dict[str, Any] | None,
    *,
    category_id: str | None = None,
    source_type: str | None = None,
) -> str:
    """Classify an evidence item into the project authority hierarchy."""

    payload = item if isinstance(item, dict) else {}
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    category = normalize_key(category_id or payload.get("category_id") or data.get("category_id"))
    layer = normalize_key(payload.get("_knowledge_layer") or payload.get("layer") or data.get("_knowledge_layer"))
    authority = normalize_key(payload.get("authority") or data.get("authority"))
    source_payload = payload.get("source") if isinstance(payload.get("source"), dict) else {}
    source = normalize_key(source_type or payload.get("source_type") or source_payload.get("type"))

    if layer == CURRENT_CONVERSATION_FACT or source in {"current_conversation", "current_conversation_fact"}:
        return CURRENT_CONVERSATION_FACT
    if layer == SHARED_PUBLIC_STRATEGY or source in {
        "shared_public_strategy",
        "shared_strategy",
        "global_guideline",
        "global_guidelines",
        "shared_guideline",
        "shared_guidelines",
    }:
        return SHARED_PUBLIC_STRATEGY
    if category == "rag_experience" or layer == AI_EXPERIENCE_POOL or source in AI_EXPERIENCE_POOL_SOURCE_TYPES:
        return AI_EXPERIENCE_POOL
    if category == PRODUCT_MASTER_CATEGORY_ID or layer == PRODUCT_MASTER or authority in {"product_master", "manual_product_master_only"}:
        return PRODUCT_MASTER
    if category in PRODUCT_SCOPED_CATEGORY_IDS or layer == "tenant_product":
        return PRODUCT_SCOPED_FORMAL
    if source in {"llm_common_sense", "common_sense"}:
        return LLM_COMMON_SENSE
    if category in STYLE_CATEGORY_IDS or source in {"style", "style_memory"}:
        return STYLE_MEMORY
    if category in FORMAL_CATEGORY_IDS or layer in {"tenant", "shared"}:
        return FORMAL_KNOWLEDGE
    if source in {"rag", "rag_chunk", "experience"} or payload.get("chunk_id"):
        return AI_EXPERIENCE_POOL
    return UNKNOWN


def annotate_authority(
    item: dict[str, Any],
    *,
    category_id: str | None = None,
    source_type: str | None = None,
) -> dict[str, Any]:
    result = dict(item)
    level = classify_evidence(result, category_id=category_id, source_type=source_type)
    result["authority_level"] = level
    result["authority_rank"] = authority_rank(level)
    result.update(layer_attribution(level))
    return result


def sort_evidence_by_authority(items: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    annotated = [annotate_authority(item) for item in items if isinstance(item, dict)]
    return sorted(annotated, key=lambda item: (-int(item.get("authority_rank") or 0), str(item.get("id") or "")))


def can_authorize_reply_content(
    item: dict[str, Any] | None,
    *,
    category_id: str | None = None,
    source_type: str | None = None,
) -> bool:
    """Return whether an evidence item may be used as customer-visible content basis."""

    payload = item if isinstance(item, dict) else {}
    level = classify_evidence(payload, category_id=category_id, source_type=source_type)
    if level not in CONTENT_BASIS_ALLOWED_LEVELS:
        return False
    if normalize_key(payload.get("runtime_enabled")) == "false":
        return False
    if normalize_key(payload.get("status")) in {"draft", "pending", "candidate", "discarded", "archived"}:
        return False
    return True


def can_authorize_product_fact(
    item: dict[str, Any] | None,
    *,
    category_id: str | None = None,
    source_type: str | None = None,
) -> bool:
    level = classify_evidence(item, category_id=category_id, source_type=source_type)
    return level == PRODUCT_MASTER


def can_authorize_formal_rule(
    item: dict[str, Any] | None,
    *,
    category_id: str | None = None,
    source_type: str | None = None,
) -> bool:
    level = classify_evidence(item, category_id=category_id, source_type=source_type)
    return level in {FORMAL_KNOWLEDGE, PRODUCT_SCOPED_FORMAL}


def can_affect_style(item: dict[str, Any] | None, *, source_type: str | None = None) -> bool:
    payload = item if isinstance(item, dict) else {}
    source_payload = payload.get("source") if isinstance(payload.get("source"), dict) else {}
    source = normalize_key(source_type or payload.get("source_type") or source_payload.get("type"))
    level = classify_evidence(payload, source_type=source_type)
    return level == STYLE_MEMORY or source in STYLE_ONLY_SOURCE_TYPES


def dedupe_authoritative_products(*buckets: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge product buckets without letting weaker duplicates override stronger ones."""

    best_by_key: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for bucket in buckets:
        for raw in bucket or []:
            if not isinstance(raw, dict):
                continue
            item = annotate_authority(raw, category_id=PRODUCT_MASTER_CATEGORY_ID)
            key = product_identity_key(item)
            if not key:
                continue
            previous = best_by_key.get(key)
            if previous is None:
                order.append(key)
                best_by_key[key] = item
                continue
            if int(item.get("authority_rank") or 0) > int(previous.get("authority_rank") or 0):
                best_by_key[key] = item
    return [best_by_key[key] for key in order if key in best_by_key]


def product_identity_key(item: dict[str, Any]) -> str:
    data = item.get("data") if isinstance(item.get("data"), dict) else {}
    for key in ("id", "sku", "name"):
        value = str(item.get(key) or data.get(key) or "").strip().lower()
        if value:
            return f"{key}:{value}"
    return ""


def normalize_key(value: Any) -> str:
    return str(value or "").strip().lower()
