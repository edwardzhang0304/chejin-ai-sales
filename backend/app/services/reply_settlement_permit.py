"""Retrieve an existing send permit for receipts; never authorize a send.

Expiry and business revocation do not erase historical identity. Task fencing,
client identity and original Flow must still be proved. This path performs no
task/authorization/vehicle eligibility transitions and returns no reply body.
"""
from sqlalchemy import select

from app.errors import AppError
from app.models.c3 import Conversation, ReplyAction, SentAck
from app.models.task import Task
from app.models.wechat import WechatSessionBinding
from app.models.worker import Worker


def claim_identity(*, worker, binding, action, task, client_instance_id):
    flow = worker.inflight_flow_state or {}
    if (flow.get("status") not in {"active", "draining"}
            or not flow.get("flow_id")
            or not (flow.get("conversation_id") == action.conversation_id
                    or (flow.get("flow_kind") in {"task", "chat_reply"}
                        and flow["flow_id"] == task.id))
            or task.lease_owner_worker_id != worker.id
            or task.lease_owner_client_instance_id != client_instance_id
            or not task.lease_fencing_token):
        return None
    return {"version": 1, "reply_action_id": action.id, "task_id": task.id,
            "worker_id": worker.id, "client_instance_id": client_instance_id,
            "conversation_id": action.conversation_id, "binding_id": binding.id,
            "flow_id": flow["flow_id"], "lease_fencing_token": task.lease_fencing_token,
            "reply_text_hash": action.reply_text_hash}


def _identity_error():
    raise AppError("REPLY_ACTION_SETTLEMENT_IDENTITY_UNCONFIRMED", "无法核实原发送许可归属", 409)


def original_permit(db, *, worker, reply_action_id, task_id,
                    client_instance_id, flow_id, lease_fencing_token):
    from app.services.followup_eligibility import lock_leads

    task = db.get(Task, task_id)
    if not task or task.deleted_at or task.task_type != "chat_reply" or task.reply_action_id != reply_action_id:
        _identity_error()
    lock_leads(db, [task.lead_id])
    conversation_id = db.scalar(select(ReplyAction.conversation_id).where(ReplyAction.id == reply_action_id))
    binding = db.scalar(select(WechatSessionBinding).where(
        WechatSessionBinding.conversation_id == conversation_id,
        WechatSessionBinding.deleted_at.is_(None)).with_for_update().execution_options(populate_existing=True))
    if not binding:
        _identity_error()
    owners = list(db.scalars(select(Worker).where(Worker.id.in_({worker.id, binding.worker_id}))
                  .order_by(Worker.id).with_for_update().execution_options(populate_existing=True)))
    worker = next(row for row in owners if row.id == worker.id)
    db.scalar(select(Conversation).where(Conversation.conversation_id == conversation_id)
              .with_for_update().execution_options(populate_existing=True))
    action = db.scalar(select(ReplyAction).where(ReplyAction.id == reply_action_id)
                       .with_for_update().execution_options(populate_existing=True))
    task = db.scalar(select(Task).where(Task.id == task_id).with_for_update().execution_options(populate_existing=True))
    if (not action or action.deleted_at or action.conversation_id != conversation_id
            or task.worker_id != worker.id or binding.worker_id != worker.id
            or worker.client_instance_id != client_instance_id):
        _identity_error()
    capability = ((worker.local_lock_summary or {}).get("capabilities") or {}).get("pre_send_read_recovery_version")
    if type(capability) is not int or capability != 1:
        raise AppError("WORKER_PRE_SEND_READ_RECOVERY_UNSUPPORTED", "客户端未声明原许可结算能力", 409)
    if not action.send_token:
        raise AppError("REPLY_ACTION_ORIGINAL_PERMIT_MISSING", "原动作未签发发送许可", 409)
    if action.claimed_task_id != task.id or action.claimed_by_worker_id != worker.id:
        _identity_error()
    saved = (action.ai_payload or {}).get("send_claim_identity")
    if saved is None:
        # Older actions may use intact claim + task lease + live original Flow.
        # Never backfill a missing client/fencing/Flow from request defaults.
        saved = claim_identity(worker=worker, binding=binding, action=action,
                               task=task, client_instance_id=client_instance_id)
    expected = {"version": 1, "reply_action_id": action.id, "task_id": task.id,
                "worker_id": worker.id, "client_instance_id": client_instance_id,
                "conversation_id": action.conversation_id, "binding_id": binding.id,
                "flow_id": flow_id, "lease_fencing_token": lease_fencing_token,
                "reply_text_hash": action.reply_text_hash}
    if (not isinstance(saved, dict) or type(saved.get("version")) is not int
            or saved != expected or not flow_id
            or type(lease_fencing_token) is not int or lease_fencing_token <= 0
            or task.lease_fencing_token != lease_fencing_token):
        _identity_error()
    ack = db.scalar(select(SentAck).where(SentAck.reply_action_id == action.id)
                    .with_for_update().execution_options(populate_existing=True))
    if ack and (ack.task_id != task.id or ack.worker_id != worker.id
                or ack.client_instance_id != client_instance_id
                or ack.send_token != action.send_token):
        _identity_error()
    return {"settlement_only": True, "send_allowed": False,
            "reply_action_id": action.id, "task_id": task.id,
            "conversation_id": action.conversation_id, "flow_id": flow_id,
            "lease_fencing_token": lease_fencing_token,
            "send_token": action.send_token, "reply_text_hash": action.reply_text_hash,
            "action_status": action.status,
            "ack": {"id": ack.id, "send_result": ack.send_result,
                    "action_phase": ack.action_phase, "error_code": ack.error_code} if ack else None}
