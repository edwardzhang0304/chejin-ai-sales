from fastapi import APIRouter, Depends, Header
from sqlalchemy.orm import Session

from app.api.response import ok
from app.core.auth import require_internal_service_auth
from app.core.database import get_db
from app.errors import AppError
from app.schemas.c3 import (
    MessageBatchCollectRequest,
    MessageBatchGenerateRequest,
    ReplyActionClaimSendRequest,
    ReplyActionSentAckRequest,
)
from app.services import c3_service, worker_service


router = APIRouter(tags=["c3"])


@router.post("/internal/conversations/{conversation_id}/message-batches/collect")
def collect_message_batch(
    conversation_id: str,
    payload: MessageBatchCollectRequest,
    db: Session = Depends(get_db),
    _internal_auth: None = Depends(require_internal_service_auth),
):
    try:
        data = c3_service.collect_message_batch(
            db,
            conversation_id=conversation_id,
            trigger_message_event_id=payload.trigger_message_event_id,
            trace_id=payload.trace_id,
        )
        db.commit()
        return ok(data)
    except Exception:
        db.rollback()
        raise


@router.post("/internal/message-batches/{batch_id}/generate")
def generate_message_batch(
    batch_id: str,
    payload: MessageBatchGenerateRequest,
    db: Session = Depends(get_db),
    _internal_auth: None = Depends(require_internal_service_auth),
):
    try:
        data = c3_service.generate_for_batch(db, batch_id=batch_id, force=payload.force)
        db.commit()
        return ok(data)
    except Exception:
        db.rollback()
        raise


@router.post("/reply-actions/{reply_action_id}/claim-send")
def claim_send(
    reply_action_id: str,
    payload: ReplyActionClaimSendRequest,
    db: Session = Depends(get_db),
    x_worker_token: str | None = Header(default=None, alias="X-Worker-Token"),
    x_client_instance_id: str | None = Header(default=None, alias="X-Client-Instance-Id"),
    x_task_lease_fencing_token: int | None = Header(default=None, alias="X-Task-Lease-Fencing-Token"),
):
    try:
        worker_service.authenticate_worker_client(db, payload.worker_id, x_worker_token, x_client_instance_id)
        data = c3_service.claim_send(
            db,
            reply_action_id=reply_action_id,
            task_id=payload.task_id,
            worker_id=payload.worker_id,
            client_instance_id=x_client_instance_id,
            lease_fencing_token=x_task_lease_fencing_token,
        )
        db.commit()
        return ok(data)
    except AppError as exc:
        if exc.code == c3_service.VEHICLE_FACT_STALE_CODE:
            db.commit()
        else:
            db.rollback()
        raise
    except Exception:
        db.rollback()
        raise


@router.post("/reply-actions/{reply_action_id}/sent-ack")
def sent_ack(
    reply_action_id: str,
    payload: ReplyActionSentAckRequest,
    db: Session = Depends(get_db),
    x_worker_token: str | None = Header(default=None, alias="X-Worker-Token"),
    x_client_instance_id: str | None = Header(default=None, alias="X-Client-Instance-Id"),
):
    try:
        worker_service.authenticate_worker_client(db, payload.worker_id, x_worker_token, x_client_instance_id or payload.client_instance_id)
        data = c3_service.sent_ack(db, reply_action_id=reply_action_id, payload=payload)
        db.commit()
        return ok(data)
    except Exception:
        db.rollback()
        raise
