from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
import re
import uuid
from zoneinfo import ZoneInfo

from fastapi.encoders import jsonable_encoder
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.request_context import ActorContext
from app.core.request_id import get_request_id
from app.contracts.c2 import (
    c2_contract_v3,
    contract_revision,
    contract_row_rules,
    contract_sha256,
    contract_values,
    validate_image_result_schema,
)
from app.errors import AppError
from app.models.base import utcnow
from app.models.c3 import Conversation, MessageBatch, ReplyAction
from app.models.lead import Lead
from app.models.sales import Sales
from app.models.task import Task
from app.models.wechat import MessageEvent, WechatScanRun, WechatSessionBinding
from app.models.worker import Worker
from app.schemas.wechat import (
    WechatFriendActivationConfirmRequest,
    WechatMessageIngestRequest,
    WechatSessionScanItem,
    WechatSessionScanResultRequest,
)


def _latest_datetime(*values: datetime | None) -> datetime:
    present = [value for value in values if value is not None]
    if not present:
        return utcnow()

    def comparable(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    return max(present, key=comparable)
from app.services.message_contract import canonical_reply_text, reply_text_hash


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
LISTEN_STATUS_PAUSED = "paused"
LISTEN_STATUS_DEGRADED = "degraded"
LISTEN_STATUS_ERROR = "error"
LISTEN_STATUS_DISABLED = "disabled"

NEXT_ACTION_NONE = "none"
LOW_CONFIDENCE_THRESHOLD = 0.7
CONVERSATION_CLOSED_STATUSES = {"closed", "rejected"}
PERMANENT_BINDING_DISABLE_REASONS = {
    "customer_hard_opt_out",
    "conversation_closed",
    "remark_code_removed_confirmed",
    "admin_disabled",
    "replaced_binding",
}
READ_NO_CHANGE_BACKOFF_SECONDS = (120, 300, 600)
IDENTITY_CHECKPOINT_RECENT_LIMIT = 200
SALES_SIDE_SENDER_ROLES = {"self", "sales", "sales_candidate"}
MESSAGE_TYPES_V3 = contract_values("message_types")
SENDER_ROLES_V3 = contract_values("sender_roles")
FLOW_STATES_V3 = contract_values("flow_states")
ROW_RULES_V3 = contract_row_rules()
FAILED_INGESTIBLE_MESSAGE_TYPES_V3 = {
    str(rule.get("message_type") or "")
    for rule in ROW_RULES_V3.values()
    if bool(rule.get("failed_ingestible"))
}
FLOW_GATE_STRONG_POSITION_SOURCES_V3 = {
    str(value)
    for value in (
        c2_contract_v3()
        .get("flow_gate_detail_schema", {})
        .get("strong_position_sources", [])
    )
}
TEMPORARY_CAPABILITY_GATE_CODES_V3 = contract_values(
    "temporary_capability_gate_codes"
)
RETIRED_FLOW_GATE_CODES_V3 = contract_values(
    "retired_flow_gate_codes"
)
CONTRACT_REVISION_V3 = contract_revision()
CONTRACT_SHA256_V3 = contract_sha256()
OBSERVATION_SCHEMA_VERSION_V3 = int(c2_contract_v3()["observation_schema_version"])
IMAGE_PERSISTENCE_POLICY = dict(c2_contract_v3().get("image_persistence_policy") or {})
VOICE_FAILURE_ERROR_CODES = {
    "VOICE_TRANSCRIBE_FAILED",
    "VOICE_TRANSCRIBE_CLICK_FAILED",
    "VOICE_TRANSCRIBE_LOCK_TIMEOUT",
    "VOICE_TRANSCRIBE_EMPTY",
    "VOICE_MESSAGE_UNCONFIRMED",
    "TARGET_NOT_CONFIRMED_FOR_VOICE_TRANSCRIBE",
}
IMAGE_UNDERSTANDING_FIELDS = set(IMAGE_PERSISTENCE_POLICY.get("customer_image_understanding_allowed_fields") or [])
IMAGE_UNDERSTANDING_AUDIT_FIELDS = set(
    IMAGE_PERSISTENCE_POLICY.get("customer_image_understanding_audit_allowed_fields") or []
)
VISUAL_BRIDGE_FIELDS = set(IMAGE_PERSISTENCE_POLICY.get("visual_bridge_input_allowed_fields") or [])
IMAGE_FORBIDDEN_FIELD_NAMES = set(IMAGE_PERSISTENCE_POLICY.get("forbidden_field_names") or [])
IMAGE_FORBIDDEN_FIELD_PREFIXES = (
    "provider_response",
    "raw_provider_response",
    "retry_response",
    "initial_response",
)
AI_REPLY_RECEIPT_CLOCK_SKEW = timedelta(minutes=5)
READ_TARGET_FAILURE_RESULTS = {"target_not_confirmed", "search_not_found", "search_ambiguous"}
READ_REASON_PRIORITY = {
    "recall_precheck": 0,
    "friend_acceptance_visible_hit": 0,
    "visible_unread": 1,
    "recent_ai_sent": 2,
    "waiting_user_reply": 3,
    "waiting_sales_reply": 4,
}
logger = logging.getLogger(__name__)
VOICE_DURATION_RE = re.compile(
    r"^\s*(?:\[?语音\]?\s*)?\d{1,3}(?:\.\d+)?\s*(?:\"|”|″|秒|s|S)\s*$"
)


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
        "authorization_revision": int(binding.authorization_revision or 1),
        "error_code": binding.error_code,
        "disable_reason": binding.disable_reason,
        "disabled_at": binding.disabled_at,
        "disabled_by": binding.disabled_by,
        "replacement_binding_id": binding.replacement_binding_id,
        "unread_hint": binding.unread_hint,
        "last_message_preview": binding.last_message_preview,
        "ocr_confidence": binding.ocr_confidence,
        "first_seen_at": binding.first_seen_at,
        "last_seen_at": binding.last_seen_at,
        "last_ingested_at": binding.last_ingested_at,
        "last_read_dispatched_at": binding.last_read_dispatched_at,
        "last_read_completed_at": binding.last_read_completed_at,
        "last_read_result": binding.last_read_result,
        "last_read_run_id": binding.last_read_run_id,
        "no_change_read_count": binding.no_change_read_count,
        "next_read_due_at": binding.next_read_due_at,
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
        "raw_payload": message.raw_payload,
        "evidence": message.evidence,
        "ocr_confidence": message.ocr_confidence,
        "occurred_at": message.occurred_at,
        "observed_at": message.observed_at,
        "observation_order": message.observation_order,
        "ingested_at": message.ingested_at,
        "error_code": message.error_code,
    }


def _normalized_content_hash(value: object) -> str:
    normalized = canonical_reply_text(value)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _nested_dict(value: object, *path: str) -> dict:
    current = value
    for key in path:
        if not isinstance(current, dict):
            return {}
        current = current.get(key)
    return current if isinstance(current, dict) else {}


def _first_stable_value(*values: object) -> object:
    for value in values:
        if isinstance(value, dict) and value:
            return value
        if isinstance(value, (str, int, float)) and str(value).strip():
            return value
    return ""


def _media_identity_hash(message_type: str, raw_payload: dict | None) -> str:
    raw = raw_payload if isinstance(raw_payload, dict) else {}
    observation = _nested_dict(raw, "observation")
    source = _nested_dict(raw, "observation", "source_message")
    voice_meta = _nested_dict(raw, "voice_transcription_meta")
    normalized_type = str(message_type or "").strip().lower()
    identity: object = ""
    if normalized_type == "voice":
        identity = _first_stable_value(
            raw.get("voice_anchor_stable_key"),
            voice_meta.get("voice_anchor_stable_key"),
            observation.get("parent_voice_anchor_key"),
            observation.get("voice_anchor_key"),
            source.get("voice_anchor_stable_key"),
            source.get("voice_anchor_key"),
        )
    elif normalized_type == "image":
        identity = _first_stable_value(
            raw.get("canonical_visual_id"),
            raw.get("canonical_input_id"),
            observation.get("canonical_visual_id"),
            source.get("canonical_visual_id"),
            observation.get("image_physical_anchor"),
            source.get("image_physical_anchor"),
        )
    elif normalized_type == "file":
        identity = _first_stable_value(
            raw.get("file_stable_id"),
            raw.get("media_stable_id"),
            source.get("file_stable_id"),
            source.get("media_stable_id"),
        )
    if identity == "":
        return ""
    encoded = json.dumps(
        identity,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _message_identity_summary(
    *,
    sender_role: str,
    message_type: str,
    content: object,
    raw_payload: dict | None,
) -> dict[str, str]:
    normalized_role = str(sender_role or "").strip().lower()
    normalized_type = str(message_type or "").strip().lower()
    content_hash = _normalized_content_hash(content)
    media_hash = _media_identity_hash(normalized_type, raw_payload)
    alignment_payload = {
        "sender_role": normalized_role,
        "message_type": normalized_type,
        "normalized_content_hash": content_hash,
        "media_identity_hash": media_hash,
    }
    alignment_signature = hashlib.sha256(
        json.dumps(
            alignment_payload,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return {**alignment_payload, "alignment_signature": alignment_signature}


def _worker_stable_id_from_raw(raw_payload: object) -> str:
    raw = raw_payload if isinstance(raw_payload, dict) else {}
    basis = raw.get("dedupe_basis") if isinstance(raw.get("dedupe_basis"), dict) else {}
    candidates = (
        basis.get("worker_stable_id"),
        raw.get("worker_stable_id"),
        _nested_dict(raw, "ai_reply_receipt").get("worker_stable_id"),
    )
    for value in candidates:
        stable_id = str(value or "").strip()
        if stable_id:
            return stable_id
    return ""


def _worker_stable_id(message: MessageEvent) -> str:
    return _worker_stable_id_from_raw(message.raw_payload)


def _checkpoint_alignment_signature(message: MessageEvent) -> str:
    raw = message.raw_payload if isinstance(message.raw_payload, dict) else {}
    observation = raw.get("observation")
    if not isinstance(observation, dict):
        return _message_identity_summary(
            sender_role=message.sender_role,
            message_type=message.message_type,
            content=message.content,
            raw_payload=raw,
        )["alignment_signature"]
    row_kind = str(observation.get("row_kind") or "").strip().lower()
    sender_role = str(
        observation.get("sender_role") or message.sender_role or ""
    ).strip().lower()
    message_type = str(
        observation.get("message_type") or message.message_type or ""
    ).strip().lower()
    if row_kind == "image_bubble":
        source = observation.get("source_message")
        source = source if isinstance(source, dict) else {}
        anchor = observation.get("image_physical_anchor")
        if not isinstance(anchor, dict):
            anchor = source.get("image_physical_anchor")
        anchor = anchor if isinstance(anchor, dict) else {}
        basis = {
            "row_kind": row_kind,
            "sender_role": sender_role,
            "message_type": message_type,
            "bubble_visual_fingerprint": anchor.get(
                "bubble_visual_fingerprint"
            ),
        }
    else:
        content_hash = hashlib.sha256(
            re.sub(
                r"\s+",
                " ",
                str(
                    observation.get("content_clean")
                    or message.content
                    or ""
                ).strip(),
            ).encode("utf-8")
        ).hexdigest()[:24]
        basis = {
            "row_kind": row_kind,
            "sender_role": sender_role,
            "message_type": message_type,
            "content_hash": content_hash,
        }
    encoded = json.dumps(
        basis,
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    ).encode("utf-8")
    return hashlib.sha1(encoded).hexdigest()[:40]


def _identity_checkpoint(
    db: Session,
    *,
    conversation_id: str,
) -> dict:
    events = list(
        db.scalars(
            select(MessageEvent)
            .where(MessageEvent.conversation_id == conversation_id)
            .order_by(MessageEvent.ingested_at.desc(), MessageEvent.id.desc())
            .limit(IDENTITY_CHECKPOINT_RECENT_LIMIT)
        )
    )
    max_sequence = 0
    historical_payloads = db.scalars(
        select(MessageEvent.raw_payload).where(
            MessageEvent.conversation_id == conversation_id
        )
    )
    for raw_payload in historical_payloads:
        match = re.fullmatch(
            r"worker-message-(\d+)",
            _worker_stable_id_from_raw(raw_payload),
        )
        if match:
            max_sequence = max(max_sequence, int(match.group(1)))
    recent_messages: list[dict[str, str]] = []
    for message in reversed(events):
        stable_id = _worker_stable_id(message)
        summary = _message_identity_summary(
            sender_role=message.sender_role,
            message_type=message.message_type,
            content=message.content,
            raw_payload=message.raw_payload,
        )
        recent_messages.append(
            {
                "stable_id": stable_id,
                "source_message_key": str(message.source_message_key or ""),
                "dedupe_key": message.dedupe_key,
                "sender_role": summary["sender_role"],
                "message_type": summary["message_type"],
                "normalized_content_hash": summary[
                    "normalized_content_hash"
                ],
                "media_identity_hash": summary["media_identity_hash"],
                "alignment_signature": _checkpoint_alignment_signature(
                    message
                ),
            }
        )
    return {
        "version": 1,
        "next_sequence_floor": max_sequence + 1,
        "recent_messages": recent_messages,
    }


def _raise_message_identity_collision(
    db: Session,
    *,
    existing: MessageEvent,
    incoming_sender_role: str,
    incoming_message_type: str,
    incoming_content: object,
    incoming_raw_payload: dict,
    source_message_key: str,
    dedupe_key: str,
) -> None:
    existing_identity = _message_identity_summary(
        sender_role=existing.sender_role,
        message_type=existing.message_type,
        content=existing.content,
        raw_payload=existing.raw_payload,
    )
    incoming_identity = _message_identity_summary(
        sender_role=incoming_sender_role,
        message_type=incoming_message_type,
        content=incoming_content,
        raw_payload=incoming_raw_payload,
    )
    if existing_identity == incoming_identity:
        return
    checkpoint = _identity_checkpoint(
        db,
        conversation_id=existing.conversation_id,
    )
    data = {
        "source_message_key": source_message_key,
        "dedupe_key": dedupe_key,
        "existing_identity": existing_identity,
        "incoming_identity": incoming_identity,
        "next_sequence_floor": checkpoint["next_sequence_floor"],
    }
    logger.warning(
        "C2 message identity collision",
        extra={
            "conversation_id": existing.conversation_id,
            "dedupe_key": dedupe_key,
            "identity_collision": data,
        },
    )
    raise AppError(
        "MESSAGE_IDENTITY_COLLISION",
        "消息去重键与已有消息身份不一致",
        409,
        data,
    )


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
    unread_became_visible = not bool(binding.unread_hint) and bool(item.unread_hint)
    binding.display_name = item.display_name
    binding.rpa_session_key = item.rpa_session_key
    binding.row_fingerprint = item.row_fingerprint or ""
    binding.unread_hint = item.unread_hint
    binding.last_message_preview = item.last_message_preview
    binding.ocr_confidence = item.ocr_confidence
    binding.last_seen_at = now
    binding.last_scan_snapshot = _scan_snapshot(payload, item)
    if unread_became_visible:
        _reset_read_backoff(binding)


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
    binding.disable_reason = "replaced_binding"
    binding.disabled_at = now
    binding.disabled_by = "system:remark_code_rebind"
    binding.replacement_binding_id = replacement_binding_id
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
        _reset_read_backoff(binding)


def _reset_read_backoff(binding: WechatSessionBinding) -> None:
    binding.no_change_read_count = 0
    binding.next_read_due_at = None


def _conversation_allows_binding_recovery(
    conversation: Conversation | None,
) -> bool:
    return bool(
        conversation
        and conversation.deleted_at is None
        and conversation.ai_enabled
        and conversation.status not in CONVERSATION_CLOSED_STATUSES
        and not str(conversation.close_reason or "").strip()
    )


def _legacy_disabled_pause_is_recoverable(
    db: Session,
    binding: WechatSessionBinding,
) -> bool:
    return bool(
        binding.bind_status == BIND_STATUS_DISABLED
        and binding.listen_status == LISTEN_STATUS_PAUSED
        and not str(binding.disable_reason or "").strip()
        and binding.disabled_at is None
        and not str(binding.disabled_by or "").strip()
        and not binding.replacement_binding_id
        and binding.deleted_at is None
        and _conversation_allows_binding_recovery(
            db.get(Conversation, binding.conversation_id)
        )
    )


def _normalize_legacy_disabled_pause(
    binding: WechatSessionBinding,
) -> None:
    _set_binding_state(
        binding,
        status=BIND_STATUS_BOUND,
        listen_status=LISTEN_STATUS_PAUSED,
        allow_listening=False,
        error_code="SESSION_BINDING_MIGRATED_TO_PAUSED",
        remark_code=binding.remark_code,
        preserve_lead=True,
    )
    binding.disable_reason = None
    binding.disabled_at = None
    binding.disabled_by = None


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
        if _legacy_disabled_pause_is_recoverable(db, binding):
            _normalize_legacy_disabled_pause(binding)
        else:
            return {
                **_binding_to_dict(binding),
                "bind_status": BIND_STATUS_DISABLED,
                "can_ingest_messages": False,
                "error_code": "SESSION_BINDING_DISABLED",
                "recovery_state": (
                    "retired"
                    if binding.deleted_at is not None
                    or binding.replacement_binding_id
                    else "permanently_disabled"
                ),
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
    binding.disable_reason = None
    binding.disabled_at = None
    binding.disabled_by = None
    binding.replacement_binding_id = None
    conversation = _upsert_conversation_for_binding(db, binding)
    if not already_bound:
        completed_add_friend = db.scalar(
            select(Task)
            .where(
                Task.lead_id == lead.id,
                Task.worker_id == worker.id,
                Task.task_type == "add_friend",
                Task.status == "completed",
                Task.result_code.in_(["invite_sent", "already_friend"]),
                Task.deleted_at.is_(None),
            )
            .order_by(Task.completed_at.desc())
        )
        if completed_add_friend:
            conversation.friend_state = "friend_active" if completed_add_friend.result_code == "already_friend" else "friend_request_sent"
            conversation.status = conversation.friend_state
    result = _binding_to_dict(binding)
    result["bind_status"] = BIND_STATUS_ALREADY_BOUND if already_bound else BIND_STATUS_BOUND
    result["can_ingest_messages"] = True
    result["recovery_state"] = "none"
    return result


def ingest_scan_result(db: Session, worker: Worker, payload: WechatSessionScanResultRequest) -> dict:
    existing = db.scalar(select(WechatScanRun).where(WechatScanRun.scan_id == payload.scan_id))
    if existing:
        if existing.worker_id != worker.id:
            raise AppError("SESSION_SCAN_ID_CONFLICT", "扫描批次不属于当前 Worker", 409)
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


def _sync_read_backoff_with_conversation(
    binding: WechatSessionBinding,
    conversation: Conversation,
) -> None:
    current_status = str(conversation.status or "")
    previous_status = str(binding.last_read_conversation_status or "")
    if previous_status and previous_status != current_status:
        _reset_read_backoff(binding)
    binding.last_read_conversation_status = current_status


def _read_is_due(
    binding: WechatSessionBinding,
    *,
    read_reason: str,
) -> bool:
    if read_reason in {
        "visible_unread",
        "friend_acceptance_visible_hit",
        "recall_precheck",
    }:
        return True
    due_at = binding.next_read_due_at
    if due_at is None:
        return True
    if due_at.tzinfo is None:
        due_at = due_at.replace(tzinfo=timezone.utc)
    return due_at.astimezone(timezone.utc) <= utcnow().astimezone(timezone.utc)


def _read_completion_payload(binding: WechatSessionBinding) -> dict:
    return {
        "result": binding.last_read_result,
        "completed_at": binding.last_read_completed_at,
        "no_change_read_count": int(binding.no_change_read_count or 0),
        "next_read_due_at": binding.next_read_due_at,
    }


def _settle_completed_read(
    binding: WechatSessionBinding,
    conversation: Conversation,
    *,
    read_run_id: str,
    has_new_facts: bool,
) -> dict:
    if (
        binding.last_read_run_id == read_run_id
        and binding.last_read_completed_at is not None
        and binding.last_read_result in {"new_facts", "no_change"}
    ):
        return _read_completion_payload(binding)
    now = utcnow()
    binding.last_read_run_id = read_run_id
    binding.last_read_completed_at = now
    binding.last_read_conversation_status = str(conversation.status or "")
    if has_new_facts:
        binding.last_read_result = "new_facts"
        _reset_read_backoff(binding)
    else:
        binding.last_read_result = "no_change"
        binding.no_change_read_count = int(binding.no_change_read_count or 0) + 1
        backoff_index = min(
            binding.no_change_read_count - 1,
            len(READ_NO_CHANGE_BACKOFF_SECONDS) - 1,
        )
        binding.next_read_due_at = now + timedelta(
            seconds=READ_NO_CHANGE_BACKOFF_SECONDS[backoff_index]
        )
    return _read_completion_payload(binding)


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
        from app.services.c3_service import enforce_open_handoff_gate

        enforce_open_handoff_gate(db, conversation)
        _prepare_due_recall(conversation)
        _sync_read_backoff_with_conversation(item, conversation)
        if conversation.status in CONVERSATION_CLOSED_STATUSES:
            continue
        read_reason = _read_reason(item, conversation)
        if not read_reason or not _read_is_due(item, read_reason=read_reason):
            continue
        target = _read_target_payload(db, item, read_reason=read_reason)
        target["_dispatch_binding"] = item
        targets.append(target)
    def dispatch_sort_key(target: dict) -> tuple[float, int, float]:
        dispatch_binding = target["_dispatch_binding"]
        dispatched_at = dispatch_binding.last_read_dispatched_at
        last_seen_at = dispatch_binding.last_seen_at
        dispatched_value = (
            _latest_datetime(dispatched_at).timestamp()
            if dispatched_at is not None
            else float("-inf")
        )
        last_seen_value = (
            _latest_datetime(last_seen_at).timestamp()
            if last_seen_at is not None
            else 0.0
        )
        return (
            dispatched_value,
            READ_REASON_PRIORITY.get(target["read_reason"], 99),
            -last_seen_value,
        )

    targets.sort(key=dispatch_sort_key)
    targets = targets[:limit]
    dispatched_at = utcnow()
    for target in targets:
        target.pop("_dispatch_binding").last_read_dispatched_at = dispatched_at
    for target in targets:
        checkpoint = target["identity_checkpoint"]
        target["identity_transition"] = {
            "version": 1,
            "source_version": "v16.104",
            "legacy_messages": [
                {
                    "dedupe_key": item["dedupe_key"],
                    "source_message_key": item["source_message_key"],
                    "message_type": item["message_type"],
                    "sender_role": item["sender_role"],
                }
                for item in checkpoint["recent_messages"]
            ],
        }
    return {
        "targets": targets,
        "poll_after_seconds": 10,
        "next_action": NEXT_ACTION_NONE,
    }


def _read_target_payload(
    db: Session,
    binding: WechatSessionBinding,
    *,
    read_reason: str,
) -> dict:
    target = {
        "conversation_id": binding.conversation_id,
        "lead_id": binding.lead_id,
        "sales_id": binding.sales_id,
        "remark_code": binding.remark_code,
        "rpa_session_key": binding.rpa_session_key,
        "display_name": binding.display_name,
        "last_ingested_at": binding.last_ingested_at,
        "read_reason": read_reason,
        "authorization_revision": _authorization_revision(binding),
        "identity_checkpoint": _identity_checkpoint(
            db,
            conversation_id=binding.conversation_id,
        ),
        "next_read_due_at": binding.next_read_due_at,
    }
    if _clean_locator(binding.row_fingerprint):
        target["row_fingerprint"] = binding.row_fingerprint
    if binding.ocr_confidence is not None:
        target["ocr_confidence"] = binding.ocr_confidence
    return target


def read_authorization_snapshot(
    db: Session,
    *,
    binding: WechatSessionBinding,
    enforce_read_due: bool = True,
) -> dict:
    """Return the current lightweight authorization without legacy identity history."""

    conversation = _upsert_conversation_for_binding(db, binding)
    from app.services.c3_service import enforce_open_handoff_gate

    enforce_open_handoff_gate(db, conversation)
    _prepare_due_recall(conversation)
    _sync_read_backoff_with_conversation(binding, conversation)
    read_reason = _read_reason(binding, conversation)
    allowed = bool(
        binding.bind_status == BIND_STATUS_BOUND
        and binding.listen_status in {LISTEN_STATUS_LISTENING, LISTEN_STATUS_DEGRADED}
        and binding.allow_listening
        and _clean_locator(binding.remark_code)
        and binding.conversation_id
        and binding.deleted_at is None
        and conversation.status not in CONVERSATION_CLOSED_STATUSES
        and read_reason
        and (
            not enforce_read_due
            or _read_is_due(binding, read_reason=str(read_reason))
        )
    )
    checkpoint = _identity_checkpoint(
        db,
        conversation_id=binding.conversation_id,
    )
    result = {
        "allowed": allowed,
        "recovery_decision": "allowed" if allowed else "retry_later",
        "conversation_id": binding.conversation_id,
        "authorization_revision": _authorization_revision(binding),
        "read_reason": read_reason or "",
        "identity_checkpoint": checkpoint,
        "next_read_due_at": binding.next_read_due_at,
    }
    if allowed:
        result["target"] = _read_target_payload(
            db,
            binding,
            read_reason=str(read_reason),
        )
    return result


def read_authorization_for_worker(
    db: Session,
    *,
    worker: Worker,
    conversation_id: str,
    continuation_batch_id: str | None = None,
    continuation_token: str | None = None,
) -> dict:
    """Lightweight long-action authorization check without target discovery data."""

    binding = db.scalar(
        select(WechatSessionBinding).where(
            WechatSessionBinding.conversation_id == conversation_id,
        )
    )
    terminal_binding = bool(
        not binding
        or binding.worker_id != worker.id
        or binding.deleted_at is not None
        or binding.bind_status
        in {
            BIND_STATUS_UNBOUND,
            BIND_STATUS_FAILED,
            BIND_STATUS_DISABLED,
        }
        or binding.listen_status == LISTEN_STATUS_DISABLED
    )
    if terminal_binding:
        return {
            "allowed": False,
            "recovery_decision": "target_terminated",
            "conversation_id": conversation_id,
            "authorization_revision": "",
            "read_reason": "",
        }
    if bool(continuation_batch_id) != bool(continuation_token):
        raise AppError(
            "C3_BATCH_CONTINUATION_INCOMPLETE",
            "批次续行标识和 token 必须同时提供",
            400,
        )
    if continuation_batch_id:
        batch = db.get(MessageBatch, str(continuation_batch_id))
        if (
            not batch
            or batch.deleted_at
            or batch.conversation_id != conversation_id
        ):
            return {
                "allowed": False,
                "authorization_scope": "batch_continuation",
                "batch_id": str(continuation_batch_id),
                "conversation_id": conversation_id,
                "authorization_revision": "",
                "read_reason": "",
            }
        from app.services.c3_service import message_batch_continuation_authorization

        result = message_batch_continuation_authorization(
            db,
            worker=worker,
            batch=batch,
            binding=binding,
            presented_token=str(continuation_token),
        )
        result["identity_checkpoint"] = _identity_checkpoint(
            db,
            conversation_id=binding.conversation_id,
        )
        return result
    conversation = db.get(Conversation, conversation_id)
    if conversation and (
        conversation.deleted_at is not None
        or conversation.status in CONVERSATION_CLOSED_STATUSES
    ):
        return {
            "allowed": False,
            "recovery_decision": "target_terminated",
            "conversation_id": conversation_id,
            "authorization_revision": _authorization_revision(binding),
            "read_reason": "",
        }
    return read_authorization_snapshot(db, binding=binding)


def confirm_friend_activation(
    db: Session,
    worker: Worker,
    conversation_id: str,
    payload: WechatFriendActivationConfirmRequest,
) -> dict:
    binding = db.scalar(
        select(WechatSessionBinding)
        .where(
            WechatSessionBinding.conversation_id == conversation_id,
            WechatSessionBinding.worker_id == worker.id,
            WechatSessionBinding.deleted_at.is_(None),
        )
        .with_for_update()
    )
    if not binding:
        raise AppError("WECHAT_BINDING_NOT_FOUND", "微信会话绑定不存在", 404)
    if (
        binding.bind_status != BIND_STATUS_BOUND
        or not binding.allow_listening
        or binding.listen_status not in {LISTEN_STATUS_LISTENING, LISTEN_STATUS_DEGRADED}
    ):
        raise AppError("C2_TARGET_NOT_AUTHORIZED", "好友激活确认时会话已不允许读取", 409)
    if str(payload.authorization_revision or "") != _authorization_revision(binding):
        raise AppError("MESSAGE_AUTHORIZATION_REVISION_EXPIRED", "好友激活授权已过期", 409)
    if _clean_locator(payload.remark_code) != _clean_locator(binding.remark_code):
        raise AppError("MESSAGE_TARGET_IDENTITY_MISMATCH", "好友激活的短码与绑定会话不一致", 409)
    title_evidence = payload.title_evidence if isinstance(payload.title_evidence, dict) else {}
    if (
        str(payload.conversation_type or "").strip().lower() != "private"
        or not payload.chat_surface_ready
        or title_evidence.get("short_code_confirmed") is not True
        or title_evidence.get("admission_allowed") is not True
        or str(title_evidence.get("conversation_type") or "").strip().lower() != "private"
    ):
        raise AppError(
            "C2_FRIEND_ACTIVATION_EVIDENCE_INVALID",
            "好友激活缺少短码、private 单聊或会话就绪证据",
            409,
        )
    conversation = _upsert_conversation_for_binding(db, binding)
    if (
        conversation.friend_state == "friend_request_sent"
        and conversation.status == "friend_request_sent"
    ):
        conversation.friend_state = "friend_active"
        conversation.status = "friend_activation_reading"
    elif (
        conversation.friend_state == "friend_active"
        and conversation.status == "friend_active"
    ):
        conversation.status = "friend_activation_reading"
    elif not (
        conversation.friend_state == "friend_active"
        and conversation.status == "friend_activation_reading"
    ):
        raise AppError("C2_FRIEND_ACTIVATION_STATE_INVALID", "当前好友状态不允许执行激活确认", 409)
    db.flush()
    return {
        "conversation_id": conversation_id,
        "friend_state": conversation.friend_state,
        "conversation_status": conversation.status,
        "activation_confirmed": True,
        "authorization_revision": _authorization_revision(binding),
        "next_action": "read_current_chat",
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


def _friend_acceptance_recently_visible(binding: WechatSessionBinding) -> bool:
    last_seen_at = binding.last_seen_at
    if last_seen_at is None:
        return False
    if last_seen_at.tzinfo is None:
        last_seen_at = last_seen_at.replace(tzinfo=timezone.utc)
    age_seconds = (
        utcnow() - last_seen_at.astimezone(timezone.utc)
    ).total_seconds()
    return 0 <= age_seconds <= get_settings().c2_friend_acceptance_visible_ttl_seconds


def _read_reason(binding: WechatSessionBinding, conversation: Conversation) -> str | None:
    if (
        conversation.friend_state == "friend_request_sent"
        and conversation.status == "friend_request_sent"
    ):
        return (
            "friend_acceptance_visible_hit"
            if _friend_acceptance_recently_visible(binding)
            else None
        )
    if (
        conversation.friend_state == "friend_active"
        and conversation.status == "friend_active"
    ):
        return (
            "friend_acceptance_visible_hit"
            if _friend_acceptance_recently_visible(binding)
            else None
        )
    if (
        conversation.friend_state == "friend_active"
        and conversation.status == "friend_activation_reading"
    ):
        return "friend_acceptance_visible_hit"
    if conversation.status == "recall_precheck":
        return "recall_precheck"
    if conversation.status == "ai_active" and binding.unread_hint:
        return "visible_unread"
    if conversation.status == "waiting_user_reply" and conversation.last_ai_reply_at:
        return "recent_ai_sent"
    if conversation.status in {"waiting_user_reply", "recalled_waiting_user", "sales_replied_waiting_user"}:
        return "waiting_user_reply"
    if conversation.status == "waiting_sales_reply":
        return "waiting_sales_reply"
    return None


def _prepare_due_recall(conversation: Conversation) -> None:
    settings = get_settings()
    if conversation.status not in {"waiting_user_reply", "sales_replied_waiting_user", "recalled_waiting_user"}:
        return
    if not conversation.ai_enabled or not conversation.next_recall_at or conversation.recall_count >= settings.c3_recall_max_cycles:
        return
    now = utcnow()
    comparable_now = now if getattr(conversation.next_recall_at, "tzinfo", None) else now.replace(tzinfo=None)
    if conversation.next_recall_at > comparable_now:
        return
    local_now = now.astimezone(ZoneInfo("Asia/Shanghai"))
    hour = local_now.hour
    start = settings.c3_recall_quiet_start_hour
    end = settings.c3_recall_quiet_end_hour
    in_quiet = hour >= start or hour < end if start > end else start <= hour < end
    if in_quiet:
        return
    today = local_now.date()
    if conversation.recall_daily_date != today:
        conversation.recall_daily_date = today
        conversation.recall_daily_count = 0
    if conversation.recall_daily_count >= settings.c3_recall_daily_limit:
        return
    conversation.recall_origin_status = conversation.status
    conversation.status = "recall_precheck"
    conversation.recall_cycle_id = conversation.recall_cycle_id or f"recall-{uuid.uuid4()}"


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


def _validate_text_only_image_value(value: object, *, path: str) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            normalized_key = str(key).strip().lower()
            if normalized_key in IMAGE_FORBIDDEN_FIELD_NAMES or normalized_key.startswith(IMAGE_FORBIDDEN_FIELD_PREFIXES):
                raise AppError("IMAGE_PERSISTENCE_FIELD_FORBIDDEN", f"图片结果包含禁止持久化字段: {path}.{key}", 409)
            _validate_text_only_image_value(child, path=f"{path}.{key}")
        return
    if isinstance(value, list):
        for index, child in enumerate(value):
            _validate_text_only_image_value(child, path=f"{path}[{index}]")
        return
    if isinstance(value, (str, int, float, bool)) or value is None:
        if isinstance(value, str):
            compact = value.strip().lower()
            if compact.startswith(("data:image/", "file://")) or re.match(r"^[a-z]:[\\/]", compact):
                raise AppError("IMAGE_PERSISTENCE_VALUE_FORBIDDEN", f"图片结果包含可还原图片或本地路径: {path}", 409)
        return
    raise AppError("IMAGE_PERSISTENCE_VALUE_INVALID", f"图片结果字段不是 JSON 文字结构: {path}", 409)


def _media_error_data(source_message_key: str) -> dict[str, str]:
    source_key = str(source_message_key or "").strip()
    return {"source_message_key": source_key} if source_key else {}


def _validate_image_understanding(
    raw_payload: dict,
    *,
    source_message_key: str = "",
) -> None:
    # Validate the complete persisted payload, including nested OmniAuto
    # observation/source evidence. Forbidden image material must not be hidden
    # outside the two formal Vision result fields.
    _validate_text_only_image_value(raw_payload, path="raw_payload")
    understanding = raw_payload.get("customer_image_understanding")
    bridge = raw_payload.get("visual_bridge_input")
    if not isinstance(understanding, dict) or not isinstance(bridge, dict):
        raise AppError(
            "IMAGE_UNDERSTANDING_REQUIRED",
            "图片消息缺少 Vision 文字化结果",
            409,
            _media_error_data(source_message_key),
        )
    unexpected = sorted(set(understanding) - IMAGE_UNDERSTANDING_FIELDS)
    if unexpected:
        raise AppError(
            "IMAGE_UNDERSTANDING_FIELD_INVALID",
            f"图片理解结果包含非白名单字段: {unexpected}",
            409,
            _media_error_data(source_message_key),
        )
    if int(understanding.get("schema_version") or 0) != 1:
        raise AppError(
            "IMAGE_UNDERSTANDING_SCHEMA_INVALID",
            "图片理解结果 schema_version 必须为 1",
            409,
            _media_error_data(source_message_key),
        )
    audit = understanding.get("audit") if isinstance(understanding.get("audit"), dict) else {}
    unexpected_audit = sorted(set(audit) - IMAGE_UNDERSTANDING_AUDIT_FIELDS)
    if unexpected_audit:
        raise AppError(
            "IMAGE_UNDERSTANDING_FIELD_INVALID",
            f"图片审计结果包含非白名单字段: {unexpected_audit}",
            409,
            _media_error_data(source_message_key),
        )
    unexpected_bridge = sorted(set(bridge) - VISUAL_BRIDGE_FIELDS)
    if unexpected_bridge:
        raise AppError(
            "IMAGE_VISUAL_BRIDGE_FIELD_INVALID",
            f"图片 Brain 桥接结果包含非白名单字段: {unexpected_bridge}",
            409,
            _media_error_data(source_message_key),
        )
    understanding_errors = validate_image_result_schema(
        understanding,
        "customer_image_understanding_v1",
    )
    bridge_errors = validate_image_result_schema(
        bridge,
        "visual_bridge_input_v1",
    )
    if understanding_errors or bridge_errors:
        raise AppError(
            "IMAGE_UNDERSTANDING_SCHEMA_INVALID",
            "图片理解结果不符合共享合同",
            409,
            {
                **_media_error_data(source_message_key),
                "schema_errors": [
                    *understanding_errors[:8],
                    *bridge_errors[:8],
                ],
            },
        )
    _validate_text_only_image_value(understanding, path="customer_image_understanding")
    _validate_text_only_image_value(bridge, path="visual_bridge_input")


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
    return canonical_reply_text(value)


def _reply_text_hash(value: object) -> str:
    return reply_text_hash(value)


def _verified_ai_reply_action_for_self_message(
    db: Session,
    *,
    conversation_id: str,
    content: object,
    source_message_key: str,
    raw_payload: dict,
) -> ReplyAction | None:
    """Validate a Worker-confirmed stable bubble receipt against one sent action."""
    normalized = _normalized_contract_text(content)
    receipt = (
        raw_payload.get("ai_reply_receipt")
        if isinstance(raw_payload.get("ai_reply_receipt"), dict)
        else {}
    )
    action_id = str(receipt.get("reply_action_id") or "").strip()
    receipt_hash = str(receipt.get("reply_text_hash") or "").strip()
    receipt_source_key = str(receipt.get("source_message_key") or "").strip()
    worker_stable_id = str(receipt.get("worker_stable_id") or "").strip()
    reconciliation_state = str(
        receipt.get("reconciliation_state") or "confirmed"
    ).strip()
    if (
        not normalized
        or not action_id
        or not receipt_hash
        or not worker_stable_id
        or receipt_source_key != source_message_key
        or receipt_hash != _reply_text_hash(normalized)
    ):
        return None

    used_action_ids: set[str] = set()
    recent_self_events = db.scalars(
        select(MessageEvent)
        .where(
            MessageEvent.conversation_id == conversation_id,
            MessageEvent.sender_role.in_(SALES_SIDE_SENDER_ROLES),
        )
        .order_by(MessageEvent.ingested_at.desc())
        .limit(200)
    ).all()
    for event in recent_self_events:
        raw = event.raw_payload if isinstance(event.raw_payload, dict) else {}
        used_action_id = str(raw.get("ai_reply_action_id") or "").strip()
        if used_action_id:
            used_action_ids.add(used_action_id)

    if (
        action_id in used_action_ids
        and reconciliation_state != "ai_unreconciled"
    ):
        return None
    action = db.get(ReplyAction, action_id)
    if (
        not action
        or action.deleted_at is not None
        or action.conversation_id != conversation_id
        or action.status not in {
            "sending",
            "sent",
            "unknown_send_result",
        }
        or action.reply_text_hash != receipt_hash
        or _normalized_contract_text(action.reply_text) != normalized
    ):
        return None
    if reconciliation_state == "ai_unreconciled":
        return action
    try:
        confirmed_at = datetime.fromisoformat(
            str(receipt.get("confirmed_at") or "").replace("Z", "+00:00")
        )
    except ValueError:
        return None
    now = utcnow()
    if confirmed_at.tzinfo is None:
        confirmed_at = confirmed_at.replace(tzinfo=now.tzinfo)
    if action.status == "sent":
        sent_at = action.sent_at
        if sent_at is None:
            return None
        if sent_at.tzinfo is None:
            sent_at = sent_at.replace(tzinfo=now.tzinfo)
        if abs(confirmed_at - sent_at) > AI_REPLY_RECEIPT_CLOCK_SKEW:
            return None
    else:
        claimed_at = action.sending_claimed_at
        if claimed_at is None:
            return None
        if claimed_at.tzinfo is None:
            claimed_at = claimed_at.replace(tzinfo=now.tzinfo)
        if confirmed_at + AI_REPLY_RECEIPT_CLOCK_SKEW < claimed_at:
            return None
    return action


def _unreconciled_ai_reply_action_without_local_receipt(
    db: Session,
    *,
    conversation_id: str,
    content: object,
) -> ReplyAction | None:
    """Protect one unresolved AI send when the Worker's local receipt is lost."""

    normalized = _normalized_contract_text(content)
    if not normalized:
        return None
    used_action_ids: set[str] = set()
    recent_self_events = db.scalars(
        select(MessageEvent)
        .where(
            MessageEvent.conversation_id == conversation_id,
            MessageEvent.sender_role.in_(SALES_SIDE_SENDER_ROLES),
        )
        .order_by(MessageEvent.ingested_at.desc())
        .limit(200)
    ).all()
    for event in recent_self_events:
        raw = event.raw_payload if isinstance(event.raw_payload, dict) else {}
        action_id = str(raw.get("ai_reply_action_id") or "").strip()
        if action_id:
            used_action_ids.add(action_id)

    candidates = db.scalars(
        select(ReplyAction)
        .where(
            ReplyAction.conversation_id == conversation_id,
            ReplyAction.status == "unknown_send_result",
            ReplyAction.reply_text_hash == _reply_text_hash(normalized),
            ReplyAction.deleted_at.is_(None),
        )
        .order_by(ReplyAction.created_at.desc(), ReplyAction.id.desc())
    ).all()
    return next(
        (
            action
            for action in candidates
            if action.id not in used_action_ids
            and _normalized_contract_text(action.reply_text) == normalized
        ),
        None,
    )


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
    effective_rule = dict(rule)
    item_state = str(observation.get("item_state") or "").strip().lower()
    terminal_non_ingestible = row_kind == "image_bubble" and item_state in {
        str(value) for value in rule.get("terminal_non_ingestible_item_states") or []
    }
    if terminal_non_ingestible:
        effective_rule["ingestible"] = False
        effective_rule["required_fields"] = [
            "observation_id", "row_kind", "sender_role", "sender_role_source",
            "message_type", "voice_state", "source_message",
        ]
    elif row_kind == "image_bubble" and item_state == "failed":
        effective_rule["required_fields"] = list(rule.get("failed_required_fields") or [])
    elif item_state == "failed" and bool(rule.get("failed_ingestible")):
        effective_rule["ingestible"] = True
        effective_rule["required_fields"] = list(
            rule.get("failed_required_fields") or []
        )
    if require_ingestible is True and not bool(effective_rule.get("ingestible")):
        raise AppError("MESSAGE_ROW_KIND_NOT_INGESTIBLE", "V3 非可入库行被组装成最终消息", 409)
    for field in effective_rule.get("required_fields") or []:
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
    return observation_id, effective_rule


def _validate_v3_request_contract(payload: WechatMessageIngestRequest) -> None:
    if str(payload.contract_revision or "").strip() != CONTRACT_REVISION_V3:
        raise AppError("MESSAGE_CONTRACT_REVISION_MISMATCH", "V3 消息合同修订号不一致", 409)
    if str(payload.contract_sha256 or "").strip().lower() != CONTRACT_SHA256_V3:
        raise AppError("MESSAGE_CONTRACT_SHA256_MISMATCH", "V3 消息合同指纹不一致", 409)
    if int(payload.observation_schema_version or 0) != OBSERVATION_SCHEMA_VERSION_V3:
        raise AppError("MESSAGE_OBSERVATION_SCHEMA_VERSION_MISMATCH", "V3 observation schema 版本不一致", 409)

    evidence = payload.evidence.model_dump(mode="json")
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

        item_state = str(item.item_state or "").strip().lower()
        failed_voice = canonical_type == "voice" and item_state == "failed"
        if (
            canonical_type in {"text", "voice", "system"}
            and not failed_voice
        ) or (
            canonical_type == "image" and str(item.item_state or "").strip().lower() == "completed"
        ):
            observed_content = _normalized_contract_text(observation.get("content_clean"))
            canonical_content = _normalized_contract_text(item.content)
            if not observed_content or canonical_content != observed_content:
                raise AppError("MESSAGE_ROW_CONTENT_MISMATCH", "OmniAuto observation 与 Worker 正文不一致", 409)
        if canonical_type == "image":
            if str(item.item_state or "").strip().lower() == "completed":
                _validate_image_understanding(
                    raw_payload,
                    source_message_key=item.source_message_key,
                )
                if observation.get("customer_image_understanding") != raw_payload.get("customer_image_understanding"):
                    raise AppError(
                        "IMAGE_UNDERSTANDING_EVIDENCE_MISMATCH",
                        "图片理解结果与原始 image_bubble 证据不一致",
                        409,
                        _media_error_data(item.source_message_key),
                    )
                if observation.get("visual_bridge_input") != raw_payload.get("visual_bridge_input"):
                    raise AppError(
                        "IMAGE_BRIDGE_EVIDENCE_MISMATCH",
                        "图片桥接输入与原始 image_bubble 证据不一致",
                        409,
                        _media_error_data(item.source_message_key),
                    )
            else:
                _validate_text_only_image_value(raw_payload, path="raw_payload")
                reason = str(raw_payload.get("image_processing_reason") or observation.get("image_processing_reason") or "").strip()
                if not reason:
                    raise AppError(
                        "IMAGE_FAILURE_REASON_MISSING",
                        "失败图片事实缺少明确原因",
                        409,
                        _media_error_data(item.source_message_key),
                    )
        elif failed_voice:
            reason = str(
                raw_payload.get("voice_processing_reason")
                or observation.get("voice_processing_reason")
                or ""
            ).strip()
            if not reason:
                raise AppError(
                    "VOICE_FAILURE_REASON_MISSING",
                    "失败语音事实缺少明确原因",
                    409,
                    _media_error_data(item.source_message_key),
                )
            if reason != str(
                observation.get("voice_processing_reason") or ""
            ).strip():
                raise AppError(
                    "VOICE_FAILURE_REASON_MISMATCH",
                    "失败语音原因与原始 observation 不一致",
                    409,
                    _media_error_data(item.source_message_key),
                )

    if mapped_observation_ids != ingestible_observation_ids:
        missing = sorted(ingestible_observation_ids - mapped_observation_ids)
        unexpected = sorted(mapped_observation_ids - ingestible_observation_ids)
        raise AppError(
            "MESSAGE_OBSERVATION_MAPPING_INCOMPLETE",
            f"V3 observation 与最终消息不是一一对应: missing={missing}, unexpected={unexpected}",
            409,
        )


def _ordered_v3_messages(payload: WechatMessageIngestRequest) -> list:
    """Validate the only business ordering signal and return messages top-to-bottom."""

    orders = [int(item.message_position.screen_order) for item in payload.messages]
    if len(orders) != len(set(orders)):
        raise AppError(
            "MESSAGE_SCREEN_ORDER_DUPLICATED",
            "同一批消息的 screen_order 不能重复",
            409,
        )
    return sorted(
        list(payload.messages),
        key=lambda item: int(item.message_position.screen_order),
    )


def _visible_existing_message_orders(
    db: Session,
    *,
    conversation_id: str,
    evidence_payload: dict,
) -> dict[str, int]:
    """Resolve persisted message ids against Worker's complete final-frame slots."""

    source_orders: dict[str, int] = {}
    conflicted_sources: set[str] = set()
    for raw in evidence_payload.get("slot_ledger_states") or []:
        if not isinstance(raw, dict):
            continue
        if str(raw.get("order_source") or "").strip() != "visual_top":
            continue
        source_key = str(raw.get("source_message_key") or "").strip()
        try:
            screen_order = int(raw.get("screen_order") or 0)
        except (TypeError, ValueError):
            continue
        if not source_key or screen_order <= 0:
            continue
        existing_order = source_orders.get(source_key)
        if existing_order is not None and existing_order != screen_order:
            conflicted_sources.add(source_key)
            continue
        source_orders[source_key] = screen_order
    for source_key in conflicted_sources:
        source_orders.pop(source_key, None)
    if not source_orders:
        return {}

    events = db.scalars(
        select(MessageEvent).where(
            MessageEvent.conversation_id == conversation_id,
            MessageEvent.source_message_key.in_(list(source_orders)),
        )
    )
    return {
        event.id: source_orders[event.source_message_key]
        for event in events
        if event.source_message_key in source_orders
    }


def _flow_gate_details_by_code(evidence_payload: dict) -> dict[str, list[dict]]:
    strong_position_sources = FLOW_GATE_STRONG_POSITION_SOURCES_V3
    result: dict[str, list[dict]] = {}
    for raw in evidence_payload.get("flow_gate_details") or []:
        if not isinstance(raw, dict):
            raise AppError(
                "MESSAGE_FLOW_GATE_DETAIL_INVALID",
                "安全门禁位置证据必须是对象",
                409,
            )
        code = str(raw.get("error_code") or "").strip()
        position_source = str(raw.get("position_source") or "").strip()
        if not code or not position_source:
            raise AppError(
                "MESSAGE_FLOW_GATE_DETAIL_INVALID",
                "安全门禁位置证据缺少 error_code 或 position_source",
                409,
            )
        detail = {
            "error_code": code,
            "position_source": position_source,
        }
        subject_sender_role = str(raw.get("subject_sender_role") or "").strip()
        if subject_sender_role:
            if subject_sender_role not in {"customer", "self"}:
                raise AppError(
                    "MESSAGE_FLOW_GATE_SUBJECT_ROLE_INVALID",
                    "安全门禁发送方角色不合法",
                    409,
                )
            detail["subject_sender_role"] = subject_sender_role
        for key in ("min_screen_order", "max_screen_order"):
            value = raw.get(key)
            if value is None:
                continue
            try:
                parsed = int(value)
            except (TypeError, ValueError) as exc:
                raise AppError(
                    "MESSAGE_FLOW_GATE_POSITION_INVALID",
                    "安全门禁 screen_order 不合法",
                    409,
                ) from exc
            if parsed < 1:
                raise AppError(
                    "MESSAGE_FLOW_GATE_POSITION_INVALID",
                    "安全门禁 screen_order 必须大于零",
                    409,
                )
            detail[key] = parsed
        if (
            detail.get("min_screen_order") is not None
            and detail.get("max_screen_order") is not None
            and int(detail["min_screen_order"]) > int(detail["max_screen_order"])
        ):
            raise AppError(
                "MESSAGE_FLOW_GATE_POSITION_INVALID",
                "安全门禁位置范围前后颠倒",
                409,
            )
        has_position = (
            detail.get("min_screen_order") is not None
            or detail.get("max_screen_order") is not None
        )
        if has_position and position_source not in strong_position_sources:
            raise AppError(
                "MESSAGE_FLOW_GATE_POSITION_SOURCE_UNTRUSTED",
                "安全门禁位置没有真实气泡坐标证据",
                409,
            )
        result.setdefault(code, []).append(detail)
    return result


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
    ordered_messages = _ordered_v3_messages(payload)
    evidence_payload = payload.evidence.model_dump(mode="json")
    ingest_partition = (
        evidence_payload.get("ingest_partition")
        if isinstance(evidence_payload.get("ingest_partition"), dict)
        else {}
    )
    partition_index = int(ingest_partition.get("index") or 0)
    partition_count = int(ingest_partition.get("count") or 0)
    partitioned = bool(partition_index and partition_count)
    partition_final = bool(
        partitioned and partition_index == partition_count
    )
    if partitioned:
        if str(ingest_partition.get("group_id") or "") != payload.read_run_id:
            raise AppError(
                "MESSAGE_INGEST_PARTITION_IDENTITY_MISMATCH",
                "消息分片与 read_run_id 不一致",
                409,
            )
        expected_source_keys = {
            str(value).strip()
            for value in (
                ingest_partition.get("expected_source_message_keys") or []
            )
            if str(value).strip()
        }
        current_source_keys = {
            str(item.source_message_key or "").strip()
            for item in ordered_messages
            if str(item.source_message_key or "").strip()
        }
        if not current_source_keys.issubset(expected_source_keys):
            raise AppError(
                "MESSAGE_INGEST_PARTITION_SOURCE_MISMATCH",
                "消息分片包含完整清单之外的消息",
                409,
            )
    else:
        expected_source_keys = set()
    incoming_read_reason = str(
        evidence_payload.get("authorization_read_reason") or ""
    ).strip()
    if not incoming_read_reason:
        raise AppError(
            "MESSAGE_AUTHORIZATION_READ_REASON_MISSING",
            "V3 请求缺少取得消息时的授权原因",
            409,
        )
    flow_gate_details_by_code = _flow_gate_details_by_code(evidence_payload)
    flow_gate_error_codes = [
        str(value).strip()
        for value in (evidence_payload.get("flow_gate_errors") or [])
        if str(value).strip()
    ]
    retired_flow_gate_codes = sorted(
        set(flow_gate_error_codes) & RETIRED_FLOW_GATE_CODES_V3
    )
    if retired_flow_gate_codes:
        raise AppError(
            "MESSAGE_FLOW_GATE_CODE_RETIRED",
            "请求包含已退出 V3 合同的图片临时门禁",
            409,
            {
                "retired_flow_gate_codes": retired_flow_gate_codes,
            },
        )
    if set(flow_gate_details_by_code) != set(flow_gate_error_codes):
        raise AppError(
            "MESSAGE_FLOW_GATE_DETAIL_MISMATCH",
            "安全门禁错误码与位置证据不是一一对应",
            409,
        )
    partition_gate = "C2_INGEST_PARTITION_INCOMPLETE"
    if partitioned and not partition_final:
        if flow_gate_error_codes != [partition_gate]:
            raise AppError(
                "MESSAGE_INGEST_PARTITION_GATE_MISSING",
                "非末尾消息分片必须使用临时分片门禁",
                409,
            )
    elif partition_gate in flow_gate_error_codes:
        raise AppError(
            "MESSAGE_INGEST_PARTITION_GATE_STALE",
            "末尾分片或普通批次不能保留临时分片门禁",
            409,
        )
    for item in ordered_messages:
        if str(item.sender_role_hint or "").strip().lower() not in SENDER_ROLES_V3:
            raise AppError("MESSAGE_SENDER_ROLE_INVALID", "V3 消息发送方角色不合法", 409)
        if str(item.message_type or "").strip().lower() not in MESSAGE_TYPES_V3:
            raise AppError("MESSAGE_TYPE_INVALID", "V3 消息类型不合法", 409)
        item_state = str(item.item_state or "").strip().lower()
        message_type = str(item.message_type or "").strip().lower()
        failed_fact_allowed = (
            item_state == "failed"
            and message_type in FAILED_INGESTIBLE_MESSAGE_TYPES_V3
        )
        if item_state != "completed" and not failed_fact_allowed:
            raise AppError(
                "MESSAGE_ITEM_STATE_INVALID",
                "只有完成消息或明确失败的图片/语音事实可以入库",
                409,
            )
        if str(item.flow_state or "").strip().lower() not in FLOW_STATES_V3:
            raise AppError("MESSAGE_FLOW_STATE_INVALID", "V3 消息流程状态不合法", 409)
    conversation = _upsert_conversation_for_binding(db, binding)
    from app.services.c3_service import enforce_open_handoff_gate

    open_handoff_active = bool(
        enforce_open_handoff_gate(
            db,
            conversation,
            for_update=True,
        )
    )
    origin_conversation_status = str(
        conversation.recall_origin_status
        if conversation.status == "recall_precheck"
        and conversation.recall_origin_status
        else conversation.status
        or ""
    )
    current_authorization = read_authorization_snapshot(
        db,
        binding=binding,
        enforce_read_due=False,
    )
    continuation_batch_id = str(
        evidence_payload.get("continuation_batch_id") or ""
    ).strip()
    continuation_token = str(
        evidence_payload.get("continuation_token") or ""
    ).strip()
    continuation_authorization: dict = {}
    if continuation_batch_id and continuation_token:
        continuation_batch = db.get(MessageBatch, continuation_batch_id)
        if (
            continuation_batch
            and not continuation_batch.deleted_at
            and continuation_batch.conversation_id == payload.conversation_id
        ):
            from app.services.c3_service import (
                message_batch_continuation_authorization,
            )

            continuation_authorization = (
                message_batch_continuation_authorization(
                    db,
                    worker=worker,
                    batch=continuation_batch,
                    binding=binding,
                    presented_token=continuation_token,
                )
            )
    current_authorization_matches = bool(
        current_authorization.get("allowed") is True
        and str(current_authorization.get("read_reason") or "")
        == incoming_read_reason
    )
    continuation_authorization_matches = bool(
        continuation_authorization.get("allowed") is True
        and str(continuation_authorization.get("read_reason") or "")
        == incoming_read_reason
    )
    state_transition_allowed = bool(
        current_authorization_matches or continuation_authorization_matches
    )
    state_transition_reason = (
        "batch_continuation_matches"
        if continuation_authorization_matches
        else "authorization_state_matches"
        if state_transition_allowed
        else (
            "batch_continuation_invalid"
            if continuation_batch_id
            else "authorization_read_reason_changed"
        )
    )
    conversation_allowed = conversation.status not in CONVERSATION_CLOSED_STATUSES
    observed_rpa_session_key = payload.rpa_session_key or ""

    ingested_count = 0
    duplicated_count = 0
    ignored_count = 0
    read_has_new_facts = False
    results: list[dict] = []
    new_customer_message_ids: list[str] = []
    new_sales_message = False
    human_sales_observed = False
    last_human_sales_screen_order = 0
    visible_message_orders = _visible_existing_message_orders(
        db,
        conversation_id=payload.conversation_id,
        evidence_payload=evidence_payload,
    )
    for item in ordered_messages:
        message_type = str(item.message_type or "").strip().lower()
        source_dedupe_key = item.dedupe_key.strip() if item.dedupe_key else ""
        raw_payload = dict(item.raw_payload or {})
        if item.message_position:
            raw_payload["message_position"] = item.message_position.model_dump(exclude_none=True)
        content = item.content
        if message_type == "image" and str(item.item_state or "").strip().lower() == "completed":
            _validate_image_understanding(
                raw_payload,
                source_message_key=item.source_message_key,
            )
        read_failure = _read_failure_result(item.raw_payload, evidence_payload)
        if read_failure:
            raise AppError(read_failure.upper(), "V3 读取失败证据不能伪装成可入库消息", 409)
        sender_role = str(item.sender_role_hint or "").strip().lower()
        if message_type == "voice":
            if str(item.item_state or "").strip().lower() == "failed":
                if item.content is not None:
                    raise AppError(
                        "VOICE_FAILURE_CONTENT_INVALID",
                        "失败语音事实不能伪造转写正文",
                        409,
                        _media_error_data(item.source_message_key),
                    )
                content = None
            else:
                voice_error_code = _voice_failure_code(raw_payload)
                transcript = str(item.content or "").strip()
                if not voice_error_code and not transcript:
                    voice_error_code = "VOICE_TRANSCRIBE_EMPTY"
                if VOICE_DURATION_RE.match(transcript) or _looks_like_voice_payload_text(transcript):
                    voice_error_code = "VOICE_TRANSCRIBE_INVALID_CONTENT"
                if voice_error_code:
                    raise AppError(
                        voice_error_code,
                        "V3 未完成或无效的语音不能入库",
                        409,
                        _media_error_data(item.source_message_key),
                    )
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
            _raise_message_identity_collision(
                db,
                existing=exists,
                incoming_sender_role=sender_role,
                incoming_message_type=message_type,
                incoming_content=content,
                incoming_raw_payload=raw_payload,
                source_message_key=item.source_message_key,
                dedupe_key=dedupe_key,
            )
            duplicated_count += 1
            results.append(
                {
                    "source_message_key": item.source_message_key,
                    "dedupe_key": dedupe_key,
                    "ingest_result": "duplicated",
                    "error_code": "MESSAGE_INGEST_DUPLICATED",
                }
            )
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

        ai_reply_action = None
        if sender_role in SALES_SIDE_SENDER_ROLES:
            ai_reply_action = _verified_ai_reply_action_for_self_message(
                db,
                conversation_id=payload.conversation_id,
                content=content,
                source_message_key=item.source_message_key,
                raw_payload=raw_payload,
            )
            local_receipt = (
                raw_payload.get("ai_reply_receipt")
                if isinstance(raw_payload.get("ai_reply_receipt"), dict)
                else {}
            )
            server_guarded_unreconciled = False
            if not ai_reply_action and not local_receipt:
                ai_reply_action = _unreconciled_ai_reply_action_without_local_receipt(
                    db,
                    conversation_id=payload.conversation_id,
                    content=content,
                )
                server_guarded_unreconciled = ai_reply_action is not None
            raw_payload["sender_source"] = (
                "ai"
                if ai_reply_action and ai_reply_action.status == "sent"
                else "ai_unreconciled_server_guard"
                if server_guarded_unreconciled
                else "ai_unreconciled"
                if (
                    ai_reply_action
                    and str(
                        (
                            raw_payload.get("ai_reply_receipt")
                            if isinstance(
                                raw_payload.get("ai_reply_receipt"),
                                dict,
                            )
                            else {}
                        ).get("reconciliation_state")
                        or ""
                    )
                    == "ai_unreconciled"
                )
                else "ai_pending_ack"
                if ai_reply_action
                else "human"
            )
            if ai_reply_action:
                raw_payload["ai_reply_action_id"] = ai_reply_action.id
                raw_payload["ai_reply_text_hash"] = ai_reply_action.reply_text_hash

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
            raw_payload=raw_payload,
            evidence=evidence_payload,
            ocr_confidence=item.ocr_confidence,
            item_state=item.item_state,
            flow_state=item.flow_state,
            occurred_at=item.occurred_at,
            observed_at=payload.evidence.finished_at,
            observation_order=int(item.message_position.screen_order),
            ingested_at=utcnow(),
            error_code=(
                str(raw_payload.get("image_processing_reason") or "IMAGE_UNDERSTANDING_FAILED")[:64]
                if message_type == "image" and str(item.item_state or "").strip().lower() == "failed"
                else (
                    str(
                        raw_payload.get("voice_processing_reason")
                        or "VOICE_TRANSCRIBE_FAILED"
                    )[:64]
                    if message_type == "voice"
                    and str(item.item_state or "").strip().lower() == "failed"
                    else None
                )
            ),
        )
        try:
            with db.begin_nested():
                db.add(message)
                db.flush()
        except IntegrityError:
            concurrent_existing = db.scalar(
                select(MessageEvent).where(
                    MessageEvent.conversation_id == payload.conversation_id,
                    MessageEvent.dedupe_key == dedupe_key,
                )
            )
            if concurrent_existing is None:
                concurrent_source = db.scalar(
                    select(MessageEvent).where(
                        MessageEvent.conversation_id == payload.conversation_id,
                        MessageEvent.read_run_id == payload.read_run_id,
                        MessageEvent.source_message_key == item.source_message_key,
                    )
                )
                if concurrent_source is not None:
                    raise AppError(
                        "MESSAGE_SOURCE_CONFLICT",
                        "同一读取批次的来源消息已存在",
                        409,
                    )
                raise
            _raise_message_identity_collision(
                db,
                existing=concurrent_existing,
                incoming_sender_role=sender_role,
                incoming_message_type=message_type,
                incoming_content=content,
                incoming_raw_payload=raw_payload,
                source_message_key=item.source_message_key,
                dedupe_key=dedupe_key,
            )
            duplicated_count += 1
            results.append(
                {
                    "source_message_key": item.source_message_key,
                    "dedupe_key": dedupe_key,
                    "ingest_result": "duplicated",
                    "error_code": "MESSAGE_INGEST_DUPLICATED",
                }
            )
            continue
        binding.last_ingested_at = message.ingested_at
        event_time = message.occurred_at or message.ingested_at
        if state_transition_allowed:
            if message.sender_role == "customer":
                conversation.last_inbound_at = _latest_datetime(
                    conversation.last_inbound_at,
                    event_time,
                )
                conversation.recall_cycle_id = None
                conversation.recall_origin_status = None
                conversation.next_recall_at = None
                if open_handoff_active:
                    conversation.status = "waiting_sales_reply"
                else:
                    conversation.status = "ai_active"
                    new_customer_message_ids.append(message.id)
                    from app.services.c3_service import (
                        supersede_open_reply_actions_for_new_inbound,
                    )

                    supersede_open_reply_actions_for_new_inbound(
                        db,
                        binding.conversation_id,
                    )
            elif message.sender_role in SALES_SIDE_SENDER_ROLES:
                new_sales_message = True
                if str(raw_payload.get("sender_source") or "") in {
                    "ai",
                    "ai_pending_ack",
                    "ai_unreconciled",
                    "ai_unreconciled_server_guard",
                }:
                    # This is the right-side bubble produced by our own sent
                    # ReplyAction. It closes customer facts above it, but it is not
                    # evidence that a human sales person took over the conversation.
                    new_customer_message_ids.clear()
                    conversation.last_outbound_at = _latest_datetime(
                        conversation.last_outbound_at,
                        event_time,
                    )
                else:
                    from app.services.c3_service import (
                        close_open_handoffs_for_human_sales,
                    )

                    had_open_handoff = open_handoff_active
                    handoff_resolution = close_open_handoffs_for_human_sales(
                        db,
                        conversation_id=binding.conversation_id,
                        sales_message=message,
                        visible_message_orders=visible_message_orders,
                        sales_screen_order=int(item.message_position.screen_order),
                    )
                    open_handoff_active = bool(
                        handoff_resolution["remaining_open_count"]
                    )
                    # ordered_messages is the backend-validated screen order.
                    # A later human sales reply supersedes all earlier customer turns
                    # in this batch; only customer messages after that reply may open
                    # a new Brain batch.
                    new_customer_message_ids.clear()
                    conversation.last_outbound_at = _latest_datetime(
                        conversation.last_outbound_at,
                        event_time,
                    )
                    if (
                        had_open_handoff
                        and handoff_resolution["closed_count"] == 0
                    ) or open_handoff_active:
                        conversation.status = "waiting_sales_reply"
                    else:
                        if handoff_resolution["resume_ai"]:
                            conversation.ai_enabled = True
                        conversation.last_sales_reply_at = _latest_datetime(
                            conversation.last_sales_reply_at,
                            event_time,
                        )
                        conversation.sales_first_reply_at = (
                            conversation.sales_first_reply_at or event_time
                        )
                        conversation.status = "sales_replied_waiting_user"
                        conversation.recall_origin_status = None
                        conversation.handoff_reason_code = None
                        conversation.next_recall_at = event_time + timedelta(
                            hours=get_settings().c3_recall_after_hours
                        )
                        human_sales_observed = True
                        last_human_sales_screen_order = max(
                            last_human_sales_screen_order,
                            int(item.message_position.screen_order),
                        )
                        from app.services.c3_service import (
                            cancel_active_batches_for_conversation_change,
                        )

                        cancel_active_batches_for_conversation_change(
                            db,
                            binding.conversation_id,
                            reason="销售已人工回复，取消开场、召回和未发送 AI 回复",
                        )
        ingested_count += 1
        read_has_new_facts = True
        result = {
            "source_message_key": item.source_message_key,
            "dedupe_key": dedupe_key,
            "ingest_result": "ingested",
            "message_id": message.id,
            "message_event_id": message.id,
        }
        if source_dedupe_key and source_dedupe_key != dedupe_key:
            result["source_dedupe_key"] = source_dedupe_key
        results.append(result)

    if partition_final:
        partition_events = list(
            db.scalars(
                select(MessageEvent).where(
                    MessageEvent.conversation_id == payload.conversation_id,
                    MessageEvent.source_message_key.in_(
                        sorted(expected_source_keys)
                    ),
                )
            )
        )
        found_source_keys = {
            str(event.source_message_key or "").strip()
            for event in partition_events
            if str(event.source_message_key or "").strip()
        }
        if found_source_keys != expected_source_keys:
            raise AppError(
                "MESSAGE_INGEST_PARTITION_INCOMPLETE",
                "消息分片尚未全部入库，末尾分片稍后重试",
                425,
            )
        current_run_events = [
            event
            for event in partition_events
            if str(event.read_run_id or "") == payload.read_run_id
        ]
        read_has_new_facts = bool(current_run_events)
        sales_orders = [
            int(event.observation_order or 0)
            for event in current_run_events
            if event.sender_role in SALES_SIDE_SENDER_ROLES
        ]
        latest_sales_order = max(sales_orders, default=0)
        new_customer_message_ids = [
            event.id
            for event in current_run_events
            if event.sender_role == "customer"
            and int(event.observation_order or 0) > latest_sales_order
        ]
        new_sales_message = bool(sales_orders)
        human_sales_events = [
            event
            for event in current_run_events
            if event.sender_role in SALES_SIDE_SENDER_ROLES
            and str(
                (
                    event.raw_payload
                    if isinstance(event.raw_payload, dict)
                    else {}
                ).get("sender_source")
                or ""
            )
            == "human"
        ]
        human_sales_observed = bool(human_sales_events)
        last_human_sales_screen_order = max(
            (
                int(event.observation_order or 0)
                for event in human_sales_events
            ),
            default=0,
        )

    if not state_transition_allowed:
        db.flush()
        return {
            "ingested_count": ingested_count,
            "duplicated_count": duplicated_count,
            "ignored_count": ignored_count,
            "results": results,
            "state_transition_applied": False,
            "state_transition_reason": state_transition_reason,
            "current_read_reason": str(
                current_authorization.get("read_reason") or ""
            ),
            "next_action": NEXT_ACTION_NONE,
        }

    if (
        current_authorization_matches
        and binding.unread_hint
        and (not partitioned or partition_final)
    ):
        binding.unread_hint = False

    message_batch = None
    flow_gate_errors = list(flow_gate_error_codes)
    failed_images = [
        item
        for item in ordered_messages
        if str(item.message_type or "").strip().lower() == "image"
        and str(item.item_state or "").strip().lower() == "failed"
        and str(item.sender_role_hint or "").strip().lower()
        in {"customer", "self"}
    ]
    if failed_images and "C2_IMAGE_UNDERSTANDING_FAILED" not in flow_gate_errors:
        flow_gate_errors.append("C2_IMAGE_UNDERSTANDING_FAILED")
        failed_image_details: list[dict[str, Any]] = []
        for sender_role in ("customer", "self"):
            role_items = [
                item
                for item in failed_images
                if str(item.sender_role_hint or "").strip().lower()
                == sender_role
            ]
            if not role_items:
                continue
            detail: dict[str, Any] = {
                "error_code": "C2_IMAGE_UNDERSTANDING_FAILED",
                "position_source": "position_unavailable",
                "subject_sender_role": sender_role,
            }
            if all(
                item.message_position.order_source == "visual_top"
                for item in role_items
            ):
                orders = [
                    int(item.message_position.screen_order)
                    for item in role_items
                ]
                detail.update(
                    {
                        "min_screen_order": min(orders),
                        "max_screen_order": max(orders),
                        "position_source": "failed_image_visual_top",
                    }
                )
            failed_image_details.append(detail)
        flow_gate_details_by_code[
            "C2_IMAGE_UNDERSTANDING_FAILED"
        ] = failed_image_details
    failed_voices = [
        item
        for item in ordered_messages
        if str(item.message_type or "").strip().lower() == "voice"
        and str(item.item_state or "").strip().lower() == "failed"
        and str(item.sender_role_hint or "").strip().lower()
        in {"customer", "self"}
    ]
    if failed_voices and "C2_VOICE_TRANSCRIBE_FAILED" not in flow_gate_errors:
        flow_gate_errors.append("C2_VOICE_TRANSCRIBE_FAILED")
        failed_voice_details: list[dict] = []
        for sender_role in ("customer", "self"):
            role_items = [
                item
                for item in failed_voices
                if str(item.sender_role_hint or "").strip().lower()
                == sender_role
            ]
            if not role_items:
                continue
            detail: dict[str, Any] = {
                "error_code": "C2_VOICE_TRANSCRIBE_FAILED",
                "position_source": "position_unavailable",
                "subject_sender_role": sender_role,
            }
            if all(
                item.message_position.order_source == "visual_top"
                for item in role_items
            ):
                orders = [
                    int(item.message_position.screen_order)
                    for item in role_items
                ]
                detail.update(
                    {
                        "min_screen_order": min(orders),
                        "max_screen_order": max(orders),
                        "position_source": "failed_voice_visual_top",
                    }
                )
            failed_voice_details.append(detail)
        flow_gate_details_by_code["C2_VOICE_TRANSCRIBE_FAILED"] = (
            failed_voice_details
        )

    def gate_is_proven_before_latest_human_sales(code: str) -> bool:
        if (
            not human_sales_observed
            or new_customer_message_ids
            or last_human_sales_screen_order <= 0
        ):
            return False
        details = flow_gate_details_by_code.get(code) or []
        if not details:
            return False
        strong_position_sources = FLOW_GATE_STRONG_POSITION_SOURCES_V3
        return all(
            detail.get("position_source") in strong_position_sources
            and detail.get("max_screen_order") is not None
            and (
                int(detail["max_screen_order"]) < last_human_sales_screen_order
                or (
                    code
                    in {
                        "C2_VOICE_TRANSCRIBE_FAILED",
                        "C2_IMAGE_UNDERSTANDING_FAILED",
                    }
                    and detail.get("subject_sender_role") == "self"
                    and int(detail["max_screen_order"])
                    == last_human_sales_screen_order
                )
            )
            for detail in details
        )

    flow_gate_errors = [
        code
        for code in flow_gate_errors
        if not gate_is_proven_before_latest_human_sales(code)
    ]
    temporary_capability_gates = [
        code
        for code in flow_gate_errors
        if code in TEMPORARY_CAPABILITY_GATE_CODES_V3
    ]
    handoff_flow_gates = [
        code
        for code in flow_gate_errors
        if code not in TEMPORARY_CAPABILITY_GATE_CODES_V3
    ]
    if open_handoff_active:
        if handoff_flow_gates:
            from app.services.c3_service import (
                open_handoff_events_for_conversation,
            )

            existing_handoff = next(
                iter(
                    open_handoff_events_for_conversation(
                        db,
                        payload.conversation_id,
                        for_update=True,
                    )
                ),
                None,
            )
            existing_batch = (
                db.get(MessageBatch, existing_handoff.batch_id)
                if existing_handoff and existing_handoff.batch_id
                else None
            )
            if existing_batch:
                message_batch = {
                    "batch_id": existing_batch.id,
                    "batch_status": existing_batch.status,
                }
    elif handoff_flow_gates:
        from app.services.c3_service import create_deterministic_handoff_for_ingest

        stable_flow_gate_key = str(evidence_payload.get("flow_gate_identity_key") or "").strip()
        message_batch = create_deterministic_handoff_for_ingest(
            db,
            conversation_id=payload.conversation_id,
            message_event_ids=new_customer_message_ids,
            reason_codes=handoff_flow_gates,
            trigger_key=(
                f"identity:{stable_flow_gate_key}"
                if stable_flow_gate_key
                else f"{payload.read_run_id}:{handoff_flow_gates[0]}"
            ),
            trace_id=get_request_id(),
        )
    elif temporary_capability_gates:
        # Request-level protocol capability gates keep safe facts readable
        # without creating a customer-service handoff.
        readable_origin_statuses = {
            "friend_activation_reading",
            "waiting_user_reply",
            "recalled_waiting_user",
            "waiting_sales_reply",
            "sales_replied_waiting_user",
        }
        if conversation.status == "ai_active":
            conversation.status = (
                origin_conversation_status
                if origin_conversation_status in readable_origin_statuses
                else "waiting_user_reply"
            )
        message_batch = {
            "batch_id": None,
            "batch_status": "capability_paused",
            "reason_codes": temporary_capability_gates,
        }
    elif new_customer_message_ids:
        from app.services.c3_service import collect_customer_message_batch

        message_batch = collect_customer_message_batch(
            db,
            conversation_id=payload.conversation_id,
            message_event_ids=new_customer_message_ids,
            trace_id=get_request_id(),
        )
    elif not new_sales_message:
        read_reason = str(evidence_payload.get("read_reason") or "").strip()
        from app.services.c3_service import create_control_message_batch

        if (
            conversation.friend_state == "friend_active"
            and conversation.status == "friend_activation_reading"
            and read_reason == "friend_acceptance_visible_hit"
        ):
            has_human_messages = db.scalar(
                select(MessageEvent.id)
                .where(
                    MessageEvent.conversation_id == payload.conversation_id,
                    MessageEvent.sender_role.in_(["customer", "self"]),
                )
                .limit(1)
            )
            conversation.status = "ai_active"
            if not has_human_messages:
                message_batch = create_control_message_batch(
                    db,
                    conversation_id=payload.conversation_id,
                    trigger_type="friend_welcome",
                    trigger_key="friend_welcome",
                    trace_id=get_request_id(),
                )
        elif conversation.status == "recall_precheck" and read_reason == "recall_precheck" and conversation.recall_cycle_id:
            cycle_id = conversation.recall_cycle_id
            message_batch = create_control_message_batch(
                db,
                conversation_id=payload.conversation_id,
                trigger_type="recall",
                trigger_key=cycle_id,
                recall_cycle_id=cycle_id,
                trace_id=get_request_id(),
            )
            local_today = utcnow().astimezone(ZoneInfo("Asia/Shanghai")).date()
            if conversation.recall_daily_date != local_today:
                conversation.recall_daily_date = local_today
                conversation.recall_daily_count = 0
            conversation.recall_count += 1
            conversation.recall_daily_count += 1
            conversation.status = "ai_active"
            conversation.next_recall_at = None
    read_completion = None
    if not partitioned or partition_final:
        read_completion = _settle_completed_read(
            binding,
            conversation,
            read_run_id=payload.read_run_id,
            has_new_facts=read_has_new_facts,
        )
    db.flush()
    response = {
        "ingested_count": ingested_count,
        "duplicated_count": duplicated_count,
        "ignored_count": ignored_count,
        "results": results,
        "state_transition_applied": True,
        "state_transition_reason": state_transition_reason,
        "next_action": NEXT_ACTION_NONE,
    }
    if read_completion is not None:
        response["read_completion"] = read_completion
    if partitioned:
        response["ingest_partition"] = {
            "group_id": payload.read_run_id,
            "index": partition_index,
            "count": partition_count,
            "complete": partition_final,
        }
    if message_batch:
        batch_row = db.get(MessageBatch, str(message_batch["batch_id"]))
        continuation = None
        if batch_row and batch_row.active and batch_row.status in {
            "collecting",
            "generating",
            "retry_wait",
        }:
            from app.services.c3_service import bind_message_batch_continuation

            continuation = bind_message_batch_continuation(
                db,
                batch_id=batch_row.id,
                binding=binding,
                authorization_revision=_authorization_revision(binding),
                read_reason=incoming_read_reason,
                origin_conversation_status=origin_conversation_status,
            )
        response["message_batch"] = {
            "batch_id": message_batch["batch_id"],
            "batch_status": message_batch["batch_status"],
        }
        if continuation:
            response["message_batch"]["continuation"] = continuation
    return response


def record_ingest_technical_terminal(
    db: Session,
    *,
    worker: Worker,
    payload: WechatMessageIngestRequest,
    error_code: str,
) -> dict[str, Any]:
    """Persist the backend-owned terminal for an unrecoverable C2 identity fault."""

    binding = db.scalar(
        select(WechatSessionBinding).where(
            WechatSessionBinding.conversation_id == payload.conversation_id,
            WechatSessionBinding.worker_id == worker.id,
            WechatSessionBinding.deleted_at.is_(None),
        )
    )
    if not binding:
        return {"terminal_confirmed": False}
    conversation = _upsert_conversation_for_binding(db, binding)
    from app.services.c3_service import create_deterministic_handoff_for_ingest

    result = create_deterministic_handoff_for_ingest(
        db,
        conversation_id=conversation.conversation_id,
        message_event_ids=[],
        reason_codes=[str(error_code)],
        trigger_key=(
            f"c2-technical-terminal:{payload.read_run_id}:{error_code}"
        ),
        trace_id=get_request_id(),
    )
    return {
        "terminal_confirmed": True,
        "conversation_id": conversation.conversation_id,
        "message_batch": result,
    }


def restore_binding(
    db: Session,
    *,
    conversation_id: str,
    reason: str,
    actor: ActorContext,
) -> dict:
    binding = db.scalar(
        select(WechatSessionBinding)
        .where(WechatSessionBinding.conversation_id == conversation_id)
        .with_for_update()
    )
    if not binding:
        raise AppError("WECHAT_BINDING_NOT_FOUND", "微信会话绑定不存在", 404)
    if binding.deleted_at is not None or binding.replacement_binding_id:
        raise AppError(
            "WECHAT_BINDING_RESTORE_HISTORY_FORBIDDEN",
            "历史绑定或已被替代绑定不能恢复",
            409,
        )
    conversation = db.get(Conversation, conversation_id)
    if not _conversation_allows_binding_recovery(conversation):
        raise AppError(
            "WECHAT_BINDING_RESTORE_CONVERSATION_TERMINATED",
            "已拒绝、关闭或停用自动化的会话不能恢复监听",
            409,
        )
    disable_reason = str(binding.disable_reason or "").strip()
    if disable_reason in PERMANENT_BINDING_DISABLE_REASONS:
        raise AppError(
            "WECHAT_BINDING_RESTORE_PERMANENTLY_DISABLED",
            "永久停用的微信绑定不能恢复",
            409,
            {"disable_reason": disable_reason},
        )
    if (
        conversation.worker_id
        and conversation.worker_id != binding.worker_id
    ):
        raise AppError(
            "WECHAT_BINDING_RESTORE_WORKER_MISMATCH",
            "会话与微信绑定不属于同一 Worker",
            409,
        )
    sales = db.get(Sales, binding.sales_id) if binding.sales_id else None
    if sales and sales.worker_id and sales.worker_id != binding.worker_id:
        raise AppError(
            "WECHAT_BINDING_RESTORE_WORKER_MISMATCH",
            "销售当前 Worker 与微信绑定不一致",
            409,
        )
    remark_code = _clean_locator(binding.remark_code)
    if not remark_code:
        raise AppError(
            "WECHAT_BINDING_RESTORE_REMARK_CODE_MISSING",
            "缺少客户短码的绑定不能恢复",
            409,
        )
    conflicting_binding = db.scalar(
        select(WechatSessionBinding.id).where(
            WechatSessionBinding.id != binding.id,
            WechatSessionBinding.remark_code == remark_code,
            WechatSessionBinding.deleted_at.is_(None),
            WechatSessionBinding.replacement_binding_id.is_(None),
        )
    )
    conflicting_lead_binding = db.scalar(
        select(WechatSessionBinding.id).where(
            WechatSessionBinding.id != binding.id,
            WechatSessionBinding.lead_id == binding.lead_id,
            WechatSessionBinding.bind_status == BIND_STATUS_BOUND,
            WechatSessionBinding.deleted_at.is_(None),
        )
    )
    if conflicting_binding or conflicting_lead_binding:
        raise AppError(
            "WECHAT_BINDING_RESTORE_CONFLICT",
            "短码或线索存在其他当前绑定，不能恢复",
            409,
        )
    recoverable_state = bool(
        binding.bind_status == BIND_STATUS_BOUND
        and binding.listen_status == LISTEN_STATUS_PAUSED
    ) or _legacy_disabled_pause_is_recoverable(db, binding)
    if not recoverable_state:
        raise AppError(
            "WECHAT_BINDING_RESTORE_STATE_INVALID",
            "只有临时暂停或可确认的历史错误停用绑定可以恢复",
            409,
        )

    before = {
        "bind_status": binding.bind_status,
        "listen_status": binding.listen_status,
        "allow_listening": binding.allow_listening,
        "authorization_revision": int(binding.authorization_revision or 1),
        "disable_reason": binding.disable_reason,
    }
    previous_revision = int(binding.authorization_revision or 1)
    _set_binding_state(
        binding,
        status=BIND_STATUS_BOUND,
        listen_status=LISTEN_STATUS_PAUSED,
        allow_listening=False,
        error_code="SESSION_BINDING_RESTORE_PENDING_SCAN",
        remark_code=remark_code,
        preserve_lead=True,
    )
    if int(binding.authorization_revision or 1) <= previous_revision:
        binding.authorization_revision = previous_revision + 1
    binding.disable_reason = None
    binding.disabled_at = None
    binding.disabled_by = None
    binding.replacement_binding_id = None
    _reset_read_backoff(binding)
    after = {
        "bind_status": binding.bind_status,
        "listen_status": binding.listen_status,
        "allow_listening": binding.allow_listening,
        "authorization_revision": int(binding.authorization_revision),
        "disable_reason": binding.disable_reason,
    }
    from app.services.audit_service import write_log

    write_log(
        db,
        actor,
        event_type="wechat_binding_restored",
        module="wechat",
        target_type="wechat_session_binding",
        target_id=binding.id,
        lead_id=binding.lead_id,
        before_data=before,
        after_data=after,
        metadata={"reason": reason.strip(), "conversation_id": conversation_id},
    )
    db.flush()
    return {
        **_binding_to_dict(binding),
        "recovery_state": "paused_waiting_worker",
        "restore_reason": reason.strip(),
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
    query = (
        select(MessageEvent)
        .where(MessageEvent.conversation_id == conversation_id)
        .order_by(
            func.coalesce(
                MessageEvent.observed_at,
                MessageEvent.occurred_at,
                MessageEvent.ingested_at,
            ).desc(),
            MessageEvent.observation_order.desc(),
            MessageEvent.id.desc(),
        )
    )
    items = list(db.scalars(query.offset((page - 1) * page_size).limit(page_size)))
    return {"items": [_message_to_dict(item) for item in items], "page": page, "page_size": page_size}
