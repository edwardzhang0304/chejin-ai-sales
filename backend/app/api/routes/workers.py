from fastapi import APIRouter, Depends, Header, Request
from sqlalchemy.orm import Session

from app.api.response import ok
from app.core.auth import require_admin_auth
from app.core.database import get_db
from app.core.request_context import ActorContext, get_actor_context, worker_actor_context
from app.schemas.worker import (
    WorkerClientBindRequest,
    WorkerCreate,
    WorkerHeartbeat,
    WorkerInflightFlowFinishRequest,
    WorkerInflightFlowStartRequest,
    WorkerResetBindingRequest,
    WorkerRunStatusRequest,
    WorkerUpdate,
)
from app.services import task_service, worker_service


router = APIRouter(tags=["workers"])


@router.get("/workers")
def list_workers(
    db: Session = Depends(get_db),
    _admin_auth: None = Depends(require_admin_auth),
):
    return ok({"items": worker_service.list_workers(db)})


@router.post("/workers")
def create_worker(
    payload: WorkerCreate,
    db: Session = Depends(get_db),
    actor: ActorContext = Depends(get_actor_context),
    _admin_auth: None = Depends(require_admin_auth),
):
    try:
        data = worker_service.create_worker(db, payload, actor)
        db.commit()
        return ok(data)
    except Exception:
        db.rollback()
        raise


@router.get("/workers/{worker_id}")
def get_worker(
    worker_id: str,
    db: Session = Depends(get_db),
    _admin_auth: None = Depends(require_admin_auth),
):
    return ok(worker_service.get_worker_detail(db, worker_id))


@router.put("/workers/{worker_id}")
def update_worker(
    worker_id: str,
    payload: WorkerUpdate,
    db: Session = Depends(get_db),
    actor: ActorContext = Depends(get_actor_context),
    _admin_auth: None = Depends(require_admin_auth),
):
    try:
        data = worker_service.update_worker(db, worker_id, payload, actor)
        db.commit()
        return ok(data)
    except Exception:
        db.rollback()
        raise


@router.post("/workers/{worker_id}/enable")
def enable_worker(
    worker_id: str,
    db: Session = Depends(get_db),
    actor: ActorContext = Depends(get_actor_context),
    _admin_auth: None = Depends(require_admin_auth),
):
    try:
        data = worker_service.set_worker_enabled(db, worker_id, True, actor)
        db.commit()
        return ok(data)
    except Exception:
        db.rollback()
        raise


@router.post("/workers/{worker_id}/disable")
def disable_worker(
    worker_id: str,
    db: Session = Depends(get_db),
    actor: ActorContext = Depends(get_actor_context),
    _admin_auth: None = Depends(require_admin_auth),
):
    try:
        data = worker_service.set_worker_enabled(db, worker_id, False, actor)
        db.commit()
        return ok(data)
    except Exception:
        db.rollback()
        raise


@router.post("/workers/{worker_id}/heartbeat")
def heartbeat_worker(
    worker_id: str,
    payload: WorkerHeartbeat,
    db: Session = Depends(get_db),
    x_worker_token: str | None = Header(default=None, alias="X-Worker-Token"),
):
    try:
        data = worker_service.heartbeat_worker(db, worker_id, x_worker_token, payload)
        db.commit()
        return ok(data)
    except Exception:
        db.rollback()
        raise


@router.post("/workers/{worker_id}/client-bind")
def bind_worker_client(
    worker_id: str,
    payload: WorkerClientBindRequest,
    db: Session = Depends(get_db),
):
    try:
        data = worker_service.bind_worker_client(db, worker_id, payload)
        db.commit()
        return ok(data)
    except Exception:
        db.rollback()
        raise


@router.post("/workers/{worker_id}/run-status")
def set_worker_run_status(
    worker_id: str,
    payload: WorkerRunStatusRequest,
    db: Session = Depends(get_db),
    x_worker_token: str | None = Header(default=None, alias="X-Worker-Token"),
):
    try:
        data = worker_service.set_worker_run_status(db, worker_id, x_worker_token, payload)
        db.commit()
        return ok(data)
    except Exception:
        db.rollback()
        raise


@router.post("/workers/{worker_id}/inflight-flow/start")
def start_worker_inflight_flow(
    worker_id: str,
    payload: WorkerInflightFlowStartRequest,
    db: Session = Depends(get_db),
    x_worker_token: str | None = Header(default=None, alias="X-Worker-Token"),
    x_client_instance_id: str | None = Header(default=None, alias="X-Client-Instance-Id"),
):
    try:
        worker = worker_service.authenticate_worker_client(
            db, worker_id, x_worker_token, x_client_instance_id
        )
        data = worker_service.start_inflight_flow(db, worker, payload)
        db.commit()
        return ok(data)
    except Exception:
        db.rollback()
        raise


@router.post("/workers/{worker_id}/inflight-flow/finish")
def finish_worker_inflight_flow(
    request: Request,
    worker_id: str,
    payload: WorkerInflightFlowFinishRequest,
    db: Session = Depends(get_db),
    x_worker_token: str | None = Header(default=None, alias="X-Worker-Token"),
    x_client_instance_id: str | None = Header(default=None, alias="X-Client-Instance-Id"),
    x_inflight_flow_id: str | None = Header(default=None, alias="X-Inflight-Flow-Id"),
):
    try:
        worker = worker_service.authenticate_worker_client(
            db, worker_id, x_worker_token, x_client_instance_id
        )
        worker_service.validate_inflight_continuation(
            worker, x_inflight_flow_id
        )
        actor = worker_actor_context(
            request, worker_id=worker.id, worker_name=worker.worker_name
        )
        data = worker_service.finish_inflight_flow(db, worker, payload, actor)
        db.commit()
        return ok(data)
    except Exception:
        db.rollback()
        raise


@router.get("/workers/{worker_id}/tasks/pull")
def pull_worker_task(
    worker_id: str,
    db: Session = Depends(get_db),
    x_worker_token: str | None = Header(default=None, alias="X-Worker-Token"),
    x_client_instance_id: str | None = Header(default=None, alias="X-Client-Instance-Id"),
):
    try:
        worker = worker_service.authenticate_worker_client(db, worker_id, x_worker_token, x_client_instance_id)
        worker_service.validate_inflight_continuation(worker, None, new_work=True)
        data = task_service.pull_task_for_worker(db, worker)
        db.commit()
        return ok(data)
    except Exception:
        db.rollback()
        raise


@router.post("/workers/{worker_id}/reset-binding")
def reset_worker_binding(
    worker_id: str,
    _: WorkerResetBindingRequest,
    db: Session = Depends(get_db),
    actor: ActorContext = Depends(get_actor_context),
    _admin_auth: None = Depends(require_admin_auth),
):
    try:
        data = worker_service.reset_worker_binding(db, worker_id, actor)
        db.commit()
        return ok(data)
    except Exception:
        db.rollback()
        raise


@router.post("/workers/{worker_id}/reset-client-bind")
def reset_worker_client_binding(
    worker_id: str,
    payload: WorkerResetBindingRequest,
    db: Session = Depends(get_db),
    actor: ActorContext = Depends(get_actor_context),
    _admin_auth: None = Depends(require_admin_auth),
):
    try:
        data = worker_service.reset_worker_binding(db, worker_id, actor)
        db.commit()
        return ok(data)
    except Exception:
        db.rollback()
        raise
