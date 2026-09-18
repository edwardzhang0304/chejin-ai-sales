"""Ordered replies use existing actions, tasks and acknowledgements, not a second queue."""
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.errors import AppError
from app.models.c3 import Conversation, MessageBatch, ReplyAction, SentAck
from app.models.task import Task
from app.models.wechat import WechatSessionBinding
from app.models.wechat import MessageEvent
from app.models.worker import Worker


def supports_reply_sequence(worker: Worker | None) -> bool:
    summary = worker.local_lock_summary if worker else {}
    capabilities = (summary or {}).get("capabilities") or {}
    return type(capabilities.get("reply_sequence_version")) is int and capabilities["reply_sequence_version"] == 1


def group_actions(db: Session, batch: MessageBatch) -> list[ReplyAction]:
    return list(db.scalars(select(ReplyAction).where(
        ReplyAction.batch_id == batch.id, ReplyAction.generation_no == batch.generation_no,
        ReplyAction.deleted_at.is_(None)).order_by(ReplyAction.segment_index)))


def sequence_summary(db: Session, batch: MessageBatch) -> dict:
    actions = group_actions(db, batch)
    return {"batch_id": batch.id, "generation_no": batch.generation_no,
            "segment_count": len(actions),
            "sent_count": sum(action.status == "sent" for action in actions),
            "terminal": not batch.active and all(action.status not in {"draft", "guarding", "queued", "sending"} for action in actions)}


def lock_sequence_conversation(db: Session, conversation_id: str) -> None:
    # All callers first own the Lead lock. Match C2 ingest: binding, Worker,
    # conversation, then batch/action/task. Never acquire these after an action.
    binding = db.scalar(select(WechatSessionBinding).where(
        WechatSessionBinding.conversation_id == conversation_id,
        WechatSessionBinding.deleted_at.is_(None)).with_for_update().execution_options(populate_existing=True))
    if binding:
        db.scalar(select(Worker).where(Worker.id == binding.worker_id).with_for_update().execution_options(populate_existing=True))
    db.scalar(select(Conversation).where(Conversation.conversation_id == conversation_id)
              .with_for_update().execution_options(populate_existing=True))


def cancel_remaining(db: Session, action: ReplyAction, reason: str) -> None:
    from app.services.c3_service import _cancel_task_for_action
    batch = db.get(MessageBatch, action.batch_id)
    for candidate in group_actions(db, batch):
        if candidate.segment_index <= action.segment_index or candidate.status != "queued":
            continue
        candidate.status = "cancelled"
        candidate.current = False
        candidate.error_code = reason
        _cancel_task_for_action(db, candidate, reason=reason)


def require_current_segment(db: Session, action: ReplyAction, worker: Worker) -> None:
    if action.segment_count <= 1:
        return
    if not supports_reply_sequence(worker):
        raise AppError("WORKER_REPLY_SEQUENCE_UNSUPPORTED", "当前客户端不支持分段回复", 409)
    if not action.current:
        raise AppError("REPLY_PREDECESSOR_NOT_SENT", "前一段尚未确认发送", 409)
    if action.predecessor_reply_action_id:
        ack = db.scalar(select(SentAck).where(SentAck.reply_action_id == action.predecessor_reply_action_id))
        if ack is None or ack.send_result != "sent":
            raise AppError("REPLY_PREDECESSOR_NOT_SENT", "前一段尚未确认发送", 409)


def settle_unavailable_sequence(db: Session, *, batch: MessageBatch, worker: Worker) -> None:
    """Expiry/capability loss cancels only untriggered work, never a send receipt."""
    from app.services.c3_service import _cancel_task_for_action, _is_past
    from app.services.followup_eligibility import conversation_lead_id, lock_leads
    lock_leads(db, [conversation_lead_id(db, batch.conversation_id)])
    lock_sequence_conversation(db, batch.conversation_id)
    actions = group_actions(db, batch)
    reason = "WORKER_REPLY_SEQUENCE_UNSUPPORTED" if not supports_reply_sequence(worker) else None
    if any(a.status == "queued" and _is_past(a.expire_at) for a in actions):
        reason = "REPLY_ACTION_EXPIRED"
    if reason:
        for action in actions:
            if action.status == "queued":
                action.status, action.error_code = "cancelled", reason
                _cancel_task_for_action(db, action, reason=reason)


def require_sequence_settled_for_flow(db: Session, *, conversation_id: str, technical_failed: bool) -> None:
    from app.services.c3_service import _cancel_task_for_action
    actions = list(db.scalars(select(ReplyAction).where(
        ReplyAction.conversation_id == conversation_id, ReplyAction.segment_count > 1,
        ReplyAction.status.in_({"queued", "sending"}), ReplyAction.deleted_at.is_(None))))
    if any(a.status == "sending" for a in actions) or (actions and not technical_failed):
        raise AppError("WORKER_INFLIGHT_FLOW_NOT_SETTLED", "分段回复尚未结算", 409)
    for action in actions:
        action.status, action.error_code = "cancelled", "WORKER_TECHNICAL_FAILED"
        _cancel_task_for_action(db, action, reason=action.error_code)


def interrupt_sequence(db: Session, *, worker: Worker, batch_id: str, flow_id: str, evidence: dict) -> dict:
    from app.services.c3_service import _cancel_task_for_action
    from app.services.followup_eligibility import conversation_lead_id, lock_leads
    from app.services.worker_service import validate_inflight_continuation
    batch = db.get(MessageBatch, batch_id)
    if batch is None:
        raise AppError("MESSAGE_BATCH_NOT_FOUND", "消息批次不存在", 404)
    lock_leads(db, [conversation_lead_id(db, batch.conversation_id)])
    lock_sequence_conversation(db, batch.conversation_id)
    binding = db.scalar(select(WechatSessionBinding).where(
        WechatSessionBinding.conversation_id == batch.conversation_id, WechatSessionBinding.deleted_at.is_(None)))
    validate_inflight_continuation(worker, flow_id)
    flow = worker.inflight_flow_state or {}
    if not binding or binding.worker_id != worker.id or flow.get("conversation_id") != batch.conversation_id:
        raise AppError("WORKER_INFLIGHT_FLOW_SCOPE_MISMATCH", "中断仅限当前同一客户流程", 409)
    actions = group_actions(db, batch)
    if not supports_reply_sequence(worker) or not any(a.segment_count > 1 for a in actions):
        raise AppError("WORKER_REPLY_SEQUENCE_UNSUPPORTED", "当前批次不支持分段中断", 409)
    previous = (batch.ai_response_snapshot or {}).get("reply_sequence_interrupt")
    if previous and previous.get("flow_id") != flow_id:
        raise AppError("WORKER_INFLIGHT_FLOW_SCOPE_MISMATCH", "中断已归属其他流程", 409)
    if not previous:
        batch.ai_response_snapshot = {**(batch.ai_response_snapshot or {}),
            "reply_sequence_interrupt": {"flow_id": flow_id, "worker_id": worker.id, **evidence}}
    for action in actions:
        if action.status == "queued":
            action.status, action.error_code = "superseded", "REPLY_ACTION_SUPERSEDED"
            _cancel_task_for_action(db, action, reason=action.error_code)
    batch.status, batch.active, batch.error_code = "superseded", False, "MESSAGE_BATCH_SUPERSEDED"
    db.flush()
    return {"cancelled": True, "batch_id": batch_id, "reply_sequence": sequence_summary(db, batch)}


def settle_customer_interrupted_segment(db: Session, action: ReplyAction, payload) -> bool:
    """Settle the unused permit and cancel its tail, retaining this Flow's read permission."""
    from app.contracts.shared_rules import shared_adapter
    if action.segment_count <= 1 or payload.send_result != "failed":
        return False
    proof = shared_adapter("reply_sequence").confirmed_customer_interruption(
        error_code=payload.error_code, action_phase=payload.action_phase, evidence=payload.evidence)
    worker = db.get(Worker, payload.worker_id)
    flow = (worker.inflight_flow_state or {}) if worker else {}
    if not proof or not supports_reply_sequence(worker) or not flow.get("flow_id") or flow.get("conversation_id") != action.conversation_id:
        return False
    interrupt_sequence(db, worker=worker, batch_id=action.batch_id, flow_id=flow["flow_id"], evidence=proof)
    return True


def interrupted_customer_tail(db: Session, *, batch_id: str, flow_id: str, worker_id: str, conversation_id: str, visible_message_orders: dict[str, int]) -> list[str]:
    """A complete authorized reread can collect media settled earlier without UI.

    Fact settlement itself never generates. The ordinary collector remains
    idempotent for events already owned by a batch.
    """
    batch = db.get(MessageBatch, batch_id)
    interruption = (batch.ai_response_snapshot or {}).get("reply_sequence_interrupt") if batch else None
    if (not batch or batch.conversation_id != conversation_id or not interruption
            or interruption.get("flow_id") != flow_id or interruption.get("worker_id") != worker_id):
        return []
    events = list(db.scalars(select(MessageEvent).where(
        MessageEvent.conversation_id == conversation_id,
        MessageEvent.id.in_(list(visible_message_orders)),
    )))
    tail = []
    for event in sorted(events, key=lambda row: visible_message_orders[row.id]):
        if event.sender_role == "customer":
            tail.append(event.id)
        elif event.sender_role in {"self", "sales"}:
            tail.clear()
    return tail


def advance_after_sent_ack(db: Session, action: ReplyAction, *, followup_cancelled: bool) -> None:
    """Called under the same conversation lock and transaction as the receipt."""
    if action.segment_count <= 1:
        return
    from app.services.c3_service import _ensure_reply_action_send_eligible, _is_past
    batch = db.get(MessageBatch, action.batch_id)
    next_action = db.scalar(select(ReplyAction).where(
        ReplyAction.predecessor_reply_action_id == action.id,
        ReplyAction.batch_id == action.batch_id,
        ReplyAction.generation_no == action.generation_no,
        ReplyAction.segment_index == action.segment_index + 1,
        ReplyAction.deleted_at.is_(None)))
    if not next_action or next_action.status != "queued":
        return
    binding = db.scalar(select(WechatSessionBinding).where(
        WechatSessionBinding.conversation_id == action.conversation_id,
        WechatSessionBinding.deleted_at.is_(None)))
    conversation = db.get(Conversation, action.conversation_id)
    reason = None
    if followup_cancelled:
        reason = "LEAD_INVALID"
    elif batch.status != "reply_action_created" or batch.superseded_by_batch_id:
        reason = "REPLY_ACTION_SUPERSEDED"
    elif _is_past(next_action.expire_at):
        reason = "REPLY_ACTION_EXPIRED"
    else:
        try:
            _ensure_reply_action_send_eligible(db, binding=binding, conversation=conversation, action=next_action)
        except AppError as exc:
            reason = exc.code
    if reason:
        cancel_remaining(db, action, reason)
        return
    action.current = False
    db.flush()  # Preserve the one-current-action partial unique constraint.
    next_action.current = True
    task = db.scalar(select(Task).where(Task.reply_action_id == next_action.id, Task.deleted_at.is_(None)))
    if task and task.status == "blocked" and task.block_code == "REPLY_PREDECESSOR_NOT_SENT":
        task.status = "pending"
        task.block_code = None
    # Next action remains without a checkpoint until an actual complete C2
    # read proves the predecessor's confirmed AI bubble in the current frame.


def freeze_next_checkpoint_from_read(db: Session, *, conversation_id: str, frame_evidence: dict) -> None:
    """Run only after a complete, validated, non-error C2 ingest transaction."""
    from app.services.c3_service import _build_pre_send_fact_checkpoint, _checkpoint_tail_from_latest_complete_frame
    action = db.scalar(select(ReplyAction).where(
        ReplyAction.conversation_id == conversation_id, ReplyAction.current.is_(True),
        ReplyAction.status == "queued", ReplyAction.segment_index > 1,
        ReplyAction.deleted_at.is_(None)))
    if action is None or action.pre_send_fact_checkpoint:
        return
    from app.services.message_effective_text import require_current_context
    batch = db.get(MessageBatch, action.batch_id)
    require_current_context(db, batch)
    ack = db.scalar(select(SentAck).where(SentAck.reply_action_id == action.predecessor_reply_action_id))
    if ack is None or ack.send_result != "sent":
        return
    messages = list(db.scalars(select(MessageEvent).where(
        MessageEvent.conversation_id == conversation_id)))
    tail = _checkpoint_tail_from_latest_complete_frame(messages, frame_evidence=frame_evidence)
    if not any(message.sender_role == "self" and (message.raw_payload or {}).get("ai_reply_action_id") == action.predecessor_reply_action_id
               and (message.raw_payload or {}).get("sender_source") == "ai" for message in tail):
        return
    checkpoint = _build_pre_send_fact_checkpoint(
        db=db, batch=batch, ordered_messages=tail,
        authoritative_frame_source=str(frame_evidence.get("authoritative_frame_source") or ""),
    )
    if checkpoint["tail_complete"]:
        action.pre_send_fact_checkpoint = checkpoint
