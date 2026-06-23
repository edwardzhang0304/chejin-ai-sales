from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.request_context import ActorContext
from app.errors import AppError
from app.models.base import utcnow
from app.models.lead import Lead
from app.models.sales import Sales
from app.models.worker import Worker
from app.schemas.sales import SalesWorkerBindRequest
from app.schemas.sales import SalesUpsert
from app.services.audit_service import write_log
from app.services.worker_service import worker_summary


def _mask_phone(value: str | None) -> str | None:
    if not value:
        return None
    digits = "".join(ch for ch in value if ch.isdigit())
    if len(digits) == 11:
        return f"{digits[:3]}****{digits[-4:]}"
    return value


def list_sales(db: Session) -> list[dict]:
    rows = db.execute(
        select(Sales, func.count(Lead.id))
        .outerjoin(Lead, (Lead.sales_id == Sales.id) & (Lead.deleted_at.is_(None)))
        .where(Sales.deleted_at.is_(None))
        .group_by(Sales.id)
        .order_by(Sales.sort_order.is_(None), Sales.sort_order.asc(), Sales.created_at.desc())
    ).all()
    return [
        {
            "id": sales.id,
            "sales_name": sales.sales_name,
            "phone": _mask_phone(sales.phone),
            "wechat": sales.wechat,
            "feishu_user_id": sales.feishu_user_id,
            "worker_id": sales.worker_id,
            "current_worker": worker_summary(db, sales.worker, include_token=False),
            "enabled": sales.enabled,
            "sort_order": sales.sort_order,
            "remark": sales.remark,
            "lead_count": lead_count,
        }
        for sales, lead_count in rows
    ]


def create_sales(db: Session, payload: SalesUpsert, actor: ActorContext) -> Sales:
    data = payload.model_dump()
    worker_id = data.pop("worker_id", None)
    sales = Sales(**data)
    db.add(sales)
    db.flush()
    if worker_id:
        bind_worker(db, sales.id, SalesWorkerBindRequest(worker_id=worker_id), actor)
    write_log(
        db,
        actor,
        event_type="sales_created",
        module="sales",
        target_type="sales",
        target_id=sales.id,
        after_data={"sales_name": sales.sales_name, "enabled": sales.enabled},
    )
    return sales


def get_sales_detail(db: Session, sales_id: str) -> dict:
    sales = db.get(Sales, sales_id)
    if not sales or sales.deleted_at:
        raise AppError("SALES_NOT_FOUND", "销售不存在", 404)

    from app.enums import TaskBlockCode, TaskStatus, TaskType
    from app.models.task import Task

    today_start = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    today_assigned = (
        db.scalar(
            select(func.count())
            .select_from(Lead)
            .where(
                Lead.sales_id == sales.id,
                Lead.assigned_at >= today_start,
                Lead.deleted_at.is_(None),
            )
        )
        or 0
    )
    blocking_task_count = (
        db.scalar(
            select(func.count())
            .select_from(Task)
            .where(
                Task.sales_id == sales.id,
                Task.task_type == TaskType.add_friend.value,
                Task.status == TaskStatus.blocked.value,
                Task.block_code == TaskBlockCode.SALES_WORKER_NOT_BOUND.value,
                Task.deleted_at.is_(None),
            )
        )
        or 0
    )
    return {
        "id": sales.id,
        "sales_name": sales.sales_name,
        "phone": _mask_phone(sales.phone),
        "wechat": sales.wechat,
        "feishu_user_id": sales.feishu_user_id,
        "enabled": sales.enabled,
        "sort_order": sales.sort_order,
        "remark": sales.remark,
        "worker_id": sales.worker_id,
        "current_worker": worker_summary(db, sales.worker, include_token=False),
        "worker_status": worker_summary(db, sales.worker, include_token=False),
        "worker_last_heartbeat_at": sales.worker.last_heartbeat_at if sales.worker else None,
        "today_assignment_count": today_assigned,
        "blocking_task_count": blocking_task_count,
        "created_at": sales.created_at,
        "updated_at": sales.updated_at,
    }


def _validate_worker_for_binding(db: Session, worker_id: str, sales_id: str) -> Worker:
    worker = db.get(Worker, worker_id)
    if not worker or worker.deleted_at:
        raise AppError("WORKER_NOT_FOUND", "Worker 不存在", 404)
    if not worker.enabled:
        raise AppError("WORKER_DISABLED_CANNOT_BIND", "已停用 Worker 不可被选择绑定", 400)

    bound_sales = db.scalar(
        select(Sales).where(
            Sales.worker_id == worker_id,
            Sales.id != sales_id,
            Sales.deleted_at.is_(None),
        )
    )
    if bound_sales:
        raise AppError("WORKER_ALREADY_BOUND", "该 Worker 已绑定其他销售，不可重复绑定", 409)
    return worker


def bind_worker(db: Session, sales_id: str, payload: SalesWorkerBindRequest, actor: ActorContext) -> dict:
    sales = db.get(Sales, sales_id)
    if not sales or sales.deleted_at:
        raise AppError("SALES_NOT_FOUND", "销售不存在", 404)

    before = {"worker_id": sales.worker_id}
    if payload.worker_id:
        _validate_worker_for_binding(db, payload.worker_id, sales.id)
        sales.worker_id = payload.worker_id
    else:
        sales.worker_id = None
    db.flush()

    after = {"worker_id": sales.worker_id}
    event_type = "sales_worker_unbound" if after["worker_id"] is None else "sales_worker_bound"
    write_log(
        db,
        actor,
        event_type=event_type,
        module="sales",
        target_type="sales",
        target_id=sales.id,
        before_data=before,
        after_data=after,
    )
    if after["worker_id"]:
        from app.services.task_service import unblock_sales_worker_tasks

        unblock_sales_worker_tasks(db, sales.id, after["worker_id"], actor)
    return get_sales_detail(db, sales.id)


def update_sales(db: Session, sales_id: str, payload: SalesUpsert, actor: ActorContext) -> Sales:
    sales = db.get(Sales, sales_id)
    if not sales or sales.deleted_at:
        raise AppError("SALES_NOT_FOUND", "销售不存在", 404)

    before = {
        "sales_name": sales.sales_name,
        "enabled": sales.enabled,
        "sort_order": sales.sort_order,
    }
    data = payload.model_dump(exclude_unset=True)
    worker_id_provided = "worker_id" in data
    worker_id = data.pop("worker_id", None)
    for key, value in data.items():
        setattr(sales, key, value)
    db.flush()
    if worker_id_provided:
        bind_worker(db, sales.id, SalesWorkerBindRequest(worker_id=worker_id), actor)

    after = {
        "sales_name": sales.sales_name,
        "enabled": sales.enabled,
        "sort_order": sales.sort_order,
        "worker_id": sales.worker_id,
    }
    event_type = "sales_updated"
    if before["enabled"] != after["enabled"]:
        event_type = "sales_enabled_changed"

    write_log(
        db,
        actor,
        event_type=event_type,
        module="sales",
        target_type="sales",
        target_id=sales.id,
        before_data=before,
        after_data=after,
    )
    return sales
