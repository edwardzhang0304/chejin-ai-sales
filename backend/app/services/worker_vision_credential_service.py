"""The only storage/decryption boundary for Worker Vision credentials."""

import json

from pydantic import SecretStr
from sqlalchemy.orm import Session

from app.core.request_context import ActorContext
from app.errors import AppError
from app.models.base import utcnow
from app.models.worker import Worker
from app.services.audit_service import write_log
from app.services.worker_token_service import _cipher


def credential_status(worker: Worker) -> dict:
    return {
        "vision_configured": bool(worker.vision_api_key_encrypted),
        "vision_credential_updated_at": worker.vision_credential_updated_at,
    }


def save_credential(db: Session, worker: Worker, key: SecretStr | None, actor: ActorContext, *, clear: bool = False) -> dict:
    if key is None and not clear:
        return credential_status(worker)
    if clear:
        worker.vision_api_key_encrypted = None
    else:
        try:
            envelope = json.dumps({"worker_id": worker.id, "key": key.get_secret_value()}).encode("utf-8")
            worker.vision_api_key_encrypted = _cipher().encrypt(envelope).decode("ascii")
        except Exception:
            raise AppError("VISION_CREDENTIAL_SAVE_FAILED", "Vision 配置保存失败", 500) from None
    worker.vision_credential_updated_at = utcnow()
    worker.vision_credential_updated_by = str(actor.operator_id)
    db.flush()
    write_log(
        db, actor, event_type="worker_vision_credential_cleared" if clear else "worker_vision_credential_saved",
        module="worker", target_type="worker", target_id=worker.id,
        after_data={"result": "success"},
    )
    return credential_status(worker)


def read_credential(worker: Worker) -> dict:
    key = None
    if worker.vision_api_key_encrypted:
        try:
            envelope = json.loads(_cipher().decrypt(worker.vision_api_key_encrypted.encode("ascii")))
            key = envelope["key"]
            if envelope["worker_id"] != worker.id or not isinstance(key, str) or not key.strip():
                raise ValueError()
        except Exception:
            raise AppError("VISION_CREDENTIAL_DECRYPT_FAILED", "Vision 配置读取失败", 503) from None
    return {
        "worker_id": worker.id, "client_instance_id": worker.client_instance_id,
        "configured": key is not None, "vision_api_key": key,
    }
