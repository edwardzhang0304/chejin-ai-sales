"""Formal Brain generation/repair and Guard, replacing only external model IO.

The evidence pack is a synthetic product fixture. These tests do not exercise
live model behavior, knowledge ingestion, or Windows sending.
"""
import json
import pytest

import run_customer_service_brain_contract_checks as fixtures
from test_cargo_claim_scope import QUESTION, ERROR, cargo_pack, plan_for


SAFE = "不能保证能装下梯子，建议带上梯子核对后备箱开口和实际尺寸。"
UNSAFE = "不能保证，但肯定能装下梯子。"
TURN = QUESTION + "我工作要用，想先确认后备箱实际装载情况再决定。"


def run_formal_brain(monkeypatch, drafts):
    brain = fixtures.brain_module
    calls = []
    def model_response(**kwargs):
        index = len(calls)
        calls.append(kwargs)
        assert index < len(drafts), "Unexpected extra model call"
        plan = plan_for(drafts[index])
        plan["facts_claimed"] = [{"fact_type": "product_name", "value": "秦PLUS",
                                  "source_level": "product_master",
                                  "source_id": "chejin_qinplus_2022_dmi55"}]
        return {"ok": True, "status": 200, "provider": "test-transport",
                "response_text": json.dumps(plan, ensure_ascii=False)}
    monkeypatch.setattr(brain, "call_llm_request_with_failover", model_response)
    monkeypatch.setattr(brain, "resolve_llm_api_key", lambda **_: "isolated-test-credential")
    config = fixtures.base_config(plan_for(drafts[0]))
    config["customer_service_brain"].update(
        mode="brain_first", provider="openai", model="isolated-test-model",
        semantic_reviewer_provider="manual_json",
    )
    pack = cargo_pack()
    pack["current_message"] = TURN
    pack["current_batch"][0]["content"] = TURN
    with fixtures.patched_evidence_pack(pack):
        result = brain.maybe_run_customer_service_brain(
            config=config, target_name="许聪",
            target_state={"conversation_context": {"last_product_id": "chejin_qinplus_2022_dmi55"}},
            batch=[{"id": "msg1", "sender": "customer", "content": TURN}],
            combined=TURN, decision=fixtures.ReplyDecision("", "", False, False, ""),
            reply_text="", intent_assist={}, rag_reply={}, llm_reply={},
            product_knowledge={}, data_capture={},
            raw_capture={"conversation": {"conversation_id": "cargo-test", "chat_type": "private"}},
            customer_profile=None,
        )
    return result, calls


@pytest.mark.parametrize('reply', [SAFE, '不能保证一定能装下梯子，需要实车测量。',
                                 '无法保证肯定能装下梯子，建议实车测量。'])
def test_formal_generation_accepts_negation_without_repair(monkeypatch, reply):
    result, calls = run_formal_brain(monkeypatch, [reply])
    assert result["adoptable"] is True, result
    assert result["reply_text"] == reply
    assert result["guard"]["allowed"] is True
    assert [call["progress_stage"] for call in calls] == ["brain_llm"]


def test_formal_repair_receives_scope_error_and_produces_own_safe_reply(monkeypatch):
    result, calls = run_formal_brain(monkeypatch, [UNSAFE, SAFE])
    assert result["adoptable"] is True, result
    assert result["reply_text"] == SAFE
    assert result["guard"]["allowed"] is True
    assert [call["progress_stage"] for call in calls] == ["brain_llm", "brain_quality_repair"]
    assert ERROR in json.dumps(calls[1]["messages"], ensure_ascii=False)
    assert result["visible_reply_owner"] == "brain_repair"


def test_formal_repair_cannot_repeat_an_unsupported_affirmation(monkeypatch):
    result, calls = run_formal_brain(monkeypatch, [UNSAFE, UNSAFE])
    assert result["adoptable"] is False, result
    assert not result.get("reply_text"), result
    assert ERROR in result["repaired_quality_verification"]["errors"]
    assert [call["progress_stage"] for call in calls] == ["brain_llm", "brain_quality_repair"]


def test_runtime_emits_dependency_evidence_from_actual_full_input(monkeypatch):
    from apps.wechat_ai_customer_service.adapters.image_order_dependencies import input_dependency_evidence
    result,calls=run_formal_brain(monkeypatch,[SAFE])
    proof=result['brain_input_summary']['input_dependency_evidence']
    assert proof==input_dependency_evidence(result['brain_input'])
    assert proof['complete'] and proof['image_dependency'] is False
    assert proof['message_ids']==['msg1'] and proof['conversation_id']=='cargo-test'
    assert len(calls)==1
