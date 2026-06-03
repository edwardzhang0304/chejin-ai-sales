from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.request_context import ActorContext
from app.enums import AssignmentResult, AssignmentType, AssignStatus, LeadStatus
from app.models.lead import AssignmentRoundRobinState, Lead, LeadAssignment
from app.models.sales import Sales
from app.models.base import utcnow
from app.services.audit_service import write_log


def _available_sales(db: Session) -> list[Sales]:
    return list(
        db.scalars(
            select(Sales)
            .where(
                Sales.enabled.is_(True),
                Sales.deleted_at.is_(None),
                Sales.sales_name != "",
            )
            .order_by(Sales.sort_order.is_(None), Sales.sort_order.asc(), Sales.id.asc())
        )
    )


def _get_state_for_update(db: Session) -> AssignmentRoundRobinState:
    state = db.scalar(select(AssignmentRoundRobinState).where(AssignmentRoundRobinState.id == 1).with_for_update())
    if not state:
        state = AssignmentRoundRobinState(id=1)
        db.add(state)
        db.flush()
    return state


def assign_lead_round_robin(
    db: Session,
    lead: Lead,
    actor: ActorContext,
    assignment_type: AssignmentType = AssignmentType.round_robin,
) -> LeadAssignment:
    state = _get_state_for_update(db)
    sales_list = _available_sales(db)
    cursor_before = state.current_sales_id

    if not sales_list:
        lead.status = LeadStatus.unassigned.value
        lead.sales_id = None
        lead.assigned_at = None
        lead.assign_status = AssignStatus.assign_failed.value
        lead.assign_failure_reason = "无可用销售"
        assignment = LeadAssignment(
            lead_id=lead.id,
            assignment_type=assignment_type.value,
            assignment_status=AssignmentResult.failed.value,
            failure_reason="无可用销售",
            round_robin_cursor_before=cursor_before,
            round_robin_cursor_after=cursor_before,
            operator_id=str(actor.operator_id) if assignment_type == AssignmentType.retry_round_robin else None,
            remark="没有启用销售",
        )
        db.add(assignment)
        write_log(
            db,
            actor,
            event_type="lead_assign_failed",
            module="assignment",
            target_type="lead",
            target_id=lead.id,
            lead_id=lead.id,
            metadata={"reason": "无可用销售"},
        )
        return assignment

    selected_index = 0
    if cursor_before:
        ids = [sales.id for sales in sales_list]
        if cursor_before in ids:
            selected_index = ids.index(cursor_before)

    selected = sales_list[selected_index]
    next_sales = sales_list[(selected_index + 1) % len(sales_list)]
    state.current_sales_id = next_sales.id
    state.updated_at = utcnow()

    from_sales_id = lead.sales_id
    lead.sales_id = selected.id
    lead.status = LeadStatus.assigned.value
    lead.assigned_at = utcnow()
    lead.assign_status = AssignStatus.assigned.value
    lead.assign_failure_reason = None

    assignment = LeadAssignment(
        lead_id=lead.id,
        from_sales_id=from_sales_id,
        to_sales_id=selected.id,
        assignment_type=assignment_type.value,
        assignment_status=AssignmentResult.succeeded.value,
        round_robin_cursor_before=cursor_before,
        round_robin_cursor_after=next_sales.id,
        operator_id=str(actor.operator_id) if assignment_type == AssignmentType.retry_round_robin else None,
        remark=f"轮询自动分配给 {selected.sales_name}",
    )
    db.add(assignment)
    write_log(
        db,
        actor,
        event_type="lead_auto_assigned" if assignment_type == AssignmentType.round_robin else "lead_retry_assign",
        module="assignment",
        target_type="lead",
        target_id=lead.id,
        lead_id=lead.id,
        metadata={"sales_id": selected.id, "sales_name": selected.sales_name},
    )
    return assignment


def retry_auto_assign(db: Session, lead_ids: list[str], actor: ActorContext) -> dict:
    query = select(Lead).where(Lead.status == LeadStatus.unassigned.value)
    if lead_ids:
        query = query.where(Lead.id.in_(lead_ids))
    leads = list(db.scalars(query.order_by(Lead.created_at.asc())).all())
    requested = len(lead_ids) if lead_ids else len(leads)
    results = []
    succeeded = failed = skipped = 0

    found_ids = {lead.id for lead in leads}
    for requested_id in lead_ids:
        if requested_id not in found_ids:
            skipped += 1
            results.append(
                {
                    "lead_id": requested_id,
                    "result": "skipped",
                    "status": "skipped",
                    "reason": "线索不存在或当前状态不可重新分配",
                }
            )

    for lead in leads:
        assignment = assign_lead_round_robin(db, lead, actor, AssignmentType.retry_round_robin)
        if assignment.assignment_status == AssignmentResult.succeeded.value:
            succeeded += 1
            sales = db.get(Sales, assignment.to_sales_id) if assignment.to_sales_id else None
            results.append(
                {
                    "lead_id": lead.id,
                    "result": "succeeded",
                    "status": "succeeded",
                    "sales_id": assignment.to_sales_id,
                    "sales_name": sales.sales_name if sales else None,
                    "failure_reason": None,
                }
            )
        else:
            failed += 1
            results.append(
                {
                    "lead_id": lead.id,
                    "result": "failed",
                    "status": "failed",
                    "sales_id": None,
                    "sales_name": None,
                    "failure_reason": assignment.failure_reason,
                }
            )

    return {"requested": requested, "succeeded": succeeded, "failed": failed, "skipped": skipped, "items": results}


def round_robin_state(db: Session) -> dict:
    state = db.scalar(select(AssignmentRoundRobinState).where(AssignmentRoundRobinState.id == 1))
    current_sales = db.get(Sales, state.current_sales_id) if state and state.current_sales_id else None
    available_count = db.scalar(
        select(func.count())
        .select_from(Sales)
        .where(
            Sales.enabled.is_(True),
            Sales.deleted_at.is_(None),
        )
    )
    round_robin_sales = db.execute(
        select(Sales, func.count(Lead.id))
        .outerjoin(Lead, (Lead.sales_id == Sales.id) & (Lead.deleted_at.is_(None)))
        .where(
            Sales.enabled.is_(True),
            Sales.deleted_at.is_(None),
        )
        .group_by(Sales.id)
        .order_by(Sales.sort_order.is_(None), Sales.sort_order.asc(), Sales.id.asc())
    ).all()
    return {
        "current_cursor_sales_id": state.current_sales_id if state else None,
        "current_cursor_sales_name": current_sales.sales_name if current_sales else None,
        "enabled_sales_count": available_count or 0,
        "round_robin_sales": [
            {"id": sales.id, "sales_name": sales.sales_name, "sort_order": sales.sort_order, "lead_count": lead_count}
            for sales, lead_count in round_robin_sales
        ],
        "updated_at": state.updated_at if state else None,
    }
