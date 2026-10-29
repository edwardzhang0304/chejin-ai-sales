import hashlib
import hmac
import json
import re
from typing import Any

from fastapi.responses import JSONResponse
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.api.response import error_response, ok
from app.core.config import get_settings
from app.core.request_context import ActorContext
from app.core.request_id import get_request_id
from app.errors import AppError, DuplicateLeadError
from app.models.feiyu import FeiyuLeadReceipt
from app.schemas.lead import LeadCreate
from app.services import lead_service


_FEIYU_LOCK_NAMESPACE = 1178948457
_ACCOUNT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_RESPONSE_KEYS = (
    "created",
    "id",
    "status",
    "assign_status",
    "sales_id",
    "sales_name",
    "assignment",
)


def _clue_identity(custom_fields: dict[str, Any]) -> str | None:
    feiyu = custom_fields.get("feiyu")
    if not isinstance(feiyu, dict):
        return None
    values = [str(feiyu[key]).strip() for key in ("id", "clue_id") if feiyu.get(key) is not None]
    if len(set(values)) > 1:
        raise AppError("FEIYU_CLUE_ID_CONFLICT", "飞鱼线索编号字段不一致", 400)
    clue_id = values[0] if values else ""
    if not clue_id:
        return None
    if len(clue_id) > 128:
        raise AppError("FEIYU_CLUE_ID_INVALID", "飞鱼线索编号超出长度限制", 400)
    return clue_id


def _contact_fingerprint(payload: LeadCreate) -> str:
    normalized = lead_service._normalize_payload_contacts(payload)
    contacts = [
        (contact_type, contact.contact_hash)
        for contact_type, items in normalized.items()
        for contact in items
    ]
    encoded = json.dumps(sorted(contacts), ensure_ascii=True, separators=(",", ":")).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def _payload_fingerprint(payload: LeadCreate, clue_id: str) -> str:
    canonical = payload.model_dump(mode="json")
    custom_fields = canonical.get("custom_fields")
    feiyu = custom_fields.get("feiyu") if isinstance(custom_fields, dict) else None
    if isinstance(feiyu, dict):
        normalized_feiyu = dict(feiyu)
        normalized_feiyu.pop("clue_id", None)
        normalized_feiyu["id"] = clue_id
        custom_fields["feiyu"] = normalized_feiyu
    encoded = json.dumps(
        canonical,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    secret = get_settings().phone_hash_secret.encode("utf-8")
    return hmac.new(secret, b"feiyu-payload-v1\0" + encoded, hashlib.sha256).hexdigest()


def _lock_identity(db: Session, account_id: str, clue_id: str) -> None:
    if db.get_bind().dialect.name != "postgresql":
        return
    digest = hashlib.sha256(f"{account_id}\0{clue_id}".encode("utf-8")).digest()
    lock_key = int.from_bytes(digest[:4], "big", signed=True)
    db.execute(
        text("SELECT pg_advisory_xact_lock(:namespace, :lock_key)"),
        {"namespace": _FEIYU_LOCK_NAMESPACE, "lock_key": lock_key},
    )


def _compact_success(data: dict[str, Any]) -> dict[str, Any]:
    return ok({key: data[key] for key in _RESPONSE_KEYS if key in data})


def _response_from_receipt(receipt: FeiyuLeadReceipt) -> JSONResponse:
    return JSONResponse(status_code=receipt.response_status, content=receipt.response_body)


def _save_receipt(
    db: Session,
    *,
    account_id: str,
    clue_id: str,
    lead_id: str,
    contact_fingerprint: str,
    payload_fingerprint: str,
    response: JSONResponse,
) -> JSONResponse:
    body = json.loads(response.body.decode("utf-8"))
    db.add(
        FeiyuLeadReceipt(
            account_id=account_id,
            clue_id=clue_id,
            lead_id=lead_id,
            contact_fingerprint=contact_fingerprint,
            payload_fingerprint=payload_fingerprint,
            response_status=response.status_code,
            response_body=body,
        )
    )
    db.flush()
    db.commit()
    return response


def process_feiyu_lead(
    db: Session,
    payload: LeadCreate,
    actor: ActorContext,
    account_id: str,
) -> JSONResponse:
    if not _ACCOUNT_ID_RE.fullmatch(account_id):
        raise AppError("FEIYU_ACCOUNT_ID_INVALID", "飞鱼账号标识格式不正确", 400)

    clue_id = _clue_identity(payload.custom_fields)
    if not clue_id:
        raise AppError(
            "FEIYU_CLUE_ID_REQUIRED",
            "飞鱼线索缺少编号，已拒绝写入；请配置接收器线索编号字段后重试",
            400,
        )

    fingerprint = _contact_fingerprint(payload)
    payload_fingerprint = _payload_fingerprint(payload, clue_id)
    _lock_identity(db, account_id, clue_id)
    existing = db.scalar(
        select(FeiyuLeadReceipt).where(
            FeiyuLeadReceipt.account_id == account_id,
            FeiyuLeadReceipt.clue_id == clue_id,
        )
    )
    if existing:
        if existing.contact_fingerprint != fingerprint:
            raise AppError(
                "FEIYU_CLUE_IDENTITY_CONFLICT",
                "同一飞鱼线索编号对应的联系方式发生变化，已拦截本次处理",
                409,
                {"lead_id": existing.lead_id},
            )
        if existing.payload_fingerprint != payload_fingerprint:
            raise AppError(
                "FEIYU_CLUE_UPDATE_REQUIRES_REVIEW",
                "同一飞鱼线索编号的资料无法验证或已变化，本次更新未应用，请人工核对处理",
                409,
                {
                    "lead_id": existing.lead_id,
                    "update_applied": False,
                    "manual_review_required": True,
                },
            )
        return _response_from_receipt(existing)

    try:
        data = lead_service.create_lead(
            db,
            payload,
            actor,
            source_type="feiyu",
            source_name_snapshot="飞鱼线索",
        )
        response = JSONResponse(status_code=200, content=_compact_success(data))
        return _save_receipt(
            db,
            account_id=account_id,
            clue_id=clue_id,
            lead_id=data["id"],
            contact_fingerprint=fingerprint,
            payload_fingerprint=payload_fingerprint,
            response=response,
        )
    except DuplicateLeadError as exc:
        response = error_response(
            exc.status_code,
            exc.code,
            exc.message,
            exc.data,
            trace_id=get_request_id(),
        )
        lead_id = exc.data.get("duplicate_lead", {}).get("id")
        if not lead_id:
            raise
        return _save_receipt(
            db,
            account_id=account_id,
            clue_id=clue_id,
            lead_id=lead_id,
            contact_fingerprint=fingerprint,
            payload_fingerprint=payload_fingerprint,
            response=response,
        )
