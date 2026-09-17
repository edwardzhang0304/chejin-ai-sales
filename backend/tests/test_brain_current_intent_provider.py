"""Synthetic role-labelled contexts through real Brain/Guard/HTTP Provider.

No decision/evidence builder/Guard is mocked. These varied contexts complement
the unchanged architect incident request and the Worker/DB recovery flow.
"""
from dataclasses import asdict
import json

import pytest

from app.services.ai_adapter import RealOmniAutoAIEngineAdapter
from app.services.knowledge_management_service import build_retrieval_index
from test_brain_current_intent_scope import _digest
from test_c3_brain_context_bridge import _snapshot, _current_batch
from test_partial_reply_recovery_provider import controlled_recovery_provider
from apps.wechat_ai_customer_service.workflows.chejin_brain_context_bridge import prior_messages_sha256


def _request(history, current):
    snapshot = _snapshot()
    snapshot["prior_messages"][-1]["content"] = history
    snapshot["prior_messages_sha256"] = prior_messages_sha256(snapshot["prior_messages"])
    messages = _current_batch()
    messages[0]["content"] = current
    index = build_retrieval_index([])
    return {"message_batch": {"trigger_type": "customer_message", "messages": messages},
            "conversation_context": {"conversation_id": snapshot["conversation_id"],
                "remark_code": "CJSCOPET", "brain_context_snapshot": snapshot,
                "knowledge_release_snapshot": {"release_id": "scope-test-release", "version": "KR-TEST",
                    "items": [], "snapshot_sha256": _digest([]), "retrieval_index": index,
                    "retrieval_index_sha256": _digest(index)}}}


def _run(monkeypatch, tmp_path, history, current, answer, *, review_verdict="pass"):
    requests = []
    try:
        with controlled_recovery_provider(monkeypatch, answer, requests, review_verdict=review_verdict):
            result = RealOmniAutoAIEngineAdapter().generate_reply_decision(**_request(history, current))
            (tmp_path / "decision.json").write_text(json.dumps(asdict(result), ensure_ascii=False, indent=2, default=str))
            return result, requests
    finally:
        (tmp_path / "requests.json").write_text(json.dumps(requests, ensure_ascii=False, indent=2))


@pytest.mark.parametrize("history", ["车辆资料需要核实。", "我们会确认一下。", "可向顾问了解。"])
@pytest.mark.parametrize("current,answer,fast", [
    ("在吗", "抱歉让您久等了，刚才的消息我看到了，您可以接着说。", True),
    ("买二手车需要注意哪些事情？", "您可以先说说平时的用车需求和预算，再逐步了解车辆资料。", False),
])
def test_both_brain_paths_keep_history_without_current_handoff(monkeypatch, tmp_path, history, current, answer, fast):
    result, requests = _run(monkeypatch, tmp_path, history, current, answer)
    assert result.decision == "send_reply" and result.reply_text == answer, result
    brain = result.raw_payload["omniauto_brain_result"]
    assert brain["low_authority_fast_profile"]["enabled"] is fast
    prompt = next(p for p in requests if p["model"] == "recovery-author")
    # Still passed to the model as history, never removed to make safety pass.
    assert history in json.dumps(prompt, ensure_ascii=False)
    user = next(m["content"] for m in prompt["messages"] if m["role"] == "user")
    payload, _ = json.JSONDecoder().raw_decode(user.lstrip())
    safety = payload["brain_input"]["safety"]
    if fast:
        # The fast prompt deliberately projects only these two safety flags.
        assert safety == {"must_handoff": False, "allowed_auto_reply": True}
    else:
        assert safety["soft_advisory_guard"] is True
        assert "handoff_intent_detected" not in safety["reasons"]


@pytest.mark.parametrize("current", ["请转人工处理", "我要找人工顾问", "合同盖章和付款怎么安排"])
def test_current_request_still_reaches_original_guard(monkeypatch, tmp_path, current):
    result, _ = _run(monkeypatch, tmp_path, "我们可以继续了解购车需求。", current,
                     "可以先说说您的需求。")
    # An innocent controlled author cannot erase the current hard/review input.
    assert result.decision != "send_reply", result
    assert result.raw_payload["omniauto_brain_result"]["guard"]["reason"] == "existing_safety_requires_handoff"


def test_semantic_rejection_still_blocks_unapproved_commitment(monkeypatch, tmp_path):
    result, _ = _run(monkeypatch, tmp_path, "车辆资料需要核实。", "买二手车需要注意哪些事情？",
                     "我保证给您最低价，合同可以无条件退款。", review_verdict="block")
    assert result.decision != "send_reply", result
