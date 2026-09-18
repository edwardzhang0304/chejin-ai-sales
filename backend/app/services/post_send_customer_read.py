"""Settle the send first; observations only schedule an authorized fresh read."""
from app.contracts.shared_rules import shared_adapter
from app.models.base import utcnow
from app.models.c3 import MessageBatch
from app.models.worker import Worker


def settle(db, *, action, payload, binding, conversation, followup_cancelled):
    from app.services.c3_service import open_handoff_events_for_conversation
    from app.services.reply_sequence_service import (
        cancel_remaining, interrupt_sequence, supports_reply_sequence,
    )

    if payload.send_result != "sent" or payload.action_phase != "confirmed":
        return False
    proof = shared_adapter("text_correspondence").confirmed_post_send_customer_suffix(
        payload.evidence, target=binding.remark_code, text=action.reply_text,
    )
    if not proof:
        return False
    # The observed customer suffix is not ingested here and cannot grant new
    # send permission. It only invalidates unsent segments of this generation.
    cancel_remaining(db, action, "CUSTOMER_MESSAGE_AFTER_SEND")
    batch = db.get(MessageBatch, action.batch_id)
    if batch and action.segment_count > 1:
        batch.active = False
        batch.status = "superseded"
        batch.error_code = "MESSAGE_BATCH_SUPERSEDED"
    allowed = (
        not followup_cancelled and conversation.ai_enabled
        and conversation.status in {"ai_active", "waiting_user_reply", "recalled_waiting_user"}
        and binding.bind_status == "bound" and binding.allow_listening
        and binding.listen_status in {"listening", "degraded"}
        and not open_handoff_events_for_conversation(db, action.conversation_id, for_update=True)
    )
    if not allowed:
        return True
    worker = db.get(Worker, payload.worker_id)
    flow = (worker.inflight_flow_state or {}) if worker else {}
    if (action.segment_count > 1 and supports_reply_sequence(worker)
            and binding.worker_id == worker.id
            and flow.get("flow_id") and flow.get("status") in {"active", "draining"}
            and flow.get("conversation_id") == action.conversation_id):
        interrupt_sequence(db, worker=worker, batch_id=action.batch_id,
                           flow_id=flow["flow_id"], evidence={"post_send_customer_suffix": proof})
    conversation.next_recall_at = None
    binding.next_read_due_at = utcnow()
    binding.no_change_read_count = 0
    return True


def attach_confirmed_prefix(db, *, old_batch_id, new_batch_id, worker_id,
                            flow_id, conversation_id, visible_message_orders):
    """Use original confirmed receipts in the existing Brain recovery context."""
    from sqlalchemy import select
    from app.models.c3 import ReplyAction, SentAck
    from app.models.wechat import MessageEvent

    old = db.get(MessageBatch, old_batch_id)
    new = db.get(MessageBatch, new_batch_id)
    interruption = (old.ai_response_snapshot or {}).get("reply_sequence_interrupt") if old else None
    if (not old or not new or old.id == new.id or new.status != "collecting"
            or old.conversation_id != conversation_id or new.conversation_id != conversation_id
            or not interruption or not interruption.get("post_send_customer_suffix")
            or interruption.get("worker_id") != worker_id or interruption.get("flow_id") != flow_id):
        return
    prefix = list(db.scalars(select(ReplyAction).join(SentAck, SentAck.reply_action_id == ReplyAction.id).where(
        ReplyAction.batch_id == old.id, ReplyAction.generation_no == old.generation_no,
        ReplyAction.status == "sent", ReplyAction.deleted_at.is_(None),
        SentAck.send_result == "sent", SentAck.action_phase == "confirmed",
        SentAck.reply_text_hash == ReplyAction.reply_text_hash,
        SentAck.task_id == ReplyAction.claimed_task_id, SentAck.worker_id == worker_id,
    ).order_by(ReplyAction.segment_index)))
    if not prefix or [a.segment_index for a in prefix] != list(range(1, len(prefix) + 1)):
        return
    visible = list(db.scalars(select(MessageEvent).where(
        MessageEvent.conversation_id == conversation_id, MessageEvent.id.in_(visible_message_orders),
        MessageEvent.sender_role == "self",
    )))
    confirmed = []
    for action in prefix:
        matches = [event for event in visible if (event.raw_payload or {}).get("sender_source") == "ai"
                   and (event.raw_payload or {}).get("ai_reply_action_id") == action.id
                   and (event.raw_payload or {}).get("ai_reply_text_hash") == action.reply_text_hash]
        if len(matches) != 1:
            return
        confirmed.append({"reply_action_id": action.id, "message_event_id": matches[0].id,
                          "text": action.reply_text})
    new.ai_request_snapshot = {**(new.ai_request_snapshot or {}), "partial_reply_recovery": {
        "origin_batch_id": old.id, "origin_reply_action_id": prefix[-1].id,
        "confirmed_prefix": confirmed,
    }}
