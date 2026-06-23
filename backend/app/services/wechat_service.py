from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.errors import AppError
from app.models.base import utcnow
from app.models.lead import Lead
from app.models.sales import Sales
from app.models.wechat import MessageEvent, WechatSessionBinding
from app.models.worker import Worker
from app.schemas.wechat import WechatMessageIngestRequest, WechatSessionScanItem, WechatSessionScanResultRequest


BIND_STATUS_BOUND = "bound"
BIND_STATUS_ALREADY_BOUND = "already_bound"
BIND_STATUS_UNBOUND = "unbound"
BIND_STATUS_CANDIDATE = "binding_candidate"
BIND_STATUS_NEEDS_REVIEW = "needs_review"
BIND_STATUS_FAILED = "binding_failed"
BIND_STATUS_DISABLED = "disabled"

LISTEN_STATUS_NOT_STARTED = "not_started"
LISTEN_STATUS_LISTENING = "listening"
LISTEN_STATUS_DEGRADED = "degraded"
LISTEN_STATUS_ERROR = "error"
LISTEN_STATUS_DISABLED = "disabled"

NEXT_ACTION_NONE = "none"
LOW_CONFIDENCE_THRESHOLD = 0.7


def _clean_candidates(candidates: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in candidates:
        value = str(item or "").strip()
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _find_leads_by_remark_code(db: Session, remark_code: str) -> list[Lead]:
    rows = db.scalars(select(Lead).where(Lead.deleted_at.is_(None))).all()
    return [
        lead
        for lead in rows
        if isinstance(lead.custom_fields, dict) and str(lead.custom_fields.get("remark_code") or "").strip() == remark_code
    ]


def _binding_to_dict(binding: WechatSessionBinding) -> dict:
    return {
        "id": binding.id,
        "conversation_id": binding.conversation_id,
        "lead_id": binding.lead_id,
        "sales_id": binding.sales_id,
        "worker_id": binding.worker_id,
        "remark_code": binding.remark_code,
        "display_name": binding.display_name,
        "rpa_session_key": binding.rpa_session_key,
        "row_fingerprint": binding.row_fingerprint,
        "bind_status": binding.bind_status,
        "listen_status": binding.listen_status,
        "allow_listening": binding.allow_listening,
        "reason_code": binding.reason_code,
        "error_code": binding.error_code,
        "unread_hint": binding.unread_hint,
        "last_message_preview": binding.last_message_preview,
        "ocr_confidence": binding.ocr_confidence,
        "first_seen_at": binding.first_seen_at,
        "last_seen_at": binding.last_seen_at,
        "last_ingested_at": binding.last_ingested_at,
        "last_scan_snapshot": binding.last_scan_snapshot,
        "created_at": binding.created_at,
        "updated_at": binding.updated_at,
    }


def _message_to_dict(message: MessageEvent) -> dict:
    return {
        "id": message.id,
        "conversation_id": message.conversation_id,
        "binding_id": message.binding_id,
        "lead_id": message.lead_id,
        "sales_id": message.sales_id,
        "worker_id": message.worker_id,
        "rpa_session_key": message.rpa_session_key,
        "read_run_id": message.read_run_id,
        "dedupe_key": message.dedupe_key,
        "sender_role": message.sender_role,
        "message_type": message.message_type,
        "content": message.content,
        "image_local_path": message.image_local_path,
        "raw_payload": message.raw_payload,
        "evidence": message.evidence,
        "ocr_confidence": message.ocr_confidence,
        "occurred_at": message.occurred_at,
        "ingested_at": message.ingested_at,
        "ingest_status": message.ingest_status,
        "error_code": message.error_code,
    }


def _scan_snapshot(payload: WechatSessionScanResultRequest, item: WechatSessionScanItem) -> dict:
    return {
        "scan_id": payload.scan_id,
        "sidecar_run_id": payload.sidecar_run_id,
        "wechat_account_hint": payload.wechat_account_hint,
        "started_at": payload.started_at.isoformat(),
        "finished_at": payload.finished_at.isoformat(),
        "evidence": payload.evidence or {},
        "remark_code_candidates": item.remark_code_candidates,
    }


def _session_binding(db: Session, worker: Worker, rpa_session_key: str) -> WechatSessionBinding | None:
    return db.scalar(
        select(WechatSessionBinding).where(
            WechatSessionBinding.worker_id == worker.id,
            WechatSessionBinding.rpa_session_key == rpa_session_key,
            WechatSessionBinding.deleted_at.is_(None),
        )
    )


def _remark_code_binding(db: Session, worker: Worker, remark_code: str) -> WechatSessionBinding | None:
    return db.scalar(
        select(WechatSessionBinding).where(
            WechatSessionBinding.worker_id == worker.id,
            WechatSessionBinding.remark_code == remark_code,
            WechatSessionBinding.bind_status != BIND_STATUS_DISABLED,
            WechatSessionBinding.deleted_at.is_(None),
        )
    )


def _apply_scan_fields(binding: WechatSessionBinding, payload: WechatSessionScanResultRequest, item: WechatSessionScanItem) -> None:
    now = utcnow()
    binding.display_name = item.display_name
    binding.rpa_session_key = item.rpa_session_key
    binding.row_fingerprint = item.row_fingerprint
    binding.unread_hint = item.unread_hint
    binding.last_message_preview = item.last_message_preview
    binding.ocr_confidence = item.ocr_confidence
    binding.last_seen_at = now
    binding.last_scan_snapshot = _scan_snapshot(payload, item)


def _upsert_binding_base(
    db: Session,
    worker: Worker,
    payload: WechatSessionScanResultRequest,
    item: WechatSessionScanItem,
    *,
    remark_code: str | None = None,
) -> WechatSessionBinding:
    binding = _remark_code_binding(db, worker, remark_code) if remark_code else None
    if binding and binding.rpa_session_key != item.rpa_session_key:
        stale_session_binding = _session_binding(db, worker, item.rpa_session_key)
        if stale_session_binding and stale_session_binding.id != binding.id:
            db.delete(stale_session_binding)
            db.flush()
    if not binding:
        binding = _session_binding(db, worker, item.rpa_session_key)
    if not binding:
        now = utcnow()
        binding = WechatSessionBinding(
            worker_id=worker.id,
            display_name=item.display_name,
            rpa_session_key=item.rpa_session_key,
            row_fingerprint=item.row_fingerprint,
            bind_status=BIND_STATUS_UNBOUND,
            listen_status=LISTEN_STATUS_NOT_STARTED,
            allow_listening=False,
            unread_hint=item.unread_hint,
            first_seen_at=now,
            last_seen_at=now,
            last_scan_snapshot=_scan_snapshot(payload, item),
        )
        db.add(binding)
        db.flush()
    _apply_scan_fields(binding, payload, item)
    return binding


def _set_binding_state(
    binding: WechatSessionBinding,
    *,
    status: str,
    listen_status: str,
    allow_listening: bool,
    reason_code: str | None = None,
    error_code: str | None = None,
    lead: Lead | None = None,
    remark_code: str | None = None,
) -> None:
    binding.bind_status = status
    binding.listen_status = listen_status
    binding.allow_listening = allow_listening
    binding.reason_code = reason_code
    binding.error_code = error_code
    binding.lead_id = lead.id if lead else None
    binding.sales_id = lead.sales_id if lead else None
    binding.remark_code = remark_code


def _bind_one_session(db: Session, worker: Worker, payload: WechatSessionScanResultRequest, item: WechatSessionScanItem) -> dict:
    candidates = _clean_candidates(item.remark_code_candidates)
    remark_code_anchor = candidates[0] if len(candidates) == 1 else None
    binding = _upsert_binding_base(db, worker, payload, item, remark_code=remark_code_anchor)
    if binding.bind_status == BIND_STATUS_DISABLED:
        return {
            **_binding_to_dict(binding),
            "bind_status": BIND_STATUS_DISABLED,
            "can_ingest_messages": False,
            "reason_code": "SESSION_BINDING_DISABLED",
        }

    if not candidates:
        _set_binding_state(
            binding,
            status=BIND_STATUS_UNBOUND,
            listen_status=LISTEN_STATUS_NOT_STARTED,
            allow_listening=False,
            reason_code="SESSION_REMARK_CODE_NOT_FOUND",
            error_code="SESSION_REMARK_CODE_NOT_FOUND",
        )
        return {**_binding_to_dict(binding), "can_ingest_messages": False}

    was_bound_to_lead_id = binding.lead_id if binding.bind_status == BIND_STATUS_BOUND else None
    binding.bind_status = BIND_STATUS_CANDIDATE
    if len(candidates) > 1:
        _set_binding_state(
            binding,
            status=BIND_STATUS_NEEDS_REVIEW,
            listen_status=LISTEN_STATUS_NOT_STARTED,
            allow_listening=False,
            reason_code="SESSION_REMARK_CODE_DUPLICATED",
            error_code="SESSION_REMARK_CODE_DUPLICATED",
            remark_code=",".join(candidates),
        )
        return {**_binding_to_dict(binding), "can_ingest_messages": False}

    remark_code = candidates[0]
    matches = _find_leads_by_remark_code(db, remark_code)
    if not matches:
        _set_binding_state(
            binding,
            status=BIND_STATUS_FAILED,
            listen_status=LISTEN_STATUS_ERROR,
            allow_listening=False,
            reason_code="SESSION_REMARK_CODE_INVALID",
            error_code="SESSION_REMARK_CODE_INVALID",
            remark_code=remark_code,
        )
        return {**_binding_to_dict(binding), "can_ingest_messages": False}
    if len(matches) > 1:
        _set_binding_state(
            binding,
            status=BIND_STATUS_NEEDS_REVIEW,
            listen_status=LISTEN_STATUS_NOT_STARTED,
            allow_listening=False,
            reason_code="SESSION_REMARK_CODE_DUPLICATED",
            error_code="SESSION_REMARK_CODE_DUPLICATED",
            remark_code=remark_code,
        )
        return {**_binding_to_dict(binding), "can_ingest_messages": False}

    lead = matches[0]
    sales = db.get(Sales, lead.sales_id) if lead.sales_id else None
    if sales and sales.worker_id and sales.worker_id != worker.id:
        _set_binding_state(
            binding,
            status=BIND_STATUS_NEEDS_REVIEW,
            listen_status=LISTEN_STATUS_NOT_STARTED,
            allow_listening=False,
            reason_code="SESSION_BINDING_CONFLICT",
            error_code="SESSION_BINDING_CONFLICT",
            remark_code=remark_code,
        )
        return {**_binding_to_dict(binding), "can_ingest_messages": False}

    other = db.scalar(
        select(WechatSessionBinding).where(
            WechatSessionBinding.lead_id == lead.id,
            WechatSessionBinding.id != binding.id,
            WechatSessionBinding.bind_status == BIND_STATUS_BOUND,
            WechatSessionBinding.deleted_at.is_(None),
        )
    )
    if other:
        _set_binding_state(
            binding,
            status=BIND_STATUS_NEEDS_REVIEW,
            listen_status=LISTEN_STATUS_NOT_STARTED,
            allow_listening=False,
            reason_code="SESSION_BINDING_CONFLICT",
            error_code="SESSION_BINDING_CONFLICT",
            remark_code=remark_code,
        )
        return {**_binding_to_dict(binding), "can_ingest_messages": False}

    already_bound = was_bound_to_lead_id == lead.id
    _set_binding_state(
        binding,
        status=BIND_STATUS_BOUND,
        listen_status=LISTEN_STATUS_LISTENING,
        allow_listening=True,
        lead=lead,
        remark_code=remark_code,
    )
    result = _binding_to_dict(binding)
    result["bind_status"] = BIND_STATUS_ALREADY_BOUND if already_bound else BIND_STATUS_BOUND
    result["can_ingest_messages"] = True
    return result


def ingest_scan_result(db: Session, worker: Worker, payload: WechatSessionScanResultRequest) -> dict:
    if payload.scan_failed:
        return {
            "accepted_count": 0,
            "bound_count": 0,
            "needs_review_count": 0,
            "bindings": [],
            "next_action": NEXT_ACTION_NONE,
            "error_code": payload.error_code or "SESSION_SCAN_FAILED",
        }

    bindings = [_bind_one_session(db, worker, payload, item) for item in payload.sessions]
    db.flush()
    return {
        "accepted_count": len(payload.sessions),
        "bound_count": sum(1 for item in bindings if item["bind_status"] in {BIND_STATUS_BOUND, BIND_STATUS_ALREADY_BOUND}),
        "needs_review_count": sum(1 for item in bindings if item["bind_status"] == BIND_STATUS_NEEDS_REVIEW),
        "bindings": bindings,
        "next_action": NEXT_ACTION_NONE,
    }


def read_targets(db: Session, worker: Worker, limit: int = 20) -> dict:
    rows = list(
        db.scalars(
            select(WechatSessionBinding)
            .where(
                WechatSessionBinding.worker_id == worker.id,
                WechatSessionBinding.bind_status == BIND_STATUS_BOUND,
                WechatSessionBinding.listen_status == LISTEN_STATUS_LISTENING,
                WechatSessionBinding.allow_listening.is_(True),
                WechatSessionBinding.deleted_at.is_(None),
            )
            .order_by(WechatSessionBinding.unread_hint.desc(), WechatSessionBinding.last_ingested_at.asc().nullsfirst(), WechatSessionBinding.last_seen_at.desc())
            .limit(limit)
        )
    )
    return {
        "targets": [
            {
                "conversation_id": item.conversation_id,
                "lead_id": item.lead_id,
                "sales_id": item.sales_id,
                "rpa_session_key": item.rpa_session_key,
                "display_name": item.display_name,
                "last_ingested_at": item.last_ingested_at,
                "read_reason": "unread_hint" if item.unread_hint else "periodic_scan",
            }
            for item in rows
        ],
        "poll_after_seconds": 10,
        "next_action": NEXT_ACTION_NONE,
    }


def _sender_role(value: str) -> str:
    mapping = {"self": "sales_candidate", "customer": "customer", "system": "system", "unknown": "unknown"}
    return mapping.get(value, value if value in {"ai_worker", "sales_candidate"} else "unknown")


def ingest_messages(db: Session, worker: Worker, payload: WechatMessageIngestRequest) -> dict:
    binding = db.scalar(
        select(WechatSessionBinding).where(
            WechatSessionBinding.conversation_id == payload.conversation_id,
            WechatSessionBinding.worker_id == worker.id,
            WechatSessionBinding.rpa_session_key == payload.rpa_session_key,
            WechatSessionBinding.deleted_at.is_(None),
        )
    )
    if not binding or binding.bind_status != BIND_STATUS_BOUND or not binding.allow_listening:
        raise AppError("MESSAGE_CONVERSATION_NOT_BOUND", "会话未绑定，不能入库消息", 409)

    ingested_count = 0
    duplicated_count = 0
    ignored_count = 0
    results: list[dict] = []
    for item in payload.messages:
        if not item.dedupe_key or not item.dedupe_key.strip():
            raise AppError("MESSAGE_DEDUPE_KEY_MISSING", "消息缺少 dedupe_key", 400)
        dedupe_key = item.dedupe_key.strip()
        exists = db.scalar(
            select(MessageEvent).where(
                MessageEvent.worker_id == worker.id,
                MessageEvent.conversation_id == payload.conversation_id,
                MessageEvent.dedupe_key == dedupe_key,
            )
        )
        if exists:
            duplicated_count += 1
            results.append({"dedupe_key": dedupe_key, "ingest_status": "duplicated", "error_code": "MESSAGE_INGEST_DUPLICATED"})
            continue

        message = MessageEvent(
            conversation_id=payload.conversation_id,
            binding_id=binding.id,
            lead_id=binding.lead_id,
            sales_id=binding.sales_id,
            worker_id=worker.id,
            rpa_session_key=payload.rpa_session_key,
            read_run_id=payload.read_run_id,
            dedupe_key=dedupe_key,
            sender_role=_sender_role(item.sender_role_hint),
            message_type=item.message_type,
            content=item.content,
            image_local_path=item.image_local_path,
            raw_payload=item.raw_payload or {},
            evidence=payload.evidence or {},
            ocr_confidence=item.ocr_confidence,
            occurred_at=item.occurred_at,
            ingested_at=utcnow(),
            ingest_status="ingested",
        )
        db.add(message)
        db.flush()
        binding.last_ingested_at = message.ingested_at
        ingested_count += 1
        results.append({"dedupe_key": dedupe_key, "ingest_status": "ingested", "message_id": message.id})

    db.flush()
    return {
        "ingested_count": ingested_count,
        "duplicated_count": duplicated_count,
        "ignored_count": ignored_count,
        "conversation_status": "bound",
        "results": results,
        "next_action": NEXT_ACTION_NONE,
    }


def get_binding_by_conversation(db: Session, conversation_id: str) -> dict:
    binding = db.scalar(select(WechatSessionBinding).where(WechatSessionBinding.conversation_id == conversation_id, WechatSessionBinding.deleted_at.is_(None)))
    if not binding:
        raise AppError("WECHAT_BINDING_NOT_FOUND", "微信会话绑定不存在", 404)
    return _binding_to_dict(binding)


def get_bindings_by_lead(db: Session, lead_id: str) -> dict:
    items = list(db.scalars(select(WechatSessionBinding).where(WechatSessionBinding.lead_id == lead_id, WechatSessionBinding.deleted_at.is_(None)).order_by(WechatSessionBinding.updated_at.desc())))
    return {"items": [_binding_to_dict(item) for item in items]}


def list_messages(db: Session, conversation_id: str, page: int = 1, page_size: int = 20) -> dict:
    query = select(MessageEvent).where(MessageEvent.conversation_id == conversation_id).order_by(MessageEvent.ingested_at.desc())
    items = list(db.scalars(query.offset((page - 1) * page_size).limit(page_size)))
    return {"items": [_message_to_dict(item) for item in items], "page": page, "page_size": page_size}
