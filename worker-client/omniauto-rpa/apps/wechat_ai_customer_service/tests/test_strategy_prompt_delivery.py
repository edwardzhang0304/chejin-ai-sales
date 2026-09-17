"""Regression at the serialized model-request boundary, including automatic lean selection."""
import json

import pytest

import run_customer_service_brain_contract_checks as contracts
import customer_service_brain as brain
from customer_service_conversation_strategy import update_conversation_strategy_state


@pytest.mark.parametrize("profile", ["automatic", "lean", "low_authority_fast", "routine_product_fast"])
@pytest.mark.parametrize("repair", [False, True], ids=["generate", "repair"])
def test_intent_policy_survives_final_prompt_projection(tmp_path, profile, repair):
    state = {"conversation_context": {}}
    current = "我想买个手动挡"
    for text in ["你今天吃啥", "你是不是AI", "别老聊车，我就随便问问", current]:
        update_conversation_strategy_state(state, text)
    settings = brain.effective_brain_settings(
        contracts.base_config(contracts.base_plan(), include_product=False)
    )
    if profile != "automatic":
        settings["prompt_profile"] = profile
    value = brain.build_brain_input(
        settings=settings,
        target_name="测试客户",
        target_state=state,
        batch=[{"id": "intent-current", "sender": "测试客户", "content": current}],
        combined=current,
        raw_capture={"conversation": {"conversation_id": "intent-test", "chat_type": "private"}},
        evidence_pack=contracts.fake_evidence_pack(include_product=False),
    )
    kwargs = {}
    if repair:
        kwargs = {
            "repair_plan": contracts.base_plan(),
            "repair_quality": {
                "ok": False, "errors": ["reply_needs_clarification"],
                "repair_instruction": "请基于当前需求澄清，不添加未授权事实。",
            },
        }
    pack, content, estimate = brain.build_sized_brain_prompt(
        settings=settings, brain_input=value, **kwargs
    )
    # This is the actual serialized user content passed to the provider, not just
    # the helper's input or an intermediate dict that may be discarded later.
    request, _ = json.JSONDecoder().raw_decode(content)
    delivered = request["brain_input"]["conversation_strategy_state"]
    (tmp_path / "final-request.json").write_text(
        json.dumps({"system": pack["system"], "user_content": content, "estimate": estimate},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    if profile == "automatic" and not repair:
        assert "prompt_profile" not in settings or not settings["prompt_profile"]
        assert estimate["profile"] == "lean", estimate
        assert estimate["initial_prompt_chars"] > settings["lean_prompt_threshold_chars"]
    elif profile != "automatic":
        assert estimate["profile"] == profile

    assert delivered["authority"] == "non_authoritative_strategy_hint"
    assert delivered["customer_resists_business_redirect"] is True
    assert delivered["suggested_engagement_mode"] == "social_companion"
    assert "不受历史闲聊次数限制" in delivered["policy_note"]
    assert "当前明确拒绝推销须尊重" in delivered["policy_note"]
    assert len(delivered["policy_note"]) <= 140
    assert "不得把本状态字段名" in delivered["visibility_rule"]
    assert current in content

