import pytest

from app.services.reply_sequence_policy import segments_for_decision


def payload(parts):
    return {"raw_payload": {"omniauto_brain_result": {"brain_plan": {"reply_segments": parts}}}}


@pytest.mark.parametrize("length", [1, 107, 108])
def test_short_reply_stays_one_message(length):
    text = "啊" * length
    assert segments_for_decision(text, payload([text])) == [text]


def test_punctuation_space_and_repeated_words_are_not_dropped():
    parts = ["这台车的标价是12.88万元。" * 4, "是否能优惠，需要销售确认。" * 4, "这台车的标价是12.88万元。" * 4]
    text = " ".join(parts)
    packed = segments_for_decision(text, payload(parts))
    assert len(packed) == 3
    assert " ".join(packed) == text
    assert all(len(part) <= 108 for part in packed)


def test_109_characters_require_a_real_semantic_boundary():
    parts = ["啊" * 53 + "。", "不保证一定能贷款，请以审核结果为准。" + "啊" * 35 + "。"]
    text = " ".join(parts)
    assert len(text) > 108
    assert segments_for_decision(text, payload(parts)) == parts
    with pytest.raises(ValueError, match="REPLY_SEQUENCE_REWRITE_REQUIRED"):
        segments_for_decision("啊" * 109, payload(["啊" * 109]))


@pytest.mark.parametrize("parts", [["啊" * 108] * 4, ["啊" * 109, "尾巴不能丢。"]])
def test_capacity_overflow_requires_rewrite_not_tail_truncation(parts):
    with pytest.raises(ValueError, match="REPLY_SEQUENCE_REWRITE_REQUIRED"):
        segments_for_decision(" ".join(parts), payload(parts))


def test_provider_candidates_cannot_replace_guarded_text():
    with pytest.raises(ValueError, match="REPLY_SEQUENCE_REWRITE_REQUIRED"):
        segments_for_decision("原文" * 90, payload(["另一份" * 30, "内容" * 30]))


def test_sequence_brain_retains_fourth_segment_for_repair():
    from apps.wechat_ai_customer_service.workflows.customer_service_brain_contract import normalize_brain_plan
    parts = ["第一条完整的句子。", "第二条完整的句子。", "第三条完整的句子。", "不保证一定能通过审核。"]
    assert normalize_brain_plan({"reply_segments": parts}, preserve_all_segments=True)["reply_segments"] == parts
    # Existing external consumers keep the old optional argument behavior.
    assert len(normalize_brain_plan({"reply_segments": parts})["reply_segments"]) == 3


@pytest.mark.parametrize("action", ["send_reply", "reply_then_handoff"])
def test_actual_brain_quality_keeps_limit_hard_even_if_general_verifier_disabled(action):
    from app.services.ai_adapter import RealOmniAutoAIEngineAdapter
    RealOmniAutoAIEngineAdapter._load_brain()  # Production import topology includes a top-level contract module.
    from customer_service_brain_contract import verify_brain_reply_quality
    settings = {"reply_sequence_version": 1, "reply_sequence_max_chars": 108,
                "reply_sequence_max_segments": 3, "quality_verifier_enabled": False}
    result = verify_brain_reply_quality({"recommended_action": action, "reply_segments": ["啊" * 109]},
                                       current_message="介绍一下", settings=settings)
    assert result["ok"] is False and result["errors"] == ["reply_sequence_rewrite_required"]
    assert verify_brain_reply_quality({"recommended_action": action, "reply_segments": ["啊" * 108]},
                                     current_message="介绍一下", settings=settings)["ok"]


def test_actual_provider_prompt_has_same_limit_and_legacy_default_is_unchanged():
    from app.services.ai_adapter import RealOmniAutoAIEngineAdapter
    RealOmniAutoAIEngineAdapter._load_brain()
    from apps.wechat_ai_customer_service.workflows.customer_service_brain import build_brain_prompt_pack, build_brain_user_content
    from app.services.reply_sequence_policy import sequence_policy
    current = build_brain_user_content(build_brain_prompt_pack(settings=sequence_policy(), brain_input={}))
    legacy = build_brain_user_content(build_brain_prompt_pack(settings={}, brain_input={}))
    assert "108个实际字符" in current and "96个中文字符" not in current
    assert "96个中文字符" in legacy
