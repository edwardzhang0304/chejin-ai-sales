# Frozen released 0.9.79 functions: synthetic historical setup only.
# Source commit: 9c4871d900dd4abcb46c7788797ba7a63a3141cc
# Executed with the matching service globals, not imported as product code.

# backend/app/services/sales_service.py function SHA256 bdb40489d390c207eea35ace64c4bee50d8eeeab9977f951f386169c0d2fb8d3
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

# backend/app/services/task_service.py function SHA256 76e8a47197d288e882b12150ca61c11e50c61e0c964e52b0e067127b69f44076
def unblock_sales_worker_tasks(db: Session, sales_id: str, worker_id: str, actor: ActorContext) -> int:
    from app.services.followup_eligibility import lock_leads, followup_block_reason
    lock_leads(db, db.scalars(select(Task.lead_id).where(Task.sales_id == sales_id)))
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
    unblocked_count = 0
    for task in rows:
        if followup_block_reason(db, task.lead_id):
            continue
        unblocked_count += 1
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
    return unblocked_count
