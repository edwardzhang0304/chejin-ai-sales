import logging

from fastapi import APIRouter, BackgroundTasks, Depends, Header, Query
from sqlalchemy.orm import Session

from app.api.response import ok
from app.core.auth import require_admin_auth
from app.core.config import get_settings
from app.core.database import get_db
from app.core.request_context import ActorContext, get_actor_context
from app.errors import AppError
from app.schemas.wechat import (
    WechatBindingRestoreRequest,
    WechatFriendActivationConfirmRequest,
    WechatMessageIngestRequest,
    WechatSessionScanResultRequest,
)
from app.services import c3_service, wechat_service, worker_service


router = APIRouter(tags=["wechat-c2"])
logger = logging.getLogger(__name__)


@router.post("/workers/{worker_id}/wechat/sessions/scan-result")
def scan_result(
    worker_id: str,
    payload: WechatSessionScanResultRequest,
    db: Session = Depends(get_db),
    x_worker_token: str | None = Header(default=None, alias="X-Worker-Token"),
    x_client_instance_id: str | None = Header(default=None, alias="X-Client-Instance-Id"),
):
    worker = worker_service.authenticate_worker_client(db, worker_id, x_worker_token, x_client_instance_id)
    worker_service.validate_inflight_continuation(worker, None, new_work=True)
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
    worker_service.validate_inflight_continuation(worker, None, new_work=True)
    try:
        data = wechat_service.read_targets(db, worker, limit)
        db.commit()
        return ok(data)
    except Exception:
        db.rollback()
        raise


@router.get(
    "/workers/{worker_id}/wechat/conversations/{conversation_id}/read-authorization"
)
def read_authorization(
    worker_id: str,
    conversation_id: str,
    continuation_batch_id: str | None = Query(default=None, max_length=36),
    recovery_transaction_id: str | None = Query(default=None, max_length=128),
    action_kind: str | None = Query(default=None, max_length=16),
    source_message_key_digest: str | None = Query(default=None, max_length=64),
    original_authorization_revision: str | None = Query(
        default=None,
        max_length=128,
    ),
    db: Session = Depends(get_db),
    continuation_token: str | None = Header(
        default=None,
        alias="X-C2-Continuation-Token",
        max_length=64,
    ),
    x_worker_token: str | None = Header(default=None, alias="X-Worker-Token"),
    x_client_instance_id: str | None = Header(
        default=None,
        alias="X-Client-Instance-Id",
    ),
    x_inflight_flow_id: str | None = Header(default=None, alias="X-Inflight-Flow-Id"),
):
    worker = worker_service.authenticate_worker_client(
        db,
        worker_id,
        x_worker_token,
        x_client_instance_id,
    )
    worker_service.validate_inflight_continuation(worker, x_inflight_flow_id)
    try:
        data = wechat_service.read_authorization_for_worker(
            db,
            worker=worker,
            conversation_id=conversation_id,
            continuation_batch_id=continuation_batch_id,
            continuation_token=continuation_token,
            recovery_transaction_id=recovery_transaction_id,
            action_kind=action_kind,
            source_message_key_digest=source_message_key_digest,
            original_authorization_revision=original_authorization_revision,
        )
        db.commit()
        return ok(data)
    except Exception:
        db.rollback()
        raise


@router.post("/workers/{worker_id}/wechat/conversations/{conversation_id}/activation-confirm")
def confirm_friend_activation(
    worker_id: str,
    conversation_id: str,
    payload: WechatFriendActivationConfirmRequest,
    db: Session = Depends(get_db),
    x_worker_token: str | None = Header(default=None, alias="X-Worker-Token"),
    x_client_instance_id: str | None = Header(default=None, alias="X-Client-Instance-Id"),
    x_inflight_flow_id: str | None = Header(default=None, alias="X-Inflight-Flow-Id"),
):
    worker = worker_service.authenticate_worker_client(db, worker_id, x_worker_token, x_client_instance_id)
    worker_service.validate_inflight_continuation(worker, x_inflight_flow_id)
    try:
        data = wechat_service.confirm_friend_activation(db, worker, conversation_id, payload)
        db.commit()
        return ok(data)
    except Exception:
        db.rollback()
        raise


@router.post("/workers/{worker_id}/wechat/messages/ingest")
def ingest_messages(
    worker_id: str,
    payload: WechatMessageIngestRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    x_worker_token: str | None = Header(default=None, alias="X-Worker-Token"),
    x_client_instance_id: str | None = Header(default=None, alias="X-Client-Instance-Id"),
    x_c2_settlement_token: str | None = Header(
        default=None,
        alias="X-C2-Settlement-Token",
    ),
    x_inflight_flow_id: str | None = Header(default=None, alias="X-Inflight-Flow-Id"),
):
    worker = worker_service.authenticate_worker_client(db, worker_id, x_worker_token, x_client_instance_id)
    worker_service.validate_inflight_continuation(worker, x_inflight_flow_id)
    try:
        data = (
            wechat_service.settle_messages_without_ui(
                db,
                worker,
                payload,
                settlement_token=x_c2_settlement_token,
            )
            if payload.authorization_scope == "fact_settlement"
            else wechat_service.ingest_messages(db, worker, payload)
        )
        db.commit()
        message_batch = data.get("message_batch") if isinstance(data, dict) else None
        if (
            payload.authorization_scope == "active_read"
            and isinstance(message_batch, dict)
            and message_batch.get("batch_id")
            and str(message_batch.get("batch_status") or "") in {"collecting", "generating"}
        ):
            claim = c3_service.claim_message_batch_generation(
                db,
                batch_id=str(message_batch["batch_id"]),
            )
            db.commit()
            if claim.get("run"):
                attempt = int(claim["attempt"])
                if get_settings().c3_ai_adapter_mode == "mock":
                    _generate_message_batch(str(message_batch["batch_id"]), attempt)
                else:
                    background_tasks.add_task(
                        _generate_message_batch,
                        str(message_batch["batch_id"]),
                        attempt,
                    )
        return ok(data)
    except AppError as exc:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise


def _generate_message_batch(batch_id: str, expected_generation_attempt: int) -> None:
    from app.core.database import SessionLocal

    db = SessionLocal()
    try:
        c3_service.generate_for_batch(
            db,
            batch_id=batch_id,
            expected_generation_attempt=expected_generation_attempt,
        )
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("C3 message batch generation failed", extra={"batch_id": batch_id})
        raise
    finally:
        db.close()


@router.get("/workers/{worker_id}/wechat/message-batches/{batch_id}")
def get_message_batch(
    worker_id: str,
    batch_id: str,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    x_worker_token: str | None = Header(default=None, alias="X-Worker-Token"),
    x_client_instance_id: str | None = Header(default=None, alias="X-Client-Instance-Id"),
):
    worker = worker_service.authenticate_worker_client(db, worker_id, x_worker_token, x_client_instance_id)
    try:
        data = c3_service.get_message_batch_for_worker(db, worker=worker, batch_id=batch_id)
        if data.get("processing"):
            claim = c3_service.claim_message_batch_generation(
                db,
                batch_id=batch_id,
                stale_only=str(data.get("batch_status") or "") != "collecting",
            )
            db.commit()
            if claim.get("run"):
                attempt = int(claim["attempt"])
                if get_settings().c3_ai_adapter_mode == "mock":
                    _generate_message_batch(batch_id, attempt)
                else:
                    background_tasks.add_task(_generate_message_batch, batch_id, attempt)
            if claim.get("terminal"):
                data = c3_service.get_message_batch_for_worker(db, worker=worker, batch_id=batch_id)
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


@router.post("/conversations/{conversation_id}/wechat-binding/restore")
def restore_conversation_binding(
    conversation_id: str,
    payload: WechatBindingRestoreRequest,
    db: Session = Depends(get_db),
    actor: ActorContext = Depends(get_actor_context),
):
    try:
        data = wechat_service.restore_binding(
            db,
            conversation_id=conversation_id,
            reason=payload.reason,
            actor=actor,
        )
        db.commit()
        return ok(data)
    except Exception:
        db.rollback()
        raise


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
