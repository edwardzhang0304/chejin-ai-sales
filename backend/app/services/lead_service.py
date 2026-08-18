from collections import defaultdict
from datetime import datetime, timedelta, timezone
import time
from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.core.request_context import ActorContext
from app.enums import AssignStatus, ContactType, LeadStatus, NoteType
from app.errors import AppError, DuplicateLeadError
from app.models.lead import Lead, LeadAssignment, LeadContact, LeadDuplicateEvent, LeadNote
from app.models.sales import Sales
from app.models.base import utcnow
from app.schemas.lead import LeadCreate, LeadUpdate, MarkInvalidRequest
from app.services import contact_utils
from app.services.assignment_service import assign_lead_round_robin
from app.services.audit_service import write_log


def _normalize_payload_contacts(payload: LeadCreate | LeadUpdate) -> dict[str, list[contact_utils.NormalizedContact]]:
    phones = payload.phones if payload.phones is not None else []
    wechats = payload.wechats if payload.wechats is not None else []
    emails = payload.emails if payload.emails is not None else []

    normalized = {
        "phones": [contact_utils.normalize_phone(v) for v in phones],
        "wechats": [contact_utils.normalize_wechat(v) for v in wechats],
        "emails": [contact_utils.normalize_email(v) for v in emails],
    }
    for contacts in normalized.values():
        seen = set()
        for contact in contacts:
            if contact.normalized.lower() in seen:
                raise AppError("LEAD_CONTACT_DUPLICATED_IN_REQUEST", "该联系方式已填写，请勿重复添加", 400)
            seen.add(contact.normalized.lower())
    return normalized


def _contact_model(lead_id: str, contact_type: ContactType, contact: contact_utils.NormalizedContact, is_primary: bool) -> LeadContact:
    return LeadContact(
        lead_id=lead_id,
        contact_type=contact_type.value,
        contact_value_encrypted=contact.encrypted,
        contact_value_normalized=contact.normalized,
        contact_hash=contact.contact_hash,
        masked_value=contact.masked,
        is_primary=is_primary,
    )


def _primary_contact(lead: Lead, contact_type: ContactType) -> LeadContact | None:
    typed = [c for c in lead.contacts if c.contact_type == contact_type.value]
    if not typed:
        return None
    return next((c for c in typed if c.is_primary), typed[0])


def _lead_summary(lead: Lead) -> dict[str, Any]:
    primary_phone = _primary_contact(lead, ContactType.phone)
    return {
        "id": lead.id,
        "customer_name": lead.customer_name,
        "primary_phone_masked": primary_phone.masked_value if primary_phone else None,
        "sales_id": lead.sales_id,
        "sales_name": lead.sales.sales_name if lead.sales else None,
        "created_at": lead.created_at,
        "updated_at": lead.updated_at,
    }


def _duplicate_dates(db: Session, lead_id: str) -> list[str]:
    rows = db.scalars(
        select(LeadDuplicateEvent.created_at)
        .where(LeadDuplicateEvent.lead_id == lead_id)
        .order_by(LeadDuplicateEvent.created_at.desc())
        .limit(5)
    ).all()
    return sorted({row.astimezone(timezone(timedelta(hours=8))).date().isoformat() for row in rows})


def _find_existing_active_by_phone_hashes(db: Session, phone_hashes: list[str], exclude_lead_id: str | None = None) -> Lead | None:
    query = (
        select(Lead)
        .join(LeadContact)
        .options(selectinload(Lead.contacts), selectinload(Lead.sales))
        .where(
            LeadContact.contact_type == ContactType.phone.value,
            LeadContact.contact_hash.in_(phone_hashes),
            Lead.status != LeadStatus.invalid.value,
            Lead.deleted_at.is_(None),
        )
        .order_by(Lead.created_at.asc())
    )
    if exclude_lead_id:
        query = query.where(Lead.id != exclude_lead_id)
    return db.scalars(query).first()


def _potential_duplicate_contacts(db: Session, hashes: list[str], contact_type: ContactType, exclude_lead_id: str | None = None) -> list[dict]:
    if not hashes:
        return []
    query = (
        select(Lead, LeadContact)
        .join(LeadContact)
        .where(
            LeadContact.contact_type == contact_type.value,
            LeadContact.contact_hash.in_(hashes),
            Lead.deleted_at.is_(None),
        )
        .order_by(Lead.updated_at.desc())
    )
    if exclude_lead_id:
        query = query.where(Lead.id != exclude_lead_id)
    rows = db.execute(query.limit(10)).all()
    return [
        {
            "lead_id": lead.id,
            "customer_name": lead.customer_name,
            "status": lead.status,
            "masked_value": contact.masked_value,
        }
        for lead, contact in rows
    ]


def _record_duplicate(
    db: Session,
    actor: ActorContext,
    existing: Lead,
    matched_contact_hash: str,
    payload: LeadCreate,
    normalized: dict[str, list[contact_utils.NormalizedContact]],
) -> None:
    now = utcnow()
    existing.duplicate_count += 1
    existing.last_duplicate_at = now
    existing.updated_by = str(actor.operator_id)

    duplicate_event = LeadDuplicateEvent(
        lead_id=existing.id,
        matched_contact_hash=matched_contact_hash,
        submitted_customer_name=payload.customer_name,
        submitted_phone_masked=",".join(c.masked for c in normalized["phones"]),
        submitted_wechat_masked=",".join(c.masked for c in normalized["wechats"]) or None,
        submitted_email_masked=",".join(c.masked for c in normalized["emails"]) or None,
        submitted_remark=payload.remark,
        submitted_payload={
            "customer_name": payload.customer_name,
            "phones": [c.masked for c in normalized["phones"]],
            "wechats": [c.masked for c in normalized["wechats"]],
            "emails": [c.masked for c in normalized["emails"]],
            "custom_fields": payload.custom_fields,
        },
        operator_id=str(actor.operator_id),
    )
    db.add(duplicate_event)
    write_log(
        db,
        actor,
        event_type="duplicate_detected",
        module="lead",
        target_type="lead",
        target_id=existing.id,
        lead_id=existing.id,
        metadata={"submitted_phone_masked": duplicate_event.submitted_phone_masked},
    )

    if payload.remark:
        content = (
            f"[重复录入追加][{now.strftime('%Y-%m-%d %H:%M')}][操作人：{actor.operator_name}]\n"
            f"本次录入备注：{payload.remark}"
        )
        db.add(
            LeadNote(
                lead_id=existing.id,
                note_type=NoteType.duplicate_append.value,
                content=content,
                operator_id=str(actor.operator_id),
            )
        )
        write_log(
            db,
            actor,
            event_type="duplicate_note_appended",
            module="lead",
            target_type="lead",
            target_id=existing.id,
            lead_id=existing.id,
            metadata={"note_type": NoteType.duplicate_append.value},
        )


def create_lead(db: Session, payload: LeadCreate, actor: ActorContext) -> dict:
    lead_received_started = time.perf_counter()
    normalized = _normalize_payload_contacts(payload)
    phone_hashes = [c.contact_hash for c in normalized["phones"]]
    existing = _find_existing_active_by_phone_hashes(db, phone_hashes)

    if existing:
        matched_hash = next((h for h in phone_hashes if any(c.contact_hash == h for c in existing.contacts)), phone_hashes[0])
        _record_duplicate(db, actor, existing, matched_hash, payload, normalized)
        db.flush()
        message = (
            f"该手机号已存在，不能重复新建。已重复录入 {existing.duplicate_count} 次，"
            f"日期：{'、'.join(_duplicate_dates(db, existing.id))}。"
        )
        if payload.remark:
            message += "本次备注已追加到原线索。"
        raise DuplicateLeadError(
            message,
            {
                "created": False,
                "duplicate_lead": _lead_summary(existing),
                "duplicate_count": existing.duplicate_count,
                "duplicate_dates": _duplicate_dates(db, existing.id),
                "note_appended": bool(payload.remark),
            },
        )

    lead = Lead(
        customer_name=payload.customer_name,
        status=LeadStatus.unassigned.value,
        source_type="manual",
        source_name_snapshot="人工录入",
        assign_status=AssignStatus.unassigned.value,
        remark=payload.remark,
        custom_fields=payload.custom_fields,
        created_by=str(actor.operator_id),
        updated_by=str(actor.operator_id),
    )
    db.add(lead)
    db.flush()

    for index, contact in enumerate(normalized["phones"]):
        db.add(_contact_model(lead.id, ContactType.phone, contact, index == 0))
    for index, contact in enumerate(normalized["wechats"]):
        db.add(_contact_model(lead.id, ContactType.wechat, contact, index == 0))
    for index, contact in enumerate(normalized["emails"]):
        db.add(_contact_model(lead.id, ContactType.email, contact, index == 0))

    if payload.remark:
        db.add(
            LeadNote(
                lead_id=lead.id,
                note_type=NoteType.manual.value,
                content=payload.remark,
                operator_id=str(actor.operator_id),
            )
        )

    write_log(
        db,
        actor,
        event_type="lead_created",
        module="lead",
        target_type="lead",
        target_id=lead.id,
        lead_id=lead.id,
        after_data={"customer_name": lead.customer_name, "status": lead.status},
    )
    from app.services.observability_service import (
        process_run_id_for_key,
        record_server_stage_best_effort,
    )
    from app.core.request_id import get_request_id

    process_run_id = process_run_id_for_key("c0_lead", lead.id)
    observability_trace_id = get_request_id()
    record_server_stage_best_effort(
        db,
        process_run_id=process_run_id,
        stage_name="c0.lead_received",
        component="backend",
        duration_ms=int(
            round((time.perf_counter() - lead_received_started) * 1000)
        ),
        trace_id=observability_trace_id,
        stable_key=lead.id,
    )
    assignment_started = time.perf_counter()
    assignment = assign_lead_round_robin(db, lead, actor)
    db.flush()
    record_server_stage_best_effort(
        db,
        process_run_id=process_run_id,
        stage_name="c0.lead_assigned",
        component="backend",
        duration_ms=int(round((time.perf_counter() - assignment_started) * 1000)),
        status=(
            "succeeded"
            if assignment.assignment_status == "succeeded"
            else "failed"
        ),
        error_code=(
            None
            if assignment.assignment_status == "succeeded"
            else "LEAD_ASSIGN_FAILED"
        ),
        trace_id=observability_trace_id,
        stable_key=str(assignment.id),
    )
    db.flush()
    db.refresh(lead)

    potential = {
        "wechat": _potential_duplicate_contacts(db, [c.contact_hash for c in normalized["wechats"]], ContactType.wechat, lead.id),
        "email": _potential_duplicate_contacts(db, [c.contact_hash for c in normalized["emails"]], ContactType.email, lead.id),
    }
    return {
        "created": True,
        "id": lead.id,
        "status": lead.status,
        "assign_status": lead.assign_status,
        "sales_id": lead.sales_id,
        "sales_name": lead.sales.sales_name if lead.sales else None,
        "lead": {
            "id": lead.id,
            "status": lead.status,
            "sales_id": lead.sales_id,
            "sales_name": lead.sales.sales_name if lead.sales else None,
        },
        "assignment": {
            "status": assignment.assignment_status,
            "failure_reason": assignment.failure_reason,
        },
        "potential_duplicates": potential,
    }


def check_duplicate_phone(db: Session, phone: str) -> dict:
    normalized = contact_utils.normalize_phone(phone)
    active = _find_existing_active_by_phone_hashes(db, [normalized.contact_hash])
    invalid_rows = list(
        db.scalars(
            select(Lead)
            .join(LeadContact)
            .options(selectinload(Lead.contacts), selectinload(Lead.sales))
            .where(
                LeadContact.contact_type == ContactType.phone.value,
                LeadContact.contact_hash == normalized.contact_hash,
                Lead.status == LeadStatus.invalid.value,
                Lead.deleted_at.is_(None),
            )
            .order_by(Lead.updated_at.desc())
            .limit(5)
        )
    )
    return {
        "phone_masked": normalized.masked,
        "has_active_duplicate": active is not None,
        "duplicated": active is not None,
        "lead_id": active.id if active else None,
        "customer_name": active.customer_name if active else None,
        "sales_name": active.sales.sales_name if active and active.sales else None,
        "duplicate_count": active.duplicate_count if active else 0,
        "duplicate_lead": _lead_summary(active) if active else None,
        "potential_invalid_duplicates": [_lead_summary(lead) for lead in invalid_rows],
    }


def duplicate_preview(db: Session, phones: list[str]) -> dict:
    return {"items": [check_duplicate_phone(db, phone) for phone in phones]}


def _lead_list_item(lead: Lead) -> dict:
    primary_phone = _primary_contact(lead, ContactType.phone)
    primary_wechat = _primary_contact(lead, ContactType.wechat)
    return {
        "id": lead.id,
        "customer_name": lead.customer_name,
        "status": lead.status,
        "source_type": lead.source_type,
        "source_name_snapshot": lead.source_name_snapshot,
        "primary_phone_masked": primary_phone.masked_value if primary_phone else None,
        "primary_wechat_masked": primary_wechat.masked_value if primary_wechat else None,
        "sales_id": lead.sales_id,
        "sales_name": lead.sales.sales_name if lead.sales else None,
        "assign_status": lead.assign_status,
        "assign_failure_reason": lead.assign_failure_reason,
        "remark_summary": lead.remark[:20] if lead.remark else None,
        "duplicate_count": lead.duplicate_count,
        "last_duplicate_at": lead.last_duplicate_at,
        "created_by_name": None,
        "created_at": lead.created_at,
        "updated_at": lead.updated_at,
    }


def list_leads(
    db: Session,
    *,
    keyword: str | None = None,
    status: str | None = None,
    sales_id: str | None = None,
    created_by: str | None = None,
    has_duplicate: bool | None = None,
    page: int = 1,
    page_size: int = 20,
) -> dict:
    query = select(Lead).options(selectinload(Lead.contacts), selectinload(Lead.sales)).where(Lead.deleted_at.is_(None))
    if status:
        query = query.where(Lead.status == status)
    if sales_id:
        query = query.where(Lead.sales_id == sales_id)
    if created_by:
        query = query.where(Lead.created_by == created_by)
    if has_duplicate is not None:
        query = query.where(Lead.duplicate_count > 0 if has_duplicate else Lead.duplicate_count == 0)
    if keyword:
        like = f"%{keyword.strip()}%"
        contact_lead_ids = select(LeadContact.lead_id).where(
            or_(
                LeadContact.masked_value.ilike(like),
                LeadContact.contact_value_normalized.ilike(like),
            )
        )
        query = query.where(or_(Lead.customer_name.ilike(like), Lead.remark.ilike(like), Lead.id.in_(contact_lead_ids)))

    count_query = select(func.count()).select_from(query.order_by(None).subquery())
    total = db.scalar(count_query) or 0
    leads = list(db.scalars(query.order_by(Lead.updated_at.desc()).offset((page - 1) * page_size).limit(page_size)))
    return {"items": [_lead_list_item(lead) for lead in leads], "page": page, "page_size": page_size, "total": total}


def lead_stats(db: Session) -> dict:
    start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    today_new_count = db.scalar(select(func.count()).select_from(Lead).where(Lead.created_at >= start)) or 0
    today_assigned_count = (
        db.scalar(
            select(func.count())
            .select_from(Lead)
            .where(
                Lead.created_at >= start,
                Lead.status == LeadStatus.assigned.value,
            )
        )
        or 0
    )
    today_unassigned_count = (
        db.scalar(
            select(func.count())
            .select_from(Lead)
            .where(
                Lead.created_at >= start,
                Lead.status == LeadStatus.unassigned.value,
            )
        )
        or 0
    )
    assignment_success_rate = round(today_assigned_count / today_new_count * 100, 1) if today_new_count else None
    return {
        "today_new_count": today_new_count,
        "today_assigned_count": today_assigned_count,
        "today_unassigned_count": today_unassigned_count,
        "assignment_success_rate": assignment_success_rate,
        "assigned_count": db.scalar(select(func.count()).select_from(Lead).where(Lead.status == LeadStatus.assigned.value)) or 0,
        "unassigned_count": db.scalar(select(func.count()).select_from(Lead).where(Lead.status == LeadStatus.unassigned.value)) or 0,
        "duplicate_event_count": db.scalar(select(func.count()).select_from(LeadDuplicateEvent)) or 0,
    }


def get_lead_detail(db: Session, lead_id: str) -> dict:
    lead = db.scalar(
        select(Lead)
        .options(
            selectinload(Lead.contacts),
            selectinload(Lead.sales),
            selectinload(Lead.notes),
            selectinload(Lead.assignments),
            selectinload(Lead.duplicate_events),
        )
        .where(Lead.id == lead_id, Lead.deleted_at.is_(None))
    )
    if not lead:
        raise AppError("LEAD_NOT_FOUND", "线索不存在", 404)

    task_nodes = [
        {"key": "lead_created", "label": "客户线索已创建", "time": lead.created_at},
        {"key": "phone_deduped", "label": "手机号去重完成", "time": lead.created_at},
    ]
    if lead.duplicate_count:
        task_nodes.append({"key": "duplicate_note_appended", "label": "重复备注已追加", "time": lead.last_duplicate_at})
    if lead.assign_status == AssignStatus.assigned.value:
        task_nodes.append({"key": "round_robin_assigned", "label": "轮询分配完成", "time": lead.assigned_at})
    if lead.assign_status == AssignStatus.assign_failed.value:
        task_nodes.append({"key": "round_robin_failed", "label": "轮询分配失败", "time": lead.updated_at})
    if lead.status == LeadStatus.invalid.value:
        task_nodes.append({"key": "marked_invalid", "label": "标记无效", "time": lead.invalid_at})

    return {
        **_lead_list_item(lead),
        "remark": lead.remark,
        "custom_fields": lead.custom_fields,
        "invalid_reason": lead.invalid_reason,
        "invalid_remark": lead.invalid_remark,
        "invalid_at": lead.invalid_at,
        "invalid_by": lead.invalid_by,
        "contacts": [
            {
                "id": c.id,
                "contact_type": c.contact_type,
                "masked_value": c.masked_value,
                "is_primary": c.is_primary,
            }
            for c in lead.contacts
        ],
        "assignments": [
            {
                "id": a.id,
                "assignment_result": a.assignment_status,
                "sales_id": a.to_sales_id,
                "sales_name": db.get(Sales, a.to_sales_id).sales_name if a.to_sales_id and db.get(Sales, a.to_sales_id) else None,
                "from_sales_id": a.from_sales_id,
                "to_sales_id": a.to_sales_id,
                "assignment_type": a.assignment_type,
                "assignment_status": a.assignment_status,
                "failure_reason": a.failure_reason,
                "round_robin_cursor_before": a.round_robin_cursor_before,
                "round_robin_cursor_after": a.round_robin_cursor_after,
                "operator_id": a.operator_id,
                "remark": a.remark,
                "created_at": a.created_at,
            }
            for a in sorted(lead.assignments, key=lambda x: x.created_at, reverse=True)
        ],
        "notes": [
            {
                "id": n.id,
                "note_type": n.note_type,
                "content": n.content,
                "operator_id": n.operator_id,
                "created_at": n.created_at,
            }
            for n in sorted(lead.notes, key=lambda x: x.created_at, reverse=True)
        ],
        "duplicate_events": [
            {
                "id": e.id,
                "submitted_customer_name": e.submitted_customer_name,
                "submitted_phone_masked": e.submitted_phone_masked,
                "submitted_remark": e.submitted_remark,
                "operator_id": e.operator_id,
                "created_at": e.created_at,
            }
            for e in sorted(lead.duplicate_events, key=lambda x: x.created_at, reverse=True)
        ],
        "task_nodes": task_nodes,
    }


def update_lead(db: Session, lead_id: str, payload: LeadUpdate, actor: ActorContext) -> dict:
    lead = db.scalar(select(Lead).options(selectinload(Lead.contacts)).where(Lead.id == lead_id, Lead.deleted_at.is_(None)))
    if not lead:
        raise AppError("LEAD_NOT_FOUND", "线索不存在", 404)
    before = _lead_list_item(lead)

    if payload.customer_name is not None:
        lead.customer_name = payload.customer_name.strip()
    if payload.remark is not None:
        lead.remark = payload.remark
    if payload.custom_fields is not None:
        lead.custom_fields = payload.custom_fields

    if payload.phones is not None:
        normalized = _normalize_payload_contacts(payload)
        phone_hashes = [c.contact_hash for c in normalized["phones"]]
        existing = _find_existing_active_by_phone_hashes(db, phone_hashes, exclude_lead_id=lead.id)
        if existing:
            raise DuplicateLeadError("该手机号已存在，不能更新到当前线索", {"duplicate_lead": _lead_summary(existing)})
        for contact in list(lead.contacts):
            db.delete(contact)
        db.flush()
        for index, contact in enumerate(normalized["phones"]):
            db.add(_contact_model(lead.id, ContactType.phone, contact, index == 0))
        for index, contact in enumerate(normalized["wechats"]):
            db.add(_contact_model(lead.id, ContactType.wechat, contact, index == 0))
        for index, contact in enumerate(normalized["emails"]):
            db.add(_contact_model(lead.id, ContactType.email, contact, index == 0))

    lead.updated_by = str(actor.operator_id)
    db.flush()
    write_log(
        db,
        actor,
        event_type="lead_updated",
        module="lead",
        target_type="lead",
        target_id=lead.id,
        lead_id=lead.id,
        before_data=before,
        after_data={"customer_name": lead.customer_name, "status": lead.status},
    )
    db.refresh(lead)
    return get_lead_detail(db, lead.id)


def mark_invalid(db: Session, lead_id: str, payload: MarkInvalidRequest, actor: ActorContext) -> dict:
    lead = db.get(Lead, lead_id)
    if not lead or lead.deleted_at:
        raise AppError("LEAD_NOT_FOUND", "线索不存在", 404)
    before = {"status": lead.status, "invalid_reason": lead.invalid_reason}
    lead.status = LeadStatus.invalid.value
    lead.invalid_reason = payload.invalid_reason.value
    lead.invalid_remark = payload.invalid_remark
    lead.invalid_at = utcnow()
    lead.invalid_by = str(actor.operator_id)
    lead.updated_by = str(actor.operator_id)
    write_log(
        db,
        actor,
        event_type="lead_marked_invalid",
        module="lead",
        target_type="lead",
        target_id=lead.id,
        lead_id=lead.id,
        before_data=before,
        after_data={"status": lead.status, "invalid_reason": lead.invalid_reason},
    )
    return get_lead_detail(db, lead.id)


def batch_mark_invalid(db: Session, lead_ids: list[str], payload: MarkInvalidRequest, actor: ActorContext) -> dict:
    if not lead_ids:
        raise AppError("VALIDATION_ERROR", "请选择要标记的线索", 400)
    leads = list(db.scalars(select(Lead).where(Lead.id.in_(lead_ids), Lead.deleted_at.is_(None))).all())
    found = {lead.id for lead in leads}
    results = []
    for requested_id in lead_ids:
        if requested_id not in found:
            results.append({"lead_id": requested_id, "status": "skipped", "reason": "线索不存在"})

    for lead in leads:
        before = {"status": lead.status, "invalid_reason": lead.invalid_reason}
        lead.status = LeadStatus.invalid.value
        lead.invalid_reason = payload.invalid_reason.value
        lead.invalid_remark = payload.invalid_remark
        lead.invalid_at = utcnow()
        lead.invalid_by = str(actor.operator_id)
        lead.updated_by = str(actor.operator_id)
        write_log(
            db,
            actor,
            event_type="lead_marked_invalid",
            module="lead",
            target_type="lead",
            target_id=lead.id,
            lead_id=lead.id,
            before_data=before,
            after_data={"status": lead.status, "invalid_reason": lead.invalid_reason},
            metadata={"batch": True},
        )
        results.append({"lead_id": lead.id, "status": "succeeded"})
    return {
        "requested": len(lead_ids),
        "succeeded": sum(1 for item in results if item["status"] == "succeeded"),
        "skipped": sum(1 for item in results if item["status"] == "skipped"),
        "items": results,
    }


def restore_lead(db: Session, lead_id: str, actor: ActorContext) -> dict:
    lead = db.get(Lead, lead_id)
    if not lead or lead.deleted_at:
        raise AppError("LEAD_NOT_FOUND", "线索不存在", 404)
    before = {"status": lead.status, "invalid_reason": lead.invalid_reason}
    lead.status = LeadStatus.assigned.value if lead.sales_id else LeadStatus.unassigned.value
    lead.assign_status = AssignStatus.assigned.value if lead.sales_id else AssignStatus.unassigned.value
    lead.invalid_reason = None
    lead.invalid_remark = None
    lead.invalid_at = None
    lead.invalid_by = None
    lead.updated_by = str(actor.operator_id)
    write_log(
        db,
        actor,
        event_type="lead_restored",
        module="lead",
        target_type="lead",
        target_id=lead.id,
        lead_id=lead.id,
        before_data=before,
        after_data={"status": lead.status},
    )
    return get_lead_detail(db, lead.id)


def reveal_contact(db: Session, lead_id: str, contact_id: str, reason: str, actor: ActorContext) -> dict:
    contact = db.scalar(
        select(LeadContact).join(Lead).where(LeadContact.id == contact_id, LeadContact.lead_id == lead_id, Lead.deleted_at.is_(None))
    )
    if not contact:
        raise AppError("LEAD_NOT_FOUND", "联系方式不存在", 404)
    if contact.contact_type != ContactType.phone.value:
        raise AppError("VALIDATION_ERROR", "当前只允许查看手机号明文", 400)

    value = contact_utils.decrypt_for_p0(contact.contact_value_encrypted)
    write_log(
        db,
        actor,
        event_type="phone_revealed",
        module="lead",
        target_type="contact",
        target_id=contact.id,
        lead_id=lead_id,
        metadata={"reason": reason, "phone_suffix": value[-4:]},
    )
    return {"contact_id": contact.id, "contact_type": contact.contact_type, "value": value, "revealed_at": utcnow()}


def notes_for_lead(db: Session, lead_id: str) -> list[dict]:
    rows = db.scalars(select(LeadNote).where(LeadNote.lead_id == lead_id).order_by(LeadNote.created_at.desc())).all()
    return [{"id": n.id, "note_type": n.note_type, "content": n.content, "operator_id": n.operator_id, "created_at": n.created_at} for n in rows]


def duplicate_events_for_lead(db: Session, lead_id: str) -> list[dict]:
    rows = db.scalars(
        select(LeadDuplicateEvent).where(LeadDuplicateEvent.lead_id == lead_id).order_by(LeadDuplicateEvent.created_at.desc())
    ).all()
    return [
        {
            "id": e.id,
            "submitted_customer_name": e.submitted_customer_name,
            "submitted_phone_masked": e.submitted_phone_masked,
            "submitted_remark": e.submitted_remark,
            "operator_id": e.operator_id,
            "created_at": e.created_at,
        }
        for e in rows
    ]


def assignments_for_lead(db: Session, lead_id: str) -> list[dict]:
    rows = db.scalars(select(LeadAssignment).where(LeadAssignment.lead_id == lead_id).order_by(LeadAssignment.created_at.desc())).all()
    items = []
    for a in rows:
        sales = db.get(Sales, a.to_sales_id) if a.to_sales_id else None
        items.append(
            {
            "id": a.id,
            "lead_id": a.lead_id,
            "assignment_result": a.assignment_status,
            "sales_id": a.to_sales_id,
            "sales_name": sales.sales_name if sales else None,
            "from_sales_id": a.from_sales_id,
            "to_sales_id": a.to_sales_id,
            "assignment_type": a.assignment_type,
            "assignment_status": a.assignment_status,
            "failure_reason": a.failure_reason,
            "round_robin_cursor_before": a.round_robin_cursor_before,
            "round_robin_cursor_after": a.round_robin_cursor_after,
            "operator_id": a.operator_id,
            "remark": a.remark,
            "created_at": a.created_at,
        }
        )
    return items
