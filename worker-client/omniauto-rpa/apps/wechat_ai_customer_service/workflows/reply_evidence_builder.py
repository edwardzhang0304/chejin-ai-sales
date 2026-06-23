"""Evidence packaging for guarded LLM reply synthesis.

This module is intentionally additive. It reuses the existing structured
knowledge and runtime-allowed retrieval output, then packages it for an LLM that can write a
natural WeChat reply. The package is audit-friendly so operators can verify
that product master, formal knowledge and current conversation facts actually participated.
"""

from __future__ import annotations

import re
from typing import Any

from apps.wechat_ai_customer_service.platform_understanding_rules import intent_group
from admin_backend.services.raw_message_store import RawMessageStore
from apps.wechat_ai_customer_service.admin_backend.services.conversation_history import assemble_conversation_history
from knowledge_loader import build_evidence_pack
from knowledge_runtime import KnowledgeRuntime
from evidence_authority import (
    PRODUCT_MASTER_CATEGORY_ID,
    annotate_authority,
    authority_order_payload,
    can_authorize_reply_content,
    dedupe_authoritative_products,
)
from llm_common_sense_layer import build_common_sense_guidance, common_sense_prompt_fragment
from product_name_matcher import collect_matched_aliases, compact_match_text
try:
    from customer_service_brain_contract import strip_nonsemantic_runtime_markers
except Exception:  # pragma: no cover - workflow import fallback for isolated scripts
    strip_nonsemantic_runtime_markers = None  # type: ignore[assignment]


DEFAULT_MAX_HISTORY_MESSAGES = 40
DEFAULT_HISTORY_CHAR_BUDGET = 12000
DEFAULT_MAX_RAG_HITS = 5
DEFAULT_MAX_TEXT_CHARS = 900
AI_EXPERIENCE_REFERENCE_SOURCE_TYPES = {
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
AI_EXPERIENCE_RUNTIME_RISK_TERMS = {
    "最低价",
    "保证",
    "包过",
    "一定能",
    "肯定能",
    "绝对",
    "赔偿",
    "退款",
    "合同",
    "发票",
    "转账",
    "账号",
    "月结",
    "账期",
    "库存",
    "现货",
    "现车",
}
AI_EXPERIENCE_RUNTIME_NOISE_TERMS = {
    "请求添加",
    "添加你为朋友",
    "通过朋友验证",
    "以上是打招呼的内容",
    "系统消息",
    "撤回了一条消息",
    "拍了拍",
    "语音通话",
    "视频通话",
    "文件传输助手",
    "llm_synthesis_reply",
    "rag_context_reply",
}
AI_EXPERIENCE_RUNTIME_STYLE_MARKERS = {
    "你好",
    "您好",
    "在吗",
    "好的",
    "收到",
    "谢谢",
    "辛苦",
    "稍等",
    "我看一下",
    "我确认一下",
}
GENERIC_CATALOG_ALIAS_TERMS = {
    "mpv",
    "suv",
    "商务车",
    "保姆车",
    "新能源",
    "电动车",
    "绿牌",
    "混动",
    "油车",
    "家用",
    "通勤",
    "代步",
    "代步车",
    "练手",
    "练手车",
    "小车",
    "省心",
    "空间",
    "大空间",
    "七座",
    "自动挡",
}

WEAK_DESCRIPTIVE_CATALOG_ALIAS_TERMS = (
    "预算",
    "以内",
    "以下",
    "左右",
    "省油",
    "油耗",
    "省心",
    "家用",
    "通勤",
    "代步",
    "代步车",
    "练手",
    "练手车",
    "小车",
    "大空间",
    "好停车",
    "城市代步",
    "新手",
    "接待",
    "二胎",
    "需求",
)

CARGO_SPACE_NEED_TERMS = (
    "后备厢",
    "后备箱",
    "装载",
    "装东西",
    "放东西",
    "放物料",
    "拉物料",
    "拉货",
    "灯架",
    "展架",
    "背景架",
    "折叠桌",
    "工具箱",
    "梯子",
    "电钻",
    "油漆桶",
    "第二排",
    "放倒",
    "空间大",
    "大空间",
    "不只看轿车",
    "不想只看轿车",
)

CARGO_SPACE_FIT_CATEGORY_TERMS = ("suv", "mpv", "旅行", "两厢", "掀背", "皮卡", "van")
CARGO_SPACE_WEAK_CATEGORY_TERMS = ("轿车", "三厢")

CATALOG_PREFERENCE_GROUPS: tuple[dict[str, tuple[str, ...]], ...] = (
    {
        "label": ("catalog_preference_pure_electric",),
        "query": ("纯电", "电车", "电动车", "新能源车", "绿牌车"),
        "positive": ("纯电", "电动车", "换电", "新能源"),
        "negative": ("混动", "插混", "轻混", "dm-i", "dmi", "油电"),
    },
    {
        "label": ("catalog_preference_hybrid",),
        "query": ("混动", "插混", "油电", "dm-i", "dmi"),
        "positive": ("混动", "插混", "油电", "dm-i", "dmi", "绿牌"),
        "negative": ("纯电", "电动车", "换电"),
    },
    {
        "label": ("catalog_preference_mpv",),
        "query": ("mpv", "商务车", "保姆车", "七座"),
        "positive": ("mpv", "商务", "保姆车", "七座"),
        "negative": (),
    },
    {
        "label": ("catalog_preference_suv",),
        "query": ("suv", "越野", "通过性"),
        "positive": ("suv", "越野"),
        "negative": (),
    },
    {
        "label": ("catalog_preference_sedan",),
        "query": ("轿车", "三厢", "中级车"),
        "positive": ("轿车", "三厢", "中级"),
        "negative": (),
    },
    {
        "label": ("catalog_preference_automatic",),
        "query": ("自动挡", "不要手动", "自动"),
        "positive": ("自动", "cvt", "at", "双离合"),
        "negative": ("手动",),
    },
)

CATALOG_PRICE_SUPERLATIVE_TERMS = (
    "最贵",
    "价格最高",
    "报价最高",
    "标价最高",
    "最高价",
    "最高价格",
    "高端",
    "贵的车",
)

CATALOG_LIST_REQUEST_TERMS = (
    "报价",
    "标价",
    "价格表",
    "报价单",
    "车源",
    "库存",
    "现车",
    "在售",
    "有哪些车",
    "有什么车",
    "发一下价格",
    "发下价格",
    "发一下报价",
    "发下报价",
)

WEAK_CATALOG_CONTEXT_TERMS = (
    "你们这儿",
    "你们这边",
    "你们家",
    "你这",
)

QUOTE_HARD_BOUNDARY_TERMS = (
    "最低",
    "底价",
    "优惠",
    "折扣",
    "少点",
    "便宜点",
    "包过",
    "保证",
    "绝对",
    "定金",
    "订金",
    "事故",
    "水泡",
    "火烧",
    "赔",
    "赔付",
)

CATALOG_RECOMMENDATION_SOFT_HANDOFF_REASONS = {
    "matched_faq_requires_handoff",
    "auto_reply_disabled",
    "missing_authoritative_evidence",
    "no_relevant_business_evidence",
    "shared_risk_control",
}

CATALOG_RECOMMENDATION_INTENT_TAGS = {
    "catalog",
    "product",
    "quote",
    "scene_product",
    "budget",
    "shopping",
}

CATALOG_RECOMMENDATION_QUERY_TERMS = (
    "推荐",
    "挑",
    "选",
    "合适",
    "靠谱",
    "有什么",
    "有哪些",
    "买个",
    "买台",
    "换台",
    "二手车",
    "预算",
    "不贵",
    "便宜",
    "时尚",
    "家用",
    "通勤",
    "代步",
    "接送",
    "买菜",
    "练手",
    "省油",
    "省心",
    "自动挡",
)

CATALOG_RECOMMENDATION_HARD_BOUNDARY_TERMS = (
    "最低",
    "底价",
    "优惠",
    "折扣",
    "少点",
    "便宜点",
    "包过",
    "保证",
    "绝对",
    "定金",
    "订金",
    "赔付",
    "贷款包过",
    "保证通过",
    "保证能批",
    "零首付",
    "不看征信",
    "黑户",
    "征信黑",
    "保证无事故",
    "包无事故",
    "保证不是事故车",
    "包水泡",
    "包火烧",
)

RELATIVE_CONTEXT_PRODUCT_TERMS = (
    "这辆",
    "这台",
    "这两台",
    "这两个",
    "这个",
    "这款",
    "这车",
    "这个车",
    "那辆",
    "那台",
    "哪台",
    "哪个",
    "那款",
    "那车",
    "那一个",
    "刚才",
    "上面",
    "前面",
    "它",
)

CONTEXT_PRODUCT_DETAIL_TERMS = (
    "报价",
    "什么价",
    "多少钱",
    "价格",
    "车况",
    "配置",
    "第二排",
    "后排",
    "座椅",
    "放倒",
    "后备厢",
    "后备箱",
    "尾门",
    "空间",
    "尺寸",
    "装东西",
    "装载",
    "器材",
    "箱子",
    "公里",
    "贷款",
    "置换",
    "保值",
    "再卖",
    "亏得少",
)

GENERIC_PRODUCT_REFERENCE_TERMS = {
    "车",
    "车子",
    "二手车",
    "车型",
    "这辆",
    "这台",
    "这个",
    "这款",
    "这车",
    "这个车",
    "那辆",
    "那台",
    "那款",
    "那车",
    "刚才",
    "上面",
    "前面",
    "它",
    "你们",
    "你这",
    "你家",
}

RELATIVE_CONTEXT_PREFIX_FILLERS = {
    "",
    "什么",
    "多少",
    "大概",
    "大约",
    "具体",
    "现在",
    "目前",
    "这个",
    "那个",
    "这车",
    "那车",
    "车",
}


def build_reply_evidence_pack(
    *,
    config: dict[str, Any],
    target_name: str,
    target_state: dict[str, Any],
    batch: list[dict[str, Any]],
    combined: str,
    decision: Any,
    reply_text: str,
    intent_assist: dict[str, Any],
    rag_reply: dict[str, Any],
    llm_reply: dict[str, Any],
    product_knowledge: dict[str, Any],
    data_capture: dict[str, Any],
    raw_capture: dict[str, Any],
    customer_profile: dict[str, Any] | None = None,
) -> dict[str, Any]:
    settings = config.get("llm_reply_synthesis", {}) or {}
    context = dict(target_state.get("conversation_context", {}) or {})
    try:
        knowledge_pack = build_evidence_pack(combined, context=context)
        knowledge_error = ""
    except Exception as exc:
        knowledge_pack = {}
        knowledge_error = repr(exc)

    compact_knowledge = compact_knowledge_pack(
        combined,
        knowledge_pack,
        max_rag_hits=int(settings.get("max_rag_hits", DEFAULT_MAX_RAG_HITS) or DEFAULT_MAX_RAG_HITS),
        max_rag_text_chars=int(settings.get("max_rag_text_chars", DEFAULT_MAX_TEXT_CHARS) or DEFAULT_MAX_TEXT_CHARS),
        max_catalog_candidates=int(settings.get("max_catalog_candidates", 8) or 8),
        context_override=context,
    )
    compact_knowledge["conversation_context"] = context
    common_sense = common_sense_prompt_fragment(
        build_common_sense_guidance(
            customer_message=combined,
            conversation_context=context,
        )
    )
    relax_soft_synthesis_safety(compact_knowledge, text=combined)
    history = recent_history(
        raw_capture=raw_capture,
        batch=batch,
        max_messages=int(settings.get("max_history_messages", DEFAULT_MAX_HISTORY_MESSAGES) or DEFAULT_MAX_HISTORY_MESSAGES),
        char_budget=int(settings.get("history_char_budget", DEFAULT_HISTORY_CHAR_BUDGET) or DEFAULT_HISTORY_CHAR_BUDGET),
    )

    history_text_pack = assemble_conversation_history(
        target_name=target_name,
        conversation_id=raw_conversation_id(raw_capture),
        current_batch=batch,
        config=config,
    )
    if settings.get("foreground_realtime"):
        history_text_pack = dict(history_text_pack)
        history_limit = max(300, int(settings.get("history_char_budget", DEFAULT_HISTORY_CHAR_BUDGET) or DEFAULT_HISTORY_CHAR_BUDGET))
        history_text_pack["history_text"] = truncate_text(str(history_text_pack.get("history_text") or ""), history_limit)
        history_text_pack["conversation_summary"] = truncate_text(str(history_text_pack.get("summary") or ""), 500)
        history_text_pack["current_batch_text"] = truncate_text(str(history_text_pack.get("current_batch_text") or ""), 600)

    evidence_ids = collect_evidence_ids(compact_knowledge)
    return {
        "schema_version": 1,
        "target": target_name,
        "current_message": truncate_text(combined, 2000),
        "current_batch": [compact_message(item) for item in batch],
        "conversation": {
            "context": context,
            "history": history,
            "history_count": len(history),
            "history_text": history_text_pack.get("history_text", ""),
            "current_batch_text": history_text_pack.get("current_batch_text", ""),
            "conversation_summary": history_text_pack.get("summary", ""),
            "raw_conversation_id": raw_conversation_id(raw_capture),
        },
        "existing_reply": {
            "authority": "non_authoritative_legacy_candidate",
            "usage_policy": (
                "This payload is an advisory legacy draft for audit/risk context only. "
                "It must not authorize facts, override product/formal evidence, or finalize customer-visible replies."
            ),
            "decision": compact_decision(decision),
            "reply_text": truncate_text(reply_text, 1000),
            "rag_reply": compact_mapping(rag_reply, max_text_chars=500),
            "llm_reply": compact_mapping(llm_reply, max_text_chars=500),
        },
        "product_knowledge": compact_mapping(product_knowledge, max_text_chars=900),
        "data_capture": compact_mapping(data_capture, max_text_chars=500),
        "intent_assist": compact_mapping(intent_assist, max_text_chars=900),
        "knowledge": compact_knowledge,
        "authority_order": authority_order_payload(),
        "common_sense": common_sense,
        "knowledge_error": knowledge_error,
        "evidence_ids": evidence_ids,
        "safety": compact_knowledge.get("safety", {}),
        "intent_tags": compact_knowledge.get("intent_tags", []),
        "customer_profile": _compact_profile(customer_profile),
        "ai_experience_pool": compact_knowledge.get("ai_experience_pool", {}),
        "rag": compact_knowledge.get("rag_evidence", {}),
        "audit_summary": {
            "structured_evidence_count": structured_evidence_count(compact_knowledge),
            "runtime_rag_hit_count": len((compact_knowledge.get("rag_evidence", {}) or {}).get("hits", []) or []),
            "rag_hit_count": len((compact_knowledge.get("rag_evidence", {}) or {}).get("hits", []) or []),
            "excluded_ai_experience_pool_hit_count": int((compact_knowledge.get("ai_experience_pool", {}) or {}).get("excluded_hit_count") or 0),
            "ai_experience_pool_reference_hit_count": int((compact_knowledge.get("ai_experience_pool", {}) or {}).get("reference_hit_count") or 0),
            "ai_experience_pool_reference_ids": (
                ((compact_knowledge.get("ai_experience_pool", {}) or {}).get("trace", {}) or {}).get("reference_ids", [])
            ),
            "ai_experience_pool_exclusion_reasons": (
                ((compact_knowledge.get("ai_experience_pool", {}) or {}).get("trace", {}) or {}).get("exclusion_reasons", {})
            ),
            "ai_experience_pool_trace": (
                ((compact_knowledge.get("ai_experience_pool", {}) or {}).get("trace", {}) or {}).get("items", [])
            ),
            "rag_chunk_ids": [
                str(item.get("chunk_id") or "")
                for item in (compact_knowledge.get("rag_evidence", {}) or {}).get("hits", []) or []
                if item.get("chunk_id")
            ],
            "evidence_ids": evidence_ids,
        },
    }


def recent_history(
    *,
    raw_capture: dict[str, Any],
    batch: list[dict[str, Any]],
    max_messages: int,
    char_budget: int,
) -> list[dict[str, Any]]:
    conversation_id = raw_conversation_id(raw_capture)
    messages: list[dict[str, Any]] = []
    if conversation_id:
        try:
            messages = RawMessageStore().list_messages(conversation_id=conversation_id, limit=max_messages)
        except Exception:
            messages = []
    if not messages:
        messages = list(batch)
    compacted = [compact_message(item) for item in reversed(messages[:max_messages])]
    return trim_history(compacted, char_budget=max(500, char_budget))


def raw_conversation_id(raw_capture: dict[str, Any]) -> str:
    conversation = raw_capture.get("conversation", {}) or {}
    return str(conversation.get("conversation_id") or raw_capture.get("conversation_id") or "")


def compact_message(message: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(message.get("id") or message.get("message_id") or message.get("raw_message_id") or ""),
        "sender": str(message.get("sender") or ""),
        "time": str(message.get("time") or message.get("message_time") or message.get("observed_at") or ""),
        "content": truncate_text(str(message.get("content") or ""), 600),
    }


def _compact_profile(profile: dict[str, Any] | None) -> dict[str, Any]:
    if not profile:
        return {}
    basic = profile.get("basic_info") if isinstance(profile.get("basic_info"), dict) else {}
    tags = profile.get("tags") if isinstance(profile.get("tags"), dict) else {}
    return {
        "display_name": str(profile.get("display_name") or ""),
        "status": str(profile.get("status") or "active"),
        "gender": str(basic.get("gender") or ""),
        "gender_confidence": float(basic.get("gender_confidence") or 0.0),
        "region": str(basic.get("region") or ""),
        "total_messages": int(basic.get("total_messages", 0) or 0),
        "total_replies": int(basic.get("total_replies", 0) or 0),
        "conversation_summary": str(profile.get("conversation_summary") or ""),
        "tags": {k: str(v) for k, v in tags.items()},
    }


def trim_history(history: list[dict[str, Any]], *, char_budget: int) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    used = 0
    for item in reversed(history):
        content = str(item.get("content") or "")
        cost = len(content)
        if result and used + cost > char_budget:
            break
        result.append(item)
        used += cost
    return list(reversed(result))


def compact_knowledge_pack(
    text: str,
    pack: dict[str, Any],
    *,
    max_rag_hits: int,
    max_rag_text_chars: int,
    max_catalog_candidates: int,
    context_override: dict[str, Any] | None = None,
) -> dict[str, Any]:
    evidence = pack.get("evidence", {}) or {}
    rag_evidence = pack.get("rag_evidence", {}) or {}
    if isinstance(context_override, dict):
        context = context_override
    else:
        context = pack.get("conversation_context") if isinstance(pack.get("conversation_context"), dict) else {}
    catalog_candidates = catalog_product_candidates(text, limit=max_catalog_candidates, context=context)
    item_limit = max(1, max_catalog_candidates)
    products = [
        annotate_authority(compact_mapping(item, max_text_chars=420), category_id=PRODUCT_MASTER_CATEGORY_ID)
        for item in (evidence.get("products", []) or [])[:item_limit]
    ]
    catalog_candidates = [
        annotate_authority(item, category_id=PRODUCT_MASTER_CATEGORY_ID)
        for item in catalog_candidates
        if isinstance(item, dict)
    ]
    context_recent_candidates = [item for item in catalog_candidates if is_context_recent_product_candidate(item)]
    if context_recent_candidates:
        other_catalog_candidates = [item for item in catalog_candidates if not is_context_recent_product_candidate(item)]
        product_master = dedupe_authoritative_products(context_recent_candidates, products, other_catalog_candidates)[:item_limit]
    elif any(is_context_need_catalog_candidate(item) for item in catalog_candidates):
        products_for_merge = [item for item in products if not is_weak_catalog_only_product_candidate(item)]
        product_master = dedupe_authoritative_products(catalog_candidates, products_for_merge)[:item_limit]
    elif any(is_explicit_catalog_candidate(item) for item in catalog_candidates):
        products_for_merge = [item for item in products if not is_context_only_product_candidate(item)]
        product_master = dedupe_authoritative_products(catalog_candidates, products_for_merge)[:item_limit]
    else:
        product_master = dedupe_authoritative_products(products, catalog_candidates)[:item_limit]
    faq = [
        annotate_authority(compact_mapping(item, max_text_chars=360), category_id="faq")
        for item in (evidence.get("faq", []) or [])[:item_limit]
    ]
    product_scoped = [
        annotate_authority(compact_mapping(item, max_text_chars=360), category_id=str(item.get("category_id") or "product_faq"))
        for item in (evidence.get("product_scoped", []) or [])[:item_limit]
        if isinstance(item, dict)
    ]
    policies = compact_mapping(evidence.get("policies", {}) or {}, max_text_chars=700)
    rag_evidence = compact_rag_evidence(rag_evidence, max_hits=max_rag_hits, max_text_chars=max_rag_text_chars)
    return {
        "authority_order": authority_order_payload(),
        "intent_tags": list(pack.get("intent_tags", []) or []),
        "selected_items": [
            compact_mapping(item, max_text_chars=400)
            for item in (pack.get("selected_items", []) or [])[:item_limit]
            if isinstance(item, dict)
        ],
        "evidence": {
            "products": product_master,
            "faq": faq,
            "policies": policies,
            "product_scoped": product_scoped,
            "catalog_candidates": catalog_candidates,
            "style_examples": [
                compact_mapping(item, max_text_chars=260)
                for item in (evidence.get("style_examples", []) or [])[: min(item_limit, 2)]
            ],
        },
        "product_master": {
            "authority_level": "product_master",
            "can_authorize_product_facts": True,
            "items": product_master,
        },
        "formal_knowledge": {
            "authority_level": "formal_knowledge",
            "can_authorize_product_facts": False,
            "faq": faq,
            "policies": policies,
            "product_scoped": product_scoped,
        },
        "ai_experience_pool": {
            "authority_level": "ai_experience_pool",
            "can_authorize_product_facts": False,
            "can_authorize_reply_content": False,
            "usage": "auxiliary_experience_and_style_only",
            "runtime_usage_policy": (
                "AI experience pool hits may guide scenario understanding, follow-up strategy, and tone only; "
                "they must not authorize product facts, policies, prices, stock, availability, or commitments."
            ),
            "excluded_hit_count": int(rag_evidence.get("excluded_hit_count") or 0),
            "reference_hit_count": int(rag_evidence.get("reference_hit_count") or 0),
            "source": {
                "enabled": rag_evidence.get("enabled", True),
                "reason": rag_evidence.get("reason") or "ai_experience_pool_not_runtime_content_basis",
                "hits": rag_evidence.get("ai_experience_hits", []),
            },
            "trace": {
                "reference_ids": rag_evidence.get("ai_experience_reference_ids", []),
                "exclusion_reasons": rag_evidence.get("ai_experience_exclusion_reasons", {}),
                "items": rag_evidence.get("ai_experience_trace", []),
            },
        },
        "rag_evidence": rag_evidence,
        "safety": compact_mapping(pack.get("safety", {}) or {}, max_text_chars=700),
        "matched_categories": list(pack.get("matched_categories", []) or []),
    }


def compact_rag_evidence(rag_evidence: dict[str, Any], *, max_hits: int, max_text_chars: int = DEFAULT_MAX_TEXT_CHARS) -> dict[str, Any]:
    hits = []
    ai_experience_hits = []
    ai_experience_trace = []
    ai_experience_exclusion_reasons: dict[str, int] = {}
    excluded_hit_count = 0
    for item in rag_evidence.get("hits", []) or []:
        if not isinstance(item, dict):
            continue
        if not can_authorize_reply_content(item, category_id=str(item.get("category") or ""), source_type=str(item.get("source_type") or "")):
            ai_experience_hit = compact_ai_experience_runtime_hit(item, max_text_chars=max_text_chars)
            decision = classify_ai_experience_runtime_usage(item)
            reason = str(decision.get("reason") or "unknown")
            ai_experience_exclusion_reasons[reason] = ai_experience_exclusion_reasons.get(reason, 0) + 1
            ai_experience_trace.append(compact_ai_experience_runtime_trace(item, decision=decision))
            if ai_experience_hit:
                ai_experience_hits.append(ai_experience_hit)
            excluded_hit_count += 1
            continue
        hits.append(
            {
                "chunk_id": item.get("chunk_id"),
                "source_id": item.get("source_id"),
                "score": item.get("score"),
                "category": item.get("category"),
                "source_type": item.get("source_type"),
                "product_id": item.get("product_id"),
                "retrieval_mode": item.get("retrieval_mode"),
                "risk_terms": item.get("risk_terms", []),
                "text": truncate_text(str(item.get("text") or ""), max(120, max_text_chars)),
            }
        )
        if len(hits) >= max(1, max_hits):
            break
    return {
        "enabled": rag_evidence.get("enabled", True),
        "ok": rag_evidence.get("ok"),
        "skipped": rag_evidence.get("skipped"),
        "reason": rag_evidence.get("reason"),
        "confidence": rag_evidence.get("confidence", 0.0),
        "rag_can_authorize": bool(rag_evidence.get("rag_can_authorize", False)),
        "structured_priority": rag_evidence.get("structured_priority", True),
        "excluded_hit_count": excluded_hit_count,
        "reference_hit_count": len(ai_experience_hits),
        "ai_experience_trace": ai_experience_trace[: max(1, max_hits * 2)],
        "ai_experience_exclusion_reasons": ai_experience_exclusion_reasons,
        "ai_experience_reference_ids": [
            str(item.get("chunk_id") or item.get("source_id") or "")
            for item in ai_experience_hits
            if str(item.get("chunk_id") or item.get("source_id") or "")
        ][: max(1, max_hits)],
        "ai_experience_hits": ai_experience_hits[: max(1, max_hits)],
        "hits": hits,
    }


def compact_ai_experience_runtime_hit(item: dict[str, Any], *, max_text_chars: int) -> dict[str, Any]:
    decision = classify_ai_experience_runtime_usage(item)
    if decision["runtime_usage"] == "drop":
        return {}
    text = truncate_text(sanitize_ai_experience_runtime_text(str(item.get("text") or "")), max(120, min(max_text_chars, 360)))
    return {
        "chunk_id": item.get("chunk_id"),
        "source_id": item.get("source_id"),
        "score": item.get("score"),
        "category": item.get("category"),
        "source_type": item.get("source_type"),
        "retrieval_mode": item.get("retrieval_mode"),
        "runtime_usage": decision["runtime_usage"],
        "reason": decision["reason"],
        "authority_level": "ai_experience_pool",
        "can_authorize_reply_content": False,
        "usage_policy": "experience/style reference only; never fact or commitment authority",
        "text": text,
    }


def compact_ai_experience_runtime_trace(item: dict[str, Any], *, decision: dict[str, str]) -> dict[str, Any]:
    return {
        "chunk_id": item.get("chunk_id"),
        "source_id": item.get("source_id"),
        "score": item.get("score"),
        "category": item.get("category"),
        "source_type": item.get("source_type"),
        "runtime_usage": decision.get("runtime_usage"),
        "reason": decision.get("reason"),
        "reference_only": True,
        "can_authorize_reply_content": False,
    }


def classify_ai_experience_runtime_usage(item: dict[str, Any]) -> dict[str, str]:
    source_type = str(item.get("source_type") or "").strip().lower()
    category = str(item.get("category") or "").strip().lower()
    text = normalize_ai_experience_text(str(item.get("text") or ""))
    if source_type not in AI_EXPERIENCE_REFERENCE_SOURCE_TYPES and category not in {"chats", "style_examples", "rag_experience"}:
        return {"runtime_usage": "drop", "reason": "not_ai_experience_pool_source"}
    if not text:
        return {"runtime_usage": "drop", "reason": "empty_text"}
    if ai_experience_text_is_noise(text):
        return {"runtime_usage": "drop", "reason": "runtime_noise_or_metadata"}
    if ai_experience_text_has_risky_commitment(text):
        return {"runtime_usage": "drop", "reason": "risky_fact_or_commitment_like_text"}
    if ai_experience_text_is_style_only(text):
        return {"runtime_usage": "style_only", "reason": "style_or_short_social_reference"}
    if ai_experience_text_is_complete_reference(text):
        return {"runtime_usage": "reference_experience", "reason": "complete_non_authoritative_experience"}
    return {"runtime_usage": "style_only", "reason": "incomplete_but_style_useful"}


def normalize_ai_experience_text(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def ai_experience_text_is_noise(text: str) -> bool:
    clean = normalize_ai_experience_text(text)
    if len(clean) < 2:
        return True
    if any(term in clean for term in AI_EXPERIENCE_RUNTIME_NOISE_TERMS):
        return True
    if re.fullmatch(r"[\[\]【】()（）握手微笑OKok,.，。!！?？\s]+", clean):
        return True
    if re.match(r"^(?:客户|客服|user|assistant|system|agent|self)\s*[:：]\s*$", clean, re.I):
        return True
    return False


def ai_experience_text_has_risky_commitment(text: str) -> bool:
    clean = normalize_ai_experience_text(text)
    return any(term in clean for term in AI_EXPERIENCE_RUNTIME_RISK_TERMS)


def sanitize_ai_experience_runtime_text(text: str) -> str:
    clean = normalize_ai_experience_text(text)
    clean = re.sub(r"(?<!\d)1[3-9]\d{9}(?!\d)", "{手机号}", clean)
    clean = re.sub(r"\d+(?:\.\d+)?\s*(?:万|万元|w|W|元|块|人民币)", "{金额}", clean)
    clean = re.sub(r"\d+(?:\.\d+)?\s*(?:公里|km|KM|天|小时|分钟)", "{数量}", clean)
    clean = re.sub(r"\d+\s*(?:个|件|台|套|箱|辆|条|次|轮)", "{数量}", clean)
    return clean


def ai_experience_text_is_style_only(text: str) -> bool:
    clean = normalize_ai_experience_text(text)
    if len(clean) <= 12 and any(term in clean for term in AI_EXPERIENCE_RUNTIME_STYLE_MARKERS):
        return True
    if not re.search(r"[？?。.!！；;，,]", clean) and len(clean) <= 18:
        return True
    return False


def ai_experience_text_is_complete_reference(text: str) -> bool:
    clean = normalize_ai_experience_text(text)
    if len(clean) < 18:
        return False
    if re.search(r"(客户|用户|买家|对方|咨询|问|想|需要|关注|担心|比较|推荐|建议|可以|适合|优先)", clean):
        return True
    return bool(re.search(r"[？?].{4,}[。.!！]", clean))


def collect_evidence_ids(pack: dict[str, Any]) -> list[str]:
    ids: list[str] = []
    evidence = pack.get("evidence", {}) or {}
    for item in evidence.get("products", []) or []:
        append_id(ids, "product", item.get("id"))
    for item in evidence.get("faq", []) or []:
        append_id(ids, "faq", item.get("intent") or item.get("id"))
    for key in (evidence.get("policies", {}) or {}).keys():
        append_id(ids, "policy", key)
    for item in evidence.get("product_scoped", []) or []:
        append_id(ids, "product_scoped", item.get("id"))
    for item in evidence.get("catalog_candidates", []) or []:
        append_id(ids, "catalog_product", item.get("id"))
    for item in (pack.get("rag_evidence", {}) or {}).get("hits", []) or []:
        if can_authorize_reply_content(item, category_id=str(item.get("category") or ""), source_type=str(item.get("source_type") or "")):
            append_id(ids, "rag", item.get("chunk_id") or item.get("source_id"))
    return ids


def append_id(items: list[str], prefix: str, value: Any) -> None:
    text = str(value or "").strip()
    if text:
        marker = f"{prefix}:{text}"
        if marker not in items:
            items.append(marker)


def structured_evidence_count(pack: dict[str, Any]) -> int:
    evidence = pack.get("evidence", {}) or {}
    return (
        len(evidence.get("products", []) or [])
        + len(evidence.get("faq", []) or [])
        + len(evidence.get("policies", {}) or {})
        + len(evidence.get("product_scoped", []) or [])
        + len(evidence.get("catalog_candidates", []) or [])
    )


def relax_soft_synthesis_safety(pack: dict[str, Any], *, text: str = "") -> None:
    """Let the additive synthesis layer use catalog and allowed retrieval evidence for soft scenes.

    The base safety layer may mark a broad natural-language question as
    `no_relevant_business_evidence` before catalog candidates are attached. For
    soft selection questions this module can now provide formal catalog
    candidates plus runtime-allowed retrieval evidence. Finance is only relaxed
    for broad process explanations with formal boundary evidence; exact numbers
    and approval promises remain hard handoff.
    """
    intent_tags = {str(item) for item in pack.get("intent_tags", []) or [] if str(item)}
    safety = pack.get("safety", {}) or {}
    if not isinstance(safety, dict):
        return
    if not safety.get("must_handoff"):
        mark_catalog_recommendation_advisory_when_answerable(pack, text=text, safety=safety)
        return
    reasons = {str(item) for item in safety.get("reasons", []) or [] if str(item)}
    if relax_finance_process_safety(pack, text=text, reasons=reasons):
        mark_finance_process_evidence_auto_reply_allowed(pack)
        safety["must_handoff"] = False
        safety["allowed_auto_reply"] = True
        safety["reasons"] = []
        safety["finance_process_soft_evidence_override"] = True
        return
    if relax_appointment_schedule_safety(pack, text=text, reasons=reasons):
        safety["must_handoff"] = False
        safety["allowed_auto_reply"] = True
        safety["reasons"] = []
        safety["appointment_schedule_soft_boundary_override"] = True
        return
    # Broad catalog recommendations should keep soft risk matches as Brain
    # advisories, while concrete product quote relaxations remain separate.
    if relax_catalog_recommendation_soft_safety(pack, text=text, reasons=reasons):
        mark_catalog_recommendation_evidence_advisory(pack)
        safety["must_handoff"] = False
        safety["allowed_auto_reply"] = True
        safety["reasons"] = []
        safety["catalog_recommendation_soft_advisory_override"] = True
        return
    # Product-master grounded shopping questions can carry broad authority tags
    # such as "stock". The bounded quote relaxer still keeps true hard-boundary
    # requests as handoff.
    if relax_product_master_grounded_quote_safety(pack, text=text, reasons=reasons):
        safety["must_handoff"] = False
        safety["allowed_auto_reply"] = True
        safety["reasons"] = []
        safety["llm_synthesis_product_master_quote_override"] = True
        return
    hard_authority_tags = intent_group("rag_authority_block") - {"quote"}
    if intent_tags & hard_authority_tags:
        return
    if not reasons or not reasons <= {"no_relevant_business_evidence"}:
        return
    if structured_evidence_count(pack) <= 0 and not ((pack.get("rag_evidence", {}) or {}).get("hits")):
        return
    safety["must_handoff"] = False
    safety["allowed_auto_reply"] = True
    safety["reasons"] = []
    safety["llm_synthesis_soft_evidence_override"] = True


def mark_catalog_recommendation_advisory_when_answerable(
    pack: dict[str, Any],
    *,
    text: str,
    safety: dict[str, Any],
) -> None:
    if safety.get("allowed_auto_reply") is not True:
        return
    if is_catalog_recommendation_hard_boundary_question(text):
        return
    if not is_catalog_recommendation_question(pack, text=text):
        return
    evidence = pack.get("evidence", {}) if isinstance(pack.get("evidence"), dict) else {}
    product_items = [item for item in evidence.get("products", []) or [] if isinstance(item, dict)]
    catalog_items = [item for item in evidence.get("catalog_candidates", []) or [] if isinstance(item, dict)]
    if not any(product_has_price_or_stock(item) for item in [*product_items, *catalog_items]):
        return
    mark_catalog_recommendation_evidence_advisory(pack)
    if any(item.get("soft_advisory_only") for bucket in catalog_recommendation_faq_buckets(pack) for item in bucket if isinstance(item, dict)):
        safety["catalog_recommendation_soft_advisory_override"] = True
    else:
        safety["llm_synthesis_product_master_quote_override"] = True


def relax_finance_process_safety(pack: dict[str, Any], *, text: str, reasons: set[str]) -> bool:
    if not reasons or not reasons <= {"matched_faq_requires_handoff", "finance_details_need_human"}:
        return False
    intent_tags = {str(item) for item in pack.get("intent_tags", []) or [] if str(item)}
    if not (intent_tags & {"finance", "payment"} or contains_finance_term(text)):
        return False
    if not formal_finance_boundary_present(pack):
        return False
    if is_specific_finance_commitment_question(text):
        return False
    return is_finance_process_question(text)


def formal_finance_boundary_present(pack: dict[str, Any]) -> bool:
    evidence = pack.get("evidence") if isinstance(pack.get("evidence"), dict) else {}
    faq = evidence.get("faq") if isinstance(evidence.get("faq"), list) else []
    policies = evidence.get("policies") if isinstance(evidence.get("policies"), dict) else {}
    text_parts: list[str] = []
    for item in faq:
        if isinstance(item, dict):
            text_parts.append(str(item.get("answer") or ""))
            text_parts.extend(str(keyword) for keyword in item.get("matched_keywords", []) or [])
    text_parts.extend(str(value) for value in policies.values())
    combined = "".join(text_parts)
    return contains_finance_term(combined) and any(term in combined for term in ("资方", "审批", "审核", "不能承诺", "一对一评估"))


def mark_finance_process_evidence_auto_reply_allowed(pack: dict[str, Any]) -> None:
    for bucket in finance_faq_buckets(pack):
        for item in bucket:
            if not isinstance(item, dict):
                continue
            item_text = "".join(
                [
                    str(item.get("answer") or ""),
                    str(item.get("reason") or ""),
                    str(item.get("policy_type") or ""),
                    "".join(str(keyword) for keyword in item.get("matched_keywords", []) or []),
                ]
            )
            if not contains_finance_term(item_text):
                continue
            if item.get("needs_handoff") is True:
                item["original_needs_handoff"] = True
            if item.get("auto_reply_allowed") is False:
                item["original_auto_reply_allowed"] = False
            item["needs_handoff"] = False
            item["auto_reply_allowed"] = True
            item["finance_process_auto_reply_allowed"] = True


def finance_faq_buckets(pack: dict[str, Any]) -> list[list[dict[str, Any]]]:
    buckets: list[list[dict[str, Any]]] = []
    evidence = pack.get("evidence") if isinstance(pack.get("evidence"), dict) else {}
    faq = evidence.get("faq") if isinstance(evidence.get("faq"), list) else []
    buckets.append(faq)
    formal = pack.get("formal_knowledge") if isinstance(pack.get("formal_knowledge"), dict) else {}
    formal_faq = formal.get("faq") if isinstance(formal.get("faq"), list) else []
    buckets.append(formal_faq)
    return buckets


def contains_finance_term(text: str) -> bool:
    clean = str(text or "")
    return any(term in clean for term in ("贷款", "分期", "金融", "按揭", "首付", "月供", "利率", "资方"))


def is_finance_process_question(text: str) -> bool:
    clean = re.sub(r"\s+", "", str(text or ""))
    if not contains_finance_term(clean):
        return False
    process_terms = (
        "流程",
        "怎么走",
        "怎么做",
        "怎么办",
        "大概",
        "一般",
        "可以吗",
        "能不能",
        "支持",
        "谁给",
        "谁来",
        "谁算",
        "方案",
        "需要什么",
        "要什么资料",
    )
    return any(term in clean for term in process_terms)


def relax_appointment_schedule_safety(pack: dict[str, Any], *, text: str, reasons: set[str]) -> bool:
    if reasons and not reasons <= {"matched_faq_requires_handoff", "missing_authoritative_evidence", "no_relevant_business_evidence"}:
        return False
    intent_tags = {str(item) for item in pack.get("intent_tags", []) or [] if str(item)}
    if not (intent_tags & {"appointment", "visit", "test_drive"} or is_appointment_schedule_question(text)):
        return False
    if is_direct_appointment_commitment_request(text):
        return True
    return is_appointment_schedule_question(text)


def is_appointment_schedule_question(text: str) -> bool:
    clean = re.sub(r"\s+", "", str(text or ""))
    if not clean:
        return False
    appointment_terms = ("到店", "看车", "试驾", "预约", "安排", "过去", "来店", "来看看", "周六", "周日", "周末", "上午", "下午", "几点")
    question_terms = ("能", "可以", "行吗", "方便", "安排", "预约", "过去", "去看", "带", "到店")
    return any(term in clean for term in appointment_terms) and any(term in clean for term in question_terms)


def is_direct_appointment_commitment_request(text: str) -> bool:
    clean = re.sub(r"\s+", "", str(text or ""))
    commitment_terms = (
        "直接约好",
        "帮我约好",
        "给我约好",
        "留车",
        "预留",
        "保证有车",
        "一定能看",
        "去了就能看",
        "直接过去",
    )
    return any(term in clean for term in commitment_terms)


def is_specific_finance_commitment_question(text: str) -> bool:
    clean = re.sub(r"\s+", "", str(text or ""))
    if not clean:
        return False
    hard_terms = (
        "包过",
        "保证过",
        "保证能过",
        "保证能批",
        "肯定能批",
        "一定能批",
        "一定通过",
        "保证通过",
        "最低价",
        "最低能",
        "底价",
        "优惠",
        "折扣",
        "便宜点",
        "少点",
    )
    if any(term in clean for term in hard_terms):
        return True
    hard_patterns = (
        r"(月供|利率|首付).{0,8}(多少|几|最低|具体|准确|算一下|给我算)",
        r"(多少|几成|几万).{0,8}(首付|月供|利率|贷款|分期)",
        r"(包过|保证|肯定|一定).{0,10}(贷款|审批|通过|征信|能批)",
        r"(贷款|审批|通过|征信|能批).{0,10}(包过|保证|肯定|一定)",
        r"(零首付|不看征信|黑户|征信黑|征信花|逾期|查询多)",
        r"(最低|固定).{0,8}(利率|月供|首付)",
    )
    return any(re.search(pattern, clean, re.IGNORECASE) for pattern in hard_patterns)


def relax_product_master_grounded_quote_safety(pack: dict[str, Any], *, text: str, reasons: set[str]) -> bool:
    """Do not let broad shared price-risk FAQs block product-master-grounded quotes.

    Product master remains the only authority for product price/stock. Formal
    risk rules still win for true hard boundaries such as discounts, guarantees,
    deposits, financing approval, or condition commitments.
    """
    if not reasons or not reasons <= {"matched_faq_requires_handoff", "shared_risk_control", "missing_authoritative_evidence"}:
        return False
    intent_tags = {str(item) for item in pack.get("intent_tags", []) or [] if str(item)}
    if not ({"quote", "product"} & intent_tags):
        return False
    compact_text = compact_match_text(text)
    if any(compact_match_text(term) in compact_text for term in QUOTE_HARD_BOUNDARY_TERMS):
        return False
    evidence = pack.get("evidence", {}) if isinstance(pack.get("evidence"), dict) else {}
    product_items = [item for item in evidence.get("products", []) or [] if isinstance(item, dict)]
    catalog_items = [item for item in evidence.get("catalog_candidates", []) or [] if isinstance(item, dict)]
    if not product_items and not catalog_items:
        return False
    return any(product_has_price_or_stock(item) for item in [*product_items, *catalog_items])


def relax_catalog_recommendation_soft_safety(pack: dict[str, Any], *, text: str, reasons: set[str]) -> bool:
    """Downgrade broad recommendation risk matches into Brain advisories.

    Shared risk FAQs often contain intentionally broad keywords like "价格".
    Those should warn the Brain not to invent discounts or commitments, but they
    should not force handoff when product master/catalog evidence can answer a
    normal shopping recommendation.
    """
    if not reasons or not reasons <= CATALOG_RECOMMENDATION_SOFT_HANDOFF_REASONS:
        return False
    if is_catalog_recommendation_hard_boundary_question(text):
        return False
    intent_tags = {str(item) for item in pack.get("intent_tags", []) or [] if str(item)}
    evidence = pack.get("evidence", {}) if isinstance(pack.get("evidence"), dict) else {}
    product_items = [item for item in evidence.get("products", []) or [] if isinstance(item, dict)]
    catalog_items = [item for item in evidence.get("catalog_candidates", []) or [] if isinstance(item, dict)]
    candidate_items = [*product_items, *catalog_items]
    if not any(product_has_price_or_stock(item) for item in candidate_items):
        return False
    return is_catalog_recommendation_question(pack, text=text)


def is_catalog_recommendation_question(pack: dict[str, Any], *, text: str) -> bool:
    intent_tags = {str(item) for item in pack.get("intent_tags", []) or [] if str(item)}
    if intent_tags & CATALOG_RECOMMENDATION_INTENT_TAGS:
        return True
    compact_text = compact_match_text(text)
    return any(compact_match_text(term) in compact_text for term in CATALOG_RECOMMENDATION_QUERY_TERMS)


def is_catalog_recommendation_hard_boundary_question(text: str) -> bool:
    compact_text = compact_match_text(text)
    if not compact_text:
        return False
    if any(compact_match_text(term) in compact_text for term in CATALOG_RECOMMENDATION_HARD_BOUNDARY_TERMS):
        return True
    return is_specific_finance_commitment_question(text)


def mark_catalog_recommendation_evidence_advisory(pack: dict[str, Any]) -> None:
    for bucket in catalog_recommendation_faq_buckets(pack):
        for item in bucket:
            if not isinstance(item, dict):
                continue
            if item.get("needs_handoff") is True:
                item["original_needs_handoff"] = True
            if item.get("auto_reply_allowed") is False:
                item["original_auto_reply_allowed"] = False
            item["needs_handoff"] = False
            item["auto_reply_allowed"] = True
            item["soft_advisory_only"] = True
            item["advisory_reason"] = "catalog_recommendation_product_master_available"


def catalog_recommendation_faq_buckets(pack: dict[str, Any]) -> list[list[dict[str, Any]]]:
    buckets: list[list[dict[str, Any]]] = []
    evidence = pack.get("evidence") if isinstance(pack.get("evidence"), dict) else {}
    faq = evidence.get("faq") if isinstance(evidence.get("faq"), list) else []
    buckets.append(faq)
    formal = pack.get("formal_knowledge") if isinstance(pack.get("formal_knowledge"), dict) else {}
    formal_faq = formal.get("faq") if isinstance(formal.get("faq"), list) else []
    buckets.append(formal_faq)
    return buckets


def product_has_price_or_stock(item: dict[str, Any]) -> bool:
    if item.get("price") not in (None, ""):
        return True
    if item.get("stock") not in (None, ""):
        return True
    data = item.get("data") if isinstance(item.get("data"), dict) else {}
    return data.get("price") not in (None, "") or data.get("inventory") not in (None, "")


def catalog_product_candidates(text: str, *, limit: int, context: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    try:
        runtime = KnowledgeRuntime()
        items = runtime.list_items("products")
    except Exception:
        return []
    context = context if isinstance(context, dict) else {}
    semantic_text = semantic_text_for_catalog_matching(text)
    context_products = context_product_candidates(runtime, context, semantic_text, items=items)
    context_product = context_products[0] if context_products else None
    scored = []
    normalized = semantic_text.lower()
    catalog_request_mode = detect_catalog_request_mode(normalized)
    for item in items:
        if not isinstance(item, dict):
            continue
        data = item.get("data", {}) or {}
        if str(item.get("status") or "active") not in {"active", "approved", "published"}:
            continue
        matched_aliases = collect_catalog_product_aliases(data, normalized)
        searchable = product_searchable_text(data)
        score = catalog_alias_match_score(matched_aliases)
        preference_score, preference_labels = catalog_preference_match_score(data, normalized)
        if preference_score:
            score += preference_score
        for token in tokenize_for_catalog(normalized):
            if token and token in searchable:
                score += 2 if len(token) >= 2 else 1
        if score <= 0:
            score = fallback_catalog_score(data, normalized)
        payload = catalog_product_payload(item)
        if matched_aliases:
            payload["matched_aliases"] = matched_aliases[:8]
            payload["match_reason"] = "product_name_alias_or_semantic_match"
        if preference_score > 0 and not matched_aliases:
            payload["matched_aliases"] = list(dict.fromkeys(preference_labels))[:8]
            payload["match_reason"] = "catalog_preference_match"
        scored.append((score, max_catalog_alias_length(matched_aliases), payload))
    scored.sort(key=lambda pair: (pair[0], pair[1]), reverse=True)
    ranked = [payload for score, _alias_len, payload in scored[: max(0, limit)] if score > 0]
    if catalog_request_mode == "price_desc":
        ranked = catalog_request_candidates(items, limit=limit, mode=catalog_request_mode, preference_text=normalized)
    elif catalog_request_mode == "price_list" and not any(is_explicit_catalog_candidate(item) for item in ranked):
        ranked = catalog_request_candidates(items, limit=limit, mode=catalog_request_mode, preference_text=normalized)
    budget_upper = extract_catalog_budget_upper(normalized)
    budget_target = extract_catalog_budget_target(normalized)
    context_need_active = should_apply_context_need_candidates(normalized, ranked, context)
    if budget_upper is None and context_need_active:
        context_need_text = str(context.get("last_customer_need_text") or "").lower()
        budget_upper = extract_catalog_budget_upper(context_need_text)
        budget_target = extract_catalog_budget_target(context_need_text)
    if budget_upper is not None:
        preference_text = " ".join(
            item
            for item in (
                normalized,
                str(context.get("last_customer_need_text") or "").lower(),
            )
            if item
        )
        budget_fit_candidates = budget_alternative_catalog_candidates(
            items,
            limit=limit,
            budget_upper=budget_upper,
            budget_target=budget_target,
            target=ranked[0] if ranked and is_explicit_catalog_candidate(ranked[0]) else None,
            preference_text=preference_text,
        )
        if should_attach_budget_alternatives(normalized, ranked):
            ranked = merge_catalog_ranked_candidates(ranked, budget_fit_candidates, limit=limit)
        elif should_attach_budget_fit_candidates(normalized, context_need_active):
            ranked = merge_catalog_priority_candidates(
                budget_fit_candidates,
                ranked,
                limit=limit,
            )
    if should_apply_context_need_candidates(normalized, ranked, context):
        ranked = merge_catalog_ranked_candidates(
            context_need_catalog_candidates(
                items,
                context_text=str(context.get("last_customer_need_text") or ""),
                limit=limit,
            ),
            ranked,
            limit=limit,
        )
    if context_products:
        context_ids = {str(item.get("id") or "") for item in context_products if str(item.get("id") or "")}
        ranked = [*context_products, *[item for item in ranked if str(item.get("id") or "") not in context_ids]]
        if should_focus_context_product(semantic_text):
            ranked = ranked[: max(1, min(limit, len(context_products)))]
    return ranked[: max(0, limit)]


def semantic_text_for_catalog_matching(text: str) -> str:
    raw = str(text or "")
    if callable(strip_nonsemantic_runtime_markers):
        return str(strip_nonsemantic_runtime_markers(raw) or "").strip()
    return raw.strip()


def should_attach_budget_alternatives(normalized_text: str, ranked: list[dict[str, Any]]) -> bool:
    if not ranked or not any(is_explicit_catalog_candidate(item) for item in ranked):
        return False
    compact = compact_match_text(normalized_text)
    return any(compact_match_text(term) in compact for term in ("预算", "替代", "备选", "够不到", "超预算"))


def should_attach_budget_fit_candidates(normalized_text: str, context_need_active: bool) -> bool:
    compact = compact_match_text(normalized_text)
    if not compact:
        return False
    recommendation_terms = (
        "推荐",
        "挑",
        "选",
        "合适",
        "靠谱",
        "省油",
        "省心",
        "别太费油",
        "帮我筛",
        "给我筛",
        "直接给我",
        "先看",
        "方向",
        "两三个",
        "几个方向",
        "后备厢",
        "后备箱",
        "物料",
        "装载",
        "不只看轿车",
        "不想只看轿车",
    )
    return context_need_active or any(compact_match_text(term) in compact for term in recommendation_terms)


def should_apply_context_need_candidates(
    normalized_text: str,
    ranked: list[dict[str, Any]],
    context: dict[str, Any],
) -> bool:
    context_text = str(context.get("last_customer_need_text") or "").strip()
    if not context_text or any(is_explicit_catalog_candidate(item) for item in ranked):
        return False
    compact = compact_match_text(normalized_text)
    if not compact:
        return False
    followup_terms = (
        "推荐",
        "合适",
        "哪台",
        "哪个",
        "你觉得",
        "直接",
        "预算",
        "不说太死",
        "先看",
        "就这",
        "这个",
        "那台",
        "它",
    )
    return len(compact) <= 28 or any(compact_match_text(term) in compact for term in followup_terms)


def context_need_catalog_candidates(
    items: list[dict[str, Any]],
    *,
    context_text: str,
    limit: int,
) -> list[dict[str, Any]]:
    context_text = str(context_text or "").strip()
    if not context_text:
        return []
    normalized_context = context_text.lower()
    scored: list[tuple[int, int, dict[str, Any]]] = []
    context_tokens = [token for token in tokenize_for_catalog(normalized_context) if len(token) >= 2]
    for item in items:
        if not isinstance(item, dict):
            continue
        if str(item.get("status") or "active") not in {"active", "approved", "published"}:
            continue
        data = item.get("data", {}) or {}
        searchable = product_searchable_text(data)
        matched_aliases = collect_catalog_product_aliases(data, normalized_context)
        token_hits = [token for token in context_tokens if token and token in searchable]
        score = catalog_alias_match_score(matched_aliases) + len(token_hits)
        if score <= 0:
            continue
        payload = catalog_product_payload(item)
        aliases = ["conversation_context_need", *matched_aliases[:6], *token_hits[:4]]
        payload["matched_aliases"] = list(dict.fromkeys(alias for alias in aliases if alias))
        payload["match_reason"] = "conversation_context_need"
        payload["context_used"] = True
        scored.append((score, max_catalog_alias_length(matched_aliases), payload))
    scored.sort(key=lambda pair: (pair[0], pair[1]), reverse=True)
    return [payload for _score, _alias_len, payload in scored[: max(0, int(limit or 0))]]


def budget_alternative_catalog_candidates(
    items: list[dict[str, Any]],
    *,
    limit: int,
    budget_upper: float,
    target: dict[str, Any] | None = None,
    budget_target: float | None = None,
    preference_text: str = "",
) -> list[dict[str, Any]]:
    target_id = str((target or {}).get("id") or "").strip()
    target_category = catalog_category_label(target or {})
    candidates: list[tuple[int, int, float, float, dict[str, Any]]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        if str(item.get("status") or "active") not in {"active", "approved", "published"}:
            continue
        payload = catalog_product_payload(item)
        if str(payload.get("id") or "") == target_id:
            continue
        price = catalog_price_value(payload)
        if price <= 0 or price > budget_upper + 0.03:
            continue
        preference_rank = catalog_preference_rank(payload, preference_text)
        if preference_rank == 0 and is_cargo_space_need(preference_text):
            payload["match_reason"] = "budget_alternative_cargo_fit"
            payload["matched_aliases"] = list(dict.fromkeys([*payload.get("matched_aliases", []), "cargo_space_fit"]))
        same_category_rank = 0 if target_category and catalog_category_label(payload) == target_category else 1
        payload["matched_aliases"] = list(dict.fromkeys([*payload.get("matched_aliases", []), "budget_alternative"]))
        payload.setdefault("match_reason", "budget_alternative_price_fit")
        target_price = budget_target if budget_target is not None and budget_target > 0 else budget_upper
        candidates.append((preference_rank, same_category_rank, abs(target_price - price), -price, payload))
    candidates.sort(key=lambda item: (item[0], item[1], item[2], item[3]))
    return [payload for _pref_rank, _same_rank, _gap, _neg_price, payload in candidates[: max(0, int(limit or 0))]]


def catalog_preference_rank(item: dict[str, Any], preference_text: str) -> int:
    if not is_cargo_space_need(preference_text):
        return 0
    searchable = compact_match_text(
        " ".join(
            str(item.get(key) or "")
            for key in ("name", "category", "specs", "recommendation", "warranty_policy", "shipping_policy")
        )
    )
    category = compact_match_text(catalog_category_label(item))
    if any(compact_match_text(term) in searchable or compact_match_text(term) in category for term in CARGO_SPACE_FIT_CATEGORY_TERMS):
        return 0
    if any(compact_match_text(term) in searchable or compact_match_text(term) in category for term in CARGO_SPACE_WEAK_CATEGORY_TERMS):
        return 2
    return 1


def catalog_preference_match_score(data: dict[str, Any], preference_text: str) -> tuple[int, list[str]]:
    compact_query = compact_match_text(preference_text)
    if not compact_query:
        return 0, []
    searchable = compact_match_text(product_searchable_text(data))
    if not searchable:
        return 0, []
    total = 0
    labels: list[str] = []
    for group in CATALOG_PREFERENCE_GROUPS:
        query_terms = tuple(str(item) for item in group.get("query", ()) if str(item))
        if not any(compact_match_text(term) in compact_query for term in query_terms):
            continue
        positive_terms = tuple(str(item) for item in group.get("positive", ()) if str(item))
        negative_terms = tuple(str(item) for item in group.get("negative", ()) if str(item))
        positive_hits = [term for term in positive_terms if compact_match_text(term) in searchable]
        negative_hits = [term for term in negative_terms if compact_match_text(term) in searchable]
        if not positive_hits:
            continue
        group_score = 70 + len(positive_hits) * 25 - len(negative_hits) * 45
        if group_score <= 0:
            continue
        total += group_score
        labels.extend(str(item) for item in group.get("label", ()) if str(item))
        labels.extend(f"preference:{term}" for term in positive_hits[:3])
    if is_cargo_space_need(preference_text):
        category = compact_match_text(str(data.get("category") or ""))
        if any(compact_match_text(term) in searchable or compact_match_text(term) in category for term in CARGO_SPACE_FIT_CATEGORY_TERMS):
            total += 65
            labels.append("catalog_preference_cargo_space")
        elif any(compact_match_text(term) in searchable or compact_match_text(term) in category for term in CARGO_SPACE_WEAK_CATEGORY_TERMS):
            total += 15
            labels.append("catalog_preference_cargo_space_weak")
    return total, list(dict.fromkeys(labels))


def is_cargo_space_need(text: str) -> bool:
    compact = compact_match_text(text)
    return any(compact_match_text(term) in compact for term in CARGO_SPACE_NEED_TERMS)


def merge_catalog_ranked_candidates(primary: list[dict[str, Any]], additions: list[dict[str, Any]], *, limit: int) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in [*(primary[:1] if primary else []), *additions, *(primary[1:] if len(primary) > 1 else [])]:
        item_id = str(item.get("id") or "")
        if item_id and item_id in seen:
            continue
        if item_id:
            seen.add(item_id)
        merged.append(item)
        if len(merged) >= max(0, int(limit or 0)):
            break
    return merged


def merge_catalog_priority_candidates(primary: list[dict[str, Any]], additions: list[dict[str, Any]], *, limit: int) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in [*primary, *additions]:
        if not isinstance(item, dict):
            continue
        item_id = str(item.get("id") or "")
        if item_id and item_id in seen:
            continue
        if item_id:
            seen.add(item_id)
        merged.append(item)
        if len(merged) >= max(0, int(limit or 0)):
            break
    return merged


def catalog_category_label(item: dict[str, Any]) -> str:
    category = str(item.get("category") or "").strip()
    if "/" in category:
        category = category.rsplit("/", 1)[-1]
    return category.strip()


def extract_catalog_budget_upper(text: str) -> float | None:
    raw = str(text or "")
    range_match = re.search(r"(\d+(?:\.\d+)?)\s*(?:到|-|~|至|—|－)\s*(\d+(?:\.\d+)?)\s*万", raw)
    if range_match:
        return max(float(range_match.group(1)), float(range_match.group(2)))
    around_matches = [
        catalog_budget_around_upper(float(item))
        for item in re.findall(r"(\d+(?:\.\d+)?)\s*(?:万|w|W)\s*(?:左右|上下|附近|出头)", raw)
    ]
    if around_matches:
        return max(around_matches)
    chinese_around_matches = [
        catalog_budget_around_upper(value)
        for value in (
            chinese_wan_budget_to_float(match.group(1))
            for match in re.finditer(
                r"([零〇一二两三四五六七八九十百]+(?:点[零〇一二两三四五六七八九]+)?)\s*万\s*(?:左右|上下|附近|出头)",
                raw,
            )
        )
        if value is not None
    ]
    if chinese_around_matches:
        return max(chinese_around_matches)
    matches = [float(item) for item in re.findall(r"(\d+(?:\.\d+)?)\s*(?:万|w|W)", raw)]
    if matches:
        return max(matches)
    chinese_matches = [
        value
        for value in (chinese_wan_budget_to_float(match.group(0)) for match in re.finditer(r"[零〇一二两三四五六七八九十百]+(?:点[零〇一二两三四五六七八九]+)?\s*万", raw))
        if value is not None
    ]
    if chinese_matches:
        return max(chinese_matches)
    return None


def extract_catalog_budget_target(text: str) -> float | None:
    raw = str(text or "")
    range_match = re.search(r"(\d+(?:\.\d+)?)\s*(?:到|-|~|至|—|－)\s*(\d+(?:\.\d+)?)\s*万", raw)
    if range_match:
        left = float(range_match.group(1))
        right = float(range_match.group(2))
        return (left + right) / 2
    around_matches = [float(item) for item in re.findall(r"(\d+(?:\.\d+)?)\s*(?:万|w|W)\s*(?:左右|上下|附近|出头)", raw)]
    if around_matches:
        return max(around_matches)
    chinese_around_matches = [
        value
        for value in (
            chinese_wan_budget_to_float(match.group(1))
            for match in re.finditer(
                r"([零〇一二两三四五六七八九十百]+(?:点[零〇一二两三四五六七八九]+)?)\s*万\s*(?:左右|上下|附近|出头)",
                raw,
            )
        )
        if value is not None
    ]
    if chinese_around_matches:
        return max(chinese_around_matches)
    matches = [float(item) for item in re.findall(r"(\d+(?:\.\d+)?)\s*(?:万|w|W)", raw)]
    if matches:
        return max(matches)
    chinese_matches = [
        value
        for value in (chinese_wan_budget_to_float(match.group(0)) for match in re.finditer(r"[零〇一二两三四五六七八九十百]+(?:点[零〇一二两三四五六七八九]+)?\s*万", raw))
        if value is not None
    ]
    if chinese_matches:
        return max(chinese_matches)
    return None


def catalog_budget_around_upper(value: float) -> float:
    """Treat "X万左右" as a real-world shopping band, not a hard ceiling."""

    if value <= 0:
        return value
    if value <= 6:
        return value + 1.0
    if value <= 15:
        return value + 1.5
    return value * 1.12


def chinese_wan_budget_to_float(text: str) -> float | None:
    token = re.sub(r"\s*万.*$", "", str(text or "").strip())
    if not token:
        return None
    if "点" in token:
        integer_text, decimal_text = token.split("点", 1)
        integer_value = chinese_integer_to_int(integer_text)
        digits = []
        digit_map = chinese_digit_map()
        for char in decimal_text:
            if char not in digit_map:
                return None
            digits.append(str(digit_map[char]))
        if integer_value is None or not digits:
            return None
        return float(f"{integer_value}.{''.join(digits)}")
    integer_value = chinese_integer_to_int(token)
    return float(integer_value) if integer_value is not None else None


def chinese_integer_to_int(text: str) -> int | None:
    token = str(text or "").strip()
    if not token:
        return None
    digit_map = chinese_digit_map()
    total = 0
    current = 0
    has_unit = False
    for char in token:
        if char in digit_map:
            current = digit_map[char]
            continue
        if char == "十":
            has_unit = True
            total += (current or 1) * 10
            current = 0
            continue
        if char == "百":
            has_unit = True
            total += (current or 1) * 100
            current = 0
            continue
        return None
    if has_unit:
        return total + current
    if len(token) == 1 and token in digit_map:
        return digit_map[token]
    return None


def chinese_digit_map() -> dict[str, int]:
    return {
        "零": 0,
        "〇": 0,
        "一": 1,
        "二": 2,
        "两": 2,
        "三": 3,
        "四": 4,
        "五": 5,
        "六": 6,
        "七": 7,
        "八": 8,
        "九": 9,
    }


def detect_catalog_request_mode(normalized_text: str) -> str:
    compact = compact_match_text(normalized_text)
    if not compact:
        return ""
    if any(compact_match_text(term) in compact for term in CATALOG_PRICE_SUPERLATIVE_TERMS):
        return "price_desc"
    if any(compact_match_text(term) in compact for term in CATALOG_LIST_REQUEST_TERMS):
        return "price_list"
    # Weak shop-context words alone are not a catalog-list request.  In real
    # chats they often appear with a specific model ("你们这儿有个奥迪A4"), and
    # treating them as a price-list request can bury the exact product evidence
    # before the Brain ever sees it.
    if any(compact_match_text(term) in compact for term in WEAK_CATALOG_CONTEXT_TERMS):
        if any(compact_match_text(term) in compact for term in ("有哪些", "有什么", "报价", "价格", "价目", "清单")):
            return "price_list"
    return ""


def catalog_request_candidates(
    items: list[dict[str, Any]],
    *,
    limit: int,
    mode: str,
    preference_text: str = "",
) -> list[dict[str, Any]]:
    candidates: list[tuple[int, float, dict[str, Any]]] = []
    normalized_preference = str(preference_text or "").lower()
    for item in items:
        if not isinstance(item, dict):
            continue
        if str(item.get("status") or "active") not in {"active", "approved", "published"}:
            continue
        data = item.get("data", {}) or {}
        payload = catalog_product_payload(item)
        price = catalog_price_value(payload)
        if price <= 0:
            continue
        preference_score, preference_labels = catalog_preference_match_score(data, normalized_preference)
        base_alias = "catalog_price_superlative" if mode == "price_desc" else "catalog_price_list"
        payload["matched_aliases"] = list(dict.fromkeys([base_alias, *preference_labels]))
        if mode == "price_list" and preference_score > 0:
            payload["match_reason"] = "catalog_preference_price_list"
        else:
            payload["match_reason"] = "catalog_price_superlative" if mode == "price_desc" else "catalog_price_list_request"
        candidates.append((preference_score, price, payload))
    if any(preference_score > 0 for preference_score, _price, _payload in candidates):
        candidates.sort(key=lambda pair: (pair[0], pair[1]), reverse=True)
    else:
        candidates.sort(key=lambda pair: pair[1], reverse=True)
    return [payload for _preference_score, _price, payload in candidates[: max(0, int(limit or 0))]]


def catalog_price_value(item: dict[str, Any]) -> float:
    raw = item.get("price")
    try:
        return float(raw)
    except (TypeError, ValueError):
        digits = re.sub(r"[^0-9.]+", "", str(raw or ""))
        try:
            return float(digits) if digits else 0.0
        except ValueError:
            return 0.0


def collect_catalog_product_aliases(data: dict[str, Any], query_text: str) -> list[str]:
    aliases = [
        str(data.get("name") or ""),
        str(data.get("sku") or ""),
        *[str(alias) for alias in data.get("aliases", []) or []],
    ]
    matched = collect_matched_aliases(aliases, query_text)
    # Keep concrete model aliases first so "秦PLUS" beats generic "新能源/混动",
    # while still leaving generic hits available as weak recall signals.
    concrete = [alias for alias in matched if is_concrete_catalog_alias(alias)]
    generic = [alias for alias in matched if alias not in concrete]
    return [*concrete, *generic]


def catalog_alias_match_score(matched_aliases: list[str]) -> int:
    if not matched_aliases:
        return 0
    concrete = [alias for alias in matched_aliases if is_concrete_catalog_alias(alias)]
    if concrete:
        return 1000 + max_catalog_alias_length(concrete) * 10 + len(concrete)
    return 20 + max_catalog_alias_length(matched_aliases)


def max_catalog_alias_length(aliases: list[str]) -> int:
    lengths = [len(compact_match_text(alias)) for alias in aliases if compact_match_text(alias)]
    return max(lengths, default=0)


def is_concrete_catalog_alias(alias: str) -> bool:
    compact = compact_match_text(alias)
    if len(compact) < 2:
        return False
    if compact in {compact_match_text(item) for item in GENERIC_CATALOG_ALIAS_TERMS}:
        return False
    if any(compact_match_text(term) in compact for term in WEAK_DESCRIPTIVE_CATALOG_ALIAS_TERMS):
        return False
    return True


def is_explicit_catalog_candidate(item: dict[str, Any]) -> bool:
    aliases = [str(alias) for alias in item.get("matched_aliases", []) or [] if str(alias)]
    if not aliases or item.get("context_used") is True:
        return False
    return any(alias != "conversation_context" and is_concrete_catalog_alias(alias) for alias in aliases)


def is_context_only_product_candidate(item: dict[str, Any]) -> bool:
    aliases = [str(alias) for alias in item.get("matched_aliases", []) or [] if str(alias)]
    return bool(aliases) and set(aliases) <= {"conversation_context"}


def is_context_need_catalog_candidate(item: dict[str, Any]) -> bool:
    return str(item.get("match_reason") or "") == "conversation_context_need"


def is_context_recent_product_candidate(item: dict[str, Any]) -> bool:
    return str(item.get("match_reason") or "") == "conversation_context_recent_products"


def is_weak_catalog_only_product_candidate(item: dict[str, Any]) -> bool:
    aliases = [str(alias) for alias in item.get("matched_aliases", []) or [] if str(alias)]
    return bool(aliases) and set(aliases) <= {"catalog"}


def context_product_candidate(
    runtime: KnowledgeRuntime,
    context: dict[str, Any],
    text: str,
    *,
    items: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    product_id = str(context.get("last_product_id") or "").strip()
    if not product_id or not should_focus_context_product(text):
        return None
    if text_has_explicit_other_catalog_product_reference(text, items=items, exclude_product_id=product_id):
        return None
    if text_has_unresolved_explicit_product_reference(text, items=items):
        return None
    try:
        item = runtime.get_item("products", product_id)
    except Exception:
        return None
    if not isinstance(item, dict):
        return None
    payload = catalog_product_payload(item)
    payload["matched_aliases"] = ["conversation_context"]
    payload["context_used"] = True
    return payload


def context_product_candidates(
    runtime: KnowledgeRuntime,
    context: dict[str, Any],
    text: str,
    *,
    items: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    if not should_focus_context_product(text):
        return []
    if text_has_explicit_other_catalog_product_reference(text, items=items, exclude_product_id=""):
        return []
    if not has_relative_context_product_reference(text) and text_has_unresolved_explicit_product_reference(text, items=items):
        return []
    raw_ids = context.get("recent_product_ids") if isinstance(context.get("recent_product_ids"), list) else []
    if not raw_ids:
        last_product_id = str(context.get("last_product_id") or "").strip()
        raw_ids = [last_product_id] if last_product_id else []
    payloads: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw_id in raw_ids[:5]:
        product_id = str(raw_id or "").strip()
        if not product_id or product_id in seen:
            continue
        seen.add(product_id)
        try:
            item = runtime.get_item("products", product_id)
        except Exception:
            continue
        if not isinstance(item, dict):
            continue
        payload = catalog_product_payload(item)
        payload["matched_aliases"] = ["conversation_context_recent"]
        payload["match_reason"] = "conversation_context_recent_products"
        payload["context_used"] = True
        payloads.append(payload)
    return payloads


def has_relative_context_product_reference(text: str) -> bool:
    compact = compact_match_text(text)
    if not compact:
        return False
    return any(compact_match_text(term) in compact for term in RELATIVE_CONTEXT_PRODUCT_TERMS)


def text_has_explicit_other_catalog_product_reference(
    text: str,
    *,
    items: list[dict[str, Any]] | None,
    exclude_product_id: str,
) -> bool:
    normalized = str(text or "").lower()
    if not normalized:
        return False
    exclude = str(exclude_product_id or "").strip()
    for item in items or []:
        if not isinstance(item, dict):
            continue
        item_id = str(item.get("id") or "").strip()
        if not item_id or (exclude and item_id == exclude):
            continue
        if str(item.get("status") or "active") not in {"active", "approved", "published"}:
            continue
        data = item.get("data", {}) or {}
        matched_aliases = collect_catalog_product_aliases(data, normalized)
        if any(is_concrete_catalog_alias(alias) for alias in matched_aliases):
            return True
    return False


def text_has_unresolved_explicit_product_reference(text: str, *, items: list[dict[str, Any]] | None) -> bool:
    """Detect product-like direct references that should not inherit old context.

    If the current catalog can match the reference, `text_has_explicit_other_catalog_product_reference`
    handles it. This fallback is for user typos or out-of-stock names such as
    "塞纳多少钱" when the active tenant has no Sienna item: replying with the last
    context product would be worse than asking the Brain to clarify.
    """
    if text_has_any_catalog_product_reference(text, items=items):
        return False
    return text_has_product_like_reference_near_detail_term(text)


def text_has_any_catalog_product_reference(text: str, *, items: list[dict[str, Any]] | None) -> bool:
    normalized = str(text or "").lower()
    if not normalized:
        return False
    for item in items or []:
        if not isinstance(item, dict):
            continue
        if str(item.get("status") or "active") not in {"active", "approved", "published"}:
            continue
        data = item.get("data", {}) or {}
        matched_aliases = collect_catalog_product_aliases(data, normalized)
        if any(is_concrete_catalog_alias(alias) for alias in matched_aliases):
            return True
    return False


def text_has_product_like_reference_near_detail_term(text: str) -> bool:
    compact = compact_match_text(text)
    if not compact:
        return False
    detail_terms = tuple(compact_match_text(term) for term in CONTEXT_PRODUCT_DETAIL_TERMS)
    relative_terms = tuple(compact_match_text(term) for term in RELATIVE_CONTEXT_PRODUCT_TERMS)
    for term in detail_terms:
        if not term:
            continue
        index = compact.find(term)
        if index <= 0:
            continue
        prefix = compact[:index]
        if not prefix or prefix_is_relative_context_reference(prefix):
            continue
        if prefix in {compact_match_text(item) for item in GENERIC_PRODUCT_REFERENCE_TERMS}:
            continue
        if not prefix_has_unresolved_product_entity_signal(prefix):
            continue
        # A short brand/model token before "多少钱/价格/配置" is an explicit entity cue.
        if re.search(r"[a-z0-9]{2,}$", prefix, flags=re.IGNORECASE):
            return True
        chinese_tail = re.search(r"[\u4e00-\u9fff]{2,}$", prefix)
        if chinese_tail and chinese_tail.group(0) not in {compact_match_text(item) for item in GENERIC_PRODUCT_REFERENCE_TERMS}:
            return True
    return False


def prefix_has_unresolved_product_entity_signal(prefix: str) -> bool:
    compact_prefix = compact_match_text(prefix)
    if not compact_prefix:
        return False
    detail_terms = tuple(compact_match_text(term) for term in CONTEXT_PRODUCT_DETAIL_TERMS)
    if any(term and term in compact_prefix for term in detail_terms):
        return False
    non_entity_terms = {
        "能不能",
        "能否",
        "可以",
        "可不可以",
        "有没有",
        "有无",
        "会不会",
        "是否",
        "大概",
        "具体",
        "现在",
        "目前",
        "问下",
        "问一下",
        "帮我",
        "麻烦",
    }
    if any(term in compact_prefix for term in non_entity_terms):
        return False
    if re.search(r"[a-z0-9]{2,}$", compact_prefix, flags=re.IGNORECASE):
        return True
    chinese_tail = re.search(r"[\u4e00-\u9fff]{2,}$", compact_prefix)
    if not chinese_tail:
        return False
    tail = chinese_tail.group(0)
    if tail in {compact_match_text(item) for item in GENERIC_PRODUCT_REFERENCE_TERMS}:
        return False
    if len(tail) > 8:
        return False
    return True


def prefix_is_relative_context_reference(prefix: str) -> bool:
    compact_prefix = compact_match_text(prefix)
    if not compact_prefix:
        return True
    relative_terms = tuple(compact_match_text(term) for term in RELATIVE_CONTEXT_PRODUCT_TERMS)
    filler_terms = {compact_match_text(item) for item in RELATIVE_CONTEXT_PREFIX_FILLERS}
    generic_terms = {compact_match_text(item) for item in GENERIC_PRODUCT_REFERENCE_TERMS}
    if compact_prefix in generic_terms:
        return True
    for relative in relative_terms:
        if not relative:
            continue
        if compact_prefix == relative or compact_prefix.endswith(relative):
            return True
        if compact_prefix.startswith(relative):
            tail = compact_prefix[len(relative) :]
            if tail in filler_terms or tail in generic_terms:
                return True
    return False


def should_focus_context_product(text: str) -> bool:
    normalized = re.sub(r"\s+", "", str(text or "").lower())
    if not normalized:
        return False
    relative_terms = (*RELATIVE_CONTEXT_PRODUCT_TERMS, *CONTEXT_PRODUCT_DETAIL_TERMS)
    return any(term in normalized for term in relative_terms)


def tokenize_for_catalog(text: str) -> list[str]:
    tokens = []
    for run in re.findall(r"[a-z0-9][a-z0-9_.-]{1,}|[\u4e00-\u9fff]{2,}", text, flags=re.IGNORECASE):
        normalized = run.lower()
        if len(normalized) <= 18:
            tokens.append(normalized)
        if re.search(r"[\u4e00-\u9fff]", normalized):
            for size in (2, 3, 4, 5, 6):
                if len(normalized) >= size:
                    tokens.extend(normalized[index : index + size] for index in range(0, len(normalized) - size + 1))
    return sorted(set(token for token in tokens if token.strip()), key=lambda item: (-len(item), item))


def fallback_catalog_score(data: dict[str, Any], query_text: str = "") -> int:
    query_tokens = set(tokenize_for_catalog(query_text))
    if not query_tokens:
        return 0
    product_tokens = set(tokenize_for_catalog(product_searchable_text(data)))
    if query_tokens & product_tokens:
        return 1
    return 0


def product_searchable_text(data: dict[str, Any]) -> str:
    parts = [
        str(data.get("name") or ""),
        str(data.get("sku") or ""),
        str(data.get("category") or ""),
        str(data.get("specs") or ""),
        " ".join(str(alias) for alias in data.get("aliases", []) or []),
        " ".join(str(rule) for rule in data.get("risk_rules", []) or []),
        " ".join(str(value) for value in (data.get("reply_templates", {}) or {}).values()),
        " ".join(str(value) for value in flatten_text_values(data.get("additional_details"))),
    ]
    return " ".join(part for part in parts if part).lower()


def flatten_text_values(value: Any) -> list[str]:
    if value in (None, ""):
        return []
    if isinstance(value, (str, int, float, bool)):
        return [str(value)]
    if isinstance(value, list):
        values: list[str] = []
        for item in value:
            values.extend(flatten_text_values(item))
        return values
    if isinstance(value, dict):
        values = []
        for item in value.values():
            values.extend(flatten_text_values(item))
        return values
    return [str(value)]


def catalog_product_payload(item: dict[str, Any]) -> dict[str, Any]:
    data = item.get("data", {}) or {}
    return {
        "id": item.get("id"),
        "category_id": PRODUCT_MASTER_CATEGORY_ID,
        "authority_level": "product_master",
        "name": data.get("name"),
        "sku": data.get("sku"),
        "category": data.get("category"),
        "aliases": list(data.get("aliases", []) or [])[:10],
        "specs": truncate_text(str(data.get("specs") or ""), 260),
        "price": data.get("price"),
        "unit": data.get("unit"),
        "stock": data.get("inventory"),
        "shipping": truncate_text(str(data.get("shipping_policy") or ""), 220),
        "warranty": truncate_text(str(data.get("warranty_policy") or ""), 220),
        "reply_templates": compact_mapping(data.get("reply_templates", {}) or {}, max_text_chars=260),
        "risk_rules": list(data.get("risk_rules", []) or [])[:8],
    }


def compact_decision(decision: Any) -> dict[str, Any]:
    return {
        "reply_text": truncate_text(str(getattr(decision, "reply_text", "") or ""), 500),
        "rule_name": str(getattr(decision, "rule_name", "") or ""),
        "matched": bool(getattr(decision, "matched", False)),
        "need_handoff": bool(getattr(decision, "need_handoff", False)),
        "reason": str(getattr(decision, "reason", "") or ""),
    }


def compact_mapping(value: Any, *, max_text_chars: int = DEFAULT_MAX_TEXT_CHARS) -> Any:
    if isinstance(value, dict):
        return {
            str(key): compact_mapping(item, max_text_chars=max_text_chars)
            for key, item in value.items()
            if item not in (None, "", [], {})
        }
    if isinstance(value, list):
        return [compact_mapping(item, max_text_chars=max_text_chars) for item in value[:20]]
    if isinstance(value, str):
        return truncate_text(value, max_text_chars)
    return value


def truncate_text(text: str, max_chars: int) -> str:
    clean = " ".join(str(text or "").split())
    if len(clean) <= max_chars:
        return clean
    return clean[: max(1, max_chars - 1)].rstrip() + "..."
