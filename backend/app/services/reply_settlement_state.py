"""Read-only settlement facts shared by Worker recovery and release checks.

An unknown physical outcome stays unknown and must never authorize a resend.
Both an accepted original ACK and a fully recorded backend timeout can close
its execution responsibility. Neither path fabricates a client receipt.
"""
from sqlalchemy import and_, or_, select

from app.models.c3 import HandoffEvent, ReplyAction, SentAck
from app.models.task import Task, TaskEvent


def unsettled_send_condition():
    released_task = (
        Task.id == ReplyAction.claimed_task_id,
        Task.reply_action_id == ReplyAction.id,
        Task.worker_id == ReplyAction.claimed_by_worker_id,
        Task.status == "failed",
        Task.lease_owner_worker_id.is_(None),
        Task.lease_owner_client_instance_id.is_(None),
        Task.lease_expires_at.is_(None),
        Task.lease_last_renewed_at.is_(None),
    )
    accepted_ack = select(SentAck.id).join(Task, Task.id == SentAck.task_id).where(
        *released_task,
        SentAck.reply_action_id == ReplyAction.id,
        SentAck.worker_id == Task.worker_id,
        SentAck.send_token == ReplyAction.send_token,
        SentAck.send_result == "unknown",
    ).correlate(ReplyAction).exists()
    any_ack = select(SentAck.id).where(
        SentAck.reply_action_id == ReplyAction.id,
    ).correlate(ReplyAction).exists()
    failed_event = select(TaskEvent.id).where(
        TaskEvent.task_id == Task.id,
        TaskEvent.worker_id == Task.worker_id,
        TaskEvent.event_type == "failed",
        TaskEvent.from_status == "running",
        TaskEvent.to_status == "failed",
        TaskEvent.error_code == Task.error_code,
        TaskEvent.created_at >= ReplyAction.sending_claimed_at,
    ).correlate(Task, ReplyAction).exists()
    timeout_handoff = select(HandoffEvent.id).where(
        HandoffEvent.conversation_id == ReplyAction.conversation_id,
        HandoffEvent.batch_id == ReplyAction.batch_id,
        HandoffEvent.deleted_at.is_(None),
        HandoffEvent.handoff_reason_code == "SEND_ACK_TIMEOUT",
        HandoffEvent.ai_payload["reply_action_id"].as_string() == ReplyAction.id,
        HandoffEvent.ai_payload["send_result"].as_string() == "unknown",
        HandoffEvent.ai_payload["error_code"].as_string() == "SEND_ACK_TIMEOUT",
        HandoffEvent.created_at >= ReplyAction.sending_claimed_at,
    ).correlate(ReplyAction).exists()
    server_timeout = select(Task.id).where(
        *released_task,
        Task.deleted_at.is_(None),
        Task.task_type == "chat_reply",
        Task.failed_at.is_not(None),
        Task.lease_fencing_token > 0,
        Task.error_code.in_(("TASK_LEASE_EXPIRED", "SEND_ACK_TIMEOUT")),
        ReplyAction.error_code == "SEND_ACK_TIMEOUT",
        ReplyAction.send_token.is_not(None),
        ReplyAction.send_token != "",
        ReplyAction.sending_claimed_at.is_not(None),
        failed_event,
        timeout_handoff,
        # An existing but inconsistent ACK is corruption, not permission to
        # fall back to server history and overlook the ownership mismatch.
        ~any_ack,
    ).correlate(ReplyAction).exists()
    return or_(
        ReplyAction.status == "sending",
        and_(ReplyAction.status == "unknown_send_result", ~or_(accepted_ack, server_timeout)),
    )
