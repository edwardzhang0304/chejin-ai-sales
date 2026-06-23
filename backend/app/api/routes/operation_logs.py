from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.response import ok
from app.core.auth import require_admin_auth
from app.core.database import get_db
from app.services.audit_service import build_log_query, paginate_logs


router = APIRouter(tags=["operation-logs"], dependencies=[Depends(require_admin_auth)])


@router.get("/operation-logs")
def operation_logs(
    keyword: str | None = None,
    event_type: str | None = None,
    module: str | None = None,
    operator_id: str | None = None,
    operator_name: str | None = None,
    target_type: str | None = None,
    target_id: str | None = None,
    result: str | None = None,
    created_from: str | None = None,
    created_to: str | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
):
    query = build_log_query(
        keyword=keyword,
        event_type=event_type,
        module=module,
        operator_id=operator_id,
        operator_name=operator_name,
        target_type=target_type,
        target_id=target_id,
        result=result,
        created_from=created_from,
        created_to=created_to,
    )
    return ok(paginate_logs(db, query, page, page_size))


@router.get("/leads/{lead_id}/operation-logs")
def lead_operation_logs(
    lead_id: str,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
):
    query = build_log_query(lead_id=lead_id)
    return ok(paginate_logs(db, query, page, page_size))
