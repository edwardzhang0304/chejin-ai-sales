from __future__ import annotations

from datetime import timedelta
import hashlib
import secrets
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.enums import TaskEventType, TaskResultCode, TaskStatus, TaskType
from app.errors import AppError
from app.models.base import utcnow
from app.models.c3 import Conversation, HandoffEvent, MessageBatch, ReplyAction, SentAck
from app.models.task import Task
from app.models.wechat import MessageEvent, WechatSessionBinding
from app.models.worker import Worker
from app.services.ai_adapter import AIEngineDecision, get_ai_engine_adapter
from app.services.task_service import _write_event, finish_task_and_release_worker, get_task_or_404, task_to_detail


ACTIVE_BATCH_STATUSES = {"collecting", "generating"}
OPEN_ACTION_STATUSES = {"draft", "guarding", "queued", "sending"}
TERMINAL_ACTION_STATUSES = {
    "sent",
    "failed",
    "unknown_send_result",
    "superseded",
    "expired",
    "cancelled",
    "handoff",
    "blocked",
}


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _is_past(value) -> bool:
    if not value:
        return False
    now = utcnow()
    if getattr(value, "tzinfo", None) is None:
        now = now.replace(tzinfo=None)
    return value <= now


def _binding_or_404(db: Session, conversation_id: str) -> WechatSessionBinding:
    binding = db.scalar(
        select(WechatSessionBinding).where(
            WechatSessionBinding.conversation_id == conversation_id,
            WechatSessionBinding.deleted_at.is_(None),
        )
    )
    if not binding:
        raise AppError("CONVERSATION_NOT_ELIGIBLE", "会话不存在或未绑定", 404, {"suggested_action": "check_conversation_binding"})
    return binding


def _conversation_for_binding(db: Session, binding: WechatSessionBinding) -> Conversation:
    conversation = db.get(Conversation, binding.conversation_id)
    if not conversation:
        conversation = Conversation(
            conversation_id=binding.conversation_id,
            lead_id=binding.lead_id,
            sales_id=binding.sales_id,
            worker_id=binding.worker_id,
        )
        db.add(conversation)
        db.flush()
    return conversation


def _ensure_conversation_eligible(binding: WechatSessionBinding, conversation: Conversation) -> None:
    if binding.bind_status != "bound" or not binding.allow_listening:
        raise AppError("CONVERSATION_NOT_ELIGIBLE", "会话未绑定或不允许监听", 409, {"suggested_action": "handoff"})
    if not conversation.ai_enabled or conversation.status in {"waiting_sales_reply", "closed", "rejected"}:
        raise AppError("CONVERSATION_NOT_ELIGIBLE", "会话已关闭 AI 或处于人工接管状态", 409, {"suggested_action": "handoff"})


def _batch_to_dict(batch: MessageBatch) -> dict[str, Any]:
    return {
        "id": batch.id,
        "conversation_id": batch.conversation_id,
        "status": batch.status,
        "active": batch.active,
        "trigger_message_event_id": batch.trigger_message_event_id,
        "message_event_ids": batch.message_event_ids,
        "message_count": batch.message_count,
        "generation_no": batch.generation_no,
        "trace_id": batch.trace_id,
        "decision": batch.decision,
        "error_code": batch.error_code,
        "suggested_action": batch.suggested_action,
        "superseded_by_batch_id": batch.superseded_by_batch_id,
        "generated_at": batch.generated_at,
        "created_at": batch.created_at,
        "updated_at": batch.updated_at,
    }


def _reply_action_to_dict(action: ReplyAction | None) -> dict[str, Any] | None:
    if not action:
        return None
    return {
        "id": action.id,
        "batch_id": action.batch_id,
        "conversation_id": action.conversation_id,
        "status": action.status,
        "current": action.current,
        "generation_no": action.generation_no,
        "decision": action.decision,
        "reply_text": action.reply_text,
        "reply_text_hash": action.reply_text_hash,
        "confidence": action.confidence,
        "risk_flags": action.risk_flags,
        "evidence_refs": action.evidence_refs,
        "guard_result": action.guard_result,
        "handoff_reason_code": action.handoff_reason_code,
        "error_code": action.error_code,
        "suggested_action": action.suggested_action,
        "expire_at": action.expire_at,
        "claimed_by_worker_id": action.claimed_by_worker_id,
        "claimed_task_id": action.claimed_task_id,
        "sending_claimed_at": action.sending_claimed_at,
        "sent_at": action.sent_at,
        "created_at": action.created_at,
        "updated_at": action.updated_at,
    }


def _handoff_to_dict(event: HandoffEvent | None) -> dict[str, Any] | None:
    if not event:
        return None
    return {
        "id": event.id,
        "conversation_id": event.conversation_id,
        "batch_id": event.batch_id,
        "status": event.status,
        "handoff_reason_code": event.handoff_reason_code,
        "reason_detail": event.reason_detail,
        "trigger_message_event_ids": event.trigger_message_event_ids,
        "risk_flags": event.risk_flags,
        "evidence_refs": event.evidence_refs,
        "notify_error_code": event.notify_error_code,
        "created_at": event.created_at,
        "updated_at": event.updated_at,
    }


def _sent_ack_to_dict(ack: SentAck) -> dict[str, Any]:
    return {
        "id": ack.id,
        "reply_action_id": ack.reply_action_id,
        "task_id": ack.task_id,
        "worker_id": ack.worker_id,
        "client_instance_id": ack.client_instance_id,
        "send_result": ack.send_result,
        "reply_text_hash": ack.reply_text_hash,
        "sidecar_run_id": ack.sidecar_run_id,
        "evidence": ack.evidence,
        "error_code": ack.error_code,
        "remark": ack.remark,
        "sent_at": ack.sent_at,
        "created_at": ack.created_at,
    }


def _active_batch(db: Session, conversation_id: str) -> MessageBatch | None:
    return db.scalar(
        select(MessageBatch)
        .where(
            MessageBatch.conversation_id == conversation_id,
            MessageBatch.active.is_(True),
            MessageBatch.deleted_at.is_(None),
        )
        .order_by(MessageBatch.created_at.desc())
    )


def _customer_messages(db: Session, batch: MessageBatch) -> list[MessageEvent]:
    if not batch.message_event_ids:
        return []
    return list(
        db.scalars(
            select(MessageEvent)
            .where(
                MessageEvent.id.in_(batch.message_event_ids),
                MessageEvent.sender_role == "customer",
            )
            .order_by(MessageEvent.occurred_at.asc().nullsfirst(), MessageEvent.ingested_at.asc(), MessageEvent.id.asc())
        )
    )


def _cancel_task_for_action(db: Session, action: ReplyAction, *, reason: str) -> None:
    task = db.scalar(select(Task).where(Task.reply_action_id == action.id, Task.deleted_at.is_(None)))
    if task and task.status in {TaskStatus.blocked.value, TaskStatus.pending.value, TaskStatus.running.value}:
        before = task.status
        task.status = TaskStatus.cancelled.value
        task.cancel_reason = reason
        task.cancelled_at = utcnow()
        finish_task_and_release_worker(task)
        _write_event(db, task, TaskEventType.cancelled, from_status=before, to_status=task.status, remark=reason)


def _supersede_open_actions(db: Session, conversation_id: str, *, reason: str) -> None:
    actions = list(
        db.scalars(
            select(ReplyAction).where(
                ReplyAction.conversation_id == conversation_id,
                ReplyAction.status.in_(OPEN_ACTION_STATUSES),
                ReplyAction.deleted_at.is_(None),
            )
        )
    )
    for action in actions:
        action.status = "superseded"
        action.current = False
        action.error_code = "REPLY_ACTION_SUPERSEDED"
        action.suggested_action = "regenerate"
        _cancel_task_for_action(db, action, reason=reason)


def supersede_open_reply_actions_for_new_inbound(db: Session, conversation_id: str) -> None:
    _supersede_open_actions(db, conversation_id, reason="客户新消息到来，旧回复动作作废")


def cancel_open_reply_actions_for_conversation_change(db: Session, conversation_id: str, *, reason: str) -> None:
    actions = list(
        db.scalars(
            select(ReplyAction).where(
                ReplyAction.conversation_id == conversation_id,
                ReplyAction.status.in_(OPEN_ACTION_STATUSES),
                ReplyAction.deleted_at.is_(None),
            )
        )
    )
    for action in actions:
        action.status = "cancelled"
        action.current = False
        _cancel_task_for_action(db, action, reason=reason)


def collect_message_batch(
    db: Session,
    *,
    conversation_id: str,
    trigger_message_event_id: str,
    trace_id: str | None = None,
) -> dict[str, Any]:
    binding = _binding_or_404(db, conversation_id)
    conversation = _conversation_for_binding(db, binding)
    _ensure_conversation_eligible(binding, conversation)
    message = db.get(MessageEvent, trigger_message_event_id)
    if not message or message.conversation_id != conversation_id:
        raise AppError("MESSAGE_EVENT_NOT_FOUND", "触发消息不存在或不属于该会话", 404, {"suggested_action": "check_message_event"})
    if message.sender_role != "customer":
        return {
            "batch_id": None,
            "batch_status": "no_action",
            "next_step": "no_action",
            "error_code": None,
            "suggested_action": "ignore_non_customer_message",
        }

    active = _active_batch(db, conversation_id)
    if active and active.status == "generating":
        active.status = "superseded"
        active.active = False
        active.error_code = "MESSAGE_BATCH_SUPERSEDED"
        _supersede_open_actions(db, conversation_id, reason="客户新消息到来，旧回复动作作废")
        active = None

    if active and active.status == "collecting":
        ids = list(active.message_event_ids or [])
        if message.id not in ids:
            ids.append(message.id)
            active.message_event_ids = ids
            active.message_count = len(ids)
            active.trigger_message_event_id = message.id
            active.trace_id = trace_id or active.trace_id
        db.flush()
        return {"batch_id": active.id, "batch_status": active.status, "next_step": "generate", "batch": _batch_to_dict(active)}

    batch = MessageBatch(
        conversation_id=conversation_id,
        status="collecting",
        active=True,
        trigger_message_event_id=message.id,
        message_event_ids=[message.id],
        message_count=1,
        generation_no=1,
        trace_id=trace_id,
    )
    db.add(batch)
    db.flush()
    return {"batch_id": batch.id, "batch_status": batch.status, "next_step": "generate", "batch": _batch_to_dict(batch)}


def _build_ai_context(db: Session, binding: WechatSessionBinding, conversation: Conversation, batch: MessageBatch) -> dict[str, Any]:
    messages = _customer_messages(db, batch)
    return {
        "conversation": {
            "conversation_id": binding.conversation_id,
            "lead_id": binding.lead_id,
            "sales_id": binding.sales_id,
            "worker_id": binding.worker_id,
            "remark_code": binding.remark_code,
            "status": conversation.status,
            "ai_enabled": conversation.ai_enabled,
            "reply_count": conversation.reply_count,
        },
        "messages": [
            {
                "id": item.id,
                "content": item.content,
                "message_type": item.message_type,
                "dedupe_key": item.dedupe_key,
                "occurred_at": item.occurred_at.isoformat() if item.occurred_at else None,
                "ingested_at": item.ingested_at.isoformat(),
            }
            for item in messages
        ],
    }


def _decision_payload(decision: AIEngineDecision) -> dict[str, Any]:
    return {
        "decision": decision.decision,
        "reply_text": decision.reply_text,
        "confidence": decision.confidence,
        "handoff_reason_code": decision.handoff_reason_code,
        "risk_flags": decision.risk_flags or [],
        "evidence_refs": decision.evidence_refs or [],
        "guard_result": decision.guard_result,
        "rewrite_required": decision.rewrite_required,
        "error_code": decision.error_code,
        "suggested_action": decision.suggested_action,
        "raw_payload": decision.raw_payload or {},
    }


def _create_handoff(
    db: Session,
    *,
    binding: WechatSessionBinding,
    conversation: Conversation,
    batch: MessageBatch,
    decision: AIEngineDecision,
    handoff_reason_code: str,
) -> HandoffEvent:
    event = HandoffEvent(
        conversation_id=binding.conversation_id,
        batch_id=batch.id,
        status="created",
        handoff_reason_code=handoff_reason_code,
        reason_detail=decision.handoff_reason_code or handoff_reason_code,
        trigger_message_event_ids=list(batch.message_event_ids or []),
        risk_flags=decision.risk_flags or [],
        evidence_refs=decision.evidence_refs or [],
        ai_payload=_decision_payload(decision),
    )
    db.add(event)
    conversation.ai_enabled = False
    conversation.status = "waiting_sales_reply"
    conversation.handoff_reason_code = handoff_reason_code
    conversation.handoff_at = utcnow()
    batch.status = "handoff_created"
    batch.active = False
    batch.decision = "handoff"
    batch.error_code = handoff_reason_code
    batch.suggested_action = "handoff"
    batch.ai_response_snapshot = _decision_payload(decision)
    batch.generated_at = utcnow()
    return event


def _create_chat_reply_task(db: Session, *, binding: WechatSessionBinding, action: ReplyAction) -> Task:
    existing = db.scalar(select(Task).where(Task.reply_action_id == action.id, Task.deleted_at.is_(None)))
    if existing:
        return existing
    task = Task(
        task_type=TaskType.chat_reply.value,
        status=TaskStatus.pending.value,
        lead_id=binding.lead_id,
        sales_id=binding.sales_id,
        worker_id=binding.worker_id,
        reply_action_id=action.id,
        remark="C3 AI 回复发送任务",
        created_by="system",
        updated_by="system",
    )
    db.add(task)
    db.flush()
    _write_event(db, task, TaskEventType.created, to_status=task.status, remark="reply_action 已通过 Guard，创建 chat_reply 任务")
    return task


def generate_for_batch(db: Session, *, batch_id: str, force: bool = False) -> dict[str, Any]:
    batch = db.scalar(select(MessageBatch).where(MessageBatch.id == batch_id, MessageBatch.deleted_at.is_(None)).with_for_update())
    if not batch:
        raise AppError("MESSAGE_BATCH_NOT_FOUND", "消息批次不存在", 404)

    existing_action = db.scalar(select(ReplyAction).where(ReplyAction.batch_id == batch.id, ReplyAction.current.is_(True)))
    existing_handoff = db.scalar(select(HandoffEvent).where(HandoffEvent.batch_id == batch.id, HandoffEvent.deleted_at.is_(None)))
    if not force and batch.status in {"reply_action_created", "handoff_created", "no_action", "failed"}:
        existing_task = db.scalar(select(Task).where(Task.reply_action_id == existing_action.id, Task.deleted_at.is_(None))) if existing_action else None
        return {
            "decision": batch.decision,
            "batch": _batch_to_dict(batch),
            "reply_action_id": existing_action.id if existing_action else None,
            "reply_action": _reply_action_to_dict(existing_action),
            "task_id": existing_task.id if existing_task else None,
            "task": task_to_detail(get_task_or_404(db, existing_task.id)) if existing_task else None,
            "handoff_event_id": existing_handoff.id if existing_handoff else None,
            "handoff_event": _handoff_to_dict(existing_handoff),
            "error_code": batch.error_code,
            "suggested_action": batch.suggested_action,
        }

    binding = _binding_or_404(db, batch.conversation_id)
    conversation = _conversation_for_binding(db, binding)
    try:
        _ensure_conversation_eligible(binding, conversation)
    except AppError as exc:
        decision = AIEngineDecision(
            decision="handoff",
            handoff_reason_code=exc.code,
            risk_flags=[exc.code.lower()],
            guard_result="handoff",
            error_code=exc.code,
            suggested_action="handoff",
        )
        handoff = _create_handoff(db, binding=binding, conversation=conversation, batch=batch, decision=decision, handoff_reason_code=exc.code)
        db.flush()
        return {
            "decision": "handoff",
            "batch": _batch_to_dict(batch),
            "handoff_event_id": handoff.id,
            "handoff_event": _handoff_to_dict(handoff),
            "error_code": exc.code,
            "suggested_action": "handoff",
        }

    batch.status = "generating"
    context = _build_ai_context(db, binding, conversation, batch)
    batch.ai_request_snapshot = context
    messages = context["messages"]
    if not messages:
        batch.status = "no_action"
        batch.active = False
        batch.decision = "no_action"
        batch.error_code = "MESSAGE_BATCH_EMPTY"
        batch.suggested_action = "wait_more"
        batch.generated_at = utcnow()
        db.flush()
        return {"decision": "no_action", "batch": _batch_to_dict(batch), "error_code": "MESSAGE_BATCH_EMPTY", "suggested_action": "wait_more"}

    try:
        decision = get_ai_engine_adapter().generate_reply_decision(conversation_context=context["conversation"], message_batch={"id": batch.id, "messages": messages})
    except AppError as exc:
        decision = AIEngineDecision(
            decision="handoff",
            handoff_reason_code=exc.code,
            risk_flags=[exc.code.lower()],
            guard_result="handoff",
            error_code=exc.code,
            suggested_action=exc.data.get("suggested_action") or "handoff",
        )

    payload = _decision_payload(decision)
    batch.ai_response_snapshot = payload

    if decision.decision == "send_reply":
        if not decision.reply_text or decision.guard_result not in {"pass", "rewrite_passed"}:
            decision = AIEngineDecision(
                decision="handoff",
                handoff_reason_code="AI_ENGINE_CONTRACT_INVALID",
                risk_flags=["ai_contract_invalid"],
                guard_result="handoff",
                error_code="AI_ENGINE_CONTRACT_INVALID",
                suggested_action="handoff",
                raw_payload=payload,
            )
            handoff = _create_handoff(
                db,
                binding=binding,
                conversation=conversation,
                batch=batch,
                decision=decision,
                handoff_reason_code="AI_ENGINE_CONTRACT_INVALID",
            )
            db.flush()
            return {
                "decision": "handoff",
                "batch": _batch_to_dict(batch),
                "handoff_event_id": handoff.id,
                "handoff_event": _handoff_to_dict(handoff),
                "error_code": "AI_ENGINE_CONTRACT_INVALID",
                "suggested_action": "handoff",
            }

        _supersede_open_actions(db, binding.conversation_id, reason="生成新的当前回复动作")
        expire_at = utcnow() + timedelta(seconds=get_settings().c3_reply_action_ttl_seconds)
        action = ReplyAction(
            batch_id=batch.id,
            conversation_id=binding.conversation_id,
            status="queued",
            current=True,
            generation_no=batch.generation_no,
            decision="send_reply",
            reply_text=decision.reply_text,
            reply_text_hash=_hash_text(decision.reply_text),
            confidence=decision.confidence,
            risk_flags=decision.risk_flags or [],
            evidence_refs=decision.evidence_refs or [],
            guard_result=decision.guard_result,
            expire_at=expire_at,
            ai_payload=payload,
        )
        db.add(action)
        db.flush()
        task = _create_chat_reply_task(db, binding=binding, action=action)
        batch.status = "reply_action_created"
        batch.active = False
        batch.decision = "send_reply"
        batch.error_code = None
        batch.suggested_action = "claim_send"
        batch.generated_at = utcnow()
        db.flush()
        return {
            "decision": "send_reply",
            "batch": _batch_to_dict(batch),
            "reply_action_id": action.id,
            "reply_action": _reply_action_to_dict(action),
            "task_id": task.id,
            "task": task_to_detail(get_task_or_404(db, task.id)),
            "error_code": None,
            "suggested_action": "claim_send",
        }

    if decision.decision == "handoff":
        handoff_reason_code = decision.error_code or decision.handoff_reason_code or "HANDOFF_REQUIRED"
        handoff = _create_handoff(
            db,
            binding=binding,
            conversation=conversation,
            batch=batch,
            decision=decision,
            handoff_reason_code=handoff_reason_code,
        )
        db.flush()
        return {
            "decision": "handoff",
            "batch": _batch_to_dict(batch),
            "handoff_event_id": handoff.id,
            "handoff_event": _handoff_to_dict(handoff),
            "error_code": handoff_reason_code,
            "suggested_action": "handoff",
        }

    if decision.decision in {"no_action", "pause", "retry_later"}:
        batch.status = "no_action" if decision.decision == "no_action" else "failed"
        batch.active = False
        batch.decision = decision.decision
        batch.error_code = decision.error_code
        batch.suggested_action = decision.suggested_action or decision.decision
        batch.ai_response_snapshot = payload
        batch.generated_at = utcnow()
        db.flush()
        return {
            "decision": decision.decision,
            "batch": _batch_to_dict(batch),
            "error_code": decision.error_code,
            "suggested_action": batch.suggested_action,
        }

    decision = AIEngineDecision(
        decision="handoff",
        handoff_reason_code="AI_ENGINE_CONTRACT_INVALID",
        risk_flags=["ai_contract_invalid"],
        guard_result="handoff",
        error_code="AI_ENGINE_CONTRACT_INVALID",
        suggested_action="handoff",
        raw_payload=payload,
    )
    handoff = _create_handoff(
        db,
        binding=binding,
        conversation=conversation,
        batch=batch,
        decision=decision,
        handoff_reason_code="AI_ENGINE_CONTRACT_INVALID",
    )
    db.flush()
    return {
        "decision": "handoff",
        "batch": _batch_to_dict(batch),
        "handoff_event_id": handoff.id,
        "handoff_event": _handoff_to_dict(handoff),
        "error_code": "AI_ENGINE_CONTRACT_INVALID",
        "suggested_action": "handoff",
    }


def validate_chat_reply_task_claim(db: Session, task: Task, worker: Worker) -> None:
    if task.task_type != TaskType.chat_reply.value:
        return
    if not task.reply_action_id:
        raise AppError("REPLY_ACTION_NOT_FOUND", "chat_reply 任务缺少 reply_action_id", 409, {"suggested_action": "cancel_task"})
    action = db.get(ReplyAction, task.reply_action_id)
    if not action or action.deleted_at:
        raise AppError("REPLY_ACTION_NOT_FOUND", "reply_action 不存在", 404, {"suggested_action": "cancel_task"})
    if action.status != "queued":
        raise AppError("REPLY_ACTION_CLAIM_CONFLICT", "reply_action 当前状态不允许领取任务", 409, {"status": action.status, "suggested_action": "do_not_send"})
    if _is_past(action.expire_at):
        action.status = "expired"
        action.current = False
        task.status = TaskStatus.cancelled.value
        task.cancel_reason = "reply_action 已过期"
        task.cancelled_at = utcnow()
        finish_task_and_release_worker(task)
        raise AppError("REPLY_ACTION_EXPIRED", "回复动作已过期", 409, {"suggested_action": "do_not_send"})
    if task.worker_id and task.worker_id != worker.id:
        raise AppError("TASK_WORKER_MISMATCH", "该 chat_reply 任务已指定其他 Worker", 409, {"suggested_action": "do_not_send"})


def claim_send(db: Session, *, reply_action_id: str, task_id: str, worker_id: str) -> dict[str, Any]:
    # Local import avoids the c3 <-> wechat service module cycle while keeping
    # the authorization algorithm owned by exactly one backend implementation.
    from app.services.wechat_service import _authorization_revision

    action = db.scalar(select(ReplyAction).where(ReplyAction.id == reply_action_id, ReplyAction.deleted_at.is_(None)).with_for_update())
    if not action:
        raise AppError("REPLY_ACTION_NOT_FOUND", "reply_action 不存在", 404)
    task = db.scalar(select(Task).where(Task.id == task_id, Task.deleted_at.is_(None)).with_for_update())
    if not task or task.reply_action_id != action.id:
        raise AppError("TASK_NOT_FOUND", "chat_reply 任务不存在或不匹配", 404, {"suggested_action": "do_not_send"})
    if task.task_type != TaskType.chat_reply.value:
        raise AppError("TASK_TYPE_NOT_SUPPORTED", "仅 chat_reply 任务支持 claim-send", 400)
    if task.status != TaskStatus.running.value:
        raise AppError("REPLY_ACTION_CLAIM_CONFLICT", "Worker 必须先领取 chat_reply 任务再 claim-send", 409, {"suggested_action": "claim_task_first"})
    if task.worker_id != worker_id:
        raise AppError("TASK_WORKER_MISMATCH", "任务 Worker 不匹配", 409, {"suggested_action": "do_not_send"})
    if action.status != "queued":
        raise AppError("REPLY_ACTION_CLAIM_CONFLICT", "reply_action 已被领取或不可发送", 409, {"status": action.status, "suggested_action": "do_not_send"})
    if _is_past(action.expire_at):
        action.status = "expired"
        action.current = False
        task.status = TaskStatus.cancelled.value
        task.cancel_reason = "reply_action 已过期"
        task.cancelled_at = utcnow()
        finish_task_and_release_worker(task)
        raise AppError("REPLY_ACTION_EXPIRED", "回复动作已过期", 409, {"suggested_action": "do_not_send"})
    binding = _binding_or_404(db, action.conversation_id)
    conversation = _conversation_for_binding(db, binding)
    _ensure_conversation_eligible(binding, conversation)
    send_token = secrets.token_urlsafe(32)
    action.status = "sending"
    action.send_token = send_token
    action.claimed_by_worker_id = worker_id
    action.claimed_task_id = task.id
    action.sending_claimed_at = utcnow()
    task.current_step = "reply_action_claimed"
    _write_event(db, task, TaskEventType.step_updated, from_status=task.status, to_status=task.status, worker_id=worker_id, remark="claim-send 成功")
    db.flush()
    return {
        "reply_action_id": action.id,
        "task_id": task.id,
        "send_token": send_token,
        "reply_text": action.reply_text,
        "reply_text_hash": action.reply_text_hash,
        "conversation_id": action.conversation_id,
        "rpa_session_key": binding.rpa_session_key,
        "remark_code": binding.remark_code,
        "display_name": binding.display_name,
        "authorization_revision": _authorization_revision(binding),
        "expire_at": action.expire_at,
        "suggested_action": "send_via_worker",
    }


def sent_ack(db: Session, *, reply_action_id: str, payload: Any) -> dict[str, Any]:
    existing = db.scalar(select(SentAck).where(SentAck.reply_action_id == reply_action_id))
    if existing:
        return {"duplicated": True, "ack": _sent_ack_to_dict(existing), "error_code": "SEND_ACK_DUPLICATED", "suggested_action": "use_existing_ack"}

    action = db.scalar(select(ReplyAction).where(ReplyAction.id == reply_action_id, ReplyAction.deleted_at.is_(None)).with_for_update())
    if not action:
        raise AppError("REPLY_ACTION_NOT_FOUND", "reply_action 不存在", 404)
    task = db.scalar(select(Task).where(Task.id == payload.task_id, Task.deleted_at.is_(None)).with_for_update())
    if not task or task.reply_action_id != action.id:
        raise AppError("TASK_NOT_FOUND", "chat_reply 任务不存在或不匹配", 404)
    if action.status != "sending":
        raise AppError("REPLY_ACTION_CLAIM_CONFLICT", "reply_action 未处于 sending 状态，不能回执", 409, {"status": action.status, "suggested_action": "do_not_retry_send"})
    if payload.send_token != action.send_token:
        raise AppError("REPLY_ACTION_CLAIM_CONFLICT", "send_token 不匹配", 409, {"suggested_action": "do_not_retry_send"})
    if payload.reply_text_hash and action.reply_text_hash and payload.reply_text_hash != action.reply_text_hash:
        payload.error_code = payload.error_code or "SEND_TEXT_HASH_MISMATCH"
        payload.send_result = "failed"

    ack = SentAck(
        reply_action_id=action.id,
        task_id=task.id,
        worker_id=payload.worker_id,
        client_instance_id=payload.client_instance_id,
        send_token=payload.send_token,
        send_result=payload.send_result,
        reply_text_hash=payload.reply_text_hash,
        sidecar_run_id=payload.sidecar_run_id,
        evidence=payload.evidence or {},
        error_code=payload.error_code,
        remark=payload.remark,
        sent_at=payload.sent_at,
    )
    db.add(ack)

    before = task.status
    binding = _binding_or_404(db, action.conversation_id)
    conversation = _conversation_for_binding(db, binding)
    if payload.send_result == "sent":
        action.status = "sent"
        action.sent_at = payload.sent_at or utcnow()
        task.status = TaskStatus.completed.value
        task.result_code = TaskResultCode.chat_reply_sent.value
        task.error_code = None
        task.completed_at = action.sent_at
        conversation.status = "waiting_user_reply"
        conversation.reply_count = (conversation.reply_count or 0) + 1
        conversation.last_outbound_at = action.sent_at
        conversation.last_ai_reply_at = action.sent_at
        finish_task_and_release_worker(task)
        _write_event(db, task, TaskEventType.completed, from_status=before, to_status=task.status, worker_id=payload.worker_id, remark=payload.remark)
    elif payload.send_result == "failed":
        action.status = "failed"
        action.error_code = payload.error_code or "RPA_SEND_REPLY_FAILED"
        task.status = TaskStatus.failed.value
        task.error_code = action.error_code
        task.failure_step = "send_reply"
        task.failure_remark = payload.remark
        task.failed_at = utcnow()
        finish_task_and_release_worker(task)
        _write_event(db, task, TaskEventType.failed, from_status=before, to_status=task.status, worker_id=payload.worker_id, remark=payload.remark)
    else:
        action.status = "unknown_send_result"
        action.error_code = payload.error_code or "SEND_RESULT_UNKNOWN"
        task.status = TaskStatus.failed.value
        task.error_code = action.error_code
        task.failure_step = "send_reply_unknown"
        task.failure_remark = payload.remark or "发送结果未知，需人工确认"
        task.failed_at = utcnow()
        finish_task_and_release_worker(task)
        _write_event(db, task, TaskEventType.failed, from_status=before, to_status=task.status, worker_id=payload.worker_id, remark=task.failure_remark)

    db.flush()
    return {"duplicated": False, "ack": _sent_ack_to_dict(ack), "reply_action": _reply_action_to_dict(action), "task": task_to_detail(get_task_or_404(db, task.id))}
