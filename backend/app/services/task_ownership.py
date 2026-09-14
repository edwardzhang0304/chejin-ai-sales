"""Sales owns unstarted add-friend work; a Worker is its current executor.

Lock order: lead(s), sales (sorted), task(s) (sorted), Worker(s) (sorted).
Settlement keeps the original lease/Flow identity and does not call this new
work gate. Changing an executor must never replay already-started work.
"""
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.errors import AppError
from app.models.base import utcnow
from app.models.sales import Sales
from app.models.task import Task
from app.models.worker import Worker
from app.services.audit_service import write_log


def lock_sales(db: Session, sales_ids) -> dict[str, Sales]:
    ids = sorted({value for value in sales_ids if value})
    if not ids:
        return {}
    return {row.id: row for row in db.scalars(
        select(Sales).where(Sales.id.in_(ids)).order_by(Sales.id)
        .with_for_update().execution_options(populate_existing=True)
    )}


def unclaimed_add_friend(task: Task) -> bool:
    """Task-only evidence; a registered Flow may already exist before claim."""
    return bool(
        task.task_type == "add_friend" and task.status in {"pending", "blocked"}
        and task.claimed_at is None and not task.lease_owner_worker_id
        and not task.lease_owner_client_instance_id
        and not task.lease_expires_at and not int(task.lease_fencing_token or 0)
    )


def current_add_friend_owner(db: Session, task: Task) -> str | None:
    sales = db.get(Sales, task.sales_id) if task.sales_id else None
    if not sales or sales.deleted_at:
        return None
    return sales.worker_id


def lock_task_executors(db: Session, tasks: list[Task], *, worker_ids=()) -> dict[str, Worker]:
    """Called after sales/Task locks; include historical, not just current owners.

    Registration also locks the Task before the Worker. Therefore a new Flow
    cannot appear between this lookup and migration. Match Flow/current_task
    too, so a historical rewritten Task.worker_id cannot hide its old owner.
    """
    task_ids = [task.id for task in tasks]
    ids = set(worker_ids)
    for task in tasks:
        ids.update([task.worker_id, task.lease_owner_worker_id, current_add_friend_owner(db, task)])
    ids.discard(None)
    return {row.id: row for row in db.scalars(
        select(Worker).where(or_(
            Worker.id.in_(sorted(ids)),
            Worker.current_task.in_(task_ids),
            Worker.inflight_flow_state["flow_id"].as_string().in_(task_ids),
        )).order_by(Worker.id).with_for_update().execution_options(populate_existing=True)
    )}


def has_unsettled_task_flow(task: Task, executors: dict[str, Worker], *, except_worker_id: str | None = None) -> bool:
    return any(
        worker.id != except_worker_id and (
            (worker.inflight_flow_state or {}).get("flow_id") == task.id
            or worker.current_task == task.id
        ) for worker in executors.values()
    )


def can_reassign_add_friend(task: Task, executors: dict[str, Worker]) -> bool:
    return unclaimed_add_friend(task) and not has_unsettled_task_flow(task, executors)


def task_owner_matches(db: Session, task: Task, worker_id: str, *, executors: dict[str, Worker]) -> bool:
    if task.task_type != "add_friend":
        return not task.worker_id or task.worker_id == worker_id
    return bool(current_add_friend_owner(db, task) == worker_id
                and task.worker_id == worker_id
                and not has_unsettled_task_flow(task, executors, except_worker_id=worker_id))


def require_task_owner(db: Session, task: Task, worker_id: str, *, allow_started: bool = False) -> None:
    executors = lock_task_executors(db, [task], worker_ids=[worker_id]) if task.task_type == "add_friend" else {}
    if allow_started and task.task_type == "add_friend" and (
        not unclaimed_add_friend(task) or has_unsettled_task_flow(task, executors)
    ):
        # A registered/started task can only reconcile under its original
        # executor, even if historical sales data already changed elsewhere.
        matches = task.worker_id == worker_id and not has_unsettled_task_flow(
            task, executors, except_worker_id=worker_id,
        )
    else:
        matches = task_owner_matches(db, task, worker_id, executors=executors)
    if not matches:
        raise AppError("TASK_WORKER_MISMATCH", "任务所属销售已更换 Worker，请重新获取待办", 409)


def synchronize_unstarted_task(db: Session, task: Task, actor, *, executors: dict[str, Worker]) -> bool:
    """Only reconcile never-started work, using the same owner as claim/pull."""
    if not can_reassign_add_friend(task, executors):
        return False
    owner = current_add_friend_owner(db, task)
    worker = db.get(Worker, owner) if owner else None
    usable = bool(worker and not worker.deleted_at and worker.enabled)
    before = {"worker_id": task.worker_id, "status": task.status, "block_code": task.block_code}
    task.worker_id = owner
    task.worker = worker
    if task.status == "pending" and not usable:
        task.status, task.block_code = "blocked", "SALES_WORKER_NOT_BOUND"
    elif task.status == "blocked" and task.block_code == "SALES_WORKER_NOT_BOUND" and usable:
        # Invalid leads remain subject to their separate business gate.
        from app.services.followup_eligibility import followup_block_reason
        if not followup_block_reason(db, task.lead_id, lock=False):
            task.status, task.block_code = "pending", None
    after = {"worker_id": task.worker_id, "status": task.status, "block_code": task.block_code}
    if before == after:
        return False
    task.updated_by, task.updated_at = str(actor.operator_id), utcnow()
    from app.enums import TaskEventType
    from app.services.task_service import _write_event
    if before["status"] != task.status:
        event = TaskEventType.blocked if task.status == "blocked" else TaskEventType.unblocked
        _write_event(db, task, event, actor=actor, from_status=before["status"],
                     to_status=task.status, remark="按销售当前 Worker 归属保留未开始的加好友待办")
    write_log(db, actor, event_type="task_worker_reassigned", module="tasks",
              target_type="task", target_id=task.id, before_data=before, after_data=after)
    return True


def prepare_sales_rebinding(db: Session, sales_id: str, new_worker_id: str | None) -> tuple[Sales, list[Task], dict[str, Worker]]:
    from app.services.followup_eligibility import lock_leads
    lock_leads(db, db.scalars(select(Task.lead_id).where(Task.sales_id == sales_id)))
    sales = lock_sales(db, [sales_id]).get(sales_id)
    if sales is None or sales.deleted_at:
        raise AppError("SALES_NOT_FOUND", "销售不存在", 404)
    tasks = list(db.scalars(
        select(Task).where(Task.sales_id == sales_id, Task.deleted_at.is_(None))
        .order_by(Task.id).with_for_update().execution_options(populate_existing=True)
    ))
    worker_ids = sorted({value for value in [sales.worker_id, new_worker_id] if value})
    executors = lock_task_executors(db, tasks, worker_ids=worker_ids)
    if sales.worker_id == new_worker_id and not any(
        unclaimed_add_friend(task) and task.worker_id != new_worker_id for task in tasks
    ):
        return sales, tasks, executors
    unsettled = [task.id for task in tasks
                 if has_unsettled_task_flow(task, executors) or task.status == "running" or (
                     task.task_type == "add_friend" and task.status in {"pending", "blocked"}
                     and not can_reassign_add_friend(task, executors))]
    active_workers = [worker.id for worker in executors.values()
                      if worker.id in worker_ids and (
                          (worker.inflight_flow_state or {}).get("flow_id") or worker.current_task)]
    # A formerly unbound Worker can still have another sales person's old
    # running task. Do not re-use that machine before the original result ends.
    active_workers.extend(db.scalars(select(Task.worker_id).where(
        Task.worker_id.in_(worker_ids), Task.status == "running", Task.deleted_at.is_(None))))
    if unsettled or active_workers:
        raise AppError("SALES_WORKER_REBIND_UNSETTLED", "仍有已开始的任务或流程未收尾，请完成原任务后再换绑", 409,
                       {"task_ids": unsettled, "worker_ids": sorted(set(active_workers))})
    return sales, tasks, executors
