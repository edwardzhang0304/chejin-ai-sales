"""Controlled provider outputs; real Brain parse/retry rules, no live model."""
import copy
import io
import json
from pathlib import Path

import pytest

import run_customer_service_brain_contract_checks as fixtures
from apps.wechat_ai_customer_service import llm_config
from llm_output_adapter import parse_complete_llm_json_response, parse_llm_json_object

brain = fixtures.brain_module
FULL_REPLY = "好的，丰田先排除。您还有其他品牌偏好吗？"


def plan():
    return {
        "can_answer": True, "understanding": {"excluded_brand": "丰田"},
        "answer_mode": "ask_clarifying_question", "facts_claimed": [],
        "reply_segments": [FULL_REPLY], "recommended_action": "send_reply",
        "confidence": 0.9, "risk": {"risk_level": "low", "needs_handoff": False},
    }


def response(*, cutoff=False, finish="stop", valid_json=False):
    raw = json.dumps(plan(), ensure_ascii=False)
    if cutoff:
        raw = raw[:raw.index("好的，丰田") + len("好的，丰田")]
        if valid_json:
            raw += '\"]}'
    return {"ok": True, "status": 200, "provider": "openai", "model": "isolated-test-model",
            "response_text": raw, "response_diagnostics": {"finish_reason": finish}}


@pytest.mark.parametrize("finish", ["length", "max_tokens", "model_context_window_exceeded"])
@pytest.mark.parametrize("cutoff", [False, True])
def test_token_limit_blocks_even_parseable_json(finish, cutoff):
    result = response(cutoff=cutoff, finish=finish, valid_json=True)
    assert parse_complete_llm_json_response(result) is None


def test_missing_finish_metadata_does_not_repair_cutoff_reply():
    result = response(cutoff=True)
    result.pop("response_diagnostics")
    assert parse_llm_json_object(result["response_text"])["reply_segments"] == ["好的，丰田"]
    assert parse_complete_llm_json_response(result) is None


@pytest.mark.parametrize("wrap", [lambda x: x, lambda x: f"```json\n{x}\n```", lambda x: f"结果：{x}\n结束"])
def test_complete_wrapped_reply_preserves_text(wrap):
    result = response()
    result["response_text"] = wrap(result["response_text"])
    assert parse_complete_llm_json_response(result)["reply_segments"] == [FULL_REPLY]


@pytest.fixture
def provider(monkeypatch):
    calls = []
    outputs = []
    def invoke(**kwargs):
        calls.append(copy.deepcopy(kwargs))
        assert outputs, "Unexpected model call: retry budget exceeded"
        return copy.deepcopy(outputs.pop(0))
    monkeypatch.setattr(brain, "call_llm_request_with_failover", invoke)
    monkeypatch.setattr(brain, "resolve_llm_api_key", lambda **_: "isolated-test-key")
    return outputs, calls


def run(entry, **settings):
    settings = {"provider": "openai", "model": "isolated-test-model", **settings}
    inputs = {"target": {"conversation_id": "completion-test", "target_name": "test-customer"},
              "current_message": {"clean_text": "不要丰田车"}, "evidence": {}}
    if entry == "generation":
        return brain.run_brain_llm(settings=settings, brain_input=inputs)
    return brain.run_brain_repair_llm(settings=settings, brain_input=inputs, plan=plan(),
                                     quality={"ok": False, "repair_instruction": "回答本轮客户需求。"})


@pytest.mark.parametrize("entry", ["generation", "repair"])
@pytest.mark.parametrize("finish,valid_json", [("length", False), ("length", True), ("stop", False)])
def test_real_brain_entry_retries_cutoff_once(provider, entry, finish, valid_json):
    outputs, calls = provider
    outputs.extend([response(cutoff=True, finish=finish, valid_json=valid_json), response()])
    result = run(entry)
    assert result["ok"] is True
    assert result["brain_plan"]["reply_segments"] == [FULL_REPLY]
    assert len(calls) == 2
    assert all("repair_malformed_brain_plan_json" not in json.dumps(c["messages"]) for c in calls)
    if entry == "repair":
        assert all(c["max_tokens"] == 16384 for c in calls)


@pytest.mark.parametrize("entry", ["generation", "repair"])
def test_repeated_cutoff_has_no_sendable_plan(provider, entry):
    outputs, calls = provider
    outputs.extend([response(cutoff=True, finish="length")] * 2)
    result = run(entry)
    assert result["ok"] is False
    assert not result.get("brain_plan")
    assert len(calls) == 2


@pytest.mark.parametrize("entry", ["generation", "repair"])
def test_success_does_not_add_a_model_call(provider, entry):
    outputs, calls = provider
    outputs.append(response())
    assert run(entry)["brain_plan"]["reply_segments"] == [FULL_REPLY]
    assert len(calls) == 1


def test_existing_retry_disable_is_respected(provider):
    outputs, calls = provider
    outputs.append(response(cutoff=True, finish="length"))
    result = run("repair", same_capture_brain_repair_parse_retry_enabled=False)
    assert result["ok"] is False and not result.get("brain_plan")
    assert len(calls) == 1


def test_structure_repair_cannot_restore_a_truncated_answer(provider):
    outputs, calls = provider
    result = brain.maybe_repair_brain_json_structure(
        settings={"provider": "openai"}, raw_text=response(cutoff=True)["response_text"], stage="test")
    assert result["ok"] is False and result["attempted"] is False
    assert calls == []
    outputs.append(response(cutoff=True, finish="length", valid_json=True))
    result = brain.maybe_repair_brain_json_structure(
        settings={"provider": "openai"}, raw_text="{bad", stage="test")
    assert result["ok"] is False and not result.get("brain_plan")
    assert len(calls) == 1


def test_transport_retry_still_rejects_cutoff(provider):
    outputs, calls = provider
    outputs.extend([{"ok": False, "status": 503, "error": "temporary upstream failure"},
                    response(cutoff=True, finish="length"),
                    response(cutoff=True, finish="length")])
    result = run("generation")
    assert result["ok"] is False and not result.get("brain_plan")
    # Existing policy permits one transport retry followed by one parse retry.
    assert len(calls) == 3


def test_explicit_repair_budget_is_respected(provider):
    outputs, calls = provider
    outputs.append(response())
    assert run("repair", quality_repair_max_tokens=20000)["ok"]
    assert calls[0]["max_tokens"] == 20000


def test_fast_profile_keeps_repair_budget():
    settings = brain.effective_brain_settings({})
    assert settings["quality_repair_max_tokens"] == 16384
    fast = brain.apply_low_authority_fast_brain_settings(settings, {"enabled": True})
    assert fast["quality_repair_max_tokens"] == 16384


@pytest.mark.parametrize("name", ["default.example.json", "jiangsu_chejin_xucong_live.example.json"])
def test_shipped_examples_keep_repair_budget(name):
    config = json.loads((Path(__file__).resolve().parents[1] / "configs" / name).read_text())
    assert config["customer_service_brain"]["quality_repair_max_tokens"] == 16384


@pytest.fixture
def wire_provider(monkeypatch):
    """Replace only HTTP I/O; keep decoding, diagnostics, failover and Brain."""
    calls, bodies = [], []
    def open_response(request, **_):
        calls.append(json.loads(request.data))
        assert bodies, "Unexpected HTTP call: retry budget exceeded"
        response = io.BytesIO(json.dumps(bodies.pop(0)).encode())
        response.status = 200
        response.headers = {}
        return response
    monkeypatch.setattr(llm_config, "llm_urlopen", open_response)
    monkeypatch.setattr(brain, "resolve_llm_api_key", lambda **_: "isolated-wire-key")
    return bodies, calls


def wire_body(provider, finish, reply=FULL_REPLY):
    candidate = plan()
    candidate["reply_segments"] = [reply]
    content = json.dumps(candidate, ensure_ascii=False)
    if provider == "anthropic":
        return {"type": "message", "role": "assistant", "stop_reason": finish,
                "content": [{"type": "text", "text": content}]}
    return {"choices": [{"message": {"role": "assistant", "content": content},
                         "finish_reason": finish}]}


@pytest.mark.parametrize("entry", ["generation", "repair"])
@pytest.mark.parametrize("provider_name,finish,truncated", [
    ("anthropic", "model_context_window_exceeded", True),
    ("anthropic", "max_tokens", True),
    ("anthropic", "end_turn", False),
    ("openai", "length", True),
    ("openai", "stop", False),
])
def test_wire_stop_reason_reaches_real_brain(wire_provider, entry, provider_name, finish, truncated):
    bodies, calls = wire_provider
    # Closed JSON cannot hide a provider-reported truncation.
    body = wire_body(provider_name, finish, "好的，丰田" if truncated else FULL_REPLY)
    bodies.extend([body] * (2 if truncated else 1))
    result = run(entry, provider=provider_name, base_url="http://127.0.0.1:9/v1")
    assert result["ok"] is (not truncated), result
    assert result["response_diagnostics"]["finish_reason"] == finish
    if truncated:
        assert not result.get("brain_plan")
    else:
        assert result["brain_plan"]["reply_segments"] == [FULL_REPLY]
    assert len(calls) == (2 if truncated else 1)


@pytest.mark.parametrize("entry", ["generation", "repair"])
def test_wire_context_limit_retries_to_complete_reply(wire_provider, entry):
    bodies, calls = wire_provider
    bodies.extend([wire_body("anthropic", "model_context_window_exceeded", "好的，丰田"),
                   wire_body("anthropic", "end_turn")])
    result = run(entry, provider="anthropic", base_url="http://127.0.0.1:9/v1")
    assert result["ok"] is True
    assert result["brain_plan"]["reply_segments"] == [FULL_REPLY]
    assert len(calls) == 2
