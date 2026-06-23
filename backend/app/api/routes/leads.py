from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.api.response import ok
from app.core.auth import require_admin_auth
from app.core.database import get_db
from app.core.request_context import ActorContext, get_actor_context
from app.errors import AppError, DuplicateLeadError
from app.schemas.lead import (
    BatchMarkInvalidRequest,
    DuplicatePreviewRequest,
    LeadCreate,
    LeadExportRequest,
    LeadUpdate,
    MarkInvalidRequest,
    RevealContactRequest,
    RetryAutoAssignRequest,
)
from app.services import export_service, lead_service
from app.services.assignment_service import retry_auto_assign


router = APIRouter(tags=["leads"], dependencies=[Depends(require_admin_auth)])


@router.get("/leads")
def list_leads(
    keyword: str | None = None,
    status: str | None = None,
    sales_id: str | None = None,
    created_by: str | None = None,
    has_duplicate: bool | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
):
    return ok(
        lead_service.list_leads(
            db,
            keyword=keyword,
            status=status,
            sales_id=sales_id,
            created_by=created_by,
            has_duplicate=has_duplicate,
            page=page,
            page_size=page_size,
        )
    )


@router.get("/leads/stats")
def stats(db: Session = Depends(get_db)):
    return ok(lead_service.lead_stats(db))


@router.post("/leads/duplicate-preview")
def duplicate_preview(payload: DuplicatePreviewRequest, db: Session = Depends(get_db)):
    return ok(lead_service.duplicate_preview(db, payload.phones))


@router.post("/leads")
def create_lead(
    payload: LeadCreate,
    db: Session = Depends(get_db),
    actor: ActorContext = Depends(get_actor_context),
):
    try:
        data = lead_service.create_lead(db, payload, actor)
        db.commit()
        return ok(data)
    except DuplicateLeadError:
        db.commit()
        raise
    except Exception:
        db.rollback()
        raise


@router.post("/leads/retry-auto-assign")
def retry_assign(
    payload: RetryAutoAssignRequest,
    db: Session = Depends(get_db),
    actor: ActorContext = Depends(get_actor_context),
):
    try:
        data = retry_auto_assign(db, payload.lead_ids, actor)
        db.commit()
        return ok(data)
    except Exception:
        db.rollback()
        raise


@router.post("/leads/export")
def export_leads(
    payload: LeadExportRequest,
    db: Session = Depends(get_db),
    actor: ActorContext = Depends(get_actor_context),
):
    try:
        file_name, csv_text = export_service.export_selected_leads(db, payload.lead_ids, payload.fields, actor)
        db.commit()
        return Response(
            content="\ufeff" + csv_text,
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{file_name}"'},
        )
    except Exception:
        db.rollback()
        raise


@router.post("/leads/batch-mark-invalid")
def batch_mark_invalid(
    payload: BatchMarkInvalidRequest,
    db: Session = Depends(get_db),
    actor: ActorContext = Depends(get_actor_context),
):
    try:
        data = lead_service.batch_mark_invalid(db, payload.lead_ids, payload, actor)
        db.commit()
        return ok(data)
    except Exception:
        db.rollback()
        raise


@router.get("/leads/{lead_id}")
def get_lead(lead_id: str, db: Session = Depends(get_db)):
    return ok(lead_service.get_lead_detail(db, lead_id))


@router.put("/leads/{lead_id}")
def update_lead(
    lead_id: str,
    payload: LeadUpdate,
    db: Session = Depends(get_db),
    actor: ActorContext = Depends(get_actor_context),
):
    try:
        data = lead_service.update_lead(db, lead_id, payload, actor)
        db.commit()
        return ok(data)
    except Exception:
        db.rollback()
        raise


@router.post("/leads/{lead_id}/mark-invalid")
def mark_invalid(
    lead_id: str,
    payload: MarkInvalidRequest,
    db: Session = Depends(get_db),
    actor: ActorContext = Depends(get_actor_context),
):
    try:
        data = lead_service.mark_invalid(db, lead_id, payload, actor)
        db.commit()
        return ok(data)
    except Exception:
        db.rollback()
        raise


@router.post("/leads/{lead_id}/restore")
def restore_lead(
    lead_id: str,
    db: Session = Depends(get_db),
    actor: ActorContext = Depends(get_actor_context),
):
    try:
        data = lead_service.restore_lead(db, lead_id, actor)
        db.commit()
        return ok(data)
    except Exception:
        db.rollback()
        raise


@router.get("/leads/{lead_id}/notes")
def notes(lead_id: str, db: Session = Depends(get_db)):
    return ok({"items": lead_service.notes_for_lead(db, lead_id)})


@router.get("/leads/{lead_id}/duplicate-events")
def duplicate_events(lead_id: str, db: Session = Depends(get_db)):
    return ok({"items": lead_service.duplicate_events_for_lead(db, lead_id)})


@router.get("/leads/{lead_id}/assignments")
def assignments(lead_id: str, db: Session = Depends(get_db)):
    return ok({"items": lead_service.assignments_for_lead(db, lead_id)})


@router.post("/leads/{lead_id}/contacts/{contact_id}/reveal")
def reveal_contact(
    lead_id: str,
    contact_id: str,
    payload: RevealContactRequest,
    db: Session = Depends(get_db),
    actor: ActorContext = Depends(get_actor_context),
):
    try:
        data = lead_service.reveal_contact(db, lead_id, contact_id, payload.reason, actor)
        db.commit()
        return ok(data)
    except Exception:
        db.rollback()
        raise
