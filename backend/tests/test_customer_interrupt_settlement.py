"""Receipt API regressions: business cancellation, idempotency and safety gates."""
import sys
from pathlib import Path

import pytest
import test_c3_api as base

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "worker-client/omniauto-rpa/apps/wechat_ai_customer_service/tests"))
from test_send_interruption import receipt

setup_function = base.setup_function


def claimed_reply():
    worker, binding = base._setup_bound_conversation()
    with base.SessionLocal() as db:
        db.get(base.Conversation, binding["conversation_id"]).status = "waiting_user_reply"
        db.commit()
    event = base._ingest(worker, binding["conversation_id"], "interrupt-001", "想了解 15 万 SUV")
    generated = base._generate(base._collect(binding["conversation_id"], event)["batch_id"])
    task_id, action_id = generated["task_id"], generated["reply_action_id"]
    claim = base.client.post(f"/api/tasks/{task_id}/claim", json={
        "worker_id": worker["id"], "current_step": "chat_reply_claimed",
        "claim_source": "c2_conversation_flow", "conversation_id": binding["conversation_id"],
    }, headers=base._worker_headers(worker))
    assert claim.status_code == 200, claim.text
    send = base.client.post(f"/api/reply-actions/{action_id}/claim-send",
        json={"task_id": task_id, "worker_id": worker["id"]}, headers=base._task_lease_headers(worker, claim))
    assert send.status_code == 200, send.text
    send = send.json()["data"]
    proof = receipt(); guard = proof["evidence"]["guard"]
    guard["confirmed_target"] = binding["remark_code"]
    guard["visual"]["context_check"]["snapshot"]["validation"]["confirmed_target"] = binding["remark_code"]
    payload = {key: proof[key] for key in ("send_result", "action_phase", "error_code", "evidence")}
    payload.update(send_token=send["send_token"], task_id=task_id, worker_id=worker["id"],
                   client_instance_id="client-c3", reply_text_hash=send["reply_text_hash"])
    return worker, binding, action_id, task_id, payload


def ack(worker, action_id, payload):
    response = base.client.post(f"/api/reply-actions/{action_id}/sent-ack", json=payload, headers=base._worker_headers(worker))
    assert response.status_code == 200, response.text
    return response.json()["data"]


@pytest.mark.parametrize("mode", ["legacy", "clear_once", "before_input"])
def test_customer_interruption_cancels_and_duplicate_does_not_override_newer_state(mode):
    worker, binding, action_id, task_id, payload = claimed_reply()
    evidence = payload['evidence']
    visual = evidence['guard']['visual']
    if mode == 'clear_once':
        visual['draft_clear'].update(cleared=False, clear_attempted=True,
            method='select_all_backspace', reason='confirmed_program_draft_clear_requested',
            input_region={'has_visible_text': True})
    elif mode == 'before_input':
        check = visual['context_check']
        snapshot = check.pop('snapshot')
        snapshot.update(input_region={'has_visible_text': True}, screenshot_path='/controlled/current-frame.png')
        check['expected_context_guard'] = evidence['guard']['send_baseline']['send_context_guard']
        payload['evidence'] = {'state': 'send_context_changed_before_input',
            'guard': {**snapshot['validation'], 'screenshot_path': snapshot['screenshot_path']},
            'send_baseline': snapshot, 'context_validation': check,
            'action_journal': {'ok': True, 'action_phase': 'not_attempted'}}
    result = ack(worker, action_id, payload)
    assert result["task"]["status"] == "cancelled"
    with base.SessionLocal() as db:
        action = db.get(base.ReplyAction, action_id)
        conversation = db.get(base.Conversation, binding["conversation_id"])
        session = db.query(base.WechatSessionBinding).filter_by(conversation_id=action.conversation_id).one()
        assert action.status == "superseded" and not action.current
        assert conversation.status == "waiting_user_reply" and session.next_read_due_at
        assert db.query(base.HandoffEvent).count() == 0
        # A later human takeover is an independent precondition for replay.
        conversation.status = "waiting_sales_reply"
        conversation.ai_enabled = False
        db.commit()
    ack(worker, action_id, payload)
    with base.SessionLocal() as db:
        assert db.get(base.Conversation, binding["conversation_id"]).status == "waiting_sales_reply"
        assert db.query(base.SentAck).filter_by(reply_action_id=action_id).count() == 1


@pytest.mark.parametrize("invalid", ["sales", "cleanup_failed", "unknown", "wrong_target", "missing_proof"])
def test_uncertain_or_sales_interrupt_does_not_auto_continue(invalid):
    worker, binding, action_id, task_id, payload = claimed_reply()
    visual = payload["evidence"]["guard"]["visual"]
    if invalid == "sales":
        visual["context_check"]["snapshot"]["send_context_guard"]["sequence"][-1]["sender_role"] = "self"
    elif invalid == "cleanup_failed":
        visual["draft_clear"]["cleared"] = False
    elif invalid == "unknown":
        payload.update(send_result="unknown", action_phase="trigger_attempted")
    elif invalid == "wrong_target":
        payload["evidence"]["guard"]["confirmed_target"] = "OTHER"
    else:
        payload["evidence"] = {}
    result = ack(worker, action_id, payload)
    assert result["task"]["status"] != "cancelled"
    with base.SessionLocal() as db:
        assert db.get(base.ReplyAction, action_id).status != "superseded"
        assert db.query(base.HandoffEvent).count() == 1
        assert db.get(base.Conversation, binding["conversation_id"]).status == "waiting_sales_reply"


@pytest.mark.parametrize("changed", ["ai_disabled", "closed", "binding_disabled", "human_takeover"])
def test_safe_interruption_cannot_restore_revoked_listening(changed):
    worker, binding, action_id, task_id, payload = claimed_reply()
    with base.SessionLocal() as db:
        action = db.get(base.ReplyAction, action_id)
        conversation = db.get(base.Conversation, binding["conversation_id"])
        session = db.query(base.WechatSessionBinding).filter_by(conversation_id=action.conversation_id).one()
        if changed == "ai_disabled": conversation.ai_enabled = False
        elif changed == "closed": conversation.status = "closed"
        elif changed == "binding_disabled": session.allow_listening = False
        else: conversation.status = "waiting_sales_reply"
        original_status = conversation.status
        session.next_read_due_at = None
        db.commit()
    ack(worker, action_id, payload)
    with base.SessionLocal() as db:
        conversation = db.get(base.Conversation, binding["conversation_id"])
        action = db.get(base.ReplyAction, action_id)
        assert conversation.status == original_status
        assert db.query(base.WechatSessionBinding).filter_by(conversation_id=action.conversation_id).one().next_read_due_at is None
        assert db.query(base.HandoffEvent).count() == 0
