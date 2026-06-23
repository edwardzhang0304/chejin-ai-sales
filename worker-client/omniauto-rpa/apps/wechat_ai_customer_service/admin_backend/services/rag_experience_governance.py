"""Unified final governance resolver for AI experience pool items.

This module intentionally does not write data. It folds quality, AI advice,
source authority, pollution guards and legacy review state into one effective
decision so UI, retrieval and nomination do not make conflicting choices.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from apps.wechat_ai_customer_service.admin_backend.services.knowledge_contamination_guard import (
    text_has_model_reply_marker,
    text_has_test_marker,
)
from apps.wechat_ai_customer_service.admin_backend.services.source_authority_policy import (
    PRODUCT_MASTER_CATEGORIES,
    evaluate_experience_source_authority,
    experience_source_types,
)


GOVERNANCE_POLICY_VERSION = "ai_experience_pool_governance_v2"
AUTO_TRIAGE_ACTIONS = {"discard", "already_covered"}
RETRIEVABLE_STATES: set[str] = set()
REAL_CHAT_SOURCES = {
    "cleaned_real_chat_pack",
    "real_chat",
    "real_chat_style",
    "wechat_raw_message",
    "raw_wechat_private",
    "raw_wechat_group",
    "raw_wechat_file_transfer",
}
PRODUCT_FACT_REASON_CODES = {
    "product_master_manual_intake_only",
    "product_master_facts_must_stay_manual",
    "rag_product_master_promotion_disabled",
}
STYLE_ONLY_REASON_CODES = {
    "observed_wechat_chat_experience_not_generalized",
    "observed_boundary_reply_not_generalized",
    "dynamic_product_recommendation_chat_stays_rag_only",
    *PRODUCT_FACT_REASON_CODES,
}
PRODUCT_FACT_TOKENS = (
    "商品资料",
    "商品名称",
    "型号",
    "SKU",
    "价格",
    "报价",
    "库存",
    "车源",
    "现车",
    "配置",
    "报价",
    "最低价",
    "到底价",
    "万公里",
    "宝马",
    "奔驰",
    "奥迪",
    "比亚迪",
    "凯美瑞",
    "秦PLUS",
)
STABLE_PROCESS_TOKENS = (
    "流程",
    "资料",
    "置换",
    "预约",
    "到店",
    "试驾",
    "看车",
    "过户",
    "检测",
    "照片",
    "上牌年份",
    "公里数",
)


def resolve_rag_experience_governance(item: dict[str, Any]) -> dict[str, Any]:
    """Return a single final governance decision for one AI experience pool item."""

    status = str(item.get("status") or "active")
    review = item.get("experience_review") if isinstance(item.get("experience_review"), dict) else {}
    review_status = str(review.get("status") or "")
    reviewed_by_user = bool(item.get("reviewed_by_user"))
    quality = item.get("quality") if isinstance(item.get("quality"), dict) else {}
    ai = item.get("ai_interpretation") if isinstance(item.get("ai_interpretation"), dict) else {}
    ai_action = str(ai.get("recommended_action") or "")
    ai_auto_triage = ai.get("auto_triage") if isinstance(ai.get("auto_triage"), dict) else {}
    ai_reason_code = str(ai_auto_triage.get("reason_code") or "").strip()
    relation = str(item.get("formal_relation") or "")
    source = str(item.get("source") or "")
    source_type = str(item.get("source_type") or "")
    text = governance_text(item)
    polluted_text = governance_pollution_text(item)
    is_real_chat = experience_is_real_chat(item)
    has_pollution = (
        text_has_test_marker(polluted_text)
        or text_has_model_reply_marker(governance_model_reply_text(item))
        or source_type == "raw_wechat_file_transfer"
    )
    source_decision = source_authority_decision(item)
    source_reason = str(source_decision.get("reason") or "")
    product_fact = is_product_fact_like(item, text=polluted_text, source_reason=source_reason or ai_reason_code)
    inputs = {
        "status": status,
        "review_status": review_status,
        "reviewed_by_user": reviewed_by_user,
        "quality_band": quality.get("band"),
        "quality_retrieval_allowed": bool(quality.get("retrieval_allowed")),
        "ai_recommended_action": ai_action,
        "ai_reason_code": ai_reason_code,
        "formal_relation": relation,
        "source": source,
        "source_type": source_type,
    }

    if status == "promoted":
        return governance_decision(
            "promoted",
            "respect_promoted",
            "已升级为待确认知识",
            "这条经验已经进入候选或正式知识处理链路，不再重复参与运行时检索。",
            retrieval_allowed=False,
            promotion_allowed=False,
            style_allowed=False,
            risk_level="medium",
            source_authority=source_decision,
            inputs=inputs,
        )
    if status == "discarded":
        user_discard = reviewed_by_user or bool(item.get("discarded_at"))
        return governance_decision(
            "user_discarded" if user_discard else "auto_discarded",
            "respect_user_discard" if user_discard else "auto_discard",
            "已废弃，不参与回答",
            "这条经验已经被废弃或系统自动降噪，不参与AI经验池参考、候选晋升或风格学习。",
            retrieval_allowed=False,
            promotion_allowed=False,
            style_allowed=False,
            risk_level="blocked",
            source_authority=source_decision,
            inputs=inputs,
        )
    if has_pollution:
        return governance_decision(
            "blocked",
            "block_by_policy",
            "已被污染防护阻断",
            "检测到文件传输助手、测试标记或AI自回复痕迹，不允许进入学习、检索或晋升链路。",
            retrieval_allowed=False,
            promotion_allowed=False,
            style_allowed=False,
            risk_level="blocked",
            source_authority=source_decision,
            inputs=inputs,
        )
    if reviewed_by_user and review_status == "kept" and product_fact:
        return governance_decision(
            "blocked",
            "block_by_policy",
            "人工已保留，但禁止作为回答依据",
            "这条内容命中商品主数据或具体商品事实边界，人工保留只保留审计动作，不能作为客户回复依据。",
            retrieval_allowed=False,
            promotion_allowed=False,
            style_allowed=is_real_chat,
            risk_level="blocked",
            source_authority=source_decision,
            inputs=inputs,
        )
    if auto_triaged_as_discard(review) or (
        ai_action in AUTO_TRIAGE_ACTIONS and review_status == "auto_kept" and not reviewed_by_user
    ):
        reason_code = source_reason or ai_reason_code
        if is_real_chat and reason_code in STYLE_ONLY_REASON_CODES:
            return governance_decision(
                "style_only",
                "keep_style_only",
                "仅作话术风格参考",
                "这条真实聊天样本不适合作为客户回复依据或正式知识候选，但可用于学习客服表达方式。",
                retrieval_allowed=False,
                promotion_allowed=False,
                style_allowed=True,
                risk_level="medium",
                source_authority=source_decision,
                inputs=inputs,
            )
        return governance_decision(
            "auto_discarded",
            "auto_discard",
            "建议废弃，已从AI经验池参考降噪",
            str(ai.get("action_reason") or ai_auto_triage.get("reason") or "系统判断这条经验不适合继续参与审核、检索或晋升。"),
            retrieval_allowed=False,
            promotion_allowed=False,
            style_allowed=False,
            risk_level="blocked",
            source_authority=source_decision,
            inputs=inputs,
        )
    if not source_decision.get("allowed", True):
        if is_real_chat:
            return governance_decision(
                "style_only",
                "keep_style_only",
                "仅作话术风格参考",
                str(source_decision.get("message") or "真实聊天来源不适合作为正式知识依据，仅保留表达风格价值。"),
                retrieval_allowed=False,
                promotion_allowed=False,
                style_allowed=True,
                risk_level="medium",
                source_authority=source_decision,
                inputs=inputs,
            )
        return governance_decision(
            "blocked",
            "block_by_policy",
            "来源规则阻断",
            str(source_decision.get("message") or "这条经验来源不具备对应知识权限。"),
            retrieval_allowed=False,
            promotion_allowed=False,
            style_allowed=False,
            risk_level="blocked",
            source_authority=source_decision,
            inputs=inputs,
        )
    if ai_action == "promote_to_pending" and bool(ai.get("promotion_allowed", True)):
        return governance_decision(
            "candidate_suggested",
            "suggest_candidate",
            "建议生成待确认知识",
            "这条经验看起来有稳定业务价值，可生成待确认知识，但仍需人工审核后才能进入正式知识库。",
            retrieval_allowed=False,
            promotion_allowed=True,
            candidate_auto_create_allowed=True,
            style_allowed=is_real_chat,
            risk_level="low",
            source_authority=source_decision,
            inputs=inputs,
        )
    if product_fact and is_real_chat:
        return governance_decision(
            "style_only",
            "keep_style_only",
            "仅作话术风格参考",
            "真实聊天中包含商品事实、价格或车源线索，不参与客户回复依据，但可借鉴表达方式。",
            retrieval_allowed=False,
            promotion_allowed=False,
            style_allowed=True,
            risk_level="medium",
            source_authority=source_decision,
            inputs=inputs,
        )
    if review_status in {"kept", "auto_kept"} and bool(quality.get("retrieval_allowed")):
        return governance_decision(
            "kept_experience",
            "keep_in_ai_experience_pool",
            "已吸纳为AI经验池，不参与回答依据",
            "这条经验通过质量和来源检查，保留在AI经验池用于治理、候选分发或风格学习，但不直接作为客户回复内容依据。",
            retrieval_allowed=False,
            promotion_allowed=False,
            style_allowed=is_real_chat,
            risk_level="low",
            source_authority=source_decision,
            inputs=inputs,
        )
    if is_stable_process_candidate(text) and ai_action in {"keep_as_experience", "manual_review"} and not product_fact:
        return governance_decision(
            "candidate_suggested",
            "suggest_candidate",
            "建议生成待确认知识",
            "这条经验像稳定流程或资料收集话术，可提名为候选知识等待人工审核。",
            retrieval_allowed=False,
            promotion_allowed=True,
            candidate_auto_create_allowed=True,
            style_allowed=is_real_chat,
            risk_level="low",
            source_authority=source_decision,
            inputs=inputs,
        )
    if review_status in {"kept", "auto_kept"}:
        return governance_decision(
            "style_only" if is_real_chat else "pending_review",
            "keep_style_only" if is_real_chat else "wait_for_review",
            "仅作话术风格参考" if is_real_chat else "待处理，暂不参与回答",
        "当前证据不足以作为运行时回答依据；真实聊天可保留表达风格，其余内容等待人工处理。",
            retrieval_allowed=False,
            promotion_allowed=False,
            style_allowed=is_real_chat,
            risk_level="medium",
            source_authority=source_decision,
            inputs=inputs,
        )
    return governance_decision(
        "pending_review",
        "wait_for_review",
        "待处理，暂不参与回答",
        "这条经验还没有通过人工或系统治理确认，不参与AI经验池参考或候选晋升。",
        retrieval_allowed=False,
        promotion_allowed=False,
        style_allowed=is_real_chat,
        risk_level="medium",
        source_authority=source_decision,
        inputs=inputs,
    )


def attach_governance(item: dict[str, Any]) -> dict[str, Any]:
    enriched = dict(item)
    enriched["governance"] = resolve_rag_experience_governance(enriched)
    return enriched


def governance_allows_retrieval(item: dict[str, Any]) -> bool:
    governance = item.get("governance") if isinstance(item.get("governance"), dict) else resolve_rag_experience_governance(item)
    return bool(governance.get("retrieval_allowed")) and str(governance.get("effective_state") or "") in RETRIEVABLE_STATES


def governance_display_state(item: dict[str, Any], *, fallback: str = "pending") -> str:
    governance = item.get("governance") if isinstance(item.get("governance"), dict) else resolve_rag_experience_governance(item)
    effective = str(governance.get("effective_state") or "")
    if effective in {"auto_discarded", "user_discarded", "blocked"}:
        return "discarded"
    if effective in {"promoted", "candidate_created"}:
        return "promoted"
    if effective in {"retrievable_experience", "kept_experience", "style_only", "candidate_suggested"}:
        return "kept"
    if effective == "pending_review":
        return "pending"
    return fallback


def governance_counts(items: list[dict[str, Any]]) -> dict[str, int]:
    states = {
        "pending_review": 0,
        "retrievable_experience": 0,
        "kept_experience": 0,
        "style_only": 0,
        "candidate_suggested": 0,
        "candidate_created": 0,
        "auto_discarded": 0,
        "user_discarded": 0,
        "promoted": 0,
        "blocked": 0,
        "unknown": 0,
    }
    for item in items:
        governance = item.get("governance") if isinstance(item.get("governance"), dict) else resolve_rag_experience_governance(item)
        state = str(governance.get("effective_state") or "unknown")
        states[state if state in states else "unknown"] += 1
    states["total"] = len(items)
    states["accounted_total"] = sum(value for key, value in states.items() if key not in {"total", "accounted_total", "consistent"})
    states["consistent"] = int(states["accounted_total"] == states["total"])
    return states


def governance_decision(
    effective_state: str,
    final_action: str,
    display_label: str,
    reason: str,
    *,
    retrieval_allowed: bool,
    promotion_allowed: bool,
    style_allowed: bool,
    risk_level: str,
    source_authority: dict[str, Any],
    inputs: dict[str, Any],
    candidate_auto_create_allowed: bool = False,
    requires_manual_review: bool = False,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "policy_version": GOVERNANCE_POLICY_VERSION,
        "evaluated_at": datetime.now().isoformat(timespec="seconds"),
        "effective_state": effective_state,
        "final_action": final_action,
        "display_label": display_label,
        "reason": str(reason or display_label),
        "retrieval_allowed": bool(retrieval_allowed),
        "promotion_allowed": bool(promotion_allowed),
        "candidate_auto_create_allowed": bool(candidate_auto_create_allowed),
        "style_allowed": bool(style_allowed),
        "requires_manual_review": bool(requires_manual_review),
        "risk_level": risk_level,
        "source_authority": source_authority,
        "inputs": inputs,
    }


def source_authority_decision(item: dict[str, Any]) -> dict[str, Any]:
    category = "products" if item_category(item) in PRODUCT_MASTER_CATEGORIES else "chats"
    try:
        return evaluate_experience_source_authority(item, category)
    except Exception as exc:  # pragma: no cover - defensive guard for damaged legacy records.
        return {
            "allowed": False,
            "category": category,
            "reason": "source_authority_evaluation_failed",
            "message": f"来源权限判断失败：{exc}",
            "source_types": sorted(experience_source_types(item)),
            "policy_version": "source_authority_v2",
        }


def item_category(item: dict[str, Any]) -> str:
    hit = item.get("rag_hit") if isinstance(item.get("rag_hit"), dict) else {}
    return str(item.get("category") or hit.get("category") or "").strip()


def governance_text(item: dict[str, Any]) -> str:
    parts = {
        "summary": item.get("summary"),
        "question": item.get("question"),
        "reply_text": item.get("reply_text"),
        "evidence_excerpt": item.get("evidence_excerpt"),
        "source_dialogue": item.get("source_dialogue"),
        "rag_hit": item.get("rag_hit"),
        "ai_interpretation": item.get("ai_interpretation"),
    }
    return json.dumps(parts, ensure_ascii=False, default=str)


def governance_pollution_text(item: dict[str, Any]) -> str:
    """Return only user/business content that is safe to scan for pollution.

    AI interpretation metadata can legitimately contain provider names such as
    ``unit_test_model`` or words like "内部规则" while explaining why an item was
    discarded. Treating that metadata as source text causes false contamination
    blocks, so pollution checks intentionally use the observable conversation and
    retrieved evidence only.
    """

    hit = item.get("rag_hit") if isinstance(item.get("rag_hit"), dict) else {}
    parts = {
        "summary": item.get("summary"),
        "question": item.get("question"),
        "reply_text": item.get("reply_text"),
        "raw_reply_text": item.get("raw_reply_text"),
        "evidence_excerpt": item.get("evidence_excerpt"),
        "source_dialogue": item.get("source_dialogue"),
        "rag_hit_text": hit.get("text"),
        "rag_hit_source_type": hit.get("source_type"),
    }
    return json.dumps(parts, ensure_ascii=False, default=str)


def governance_model_reply_text(item: dict[str, Any]) -> str:
    """Return text fields where model/self-reply markers are meaningful."""

    hit = item.get("rag_hit") if isinstance(item.get("rag_hit"), dict) else {}
    parts = {
        "reply_text": item.get("reply_text"),
        "raw_reply_text": item.get("raw_reply_text"),
        "evidence_excerpt": item.get("evidence_excerpt"),
        "source_dialogue": item.get("source_dialogue"),
        "rag_hit_text": hit.get("text"),
    }
    return json.dumps(parts, ensure_ascii=False, default=str)


def experience_is_real_chat(item: dict[str, Any]) -> bool:
    source_types = experience_source_types(item)
    return bool(source_types & REAL_CHAT_SOURCES) or str(item.get("source") or "") == "real_chat_style"


def is_product_fact_like(item: dict[str, Any], *, text: str, source_reason: str) -> bool:
    if item_category(item) in PRODUCT_MASTER_CATEGORIES:
        return True
    if source_reason in PRODUCT_FACT_REASON_CODES:
        return True
    return any(token and token in text for token in PRODUCT_FACT_TOKENS)


def auto_triaged_as_discard(review: dict[str, Any]) -> bool:
    if str(review.get("status") or "") != "auto_triaged":
        return False
    return str(review.get("auto_triage_action") or "") in AUTO_TRIAGE_ACTIONS


def is_stable_process_candidate(text: str) -> bool:
    if not any(token in text for token in STABLE_PROCESS_TOKENS):
        return False
    return not any(token in text for token in ("最低价", "到底价", "包过", "保证", "现车", "库存"))
