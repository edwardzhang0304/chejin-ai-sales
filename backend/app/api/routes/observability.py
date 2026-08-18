from fastapi import APIRouter, Depends, Header
from sqlalchemy.orm import Session

from app.api.response import ok
from app.core.auth import require_admin_auth
from app.core.database import get_db
from app.schemas.observability import ProcessStageEventsRequest
from app.services import observability_service, worker_service


router = APIRouter(tags=["observability"])


@router.post("/workers/{worker_id}/observability/stage-events")
def ingest_stage_events(
    worker_id: str,
    payload: ProcessStageEventsRequest,
    db: Session = Depends(get_db),
    x_worker_token: str | None = Header(default=None, alias="X-Worker-Token"),
    x_client_instance_id: str | None = Header(
        default=None, alias="X-Client-Instance-Id"
    ),
):
    worker = worker_service.authenticate_worker_client(
        db, worker_id, x_worker_token, x_client_instance_id
    )
    try:
        data = observability_service.ingest_worker_stage_events(
            db, worker=worker, events=payload.events
        )
        db.commit()
        return ok(data)
    except Exception:
        db.rollback()
        raise


@router.get(
    "/observability/process-runs/{process_run_id}",
    dependencies=[Depends(require_admin_auth)],
)
def get_process_run(process_run_id: str, db: Session = Depends(get_db)):
    return ok(observability_service.get_process_run(db, process_run_id))
