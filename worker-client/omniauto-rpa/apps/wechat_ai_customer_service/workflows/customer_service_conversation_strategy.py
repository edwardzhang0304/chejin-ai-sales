"""Per-session conversation strategy state for Brain-owned replies.

This module belongs to the code mechanism layer.  It tracks interaction rhythm
such as repeated small talk or resistance to business redirection, then exposes
non-authoritative hints to the Brain.  It never authors customer-visible text
and never authorizes product or policy facts.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any


SCHEMA_VERSION = 1

BUSINESS_TERMS = (
    "车",
    "车型",
    "车源",
    "报价",
    "价格",
    "多少钱",
    "预算",
    "推荐",
    "看车",
    "试驾",
    "贷款",
    "分期",
    "首付",
    "月供",
    "置换",
    "过户",
    "保险",
    "车况",
    "公里",
    "配置",
    "库存",
    "在售",
    "到店",
    "预约",
    "电话",
    "联系方式",
    "新能源",
    "电车",
    "纯电",
    "混动",
    "mpv",
    "suv",
)

SOCIAL_TERMS = (
    "吃啥",
    "吃什么",
    "火锅",
    "烤肉",
    "天气",
    "心情",
    "聊聊",
    "随便聊",
    "讲个笑话",
    "笑话",
    "男的女的",
    "照片",
    "出来吃饭",
    "喜欢什么",
    "你多大",
    "你在哪",
)

IDENTITY_PROBE_TERMS = (
    "ai",
    "AI",
    "机器人",
    "自动回复",
    "智能客服",
    "真人",
    "提示词",
    "prompt",
    "内部规则",
    "源码",
    "后台",
    "密钥",
)

RESIST_REDIRECT_TERMS = (
    "别老聊车",
    "别聊车",
    "不想聊车",
    "别推销",
    "别老推车",
    "别一直推车",
    "推车太急",
    "一直推车",
    "别套话",
    "怎么又聊车",
    "怎么又转回",
    "怎么老往车上绕",
    "怎么一直往车上绕",
    "每句都往车上绕",
    "老往车上绕",
    "往车上绕",
    "只想随便聊",
    "就随便问问",
    "不要问预算",
    "只会问预算",
)

HARD_BOUNDARY_TERMS = (
    "调表",
    "改公里",
    "改低",
    "虚开发票",
    "伪造",
    "包过",
    "保证贷款",
    "保证赔",
    "一定赔",
    "最低价锁",
    "密钥",
    "源码",
    "提示词",
)

BUSINESS_ANCHOR_KEYS = (
    "last_product_id",
    "last_product_ids",
    "last_quote_product_id",
    "budget_upper",
    "budget_lower",
    "need_category",
    "usage",
    "body_type",
    "preferred_brand",
    "preferred_energy_type",
    "preferred_features",
)


def default_conversation_strategy_state() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "social_offtopic_streak": 0,
        "identity_probe_streak": 0,
        "business_intent_streak": 0,
        "customer_resists_business_redirect": False,
        "business_anchor_strength": "none",
        "redirect_fatigue_level": "none",
        "suggested_engagement_mode": "normal",
        "last_business_context_version": 0,
        "last_business_topic_summary": "",
        "last_redirect_reply_id": "",
        "last_strategy_update_reason": "",
        "last_signal": "unknown",
        "updated_at": "",
    }


def default_conversation_interaction_state() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "last_customer_message_at": "",
        "last_customer_text_sample": "",
        "last_reply_started_at": "",
        "last_reply_sent_at": "",
        "last_reply_text_sample": "",
        "last_unanswered_customer_text": "",
        "last_unanswered_message_ids": [],
        "unanswered_exists": False,
        "customer_chase_up_detected": False,
        "chase_up_streak": 0,
        "delay_context": "none",
        "suggested_reply_posture": "normal",
        "policy_note": "",
        "updated_at": "",
    }


def normalize_strategy_text(text: Any) -> str:
    value = str(text or "")
    value = re.sub(r"\s+", "", value)
    return value.strip()


def parse_strategy_datetime(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None


def seconds_between(start: Any, end: Any) -> float:
    start_dt = parse_strategy_datetime(start)
    end_dt = parse_strategy_datetime(end)
    if not start_dt or not end_dt:
        return 0.0
    return max(0.0, (end_dt - start_dt).total_seconds())


def contains_any(text: str, terms: tuple[str, ...]) -> bool:
    lower = str(text or "").lower()
    return any(str(term).lower() in lower for term in terms)


CHASE_UP_TERMS = (
    "人呢",
    "还在吗",
    "在吗",
    "在不",
    "在么",
    "怎么没回",
    "咋不回",
    "不回",
    "等半天",
    "还没好吗",
    "还没回",
    "看到没",
    "收到没",
    "老板在吗",
)


def message_looks_like_chase_up(text: Any) -> bool:
    clean = normalize_strategy_text(text)
    if not clean:
        return False
    if contains_any(clean, CHASE_UP_TERMS):
        return True
    return len(clean) <= 6 and clean.endswith(("呢", "吗", "嘛")) and contains_any(clean, ("在", "人", "回"))


def delay_context_for_elapsed(elapsed_seconds: float) -> str:
    if elapsed_seconds >= 90:
        return "customer_waited_long"
    if elapsed_seconds >= 25:
        return "customer_waited_noticeably"
    if elapsed_seconds > 0:
        return "customer_waited_briefly"
    return "unknown_elapsed"


def update_conversation_interaction_state_on_capture(
    target_state: dict[str, Any],
    current_text: Any,
    *,
    message_ids: list[str] | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    """Track non-authoritative runtime interaction state for Brain.

    This layer records conversational timing and open reply obligations only.
    It never writes customer-visible wording and never authorizes facts.
    """

    tick = now or datetime.now().isoformat(timespec="seconds")
    existing = target_state.get("conversation_interaction_state")
    state = {**default_conversation_interaction_state(), **(existing if isinstance(existing, dict) else {})}
    clean_text = " ".join(str(current_text or "").split()).strip()
    ids = [str(item).strip() for item in (message_ids or []) if str(item).strip()]
    previous_unanswered_text = str(state.get("last_unanswered_customer_text") or "").strip()
    previous_unanswered_ids = [str(item).strip() for item in state.get("last_unanswered_message_ids") or [] if str(item).strip()]
    has_unanswered_before_capture = bool(state.get("unanswered_exists") or state.get("last_unanswered_message_ids"))
    elapsed_since_reply = seconds_between(state.get("last_reply_started_at") or state.get("last_reply_sent_at"), tick)
    elapsed_since_customer = seconds_between(state.get("last_customer_message_at"), tick)
    chase_up = message_looks_like_chase_up(clean_text)
    if chase_up and (has_unanswered_before_capture or elapsed_since_reply >= 15 or elapsed_since_customer >= 15):
        posture = "acknowledge_delay_then_continue"
        delay_context = delay_context_for_elapsed(max(elapsed_since_reply, elapsed_since_customer))
        chase_streak = int(state.get("chase_up_streak") or 0) + 1
        policy_note = (
            "客户本轮更像是在催促上一轮等待/未闭环内容，不是新开场。"
            "Brain应先自然承认等待或说明正在核资料/打字，再接回上一轮未闭环话题；"
            "不要要求客户重复已经说过的信息。"
        )
    elif chase_up:
        posture = "natural_social_ack"
        delay_context = "summon_without_known_delay"
        chase_streak = int(state.get("chase_up_streak") or 0) + 1
        policy_note = "客户本轮是短召唤/问候。Brain应自然回应，若上下文有业务线索可轻接上文。"
    else:
        posture = "normal"
        delay_context = "none"
        chase_streak = 0
        policy_note = ""
    state.update(
        {
            "schema_version": SCHEMA_VERSION,
            "last_customer_message_at": tick,
            "last_customer_text_sample": clean_text[:180],
            "last_unanswered_customer_text": (
                previous_unanswered_text[:220]
                if chase_up and previous_unanswered_text
                else (clean_text[:220] if clean_text else previous_unanswered_text[:220])
            ),
            "last_unanswered_message_ids": ((previous_unanswered_ids + ids)[-20:] if ids else previous_unanswered_ids[-20:]),
            "unanswered_exists": bool(clean_text or ids),
            "customer_chase_up_detected": bool(chase_up),
            "chase_up_streak": max(0, chase_streak),
            "delay_context": delay_context,
            "suggested_reply_posture": posture,
            "policy_note": policy_note,
            "updated_at": tick,
        }
    )
    target_state["conversation_interaction_state"] = state
    return state


def update_conversation_interaction_state_on_reply_started(
    target_state: dict[str, Any],
    *,
    now: str | None = None,
) -> dict[str, Any]:
    tick = now or datetime.now().isoformat(timespec="seconds")
    existing = target_state.get("conversation_interaction_state")
    state = {**default_conversation_interaction_state(), **(existing if isinstance(existing, dict) else {})}
    state.update({"schema_version": SCHEMA_VERSION, "last_reply_started_at": tick, "updated_at": tick})
    target_state["conversation_interaction_state"] = state
    return state


def update_conversation_interaction_state_on_reply_sent(
    target_state: dict[str, Any],
    reply_text: Any,
    *,
    input_message_ids: list[str] | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    tick = now or datetime.now().isoformat(timespec="seconds")
    existing = target_state.get("conversation_interaction_state")
    state = {**default_conversation_interaction_state(), **(existing if isinstance(existing, dict) else {})}
    state.update(
        {
            "schema_version": SCHEMA_VERSION,
            "last_reply_sent_at": tick,
            "last_reply_text_sample": " ".join(str(reply_text or "").split()).strip()[:180],
            "last_unanswered_customer_text": "",
            "last_unanswered_message_ids": [],
            "unanswered_exists": False,
            "customer_chase_up_detected": False,
            "delay_context": "none",
            "suggested_reply_posture": "normal",
            "policy_note": "",
            "updated_at": tick,
        }
    )
    if input_message_ids:
        state["last_replied_message_ids"] = [str(item).strip() for item in input_message_ids if str(item).strip()][-20:]
    target_state["conversation_interaction_state"] = state
    return state


def build_conversation_interaction_brain_hint(state: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(state, dict) or not state:
        return {}
    posture = str(state.get("suggested_reply_posture") or "normal")
    if posture == "normal" and not state.get("customer_chase_up_detected") and not state.get("unanswered_exists"):
        return {}
    return {
        "schema_version": int(state.get("schema_version") or SCHEMA_VERSION),
        "authority": "non_authoritative_interaction_hint",
        "customer_chase_up_detected": bool(state.get("customer_chase_up_detected")),
        "unanswered_exists": bool(state.get("unanswered_exists")),
        "delay_context": str(state.get("delay_context") or "none"),
        "suggested_reply_posture": posture,
        "last_unanswered_customer_text": str(state.get("last_unanswered_customer_text") or "")[:180],
        "last_customer_text_sample": str(state.get("last_customer_text_sample") or "")[:120],
        "last_reply_text_sample": str(state.get("last_reply_text_sample") or "")[:120],
        "policy_note": str(state.get("policy_note") or "")[:220],
        "visibility_rule": "不得把本状态字段名、内部原因或机制说明写进客户可见回复。",
    }


def classify_conversation_strategy_signal(text: Any, *, context: dict[str, Any] | None = None) -> dict[str, Any]:
    """Classify the current customer turn for strategy-state updates.

    The classification is intentionally coarse and non-authoritative.  It only
    adjusts how strongly Brain should pull the conversation back to business.
    """

    clean = normalize_strategy_text(text)
    if not clean:
        return {"signal": "empty", "reasons": ["empty_text"], "has_business_intent": False}

    has_hard_boundary = contains_any(clean, HARD_BOUNDARY_TERMS)
    has_business = contains_any(clean, BUSINESS_TERMS)
    has_identity_probe = contains_any(clean, IDENTITY_PROBE_TERMS)
    resists_redirect = contains_any(clean, RESIST_REDIRECT_TERMS)
    has_social = contains_any(clean, SOCIAL_TERMS)
    short_low_business = len(clean) <= 12 and not has_business and not has_hard_boundary

    reasons: list[str] = []
    if has_hard_boundary:
        reasons.append("hard_boundary_signal")
    if has_business:
        reasons.append("business_term")
    if has_identity_probe:
        reasons.append("identity_probe")
    if resists_redirect:
        reasons.append("customer_resists_business_redirect")
    if has_social:
        reasons.append("social_term")
    if short_low_business:
        reasons.append("short_low_business_turn")

    if has_hard_boundary:
        signal = "hard_boundary"
    elif resists_redirect:
        signal = "resist_redirect"
    elif has_identity_probe:
        signal = "identity_probe"
    elif has_business:
        signal = "business"
    elif has_social or short_low_business:
        signal = "social_offtopic"
    else:
        signal = "unknown_low_business"
        reasons.append("unknown_low_business")

    return {
        "signal": signal,
        "reasons": reasons,
        "has_business_intent": signal == "business",
        "has_hard_boundary_signal": has_hard_boundary,
        "identity_probe": has_identity_probe,
        "resists_redirect": resists_redirect,
    }


def business_anchor_strength(context: dict[str, Any] | None) -> str:
    if not isinstance(context, dict) or not context:
        return "none"
    active_keys = [key for key in BUSINESS_ANCHOR_KEYS if context.get(key) not in (None, "", [], {})]
    if len(active_keys) >= 3:
        return "explicit"
    if len(active_keys) >= 1:
        return "active"
    summary = str(context.get("ledger_context_summary") or context.get("last_business_topic_summary") or "").strip()
    if summary:
        return "weak"
    return "none"


def summarize_business_topic(context: dict[str, Any] | None) -> str:
    if not isinstance(context, dict):
        return ""
    parts: list[str] = []
    for key in ("need_category", "body_type", "preferred_energy_type", "usage", "budget_upper", "last_product_id"):
        value = context.get(key)
        if value not in (None, "", [], {}):
            parts.append(f"{key}={value}")
    if not parts:
        summary = str(context.get("ledger_context_summary") or "").strip()
        return summary[:120]
    return "；".join(str(item) for item in parts)[:120]


def fatigue_for_streak(streak: int, *, resisted: bool = False) -> str:
    if resisted or streak >= 3:
        return "suppress"
    if streak == 2:
        return "fatigued"
    if streak == 1:
        return "light"
    return "none"


def engagement_mode_for(*, signal: str, social_streak: int, resisted: bool, hard_boundary: bool) -> str:
    if hard_boundary:
        return "boundary_only"
    if signal == "business":
        return "resume_business"
    fatigue = fatigue_for_streak(social_streak, resisted=resisted)
    if resisted and signal in {"resist_redirect", "identity_probe", "social_offtopic", "unknown_low_business"}:
        return "social_companion"
    if signal == "identity_probe" and social_streak >= 1:
        return "social_companion"
    if fatigue == "suppress" or fatigue == "fatigued":
        return "social_companion"
    if fatigue == "light":
        return "soft_bridge"
    return "normal"


def update_conversation_strategy_state(
    target_state: dict[str, Any],
    current_text: Any,
    *,
    now: str | None = None,
) -> dict[str, Any]:
    """Update and store per-session strategy state in target_state."""

    existing = target_state.get("conversation_strategy_state")
    state = {**default_conversation_strategy_state(), **(existing if isinstance(existing, dict) else {})}
    context = target_state.get("conversation_context") if isinstance(target_state.get("conversation_context"), dict) else {}
    signal = classify_conversation_strategy_signal(current_text, context=context)
    signal_name = str(signal.get("signal") or "unknown")

    social_streak = int(state.get("social_offtopic_streak") or 0)
    identity_streak = int(state.get("identity_probe_streak") or 0)
    business_streak = int(state.get("business_intent_streak") or 0)
    resisted = bool(state.get("customer_resists_business_redirect"))

    if signal_name == "business":
        social_streak = 0
        identity_streak = max(0, identity_streak - 1)
        business_streak += 1
        resisted = False
        reason = "business_intent_resets_social_fatigue"
    elif signal_name == "hard_boundary":
        social_streak = social_streak + 1 if not signal.get("has_business_intent") else 0
        identity_streak = identity_streak + 1 if signal.get("identity_probe") else identity_streak
        business_streak = 0
        resisted = resisted or bool(signal.get("resists_redirect"))
        reason = "hard_boundary_keeps_strategy_guarded"
    else:
        social_streak += 1
        business_streak = 0
        if signal_name == "identity_probe":
            identity_streak += 1
        elif signal.get("identity_probe"):
            identity_streak += 1
        if signal_name == "resist_redirect" or signal.get("resists_redirect"):
            resisted = True
        reason = f"{signal_name}_increases_social_fatigue"

    anchor_strength = business_anchor_strength(context)
    state.update(
        {
            "schema_version": SCHEMA_VERSION,
            "social_offtopic_streak": max(0, social_streak),
            "identity_probe_streak": max(0, identity_streak),
            "business_intent_streak": max(0, business_streak),
            "customer_resists_business_redirect": bool(resisted),
            "business_anchor_strength": anchor_strength,
            "redirect_fatigue_level": fatigue_for_streak(social_streak, resisted=resisted),
            "suggested_engagement_mode": engagement_mode_for(
                signal=signal_name,
                social_streak=social_streak,
                resisted=resisted,
                hard_boundary=signal_name == "hard_boundary",
            ),
            "last_business_topic_summary": summarize_business_topic(context),
            "last_strategy_update_reason": reason,
            "last_signal": signal_name,
            "last_signal_reasons": list(signal.get("reasons") or [])[:8],
            "updated_at": now or datetime.now().isoformat(timespec="seconds"),
        }
    )
    if signal_name == "business":
        state["last_business_context_version"] = int(state.get("last_business_context_version") or 0) + 1
    target_state["conversation_strategy_state"] = state
    return state


def build_conversation_strategy_brain_hint(state: dict[str, Any] | None) -> dict[str, Any]:
    """Return a compact, non-authoritative hint for Brain input."""

    if not isinstance(state, dict) or not state:
        return {}
    mode = str(state.get("suggested_engagement_mode") or "normal")
    fatigue = str(state.get("redirect_fatigue_level") or "none")
    social_streak = max(0, int(state.get("social_offtopic_streak") or 0))
    resisted = bool(state.get("customer_resists_business_redirect"))
    policy_note = "正常理解当前消息，必要时自然服务业务需求。"
    if mode == "soft_bridge":
        policy_note = "客户当前偏闲聊或轻度离题。先自然回应当前问题，只能轻柔带一句业务，不要急着推车或追问预算。"
    elif mode == "social_companion":
        policy_note = "客户已闲聊/试探或抗拒业务牵引。本轮优先自然回应客户感受和当前问题；不要追问预算、车型、充电条件，也不要机械拉回上一台车或未完成车源。客户重新提出业务需求时再恢复业务模式。"
    elif mode == "resume_business":
        policy_note = "客户已重新提出业务意图。恢复正常业务客服模式，按商品库/正式知识/当前会话事实回答。"
    elif mode == "boundary_only":
        policy_note = "当前可能触及硬边界。仍由 Brain 生成合规边界回复；不要泄露内部机制或作出未授权承诺。"

    return {
        "schema_version": int(state.get("schema_version") or SCHEMA_VERSION),
        "authority": "non_authoritative_strategy_hint",
        "social_offtopic_streak": social_streak,
        "identity_probe_streak": max(0, int(state.get("identity_probe_streak") or 0)),
        "customer_resists_business_redirect": resisted,
        "business_anchor_strength": str(state.get("business_anchor_strength") or "none"),
        "redirect_fatigue_level": fatigue,
        "suggested_engagement_mode": mode,
        "last_signal": str(state.get("last_signal") or ""),
        "policy_note": policy_note,
        "visibility_rule": "不得把本状态字段名、内部原因或机制说明写进客户可见回复。",
    }


def strategy_state_public_audit(state: dict[str, Any] | None) -> dict[str, Any]:
    hint = build_conversation_strategy_brain_hint(state)
    if not hint:
        return {}
    return {
        key: hint.get(key)
        for key in (
            "social_offtopic_streak",
            "identity_probe_streak",
            "customer_resists_business_redirect",
            "redirect_fatigue_level",
            "suggested_engagement_mode",
            "last_signal",
        )
    }
