from datetime import datetime, timedelta, timezone
import hashlib
import re
from typing import Any
from uuid import UUID

from sqlalchemy import case, func, or_, select
from sqlalchemy.orm import Session, object_session, selectinload

from app.core.request_context import ActorContext
from app.core.config import get_settings
from app.enums import ContactType, TaskBlockCode, TaskEventType, TaskResultCode, TaskStatus, TaskType
from app.errors import AppError
from app.models.lead import Lead, LeadContact
from app.models.sales import Sales
from app.models.c3 import HandoffEvent, MessageBatch, ReplyAction, SentAck
from app.models.task import Task, TaskEvent, TaskEvidence, TaskNote
from app.models.worker import Worker
from app.schemas.task import TERMINAL_TASK_STATUSES
from app.models.base import utcnow
from app.services.audit_service import write_log
from app.services import contact_utils


ACTIVE_TASK_STATUSES = {TaskStatus.blocked.value, TaskStatus.pending.value, TaskStatus.running.value}
CANCELLABLE_TASK_STATUSES = {TaskStatus.blocked.value, TaskStatus.pending.value, TaskStatus.running.value}
REMARK_CODE_PREFIX = "CJ"
REMARK_CODE_ALPHABET = "ABCDEFGHKMNPRSTUVWXYZ23456789"
SYSTEM_TASK_LEASE_ACTOR = ActorContext(
    operator_id=UUID(int=0),
    operator_name="任务租约恢复器",
    role="system",
    ip_address=None,
    user_agent=None,
    request_id="task-lease-recovery",
)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def task_lease_is_expired(task: Task, *, now: datetime | None = None) -> bool:
    expires_at = _aware(task.lease_expires_at)
    return expires_at is None or expires_at <= _aware(now or utcnow())


def validate_task_lease(
    task: Task,
    *,
    worker_id: str,
    client_instance_id: str | None,
    lease_fencing_token: int | None,
) -> None:
    if task.status != TaskStatus.running.value:
        raise AppError("TASK_LEASE_NOT_RUNNING", "仅 running 任务持有服务端租约", 409)
    if not client_instance_id:
        raise AppError("TASK_LEASE_CLIENT_INSTANCE_REQUIRED", "缺少任务租约客户端实例", 401)
    if (
        task.lease_owner_worker_id != worker_id
        or task.lease_owner_client_instance_id != client_instance_id
    ):
        raise AppError("TASK_LEASE_OWNER_MISMATCH", "当前客户端不是任务租约持有者", 409)
    if int(lease_fencing_token or 0) != int(task.lease_fencing_token or 0):
        raise AppError("TASK_LEASE_FENCING_STALE", "任务租约 fencing token 已失效", 409)
    if task_lease_is_expired(task):
        raise AppError("TASK_LEASE_EXPIRED", "服务端任务租约已过期", 409)


def renew_task_lease(
    db: Session,
    task_id: str,
    *,
    worker_id: str,
    client_instance_id: str | None,
    lease_fencing_token: int,
    current_step: str | None,
) -> dict[str, Any]:
    task = db.scalar(
        select(Task)
        .options(*_task_load_options())
        .where(Task.id == task_id, Task.deleted_at.is_(None))
        .with_for_update()
    )
    if not task:
        raise AppError("TASK_NOT_FOUND", "任务不存在", 404)
    validate_task_lease(
        task,
        worker_id=worker_id,
        client_instance_id=client_instance_id,
        lease_fencing_token=lease_fencing_token,
    )
    now = utcnow()
    task.lease_last_renewed_at = now
    task.lease_expires_at = now + timedelta(seconds=get_settings().task_lease_seconds)
    if current_step:
        task.current_step = current_step
    db.flush()
    return task_to_worker_execution_detail(task)


def _parse_datetime(value: str | None, field_name: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AppError("TASK_TIME_FILTER_INVALID", f"{field_name} 时间格式不正确", 400) from exc


def _primary_contact(lead: Lead | None, contact_type: ContactType) -> LeadContact | None:
    if not lead:
        return None
    typed = [item for item in lead.contacts if item.contact_type == contact_type.value]
    if not typed:
        return None
    return next((item for item in typed if item.is_primary), typed[0])


def _phone_suffix(lead: Lead | None) -> str | None:
    contact = _primary_contact(lead, ContactType.phone)
    if not contact:
        return None
    digits = "".join(ch for ch in contact.contact_value_normalized if ch.isdigit())
    if len(digits) >= 4:
        return digits[-4:]
    if len(contact.masked_value) >= 4:
        return contact.masked_value[-4:]
    return None


def _decrypt_contact_value(contact: LeadContact | None) -> str | None:
    if not contact:
        return None
    return contact_utils.decrypt_for_p0(contact.contact_value_encrypted)


def _lead_summary(task: Task) -> dict[str, Any] | None:
    if not task.lead:
        return None
    primary_phone = _primary_contact(task.lead, ContactType.phone)
    return {
        "id": task.lead.id,
        "customer_name": task.lead.customer_name,
        "status": task.lead.status,
        "primary_phone_masked": primary_phone.masked_value if primary_phone else None,
        "phone_suffix": _phone_suffix(task.lead),
        "remark": task.lead.remark,
    }


def _worker_execution_contact(task: Task) -> dict[str, Any] | None:
    if not task.lead:
        return None
    primary_phone = _primary_contact(task.lead, ContactType.phone)
    primary_wechat = _primary_contact(task.lead, ContactType.wechat)
    return {
        "primary_phone": _decrypt_contact_value(primary_phone),
        "primary_phone_masked": primary_phone.masked_value if primary_phone else None,
        "wechat": _decrypt_contact_value(primary_wechat),
        "wechat_masked": primary_wechat.masked_value if primary_wechat else None,
    }


def _safe_remark_token(value: str | None, *, fallback: str) -> str:
    cleaned = re.sub(r"\s+", "", str(value or ""))
    cleaned = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff_-]+", "", cleaned)
    return cleaned[:8] or fallback


def _task_remark_code(task: Task) -> str:
    custom_fields = task.lead.custom_fields if task.lead and isinstance(task.lead.custom_fields, dict) else {}
    existing = str(custom_fields.get("remark_code") or "").strip()
    if existing:
        return existing
    digest = hashlib.sha256(str(task.id).encode("utf-8")).digest()
    value = int.from_bytes(digest[:8], "big")
    chars: list[str] = []
    for _ in range(6):
        value, index = divmod(value, len(REMARK_CODE_ALPHABET))
        chars.append(REMARK_CODE_ALPHABET[index])
    return f"{REMARK_CODE_PREFIX}{''.join(chars)}"


def _worker_add_friend_formal_fields(task: Task) -> dict[str, Any]:
    sales_name = task.sales.sales_name if task.sales else ""
    sales_token = _safe_remark_token(sales_name, fallback="销售")
    remark_code = _task_remark_code(task)
    remark_name = remark_code
    verify_message = f"您好，我是车金二手车的{sales_token}，您刚咨询过二手车"
    return {
        "verify_message": verify_message,
        "remark_name": remark_name,
        "remark_code": remark_code,
        "remark_code_valid": remark_code in remark_name,
    }


def _sales_summary(task: Task) -> dict[str, Any] | None:
    if not task.sales:
        return None
    return {
        "id": task.sales.id,
        "sales_name": task.sales.sales_name,
        "wechat": task.sales.wechat,
        "enabled": task.sales.enabled,
        "worker_id": task.sales.worker_id,
    }


def _worker_summary(task: Task) -> dict[str, Any] | None:
    if not task.worker:
        return None
    from app.services.worker_service import OFFLINE_TASK_NOTICE_SECONDS, computed_online_status, worker_offline_seconds

    offline_seconds = worker_offline_seconds(task.worker)
    offline_notice = (
        task.status == TaskStatus.running.value
        and offline_seconds is not None
        and offline_seconds >= OFFLINE_TASK_NOTICE_SECONDS
    )
    return {
        "id": task.worker.id,
        "worker_name": task.worker.worker_name,
        "device_name": task.worker.device_name,
        "enabled": task.worker.enabled,
        "online_status": computed_online_status(task.worker),
        "running_status": task.worker.running_status,
        "current_task": task.worker.current_task,
        "last_heartbeat_at": task.worker.last_heartbeat_at,
        "run_status": task.worker.run_status,
        "rpa_component_status": task.worker.rpa_component_status,
        "wechat_status": task.worker.wechat_status,
        "offline_seconds": offline_seconds,
        "offline_notice": "执行方离线超过 10 分钟，建议运营介入" if offline_notice else None,
    }


def _message_batch_summary(batch: MessageBatch | None) -> dict[str, Any] | None:
    if not batch:
        return None
    return {
        "id": batch.id,
        "conversation_id": batch.conversation_id,
        "status": batch.status,
        "active": batch.active,
        "message_count": batch.message_count,
        "trigger_type": batch.trigger_type,
        "recall_cycle_id": batch.recall_cycle_id,
        "generation_no": batch.generation_no,
        "decision": batch.decision,
        "error_code": batch.error_code,
        "suggested_action": batch.suggested_action,
        "superseded_by_batch_id": batch.superseded_by_batch_id,
        "generated_at": batch.generated_at,
        "created_at": batch.created_at,
        "updated_at": batch.updated_at,
    }


def _reply_action_summary(action: ReplyAction | None) -> dict[str, Any] | None:
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
        "reply_text_hash": action.reply_text_hash,
        "confidence": action.confidence,
        "risk_flags": action.risk_flags,
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


def _sent_ack_summary(ack: SentAck | None) -> dict[str, Any] | None:
    if not ack:
        return None
    return {
        "id": ack.id,
        "reply_action_id": ack.reply_action_id,
        "task_id": ack.task_id,
        "worker_id": ack.worker_id,
        "client_instance_id": ack.client_instance_id,
        "send_result": ack.send_result,
        "action_phase": ack.action_phase,
        "reply_text_hash": ack.reply_text_hash,
        "sidecar_run_id": ack.sidecar_run_id,
        "error_code": ack.error_code,
        "remark": ack.remark,
        "sent_at": ack.sent_at,
        "created_at": ack.created_at,
    }


def _handoff_event_summary(event: HandoffEvent | None) -> dict[str, Any] | None:
    if not event:
        return None
    return {
        "id": event.id,
        "conversation_id": event.conversation_id,
        "batch_id": event.batch_id,
        "status": event.status,
        "handoff_reason_code": event.handoff_reason_code,
        "reason_detail": event.reason_detail,
        "risk_flags": event.risk_flags,
        "evidence_refs": event.evidence_refs,
        "notify_error_code": event.notify_error_code,
        "closed_at": event.closed_at,
        "created_at": event.created_at,
        "updated_at": event.updated_at,
    }


def _c3_summary(task: Task) -> dict[str, Any] | None:
    if not task.reply_action_id:
        return None
    db = object_session(task)
    if db is None:
        return None
    action = db.get(ReplyAction, task.reply_action_id)
    batch = db.get(MessageBatch, action.batch_id) if action else None
    ack = db.scalar(select(SentAck).where(SentAck.reply_action_id == task.reply_action_id))
    handoff = None
    if batch:
        handoff = db.scalar(
            select(HandoffEvent)
            .where(HandoffEvent.batch_id == batch.id, HandoffEvent.deleted_at.is_(None))
            .order_by(HandoffEvent.created_at.desc())
        )
    return {
        "message_batch": _message_batch_summary(batch),
        "reply_action": _reply_action_summary(action),
        "sent_ack": _sent_ack_summary(ack),
        "handoff_event": _handoff_event_summary(handoff),
    }


def available_actions(task: Task) -> list[dict[str, Any]]:
    if task.error_code == "SEND_RESULT_UNKNOWN":
        return []
    actions: list[dict[str, Any]] = [{"code": "comment", "label": "补充备注", "enabled": True}]
    if task.status in CANCELLABLE_TASK_STATUSES:
        actions.append({"code": "cancel", "label": "取消任务", "enabled": True})
    if task.status == TaskStatus.pending.value:
        actions.append({"code": "claim", "label": "领取任务", "enabled": True})
    if task.status == TaskStatus.running.value:
        actions.extend(
            [
                {"code": "update_step", "label": "更新步骤", "enabled": True},
                {"code": "invite_sent", "label": "确认已发送邀请", "enabled": task.task_type == TaskType.add_friend.value},
                {"code": "already_friend", "label": "确认已是好友", "enabled": task.task_type == TaskType.add_friend.value},
                {"code": "fail", "label": "记录失败", "enabled": True},
            ]
        )
    if task.status == TaskStatus.blocked.value and task.block_code == TaskBlockCode.SALES_WORKER_NOT_BOUND.value:
        actions.append(
            {
                "code": "resolve_block",
                "label": "处理阻塞",
                "enabled": True,
                "target": {"module": "sales", "sales_id": task.sales_id, "field": "worker_id"},
            }
        )
    return actions


def task_event_to_dict(event: TaskEvent) -> dict[str, Any]:
    return {
        "id": event.id,
        "task_id": event.task_id,
        "event_type": event.event_type,
        "from_status": event.from_status,
        "to_status": event.to_status,
        "result_code": event.result_code,
        "error_code": event.error_code,
        "block_code": event.block_code,
        "current_step": event.current_step,
        "operator_id": event.operator_id,
        "operator_name": event.operator_name,
        "worker_id": event.worker_id,
        "remark": event.remark,
        "metadata": event.extra_metadata,
        "created_at": event.created_at,
    }


def task_note_to_dict(note: TaskNote) -> dict[str, Any]:
    return {
        "id": note.id,
        "task_id": note.task_id,
        "content": note.content,
        "operator_id": note.operator_id,
        "operator_name": note.operator_name,
        "created_at": note.created_at,
    }


def task_evidence_to_dict(evidence: TaskEvidence) -> dict[str, Any]:
    return {
        "id": evidence.id,
        "task_id": evidence.task_id,
        "worker_id": evidence.worker_id,
        "evidence_type": evidence.evidence_type,
        "file_name": evidence.file_name,
        "storage_url": evidence.storage_url,
        "content": evidence.content,
        "error_code": evidence.error_code,
        "remark": evidence.remark,
        "metadata": evidence.extra_metadata,
        "created_at": evidence.created_at,
    }


def _time_sort_value(value: datetime | None) -> float:
    return value.timestamp() if value else 0.0


def _task_process_run_id(task: Task) -> str | None:
    from app.services.observability_service import process_run_id_for_key

    if task.task_type == TaskType.add_friend.value and task.lead_id:
        return process_run_id_for_key("c0_lead", task.lead_id)
    if task.task_type != TaskType.chat_reply.value or not task.reply_action_id:
        return None
    db = object_session(task)
    action = db.get(ReplyAction, task.reply_action_id) if db is not None else None
    batch = db.get(MessageBatch, action.batch_id) if db is not None and action else None
    if batch is None:
        return None
    if db is not None and batch.trace_id:
        from app.models.observability import ProcessStageRun

        linked = db.scalar(
            select(ProcessStageRun)
            .where(
                ProcessStageRun.conversation_id == batch.conversation_id,
                ProcessStageRun.trace_id == batch.trace_id,
            )
            .order_by(ProcessStageRun.created_at.desc())
        )
        if linked is not None:
            return linked.process_run_id
    kind = "c4" if batch.trigger_type == "recall" else "c3"
    return process_run_id_for_key(kind, batch.recall_cycle_id or batch.id)


def task_to_list_item(task: Task) -> dict[str, Any]:
    lead = _lead_summary(task)
    return {
        "id": task.id,
        "task_type": task.task_type,
        "status": task.status,
        "result_code": task.result_code,
        "error_code": task.error_code,
        "block_code": task.block_code,
        "current_step": task.current_step,
        "lead_id": task.lead_id,
        "sales_id": task.sales_id,
        "worker_id": task.worker_id,
        "original_task_id": task.original_task_id,
        "reply_action_id": task.reply_action_id,
        "process_run_id": _task_process_run_id(task),
        "created_at": task.created_at,
        "updated_at": task.updated_at,
        "claimed_at": task.claimed_at,
        "lease_owner_worker_id": task.lease_owner_worker_id,
        "lease_owner_client_instance_id": task.lease_owner_client_instance_id,
        "lease_expires_at": task.lease_expires_at,
        "lease_last_renewed_at": task.lease_last_renewed_at,
        "lease_fencing_token": task.lease_fencing_token,
        "completed_at": task.completed_at,
        "failed_at": task.failed_at,
        "cancelled_at": task.cancelled_at,
        "business_object": {"type": "lead", "lead": lead} if task.lead_id else None,
        "execution": {
            "sales": _sales_summary(task),
            "worker": _worker_summary(task),
            "current_step": task.current_step,
            "claimed_at": task.claimed_at,
            "lease_owner_worker_id": task.lease_owner_worker_id,
            "lease_owner_client_instance_id": task.lease_owner_client_instance_id,
            "lease_expires_at": task.lease_expires_at,
            "lease_last_renewed_at": task.lease_last_renewed_at,
            "lease_fencing_token": task.lease_fencing_token,
            "completed_at": task.completed_at,
            "failed_at": task.failed_at,
            "cancelled_at": task.cancelled_at,
        },
        "available_actions": available_actions(task),
    }


def task_to_detail(task: Task) -> dict[str, Any]:
    events = sorted(task.events, key=lambda item: _time_sort_value(item.created_at))
    notes = sorted(task.notes, key=lambda item: _time_sort_value(item.created_at))
    evidences = sorted(task.evidences, key=lambda item: _time_sort_value(item.created_at))
    return {
        **task_to_list_item(task),
        "remark": task.remark,
        "failure_step": task.failure_step,
        "failure_remark": task.failure_remark,
        "cancel_reason": task.cancel_reason,
        "created_by": task.created_by,
        "updated_by": task.updated_by,
        "business_object": {"type": "lead", "lead": _lead_summary(task)} if task.lead_id else None,
        "execution": {
            "sales": _sales_summary(task),
            "worker": _worker_summary(task),
            "current_step": task.current_step,
            "claimed_at": task.claimed_at,
            "lease_owner_worker_id": task.lease_owner_worker_id,
            "lease_owner_client_instance_id": task.lease_owner_client_instance_id,
            "lease_expires_at": task.lease_expires_at,
            "lease_last_renewed_at": task.lease_last_renewed_at,
            "lease_fencing_token": task.lease_fencing_token,
            "completed_at": task.completed_at,
            "failed_at": task.failed_at,
            "cancelled_at": task.cancelled_at,
        },
        "events": [task_event_to_dict(event) for event in events],
        "notes": [task_note_to_dict(note) for note in notes],
        "evidences": [task_evidence_to_dict(evidence) for evidence in evidences],
        "c3": _c3_summary(task),
        "available_actions": available_actions(task),
    }


def task_to_worker_execution_detail(task: Task) -> dict[str, Any]:
    data = task_to_detail(task)
    contact = _worker_execution_contact(task)
    if contact:
        data.update(contact)
        business_object = data.get("business_object")
        if isinstance(business_object, dict) and isinstance(business_object.get("lead"), dict):
            business_object["lead"].update(contact)
    if task.task_type == TaskType.add_friend.value:
        data.update(_worker_add_friend_formal_fields(task))
    return data


def _task_load_options():
    return [
        selectinload(Task.lead).selectinload(Lead.contacts),
        selectinload(Task.sales),
        selectinload(Task.worker),
        selectinload(Task.events),
        selectinload(Task.notes),
        selectinload(Task.evidences),
    ]


def finish_task_and_release_worker(task: Task) -> None:
    task.lease_owner_worker_id = None
    task.lease_owner_client_instance_id = None
    task.lease_expires_at = None
    task.lease_last_renewed_at = None
    if task.worker:
        task.worker.running_status = "idle"
        if task.worker.current_task == task.id:
            task.worker.current_task = None
        task.worker.current_step = None


def get_task_or_404(db: Session, task_id: str) -> Task:
    task = db.scalar(select(Task).options(*_task_load_options()).where(Task.id == task_id, Task.deleted_at.is_(None)))
    if not task:
        raise AppError("TASK_NOT_FOUND", "任务不存在", 404)
    return task


def _task_claim_statement(task_id: str):
    return (
        select(Task)
        .options(*_task_load_options())
        .where(Task.id == task_id, Task.deleted_at.is_(None))
        .with_for_update()
    )


def list_tasks(
    db: Session,
    *,
    task_type: str | None = None,
    status: str | None = None,
    result_code: str | None = None,
    error_code: str | None = None,
    block_code: str | None = None,
    sales_id: str | None = None,
    worker_id: str | None = None,
    keyword: str | None = None,
    created_from: str | None = None,
    created_to: str | None = None,
    page: int = 1,
    page_size: int = 20,
) -> dict[str, Any]:
    query = (
        select(Task)
        .options(*_task_load_options())
        .outerjoin(Lead, Task.lead_id == Lead.id)
        .outerjoin(LeadContact, LeadContact.lead_id == Lead.id)
        .where(Task.deleted_at.is_(None))
    )
    if task_type:
        query = query.where(Task.task_type == task_type)
    if status:
        query = query.where(Task.status == status)
    if result_code:
        query = query.where(Task.result_code == result_code)
    if error_code:
        query = query.where(Task.error_code == error_code)
    if block_code:
        query = query.where(Task.block_code == block_code)
    if sales_id:
        query = query.where(Task.sales_id == sales_id)
    if worker_id:
        query = query.where(Task.worker_id == worker_id)
    start_at = _parse_datetime(created_from, "created_from")
    end_at = _parse_datetime(created_to, "created_to")
    if start_at:
        query = query.where(Task.created_at >= start_at)
    if end_at:
        query = query.where(Task.created_at <= end_at)
    if keyword:
        cleaned = keyword.strip()
        like = f"%{cleaned}%"
        query = query.where(
            or_(
                Task.id.ilike(like),
                Lead.customer_name.ilike(like),
                LeadContact.contact_value_normalized.ilike(like),
                LeadContact.masked_value.ilike(like),
            )
        )
    query = query.distinct().order_by(Task.created_at.desc(), Task.id.desc())
    total = db.scalar(select(func.count()).select_from(query.order_by(None).subquery())) or 0
    metrics = _task_metrics(db, query)
    items = db.scalars(query.offset((page - 1) * page_size).limit(page_size)).all()
    return {"items": [task_to_list_item(task) for task in items], "page": page, "page_size": page_size, "total": total, "metrics": metrics}


def _task_metrics(db: Session, filtered_query) -> dict[str, int]:
    subquery = filtered_query.order_by(None).subquery()
    today = utcnow().date()
    rows = db.execute(select(subquery.c.status, subquery.c.completed_at, subquery.c.failed_at)).all()
    return {
        "blocked": sum(1 for status, _completed_at, _failed_at in rows if status == TaskStatus.blocked.value),
        "pending": sum(1 for status, _completed_at, _failed_at in rows if status == TaskStatus.pending.value),
        "running": sum(1 for status, _completed_at, _failed_at in rows if status == TaskStatus.running.value),
        "completed_today": sum(
            1
            for status, completed_at, _failed_at in rows
            if status == TaskStatus.completed.value and completed_at is not None and completed_at.date() == today
        ),
        "failed_today": sum(
            1
            for status, _completed_at, failed_at in rows
            if status == TaskStatus.failed.value and failed_at is not None and failed_at.date() == today
        ),
    }


def _write_event(
    db: Session,
    task: Task,
    event_type: TaskEventType,
    *,
    actor: ActorContext | None = None,
    from_status: str | None = None,
    to_status: str | None = None,
    worker_id: str | None = None,
    remark: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> TaskEvent:
    event = TaskEvent(
        task=task,
        event_type=event_type.value,
        from_status=from_status,
        to_status=to_status,
        result_code=task.result_code,
        error_code=task.error_code,
        block_code=task.block_code,
        current_step=task.current_step,
        operator_id=str(actor.operator_id) if actor else None,
        operator_name=actor.operator_name if actor else None,
        worker_id=worker_id or task.worker_id,
        remark=remark,
        extra_metadata=metadata or {},
    )
    db.add(event)
    return event


def _write_task_log(
    db: Session,
    actor: ActorContext,
    event_type: str,
    task: Task,
    *,
    before_data: dict[str, Any] | None = None,
    after_data: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    write_log(
        db,
        actor,
        event_type=event_type,
        module="task",
        target_type="task",
        target_id=task.id,
        lead_id=task.lead_id,
        before_data=before_data,
        after_data=after_data,
        metadata=metadata,
    )


def _running_task_for_worker(db: Session, worker_id: str) -> Task | None:
    return db.scalar(
        select(Task)
        .options(*_task_load_options())
        .where(
            Task.worker_id == worker_id,
            Task.status == TaskStatus.running.value,
            Task.deleted_at.is_(None),
        )
        .order_by(Task.claimed_at.desc(), Task.updated_at.desc())
    )


def _active_add_friend_task(db: Session, lead_id: str) -> Task | None:
    return db.scalar(
        select(Task)
        .options(*_task_load_options())
        .where(
            Task.lead_id == lead_id,
            Task.task_type == TaskType.add_friend.value,
            Task.status.in_(ACTIVE_TASK_STATUSES),
            Task.deleted_at.is_(None),
        )
        .order_by(Task.created_at.desc())
    )


def _resolve_sales_and_worker(db: Session, lead: Lead, sales_id: str | None, worker_id: str | None) -> tuple[Sales | None, Worker | None]:
    sales = db.get(Sales, sales_id or lead.sales_id) if (sales_id or lead.sales_id) else None
    worker: Worker | None = None
    selected_worker_id = worker_id or (sales.worker_id if sales else None)
    if selected_worker_id:
        worker = db.get(Worker, selected_worker_id)
        if worker and (worker.deleted_at or not worker.enabled):
            worker = None
    return sales, worker


def create_add_friend_task(
    db: Session,
    *,
    lead_id: str,
    actor: ActorContext,
    sales_id: str | None = None,
    worker_id: str | None = None,
    remark: str | None = None,
) -> dict[str, Any]:
    lead = db.scalar(select(Lead).options(selectinload(Lead.contacts), selectinload(Lead.sales)).where(Lead.id == lead_id, Lead.deleted_at.is_(None)))
    if not lead:
        raise AppError("LEAD_NOT_FOUND", "线索不存在", 404)

    existing = _active_add_friend_task(db, lead.id)
    if existing:
        return {"created": False, "task": task_to_detail(existing)}

    sales, worker = _resolve_sales_and_worker(db, lead, sales_id, worker_id)
    status = TaskStatus.pending.value if worker else TaskStatus.blocked.value
    block_code = None if worker else TaskBlockCode.SALES_WORKER_NOT_BOUND.value
    task = Task(
        task_type=TaskType.add_friend.value,
        status=status,
        block_code=block_code,
        lead_id=lead.id,
        sales_id=sales.id if sales else lead.sales_id,
        worker_id=worker.id if worker else None,
        remark=remark,
        created_by=str(actor.operator_id),
        updated_by=str(actor.operator_id),
    )
    db.add(task)
    db.flush()
    if lead.custom_fields is None or not isinstance(lead.custom_fields, dict):
        lead.custom_fields = {}
    if not str(lead.custom_fields.get("remark_code") or "").strip():
        lead.custom_fields = {**lead.custom_fields, "remark_code": _task_remark_code(task)}
    _write_event(db, task, TaskEventType.created, actor=actor, to_status=task.status, remark=remark)
    if task.status == TaskStatus.blocked.value:
        _write_event(db, task, TaskEventType.blocked, actor=actor, to_status=task.status, remark="销售未绑定可用 Worker")
    _write_task_log(
        db,
        actor,
        "task_created",
        task,
        after_data={"status": task.status, "task_type": task.task_type, "block_code": task.block_code},
    )
    db.flush()
    return {"created": True, "task": task_to_detail(get_task_or_404(db, task.id))}


def create_add_friend_task_for_lead(db: Session, lead: Lead, actor: ActorContext) -> Task | None:
    result = create_add_friend_task(db, lead_id=lead.id, actor=actor, sales_id=lead.sales_id)
    return get_task_or_404(db, result["task"]["id"]) if result.get("task") else None


def unblock_sales_worker_tasks(db: Session, sales_id: str, worker_id: str, actor: ActorContext) -> int:
    worker = db.get(Worker, worker_id)
    if not worker or worker.deleted_at or not worker.enabled:
        return 0
    rows = list(
        db.scalars(
            select(Task)
            .where(
                Task.sales_id == sales_id,
                Task.task_type == TaskType.add_friend.value,
                Task.status == TaskStatus.blocked.value,
                Task.block_code == TaskBlockCode.SALES_WORKER_NOT_BOUND.value,
                Task.deleted_at.is_(None),
            )
            .with_for_update()
        )
    )
    for task in rows:
        before = task.status
        task.status = TaskStatus.pending.value
        task.worker_id = worker.id
        task.block_code = None
        task.updated_by = str(actor.operator_id)
        _write_event(db, task, TaskEventType.unblocked, actor=actor, from_status=before, to_status=task.status, remark="销售已绑定 Worker")
        _write_task_log(
            db,
            actor,
            "task_unblocked",
            task,
            before_data={"status": before, "block_code": TaskBlockCode.SALES_WORKER_NOT_BOUND.value},
            after_data={"status": task.status, "worker_id": worker.id},
        )
    db.flush()
    return len(rows)


def add_comment(db: Session, task_id: str, content: str, actor: ActorContext) -> dict[str, Any]:
    task = get_task_or_404(db, task_id)
    note = TaskNote(task_id=task.id, content=content, operator_id=str(actor.operator_id), operator_name=actor.operator_name)
    db.add(note)
    _write_event(db, task, TaskEventType.comment_added, actor=actor, remark=content)
    _write_task_log(db, actor, "task_comment_added", task, metadata={"comment_id": note.id})
    db.flush()
    return task_note_to_dict(note)


def cancel_task(db: Session, task_id: str, reason: str | None, actor: ActorContext) -> dict[str, Any]:
    task = get_task_or_404(db, task_id)
    if task.status not in CANCELLABLE_TASK_STATUSES:
        raise AppError("TASK_CANCEL_NOT_ALLOWED", "仅 blocked、pending、running 任务可取消，终态任务不可取消", 409)
    before = task.status
    task.status = TaskStatus.cancelled.value
    task.cancel_reason = reason
    task.cancelled_at = utcnow()
    task.updated_by = str(actor.operator_id)
    finish_task_and_release_worker(task)
    _write_event(db, task, TaskEventType.cancelled, actor=actor, from_status=before, to_status=task.status, remark=reason)
    _write_task_log(db, actor, "task_cancelled", task, before_data={"status": before}, after_data={"status": task.status})
    db.flush()
    return task_to_detail(get_task_or_404(db, task.id))


def claim_task(
    db: Session,
    task_id: str,
    worker_id: str,
    current_step: str | None,
    remark: str | None,
    actor: ActorContext,
    *,
    require_worker_ready: bool = False,
    allow_registered_draining_flow: bool = False,
    claim_source: str | None = None,
    conversation_id: str | None = None,
    client_instance_id: str | None = None,
) -> dict[str, Any]:
    # The task row is the server-side claim boundary. Without a row lock, two
    # transactions can both observe pending and issue different leases.
    task = db.scalar(_task_claim_statement(task_id))
    if not task:
        raise AppError("TASK_NOT_FOUND", "任务不存在", 404)
    if task.status != TaskStatus.pending.value:
        raise AppError("TASK_CLAIM_NOT_ALLOWED", "仅 pending 任务可领取", 409)
    worker = db.get(Worker, worker_id)
    if not worker or worker.deleted_at:
        raise AppError("WORKER_NOT_FOUND", "Worker 不存在", 404)
    if not worker.enabled:
        raise AppError("WORKER_DISABLED_CANNOT_CLAIM", "已停用 Worker 不可领取任务", 400)
    if task.worker_id and task.worker_id != worker.id:
        raise AppError("TASK_WORKER_MISMATCH", "该任务已指定其他 Worker", 409)
    if task.task_type == TaskType.chat_reply.value:
        from app.services.c3_service import validate_chat_reply_task_claim

        validate_chat_reply_task_claim(
            db,
            task,
            worker,
            claim_source=claim_source,
            conversation_id=conversation_id,
        )
    if require_worker_ready:
        from app.services.worker_service import worker_can_claim

        can_claim, reason = worker_can_claim(
            worker,
            allow_registered_draining_flow=(
                allow_registered_draining_flow
            ),
        )
        if not can_claim:
            raise AppError(reason or "WORKER_CANNOT_CLAIM", "当前 Worker 状态不允许领取任务", 409)
    running_task = _running_task_for_worker(db, worker.id)
    if running_task and running_task.id != task.id:
        raise AppError(
            "WORKER_HAS_RUNNING_TASK",
            "该 Worker 已存在 running 任务，客户端应优先恢复当前任务",
            409,
            {"task_id": running_task.id},
        )
    before = task.status
    task.status = TaskStatus.running.value
    task.worker_id = worker.id
    task.current_step = current_step or task.current_step or "claimed"
    now = utcnow()
    task.claimed_at = now
    task.lease_owner_worker_id = worker.id
    task.lease_owner_client_instance_id = (
        client_instance_id
        or worker.client_instance_id
        or f"admin:{actor.operator_id}"
    )
    task.lease_fencing_token = int(task.lease_fencing_token or 0) + 1
    task.lease_last_renewed_at = now
    task.lease_expires_at = now + timedelta(seconds=get_settings().task_lease_seconds)
    task.updated_by = str(actor.operator_id)
    worker.running_status = "running"
    worker.current_task = task.id
    _write_event(db, task, TaskEventType.claimed, actor=actor, from_status=before, to_status=task.status, worker_id=worker.id, remark=remark)
    process_run_id = _task_process_run_id(task)
    if process_run_id:
        from app.core.request_id import get_request_id
        from app.services.observability_service import (
            record_server_stage_best_effort,
        )

        if task.task_type == TaskType.add_friend.value:
            stage_name = "c1.add_friend_queued"
        else:
            c3 = _c3_summary(task) or {}
            batch_summary = (
                c3.get("message_batch")
                if isinstance(c3.get("message_batch"), dict)
                else {}
            )
            stage_name = (
                "c4.reply_queued"
                if str(batch_summary.get("trigger_type") or "") == "recall"
                else "c3.reply_queued"
            )
        record_server_stage_best_effort(
            db,
            process_run_id=process_run_id,
            conversation_id=conversation_id,
            worker_id=worker.id,
            stage_name=stage_name,
            component="backend",
            duration_ms=None,
            queued_at=task.created_at,
            started_at=now,
            ended_at=now,
            trace_id=get_request_id(),
            stable_key=f"{task.id}:{task.lease_fencing_token}",
            attempt=max(1, int(task.lease_fencing_token or 1)),
        )
    db.flush()
    return task_to_detail(get_task_or_404(db, task.id))


def pull_task_for_worker(db: Session, worker: Worker) -> dict[str, Any]:
    running_task = _running_task_for_worker(db, worker.id)
    if running_task:
        if task_lease_is_expired(running_task):
            fail_task(
                db,
                running_task.id,
                "TASK_LEASE_EXPIRED",
                running_task.current_step or "task_lease",
                "服务端任务租约过期；为避免旧客户端继续操作微信，任务已终止且不得自动重放。",
                SYSTEM_TASK_LEASE_ACTOR,
            )
            return {
                "mode": "lease_expired",
                "can_claim": False,
                "reason": "TASK_LEASE_EXPIRED",
                "task": None,
            }
        if (
            running_task.lease_owner_client_instance_id != worker.client_instance_id
        ):
            return {
                "mode": "lease_blocked",
                "can_claim": False,
                "reason": "TASK_LEASE_HELD_BY_OTHER_CLIENT",
                "task": None,
            }
        return {"mode": "running", "can_claim": False, "reason": None, "task": task_to_worker_execution_detail(running_task)}

    from app.services.worker_service import worker_can_claim

    can_claim, reason = worker_can_claim(worker)
    if not can_claim:
        return {"mode": "idle", "can_claim": False, "reason": reason, "task": None}

    pending_task = db.scalar(
        select(Task)
        .options(*_task_load_options())
        .where(
            Task.worker_id == worker.id,
            Task.status == TaskStatus.pending.value,
            Task.deleted_at.is_(None),
        )
        .order_by(
            case(
                (Task.task_type == TaskType.chat_reply.value, 0),
                else_=1,
            ),
            Task.created_at.asc(),
            Task.id.asc(),
        )
    )
    return {
        "mode": "pending" if pending_task else "idle",
        "can_claim": bool(pending_task),
        "reason": None if pending_task else "NO_PENDING_TASK",
        "task": task_to_worker_execution_detail(pending_task) if pending_task else None,
    }


def update_step(db: Session, task_id: str, current_step: str, remark: str | None, actor: ActorContext) -> dict[str, Any]:
    task = get_task_or_404(db, task_id)
    if task.status != TaskStatus.running.value:
        raise AppError("TASK_STEP_NOT_ALLOWED", "仅 running 任务可更新执行步骤", 409)
    task.current_step = current_step
    task.updated_by = str(actor.operator_id)
    _write_event(db, task, TaskEventType.step_updated, actor=actor, from_status=task.status, to_status=task.status, remark=remark)
    db.flush()
    return task_to_detail(get_task_or_404(db, task.id))


def complete_task(db: Session, task_id: str, result_code: TaskResultCode, remark: str | None, actor: ActorContext) -> dict[str, Any]:
    task = get_task_or_404(db, task_id)
    if task.status != TaskStatus.running.value:
        raise AppError("TASK_COMPLETE_NOT_ALLOWED", "仅 running 任务可完成", 409)
    if task.task_type == TaskType.add_friend.value and result_code not in {TaskResultCode.invite_sent, TaskResultCode.already_friend}:
        raise AppError("TASK_RESULT_CODE_INVALID", "add_friend 任务结果码不合法", 400)
    before = task.status
    task.status = TaskStatus.completed.value
    task.result_code = result_code.value
    task.error_code = None
    task.block_code = None
    task.completed_at = utcnow()
    task.updated_by = str(actor.operator_id)
    finish_task_and_release_worker(task)
    _write_event(db, task, TaskEventType.completed, actor=actor, from_status=before, to_status=task.status, remark=remark)
    db.flush()
    return task_to_detail(get_task_or_404(db, task.id))


_PRE_SEND_REIDENTIFICATION_ERRORS = {
    "C2_PRE_SEND_TEXT_CONTENT_UNREADABLE",
    "C2_PRE_SEND_MESSAGE_SEQUENCE_ALIGNMENT_FAILED",
    "C2_PRE_SEND_MESSAGE_ROLE_UNCONFIRMED",
    "C2_PRE_SEND_VOICE_TARGET_NOT_FOUND",
    "C2_PRE_SEND_VOICE_TARGET_AMBIGUOUS",
    "C2_PRE_SEND_IMAGE_TARGET_NOT_FOUND",
    "C2_PRE_SEND_IMAGE_TARGET_AMBIGUOUS",
    "C2_PRE_SEND_MESSAGE_VIEWPORT_CHANGED_AGAIN",
    "C2_PRE_SEND_SYSTEM_CONTENT_UNREADABLE",
    "C2_PRE_SEND_SYSTEM_CLASSIFICATION_UNRESOLVED",
}
_PENDING_CHAT_REPLY_RECOVERY_ERRORS = {
    "C2_REPLY_CONTEXT_MISSING",
    "C2_REPLY_TARGET_NOT_AUTHORIZED",
    "C2_REPLY_CONTEXT_RECOVERY_FAILED",
    "C3_REPLACEMENT_BATCH_MISSING",
    "TASK_LEASE_EXPIRED",
    "C2_PRE_SEND_LAYOUT_INVALID",
    *_PRE_SEND_REIDENTIFICATION_ERRORS,
}
_CHAT_REPLY_RECOVERY_HANDOFF_ERRORS = {
    "C2_REPLY_CONTEXT_MISSING",
    "C2_REPLY_TARGET_NOT_AUTHORIZED",
    "C2_REPLY_CONTEXT_RECOVERY_FAILED",
    "C3_REPLACEMENT_BATCH_MISSING",
    "TASK_LEASE_EXPIRED",
    *_PRE_SEND_REIDENTIFICATION_ERRORS,
}


def fail_task(
    db: Session,
    task_id: str,
    error_code: str,
    failure_step: str | None,
    failure_remark: str | None,
    actor: ActorContext,
    *,
    allow_pending_chat_reply_recovery: bool = False,
) -> dict[str, Any]:
    task = get_task_or_404(db, task_id)
    pending_reply_recovery = (
        allow_pending_chat_reply_recovery
        and task.status == TaskStatus.pending.value
        and task.task_type == TaskType.chat_reply.value
        and error_code in _PENDING_CHAT_REPLY_RECOVERY_ERRORS
    )
    if task.status != TaskStatus.running.value and not pending_reply_recovery:
        raise AppError("TASK_FAIL_NOT_ALLOWED", "仅 running 任务可标记失败", 409)
    before = task.status
    task.status = TaskStatus.failed.value
    task.error_code = error_code
    task.failure_step = failure_step
    task.failure_remark = failure_remark
    task.failed_at = utcnow()
    task.updated_by = str(actor.operator_id)
    if (
        task.task_type == TaskType.chat_reply.value
        and task.reply_action_id
        and (pending_reply_recovery or task.status == TaskStatus.failed.value)
    ):
        action = db.get(ReplyAction, task.reply_action_id)
        if (
            action
            and not action.deleted_at
            and action.status in {"draft", "guarding", "queued"}
        ):
            if error_code in _CHAT_REPLY_RECOVERY_HANDOFF_ERRORS:
                from app.services.c3_service import (
                    handoff_unsent_reply_recovery_failure,
                )

                handoff_unsent_reply_recovery_failure(
                    db,
                    reply_action_id=action.id,
                    error_code=error_code,
                )
            else:
                action.status = "cancelled"
                action.current = False
                action.error_code = error_code
                action.suggested_action = "wait_for_new_authorization"
                batch = db.get(MessageBatch, action.batch_id)
                if batch and not batch.deleted_at:
                    batch.status = "cancelled"
                    batch.active = False
                    batch.retryable = False
                    batch.decision = "no_action"
                    batch.error_code = error_code
                    batch.suggested_action = "wait_for_new_authorization"
    finish_task_and_release_worker(task)
    _write_event(db, task, TaskEventType.failed, actor=actor, from_status=before, to_status=task.status, remark=failure_remark)
    db.flush()
    return task_to_detail(get_task_or_404(db, task.id))


def add_evidence(
    db: Session,
    task_id: str,
    *,
    worker_id: str | None,
    evidence_type: str,
    file_name: str | None,
    storage_url: str | None,
    content: str | None,
    error_code: str | None,
    remark: str | None,
    metadata: dict[str, Any] | None,
) -> dict[str, Any]:
    task = get_task_or_404(db, task_id)
    if worker_id and task.worker_id and task.worker_id != worker_id:
        raise AppError("TASK_WORKER_MISMATCH", "该任务不属于当前 Worker", 409)
    evidence = TaskEvidence(
        task_id=task.id,
        worker_id=worker_id or task.worker_id,
        evidence_type=evidence_type,
        file_name=file_name,
        storage_url=storage_url,
        content=content,
        error_code=error_code,
        remark=remark,
        extra_metadata=metadata or {},
    )
    db.add(evidence)
    db.flush()
    return task_evidence_to_dict(evidence)


def task_events(db: Session, task_id: str) -> list[dict[str, Any]]:
    get_task_or_404(db, task_id)
    rows = db.scalars(select(TaskEvent).where(TaskEvent.task_id == task_id).order_by(TaskEvent.created_at.asc())).all()
    return [task_event_to_dict(row) for row in rows]
