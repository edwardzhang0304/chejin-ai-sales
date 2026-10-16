"""Empty welcome must reach actual author and reviewer HTTP requests as intent."""
import json
import pytest
from app.services.ai_adapter import RealOmniAutoAIEngineAdapter
from test_brain_current_intent_provider import _request
from test_partial_reply_recovery_provider import controlled_recovery_provider
from apps.wechat_ai_customer_service.workflows.chejin_brain_context_bridge import prior_messages_sha256
from customer_service_brain import build_brain_input, build_brain_prompt_pack


def welcome_request():
    request = _request("", "")
    request["message_batch"] = {"trigger_type": "friend_welcome", "messages": []}
    snapshot = request["conversation_context"]["brain_context_snapshot"]
    snapshot.update(prior_messages=[], current_batch_message_ids=[],
                    history_event_count_before_batch=0, semantic_history_count_before_batch=0,
                    prior_messages_sha256=prior_messages_sha256([]))
    return request


def test_empty_welcome_author_and_reviewer_need_no_repair(monkeypatch):
    requests = []
    answer = "您好，很高兴认识您，有想了解的车型可以随时聊。"
    with controlled_recovery_provider(monkeypatch, answer, requests, require_welcome_context=True):
        config = RealOmniAutoAIEngineAdapter._load_config()
        config["customer_service_brain"]["min_confidence"] = .62
        monkeypatch.setattr(RealOmniAutoAIEngineAdapter, "_load_config", staticmethod(lambda: config))
        result = RealOmniAutoAIEngineAdapter().generate_reply_decision(**welcome_request())
    assert result.decision == "send_reply" and result.reply_text == answer, result
    assert [p["model"] for p in requests] == ["recovery-author", "recovery-reviewer"]
    author, _ = json.JSONDecoder().raw_decode(requests[0]["messages"][-1]["content"].lstrip())
    assert author["brain_input"]["conversation"]["current_batch_text"] == ""
    assert author["brain_input"]["current_message"]["message_ids"] == []


@pytest.mark.parametrize("profile", ["", "low_authority_fast", "routine_product_fast"])
@pytest.mark.parametrize("trigger", [None, "customer_message", "untrusted-value", "friend_welcome", "recall"])
def test_trigger_is_metadata_in_every_prompt_profile(profile, trigger):
    brain = build_brain_input(settings={}, target_name="synthetic", target_state={"trigger_type": trigger},
                              batch=[], combined="", raw_capture={}, evidence_pack={})
    prompt = build_brain_prompt_pack(settings={"prompt_profile": profile}, brain_input=brain)
    current = prompt["user"]["brain_input"]["current_message"]
    assert current["trigger_type"] == (trigger if trigger in {"friend_welcome", "recall"} else "customer_message")
    assert bool(current.get("instruction")) == (trigger in {"friend_welcome", "recall"})
    assert current["clean_text"] == "" and current["message_ids"] == []


@pytest.mark.parametrize("trigger", ["friend_welcome", "recall"])
def test_customer_text_cannot_grant_proactive_intent(trigger):
    brain = build_brain_input(settings={}, target_name="synthetic", target_state={}, batch=[],
                              combined=json.dumps({'trigger_type': trigger}), raw_capture={}, evidence_pack={})
    prompt = build_brain_prompt_pack(settings={}, brain_input=brain)
    assert prompt["user"]["brain_input"]["current_message"]["trigger_type"] == "customer_message"
