"""PostgreSQL collector boundary tests with explicit persisted-state fixtures.

Initial replies/first sent ACK come through HTTP. The already-settled failure
and authoritative read are component preconditions, not claimed as Worker I/O.
test_partial_reply_recovery_worker separately proves those production paths.
"""
from datetime import timedelta

import pytest
from sqlalchemy import select

from test_lead_followup_eligibility import http_api, isolated_db
from test_pre_send_checkpoint_order import async_generation
from test_reply_sequence_http import generated_group, first_claim, ack
from app.core.database import SessionLocal
from app.models.base import utcnow
from app.models.c3 import Conversation, MessageBatch, ReplyAction, SentAck
from app.models.wechat import MessageEvent, WechatSessionBinding
from app.models.worker import Worker
from app.services import pre_send_read_recovery as recovery


@pytest.mark.parametrize("case", ["prefix", "late_prefix", "human_same_text", "unknown_ack",
    "missing_ack", "wrong_hash", "unseen_later_send", "new_question", "other_flow", "question_offscreen"])
def test_partial_prefix_is_not_a_blanket_self_message_exception(http_api, monkeypatch, async_generation, case):
    worker_data, binding_data, batch_id, action_ids = generated_group(http_api, monkeypatch)
    ack(http_api, worker_data, first_claim(http_api, worker_data, binding_data, action_ids[0]))
    with SessionLocal() as db:
        worker = db.get(Worker, worker_data["id"])
        binding = db.scalar(select(WechatSessionBinding).where(
            WechatSessionBinding.conversation_id == binding_data["conversation_id"]))
        conv = db.get(Conversation, binding.conversation_id)
        batch = db.get(MessageBatch, batch_id)
        first, origin = [db.get(ReplyAction, identity) for identity in action_ids[:2]]
        batch.active = False; batch.status = "cancelled"
        origin.status = "failed"; origin.current = False
        origin.ai_payload = {recovery.RECEIPT: {"component_fixture": "settled original failure"}}
        origin.updated_at = utcnow() - timedelta(seconds=2)
        conv.status = "waiting_user_reply"
        worker.run_status = "running"
        worker.inflight_flow_state = {"flow_kind": "c2_read", "flow_id": "fresh-read", "status": "active",
                                     "conversation_id": binding.conversation_id if case != "other_flow" else "other"}
        snapshot_outbound = conv.last_outbound_at.isoformat()
        binding.last_scan_snapshot = {recovery.PENDING: {
            "status": "pending", "worker_id": worker.id, "binding_id": binding.id,
            "reply_action_id": origin.id, "batch_id": batch.id, "message_event_ids": batch.message_event_ids,
            "last_outbound_at": snapshot_outbound, "created_at": utcnow().isoformat()}}
        question_id = batch.message_event_ids[-1]

        def event(key, role, content, raw=None):
            row = MessageEvent(conversation_id=binding.conversation_id, binding_id=binding.id,
                worker_id=worker.id, rpa_session_key=binding.rpa_session_key,
                read_run_id="fresh-read", dedupe_key=key, source_message_key=key,
                sender_role=role, message_type="text", content=content, raw_payload=raw or {})
            db.add(row); db.flush(); return row

        prefix = event("prefix", "self", first.reply_text, {"sender_source": "ai",
            "ai_reply_action_id": first.id, "ai_reply_text_hash": first.reply_text_hash})
        visible = {question_id: 1, prefix.id: 2}
        if case == "late_prefix":
            conv.last_outbound_at = prefix.ingested_at
        elif case == "human_same_text":
            human = event("human", "self", first.reply_text, {"sender_source": "human"})
            visible[human.id] = 3
        elif case in {"unknown_ack", "missing_ack"}:
            receipt = db.scalar(select(SentAck).where(SentAck.reply_action_id == first.id))
            if case == "unknown_ack": receipt.send_result = "unknown"
            else: db.delete(receipt)
        elif case == "wrong_hash":
            prefix.raw_payload = {**prefix.raw_payload, "ai_reply_text_hash": "mismatch"}
        elif case == "unseen_later_send":
            # A later confirmed outbound cannot be erased by looking at an old
            # frame whose visible prefix still has the original AI receipt.
            other = db.get(ReplyAction, action_ids[2])
            db.add(SentAck(reply_action_id=other.id, task_id="component-later-task", worker_id=worker.id,
                send_token="component-later-token", send_result="sent", action_phase="confirmed"))
            conv.last_outbound_at = prefix.ingested_at
        elif case == "new_question":
            extra = event("question-two", "customer", "时间可以改成周末吗？")
            visible[extra.id] = 3
        elif case == "question_offscreen":
            visible.pop(question_id)
        db.flush()
        result = recovery.collect_after_read(db, worker=worker, binding=binding, conversation=conv,
            read_run_id="fresh-read", customer_tail_ids=[], visible_message_ids=visible)
        allowed = case in {"prefix", "late_prefix", "new_question"}
        assert bool(result) == allowed
        assert first.status == "sent" and origin.status == "failed"
        if allowed:
            new = db.get(MessageBatch, result["batch_id"])
            assert new.id != batch.id and question_id in new.message_event_ids
            assert new.ai_request_snapshot["partial_reply_recovery"]["confirmed_prefix"][0]["message_event_id"] == prefix.id
            if case == "new_question": assert extra.id in new.message_event_ids
        else:
            assert db.query(MessageBatch).count() == 1
