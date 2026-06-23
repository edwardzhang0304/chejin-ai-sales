"""LLM-first intent router for WeChat customer-service messages.

Replaces keyword-based routing with semantic intent analysis using a fast LLM.
All customer messages are analyzed by DeepSeek flash (1-2s) before routing to
the appropriate handler: customer_data_capture, product_knowledge, RAG, or handoff.

Cache: intent results are cached in target_state for 60 seconds within the same
conversation to avoid repeated LLM calls for follow-up messages.

Fallback: if LLM fails or times out (5s), falls back to explicit keyword checks.
"""

from __future__ import annotations

import json
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

APP_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = APP_ROOT.parents[1]
for path in (PROJECT_ROOT, APP_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from apps.wechat_ai_customer_service.llm_config import (
    call_llm_request_with_failover,
    read_secret,
    resolve_deepseek_base_url,
    resolve_deepseek_tier_model,
)


DEFAULT_INTENT_CACHE_SECONDS = 60
DEFAULT_INTENT_TIMEOUT_SECONDS = 5
DEFAULT_INTENT_MAX_TOKENS = 256

INTENT_CATEGORIES = {
    "customer_data_provide",
    "product_inquiry",
    "general_chat",
    "greeting",
    "handoff_request",
    "unclear",
}

EXPLICIT_CUSTOMER_DATA_KEYWORDS = [
    "客户资料",
    "客户信息",
    "收货信息",
    "联系方式",
    "联系人",
    "收件人",
    "收货地址",
    "联系电话",
    "手机号",
]

PRODUCT_INQUIRY_KEYWORDS = [
    "多少钱",
    "什么价",
    "报价",
    "询价",
    "价格",
    "预算",
    "推荐",
    "车源",
    "车型",
    "车况",
    "公里数",
    "上牌",
    "过户",
    "分期",
    "贷款",
    "按揭",
    "首付",
    "月供",
    "置换",
    "收车",
    "卖车",
    "检测",
    "验车",
    "试驾",
    "油耗",
    "省油",
    "代步",
    "通勤",
    "家用",
    "配置",
    "排量",
    "颜色",
    "年份",
    "车龄",
]

HANDOFF_REQUEST_PATTERNS = [
    r"转\s*人工",
    r"(找|要|想要|需要|接|换|叫|请|让|安排|联系).{0,4}(人工|人工客服|真人客服|销售|顾问|专员|客服)",
    r"(人工|人工客服|真人客服|销售|顾问|专员|客服).{0,6}(联系|跟进|对接|接管|处理|回我|回复我)",
    r"(有|有没有|能不能|可以).{0,4}(人工|人工客服|真人客服)",
]

GREETING_KEYWORDS = [
    "你好",
    "您好",
    "在吗",
    "在么",
    "有人吗",
    "哈喽",
    "嗨",
    "早上好",
    "下午好",
    "晚上好",
]


@dataclass(frozen=True)
class IntentRouteResult:
    intent: str
    confidence: float
    reasoning: str
    entities: dict[str, str]
    source: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "intent": self.intent,
            "confidence": self.confidence,
            "reasoning": self.reasoning,
            "entities": self.entities,
            "source": self.source,
        }


def route_intent(
    combined: str,
    config: dict[str, Any] | None = None,
    evidence_pack: dict[str, Any] | None = None,
    target_state: dict[str, Any] | None = None,
) -> IntentRouteResult:
    """Analyze customer message intent and return routing decision.

    Priority:
    1. Check cached intent in target_state (60s TTL)
    2. Call DeepSeek flash for intent analysis
    3. On failure/timeout, fall back to keyword-based intent detection
    """
    config = config or {}
    target_state = target_state or {}

    cache_ttl = int(
        (config.get("intent_router", {}) or {}).get("cache_seconds", DEFAULT_INTENT_CACHE_SECONDS)
    )
    cached = _get_cached_intent(target_state, ttl_seconds=cache_ttl)
    if cached:
        return cached

    router_settings = config.get("intent_router", {}) or {}
    realtime_settings = config.get("realtime_reply", {}) or {}
    heuristic_first = router_settings.get("heuristic_first", realtime_settings.get("prefer_local_intent", True)) is not False
    if heuristic_first:
        fallback = _keyword_fallback_intent(combined)
        min_confidence = float(router_settings.get("heuristic_first_min_confidence", 0.5) or 0.5)
        if fallback.confidence >= min_confidence and fallback.intent in {
            "customer_data_provide",
            "product_inquiry",
            "greeting",
            "handoff_request",
        }:
            _set_cached_intent(target_state, fallback, ttl_seconds=cache_ttl)
            return fallback

    llm_configured = isinstance(router_settings.get("llm"), dict)
    llm_settings = (router_settings.get("llm", {}) or {})
    if llm_configured and llm_settings.get("enabled") is not False:
        llm_result = _call_llm_intent_analysis(
            combined=combined,
            evidence_pack=evidence_pack,
            settings=llm_settings,
        )
        if llm_result:
            _set_cached_intent(target_state, llm_result, ttl_seconds=cache_ttl)
            return llm_result

    fallback = _keyword_fallback_intent(combined)
    _set_cached_intent(target_state, fallback, ttl_seconds=cache_ttl)
    return fallback


def _get_cached_intent(
    target_state: dict[str, Any],
    ttl_seconds: int = DEFAULT_INTENT_CACHE_SECONDS,
) -> IntentRouteResult | None:
    cache = target_state.get("_intent_router_cache")
    if not isinstance(cache, dict):
        return None
    created_at = cache.get("created_at")
    if not created_at:
        return None
    try:
        elapsed = (datetime.now() - datetime.fromisoformat(str(created_at))).total_seconds()
    except Exception:
        return None
    if elapsed > ttl_seconds:
        return None
    intent = str(cache.get("intent") or "")
    if intent not in INTENT_CATEGORIES:
        return None
    return IntentRouteResult(
        intent=intent,
        confidence=float(cache.get("confidence", 0)),
        reasoning=str(cache.get("reasoning") or ""),
        entities=dict(cache.get("entities", {}) or {}),
        source=str(cache.get("source") or "cache"),
    )


def _set_cached_intent(
    target_state: dict[str, Any],
    result: IntentRouteResult,
    ttl_seconds: int = DEFAULT_INTENT_CACHE_SECONDS,
) -> None:
    target_state["_intent_router_cache"] = {
        "intent": result.intent,
        "confidence": result.confidence,
        "reasoning": result.reasoning,
        "entities": result.entities,
        "source": result.source,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "ttl_seconds": ttl_seconds,
    }


def _call_llm_intent_analysis(
    combined: str,
    evidence_pack: dict[str, Any] | None,
    settings: dict[str, Any],
) -> IntentRouteResult | None:
    api_key = read_secret("DEEPSEEK_API_KEY")
    if not api_key:
        return None

    model = resolve_deepseek_tier_model(
        tier="flash",
        explicit_model=settings.get("model") or None,
        read_secret_fn=read_secret,
    )
    base_url = resolve_deepseek_base_url(
        explicit_base_url=settings.get("base_url") or None,
        read_secret_fn=read_secret,
    )
    timeout = int(settings.get("timeout_seconds", DEFAULT_INTENT_TIMEOUT_SECONDS))
    max_tokens = int(settings.get("max_tokens", DEFAULT_INTENT_MAX_TOKENS))

    evidence_summary = _compact_evidence(evidence_pack)

    system_prompt = (
        "你是微信AI客服的意图分析专家。请根据客户消息判断其真实意图。\n\n"
        "分类标准：\n"
        "- customer_data_provide：客户明确在提供/更新自己的姓名、电话、地址、"
        "联系方式、收货信息、个人资料。例如：\"我叫张三，电话13812345678\"、"
        "\"收货地址是xxx\"。\n"
        "- product_inquiry：客户询问产品/商品信息，包括价格、型号、配置、车况、"
        "库存、规格等。例如：\"某款车多少钱\"、\"这车油耗怎么样\"。\n"
        "- handoff_request：客户明确要求转人工、找销售、找顾问。"
        "例如：\"找人工\"、\"我要真人客服\"。\n"
        "- greeting：客户打招呼、寒暄。例如：\"你好\"、\"在吗\"。\n"
        "- general_chat：其他业务咨询、闲聊、表达需求但不属于以上类别。"
        "例如：\"有什么推荐\"、\"预算10万\"。\n"
        "- unclear：消息过于模糊，无法判断意图。\n\n"
        "重要规则：\n"
        "1. 仅询问价格、询问车型不属于提供客户资料，必须标记为 product_inquiry。\n"
        "2. 消息中同时包含产品询问和客户资料时，以客户资料为主（customer_data_provide）。\n"
        "3. 不要过度推断，没有明确客户资料信号时不要标记为 customer_data_provide。\n"
        "4. 必须输出严格JSON格式，不要有任何其他文字。\n"
    )

    user_content = json.dumps(
        {
            "customer_message": combined,
            "evidence_summary": evidence_summary,
        },
        ensure_ascii=False,
    )

    result = call_llm_request_with_failover(
        provider="deepseek",
        api_key=api_key,
        base_url=base_url,
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        timeout=max(1, timeout),
        max_tokens=max_tokens,
        temperature=0.1,
        tier="flash",
        json_mode=True,
        allow_fallback=False,
    )
    if not result.get("ok"):
        return None
    content = str(result.get("response_text") or "")

    parsed = _parse_intent_json(content)
    if parsed:
        return IntentRouteResult(
            intent=parsed["intent"],
            confidence=parsed["confidence"],
            reasoning=parsed["reasoning"],
            entities=parsed["entities"],
            source="llm",
        )
    return None


def _parse_intent_json(text: str) -> dict[str, Any] | None:
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
    if not isinstance(payload, dict):
        return None

    intent = str(payload.get("intent") or "").strip()
    if intent not in INTENT_CATEGORIES:
        return None

    try:
        confidence = float(payload.get("confidence", 0))
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))

    return {
        "intent": intent,
        "confidence": confidence,
        "reasoning": str(payload.get("reasoning") or ""),
        "entities": dict(payload.get("entities", {}) or {}) if isinstance(payload.get("entities"), dict) else {},
    }


def _keyword_fallback_intent(combined: str) -> IntentRouteResult:
    """Fast keyword-based fallback when LLM is unavailable."""
    text = combined.lower()

    for keyword in EXPLICIT_CUSTOMER_DATA_KEYWORDS:
        if keyword.lower() in text:
            return IntentRouteResult(
                intent="customer_data_provide",
                confidence=0.7,
                reasoning=f"keyword_fallback: explicit customer data keyword '{keyword}'",
                entities={},
                source="keyword_fallback",
            )

    for pattern in HANDOFF_REQUEST_PATTERNS:
        if re.search(pattern, text, flags=re.IGNORECASE):
            return IntentRouteResult(
                intent="handoff_request",
                confidence=0.7,
                reasoning=f"keyword_fallback: handoff pattern '{pattern}'",
                entities={},
                source="keyword_fallback",
            )

    for keyword in PRODUCT_INQUIRY_KEYWORDS:
        if keyword.lower() in text:
            return IntentRouteResult(
                intent="product_inquiry",
                confidence=0.5,
                reasoning=f"keyword_fallback: product inquiry keyword '{keyword}'",
                entities={},
                source="keyword_fallback",
            )

    for keyword in GREETING_KEYWORDS:
        if keyword.lower() in text:
            return IntentRouteResult(
                intent="greeting",
                confidence=0.6,
                reasoning=f"keyword_fallback: greeting keyword '{keyword}'",
                entities={},
                source="keyword_fallback",
            )

    return IntentRouteResult(
        intent="unclear",
        confidence=0.3,
        reasoning="keyword_fallback: no clear intent keywords matched",
        entities={},
        source="keyword_fallback",
    )


def _compact_evidence(evidence_pack: dict[str, Any] | None) -> dict[str, Any]:
    """Build a compact summary of evidence for the LLM prompt."""
    if not evidence_pack:
        return {}
    evidence = evidence_pack.get("evidence", {}) or {}
    summary: dict[str, Any] = {}

    products = evidence.get("products", []) or []
    if products:
        summary["matched_products"] = [
            {"name": p.get("name"), "price": p.get("price")}
            for p in products[:3]
        ]

    faq = evidence.get("faq", []) or []
    if faq:
        summary["matched_faq"] = [
            {"intent": f.get("intent"), "answer_preview": str(f.get("answer", ""))[:60]}
            for f in faq[:3]
        ]

    policies = evidence.get("policies", {}) or {}
    if policies:
        summary["matched_policies"] = list(policies.keys())[:3]

    rag = evidence.get("rag", {}) or {}
    if rag and rag.get("hits"):
        summary["rag_hits"] = len(rag.get("hits", []))

    return summary


def clear_intent_cache(target_state: dict[str, Any]) -> None:
    """Clear the intent cache for a target conversation."""
    target_state.pop("_intent_router_cache", None)
