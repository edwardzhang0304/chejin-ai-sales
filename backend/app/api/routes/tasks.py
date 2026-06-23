from fastapi import APIRouter, Depends, Header, Query, Request
from sqlalchemy.orm import Session

from app.api.response import ok
from app.core.auth import require_admin_auth, validate_admin_auth
from app.core.database import get_db
from app.core.request_context import ActorContext, get_actor_context
from app.enums import TaskResultCode, TaskType
from app.errors import AppError
from app.schemas.task import (
    TaskCancelRequest,
    TaskClaimRequest,
    TaskCommentRequest,
    TaskCompleteRequest,
    TaskCreate,
    TaskEvidenceRequest,
    TaskFailRequest,
    TaskRetryRequest,
    TaskStepRequest,
)
from app.services import task_service, worker_service


router = APIRouter(tags=["tasks"])


@router.get("/tasks")
def list_tasks(
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
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
    _admin_auth: None = Depends(require_admin_auth),
):
    return ok(
        task_service.list_tasks(
            db,
            task_type=task_type,
            status=status,
            result_code=result_code,
            error_code=error_code,
            block_code=block_code,
            sales_id=sales_id,
            worker_id=worker_id,
            keyword=keyword,
            created_from=created_from,
            created_to=created_to,
            page=page,
            page_size=page_size,
        )
    )


@router.post("/tasks")
def create_task(
    payload: TaskCreate,
    db: Session = Depends(get_db),
    actor: ActorContext = Depends(get_actor_context),
    _admin_auth: None = Depends(require_admin_auth),
):
    try:
        if payload.task_type != TaskType.add_friend:
            raise AppError("TASK_TYPE_NOT_SUPPORTED", "当前阶段仅支持 add_friend 任务", 400)
        data = task_service.create_add_friend_task(
            db,
            lead_id=payload.lead_id,
            sales_id=payload.sales_id,
            worker_id=payload.worker_id,
            remark=payload.remark,
            actor=actor,
        )
        db.commit()
        return ok(data)
    except Exception:
        db.rollback()
        raise


@router.get("/tasks/{task_id}")
def get_task(
    task_id: str,
    db: Session = Depends(get_db),
    _admin_auth: None = Depends(require_admin_auth),
):
    return ok(task_service.task_to_detail(task_service.get_task_or_404(db, task_id)))


@router.get("/tasks/{task_id}/events")
def get_task_events(
    task_id: str,
    db: Session = Depends(get_db),
    _admin_auth: None = Depends(require_admin_auth),
):
    return ok({"items": task_service.task_events(db, task_id)})


@router.post("/tasks/{task_id}/comments")
def add_comment(
    task_id: str,
    payload: TaskCommentRequest,
    db: Session = Depends(get_db),
    actor: ActorContext = Depends(get_actor_context),
    _admin_auth: None = Depends(require_admin_auth),
):
    try:
        data = task_service.add_comment(db, task_id, payload.content, actor)
        db.commit()
        return ok(data)
    except Exception:
        db.rollback()
        raise


@router.post("/tasks/{task_id}/cancel")
def cancel_task(
    task_id: str,
    payload: TaskCancelRequest,
    db: Session = Depends(get_db),
    actor: ActorContext = Depends(get_actor_context),
    _admin_auth: None = Depends(require_admin_auth),
):
    try:
        data = task_service.cancel_task(db, task_id, payload.reason, actor)
        db.commit()
        return ok(data)
    except Exception:
        db.rollback()
        raise


@router.post("/tasks/{task_id}/retry")
def retry_task(
    task_id: str,
    payload: TaskRetryRequest,
    db: Session = Depends(get_db),
    actor: ActorContext = Depends(get_actor_context),
    _admin_auth: None = Depends(require_admin_auth),
):
    try:
        data = task_service.retry_task(db, task_id, payload.remark, actor)
        db.commit()
        return ok(data)
    except Exception:
        db.rollback()
        raise


@router.post("/tasks/{task_id}/claim")
def claim_task(
    task_id: str,
    payload: TaskClaimRequest,
    request: Request,
    db: Session = Depends(get_db),
    actor: ActorContext = Depends(get_actor_context),
    x_worker_token: str | None = Header(default=None, alias="X-Worker-Token"),
    x_client_instance_id: str | None = Header(default=None, alias="X-Client-Instance-Id"),
    authorization: str | None = Header(default=None, alias="Authorization"),
):
    try:
        require_worker_ready = False
        if x_worker_token:
            worker_service.authenticate_worker_client(db, payload.worker_id, x_worker_token, x_client_instance_id)
            require_worker_ready = True
        else:
            validate_admin_auth(request, authorization)
        data = task_service.claim_task(
            db,
            task_id,
            payload.worker_id,
            payload.current_step,
            payload.remark,
            actor,
            require_worker_ready=require_worker_ready,
        )
        db.commit()
        return ok(data)
    except Exception:
        db.rollback()
        raise


@router.post("/tasks/{task_id}/step")
def update_step(
    task_id: str,
    payload: TaskStepRequest,
    request: Request,
    db: Session = Depends(get_db),
    actor: ActorContext = Depends(get_actor_context),
    x_worker_token: str | None = Header(default=None, alias="X-Worker-Token"),
    x_client_instance_id: str | None = Header(default=None, alias="X-Client-Instance-Id"),
    authorization: str | None = Header(default=None, alias="Authorization"),
):
    try:
        task = task_service.get_task_or_404(db, task_id)
        if x_worker_token:
            worker_service.authenticate_worker_client(db, task.worker_id or "", x_worker_token, x_client_instance_id)
        else:
            validate_admin_auth(request, authorization)
        data = task_service.update_step(db, task_id, payload.current_step, payload.remark, actor)
        db.commit()
        return ok(data)
    except Exception:
        db.rollback()
        raise


@router.post("/tasks/{task_id}/invite-sent")
def invite_sent(
    task_id: str,
    payload: TaskCompleteRequest,
    request: Request,
    db: Session = Depends(get_db),
    actor: ActorContext = Depends(get_actor_context),
    x_worker_token: str | None = Header(default=None, alias="X-Worker-Token"),
    x_client_instance_id: str | None = Header(default=None, alias="X-Client-Instance-Id"),
    authorization: str | None = Header(default=None, alias="Authorization"),
):
    try:
        task = task_service.get_task_or_404(db, task_id)
        if x_worker_token:
            worker_service.authenticate_worker_client(db, task.worker_id or "", x_worker_token, x_client_instance_id)
        else:
            validate_admin_auth(request, authorization)
        data = task_service.complete_task(db, task_id, TaskResultCode.invite_sent, payload.remark, actor)
        db.commit()
        return ok(data)
    except Exception:
        db.rollback()
        raise


@router.post("/tasks/{task_id}/already-friend")
def already_friend(
    task_id: str,
    payload: TaskCompleteRequest,
    request: Request,
    db: Session = Depends(get_db),
    actor: ActorContext = Depends(get_actor_context),
    x_worker_token: str | None = Header(default=None, alias="X-Worker-Token"),
    x_client_instance_id: str | None = Header(default=None, alias="X-Client-Instance-Id"),
    authorization: str | None = Header(default=None, alias="Authorization"),
):
    try:
        task = task_service.get_task_or_404(db, task_id)
        if x_worker_token:
            worker_service.authenticate_worker_client(db, task.worker_id or "", x_worker_token, x_client_instance_id)
        else:
            validate_admin_auth(request, authorization)
        data = task_service.complete_task(db, task_id, TaskResultCode.already_friend, payload.remark, actor)
        db.commit()
        return ok(data)
    except Exception:
        db.rollback()
        raise


@router.post("/tasks/{task_id}/fail")
def fail_task(
    task_id: str,
    payload: TaskFailRequest,
    request: Request,
    db: Session = Depends(get_db),
    actor: ActorContext = Depends(get_actor_context),
    x_worker_token: str | None = Header(default=None, alias="X-Worker-Token"),
    x_client_instance_id: str | None = Header(default=None, alias="X-Client-Instance-Id"),
    authorization: str | None = Header(default=None, alias="Authorization"),
):
    try:
        task = task_service.get_task_or_404(db, task_id)
        if x_worker_token:
            worker_service.authenticate_worker_client(db, task.worker_id or "", x_worker_token, x_client_instance_id)
        else:
            validate_admin_auth(request, authorization)
        data = task_service.fail_task(db, task_id, payload.error_code, payload.failure_step, payload.failure_remark, actor)
        db.commit()
        return ok(data)
    except Exception:
        db.rollback()
        raise


@router.post("/tasks/{task_id}/evidences")
def add_task_evidence(
    task_id: str,
    payload: TaskEvidenceRequest,
    request: Request,
    db: Session = Depends(get_db),
    x_worker_token: str | None = Header(default=None, alias="X-Worker-Token"),
    x_client_instance_id: str | None = Header(default=None, alias="X-Client-Instance-Id"),
    authorization: str | None = Header(default=None, alias="Authorization"),
):
    try:
        task = task_service.get_task_or_404(db, task_id)
        worker_id = task.worker_id
        if x_worker_token:
            worker = worker_service.authenticate_worker_client(db, worker_id or "", x_worker_token, x_client_instance_id)
            worker_id = worker.id
        else:
            validate_admin_auth(request, authorization)
        data = task_service.add_evidence(
            db,
            task_id,
            worker_id=worker_id,
            evidence_type=payload.evidence_type,
            file_name=payload.file_name,
            storage_url=payload.storage_url,
            content=payload.content,
            error_code=payload.error_code,
            remark=payload.remark,
            metadata=payload.metadata,
        )
        db.commit()
        return ok(data)
    except Exception:
        db.rollback()
        raise
