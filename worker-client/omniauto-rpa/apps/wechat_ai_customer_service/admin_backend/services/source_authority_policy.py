"""Source-authority policy for AI-assisted knowledge candidates.

Observed conversations are useful evidence for experience and reply-style
learning, but they are not authoritative product master data.
"""

from __future__ import annotations

import json
import re
from typing import Any

from apps.wechat_ai_customer_service.platform_safety_rules import guard_term_set, load_platform_safety_rules


OBSERVED_WECHAT_SOURCE_TYPES = {
    "cleaned_real_chat_pack",
    "real_chat",
    "raw_wechat_private",
    "raw_wechat_group",
    "raw_wechat_file_transfer",
    "wechat_raw_message",
}
SOURCE_AUTHORITY_POLICY_VERSION = "source_authority_v2"
PRODUCT_MASTER_CATEGORIES = {"products", "erp_exports"}
PRODUCT_SCOPED_CATEGORIES = {"product_faq", "product_rules", "product_explanations"}
FALLBACK_PERSONALIZED_TOKENS = ("许聪", "许哥", "你们店里", "到店看车")
FALLBACK_SITUATIONAL_HANDOFF_TOKENS = ("转人工", "同事跟进", "人工确认", "请示")
FALLBACK_FINANCE_BOUNDARY_TOKENS = ("首付", "月供", "贷款", "征信", "按揭", "利率")
PERSONAL_NAME_SCENE_RE = re.compile(r"[\u4e00-\u9fff]{2,4}(?:询问|问|说|表示)")
DYNAMIC_RECOMMENDATION_INTENT_TOKENS = (
    "推荐",
    "优先看",
    "可以看",
    "适合看",
    "帮我看",
    "预算",
    "代步",
    "通勤",
    "家用",
    "省油",
    "自动挡",
    "车源",
)
DYNAMIC_RECOMMENDATION_RUNTIME_TOKENS = (
    "库存",
    "现车",
    "在售",
    "价格",
    "报价",
    "最低价",
    "优惠",
    "到店",
    "今天",
    "目前",
    "当前",
)
DYNAMIC_RECOMMENDATION_PRODUCT_HINTS = (
    "凯美瑞",
    "思域",
    "秦PLUS",
    "秦PLUS DM-i",
    "秦",
    "轩逸",
    "卡罗拉",
    "雅阁",
    "帕萨特",
    "朗逸",
    "GL8",
    "宝马",
    "奔驰",
    "奥迪",
    "比亚迪",
    "丰田",
    "本田",
    "日产",
    "大众",
    "别克",
    "新能源",
    "SUV",
    "MPV",
)
DYNAMIC_RECOMMENDATION_MODEL_LIST_RE = re.compile(
    r"(?:推荐|优先看|可以看|适合看|匹配|筛).{0,40}(?:、|,|，|/).{0,80}(?:车|PLUS|DM-i|GL8|SUV|MPV|凯美瑞|思域|雅阁|朗逸|轩逸)"
)


def visible_rule_patterns(group: str) -> list[str]:
    rules = load_platform_safety_rules().get("item", {})
    return sorted(guard_term_set(rules, group))


def matches_visible_patterns(text: str, group: str) -> bool:
    return any(re.search(pattern, text) for pattern in visible_rule_patterns(group))


def contains_any_token(text: str, tokens: tuple[str, ...]) -> bool:
    return any(token in text for token in tokens)


def text_has_dynamic_product_recommendation(text: str) -> bool:
    """Detect inventory-dependent recommendation snippets that must stay outside formal chats."""
    value = str(text or "")
    if not value.strip():
        return False
    lowered = value.lower()
    product_hits = {token for token in DYNAMIC_RECOMMENDATION_PRODUCT_HINTS if token.lower() in lowered}
    has_product_hint = bool(product_hits)
    has_multiple_products = len(product_hits) >= 2
    has_intent = contains_any_token(value, DYNAMIC_RECOMMENDATION_INTENT_TOKENS)
    has_runtime_fact = contains_any_token(value, DYNAMIC_RECOMMENDATION_RUNTIME_TOKENS)
    has_budget = bool(re.search(r"\d+(?:\.\d+)?\s*万", value)) or "预算" in value
    if has_multiple_products and (has_intent or has_budget):
        return True
    if has_product_hint and has_runtime_fact and (has_intent or has_budget):
        return True
    if DYNAMIC_RECOMMENDATION_MODEL_LIST_RE.search(value):
        return True
    return False


def candidate_target_category(candidate: dict[str, Any]) -> str:
    proposal = candidate.get("proposal") if isinstance(candidate.get("proposal"), dict) else {}
    patch = proposal.get("formal_patch") if isinstance(proposal.get("formal_patch"), dict) else {}
    return str(patch.get("target_category") or proposal.get("target_category") or "").strip()


def candidate_item_data(candidate: dict[str, Any]) -> dict[str, Any]:
    proposal = candidate.get("proposal") if isinstance(candidate.get("proposal"), dict) else {}
    patch = proposal.get("formal_patch") if isinstance(proposal.get("formal_patch"), dict) else {}
    item = patch.get("item") if isinstance(patch.get("item"), dict) else {}
    data = item.get("data") if isinstance(item.get("data"), dict) else {}
    return data


def candidate_source_types(candidate: dict[str, Any]) -> set[str]:
    source = candidate.get("source") if isinstance(candidate.get("source"), dict) else {}
    review = candidate.get("review") if isinstance(candidate.get("review"), dict) else {}
    proposal = candidate.get("proposal") if isinstance(candidate.get("proposal"), dict) else {}
    patch = proposal.get("formal_patch") if isinstance(proposal.get("formal_patch"), dict) else {}
    item = patch.get("item") if isinstance(patch.get("item"), dict) else {}
    item_source = item.get("source") if isinstance(item.get("source"), dict) else {}
    values = {
        str(source.get("type") or ""),
        str(source.get("original_type") or ""),
        str(source.get("original_channel") or ""),
        str(item_source.get("type") or ""),
        str(item_source.get("original_type") or ""),
        str(item_source.get("candidate_source_type") or ""),
    }
    for item in review.get("source_chain", []) if isinstance(review.get("source_chain"), list) else []:
        values.add(str(item or ""))
    rag_hit = source.get("rag_hit") if isinstance(source.get("rag_hit"), dict) else {}
    values.add(str(rag_hit.get("source_type") or ""))
    for hit in source.get("rag_hits", []) if isinstance(source.get("rag_hits"), list) else []:
        if isinstance(hit, dict):
            values.add(str(hit.get("source_type") or ""))
    return {value for value in values if value}


def is_observed_wechat_source(source_types: set[str]) -> bool:
    return bool(source_types & OBSERVED_WECHAT_SOURCE_TYPES) or any(value.startswith("raw_wechat_") for value in source_types)


def candidate_contains_model_reply(candidate: dict[str, Any]) -> bool:
    source = candidate.get("source") if isinstance(candidate.get("source"), dict) else {}
    review = candidate.get("review") if isinstance(candidate.get("review"), dict) else {}
    if bool(source.get("contains_model_reply") or review.get("source_contains_model_reply")):
        return True
    text = json.dumps(
        {
            "evidence_excerpt": source.get("evidence_excerpt"),
            "summary": (candidate.get("proposal") or {}).get("summary") if isinstance(candidate.get("proposal"), dict) else "",
            "suggested_fields": (candidate.get("proposal") or {}).get("suggested_fields")
            if isinstance(candidate.get("proposal"), dict)
            else {},
        },
        ensure_ascii=False,
        default=str,
    )
    return any(marker in text for marker in visible_rule_patterns("model_reply_markers"))


def candidate_review_text(candidate: dict[str, Any]) -> str:
    source = candidate.get("source") if isinstance(candidate.get("source"), dict) else {}
    review = candidate.get("review") if isinstance(candidate.get("review"), dict) else {}
    proposal = candidate.get("proposal") if isinstance(candidate.get("proposal"), dict) else {}
    data = candidate_item_data(candidate)
    return json.dumps(
        {
            "source": source,
            "review": {
                "rag_evidence": review.get("rag_evidence"),
                "source_authority": review.get("source_authority"),
            },
            "proposal_summary": proposal.get("summary"),
            "data": data,
        },
        ensure_ascii=False,
        default=str,
    )


def observed_chat_candidate_is_too_specific(candidate: dict[str, Any]) -> bool:
    data = candidate_item_data(candidate)
    proposal = candidate.get("proposal") if isinstance(candidate.get("proposal"), dict) else {}
    customer_message = str(data.get("customer_message") or "")
    service_reply = str(data.get("service_reply") or data.get("answer") or "")
    primary_text = "\n".join(
        str(value or "")
        for value in (
            customer_message,
            service_reply,
            data.get("title"),
            data.get("summary"),
            proposal.get("summary"),
        )
    )
    if matches_visible_patterns(primary_text, "personalized_reply_patterns"):
        return True
    if contains_any_token(primary_text, FALLBACK_PERSONALIZED_TOKENS) or bool(PERSONAL_NAME_SCENE_RE.search(primary_text)):
        return True
    if matches_visible_patterns(service_reply, "situational_handoff_patterns"):
        return True
    if contains_any_token(service_reply, FALLBACK_SITUATIONAL_HANDOFF_TOKENS):
        return True
    if matches_visible_patterns(customer_message + "\n" + service_reply, "finance_boundary_patterns"):
        return True
    if contains_any_token(customer_message + "\n" + service_reply, FALLBACK_FINANCE_BOUNDARY_TOKENS):
        return True
    return False


def product_scoped_has_product(candidate: dict[str, Any]) -> bool:
    data = candidate_item_data(candidate)
    return bool(str(data.get("product_id") or "").strip())


def evaluate_candidate_source_authority(candidate: dict[str, Any]) -> dict[str, Any]:
    category = candidate_target_category(candidate)
    source_types = candidate_source_types(candidate)
    observed_wechat = is_observed_wechat_source(source_types)
    contains_model_reply = candidate_contains_model_reply(candidate)

    if category in PRODUCT_MASTER_CATEGORIES:
        return denied(
            category,
            source_types,
            "product_master_manual_intake_only",
            "商品资料属于权威主数据，不能通过候选知识链路写入；请走商品库手动导入/维护入口。",
        )
    if observed_wechat and contains_model_reply and category != "chats":
        return denied(
            category,
            source_types,
            "model_reply_cannot_be_factual_source",
            "这条内容包含AI自动回复，只能用于话术经验参考，不能作为商品、政策或商品专属事实的来源。",
        )
    if observed_wechat and category in PRODUCT_SCOPED_CATEGORIES and not product_scoped_has_product(candidate):
        return denied(
            category,
            source_types,
            "product_scoped_wechat_candidate_requires_existing_product",
            "聊天中整理出的商品专属问答、规则或解释必须先关联到已有商品；没有关联商品时只能保留为AI经验。",
        )
    if category == "chats" and text_has_dynamic_product_recommendation(candidate_review_text(candidate)):
        return denied(
            category,
            source_types,
            "dynamic_product_recommendation_chat_stays_rag_only",
            "这条话术包含具体商品/车型推荐、库存或价格时效信息，不能写入正式话术；请保留在AI经验池，正式回复应在运行时读取商品库后再推荐。",
        )
    if observed_wechat and category == "chats" and observed_chat_candidate_is_too_specific(candidate):
        return denied(
            category,
            source_types,
            "observed_wechat_chat_candidate_not_generalized",
            "这条聊天话术带有具体客户、人称、转人工场景或金融边界，不能直接写入正式话术；请保留在AI经验池，或人工改写成通用边界规则后再提交。",
        )

    return {
        "allowed": True,
        "category": category,
        "source_types": sorted(source_types),
        "observed_wechat": observed_wechat,
        "contains_model_reply": contains_model_reply,
        "policy_version": SOURCE_AUTHORITY_POLICY_VERSION,
    }


def denied(category: str, source_types: set[str], reason: str, message: str) -> dict[str, Any]:
    return {
        "allowed": False,
        "category": category,
        "source_types": sorted(source_types),
        "reason": reason,
        "message": message,
        "observed_wechat": is_observed_wechat_source(source_types),
        "policy_version": SOURCE_AUTHORITY_POLICY_VERSION,
    }


def mark_candidate_source_policy(candidate: dict[str, Any], decision: dict[str, Any]) -> None:
    candidate.setdefault("review", {})["source_authority"] = decision
    if not decision.get("allowed"):
        candidate.setdefault("intake", {}).setdefault("warnings", []).append(str(decision.get("message") or decision.get("reason") or ""))


def experience_source_types(item: dict[str, Any]) -> set[str]:
    hit = item.get("rag_hit") if isinstance(item.get("rag_hit"), dict) else {}
    values = {
        str(item.get("source") or ""),
        str(item.get("source_type") or ""),
        str(item.get("original_type") or ""),
        str(item.get("original_source_type") or ""),
        str(hit.get("source_type") or ""),
    }
    original = item.get("original_source") if isinstance(item.get("original_source"), dict) else {}
    values.update(
        {
            str(original.get("type") or ""),
            str(original.get("source_type") or ""),
            str(original.get("conversation_type") or ""),
        }
    )
    for key in ("source_chain", "detected_tags"):
        if isinstance(item.get(key), list):
            values.update(str(value or "") for value in item.get(key) or [])
    return {value for value in values if value}


def experience_contains_model_reply(item: dict[str, Any]) -> bool:
    text = json.dumps(
        {
            "summary": item.get("summary"),
            "question": item.get("question"),
            "reply_text": item.get("reply_text"),
            "evidence_excerpt": item.get("evidence_excerpt"),
            "rag_hit": item.get("rag_hit"),
        },
        ensure_ascii=False,
        default=str,
    )
    return any(marker in text for marker in visible_rule_patterns("model_reply_markers"))


def experience_review_text(item: dict[str, Any]) -> str:
    return json.dumps(
        {
            "summary": item.get("summary"),
            "question": item.get("question"),
            "reply_text": item.get("reply_text"),
            "evidence_excerpt": item.get("evidence_excerpt"),
            "source_dialogue": item.get("source_dialogue"),
            "rag_hit": item.get("rag_hit"),
            "ai_interpretation": item.get("ai_interpretation"),
        },
        ensure_ascii=False,
        default=str,
    )


def observed_chat_experience_is_too_specific(item: dict[str, Any]) -> bool:
    text = experience_review_text(item)
    if matches_visible_patterns(text, "personalized_reply_patterns"):
        return True
    if contains_any_token(text, FALLBACK_PERSONALIZED_TOKENS) or bool(PERSONAL_NAME_SCENE_RE.search(text)):
        return True
    if matches_visible_patterns(text, "situational_handoff_patterns"):
        return True
    if contains_any_token(text, FALLBACK_SITUATIONAL_HANDOFF_TOKENS):
        return True
    if matches_visible_patterns(text, "finance_boundary_patterns"):
        return True
    if contains_any_token(text, FALLBACK_FINANCE_BOUNDARY_TOKENS):
        return True
    return False


def evaluate_experience_source_authority(item: dict[str, Any], category: str) -> dict[str, Any]:
    source_types = experience_source_types(item)
    observed_wechat = is_observed_wechat_source(source_types)
    contains_model_reply = experience_contains_model_reply(item)
    if category in PRODUCT_MASTER_CATEGORIES:
        return denied(
            category,
            source_types,
            "rag_product_master_promotion_disabled",
            "商品资料属于权威主数据，AI经验池不能升级为商品资料；请通过商品库手动导入/维护。",
        )
    if observed_wechat and contains_model_reply and category != "chats":
        return denied(
            category,
            source_types,
            "model_reply_rag_cannot_promote_to_factual_knowledge",
            "这条AI经验包含AI自动回复，不能作为事实知识升级；如有价值，请保留在经验层或改成话术候选。",
        )
    if category == "chats" and text_has_dynamic_product_recommendation(experience_review_text(item)):
        return denied(
            category,
            source_types,
            "dynamic_product_recommendation_chat_stays_rag_only",
            "这条话术包含具体商品/车型推荐、库存或价格时效信息，不能升级为正式话术候选；请保留在AI经验池，正式回复应在运行时读取商品库后再推荐。",
        )
    if observed_wechat and category == "chats" and observed_chat_experience_is_too_specific(item):
        return denied(
            category,
            source_types,
            "observed_wechat_chat_experience_not_generalized",
            "这条AI经验带有具体客户、人称、转人工场景或金融边界，不能直接升级为正式话术候选；请保留在经验层，或人工改写成通用边界规则后再提交。",
        )
    return {
        "allowed": True,
        "category": category,
        "source_types": sorted(source_types),
        "observed_wechat": observed_wechat,
        "contains_model_reply": contains_model_reply,
        "policy_version": SOURCE_AUTHORITY_POLICY_VERSION,
    }
