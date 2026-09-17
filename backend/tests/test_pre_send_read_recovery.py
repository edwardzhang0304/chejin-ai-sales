"""HTTP/PG receipt contract checks; no desktop or Windows claim.

Initial work comes from real asynchronous ingest. Protocol evidence is explicit
test input here; separate Worker tests must prove its capture and persistence.
"""
from copy import deepcopy
from datetime import timedelta
import time

import pytest
from sqlalchemy import select

import test_c3_api as api
from test_lead_followup_eligibility import http_api, isolated_db
from test_pre_send_checkpoint_order import async_generation
from app.core.database import SessionLocal
from app.models.base import utcnow
from app.models.c3 import Conversation, ReplyAction, MessageBatch, SentAck, HandoffEvent
from app.models.task import Task, TaskEvent
from app.models.wechat import WechatSessionBinding
from app.models.worker import Worker
from app.services.wechat_service import _authorization_revision


def setup_receipt(http, monkeypatch, *, permit=False, claimed=False, stopped=True, capable=True):
    monkeypatch.setattr(api, "client", http)
    worker, binding = api._setup_bound_conversation()
    with SessionLocal() as db:
        db.get(Conversation, binding["conversation_id"]).status = "waiting_user_reply"
        db.commit()
    api._ingest(worker, binding["conversation_id"], "unsent-customer-question", "你好，想了解看车安排")
    for _ in range(200):
        with SessionLocal() as db:
            action = db.scalar(select(ReplyAction).where(ReplyAction.status == "queued"))
            if action:
                task = db.scalar(select(Task).where(Task.reply_action_id == action.id))
                ids = {"reply_action_id": action.id, "task_id": task.id,
                       "conversation_id": action.conversation_id, "flow_id": task.id,
                       "reply_text_hash": action.reply_text_hash,
                       "authorization_revision": _authorization_revision(db.scalar(select(WechatSessionBinding)))}
                break
        time.sleep(.02)
    else:
        raise AssertionError("automatic initial reply task missing")
    headers = {**api._worker_headers(worker), "X-Inflight-Flow-Id": ids["flow_id"]}
    heartbeat = http.post(f"/api/workers/{worker['id']}/heartbeat", headers=headers, json={
        "client_instance_id": "client-c3", "run_status": "running", "running_status": "idle",
        "rpa_component_status": "ready", "wechat_status": "logged_in",
        "local_lock_summary": {"capabilities": {"pre_send_read_recovery_version": 1} if capable else {}},
    })
    assert heartbeat.status_code == 200, heartbeat.text
    started = http.post(f"/api/workers/{worker['id']}/inflight-flow/start", headers=headers, json={
        "flow_id": ids["flow_id"], "flow_kind": "chat_reply", "conversation_id": ids["conversation_id"],
        "authorization_revision": ids["authorization_revision"],
    })
    assert started.status_code == 200, started.text
    claim = None
    if permit or claimed:
        response = http.post(f"/api/tasks/{ids['task_id']}/claim", headers=headers, json={
            "worker_id": worker["id"], "claim_source": "c2_conversation_flow", "conversation_id": ids["conversation_id"]})
        assert response.status_code == 200, response.text
        headers.update(api._task_lease_headers(worker, response))
    if permit:
        response = http.post(f"/api/reply-actions/{ids['reply_action_id']}/claim-send", headers=headers,
                             json={"task_id": ids["task_id"], "worker_id": worker["id"]})
        assert response.status_code == 200, response.text
        claim = response.json()["data"]
    if stopped:
        response = http.post(f"/api/workers/{worker['id']}/run-status", headers=headers,
                             json={"run_status": "faulted", "client_instance_id": "client-c3"})
        assert response.status_code == 200, response.text
    phase = {"source": "action_journal" if permit else "read_only_before_claim", "ok": True, "action_phase": "not_attempted"}
    first = {"stage": "before_input" if permit else "pre_send_refresh", "operation": "capture",
             "call_status": "failed", "attempt_id": "attempt-one", "error_code": "SEND_BASELINE_UNAVAILABLE",
             "failure_reason": "native capture raised", "physical_send_triggered": False,
             "action_phase": "not_attempted", "phase_proof": phase, "input_state": "unverified",
             "frame_id": None, "no_frame_reason": "native capture raised before producing image"}
    proof = {"version": 1, **ids, "first_failure": first,
             "recheck": {"budget_state": "consumed", "started": True,
                         "failure": {**deepcopy(first), "attempt_id": "attempt-two"}},
             "outcome": "exhausted", "terminal_phase_proof": phase, "input_state": "unverified"}
    body = {"error_code": "C2_REPLY_CONTEXT_RECOVERY_FAILED", "evidence": {"pre_send_read_failure": proof}}
    path = f"/api/tasks/{ids['task_id']}/fail"
    if permit:
        path = f"/api/reply-actions/{ids['reply_action_id']}/sent-ack"
        body.update({"worker_id": worker["id"], "client_instance_id": "client-c3", "task_id": ids["task_id"],
                     "send_token": claim["send_token"], "reply_text_hash": ids["reply_text_hash"],
                     "send_result": "failed", "action_phase": "not_attempted"})
    return worker, ids, path, headers, body


@pytest.mark.parametrize("permit,claimed", [(False, False), (False, True), (True, True)])
def test_no_image_failure_settles_once_without_handoff(http_api, monkeypatch, async_generation, permit, claimed):
    worker, ids, path, headers, body = setup_receipt(http_api, monkeypatch, permit=permit, claimed=claimed)
    if claimed:
        with SessionLocal() as db:
            db.get(Task, ids["task_id"]).lease_expires_at = utcnow() - timedelta(seconds=10)
            db.commit()
    response = http_api.post(path, headers=headers, json=body)
    assert response.status_code == 200, response.text
    finish = http_api.post(f"/api/workers/{worker['id']}/inflight-flow/finish", headers=headers,
                          json={"flow_id": ids["flow_id"], "terminal_kind": "task_terminal", "conversation_id": ids["conversation_id"]})
    assert finish.status_code == 200, finish.text
    duplicate = http_api.post(path, headers=headers, json=body)
    assert duplicate.status_code == 200, duplicate.text
    assert duplicate.json()["data"]["duplicated"] is True
    with SessionLocal() as db:
        action = db.get(ReplyAction, ids["reply_action_id"])
        assert action.status == "failed" and not action.current
        assert db.get(Task, ids["task_id"]).status == "failed"
        assert not db.get(MessageBatch, action.batch_id).active
        assert db.query(HandoffEvent).count() == 0
        assert db.query(SentAck).count() == int(permit)
        assert db.query(TaskEvent).filter_by(task_id=ids["task_id"], event_type="failed").count() == 1
        pending = db.scalar(select(WechatSessionBinding)).last_scan_snapshot["pre_send_read_pending"]
        assert pending["message_event_ids"] and pending["status"] == "pending"
        assert db.get(Worker, worker["id"]).run_status == "faulted"
        assert not db.get(Worker, worker["id"]).inflight_flow_state
    assert async_generation["counts"]["generated"] == 1


@pytest.mark.parametrize("bad", ["physical", "phase", "same_attempt", "unstarted_failure", "wrong_action", "capability", "running"])
def test_invalid_new_evidence_never_falls_back_to_handoff(http_api, monkeypatch, async_generation, bad):
    worker, ids, path, headers, body = setup_receipt(http_api, monkeypatch, capable=bad != "capability", stopped=bad != "running")
    proof = body["evidence"]["pre_send_read_failure"]
    if bad == "physical": proof["first_failure"]["physical_send_triggered"] = True
    if bad == "phase": proof["terminal_phase_proof"]["action_phase"] = "trigger_attempted"
    if bad == "same_attempt": proof["recheck"]["failure"]["attempt_id"] = "attempt-one"
    if bad == "unstarted_failure": proof["recheck"]["started"] = False
    if bad == "wrong_action": proof["reply_action_id"] = "foreign-action"
    response = http_api.post(path, headers=headers, json=body)
    assert response.status_code == 409, response.text
    with SessionLocal() as db:
        assert db.get(Task, ids["task_id"]).status == "pending"
        assert db.get(ReplyAction, ids["reply_action_id"]).status == "queued"
        assert db.query(HandoffEvent).count() == db.query(SentAck).count() == 0
        assert "pre_send_read_pending" not in db.scalar(select(WechatSessionBinding)).last_scan_snapshot
