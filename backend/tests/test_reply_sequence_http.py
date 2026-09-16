"""Actual HTTP/PostgreSQL/automatic C3 generation; only the model is controlled.

These route tests deliberately submit protocol actions; they do not claim to
test Worker or Windows. Worker ownership is tested separately.
"""
from datetime import timedelta
import time

import pytest
from sqlalchemy import select

import test_c3_api as fixtures
from test_lead_followup_eligibility import http_api, isolated_db
from test_pre_send_checkpoint_order import async_generation
from app.core.database import SessionLocal
from app.models.c3 import MessageBatch, ReplyAction, SentAck, HandoffEvent
from app.models.task import Task
from app.models.worker import Worker
from app.services import c3_service
from app.services.ai_adapter import AIEngineDecision


PARTS = ["这台车的资料需要按实际检测结果核实，" * 3 + "我们会说明已知的情况。",
         "另一台车也需要核实使用情况，" * 4 + "不能保证没有未发现的问题。",
         "您可以先告诉我看车时间，" * 4 + "再由销售确认安排。"]


class SequenceModel:
    def generate_reply_decision(self, **kwargs):
        return AIEngineDecision(decision="send_reply", reply_text=" ".join(PARTS), guard_result="pass",
                                raw_payload={"omniauto_brain_result": {"brain_plan": {"reply_segments": PARTS}}})


def generated_group(http_api, monkeypatch, *, capable=True):
    monkeypatch.setattr(fixtures, "client", http_api)
    monkeypatch.setattr(c3_service, "get_ai_engine_adapter", SequenceModel)
    worker, binding = fixtures._setup_bound_conversation()
    if capable:
        with SessionLocal() as db:
            db.get(Worker, worker["id"]).local_lock_summary = {"capabilities": {"reply_sequence_version": 1}}
            db.commit()
    fixtures._ingest(worker, binding["conversation_id"], "sequence-question", "请详细介绍看车安排")
    for _ in range(100):
        with SessionLocal() as db:
            actions = list(db.scalars(select(ReplyAction).order_by(ReplyAction.segment_index)))
            if actions:
                assert len(actions) == 3, "automatic reply task missing"
                return worker, binding, actions[0].batch_id, [action.id for action in actions]
        time.sleep(.02)
    raise AssertionError("automatic reply task missing")


def status(http, worker, batch_id):
    response = http.get(f"/api/workers/{worker['id']}/wechat/message-batches/{batch_id}", headers=fixtures._worker_headers(worker))
    assert response.status_code == 200, response.text
    return response.json()["data"]


def first_claim(http, worker, binding, action_id):
    with SessionLocal() as db:
        task_id = db.scalar(select(Task.id).where(Task.reply_action_id == action_id))
    claimed = http.post(f"/api/tasks/{task_id}/claim", json={"worker_id": worker["id"], "claim_source": "c2_conversation_flow",
                         "conversation_id": binding["conversation_id"]}, headers=fixtures._worker_headers(worker))
    assert claimed.status_code == 200, claimed.text
    response = http.post(f"/api/reply-actions/{action_id}/claim-send", json={"task_id": task_id, "worker_id": worker["id"]},
                         headers=fixtures._task_lease_headers(worker, claimed))
    assert response.status_code == 200, response.text
    return response.json()["data"]


def ack(http, worker, claim, *, result="sent", code=None):
    response = http.post(f"/api/reply-actions/{claim['reply_action_id']}/sent-ack", json={
        "worker_id": worker["id"], "client_instance_id": "client-c3", "task_id": claim["task_id"],
        "send_token": claim["send_token"], "reply_text_hash": claim["reply_text_hash"], "send_result": result,
        "action_phase": "confirmed" if result == "sent" else "trigger_attempted" if result == "unknown" else "not_attempted",
        "error_code": code}, headers=fixtures._worker_headers(worker))
    assert response.status_code == 200, response.text
    return response.json()["data"]


def test_automatic_group_and_duplicate_ack_advance_exactly_once(http_api, monkeypatch, async_generation):
    worker, binding, batch_id, ids = generated_group(http_api, monkeypatch)
    before = status(http_api, worker, batch_id)
    assert before["reply_sequence"] == {"batch_id": batch_id, "generation_no": 1, "segment_count": 3, "sent_count": 0, "terminal": False}
    with SessionLocal() as db:
        assert [db.scalar(select(Task.status).where(Task.reply_action_id == identity)) for identity in ids] == ["pending", "blocked", "blocked"]
    claim = first_claim(http_api, worker, binding, ids[0])
    assert len(claim["reply_text"]) <= 108
    ack(http_api, worker, claim)
    assert ack(http_api, worker, claim)["duplicated"] is True
    after = status(http_api, worker, batch_id)
    assert after["reply_action"]["id"] == ids[1]
    assert after["reply_sequence"]["sent_count"] == 1
    assert after["pre_send_fact_checkpoint_pending"] is True
    with SessionLocal() as db:
        assert len(list(db.scalars(select(SentAck)))) == 1
        assert db.scalar(select(Task.status).where(Task.reply_action_id == ids[2])) == "blocked"
    assert async_generation["counts"] == {"scheduled": 1, "executed": 1, "generated": 1}


@pytest.mark.parametrize("result,code", [("failed", "C3_SEND_CONTEXT_GUARD_INVALID"), ("unknown", "SEND_RESULT_UNKNOWN")])
def test_unsent_tail_cancelled_after_terminal_non_success(http_api, monkeypatch, async_generation, result, code):
    worker, binding, batch_id, ids = generated_group(http_api, monkeypatch)
    claim = first_claim(http_api, worker, binding, ids[0])
    ack(http_api, worker, claim, result=result, code=code)
    assert status(http_api, worker, batch_id)["reply_sequence"]["terminal"] is True
    with SessionLocal() as db:
        assert all(db.get(ReplyAction, identity).status in {"cancelled", "superseded"} for identity in ids[1:])


def test_old_worker_never_receives_partial_multi_reply(http_api, monkeypatch, async_generation):
    worker, binding, batch_id, ids = generated_group(http_api, monkeypatch, capable=False)
    response = status(http_api, worker, batch_id)
    assert response["error_code"] == "WORKER_REPLY_SEQUENCE_UNSUPPORTED"
    assert response["reply_sequence"]["terminal"] is True
    with SessionLocal() as db:
        assert all(db.get(ReplyAction, identity).status == "cancelled" for identity in ids)
        assert db.get(Worker, worker["id"]).run_status == "running"


def test_disabling_auto_generation_breaks_positive_before_any_test_claim(http_api, monkeypatch, async_generation):
    async_generation["suppress"] = True
    with pytest.raises(AssertionError, match="automatic reply task missing"):
        generated_group(http_api, monkeypatch)
    with SessionLocal() as db:
        assert not list(db.scalars(select(ReplyAction)))


@pytest.mark.parametrize("cause", ["expiry", "capability"])
def test_remaining_group_terminates_when_delivery_becomes_unavailable(http_api, monkeypatch, async_generation, cause):
    from app.models.base import utcnow
    worker, binding, batch_id, ids = generated_group(http_api, monkeypatch)
    claim = first_claim(http_api, worker, binding, ids[0])
    ack(http_api, worker, claim)
    with SessionLocal() as db:
        if cause == "expiry":
            for identity in ids[1:]:
                db.get(ReplyAction, identity).expire_at = utcnow() - timedelta(seconds=1)
        else:
            db.get(Worker, worker["id"]).local_lock_summary = {}
        db.commit()
    assert status(http_api, worker, batch_id)["reply_sequence"]["terminal"]
    with SessionLocal() as db:
        assert db.get(ReplyAction, ids[0]).status == "sent"
        assert all(db.get(ReplyAction, identity).status == "cancelled" for identity in ids[1:])
        assert len(list(db.scalars(select(SentAck)))) == 1


def test_cannot_skip_predecessor_or_fresh_read(http_api, monkeypatch, async_generation):
    worker, binding, batch_id, ids = generated_group(http_api, monkeypatch)
    with SessionLocal() as db:
        second = db.scalar(select(Task.id).where(Task.reply_action_id == ids[1]))
    blocked = http_api.post(f"/api/tasks/{second}/claim", json={"worker_id": worker["id"], "claim_source": "c2_conversation_flow",
                           "conversation_id": binding["conversation_id"]}, headers=fixtures._worker_headers(worker))
    assert blocked.status_code == 409
    ack(http_api, worker, first_claim(http_api, worker, binding, ids[0]))
    claimed = http_api.post(f"/api/tasks/{second}/claim", json={"worker_id": worker["id"], "claim_source": "c2_conversation_flow",
                           "conversation_id": binding["conversation_id"]}, headers=fixtures._worker_headers(worker))
    assert claimed.status_code == 200
    denied = http_api.post(f"/api/reply-actions/{ids[1]}/claim-send", json={"task_id": second, "worker_id": worker["id"]},
                          headers=fixtures._task_lease_headers(worker, claimed))
    assert denied.status_code == 409 and denied.json()["code"] == "REPLY_SEQUENCE_FRESH_READ_REQUIRED"


def test_disabling_ack_advance_breaks_the_positive(http_api, monkeypatch, async_generation):
    monkeypatch.setattr(c3_service, "advance_after_sent_ack", lambda *args, **kwargs: None)
    with pytest.raises(AssertionError):
        test_automatic_group_and_duplicate_ack_advance_exactly_once(http_api, monkeypatch, async_generation)


def test_unknown_timeout_cancels_the_remaining_group_without_resending(http_api, monkeypatch, async_generation):
    from app.models.base import utcnow
    worker, binding, batch_id, ids = generated_group(http_api, monkeypatch)
    first_claim(http_api, worker, binding, ids[0])
    with SessionLocal() as db:
        assert c3_service.recover_stale_sending_reply_action(db, reply_action_id=ids[0], now=utcnow()+timedelta(days=1))
        db.commit()
    assert status(http_api, worker, batch_id)["reply_sequence"]["terminal"]
    with SessionLocal() as db:
        assert db.get(ReplyAction, ids[0]).status == "unknown_send_result"
        assert all(db.get(ReplyAction, i).status == "cancelled" for i in ids[1:])
        assert not list(db.scalars(select(SentAck)))  # No fabricated physical receipt.


def test_interrupt_is_scoped_and_idempotent_without_creating_message_facts(http_api, monkeypatch, async_generation):
    from app.models.wechat import MessageEvent
    worker, binding, batch_id, ids = generated_group(http_api, monkeypatch)
    flow_id = "read-sequence-interrupt"
    headers = {**fixtures._worker_headers(worker), "X-Inflight-Flow-Id": flow_id}
    started = http_api.post(f"/api/workers/{worker['id']}/inflight-flow/start", headers=fixtures._worker_headers(worker),
                           json={"flow_id": flow_id, "flow_kind": "c2_read", "conversation_id": binding['conversation_id'], "unread_generation": 0})
    assert started.status_code == 200, started.text
    path = f"/api/workers/{worker['id']}/wechat/message-batches/{batch_id}/interrupt-reply-sequence"
    evidence = {"frame_id": "new-frame", "observation_ids": ["new-customer-image"]}
    rejected = http_api.post(path, headers={**headers, "X-Inflight-Flow-Id": "another-flow"}, json=evidence)
    assert rejected.status_code == 409 and rejected.json()["code"] == "WORKER_INFLIGHT_FLOW_MISMATCH"
    with SessionLocal() as db:
        previous_messages = list(db.scalars(select(MessageEvent.id)))
    for _ in range(2):
        accepted = http_api.post(path, headers=headers, json=evidence)
        assert accepted.status_code == 200, accepted.text
        assert accepted.json()["data"]["reply_sequence"]["terminal"]
    with SessionLocal() as db:
        assert list(db.scalars(select(MessageEvent.id))) == previous_messages
        assert len(list(db.scalars(select(ReplyAction)))) == 3
        assert all(db.get(ReplyAction, i).status == "superseded" for i in ids)
        assert not list(db.scalars(select(HandoffEvent)))
