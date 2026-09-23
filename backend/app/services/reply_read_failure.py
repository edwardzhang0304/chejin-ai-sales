"""Settle an original pre-send parse failure, never an issued send permit."""
from sqlalchemy import select

from app.errors import AppError
from app.models.audit import OperationLog
from app.models.c3 import ReplyAction, SentAck
from app.models.task import Task
from app.services import task_service
from app.services.audit_service import write_log
from app.services.followup_eligibility import lock_leads
from app.services.reply_sequence_service import lock_sequence_conversation
from app.services.worker_service import validate_inflight_continuation


def settle(db, *, worker, task_id, client_instance_id, flow_id, fencing, payload, actor):
    task = task_service.get_task_or_404(db, task_id)
    action = db.get(ReplyAction, task.reply_action_id) if task.reply_action_id else None
    if task.task_type != "chat_reply" or not action:
        raise AppError("REPLY_READ_FAILURE_SCOPE_INVALID", "仅支持原回复任务的读取失败", 409)
    lock_leads(db, [task.lead_id])
    lock_sequence_conversation(db, action.conversation_id)
    action = db.scalar(select(ReplyAction).where(ReplyAction.id == action.id)
                       .with_for_update().execution_options(populate_existing=True))
    task = db.scalar(select(Task).where(Task.id == task.id)
                     .with_for_update().execution_options(populate_existing=True))
    flow = worker.inflight_flow_state or {}
    validate_inflight_continuation(worker, flow_id)
    if (task.worker_id != worker.id or worker.client_instance_id != client_instance_id
            or not flow_id or flow.get("flow_id") != flow_id
            or flow.get("conversation_id") != action.conversation_id
            or flow.get("flow_kind") not in {"c2_read", "task", "chat_reply"}
            or (flow.get("flow_kind") != "c2_read" and flow_id != task.id)
            or payload.error_code != task_service.TECHNICAL_BUBBLE_GROUPING_ERROR
            or payload.failure_step not in {"pre_send_refresh", "reply_sequence_read"}
            or action.deleted_at or task.deleted_at):
        raise AppError("REPLY_READ_FAILURE_SCOPE_INVALID", "失败回执与原客户、任务或流程不一致", 409)
    record = {
        "task_id": task.id, "conversation_id": action.conversation_id,
        "flow_id": flow_id, "flow_kind": flow["flow_kind"],
        "worker_id": worker.id, "client_instance_id": client_instance_id,
        "lease_fencing_token": int(fencing or 0),
        "error_code": payload.error_code, "failure_step": payload.failure_step,
    }
    supplied = payload.evidence.get("reply_read_failure")
    if supplied is not None and supplied != record:
        raise AppError("REPLY_READ_FAILURE_RECEIPT_MISMATCH", "失败回执身份不一致", 409)
    if int(fencing or 0) != int(task.lease_fencing_token or 0):
        raise AppError("TASK_LEASE_FENCING_STALE", "任务租约 fencing token 已变化", 409)
    # A sending/unknown/sent action must settle through its original sent_ack.
    # A read failure cannot assert that an issued send permission was unused.
    if (action.status not in {"draft", "guarding", "queued", "cancelled", "superseded"}
            or action.send_token
            or db.scalar(select(SentAck.id).where(SentAck.reply_action_id == action.id))):
        raise AppError("REPLY_ACTION_SENT_ACK_REQUIRED", "已签发发送许可，须通过原 sent_ack 结算", 409)
    def state():
        return {"status": task.status, "error_code": task.error_code,
                "failure_step": task.failure_step, "cancel_reason": task.cancel_reason,
                "action_status": action.status}

    event = "reply_read_failure_confirmed"
    previous = db.scalar(select(OperationLog).where(
        OperationLog.event_type == event, OperationLog.target_id == task.id,
        OperationLog.operator_id == worker.id).order_by(OperationLog.created_at.desc()).limit(1))
    if previous:
        if previous.before_data != record or previous.after_data != state():
            raise AppError("REPLY_READ_FAILURE_RECEIPT_MISMATCH", "失败回执与已确认结果不一致", 409)
        return {**task_service.task_to_detail(task), "reply_read_failure": record}
    if task.status == "running":
        # An expired lease forbids new UI work, but not this unchanged owner's
        # unsent terminal receipt. Do not renew the lease or issue another token.
        if (task.lease_owner_worker_id != worker.id
                or task.lease_owner_client_instance_id != client_instance_id):
            raise AppError("TASK_LEASE_OWNER_MISMATCH", "任务租约归属已变化", 409)
    elif task.status not in {"pending", "cancelled"}:
        raise AppError("TASK_FAIL_NOT_ALLOWED", "原回复任务不允许此失败收尾", 409)
    result = task_service.fail_task(db, task.id, payload.error_code, payload.failure_step,
        payload.failure_remark, actor, allow_pending_chat_reply_recovery=True)
    write_log(db, actor, event_type=event, module="tasks", target_type="task",
              target_id=task.id, lead_id=task.lead_id, before_data=record, after_data=state())
    db.flush()
    return {**result, "reply_read_failure": record}
