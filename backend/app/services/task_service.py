from datetime import datetime
import re
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.core.request_context import ActorContext
from app.enums import ContactType, TaskBlockCode, TaskEventType, TaskResultCode, TaskStatus, TaskType
from app.errors import AppError
from app.models.lead import Lead, LeadContact
from app.models.sales import Sales
from app.models.task import Task, TaskEvent, TaskEvidence, TaskNote
from app.models.worker import Worker
from app.schemas.task import TERMINAL_TASK_STATUSES
from app.models.base import utcnow
from app.services.audit_service import write_log
from app.services import contact_utils


ACTIVE_TASK_STATUSES = {TaskStatus.blocked.value, TaskStatus.pending.value, TaskStatus.running.value}
CANCELLABLE_TASK_STATUSES = {TaskStatus.blocked.value, TaskStatus.pending.value, TaskStatus.running.value}
RETRYABLE_TASK_STATUSES = {TaskStatus.failed.value, TaskStatus.cancelled.value}


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
    compact_id = re.sub(r"[^0-9A-Za-z]+", "", task.id).upper()
    return f"CJ{compact_id[:6]}"


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


def available_actions(task: Task) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = [{"code": "comment", "label": "补充备注", "enabled": True}]
    if task.status in CANCELLABLE_TASK_STATUSES:
        actions.append({"code": "cancel", "label": "取消任务", "enabled": True})
    if task.status in RETRYABLE_TASK_STATUSES:
        actions.append({"code": "retry", "label": "重新处理", "enabled": True})
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
        "customer_name": lead["customer_name"] if lead else None,
        "primary_phone_masked": lead["primary_phone_masked"] if lead else None,
        "phone_suffix": lead["phone_suffix"] if lead else None,
        "sales_id": task.sales_id,
        "sales_name": task.sales.sales_name if task.sales else None,
        "worker_id": task.worker_id,
        "worker_name": task.worker.worker_name if task.worker else None,
        "original_task_id": task.original_task_id,
        "created_at": task.created_at,
        "updated_at": task.updated_at,
        "claimed_at": task.claimed_at,
        "completed_at": task.completed_at,
        "failed_at": task.failed_at,
        "cancelled_at": task.cancelled_at,
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
            "completed_at": task.completed_at,
            "failed_at": task.failed_at,
            "cancelled_at": task.cancelled_at,
        },
        "status_flow": [task_event_to_dict(event) for event in events],
        "events": [task_event_to_dict(event) for event in events],
        "notes": [task_note_to_dict(note) for note in notes],
        "evidences": [task_evidence_to_dict(evidence) for evidence in evidences],
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


def get_task_or_404(db: Session, task_id: str) -> Task:
    task = db.scalar(select(Task).options(*_task_load_options()).where(Task.id == task_id, Task.deleted_at.is_(None)))
    if not task:
        raise AppError("TASK_NOT_FOUND", "任务不存在", 404)
    return task


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
    items = db.scalars(query.offset((page - 1) * page_size).limit(page_size)).all()
    return {"items": [task_to_list_item(task) for task in items], "page": page, "page_size": page_size, "total": total}


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
    _write_event(db, task, TaskEventType.cancelled, actor=actor, from_status=before, to_status=task.status, remark=reason)
    _write_task_log(db, actor, "task_cancelled", task, before_data={"status": before}, after_data={"status": task.status})
    db.flush()
    return task_to_detail(get_task_or_404(db, task.id))


def retry_task(db: Session, task_id: str, remark: str | None, actor: ActorContext) -> dict[str, Any]:
    original = get_task_or_404(db, task_id)
    if original.status not in RETRYABLE_TASK_STATUSES:
        raise AppError("TASK_RETRY_NOT_ALLOWED", "仅 failed、cancelled 任务可重新处理", 409)
    if original.task_type != TaskType.add_friend.value:
        raise AppError("TASK_TYPE_NOT_SUPPORTED", "当前阶段仅支持 add_friend 任务重试", 400)
    sales, worker = _resolve_sales_and_worker(db, original.lead, original.sales_id, None)
    status = TaskStatus.pending.value if worker else TaskStatus.blocked.value
    block_code = None if worker else TaskBlockCode.SALES_WORKER_NOT_BOUND.value
    new_task = Task(
        task_type=original.task_type,
        status=status,
        block_code=block_code,
        lead_id=original.lead_id,
        sales_id=sales.id if sales else original.sales_id,
        worker_id=worker.id if worker else None,
        original_task_id=original.id,
        remark=remark,
        created_by=str(actor.operator_id),
        updated_by=str(actor.operator_id),
    )
    db.add(new_task)
    db.flush()
    _write_event(db, original, TaskEventType.retried, actor=actor, remark=remark, metadata={"new_task_id": new_task.id})
    _write_event(db, new_task, TaskEventType.created, actor=actor, to_status=new_task.status, remark=remark, metadata={"original_task_id": original.id})
    if new_task.status == TaskStatus.blocked.value:
        _write_event(db, new_task, TaskEventType.blocked, actor=actor, to_status=new_task.status, remark="销售未绑定可用 Worker")
    _write_task_log(
        db,
        actor,
        "task_retried",
        original,
        metadata={"new_task_id": new_task.id, "original_task_id": original.id},
    )
    db.flush()
    return {"original_task": task_to_detail(get_task_or_404(db, original.id)), "new_task": task_to_detail(get_task_or_404(db, new_task.id))}


def claim_task(
    db: Session,
    task_id: str,
    worker_id: str,
    current_step: str | None,
    remark: str | None,
    actor: ActorContext,
    *,
    require_worker_ready: bool = False,
) -> dict[str, Any]:
    task = get_task_or_404(db, task_id)
    if task.status != TaskStatus.pending.value:
        raise AppError("TASK_CLAIM_NOT_ALLOWED", "仅 pending 任务可领取", 409)
    worker = db.get(Worker, worker_id)
    if not worker or worker.deleted_at:
        raise AppError("WORKER_NOT_FOUND", "Worker 不存在", 404)
    if not worker.enabled:
        raise AppError("WORKER_DISABLED_CANNOT_CLAIM", "已停用 Worker 不可领取任务", 400)
    if task.worker_id and task.worker_id != worker.id:
        raise AppError("TASK_WORKER_MISMATCH", "该任务已指定其他 Worker", 409)
    if require_worker_ready:
        from app.services.worker_service import worker_can_claim

        can_claim, reason = worker_can_claim(worker)
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
    task.claimed_at = utcnow()
    task.updated_by = str(actor.operator_id)
    worker.running_status = "running"
    worker.current_task = task.id
    _write_event(db, task, TaskEventType.claimed, actor=actor, from_status=before, to_status=task.status, worker_id=worker.id, remark=remark)
    db.flush()
    return task_to_detail(get_task_or_404(db, task.id))


def pull_task_for_worker(db: Session, worker: Worker) -> dict[str, Any]:
    running_task = _running_task_for_worker(db, worker.id)
    if running_task:
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
        .order_by(Task.created_at.asc(), Task.id.asc())
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
    if task.worker:
        task.worker.running_status = "idle"
        if task.worker.current_task == task.id:
            task.worker.current_task = None
    _write_event(db, task, TaskEventType.completed, actor=actor, from_status=before, to_status=task.status, remark=remark)
    db.flush()
    return task_to_detail(get_task_or_404(db, task.id))


def fail_task(db: Session, task_id: str, error_code: str, failure_step: str | None, failure_remark: str | None, actor: ActorContext) -> dict[str, Any]:
    task = get_task_or_404(db, task_id)
    if task.status != TaskStatus.running.value:
        raise AppError("TASK_FAIL_NOT_ALLOWED", "仅 running 任务可标记失败", 409)
    before = task.status
    task.status = TaskStatus.failed.value
    task.error_code = error_code
    task.failure_step = failure_step
    task.failure_remark = failure_remark
    task.failed_at = utcnow()
    task.updated_by = str(actor.operator_id)
    if task.worker:
        task.worker.running_status = "idle"
        if task.worker.current_task == task.id:
            task.worker.current_task = None
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
