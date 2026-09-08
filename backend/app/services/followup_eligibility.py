"""Lead business eligibility. Call before acquiring downstream business locks.

Settlement is deliberately separate: a revoked permit is evidence for finishing
its registered Flow, never permission to start another UI action.
"""
from __future__ import annotations

import hashlib
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.errors import AppError
from app.models.lead import Lead
from app.models.wechat import WechatSessionBinding
from app.models.c3 import Conversation, ReplyAction
from app.models.task import Task
from app.models.worker import Worker
from app.models.audit import OperationLog
from app.models.base import utcnow

LEAD_INVALID = "LEAD_INVALID"


def lock_leads(db: Session, lead_ids) -> dict[str, Lead]:
    ids = sorted({value for value in lead_ids if value})
    if not ids:
        return {}
    # Preserve already-mutated lead values in this transaction. SessionLocal
    # disables autoflush; refreshing a dirty lead would otherwise undo the
    # caller's allocation/restore changes when a nested entry rechecks it.
    for lead in sorted((row for row in db.dirty if isinstance(row, Lead) and row.id in ids), key=lambda row: row.id):
        db.flush([lead])
    return {row.id: row for row in db.scalars(
        select(Lead).where(Lead.id.in_(ids)).order_by(Lead.id)
        .with_for_update().execution_options(populate_existing=True)
    )}


def followup_block_reason(db: Session, lead_id: str | None, *, lock: bool = True) -> str | None:
    if not lead_id:
        return None
    lead = lock_leads(db, [lead_id]).get(lead_id) if lock else db.get(Lead, lead_id)
    if lead is None or lead.deleted_at is not None:
        raise AppError("LEAD_NOT_FOUND", "关联线索不存在", 409)
    return LEAD_INVALID if lead.status == "invalid" else None


def require_followup(db: Session, lead_id: str | None) -> None:
    if followup_block_reason(db, lead_id):
        raise AppError(LEAD_INVALID, "已停止跟进：线索无效", 409)


def conversation_lead_id(db: Session, conversation_id: str) -> str | None:
    binding = db.scalar(select(WechatSessionBinding).where(
        WechatSessionBinding.conversation_id == conversation_id,
        WechatSessionBinding.deleted_at.is_(None)))
    conversation = db.get(Conversation, conversation_id)
    if binding and conversation and binding.lead_id and conversation.lead_id and binding.lead_id != conversation.lead_id:
        raise AppError("MESSAGE_TARGET_IDENTITY_MISMATCH", "会话线索归属不一致", 409)
    return (binding.lead_id if binding else None) or (conversation.lead_id if conversation else None)


def require_conversation_followup(db: Session, conversation_id: str, *, require_fresh_read: bool = False) -> None:
    require_followup(db, conversation_lead_id(db, conversation_id))
    if require_fresh_read:
        binding = db.scalar(select(WechatSessionBinding).where(
            WechatSessionBinding.conversation_id == conversation_id,
            WechatSessionBinding.deleted_at.is_(None)))
        if binding and binding.followup_restore_pending:
            raise AppError("MESSAGE_AUTHORIZATION_REVISION_EXPIRED", "恢复后的新读取尚未确认，不能沿用旧批次开始跟进", 409)


def token_for_revision(binding_id: str, revision: int) -> str:
    return hashlib.sha256(f"{binding_id}|{revision}".encode()).hexdigest()[:32]


def revoke_followup(db: Session, lead: Lead, actor, *, restoring: bool = False) -> dict:
    """Caller owns the Lead row lock. Does not commit or change listen settings."""
    from app.services.audit_service import write_log
    from app.services.task_service import cancel_task
    from app.services.c3_service import cancel_active_batches_for_conversation_change

    bindings = list(db.scalars(select(WechatSessionBinding).where(
        WechatSessionBinding.lead_id == lead.id,
        WechatSessionBinding.deleted_at.is_(None)).order_by(WechatSessionBinding.id).with_for_update()))
    conversation_ids = sorted({*db.scalars(select(Conversation.conversation_id).where(
        Conversation.lead_id == lead.id, Conversation.deleted_at.is_(None))),
        *(binding.conversation_id for binding in bindings)})
    sending_action_ids = list(db.scalars(select(ReplyAction.id).where(
        ReplyAction.conversation_id.in_(conversation_ids), ReplyAction.status == "sending")))
    changes = []
    for binding in bindings:
        old = int(binding.authorization_revision or 1)
        # Historical reconciliation can be replayed without revoking twice.
        if not restoring and binding.followup_invalidated_revision == old:
            continue
        worker = db.get(Worker, binding.worker_id)
        flow = dict(worker.inflight_flow_state or {}) if worker else {}
        binding.authorization_revision = old + 1
        if not restoring:
            binding.followup_invalidated_revision = old + 1
        binding.followup_restore_pending = restoring
        changes.append({
            "binding_id": binding.id, "conversation_id": binding.conversation_id,
            "worker_id": binding.worker_id, "old_revision": old,
            "new_revision": old + 1, "old_token": (flow.get("authorization_revision") if flow.get("conversation_id") == binding.conversation_id else None) or token_for_revision(binding.id, old),
            "flow_id": flow.get("flow_id") if flow.get("conversation_id") == binding.conversation_id else None,
            "unread_generation": flow.get("unread_generation"),
        })
        if restoring:
            # Preserve the original recovery evidence, but its old timer may
            # not make decisions before a newly authorized complete read.
            binding.next_read_due_at = utcnow()
    if not restoring:
        for conversation_id in conversation_ids:
            cancel_active_batches_for_conversation_change(db, conversation_id, reason=LEAD_INVALID)
        # The batch path already cancels its pending reply tasks. With
        # autoflush disabled, the following SQL filter would otherwise still
        # select those rows and try to cancel an in-memory terminal twice.
        db.flush()
    cancelled = []
    for task in db.scalars(select(Task).where(
        Task.lead_id == lead.id, Task.status.in_(["pending", "blocked"]),
        Task.deleted_at.is_(None)).order_by(Task.id).with_for_update()):
        cancel_task(db, task.id, LEAD_INVALID, actor)
        cancelled.append(task.id)
    if changes or cancelled:
        write_log(db, actor, event_type="lead_followup_restored" if restoring else "lead_followup_revoked",
                  module="lead", target_type="lead", target_id=lead.id, lead_id=lead.id,
                  metadata={"reason": LEAD_INVALID, "bindings": changes, "cancelled_task_ids": cancelled, "sending_action_ids": sending_action_ids})
    db.flush()
    return {"bindings": changes, "cancelled_task_ids": cancelled}


def revoked_flow_proof(db: Session, binding: WechatSessionBinding, flow_id: str,
                       original_revision: str | None = None) -> dict | None:
    if not flow_id or not binding.lead_id:
        return None
    logs = db.scalars(select(OperationLog).where(
        OperationLog.lead_id == binding.lead_id,
        OperationLog.event_type == "lead_followup_revoked").order_by(OperationLog.created_at.desc()))
    for log in logs:
        for item in (log.extra_metadata or {}).get("bindings", []):
            if (item.get("binding_id") == binding.id
                    and item.get("worker_id") == binding.worker_id
                    and item.get("flow_id") == flow_id
                    and (original_revision is None or item.get("old_token") == original_revision)):
                return item
    return None


def revoked_reply_action(db: Session, lead_id: str | None, action_id: str) -> bool:
    if not lead_id:
        return False
    return any(action_id in (log.extra_metadata or {}).get("sending_action_ids", [])
               for log in db.scalars(select(OperationLog).where(
                   OperationLog.lead_id == lead_id, OperationLog.event_type == "lead_followup_revoked")))


def reconcile_invalid_followup(db: Session, actor, *, apply: bool = False) -> dict:
    """Explicit local/admin maintenance entry; caller reviews then commits.

    Default preview never mutates rows, revisions or audit records. This is
    deliberately not scheduled at startup or run as part of the migration.
    """
    leads = list(db.scalars(select(Lead).where(Lead.status == "invalid", Lead.deleted_at.is_(None)).order_by(Lead.id)))
    result = []
    for lead in leads:
        bindings = list(db.scalars(select(WechatSessionBinding).where(
            WechatSessionBinding.lead_id == lead.id, WechatSessionBinding.deleted_at.is_(None))))
        binding_ids = [row.id for row in bindings if row.followup_invalidated_revision != int(row.authorization_revision or 1)]
        task_ids = list(db.scalars(select(Task.id).where(Task.lead_id == lead.id,
            Task.status.in_(["pending", "blocked"]), Task.deleted_at.is_(None))))
        result.append({"lead_id": lead.id, "binding_ids": binding_ids, "pending_task_ids": task_ids})
    if apply:
        locked = lock_leads(db, [row.id for row in leads])
        for lead in locked.values():
            if followup_block_reason(db, lead.id):
                revoke_followup(db, lead, actor)
    return {"mode": "apply" if apply else "preview", "items": result}


def revoked_read_facts_settled(db: Session, binding: WechatSessionBinding, flow_id: str) -> bool:
    from app.models.wechat import MessageEvent, WechatRecoverySettlement
    events = list(db.scalars(select(MessageEvent).where(
        MessageEvent.worker_id == binding.worker_id,
        MessageEvent.conversation_id == binding.conversation_id,
        MessageEvent.read_run_id == flow_id)))
    if not events:
        return True
    if binding.last_read_run_id == flow_id and binding.last_read_result == "cancelled":
        return True
    # Media recovery deliberately does not advance conversation/read state.
    # Verify its original settlement records instead of inventing a complete
    # viewport or forcing the recovery endpoint to alter business state.
    for event in events:
        evidence = event.evidence or {}
        transaction_id = evidence.get("recovery_transaction_id")
        if not transaction_id:
            return False
        settlement = db.scalar(select(WechatRecoverySettlement).where(
            WechatRecoverySettlement.worker_id == binding.worker_id,
            WechatRecoverySettlement.conversation_id == binding.conversation_id,
            WechatRecoverySettlement.recovery_transaction_id == transaction_id,
            WechatRecoverySettlement.status == "settled"))
        if not settlement or not any(item.get("source_message_key") == event.source_message_key
                and item.get("ingest_result") in {"ingested", "duplicated"}
                for item in settlement.source_results_json or []):
            return False
    return True
