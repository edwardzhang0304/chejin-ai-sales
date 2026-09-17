"""Intent hints must not veto valid Brain replies (synthetic variants of incident 721847f7)."""
from copy import deepcopy

import pytest

import run_customer_service_brain_contract_checks as contracts
from customer_service_brain_contract import normalize_brain_plan, verify_brain_reply_quality, validate_brain_plan
from customer_service_conversation_strategy import (
    classify_conversation_strategy_signal,
    update_conversation_strategy_state,
    build_conversation_strategy_brain_hint,
)
from customer_service_quality_reviewer import should_invoke_semantic_reviewer

WARNING = "social_context_review:over_eager_business_redirect_after_social_fatigue"
UNMATCHED = ["我想买个手动挡", "想要手排的", "三个踏板那种", "两厢的行吗", "得带个天窗", "那就选刚才那个"]

def plan_for(reply):
    return normalize_brain_plan({
        "can_answer": True, "answer_mode": "ask_clarifying_question",
        "evidence_used": {"common_sense_topics": ["购车需求收集"]},
        "facts_claimed": [], "reply_segments": [reply],
        "recommended_action": "send_reply", "confidence": .95,
        "risk": {"risk_level": "low", "risk_tags": [], "needs_handoff": False},
    })

@pytest.mark.parametrize("message", UNMATCHED + ["", "这是一个未出现在业务词表中的较长未知表述"])
def test_missing_keyword_does_not_mean_small_talk(message):
    state = {}
    update_conversation_strategy_state(state, "你好")
    before = deepcopy(state["conversation_strategy_state"])
    after = update_conversation_strategy_state(state, message)
    assert classify_conversation_strategy_signal(message)["signal"] in {"unknown_low_business", "empty"}
    assert after["social_offtopic_streak"] == before["social_offtopic_streak"]
    assert after["customer_resists_business_redirect"] is False

@pytest.mark.parametrize("message", UNMATCHED)
def test_stale_fatigue_is_warning_and_invokes_existing_reviewer(message):
    state = {}
    for text in ["你今天吃啥", "你是不是AI", "别老聊车，我就随便问问", message]:
        strategy = update_conversation_strategy_state(state, text)
    assert strategy["customer_resists_business_redirect"] is True  # retained observation, not a veto
    pack = contracts.fake_evidence_pack(include_product=False)
    pack["conversation_strategy_state"] = build_conversation_strategy_brain_hint(strategy)
    plan = plan_for("可以按这个偏好来选，您预算多少？")
    quality = verify_brain_reply_quality(plan, current_message=message, evidence_pack=pack, settings={})
    assert quality["ok"], quality
    assert WARNING in quality["warnings"], quality
    assert should_invoke_semantic_reviewer(plan=plan, current_message=message, evidence_pack=pack,
                                          deterministic_quality=quality, settings={})

@pytest.mark.parametrize("mode", ["normal", "soft_bridge", "social_companion", "resume_business", "boundary_only"])
def test_compact_hint_keeps_current_intent_priority(mode):
    hint = build_conversation_strategy_brain_hint({"suggested_engagement_mode": mode})
    # Production Brain clips this field to 140 chars. Never hide the correction beyond that boundary.
    assert len(hint["policy_note"]) <= 140
    assert "不受历史闲聊次数限制" in hint["policy_note"]
    assert "当前明确拒绝推销须尊重" in hint["policy_note"]

@pytest.mark.parametrize("reply", ["这台售价5万。", "目前有现车库存。", "贷款审批一定通过。"])
def test_relaxed_tone_hint_does_not_authorize_unsupported_facts(reply):
    plan = plan_for(reply)
    plan["answer_mode"] = "quote_product_fact"
    assert "missing_fact_claims" in validate_brain_plan(plan, require_fact_claims=True)["errors"]

def test_explicit_social_evidence_and_session_isolation_remain():
    contracts.check_conversation_strategy_state_tracks_social_fatigue_and_resets_on_business()
    contracts.check_conversation_strategy_identity_resistance_overrides_business_terms()
    contracts.check_conversation_strategy_state_is_session_isolated()
