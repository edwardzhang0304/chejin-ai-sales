from fastapi import APIRouter, Depends, Header, Query
from sqlalchemy.orm import Session

from app.api.response import ok
from app.core.auth import require_admin_auth
from app.core.database import get_db
from app.schemas.wechat import WechatMessageIngestRequest, WechatSessionScanResultRequest
from app.services import wechat_service, worker_service


router = APIRouter(tags=["wechat-c2"])


@router.post("/workers/{worker_id}/wechat/sessions/scan-result")
def scan_result(
    worker_id: str,
    payload: WechatSessionScanResultRequest,
    db: Session = Depends(get_db),
    x_worker_token: str | None = Header(default=None, alias="X-Worker-Token"),
    x_client_instance_id: str | None = Header(default=None, alias="X-Client-Instance-Id"),
):
    worker = worker_service.authenticate_worker_client(db, worker_id, x_worker_token, x_client_instance_id)
    try:
        data = wechat_service.ingest_scan_result(db, worker, payload)
        db.commit()
        return ok(data)
    except Exception:
        db.rollback()
        raise


@router.get("/workers/{worker_id}/wechat/sessions/read-targets")
def read_targets(
    worker_id: str,
    limit: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
    x_worker_token: str | None = Header(default=None, alias="X-Worker-Token"),
    x_client_instance_id: str | None = Header(default=None, alias="X-Client-Instance-Id"),
):
    worker = worker_service.authenticate_worker_client(db, worker_id, x_worker_token, x_client_instance_id)
    return ok(wechat_service.read_targets(db, worker, limit))


@router.post("/workers/{worker_id}/wechat/messages/ingest")
def ingest_messages(
    worker_id: str,
    payload: WechatMessageIngestRequest,
    db: Session = Depends(get_db),
    x_worker_token: str | None = Header(default=None, alias="X-Worker-Token"),
    x_client_instance_id: str | None = Header(default=None, alias="X-Client-Instance-Id"),
):
    worker = worker_service.authenticate_worker_client(db, worker_id, x_worker_token, x_client_instance_id)
    try:
        data = wechat_service.ingest_messages(db, worker, payload)
        db.commit()
        return ok(data)
    except Exception:
        db.rollback()
        raise


@router.get("/conversations/{conversation_id}/wechat-binding")
def conversation_binding(
    conversation_id: str,
    db: Session = Depends(get_db),
    _admin_auth: None = Depends(require_admin_auth),
):
    return ok(wechat_service.get_binding_by_conversation(db, conversation_id))


@router.get("/conversations/{conversation_id}/messages")
def conversation_messages(
    conversation_id: str,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
    _admin_auth: None = Depends(require_admin_auth),
):
    return ok(wechat_service.list_messages(db, conversation_id, page, page_size))


@router.get("/leads/{lead_id}/wechat-bindings")
def lead_wechat_bindings(
    lead_id: str,
    db: Session = Depends(get_db),
    _admin_auth: None = Depends(require_admin_auth),
):
    return ok(wechat_service.get_bindings_by_lead(db, lead_id))
