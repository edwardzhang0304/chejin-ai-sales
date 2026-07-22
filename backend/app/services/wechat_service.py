from __future__ import annotations

from datetime import datetime
import hashlib
import logging
import re

from fastapi.encoders import jsonable_encoder
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.request_id import get_request_id
from app.contracts.c2 import c2_contract_v3, contract_revision, contract_row_rules, contract_sha256, contract_values
from app.errors import AppError
from app.models.base import utcnow
from app.models.c3 import Conversation
from app.models.lead import Lead
from app.models.sales import Sales
from app.models.wechat import MessageEvent, WechatScanRun, WechatSessionBinding
from app.models.worker import Worker
from app.schemas.wechat import WechatMessageIngestRequest, WechatSessionScanItem, WechatSessionScanResultRequest


BIND_STATUS_BOUND = "bound"
BIND_STATUS_ALREADY_BOUND = "already_bound"
BIND_STATUS_UNBOUND = "unbound"
BIND_STATUS_CANDIDATE = "binding_candidate"
BIND_STATUS_NEEDS_REVIEW = "needs_review"
BIND_STATUS_FAILED = "binding_failed"
BIND_STATUS_DISABLED = "disabled"
EFFECTIVE_BIND_STATUSES = {BIND_STATUS_BOUND, BIND_STATUS_CANDIDATE, BIND_STATUS_NEEDS_REVIEW}

LISTEN_STATUS_NOT_STARTED = "not_started"
LISTEN_STATUS_LISTENING = "listening"
LISTEN_STATUS_DEGRADED = "degraded"
LISTEN_STATUS_ERROR = "error"
LISTEN_STATUS_DISABLED = "disabled"

NEXT_ACTION_NONE = "none"
LOW_CONFIDENCE_THRESHOLD = 0.7
CONVERSATION_CLOSED_STATUSES = {"closed", "rejected"}
SALES_SIDE_SENDER_ROLES = {"self", "sales", "sales_candidate"}
MESSAGE_TYPES_V3 = contract_values("message_types")
SENDER_ROLES_V3 = contract_values("sender_roles")
FLOW_STATES_V3 = contract_values("flow_states")
ROW_RULES_V3 = contract_row_rules()
CONTRACT_REVISION_V3 = contract_revision()
CONTRACT_SHA256_V3 = contract_sha256()
OBSERVATION_SCHEMA_VERSION_V3 = int(c2_contract_v3()["observation_schema_version"])
VOICE_FAILURE_ERROR_CODES = {
    "VOICE_TRANSCRIBE_FAILED",
    "VOICE_TRANSCRIBE_CLICK_FAILED",
    "VOICE_TRANSCRIBE_LOCK_TIMEOUT",
    "VOICE_TRANSCRIBE_EMPTY",
    "VOICE_MESSAGE_UNCONFIRMED",
    "TARGET_NOT_CONFIRMED_FOR_VOICE_TRANSCRIBE",
}
IMAGE_RECOGNITION_STATUSES = {"succeeded", "failed"}
READ_TARGET_FAILURE_RESULTS = {"target_not_confirmed", "search_not_found", "search_ambiguous"}
READ_REASON_PRIORITY = {
    "recall_precheck": 0,
    "recent_ai_sent": 1,
    "waiting_user_reply": 2,
    "waiting_sales_reply": 3,
}
VOICE_DURATION_RE = re.compile(
    r"^\s*(?:\[?语音\]?\s*)?\d{1,3}(?:\.\d+)?\s*(?:\"|”|″|秒|s|S)\s*$"
)
logger = logging.getLogger(__name__)


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


def _clean_locator(value: str | None) -> str:
    return str(value or "").strip()


def _session_binding(db: Session, worker: Worker, rpa_session_key: str) -> WechatSessionBinding | None:
    return db.scalar(
        select(WechatSessionBinding).where(
            WechatSessionBinding.worker_id == worker.id,
            WechatSessionBinding.rpa_session_key == rpa_session_key,
            WechatSessionBinding.deleted_at.is_(None),
        )
    )


def _binding_has_messages(db: Session, binding: WechatSessionBinding) -> bool:
    if binding.last_ingested_at:
        return True
    return (
        db.scalar(
            select(MessageEvent.id).where(MessageEvent.binding_id == binding.id).limit(1)
        )
        is not None
    )


def _remark_code_binding(db: Session, worker: Worker, remark_code: str) -> WechatSessionBinding | None:
    rows = list(
        db.scalars(
            select(WechatSessionBinding).where(
                WechatSessionBinding.remark_code == remark_code,
                WechatSessionBinding.deleted_at.is_(None),
            )
        )
    )
    if not rows:
        return None
    message_binding_ids = {
        row.id
        for row in rows
        if row.last_ingested_at or _binding_has_messages(db, row)
    }

    # remark_code is the business identity anchor. Prefer the canonical row that
    # already owns message history, so a new empty duplicate cannot steal future
    # reads into a fresh conversation_id.
    rows.sort(
        key=lambda item: (
            item.id in message_binding_ids,
            item.deleted_at is None,
            item.bind_status == BIND_STATUS_BOUND,
            item.allow_listening,
            item.first_seen_at or item.created_at,
        ),
        reverse=True,
    )
    return rows[0]


def _retire_duplicate_effective_remark_bindings(
    db: Session,
    *,
    canonical: WechatSessionBinding,
    remark_code: str,
) -> None:
    rows = list(
        db.scalars(
            select(WechatSessionBinding).where(
                WechatSessionBinding.remark_code == remark_code,
                WechatSessionBinding.id != canonical.id,
                WechatSessionBinding.deleted_at.is_(None),
                WechatSessionBinding.bind_status.in_(EFFECTIVE_BIND_STATUSES),
            )
        )
    )
    for duplicate in rows:
        _retire_stale_session_binding(duplicate, replacement_binding_id=canonical.id)
    if rows:
        db.flush()


def _apply_scan_fields(binding: WechatSessionBinding, payload: WechatSessionScanResultRequest, item: WechatSessionScanItem) -> None:
    now = utcnow()
    binding.display_name = item.display_name
    binding.rpa_session_key = item.rpa_session_key
    binding.row_fingerprint = item.row_fingerprint or ""
    binding.unread_hint = item.unread_hint
    binding.last_message_preview = item.last_message_preview
    binding.ocr_confidence = item.ocr_confidence
    binding.last_seen_at = now
    binding.last_scan_snapshot = _scan_snapshot(payload, item)


def _retire_stale_session_binding(binding: WechatSessionBinding, *, replacement_binding_id: str) -> None:
    now = utcnow()
    original_session_key = str(binding.rpa_session_key or "").strip()
    _set_binding_state(
        binding,
        status=BIND_STATUS_DISABLED,
        listen_status=LISTEN_STATUS_DISABLED,
        allow_listening=False,
        error_code="SESSION_BINDING_REPLACED_BY_REMARK_CODE",
        remark_code=binding.remark_code,
        preserve_lead=True,
    )
    binding.deleted_at = now
    if original_session_key:
        binding.rpa_session_key = f"{original_session_key}#retired#{binding.id}"
    else:
        binding.rpa_session_key = f"retired#{binding.id}"
    snapshot = binding.last_scan_snapshot if isinstance(binding.last_scan_snapshot, dict) else {}
    binding.last_scan_snapshot = {
        **snapshot,
        "retired_at": now.isoformat(),
        "retired_reason": "same_worker_same_remark_code_session_key_changed",
        "replacement_binding_id": replacement_binding_id,
        "original_rpa_session_key": original_session_key,
    }


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
            _retire_stale_session_binding(stale_session_binding, replacement_binding_id=binding.id)
            db.flush()
    if not binding:
        binding = _session_binding(db, worker, item.rpa_session_key)
    if not binding:
        now = utcnow()
        binding = WechatSessionBinding(
            worker_id=worker.id,
            display_name=item.display_name,
            rpa_session_key=item.rpa_session_key,
            row_fingerprint=item.row_fingerprint or "",
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
    error_code: str | None = None,
    lead: Lead | None = None,
    remark_code: str | None = None,
    preserve_lead: bool = False,
) -> None:
    previous_authorization = (
        binding.bind_status,
        binding.listen_status,
        bool(binding.allow_listening),
        binding.lead_id,
        binding.sales_id,
        binding.remark_code,
    )
    binding.bind_status = status
    binding.listen_status = listen_status
    binding.allow_listening = allow_listening
    binding.error_code = error_code
    if not preserve_lead:
        binding.lead_id = lead.id if lead else None
        binding.sales_id = lead.sales_id if lead else None
    binding.remark_code = remark_code
    current_authorization = (
        binding.bind_status,
        binding.listen_status,
        bool(binding.allow_listening),
        binding.lead_id,
        binding.sales_id,
        binding.remark_code,
    )
    if current_authorization != previous_authorization:
        binding.authorization_revision = int(binding.authorization_revision or 1) + 1


def _ambiguous_scan_remark_codes(sessions: list[WechatSessionScanItem]) -> set[str]:
    session_keys_by_code: dict[str, set[str]] = {}
    for item in sessions:
        for candidate in _clean_candidates(item.remark_code_candidates):
            session_keys_by_code.setdefault(candidate, set()).add(item.rpa_session_key)
    return {code for code, session_keys in session_keys_by_code.items() if len(session_keys) > 1}


def _bind_one_session(
    db: Session,
    worker: Worker,
    payload: WechatSessionScanResultRequest,
    item: WechatSessionScanItem,
    *,
    ambiguous_remark_codes: set[str] | None = None,
) -> dict:
    candidates = _clean_candidates(item.remark_code_candidates)
    ambiguous_candidates = [code for code in candidates if code in (ambiguous_remark_codes or set())]
    if ambiguous_candidates:
        # Resolve by the physical session row only. Reusing the canonical remark
        # binding here would let two different chats overwrite the same record.
        binding = _upsert_binding_base(db, worker, payload, item)
        _set_binding_state(
            binding,
            status=BIND_STATUS_NEEDS_REVIEW,
            listen_status=LISTEN_STATUS_NOT_STARTED,
            allow_listening=False,
            error_code="SESSION_REMARK_CODE_MULTIPLE_SESSIONS",
            remark_code=",".join(candidates),
            preserve_lead=True,
        )
        return {**_binding_to_dict(binding), "can_ingest_messages": False}

    remark_code_anchor = candidates[0] if len(candidates) == 1 else None
    binding = _upsert_binding_base(db, worker, payload, item, remark_code=remark_code_anchor)
    if remark_code_anchor:
        _retire_duplicate_effective_remark_bindings(db, canonical=binding, remark_code=remark_code_anchor)
    if binding.bind_status == BIND_STATUS_DISABLED:
        return {
            **_binding_to_dict(binding),
            "bind_status": BIND_STATUS_DISABLED,
            "can_ingest_messages": False,
            "error_code": "SESSION_BINDING_DISABLED",
        }

    if not candidates:
        _set_binding_state(
            binding,
            status=BIND_STATUS_UNBOUND,
            listen_status=LISTEN_STATUS_NOT_STARTED,
            allow_listening=False,
            error_code="SESSION_REMARK_CODE_NOT_FOUND",
        )
        return {**_binding_to_dict(binding), "can_ingest_messages": False}

    was_bound_to_lead_id = binding.lead_id if binding.bind_status == BIND_STATUS_BOUND else None
    if len(candidates) > 1:
        _set_binding_state(
            binding,
            status=BIND_STATUS_NEEDS_REVIEW,
            listen_status=LISTEN_STATUS_NOT_STARTED,
            allow_listening=False,
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
            WechatSessionBinding.remark_code != remark_code,
        )
    )
    if other:
        _set_binding_state(
            binding,
            status=BIND_STATUS_NEEDS_REVIEW,
            listen_status=LISTEN_STATUS_NOT_STARTED,
            allow_listening=False,
            error_code="SESSION_BINDING_CONFLICT",
            remark_code=remark_code,
        )
        return {**_binding_to_dict(binding), "can_ingest_messages": False}

    already_bound = was_bound_to_lead_id == lead.id
    binding.worker_id = worker.id
    _set_binding_state(
        binding,
        status=BIND_STATUS_BOUND,
        listen_status=LISTEN_STATUS_LISTENING,
        allow_listening=True,
        lead=lead,
        remark_code=remark_code,
    )
    _upsert_conversation_for_binding(db, binding)
    result = _binding_to_dict(binding)
    result["bind_status"] = BIND_STATUS_ALREADY_BOUND if already_bound else BIND_STATUS_BOUND
    result["can_ingest_messages"] = True
    return result


def ingest_scan_result(db: Session, worker: Worker, payload: WechatSessionScanResultRequest) -> dict:
    existing = db.scalar(select(WechatScanRun).where(WechatScanRun.scan_id == payload.scan_id))
    if existing:
        return existing.response_snapshot or {}

    if payload.scan_failed:
        response = {
            "accepted_count": 0,
            "bound_count": 0,
            "needs_review_count": 0,
            "bindings": [],
            "next_action": NEXT_ACTION_NONE,
            "error_code": payload.error_code or "SESSION_SCAN_FAILED",
        }
        db.add(WechatScanRun(worker_id=worker.id, scan_id=payload.scan_id, status="failed", response_snapshot=jsonable_encoder(response)))
        db.flush()
        return response

    ambiguous_remark_codes = _ambiguous_scan_remark_codes(payload.sessions)
    bindings = [
        _bind_one_session(
            db,
            worker,
            payload,
            item,
            ambiguous_remark_codes=ambiguous_remark_codes,
        )
        for item in payload.sessions
    ]
    db.flush()
    response = {
        "accepted_count": len(payload.sessions),
        "bound_count": sum(1 for item in bindings if item["bind_status"] in {BIND_STATUS_BOUND, BIND_STATUS_ALREADY_BOUND}),
        "needs_review_count": sum(1 for item in bindings if item["bind_status"] == BIND_STATUS_NEEDS_REVIEW),
        "bindings": bindings,
        "next_action": NEXT_ACTION_NONE,
    }
    db.add(WechatScanRun(worker_id=worker.id, scan_id=payload.scan_id, status="processed", response_snapshot=jsonable_encoder(response)))
    db.flush()
    return response


def read_targets(db: Session, worker: Worker, limit: int = 20) -> dict:
    _degrade_invalid_bound_targets(db, worker)
    bindings = list(
        db.scalars(
            select(WechatSessionBinding)
            .where(
                WechatSessionBinding.worker_id == worker.id,
                WechatSessionBinding.bind_status == BIND_STATUS_BOUND,
                WechatSessionBinding.listen_status.in_([LISTEN_STATUS_LISTENING, LISTEN_STATUS_DEGRADED]),
                WechatSessionBinding.allow_listening.is_(True),
                WechatSessionBinding.remark_code.is_not(None),
                WechatSessionBinding.remark_code != "",
                WechatSessionBinding.conversation_id.is_not(None),
                WechatSessionBinding.conversation_id != "",
                WechatSessionBinding.deleted_at.is_(None),
            )
            .order_by(WechatSessionBinding.last_seen_at.desc())
        )
    )
    targets: list[dict] = []
    for item in bindings:
        conversation = _upsert_conversation_for_binding(db, item)
        if conversation.status in CONVERSATION_CLOSED_STATUSES:
            continue
        read_reason = _read_reason(item, conversation)
        if not read_reason:
            continue
        target = {
            "conversation_id": item.conversation_id,
            "lead_id": item.lead_id,
            "sales_id": item.sales_id,
            "remark_code": item.remark_code,
            "rpa_session_key": item.rpa_session_key,
            "display_name": item.display_name,
            "last_ingested_at": item.last_ingested_at,
            "read_reason": read_reason,
            "authorization_revision": _authorization_revision(item),
        }
        if _clean_locator(item.row_fingerprint):
            target["row_fingerprint"] = item.row_fingerprint
        if item.ocr_confidence is not None:
            target["ocr_confidence"] = item.ocr_confidence
        targets.append(target)
    targets.sort(key=lambda item: READ_REASON_PRIORITY.get(item["read_reason"], 99))
    return {
        "targets": targets[:limit],
        "poll_after_seconds": 10,
        "next_action": NEXT_ACTION_NONE,
    }


def _authorization_revision(binding: WechatSessionBinding) -> str:
    seed = f"{binding.id}|{int(binding.authorization_revision or 1)}"
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:32]


def _degrade_invalid_bound_targets(db: Session, worker: Worker) -> None:
    rows = list(
        db.scalars(
            select(WechatSessionBinding).where(
                WechatSessionBinding.worker_id == worker.id,
                WechatSessionBinding.bind_status == BIND_STATUS_BOUND,
                WechatSessionBinding.allow_listening.is_(True),
                WechatSessionBinding.deleted_at.is_(None),
            )
        )
    )
    changed = False
    for item in rows:
        error_code = None
        if not _clean_locator(item.conversation_id):
            error_code = "C2_TARGET_CONVERSATION_ID_MISSING"
        elif not _clean_locator(item.remark_code):
            error_code = "C2_TARGET_REMARK_CODE_MISSING"
        if error_code:
            _set_binding_state(
                item,
                status=BIND_STATUS_NEEDS_REVIEW,
                listen_status=LISTEN_STATUS_DEGRADED,
                allow_listening=False,
                error_code=error_code,
                remark_code=item.remark_code,
                preserve_lead=True,
            )
            changed = True
    if changed:
        db.flush()


def _read_reason(binding: WechatSessionBinding, conversation: Conversation) -> str | None:
    if conversation.status == "recall_precheck":
        return "recall_precheck"
    if conversation.status == "waiting_user_reply" and conversation.last_ai_reply_at:
        return "recent_ai_sent"
    if conversation.status in {"waiting_user_reply", "recalled_waiting_user", "sales_replied_waiting_user"}:
        return "waiting_user_reply"
    if conversation.status == "waiting_sales_reply":
        return "waiting_sales_reply"
    return None


def _looks_like_voice_payload_text(value: str) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    if text.startswith("{'") or text.startswith('{"') or text.startswith("[{"):
        return True
    sample = text[:4000]
    payload_tokens = (
        "voice_transcribe_completed",
        "voice_transcribe_review",
        "before_screenshot_path",
        "after_screenshot_path",
        "transcribed_messages",
        "context_menu_attempt",
    )
    return len(text) > 1000 and any(token in sample for token in payload_tokens)


def _voice_failure_code(raw_payload: dict | None) -> str | None:
    if not isinstance(raw_payload, dict):
        return None
    for key in (
        "voice_error_code",
        "transcribe_error_code",
        "voice_transcription_error_code",
        "error_code",
        "read_result",
        "read_status",
        "result",
        "status",
    ):
        code = str(raw_payload.get(key) or "").strip().upper()
        if code in VOICE_FAILURE_ERROR_CODES:
            return code
    return None


def _image_recognition_warning_code(raw_payload: dict | None) -> str | None:
    if not isinstance(raw_payload, dict) or "image_recognition" not in raw_payload:
        return "IMAGE_RECOGNITION_RESULT_INVALID"
    recognition = raw_payload.get("image_recognition")
    if not isinstance(recognition, dict):
        return "IMAGE_RECOGNITION_RESULT_INVALID"
    status = str(recognition.get("status") or "").strip().lower()
    if status not in IMAGE_RECOGNITION_STATUSES:
        return "IMAGE_RECOGNITION_RESULT_INVALID"
    success = recognition.get("success")
    if "success" in recognition and not isinstance(success, bool):
        return "IMAGE_RECOGNITION_RESULT_INVALID"
    supplied_code = str(recognition.get("error_code") or "").strip().upper()
    if status == "succeeded":
        if success is False or supplied_code:
            return "IMAGE_RECOGNITION_RESULT_INVALID"
        return None
    if success is True:
        return "IMAGE_RECOGNITION_RESULT_INVALID"
    normalized_code = re.sub(r"[^A-Z0-9_]+", "_", supplied_code).strip("_")
    return normalized_code[:64] or "IMAGE_RECOGNITION_FAILED"


def _read_failure_result(*payloads: dict | None) -> str | None:
    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        for key in ("read_result", "read_status", "result", "status", "error_code"):
            value = str(payload.get(key) or "").strip().lower()
            if value in READ_TARGET_FAILURE_RESULTS:
                return value
    return None


def _upsert_conversation_for_binding(db: Session, binding: WechatSessionBinding) -> Conversation:
    conversation = db.get(Conversation, binding.conversation_id)
    if not conversation:
        conversation = Conversation(conversation_id=binding.conversation_id)
        db.add(conversation)
    conversation.lead_id = binding.lead_id
    conversation.sales_id = binding.sales_id
    conversation.worker_id = binding.worker_id
    return conversation


def _normalized_contract_text(value: object) -> str:
    return " ".join(str(value or "").split())


def _validate_v3_observation(observation: object, *, require_ingestible: bool | None = None) -> tuple[str, dict]:
    if not isinstance(observation, dict):
        raise AppError("MESSAGE_OBSERVATION_MISSING", "V3 消息缺少 OmniAuto observation", 409)
    if int(observation.get("schema_version") or 0) != OBSERVATION_SCHEMA_VERSION_V3:
        raise AppError("MESSAGE_OBSERVATION_SCHEMA_VERSION_MISMATCH", "V3 observation schema 版本不一致", 409)
    if observation.get("contract_errors"):
        raise AppError("MESSAGE_OBSERVATION_CONTRACT_INVALID", "OmniAuto observation 未通过统一合同", 409)

    observation_id = str(observation.get("observation_id") or "").strip()
    if not observation_id:
        raise AppError("MESSAGE_OBSERVATION_ID_MISSING", "V3 observation 缺少唯一标识", 409)
    row_kind = str(observation.get("row_kind") or "").strip().lower()
    rule = ROW_RULES_V3.get(row_kind)
    if not isinstance(rule, dict):
        raise AppError("MESSAGE_ROW_KIND_INVALID", "V3 消息 row_kind 不合法", 409)
    if require_ingestible is True and not bool(rule.get("ingestible")):
        raise AppError("MESSAGE_ROW_KIND_NOT_INGESTIBLE", "V3 非可入库行被组装成最终消息", 409)
    for field in rule.get("required_fields") or []:
        value = observation.get(str(field))
        if value is None or (isinstance(value, str) and not value.strip()):
            raise AppError("MESSAGE_OBSERVATION_REQUIRED_FIELD_MISSING", f"V3 observation 缺少字段: {field}", 409)

    observed_type = str(observation.get("message_type") or "").strip().lower()
    if observed_type != str(rule.get("message_type") or ""):
        raise AppError("MESSAGE_ROW_TYPE_MISMATCH", "OmniAuto observation 与合同的消息类型不一致", 409)
    observed_role = str(observation.get("sender_role") or "").strip().lower()
    if observed_role not in {str(value) for value in rule.get("allowed_sender_roles") or []}:
        raise AppError("MESSAGE_ROW_SENDER_ROLE_MISMATCH", "OmniAuto observation 与合同的发送方角色不一致", 409)
    role_source = str(observation.get("sender_role_source") or "").strip().lower()
    if role_source not in {str(value) for value in rule.get("allowed_sender_role_sources") or []}:
        raise AppError("MESSAGE_ROW_ROLE_SOURCE_UNTRUSTED", "V3 消息角色不是由合同允许的唯一证据生成", 409)
    voice_state = str(observation.get("voice_state") or "").strip().lower()
    if voice_state not in {str(value) for value in rule.get("allowed_voice_states") or []}:
        raise AppError("MESSAGE_ROW_VOICE_STATE_INVALID", "V3 消息语音状态不合法", 409)
    return observation_id, rule


def _validate_v3_request_contract(payload: WechatMessageIngestRequest) -> None:
    if str(payload.contract_revision or "").strip() != CONTRACT_REVISION_V3:
        raise AppError("MESSAGE_CONTRACT_REVISION_MISMATCH", "V3 消息合同修订号不一致", 409)
    if str(payload.contract_sha256 or "").strip().lower() != CONTRACT_SHA256_V3:
        raise AppError("MESSAGE_CONTRACT_SHA256_MISMATCH", "V3 消息合同指纹不一致", 409)
    if int(payload.observation_schema_version or 0) != OBSERVATION_SCHEMA_VERSION_V3:
        raise AppError("MESSAGE_OBSERVATION_SCHEMA_VERSION_MISMATCH", "V3 observation schema 版本不一致", 409)

    evidence = payload.evidence
    if not isinstance(evidence, dict):
        raise AppError("MESSAGE_BATCH_EVIDENCE_MISSING", "V3 批次缺少完整 OmniAuto 证据", 409)
    if str(evidence.get("contract_revision") or "").strip() != CONTRACT_REVISION_V3:
        raise AppError("MESSAGE_EVIDENCE_CONTRACT_REVISION_MISMATCH", "V3 批次证据合同修订号不一致", 409)
    if str(evidence.get("contract_sha256") or "").strip().lower() != CONTRACT_SHA256_V3:
        raise AppError("MESSAGE_EVIDENCE_CONTRACT_SHA256_MISMATCH", "V3 批次证据合同指纹不一致", 409)
    if int(evidence.get("observation_schema_version") or 0) != OBSERVATION_SCHEMA_VERSION_V3:
        raise AppError("MESSAGE_EVIDENCE_OBSERVATION_SCHEMA_MISMATCH", "V3 批次证据 schema 版本不一致", 409)
    observations = evidence.get("observations")
    if not isinstance(observations, list):
        raise AppError("MESSAGE_BATCH_OBSERVATIONS_MISSING", "V3 批次缺少完整 observation 清单", 409)
    evidence_observations: dict[str, dict] = {}
    ingestible_observation_ids: set[str] = set()
    for observation in observations:
        observation_id, rule = _validate_v3_observation(observation)
        if observation_id in evidence_observations:
            raise AppError("MESSAGE_OBSERVATION_ID_CONFLICT", "V3 批次存在重复 observation 标识", 409)
        evidence_observations[observation_id] = observation
        if bool(rule.get("ingestible")):
            ingestible_observation_ids.add(observation_id)

    seen_source_keys: set[str] = set()
    mapped_observation_ids: set[str] = set()
    for item in payload.messages:
        source_key = str(item.source_message_key or "").strip()
        if not source_key:
            raise AppError("MESSAGE_SOURCE_IDENTITY_MISSING", "V3 消息缺少 source_message_key", 409)
        if source_key in seen_source_keys:
            raise AppError("MESSAGE_SOURCE_CONFLICT", "同一来源消息出现多个最终解释", 409)
        seen_source_keys.add(source_key)

        if item.message_position is None:
            raise AppError("MESSAGE_POSITION_MISSING", "V3 消息缺少权威画面位置", 409)
        if item.message_position.frame_source not in {"initial_read", "final_read"}:
            raise AppError("MESSAGE_FRAME_SOURCE_INVALID", "V3 消息权威画面来源不合法", 409)

        raw_payload = item.raw_payload
        if not isinstance(raw_payload, dict):
            raise AppError("MESSAGE_RAW_PAYLOAD_MISSING", "V3 消息缺少原始识别证据", 409)
        if int(raw_payload.get("contract_version") or 0) != 3:
            raise AppError("MESSAGE_RAW_CONTRACT_VERSION_MISMATCH", "V3 原始证据合同版本不一致", 409)
        if str(raw_payload.get("contract_revision") or "").strip() != CONTRACT_REVISION_V3:
            raise AppError("MESSAGE_RAW_CONTRACT_REVISION_MISMATCH", "V3 原始证据合同修订号不一致", 409)
        if str(raw_payload.get("contract_sha256") or "").strip().lower() != CONTRACT_SHA256_V3:
            raise AppError("MESSAGE_RAW_CONTRACT_SHA256_MISMATCH", "V3 原始证据合同指纹不一致", 409)
        if int(raw_payload.get("observation_schema_version") or 0) != OBSERVATION_SCHEMA_VERSION_V3:
            raise AppError("MESSAGE_RAW_OBSERVATION_SCHEMA_MISMATCH", "V3 原始证据 schema 版本不一致", 409)
        if str(raw_payload.get("source_message_key") or "").strip() != source_key:
            raise AppError("MESSAGE_RAW_SOURCE_IDENTITY_MISMATCH", "V3 原始证据与最终消息身份不一致", 409)

        observation = raw_payload.get("observation")
        observation_id, rule = _validate_v3_observation(observation, require_ingestible=True)
        if observation_id in mapped_observation_ids:
            raise AppError("MESSAGE_OBSERVATION_MAPPING_CONFLICT", "同一 observation 被组装成多条最终消息", 409)
        if evidence_observations.get(observation_id) != observation:
            raise AppError("MESSAGE_OBSERVATION_EVIDENCE_MISMATCH", "最终消息 observation 与批次原始证据不一致", 409)
        mapped_observation_ids.add(observation_id)

        observed_type = str(observation.get("message_type") or "").strip().lower()
        canonical_type = str(item.message_type or "").strip().lower()
        if observed_type != str(rule.get("message_type") or "") or canonical_type != observed_type:
            raise AppError("MESSAGE_ROW_TYPE_MISMATCH", "OmniAuto、Worker 与合同的消息类型不一致", 409)

        observed_role = str(observation.get("sender_role") or "").strip().lower()
        canonical_role = str(item.sender_role_hint or "").strip().lower()
        allowed_roles = {str(value) for value in rule.get("allowed_sender_roles") or []}
        if observed_role not in allowed_roles or canonical_role != observed_role:
            raise AppError("MESSAGE_ROW_SENDER_ROLE_MISMATCH", "OmniAuto、Worker 与合同的发送方角色不一致", 409)

        role_source = str(observation.get("sender_role_source") or "").strip().lower()
        allowed_sources = {str(value) for value in rule.get("allowed_sender_role_sources") or []}
        if role_source not in allowed_sources:
            raise AppError("MESSAGE_ROW_ROLE_SOURCE_UNTRUSTED", "V3 消息角色不是由合同允许的唯一证据生成", 409)

        voice_state = str(observation.get("voice_state") or "").strip().lower()
        allowed_voice_states = {str(value) for value in rule.get("allowed_voice_states") or []}
        if voice_state not in allowed_voice_states:
            raise AppError("MESSAGE_ROW_VOICE_STATE_INVALID", "V3 消息语音状态不合法", 409)

        if canonical_type in {"text", "voice", "system"}:
            observed_content = _normalized_contract_text(observation.get("content_clean"))
            canonical_content = _normalized_contract_text(item.content)
            if not observed_content or canonical_content != observed_content:
                raise AppError("MESSAGE_ROW_CONTENT_MISMATCH", "OmniAuto observation 与 Worker 正文不一致", 409)

    if mapped_observation_ids != ingestible_observation_ids:
        missing = sorted(ingestible_observation_ids - mapped_observation_ids)
        unexpected = sorted(mapped_observation_ids - ingestible_observation_ids)
        raise AppError(
            "MESSAGE_OBSERVATION_MAPPING_INCOMPLETE",
            f"V3 observation 与最终消息不是一一对应: missing={missing}, unexpected={unexpected}",
            409,
        )


def ingest_messages(db: Session, worker: Worker, payload: WechatMessageIngestRequest) -> dict:
    binding = db.scalar(
        select(WechatSessionBinding).where(
            WechatSessionBinding.conversation_id == payload.conversation_id,
            WechatSessionBinding.worker_id == worker.id,
            WechatSessionBinding.deleted_at.is_(None),
        )
    )
    if not binding or binding.bind_status != BIND_STATUS_BOUND or not binding.allow_listening or not _clean_locator(binding.remark_code):
        raise AppError("MESSAGE_CONVERSATION_NOT_BOUND", "会话未绑定，不能入库消息", 409)
    if payload.contract_version != 3:
        raise AppError("MESSAGE_CONTRACT_V3_REQUIRED", "C2 消息入库只接受统一 V3 合同", 409)
    observed_remark_code = _clean_locator(payload.remark_code)
    if not observed_remark_code:
        raise AppError("MESSAGE_TARGET_IDENTITY_MISSING", "V3 消息缺少读取目标短码", 409)
    if observed_remark_code and observed_remark_code != _clean_locator(binding.remark_code):
        raise AppError("MESSAGE_TARGET_IDENTITY_MISMATCH", "读取目标与绑定会话不一致，已拒绝入库", 409)
    if str(payload.authorization_revision or "") != _authorization_revision(binding):
        raise AppError("MESSAGE_AUTHORIZATION_REVISION_EXPIRED", "读取授权已过期，已拒绝旧任务入库", 409)
    _validate_v3_request_contract(payload)
    for item in payload.messages:
        if str(item.sender_role_hint or "").strip().lower() not in SENDER_ROLES_V3:
            raise AppError("MESSAGE_SENDER_ROLE_INVALID", "V3 消息发送方角色不合法", 409)
        if str(item.message_type or "").strip().lower() not in MESSAGE_TYPES_V3:
            raise AppError("MESSAGE_TYPE_INVALID", "V3 消息类型不合法", 409)
        if str(item.item_state or "").strip().lower() != "completed":
            raise AppError("MESSAGE_ITEM_STATE_INVALID", "只有已完成的消息项可以入库", 409)
        if str(item.flow_state or "").strip().lower() not in FLOW_STATES_V3:
            raise AppError("MESSAGE_FLOW_STATE_INVALID", "V3 消息流程状态不合法", 409)
    conversation = _upsert_conversation_for_binding(db, binding)
    conversation_allowed = conversation.status not in CONVERSATION_CLOSED_STATUSES
    observed_rpa_session_key = payload.rpa_session_key or ""

    ingested_count = 0
    duplicated_count = 0
    ignored_count = 0
    results: list[dict] = []
    for item in payload.messages:
        message_type = str(item.message_type or "").strip().lower()
        source_dedupe_key = item.dedupe_key.strip() if item.dedupe_key else ""
        raw_payload = dict(item.raw_payload or {})
        if item.message_position:
            raw_payload["message_position"] = item.message_position.model_dump(exclude_none=True)
        content = item.content
        image_warning_codes: list[str] = []
        stored_image_local_path = item.image_local_path
        if message_type == "image":
            image_warning_code = _image_recognition_warning_code(raw_payload)
            if image_warning_code:
                image_warning_codes.append(image_warning_code)
            if str(item.image_local_path or "").strip():
                stored_image_local_path = None
                image_warning_codes.append("IMAGE_LOCAL_PATH_IGNORED")
        read_failure = _read_failure_result(item.raw_payload, payload.evidence)
        if read_failure:
            raise AppError(read_failure.upper(), "V3 读取失败证据不能伪装成可入库消息", 409)
        sender_role = str(item.sender_role_hint or "").strip().lower()
        if message_type == "voice":
            voice_error_code = _voice_failure_code(raw_payload)
            transcript = str(item.content or "").strip()
            if not voice_error_code and not transcript:
                voice_error_code = "VOICE_TRANSCRIBE_EMPTY"
            if VOICE_DURATION_RE.match(transcript) or _looks_like_voice_payload_text(transcript):
                voice_error_code = "VOICE_TRANSCRIBE_INVALID_CONTENT"
            if voice_error_code:
                raise AppError(voice_error_code, "V3 未完成或无效的语音不能入库", 409)
            content = transcript
            dedupe_key = source_dedupe_key
        else:
            if not source_dedupe_key:
                raise AppError("MESSAGE_DEDUPE_KEY_MISSING", "消息缺少 dedupe_key", 400)
            dedupe_key = source_dedupe_key
        if not conversation_allowed:
            raise AppError("CONVERSATION_STATUS_NOT_LISTENABLE", "会话状态不允许入库", 409)
        if sender_role == "unknown":
            raise AppError("MESSAGE_SENDER_ROLE_UNCLEAR", "V3 消息发送方不明确", 409)
        exists = db.scalar(
            select(MessageEvent).where(
                MessageEvent.conversation_id == payload.conversation_id,
                MessageEvent.dedupe_key == dedupe_key,
            )
        )
        if exists:
            duplicated_count += 1
            results.append({"dedupe_key": dedupe_key, "ingest_result": "duplicated", "error_code": "MESSAGE_INGEST_DUPLICATED"})
            continue
        source_exists = db.scalar(
            select(MessageEvent).where(
                MessageEvent.conversation_id == payload.conversation_id,
                MessageEvent.read_run_id == payload.read_run_id,
                MessageEvent.source_message_key == item.source_message_key,
            )
        )
        if source_exists:
            raise AppError("MESSAGE_SOURCE_CONFLICT", "同一读取批次的来源消息已存在", 409)

        message = MessageEvent(
            conversation_id=payload.conversation_id,
            binding_id=binding.id,
            lead_id=binding.lead_id,
            sales_id=binding.sales_id,
            worker_id=worker.id,
            rpa_session_key=observed_rpa_session_key,
            read_run_id=payload.read_run_id,
            contract_version=payload.contract_version,
            source_message_key=item.source_message_key,
            dedupe_key=dedupe_key,
            sender_role=sender_role,
            message_type=message_type,
            content=content,
            image_local_path=stored_image_local_path,
            raw_payload=raw_payload,
            evidence=payload.evidence or {},
            ocr_confidence=item.ocr_confidence,
            item_state=item.item_state,
            flow_state=item.flow_state,
            occurred_at=item.occurred_at,
            ingested_at=utcnow(),
            error_code=image_warning_codes[0] if image_warning_codes else None,
        )
        try:
            with db.begin_nested():
                db.add(message)
                db.flush()
        except IntegrityError:
            duplicated_count += 1
            results.append({"dedupe_key": dedupe_key, "ingest_result": "duplicated", "error_code": "MESSAGE_INGEST_DUPLICATED"})
            continue
        binding.last_ingested_at = message.ingested_at
        event_time = message.occurred_at or message.ingested_at
        if message.sender_role == "customer":
            conversation.last_inbound_at = event_time
            from app.services.c3_service import supersede_open_reply_actions_for_new_inbound

            supersede_open_reply_actions_for_new_inbound(db, binding.conversation_id)
        elif message.sender_role in SALES_SIDE_SENDER_ROLES:
            conversation.last_outbound_at = event_time
            conversation.last_sales_reply_at = event_time
            conversation.sales_first_reply_at = conversation.sales_first_reply_at or event_time
            conversation.status = "sales_replied_waiting_user"
            conversation.ai_enabled = False
            from app.services.c3_service import cancel_open_reply_actions_for_conversation_change

            cancel_open_reply_actions_for_conversation_change(db, binding.conversation_id, reason="销售已人工回复，取消未发送 AI 回复")
        ingested_count += 1
        result = {"dedupe_key": dedupe_key, "ingest_result": "ingested", "message_id": message.id, "message_event_id": message.id}
        if image_warning_codes:
            trace_id = get_request_id()
            logger.warning(
                "image message ingested with warnings",
                extra={
                    "trace_id": trace_id,
                    "conversation_id": payload.conversation_id,
                    "read_run_id": payload.read_run_id,
                    "message_event_id": message.id,
                    "dedupe_key": dedupe_key,
                    "warning_codes": image_warning_codes,
                },
            )
            result["warning_code"] = image_warning_codes[0]
            result["warning_codes"] = image_warning_codes
            result["trace_id"] = trace_id
        if source_dedupe_key and source_dedupe_key != dedupe_key:
            result["source_dedupe_key"] = source_dedupe_key
        results.append(result)

    db.flush()
    return {
        "ingested_count": ingested_count,
        "duplicated_count": duplicated_count,
        "ignored_count": ignored_count,
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
