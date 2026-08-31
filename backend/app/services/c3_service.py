from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import logging
import secrets
import time
import uuid
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.enums import TaskEventType, TaskResultCode, TaskStatus, TaskType
from app.errors import AppError
from app.models.base import utcnow
from app.models.c3 import (
    Conversation,
    HandoffEvent,
    MessageBatch,
    ReplyAction,
    ReplyActionVehicleFact,
    SentAck,
)
from app.models.task import Task
from app.models.vehicle import KnowledgeItem, VehicleImage
from app.models.wechat import MessageEvent, WechatSessionBinding
from app.models.worker import Worker
from app.services.ai_adapter import AIEngineDecision, get_ai_engine_adapter
from app.services.feishu_service import enqueue_handoff_notification
from app.services.message_contract import (
    canonical_message_identity_text,
    canonical_reply_text,
    reply_text_hash,
)
from app.services.task_service import _write_event, finish_task_and_release_worker, get_task_or_404, task_to_detail


ACTIVE_BATCH_STATUSES = {"collecting", "generating", "retry_wait"}
OPEN_ACTION_STATUSES = {"draft", "guarding", "queued", "sending"}
SUPERSEDABLE_ACTION_STATUSES = {"draft", "guarding", "queued"}
TERMINAL_ACTION_STATUSES = {
    "sent",
    "failed",
    "unknown_send_result",
    "superseded",
    "expired",
    "cancelled",
    "handoff",
    "blocked",
}
UNKNOWN_SEND_TERMINAL_REMARK = (
    "发送结果未知，原动作已终结且禁止补发；"
    "会话已转销售正常接管。"
)
FAILED_SEND_TERMINAL_REMARK = (
    "自动发送已终止，会话已转销售正常接管，禁止自动补发。"
)
VEHICLE_FACT_STALE_CODE = "REPLY_ACTION_VEHICLE_FACT_STALE"
VEHICLE_FACT_STALE_REASON = "车辆资料或上下架状态已变化，旧回复动作作废"
logger = logging.getLogger(__name__)


PRE_SEND_FACT_CHECKPOINT_REVISION = 5
PRE_SEND_CHECKPOINT_FRAME_SOURCES = {"initial_read", "final_read"}
PRE_SEND_TERMINAL_ACTION_COMMIT_BASES = {
    "confirmed_voice_action",
    "confirmed_image_action",
}
TECHNICAL_SEND_FAILURE_NO_HANDOFF_CODES = {
    "C2_PRE_SEND_FACT_CHECKPOINT_INVALID",
    "C3_SEND_CONTEXT_GUARD_REQUIRED",
    "C3_SEND_CONTEXT_GUARD_INVALID",
}


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()


def _is_sha256(value: object) -> bool:
    text = str(value or "").strip().lower()
    return len(text) == 64 and all(
        character in "0123456789abcdef" for character in text
    )


def _message_worker_stable_id(message: MessageEvent) -> str:
    raw = message.raw_payload if isinstance(message.raw_payload, dict) else {}
    basis = raw.get("dedupe_basis") if isinstance(raw.get("dedupe_basis"), dict) else {}
    return str(
        basis.get("worker_stable_id")
        or raw.get("worker_stable_id")
        or ""
    ).strip()


def _message_item_state(message: MessageEvent) -> str:
    raw = message.raw_payload if isinstance(message.raw_payload, dict) else {}
    observation = raw.get("observation") if isinstance(raw.get("observation"), dict) else {}
    state = str(
        raw.get("item_state")
        or observation.get("item_state")
        or "completed"
    ).strip().lower()
    return state if state in {"completed", "failed"} else "completed"


def _message_image_content_sha256(message: MessageEvent) -> str:
    raw = message.raw_payload if isinstance(message.raw_payload, dict) else {}
    observation = raw.get("observation") if isinstance(raw.get("observation"), dict) else {}
    action_summary = (
        observation.get("_worker_image_action_summary")
        if isinstance(observation.get("_worker_image_action_summary"), dict)
        else {}
    )
    committed = (
        observation.get("_worker_committed_message")
        if isinstance(observation.get("_worker_committed_message"), dict)
        else {}
    )
    proof = committed.get("proof") if isinstance(committed.get("proof"), dict) else {}
    return str(
        proof.get("image_sha256")
        or action_summary.get("image_sha256")
        or raw.get("image_sha256")
        or ""
    ).strip().lower()


def _observation_for_message(message: MessageEvent) -> dict[str, Any]:
    raw = message.raw_payload if isinstance(message.raw_payload, dict) else {}
    observation = (
        raw.get("observation")
        if isinstance(raw.get("observation"), dict)
        else {}
    )
    return dict(observation)


def _normalize_voice_duration(value: object) -> str:
    text = str(value or "").strip().lower()
    for suffix in ("seconds", "second", "secs", "sec", "秒", "s"):
        if text.endswith(suffix):
            text = text[: -len(suffix)].strip()
            break
    try:
        number = float(text)
    except (TypeError, ValueError):
        return ""
    if number <= 0:
        return ""
    return (
        str(int(number))
        if number.is_integer()
        else format(number, ".3f").rstrip("0").rstrip(".")
    )


def _observation_voice_duration(observation: dict[str, Any]) -> str:
    source = (
        observation.get("source_message")
        if isinstance(observation.get("source_message"), dict)
        else {}
    )
    return _normalize_voice_duration(
        observation.get("voice_duration")
        or source.get("voice_duration")
        or observation.get("voice_duration_text")
        or source.get("voice_duration_text")
        or ""
    )


def _exact_image_content_sha256(value: object) -> str:
    text = str(value or "").strip().lower()
    if text.startswith("imagev2:"):
        parts = text.split(":", 2)
        digest = parts[2] if len(parts) == 3 else ""
    elif text.startswith("sha256:"):
        digest = text.split(":", 1)[1]
    else:
        digest = ""
    if len(digest) != 64 or any(
        character not in "0123456789abcdef" for character in digest
    ):
        return ""
    return digest


def _reply_fact_evidence(
    observation: dict[str, Any],
    *,
    item_state: str,
    image_content_sha256: str = "",
) -> dict[str, str]:
    message_type = str(
        observation.get("message_type") or ""
    ).strip().lower()
    role = str(observation.get("sender_role") or "").strip().lower()
    state = str(item_state or "").strip().lower()
    evidence: dict[str, str] = {
        "sender_role": role,
        "message_type": message_type,
        "item_state": state,
    }
    if message_type == "voice" and state == "completed":
        content = observation.get("content_clean")
        if content is None:
            content = observation.get("content")
        normalized = canonical_message_identity_text(content)
        evidence["normalized_transcript_sha256"] = (
            hashlib.sha256(normalized.encode("utf-8")).hexdigest()
            if normalized
            else ""
        )
        evidence["voice_duration"] = _observation_voice_duration(
            observation
        )
    elif message_type == "image" and state == "completed":
        # The persisted clipboard-bytes digest is the formal image result.
        # Runtime bubble summaries are deliberately stripped before ingest
        # and must never be reconstructed into cross-frame identity here.
        evidence["exact_image_content_sha256"] = str(
            image_content_sha256 or ""
        ).strip().lower()
    return evidence


def _validated_message_commit_evidence(
    message: MessageEvent,
    observation: dict[str, Any],
    *,
    item_state: str,
) -> dict[str, Any]:
    raw = message.raw_payload if isinstance(message.raw_payload, dict) else {}
    evidence = (
        raw.get("message_commit_evidence")
        if isinstance(raw.get("message_commit_evidence"), dict)
        else {}
    )
    action_receipt = (
        evidence.get("action_receipt")
        if isinstance(evidence.get("action_receipt"), dict)
        else {}
    )
    reply_fact = (
        evidence.get("reply_fact_evidence")
        if isinstance(evidence.get("reply_fact_evidence"), dict)
        else {}
    )
    commit_basis = str(evidence.get("commit_basis") or "").strip()
    action_digest = str(
        evidence.get("action_receipt_digest") or ""
    ).strip().lower()
    reply_digest = str(
        evidence.get("reply_fact_digest") or ""
    ).strip().lower()
    stable_id = _message_worker_stable_id(message)
    observation_id = str(
        observation.get("observation_id") or ""
    ).strip()
    message_type = str(message.message_type or "").strip().lower()
    if (
        int(evidence.get("schema_version") or 0) != 1
        or commit_basis not in PRE_SEND_TERMINAL_ACTION_COMMIT_BASES
        or commit_basis != f"confirmed_{message_type}_action"
        or not action_receipt
        or action_receipt.get("binding_confirmed") is not True
        or str(
            action_receipt.get("reserved_worker_stable_id") or ""
        ).strip()
        != stable_id
        or str(action_receipt.get("post_observation_id") or "").strip()
        != observation_id
        or not str(
            action_receipt.get("canonical_action_id") or ""
        ).strip()
        or not str(action_receipt.get("pre_observation_id") or "").strip()
        or not _is_sha256(action_digest)
        or action_digest != _canonical_sha256(action_receipt)
        or not reply_fact
        or reply_fact != _reply_fact_evidence(
            observation,
            item_state=item_state,
            image_content_sha256=_message_image_content_sha256(message),
        )
        or not _is_sha256(reply_digest)
        or reply_digest != _canonical_sha256(reply_fact)
    ):
        return {}
    if commit_basis == "confirmed_voice_action" and not _is_sha256(
        action_receipt.get("selected_action_token_sha256")
    ):
        return {}
    if commit_basis == "confirmed_image_action":
        receipt_image_sha256 = str(
            action_receipt.get("image_sha256") or ""
        ).strip().lower()
        if (
            not _is_sha256(receipt_image_sha256)
            or receipt_image_sha256
            != _message_image_content_sha256(message)
        ):
            return {}
    return {
        "commit_basis": commit_basis,
        "action_receipt_digest": action_digest,
        "reply_fact_evidence": dict(reply_fact),
    }


def _stable_fact_signature(
    *,
    sender_role: object,
    message_type: object,
    item_state: object,
    content: object = "",
    voice_duration: object = "",
    image_content_sha256: object = "",
    error_code: object = "",
) -> str:
    normalized_type = str(message_type or "").strip().lower()
    normalized_state = str(item_state or "").strip().lower()
    material: dict[str, str] = {
        "sender_role": str(sender_role or "").strip().lower(),
        "message_type": normalized_type,
        "item_state": normalized_state,
    }
    if normalized_type != "image":
        material["normalized_content_hash"] = hashlib.sha256(
            canonical_message_identity_text(content).encode("utf-8")
        ).hexdigest()
        if normalized_type == "voice":
            material["voice_duration"] = _normalize_voice_duration(
                voice_duration
            )
    else:
        # Preserve and validate the formal image action receipt separately;
        # its bubble/ROI digest cannot veto a later send frame.
        _ = image_content_sha256
    if normalized_state == "failed":
        material["error_code"] = str(error_code or "").strip()
    return _canonical_sha256(material)


def _build_pre_send_fact_checkpoint(
    *,
    batch: MessageBatch,
    ordered_messages: list[MessageEvent],
    baseline_kind: str = "message_tail",
    authoritative_frame_source: str = "",
    tail_complete: bool | None = None,
) -> dict[str, Any]:
    fact_items: list[dict[str, Any]] = []
    complete = True
    for message in ordered_messages:
        message_type = str(message.message_type or "").strip().lower()
        if message_type not in {"text", "voice", "image", "system"}:
            continue
        raw = message.raw_payload if isinstance(message.raw_payload, dict) else {}
        business_projection = (
            dict(raw.get("business_projection"))
            if isinstance(raw.get("business_projection"), dict)
            else {}
        )
        strong_boundary_tokens = [
            str(token or "").strip()
            for token in (raw.get("strong_boundary_tokens") or [])
            if str(token or "").strip()
        ]
        strong_boundary_anchor = (
            dict(raw.get("strong_boundary_anchor"))
            if isinstance(raw.get("strong_boundary_anchor"), dict)
            else {}
        )
        message_identity_commit_record = (
            dict(raw.get("message_identity_commit_record") or {})
            if isinstance(
                raw.get("message_identity_commit_record"), dict
            )
            else {}
        )
        message_identity_runtime_evidence = (
            {
                str(key): dict(value)
                for key, value in (
                    raw.get("message_identity_runtime_evidence") or {}
                ).items()
                if isinstance(value, dict)
            }
            if isinstance(
                raw.get("message_identity_runtime_evidence"), dict
            )
            else {}
        )
        stable_id = _message_worker_stable_id(message)
        item_state = _message_item_state(message)
        image_fingerprint = (
            _message_image_content_sha256(message)
            if message_type == "image"
            else ""
        )
        if (
            not stable_id
            or (message_type == "image" and not image_fingerprint)
            or set(business_projection)
            != {
                "screen_order",
                "sender_role",
                "message_type",
                "normalized_content_signature",
                "media_state",
            }
            or not message_identity_commit_record
        ):
            complete = False
        observation = _observation_for_message(message)
        reply_fact_evidence = _reply_fact_evidence(
            observation,
            item_state=item_state,
            image_content_sha256=image_fingerprint,
        )
        commit_basis = str(
            message_identity_commit_record.get("commit_basis") or ""
        ).strip()
        action_receipt_digest = ""
        if message_type in {"voice", "image"}:
            commit_evidence = _validated_message_commit_evidence(
                message,
                observation,
                item_state=item_state,
            )
            if (
                not commit_evidence
                or str(commit_evidence.get("commit_basis") or "").strip()
                != commit_basis
            ):
                complete = False
            action_receipt_digest = str(
                commit_evidence.get("action_receipt_digest") or ""
            ).strip()
        fact_items.append(
            {
                "worker_stable_id": stable_id,
                "source_message_key": str(
                    raw.get("source_message_key") or ""
                ).strip(),
                "sender_role": str(message.sender_role or "").strip().lower(),
                "message_type": message_type,
                "item_state": item_state,
                "stable_fact_signature": _stable_fact_signature(
                    sender_role=message.sender_role,
                    message_type=message_type,
                    item_state=item_state,
                    content=message.content,
                    voice_duration=_observation_voice_duration(
                        observation
                    ),
                    image_content_sha256=image_fingerprint,
                    error_code=(
                        raw.get("error_code")
                        or message.error_code
                        or ""
                    ),
                ),
                "commit_basis": commit_basis,
                "action_receipt_digest": action_receipt_digest,
                "reply_fact_evidence": reply_fact_evidence,
                "business_projection": business_projection,
                "strong_boundary_tokens": strong_boundary_tokens,
                "strong_boundary_anchor": strong_boundary_anchor,
                "message_identity_commit_record": (
                    message_identity_commit_record
                ),
                "message_identity_runtime_evidence": (
                    message_identity_runtime_evidence
                ),
            }
        )
    committed_tail: list[dict[str, Any]] = []
    for item in fact_items:
        committed_tail.append(
            {
                str(key): (
                    dict(value)
                    if key in {
                        "reply_fact_evidence",
                        "business_projection",
                        "strong_boundary_anchor",
                        "message_identity_commit_record",
                        "message_identity_runtime_evidence",
                    }
                    and isinstance(value, dict)
                    else list(value)
                    if key == "strong_boundary_tokens"
                    and isinstance(value, list)
                    else str(value)
                )
                for key, value in item.items()
            }
        )
    normalized_baseline_kind = str(baseline_kind or "").strip()
    is_empty_welcome = normalized_baseline_kind == "friend_welcome_empty"
    computed_complete = bool(
        complete
        and (
            is_empty_welcome
            or (
                committed_tail
                and authoritative_frame_source
                in PRE_SEND_CHECKPOINT_FRAME_SOURCES
            )
        )
    )
    return {
        "checkpoint_revision": PRE_SEND_FACT_CHECKPOINT_REVISION,
        "conversation_id": batch.conversation_id,
        "batch_id": batch.id,
        "baseline_kind": normalized_baseline_kind,
        "authoritative_frame_source": str(
            authoritative_frame_source or ""
        ).strip(),
        "committed_tail": committed_tail,
        "tail_complete": (
            computed_complete
            if tail_complete is None
            else bool(tail_complete and computed_complete)
        ),
    }


def _checkpoint_tail_from_latest_complete_frame(
    ordered_messages: list[MessageEvent],
) -> list[MessageEvent]:
    """Project the latest complete Worker frame onto Brain-visible facts.

    A conversation may have far more history than one WeChat viewport.  The
    checkpoint therefore uses only the latest authoritative full-frame tail,
    while every item still has to resolve to a formal MessageEvent that was
    actually included in this Brain request.
    """

    if not ordered_messages:
        return []

    by_stable_id = {
        _message_worker_stable_id(message): message
        for message in ordered_messages
        if _message_worker_stable_id(message)
    }
    by_source_message_key: dict[str, MessageEvent] = {}
    duplicate_source_keys: set[str] = set()
    for message in ordered_messages:
        raw = message.raw_payload if isinstance(message.raw_payload, dict) else {}
        source_key = str(
            getattr(message, "source_message_key", None)
            or raw.get("source_message_key")
            or ""
        ).strip()
        if not source_key:
            continue
        if source_key in by_source_message_key:
            duplicate_source_keys.add(source_key)
            continue
        by_source_message_key[source_key] = message
    for source_key in duplicate_source_keys:
        by_source_message_key.pop(source_key, None)
    source_message = ordered_messages[-1]
    evidence = (
        source_message.evidence
        if isinstance(source_message.evidence, dict)
        else {}
    )
    observations = evidence.get("observations")
    slot_states = evidence.get("slot_ledger_states")
    if (
        str(evidence.get("authoritative_frame_source") or "").strip()
        not in PRE_SEND_CHECKPOINT_FRAME_SOURCES
        or not isinstance(observations, list)
        or not observations
        or bool(evidence.get("observation_validation_errors"))
    ):
        return []
    source_key_by_observation_id: dict[str, str] = {}
    if isinstance(slot_states, list):
        for slot in slot_states:
            if not isinstance(slot, dict):
                continue
            observation_id = str(slot.get("observation_id") or "").strip()
            source_key = str(slot.get("source_message_key") or "").strip()
            if not observation_id or not source_key:
                continue
            previous = source_key_by_observation_id.get(observation_id)
            if previous is not None and previous != source_key:
                return []
            source_key_by_observation_id[observation_id] = source_key
    projected: list[MessageEvent] = []
    seen_event_ids: set[str] = set()
    for observation in observations:
        if not isinstance(observation, dict):
            continue
        message_type = str(
            observation.get("message_type") or ""
        ).strip().lower()
        row_kind = str(
            observation.get("row_kind") or ""
        ).strip().lower()
        if row_kind in {"call_event", "system_banner"}:
            continue
        if message_type not in {"text", "voice", "image", "system"}:
            if row_kind in {
                "text_bubble",
                "voice_bubble",
                "voice_transcript",
                "image_bubble",
                "system_message",
            }:
                return []
            continue
        committed = (
            observation.get("_worker_committed_message")
            if isinstance(
                observation.get("_worker_committed_message"), dict
            )
            else {}
        )
        stable_id = str(
            observation.get("_worker_stable_id")
            or committed.get("worker_stable_id")
            or ""
        ).strip()
        matched_by_stable_id = by_stable_id.get(stable_id)
        observation_id = str(
            observation.get("observation_id") or ""
        ).strip()
        source_key = source_key_by_observation_id.get(observation_id, "")
        matched_by_source_key = by_source_message_key.get(source_key)
        if (
            matched_by_stable_id is not None
            and matched_by_source_key is not None
            and matched_by_stable_id is not matched_by_source_key
        ):
            return []
        matched = matched_by_stable_id or matched_by_source_key
        matched_id = str(matched.id) if matched is not None else ""
        if (
            matched is None
            or not matched_id
            or matched_id in seen_event_ids
            or str(matched.sender_role or "").strip().lower()
            != str(observation.get("sender_role") or "").strip().lower()
            or str(matched.message_type or "").strip().lower()
            != message_type
        ):
            return []
        seen_event_ids.add(matched_id)
        projected.append(matched)
    if projected and source_message in projected:
        return projected
    return []


def _checkpoint_frame_source(
    ordered_messages: list[MessageEvent],
) -> str:
    if not ordered_messages:
        return ""
    evidence = (
        ordered_messages[-1].evidence
        if isinstance(ordered_messages[-1].evidence, dict)
        else {}
    )
    source = str(
        evidence.get("authoritative_frame_source") or ""
    ).strip()
    return source if source in PRE_SEND_CHECKPOINT_FRAME_SOURCES else ""


def _pre_send_fact_checkpoint_response(
    batch: MessageBatch,
    action: ReplyAction | None,
) -> dict[str, Any]:
    snapshot = (
        batch.ai_request_snapshot
        if isinstance(batch.ai_request_snapshot, dict)
        else {}
    )
    checkpoint = snapshot.get("pre_send_fact_checkpoint")
    if not isinstance(checkpoint, dict) or not checkpoint:
        return {}
    digest = _canonical_sha256(checkpoint)
    return {
        "pre_send_fact_checkpoint": dict(checkpoint),
        "pre_send_fact_checkpoint_binding": {
            "conversation_id": batch.conversation_id,
            "batch_id": batch.id,
            "reply_action_id": action.id if action is not None else "",
            "checkpoint_digest": digest,
        },
    }


def _brain_plan_vehicle_ids(payload: dict[str, Any]) -> list[str]:
    raw_payload = payload.get("raw_payload") if isinstance(payload.get("raw_payload"), dict) else {}
    result = raw_payload.get("omniauto_brain_result") if isinstance(raw_payload.get("omniauto_brain_result"), dict) else {}
    plan = result.get("brain_plan") if isinstance(result.get("brain_plan"), dict) else {}
    evidence = plan.get("evidence_used") if isinstance(plan.get("evidence_used"), dict) else {}
    values = evidence.get("product_ids") if isinstance(evidence.get("product_ids"), list) else []
    vehicle_ids = {str(value).strip() for value in values if str(value).strip()}
    for fact in plan.get("facts_claimed") or []:
        if not isinstance(fact, dict) or str(fact.get("source_level") or "") != "product_master":
            continue
        source_id = str(fact.get("source_id") or "").strip()
        if source_id and source_id != "multiple":
            vehicle_ids.add(source_id)
    return sorted(vehicle_ids)


def _vehicle_fact_query(vehicle_ids: list[str]):
    settings = get_settings()
    return select(KnowledgeItem).where(
        KnowledgeItem.tenant_id == settings.omniauto_knowledge_tenant,
        KnowledgeItem.layer == "product_master",
        KnowledgeItem.category_id == "products",
        KnowledgeItem.product_id == "",
        KnowledgeItem.item_id.in_(vehicle_ids),
    )


def _vehicle_fact_fingerprint(db: Session, vehicle: KnowledgeItem) -> str:
    images = list(
        db.scalars(
            select(VehicleImage)
            .where(
                VehicleImage.tenant_id == vehicle.tenant_id,
                VehicleImage.vehicle_id == vehicle.item_id,
            )
            .order_by(VehicleImage.sort_order, VehicleImage.id)
        )
    )
    value = {
        "status": vehicle.status,
        "payload": vehicle.payload or {},
        "images": [
            {"sha256": image.sha256, "sort_order": image.sort_order}
            for image in images
        ],
    }
    serialized = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _lock_vehicle_facts_for_action(db: Session, reply_action_id: str) -> list[KnowledgeItem]:
    vehicle_ids = list(
        db.scalars(
            select(ReplyActionVehicleFact.vehicle_id)
            .where(ReplyActionVehicleFact.reply_action_id == reply_action_id)
            .order_by(ReplyActionVehicleFact.vehicle_id)
        )
    )
    if not vehicle_ids:
        legacy_payload = db.scalar(
            select(ReplyAction.ai_payload).where(
                ReplyAction.id == reply_action_id,
                ReplyAction.deleted_at.is_(None),
            )
        )
        if isinstance(legacy_payload, dict):
            vehicle_ids = _brain_plan_vehicle_ids(legacy_payload)
    if not vehicle_ids:
        return []
    return list(db.scalars(_vehicle_fact_query(vehicle_ids).order_by(KnowledgeItem.item_id).with_for_update()))


def _snapshot_action_vehicle_facts(
    db: Session,
    *,
    action: ReplyAction,
    payload: dict[str, Any],
) -> list[str]:
    vehicle_ids = _brain_plan_vehicle_ids(payload)
    if not vehicle_ids:
        return []
    vehicles = list(db.scalars(_vehicle_fact_query(vehicle_ids).order_by(KnowledgeItem.item_id).with_for_update()))
    by_id = {vehicle.item_id: vehicle for vehicle in vehicles}
    unavailable = [vehicle_id for vehicle_id in vehicle_ids if vehicle_id not in by_id or by_id[vehicle_id].status != "active"]
    if unavailable:
        raise AppError(
            VEHICLE_FACT_STALE_CODE,
            "Brain 引用的车辆已不存在或已下架",
            409,
            {"vehicle_ids": unavailable, "suggested_action": "regenerate"},
        )
    for vehicle_id in vehicle_ids:
        vehicle = by_id[vehicle_id]
        db.add(
            ReplyActionVehicleFact(
                reply_action_id=action.id,
                vehicle_id=vehicle_id,
                fact_fingerprint=_vehicle_fact_fingerprint(db, vehicle),
                vehicle_updated_at=vehicle.updated_at,
            )
        )
    db.flush()
    return vehicle_ids


def _stale_action_vehicle_ids(
    db: Session,
    action: ReplyAction,
    *,
    locked_vehicles: list[KnowledgeItem] | None = None,
) -> list[str]:
    snapshots = list(
        db.scalars(
            select(ReplyActionVehicleFact)
            .where(ReplyActionVehicleFact.reply_action_id == action.id)
            .order_by(ReplyActionVehicleFact.vehicle_id)
        )
    )
    if not snapshots:
        return _brain_plan_vehicle_ids(action.ai_payload or {})
    vehicles = locked_vehicles if locked_vehicles is not None else list(
        db.scalars(_vehicle_fact_query([item.vehicle_id for item in snapshots]).order_by(KnowledgeItem.item_id))
    )
    by_id = {vehicle.item_id: vehicle for vehicle in vehicles}
    return [
        snapshot.vehicle_id
        for snapshot in snapshots
        if snapshot.vehicle_id not in by_id
        or by_id[snapshot.vehicle_id].status != "active"
        or _vehicle_fact_fingerprint(db, by_id[snapshot.vehicle_id]) != snapshot.fact_fingerprint
    ]


def _batch_continuation_token(
    batch: MessageBatch,
    binding: WechatSessionBinding,
) -> str:
    if not (
        batch.continuation_authorization_revision
        and batch.continuation_read_reason
    ):
        return ""
    seed = "|".join(
        [
            "c3-batch-continuation-v1",
            batch.id,
            batch.conversation_id,
            binding.worker_id,
            batch.continuation_authorization_revision,
            batch.continuation_read_reason,
        ]
    ).encode("utf-8")
    secret = get_settings().contact_encryption_secret.encode("utf-8")
    return hmac.new(secret, seed, hashlib.sha256).hexdigest()


def bind_message_batch_continuation(
    db: Session,
    *,
    batch_id: str,
    binding: WechatSessionBinding,
    authorization_revision: str,
    read_reason: str,
    origin_conversation_status: str,
) -> dict[str, Any]:
    """Freeze the one continuation scope that may advance this C2-C3 flow."""

    batch = db.get(MessageBatch, batch_id)
    if not batch or batch.deleted_at:
        raise AppError("MESSAGE_BATCH_NOT_FOUND", "消息批次不存在", 404)
    if batch.conversation_id != binding.conversation_id:
        raise AppError("MESSAGE_BATCH_CONVERSATION_MISMATCH", "批次与会话不一致", 409)
    if not batch.continuation_authorization_revision:
        batch.continuation_authorization_revision = str(authorization_revision)
        batch.continuation_read_reason = str(read_reason)
        batch.origin_conversation_status = str(origin_conversation_status or "")
    elif (
        batch.continuation_authorization_revision != str(authorization_revision)
        or batch.continuation_read_reason != str(read_reason)
    ):
        raise AppError(
            "MESSAGE_BATCH_CONTINUATION_CONFLICT",
            "消息批次已绑定其他授权流程",
            409,
        )
    db.flush()
    return {
        "batch_id": batch.id,
        "token": _batch_continuation_token(batch, binding),
        "authorization_revision": batch.continuation_authorization_revision,
        "read_reason": batch.continuation_read_reason,
    }


def message_batch_continuation_authorization(
    db: Session,
    *,
    worker: Worker,
    batch: MessageBatch,
    binding: WechatSessionBinding,
    presented_token: str | None = None,
) -> dict[str, Any]:
    """Return a batch-scoped ticket; never widen global read-target admission."""

    from app.services.wechat_service import _authorization_revision

    action = db.scalar(
        select(ReplyAction).where(
            ReplyAction.batch_id == batch.id,
            ReplyAction.current.is_(True),
            ReplyAction.deleted_at.is_(None),
        )
    )
    task = (
        db.scalar(
            select(Task).where(
                Task.reply_action_id == action.id,
                Task.deleted_at.is_(None),
            )
        )
        if action
        else None
    )
    processing = bool(batch.active and batch.status in ACTIVE_BATCH_STATUSES)
    sendable = bool(
        action
        and action.status in {"queued", "sending"}
        and not _is_past(action.expire_at)
        and task
        and task.status in {TaskStatus.pending.value, TaskStatus.running.value}
    )
    expected_token = _batch_continuation_token(batch, binding)
    token_matches = bool(
        expected_token
        and (
            presented_token is None
            or hmac.compare_digest(str(presented_token), expected_token)
        )
    )
    revision_matches = bool(
        batch.continuation_authorization_revision
        and batch.continuation_authorization_revision
        == _authorization_revision(binding)
    )
    conversation = db.get(Conversation, batch.conversation_id)
    reply_then_handoff_sendable = bool(
        action
        and action.decision == "reply_then_handoff"
        and conversation
        and conversation.status == "waiting_sales_reply"
        and db.scalar(
            select(HandoffEvent.id).where(
                HandoffEvent.conversation_id == batch.conversation_id,
                HandoffEvent.batch_id == batch.id,
                HandoffEvent.closed_at.is_(None),
                HandoffEvent.deleted_at.is_(None),
            )
        )
    )
    allowed = bool(
        token_matches
        and revision_matches
        and worker.id == binding.worker_id
        and worker.run_status == "running"
        and binding.bind_status == "bound"
        and binding.listen_status in {"listening", "degraded"}
        and binding.allow_listening
        and binding.deleted_at is None
        and conversation
        and (
            conversation.status == "ai_active"
            or reply_then_handoff_sendable
        )
        and not batch.superseded_by_batch_id
        and (processing or sendable)
    )
    return {
        "allowed": allowed,
        "authorization_scope": "batch_continuation",
        "batch_id": batch.id,
        "continuation_token": expected_token if allowed else "",
        "conversation_id": batch.conversation_id,
        "authorization_revision": (
            batch.continuation_authorization_revision or ""
        ),
        "read_reason": batch.continuation_read_reason or "",
        "remark_code": binding.remark_code or "",
        "rpa_session_key": binding.rpa_session_key or "",
        "display_name": binding.display_name or "",
        "lead_id": binding.lead_id,
        "sales_id": binding.sales_id,
    }


def _restore_conversation_after_no_action(
    conversation: Conversation,
    batch: MessageBatch,
) -> None:
    origin = str(batch.origin_conversation_status or "")
    if origin in {
        "waiting_user_reply",
        "recalled_waiting_user",
        "waiting_sales_reply",
        "sales_replied_waiting_user",
    }:
        conversation.status = origin
    elif batch.continuation_read_reason == "recall_precheck":
        conversation.status = "sales_replied_waiting_user"
    else:
        conversation.status = "waiting_user_reply"
    if batch.trigger_type == "recall":
        conversation.recall_origin_status = None
        conversation.recall_cycle_id = None
        conversation.next_recall_at = utcnow() + timedelta(
            hours=get_settings().c3_recall_after_hours
        )


def _final_send_text(value: str | None) -> str:
    return canonical_reply_text(value)


def _hash_text(value: str) -> str:
    return reply_text_hash(value)


def _is_past(value) -> bool:
    if not value:
        return False
    now = utcnow()
    if getattr(value, "tzinfo", None) is None:
        now = now.replace(tzinfo=None)
    return value <= now


def _binding_or_404(db: Session, conversation_id: str) -> WechatSessionBinding:
    binding = db.scalar(
        select(WechatSessionBinding).where(
            WechatSessionBinding.conversation_id == conversation_id,
            WechatSessionBinding.deleted_at.is_(None),
        )
    )
    if not binding:
        raise AppError("CONVERSATION_NOT_ELIGIBLE", "会话不存在或未绑定", 404, {"suggested_action": "check_conversation_binding"})
    return binding


def _conversation_for_binding(db: Session, binding: WechatSessionBinding) -> Conversation:
    conversation = db.get(Conversation, binding.conversation_id)
    if not conversation:
        conversation = Conversation(
            conversation_id=binding.conversation_id,
            lead_id=binding.lead_id,
            sales_id=binding.sales_id,
            worker_id=binding.worker_id,
        )
        db.add(conversation)
        db.flush()
    return conversation


def _ensure_conversation_eligible(binding: WechatSessionBinding, conversation: Conversation) -> None:
    if binding.bind_status != "bound" or not binding.allow_listening:
        raise AppError("CONVERSATION_NOT_ELIGIBLE", "会话未绑定或不允许监听", 409, {"suggested_action": "handoff"})
    if not conversation.ai_enabled or conversation.status in {
        "waiting_sales_reply",
        "sales_replied_waiting_user",
        "closed",
        "rejected",
    }:
        raise AppError("CONVERSATION_NOT_ELIGIBLE", "会话已关闭 AI 或处于人工接管状态", 409, {"suggested_action": "handoff"})


def _ensure_reply_action_send_eligible(
    db: Session,
    *,
    binding: WechatSessionBinding,
    conversation: Conversation,
    action: ReplyAction,
) -> None:
    if action.decision != "reply_then_handoff":
        _ensure_conversation_eligible(binding, conversation)
        return
    if binding.bind_status != "bound" or not binding.allow_listening:
        raise AppError(
            "CONVERSATION_NOT_ELIGIBLE",
            "会话未绑定或不允许监听",
            409,
            {"suggested_action": "do_not_send"},
        )
    handoff = db.scalar(
        select(HandoffEvent.id).where(
            HandoffEvent.conversation_id == action.conversation_id,
            HandoffEvent.batch_id == action.batch_id,
            HandoffEvent.closed_at.is_(None),
            HandoffEvent.deleted_at.is_(None),
        )
    )
    if (
        not conversation.ai_enabled
        or conversation.status != "waiting_sales_reply"
        or not handoff
    ):
        raise AppError(
            "REPLY_THEN_HANDOFF_NOT_ELIGIBLE",
            "转人工边界回复已失效或人工接管事实不存在",
            409,
            {"suggested_action": "do_not_send"},
        )


def _batch_to_dict(batch: MessageBatch) -> dict[str, Any]:
    retry_after = None
    if batch.status == "retry_wait" and batch.generated_at:
        retry_after = batch.generated_at + timedelta(
            seconds=max(0.0, float(get_settings().c3_batch_retry_delay_seconds))
        )
    return {
        "id": batch.id,
        "conversation_id": batch.conversation_id,
        "status": batch.status,
        "active": batch.active,
        "trigger_type": batch.trigger_type,
        "trigger_key": batch.trigger_key,
        "recall_cycle_id": batch.recall_cycle_id,
        "retryable": batch.retryable,
        "trigger_message_event_id": batch.trigger_message_event_id,
        "message_event_ids": batch.message_event_ids,
        "message_count": batch.message_count,
        "generation_no": batch.generation_no,
        "generation_attempt_count": batch.generation_attempt_count,
        "generation_started_at": batch.generation_started_at,
        "trace_id": batch.trace_id,
        "decision": batch.decision,
        "error_code": batch.error_code,
        "suggested_action": batch.suggested_action,
        "superseded_by_batch_id": batch.superseded_by_batch_id,
        "generated_at": batch.generated_at,
        "retry_after": retry_after,
        "created_at": batch.created_at,
        "updated_at": batch.updated_at,
    }


def _reply_action_to_dict(action: ReplyAction | None) -> dict[str, Any] | None:
    if not action:
        return None
    return {
        "id": action.id,
        "batch_id": action.batch_id,
        "conversation_id": action.conversation_id,
        "status": action.status,
        "current": action.current,
        "generation_no": action.generation_no,
        "decision": action.decision,
        "reply_text": action.reply_text,
        "reply_text_hash": action.reply_text_hash,
        "confidence": action.confidence,
        "risk_flags": action.risk_flags,
        "evidence_refs": action.evidence_refs,
        "guard_result": action.guard_result,
        "handoff_reason_code": action.handoff_reason_code,
        "error_code": action.error_code,
        "suggested_action": action.suggested_action,
        "expire_at": action.expire_at,
        "claimed_by_worker_id": action.claimed_by_worker_id,
        "claimed_task_id": action.claimed_task_id,
        "sending_claimed_at": action.sending_claimed_at,
        "sent_at": action.sent_at,
        "created_at": action.created_at,
        "updated_at": action.updated_at,
    }


def _handoff_to_dict(event: HandoffEvent | None) -> dict[str, Any] | None:
    if not event:
        return None
    return {
        "id": event.id,
        "conversation_id": event.conversation_id,
        "batch_id": event.batch_id,
        "status": event.status,
        "handoff_reason_code": event.handoff_reason_code,
        "reason_detail": event.reason_detail,
        "trigger_message_event_ids": event.trigger_message_event_ids,
        "risk_flags": event.risk_flags,
        "evidence_refs": event.evidence_refs,
        "notify_error_code": event.notify_error_code,
        "closed_at": event.closed_at,
        "created_at": event.created_at,
        "updated_at": event.updated_at,
    }


def _sent_ack_to_dict(ack: SentAck) -> dict[str, Any]:
    return {
        "id": ack.id,
        "reply_action_id": ack.reply_action_id,
        "task_id": ack.task_id,
        "worker_id": ack.worker_id,
        "client_instance_id": ack.client_instance_id,
        "send_result": ack.send_result,
        "action_phase": ack.action_phase,
        "reply_text_hash": ack.reply_text_hash,
        "sidecar_run_id": ack.sidecar_run_id,
        "evidence": ack.evidence,
        "error_code": ack.error_code,
        "remark": ack.remark,
        "sent_at": ack.sent_at,
        "created_at": ack.created_at,
    }


def _active_batch(db: Session, conversation_id: str) -> MessageBatch | None:
    return db.scalar(
        select(MessageBatch)
        .where(
            MessageBatch.conversation_id == conversation_id,
            MessageBatch.active.is_(True),
            MessageBatch.deleted_at.is_(None),
        )
        .order_by(MessageBatch.created_at.desc())
    )


def _customer_messages(db: Session, batch: MessageBatch) -> list[MessageEvent]:
    if not batch.message_event_ids:
        return []
    rows = list(
        db.scalars(
            select(MessageEvent)
            .where(
                MessageEvent.id.in_(batch.message_event_ids),
                MessageEvent.sender_role == "customer",
            )
        )
    )
    by_id = {item.id: item for item in rows}
    # message_event_ids is populated from Worker's authoritative top-to-bottom
    # V3 array. Do not rebuild a second order from OCR time or database UUIDs.
    return [by_id[event_id] for event_id in batch.message_event_ids if event_id in by_id]


def _cancel_task_for_action(db: Session, action: ReplyAction, *, reason: str) -> None:
    task = db.scalar(select(Task).where(Task.reply_action_id == action.id, Task.deleted_at.is_(None)))
    if task and task.status in {TaskStatus.blocked.value, TaskStatus.pending.value, TaskStatus.running.value}:
        before = task.status
        task.status = TaskStatus.cancelled.value
        task.cancel_reason = reason
        task.cancelled_at = utcnow()
        finish_task_and_release_worker(task)
        _write_event(db, task, TaskEventType.cancelled, from_status=before, to_status=task.status, remark=reason)


def _supersede_action_for_stale_vehicle_facts(
    db: Session,
    action: ReplyAction,
    *,
    vehicle_ids: list[str],
) -> None:
    action.status = "superseded"
    action.current = False
    action.error_code = VEHICLE_FACT_STALE_CODE
    action.suggested_action = "regenerate"
    batch = db.get(MessageBatch, action.batch_id)
    if batch and batch.status not in {"superseded", "cancelled", "handoff_created"}:
        batch.status = "superseded"
        batch.active = False
        batch.retryable = False
        batch.error_code = "MESSAGE_BATCH_VEHICLE_FACT_STALE"
        batch.suggested_action = "regenerate"
    _cancel_task_for_action(
        db,
        action,
        reason=f"{VEHICLE_FACT_STALE_REASON}：{','.join(vehicle_ids)}",
    )


def invalidate_vehicle_dependent_reply_actions(db: Session, vehicle_id: str) -> list[str]:
    action_ids = set(
        db.scalars(
            select(ReplyActionVehicleFact.reply_action_id)
            .join(ReplyAction, ReplyAction.id == ReplyActionVehicleFact.reply_action_id)
            .where(
                ReplyActionVehicleFact.vehicle_id == vehicle_id,
                ReplyAction.status.in_(SUPERSEDABLE_ACTION_STATUSES),
                ReplyAction.current.is_(True),
                ReplyAction.deleted_at.is_(None),
            )
            .order_by(ReplyActionVehicleFact.reply_action_id)
        )
    )
    legacy_candidates = list(
        db.scalars(
            select(ReplyAction).where(
                ReplyAction.status.in_(SUPERSEDABLE_ACTION_STATUSES),
                ReplyAction.current.is_(True),
                ReplyAction.deleted_at.is_(None),
            )
        )
    )
    action_ids.update(
        action.id
        for action in legacy_candidates
        if vehicle_id in _brain_plan_vehicle_ids(action.ai_payload or {})
    )
    if not action_ids:
        return []
    actions = list(
        db.scalars(
            select(ReplyAction)
            .where(ReplyAction.id.in_(action_ids))
            .order_by(ReplyAction.id)
            .with_for_update()
        )
    )
    for action in actions:
        if action.status in SUPERSEDABLE_ACTION_STATUSES and action.current:
            _supersede_action_for_stale_vehicle_facts(db, action, vehicle_ids=[vehicle_id])
    db.flush()
    return [action.id for action in actions if action.error_code == VEHICLE_FACT_STALE_CODE]


def _reject_stale_vehicle_reply(
    db: Session,
    action: ReplyAction,
    *,
    stale_vehicle_ids: list[str],
) -> None:
    _supersede_action_for_stale_vehicle_facts(db, action, vehicle_ids=stale_vehicle_ids)
    db.flush()
    raise AppError(
        VEHICLE_FACT_STALE_CODE,
        "车辆资料或上下架状态已变化，禁止发送旧回复",
        409,
        {"vehicle_ids": stale_vehicle_ids, "suggested_action": "do_not_send"},
    )


def _supersede_open_actions(db: Session, conversation_id: str, *, reason: str) -> None:
    actions = list(
        db.scalars(
            select(ReplyAction).where(
                ReplyAction.conversation_id == conversation_id,
                ReplyAction.status.in_(SUPERSEDABLE_ACTION_STATUSES),
                ReplyAction.deleted_at.is_(None),
            )
        )
    )
    for action in actions:
        action.status = "superseded"
        action.current = False
        action.error_code = "REPLY_ACTION_SUPERSEDED"
        action.suggested_action = "regenerate"
        batch = db.get(MessageBatch, action.batch_id)
        if batch and batch.status not in {"superseded", "cancelled", "handoff_created"}:
            batch.status = "superseded"
            batch.active = False
            batch.retryable = False
            batch.error_code = "MESSAGE_BATCH_SUPERSEDED"
            batch.suggested_action = "regenerate"
        _cancel_task_for_action(db, action, reason=reason)


def supersede_open_reply_actions_for_new_inbound(db: Session, conversation_id: str) -> None:
    _supersede_open_actions(db, conversation_id, reason="客户新消息到来，旧回复动作作废")


def cancel_open_reply_actions_for_conversation_change(db: Session, conversation_id: str, *, reason: str) -> None:
    actions = list(
        db.scalars(
            select(ReplyAction).where(
                ReplyAction.conversation_id == conversation_id,
                ReplyAction.status.in_(SUPERSEDABLE_ACTION_STATUSES),
                ReplyAction.deleted_at.is_(None),
            )
        )
    )
    for action in actions:
        action.status = "cancelled"
        action.current = False
        batch = db.get(MessageBatch, action.batch_id)
        if batch and batch.status not in {"superseded", "cancelled", "handoff_created"}:
            batch.status = "cancelled"
            batch.active = False
            batch.retryable = False
            batch.error_code = "MESSAGE_BATCH_CANCELLED_BY_CONVERSATION_CHANGE"
            batch.suggested_action = reason
        _cancel_task_for_action(db, action, reason=reason)


def cancel_active_batches_for_conversation_change(db: Session, conversation_id: str, *, reason: str) -> None:
    for batch in db.scalars(
        select(MessageBatch).where(
            MessageBatch.conversation_id == conversation_id,
            MessageBatch.active.is_(True),
            MessageBatch.deleted_at.is_(None),
        )
    ):
        batch.status = "cancelled"
        batch.active = False
        batch.error_code = "MESSAGE_BATCH_CANCELLED_BY_CONVERSATION_CHANGE"
        batch.suggested_action = reason
    cancel_open_reply_actions_for_conversation_change(db, conversation_id, reason=reason)


def _validated_hard_opt_out_event(
    db: Session,
    *,
    batch: MessageBatch,
    evidence: dict | None,
) -> MessageEvent | None:
    payload = evidence if isinstance(evidence, dict) else {}
    event_id = str(payload.get("message_event_id") or "").strip()
    source_key = str(payload.get("source_message_key") or "").strip()
    customer_text = " ".join(str(payload.get("customer_text") or "").split())
    if not event_id or event_id not in set(batch.message_event_ids or []) or not customer_text:
        return None
    event = db.scalar(
        select(MessageEvent)
        .where(
            MessageEvent.id == event_id,
            MessageEvent.conversation_id == batch.conversation_id,
            MessageEvent.sender_role == "customer",
        )
        .with_for_update()
    )
    if not event:
        return None
    event_source_key = str(
        event.source_message_key
        or ((event.raw_payload or {}).get("source_message_key") if isinstance(event.raw_payload, dict) else "")
        or ""
    ).strip()
    event_text = " ".join(str(event.content or "").split())
    if source_key != event_source_key or customer_text != event_text:
        return None
    return event


def _reject_conversation_for_hard_opt_out(
    db: Session,
    *,
    binding: WechatSessionBinding,
    conversation: Conversation,
    batch: MessageBatch,
    decision: AIEngineDecision,
    evidence_event: MessageEvent,
) -> dict[str, Any]:
    reason = "客户明确要求停止自动联系"
    cancel_active_batches_for_conversation_change(
        db,
        binding.conversation_id,
        reason=reason,
    )
    conversation.status = "rejected"
    conversation.ai_enabled = False
    conversation.next_recall_at = None
    conversation.recall_cycle_id = None
    conversation.recall_origin_status = None
    conversation.close_reason = f"customer_hard_opt_out:{evidence_event.id}"
    binding.unread_hint = False

    batch.status = "rejected"
    batch.active = False
    batch.retryable = False
    batch.decision = "hard_opt_out"
    batch.error_code = None
    batch.suggested_action = "do_not_contact"
    batch.ai_response_snapshot = _preserve_generation_attempt_history(
        batch,
        _decision_payload(decision),
    )
    batch.generated_at = utcnow()
    db.flush()
    return {
        "decision": "hard_opt_out",
        "batch": _batch_to_dict(batch),
        "error_code": None,
        "suggested_action": "do_not_contact",
        "conversation_status": conversation.status,
        "evidence_message_event_id": evidence_event.id,
    }


def create_control_message_batch(
    db: Session,
    *,
    conversation_id: str,
    trigger_type: str,
    trigger_key: str,
    recall_cycle_id: str | None = None,
    trace_id: str | None = None,
) -> dict[str, Any]:
    conversation = db.scalar(
        select(Conversation)
        .where(Conversation.conversation_id == conversation_id)
        .with_for_update()
    )
    if not conversation:
        raise AppError("CONVERSATION_NOT_FOUND", "会话不存在", 404)
    existing = db.scalar(
        select(MessageBatch).where(
            MessageBatch.conversation_id == conversation_id,
            MessageBatch.trigger_type == trigger_type,
            MessageBatch.trigger_key == trigger_key,
            MessageBatch.deleted_at.is_(None),
        )
    )
    if existing:
        return {"batch_id": existing.id, "batch_status": existing.status, "batch": _batch_to_dict(existing)}
    active = _active_batch(db, conversation_id)
    if active:
        active.status = "superseded"
        active.active = False
        active.error_code = "MESSAGE_BATCH_SUPERSEDED"
    batch = MessageBatch(
        conversation_id=conversation_id,
        status="collecting",
        active=True,
        trigger_type=trigger_type,
        trigger_key=trigger_key,
        recall_cycle_id=recall_cycle_id,
        message_event_ids=[],
        message_count=0,
        trace_id=trace_id,
    )
    db.add(batch)
    db.flush()
    return {"batch_id": batch.id, "batch_status": batch.status, "batch": _batch_to_dict(batch)}


def collect_message_batch(
    db: Session,
    *,
    conversation_id: str,
    trigger_message_event_id: str,
    trace_id: str | None = None,
) -> dict[str, Any]:
    binding = _binding_or_404(db, conversation_id)
    conversation = db.scalar(
        select(Conversation)
        .where(Conversation.conversation_id == conversation_id)
        .with_for_update()
    )
    if not conversation:
        conversation = _conversation_for_binding(db, binding)
        db.flush()
    message = db.get(MessageEvent, trigger_message_event_id)
    if not message or message.conversation_id != conversation_id:
        raise AppError("MESSAGE_EVENT_NOT_FOUND", "触发消息不存在或不属于该会话", 404, {"suggested_action": "check_message_event"})
    if message.sender_role != "customer":
        return {
            "batch_id": None,
            "batch_status": "no_action",
            "next_step": "no_action",
            "error_code": None,
            "suggested_action": "ignore_non_customer_message",
        }

    existing_for_event = next(
        (
            item
            for item in db.scalars(
                select(MessageBatch)
                .where(
                    MessageBatch.conversation_id == conversation_id,
                    MessageBatch.deleted_at.is_(None),
                )
                .order_by(MessageBatch.created_at.desc())
            )
            if message.id in (item.message_event_ids or [])
        ),
        None,
    )
    if existing_for_event:
        return {
            "batch_id": existing_for_event.id,
            "batch_status": existing_for_event.status,
            "next_step": "generate" if existing_for_event.status in ACTIVE_BATCH_STATUSES else "use_existing",
            "batch": _batch_to_dict(existing_for_event),
        }

    _ensure_conversation_eligible(binding, conversation)

    active = _active_batch(db, conversation_id)
    if active and active.status in {"generating", "retry_wait"}:
        active.status = "superseded"
        active.active = False
        active.retryable = False
        active.error_code = "MESSAGE_BATCH_SUPERSEDED"
        _supersede_open_actions(db, conversation_id, reason="客户新消息到来，旧回复动作作废")
        active = None

    if active and active.status == "collecting":
        ids = list(active.message_event_ids or [])
        if message.id not in ids:
            ids.append(message.id)
            active.message_event_ids = ids
            active.message_count = len(ids)
            active.trigger_message_event_id = message.id
            active.trace_id = trace_id or active.trace_id
        db.flush()
        return {"batch_id": active.id, "batch_status": active.status, "next_step": "generate", "batch": _batch_to_dict(active)}

    batch = MessageBatch(
        conversation_id=conversation_id,
        status="collecting",
        active=True,
        trigger_message_event_id=message.id,
        message_event_ids=[message.id],
        message_count=1,
        generation_no=1,
        trace_id=trace_id,
    )
    db.add(batch)
    db.flush()
    return {"batch_id": batch.id, "batch_status": batch.status, "next_step": "generate", "batch": _batch_to_dict(batch)}


def collect_customer_message_batch(
    db: Session,
    *,
    conversation_id: str,
    message_event_ids: list[str],
    trace_id: str | None = None,
) -> dict[str, Any] | None:
    """Atomically collect only newly inserted customer events from one ingest call."""
    unique_ids = list(dict.fromkeys(message_event_ids))
    if not unique_ids:
        return None
    result: dict[str, Any] | None = None
    for event_id in unique_ids:
        result = collect_message_batch(
            db,
            conversation_id=conversation_id,
            trigger_message_event_id=event_id,
            trace_id=trace_id,
        )
    return result


def collect_recovered_customer_message_batch(
    db: Session,
    *,
    conversation_id: str,
    message_event_ids: list[str],
    trace_id: str | None = None,
) -> dict[str, Any] | None:
    """Create one deterministic batch for an auto-recovered customer tail.

    A recoverable C2 handoff may already own the customer events that a later
    authoritative read proves complete.  The normal collector deliberately
    reuses any batch that already contains an event, so it cannot create the
    fresh reply work needed after that handoff closes.  Recovery therefore has
    its own stable trigger, keyed by the ordered database event IDs.
    """

    unique_ids = list(
        dict.fromkeys(
            str(value).strip()
            for value in message_event_ids
            if str(value).strip()
        )
    )
    if not unique_ids:
        return None
    events = list(
        db.scalars(
            select(MessageEvent).where(
                MessageEvent.conversation_id == conversation_id,
                MessageEvent.id.in_(unique_ids),
            )
        )
    )
    events_by_id = {event.id: event for event in events}
    if set(events_by_id) != set(unique_ids):
        raise AppError(
            "C2_RECOVERY_MESSAGE_TAIL_INCOMPLETE",
            "C2 自动恢复的客户消息尾部与数据库记录不一致",
            409,
        )
    if any(
        events_by_id[event_id].sender_role != "customer"
        for event_id in unique_ids
    ):
        raise AppError(
            "C2_RECOVERY_MESSAGE_TAIL_INVALID",
            "C2 自动恢复批次只能包含客户消息",
            409,
        )

    trigger_key = "customer-tail:" + hashlib.sha256(
        "\n".join(unique_ids).encode("utf-8")
    ).hexdigest()
    existing = db.scalar(
        select(MessageBatch).where(
            MessageBatch.conversation_id == conversation_id,
            MessageBatch.trigger_type == "c2_handoff_recovery",
            MessageBatch.trigger_key == trigger_key,
            MessageBatch.deleted_at.is_(None),
        )
    )
    if existing:
        return {
            "batch_id": existing.id,
            "batch_status": existing.status,
            "next_step": (
                "generate"
                if existing.status in ACTIVE_BATCH_STATUSES
                else "use_existing"
            ),
            "batch": _batch_to_dict(existing),
        }

    binding = _binding_or_404(db, conversation_id)
    conversation = db.scalar(
        select(Conversation)
        .where(Conversation.conversation_id == conversation_id)
        .with_for_update()
    )
    if not conversation:
        conversation = _conversation_for_binding(db, binding)
        db.flush()
    _ensure_conversation_eligible(binding, conversation)

    # The conversation lock serializes recovery with other batch creation.
    # Re-read after acquiring it so simultaneous retries converge on the batch
    # committed by the first request instead of racing the unique indexes.
    existing = db.scalar(
        select(MessageBatch).where(
            MessageBatch.conversation_id == conversation_id,
            MessageBatch.trigger_type == "c2_handoff_recovery",
            MessageBatch.trigger_key == trigger_key,
            MessageBatch.deleted_at.is_(None),
        )
    )
    if existing:
        return {
            "batch_id": existing.id,
            "batch_status": existing.status,
            "next_step": (
                "generate"
                if existing.status in ACTIVE_BATCH_STATUSES
                else "use_existing"
            ),
            "batch": _batch_to_dict(existing),
        }

    active = _active_batch(db, conversation_id)
    if active:
        active.status = "superseded"
        active.active = False
        active.retryable = False
        active.error_code = "MESSAGE_BATCH_SUPERSEDED"
        _supersede_open_actions(
            db,
            conversation_id,
            reason="C2 权威重读恢复后，以最新未回复客户尾部重建回复任务",
        )

    batch = MessageBatch(
        conversation_id=conversation_id,
        status="collecting",
        active=True,
        trigger_type="c2_handoff_recovery",
        trigger_key=trigger_key,
        trigger_message_event_id=unique_ids[-1],
        message_event_ids=unique_ids,
        message_count=len(unique_ids),
        generation_no=1,
        trace_id=trace_id,
    )
    db.add(batch)
    db.flush()
    return {
        "batch_id": batch.id,
        "batch_status": batch.status,
        "next_step": "generate",
        "batch": _batch_to_dict(batch),
    }


def _brain_snapshot_text(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split()).strip()[:limit]


def _brain_snapshot_event_time(item: MessageEvent) -> str:
    value = item.observed_at or item.occurred_at or item.ingested_at
    if value is None:
        raise AppError(
            "AI_CONTEXT_BUILD_FAILED",
            "MessageEvent 缺少可冻结的事件时间",
            409,
            {"message_event_id": item.id},
        )
    return value.isoformat()


def _brain_snapshot_message(item: MessageEvent) -> dict[str, Any]:
    raw = item.raw_payload if isinstance(item.raw_payload, dict) else {}
    result: dict[str, Any] = {
        "message_event_id": _brain_snapshot_text(item.id, 128),
        "source_message_key": _brain_snapshot_text(
            item.source_message_key or raw.get("source_message_key"), 255
        ),
        "sender_role": _brain_snapshot_text(item.sender_role, 32).lower(),
        "message_type": _brain_snapshot_text(item.message_type, 32).lower(),
        "content": _brain_snapshot_text(item.content, 4000),
        "item_state": _brain_snapshot_text(
            item.item_state or raw.get("item_state"), 32
        ).lower(),
        "error_code": _brain_snapshot_text(
            item.error_code or raw.get("error_code"), 64
        ),
        "occurred_at": _brain_snapshot_event_time(item),
    }
    if result["message_type"] != "image":
        return result
    understanding = (
        raw.get("customer_image_understanding")
        if isinstance(raw.get("customer_image_understanding"), dict)
        else {}
    )
    bridge = (
        raw.get("visual_bridge_input")
        if isinstance(raw.get("visual_bridge_input"), dict)
        else {}
    )
    catalog_assist = (
        bridge.get("catalog_assist")
        if isinstance(bridge.get("catalog_assist"), dict)
        else {}
    )
    understanding_bridge = (
        understanding.get("bridge")
        if isinstance(understanding.get("bridge"), dict)
        else {}
    )
    image_ocr = understanding.get("image_ocr_text")
    if not isinstance(image_ocr, list):
        image_ocr = []
    classification = understanding.get("classification")
    entities = understanding.get("entities")
    result.update(
        {
            "vision_summary": _brain_snapshot_text(
                understanding.get("vision_summary"), 2000
            ),
            "image_ocr_text": [
                _brain_snapshot_text(value, 500)
                for value in image_ocr[:20]
                if _brain_snapshot_text(value, 500)
            ],
            "classification": (
                dict(classification) if isinstance(classification, dict) else {}
            ),
            "entities": dict(entities) if isinstance(entities, dict) else {},
            "normalized_vehicle_query": _brain_snapshot_text(
                catalog_assist.get("normalized_vehicle_query")
                or understanding_bridge.get("normalized_vehicle_query"),
                500,
            ),
            "server_validated_product_id": _brain_snapshot_text(
                catalog_assist.get("server_validated_product_id")
                or raw.get("server_validated_product_id"),
                128,
            ),
        }
    )
    return result


def _brain_snapshot_sha256(messages: list[dict[str, Any]]) -> str:
    encoded = json.dumps(
        messages,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _brain_snapshot_message_is_semantic(item: dict[str, Any]) -> bool:
    if item.get("error_code") or item.get("item_state") == "failed":
        return False
    if item.get("sender_role") not in {"customer", "self", "system"}:
        return False
    if item.get("message_type") != "image":
        return bool(item.get("content"))
    return bool(
        item.get("content")
        or item.get("vision_summary")
        or item.get("image_ocr_text")
    )


def _build_brain_context_snapshot(
    db: Session,
    *,
    binding: WechatSessionBinding,
    batch: MessageBatch,
) -> dict[str, Any]:
    existing = (
        batch.ai_request_snapshot.get("brain_context_snapshot")
        if isinstance(batch.ai_request_snapshot, dict)
        else None
    )
    if isinstance(existing, dict) and existing:
        return dict(existing)

    current_ids = [str(value) for value in (batch.message_event_ids or [])]
    history_filter = MessageEvent.conversation_id == binding.conversation_id
    if current_ids:
        history_filter = history_filter & MessageEvent.id.not_in(current_ids)
    all_history_rows = list(
        db.scalars(
            select(MessageEvent)
            .where(history_filter)
            .order_by(
                func.coalesce(
                    MessageEvent.observed_at,
                    MessageEvent.occurred_at,
                    MessageEvent.ingested_at,
                ),
                MessageEvent.observation_order,
                MessageEvent.id,
            )
        )
    )
    history_count = len(all_history_rows)
    window_rows = all_history_rows[-50:]
    prior_messages = [_brain_snapshot_message(item) for item in window_rows]
    # Only the frozen 50-event window can be rendered into history_text.
    # Counting older, deliberately excluded rows would incorrectly report a
    # lost history when the retained window legitimately contains no semantic
    # fact.
    semantic_count = sum(
        1
        for item in prior_messages
        if _brain_snapshot_message_is_semantic(item)
    )
    return {
        "schema_version": 1,
        "history_authority": "chejin_message_events_v1",
        "conversation_id": binding.conversation_id,
        "prior_messages": prior_messages,
        "current_batch_message_ids": current_ids,
        "history_event_count_before_batch": history_count,
        "semantic_history_count_before_batch": semantic_count,
        "prior_messages_sha256": _brain_snapshot_sha256(prior_messages),
        "history_window_complete": len(prior_messages) == min(50, history_count),
    }


def _build_ai_context(db: Session, binding: WechatSessionBinding, conversation: Conversation, batch: MessageBatch) -> dict[str, Any]:
    messages = _customer_messages(db, batch)
    brain_context_snapshot = _build_brain_context_snapshot(
        db,
        binding=binding,
        batch=batch,
    )
    history_rows = list(
        db.scalars(
            select(MessageEvent)
            .where(MessageEvent.conversation_id == binding.conversation_id)
            .order_by(
                func.coalesce(
                    MessageEvent.observed_at,
                    MessageEvent.occurred_at,
                    MessageEvent.ingested_at,
                ).desc(),
                MessageEvent.observation_order.desc(),
                MessageEvent.id.desc(),
            )
            .limit(50)
        )
    )
    history_rows.reverse()

    def compact_message(item: MessageEvent) -> dict[str, Any]:
        raw = item.raw_payload if isinstance(item.raw_payload, dict) else {}
        result: dict[str, Any] = {
            "id": item.id,
            "source_message_key": str(
                item.source_message_key
                or raw.get("source_message_key")
                or ""
            ),
            "sender_role": item.sender_role,
            "message_type": item.message_type,
            "content": item.content,
            "item_state": str(
                item.item_state or raw.get("item_state") or ""
            ),
            "error_code": str(
                item.error_code
                or raw.get("error_code")
                or ""
            ),
            "occurred_at": (
                item.occurred_at.isoformat()
                if item.occurred_at
                else None
            ),
            "message_position": raw.get("message_position"),
        }
        if str(item.message_type or "").strip().lower() != "image":
            return result
        understanding = (
            raw.get("customer_image_understanding")
            if isinstance(raw.get("customer_image_understanding"), dict)
            else {}
        )
        bridge = (
            raw.get("visual_bridge_input")
            if isinstance(raw.get("visual_bridge_input"), dict)
            else {}
        )
        catalog_assist = (
            bridge.get("catalog_assist")
            if isinstance(bridge.get("catalog_assist"), dict)
            else {}
        )
        result.update(
            {
                "vision_summary": str(
                    understanding.get("vision_summary") or ""
                )[:2000],
                "image_ocr_text": list(
                    understanding.get("image_ocr_text") or []
                )[:20],
                "classification": dict(
                    understanding.get("classification") or {}
                ),
                "entities": dict(understanding.get("entities") or {}),
                "normalized_vehicle_query": str(
                    catalog_assist.get("normalized_vehicle_query")
                    or (understanding.get("bridge") or {}).get(
                        "normalized_vehicle_query"
                    )
                    or ""
                )[:500],
                "server_validated_product_id": str(
                    catalog_assist.get("server_validated_product_id")
                    or raw.get("server_validated_product_id")
                    or ""
                )[:128],
                "customer_image_understanding": understanding,
                "visual_bridge_input": bridge,
            }
        )
        return result

    context = {
        # The immutable snapshot has one canonical storage location in
        # MessageBatch.ai_request_snapshot.  The Adapter receives it
        # explicitly below; do not duplicate it under conversation where a
        # same-batch retry could accidentally miss it and rebuild history.
        "brain_context_snapshot": brain_context_snapshot,
        "conversation": {
            "conversation_id": binding.conversation_id,
            "lead_id": binding.lead_id,
            "sales_id": binding.sales_id,
            "worker_id": binding.worker_id,
            "remark_code": binding.remark_code,
            "status": conversation.status,
            "ai_enabled": conversation.ai_enabled,
            "reply_count": conversation.reply_count,
        },
        "messages": [
            {
                **compact_message(item),
                "dedupe_key": item.dedupe_key,
                "ingested_at": item.ingested_at.isoformat(),
            }
            for item in messages
        ],
    }
    existing_snapshot = (
        batch.ai_request_snapshot
        if isinstance(batch.ai_request_snapshot, dict)
        else {}
    )
    frozen_checkpoint = existing_snapshot.get("pre_send_fact_checkpoint")
    if not isinstance(frozen_checkpoint, dict) or not frozen_checkpoint:
        checkpoint_tail = _checkpoint_tail_from_latest_complete_frame(
            history_rows
        )
        if batch.trigger_type == "friend_welcome" and not history_rows:
            frozen_checkpoint = _build_pre_send_fact_checkpoint(
                batch=batch,
                ordered_messages=[],
                baseline_kind="friend_welcome_empty",
                authoritative_frame_source="control_empty",
                tail_complete=True,
            )
        else:
            # Never fall back to the entire database history.  It can contain
            # rows that are no longer visible in the authoritative WeChat
            # viewport and would manufacture a false pre-send prefix.
            frozen_checkpoint = _build_pre_send_fact_checkpoint(
                batch=batch,
                ordered_messages=checkpoint_tail,
                baseline_kind="message_tail",
                authoritative_frame_source=(
                    _checkpoint_frame_source(checkpoint_tail)
                ),
                tail_complete=bool(checkpoint_tail),
            )
    context["pre_send_fact_checkpoint"] = dict(frozen_checkpoint)
    return context


def _decision_payload(decision: AIEngineDecision) -> dict[str, Any]:
    return {
        "decision": decision.decision,
        "reply_text": decision.reply_text,
        "confidence": decision.confidence,
        "handoff_reason_code": decision.handoff_reason_code,
        "risk_flags": decision.risk_flags or [],
        "evidence_refs": decision.evidence_refs or [],
        "guard_result": decision.guard_result,
        "rewrite_required": decision.rewrite_required,
        "error_code": decision.error_code,
        "suggested_action": decision.suggested_action,
        "hard_opt_out_evidence": decision.hard_opt_out_evidence or {},
        "raw_payload": decision.raw_payload or {},
    }


def _generation_attempt_history(batch: MessageBatch) -> list[dict[str, Any]]:
    snapshot = batch.ai_response_snapshot if isinstance(batch.ai_response_snapshot, dict) else {}
    history = snapshot.get("generation_attempt_history")
    if not isinstance(history, list):
        return []
    return [dict(item) for item in history if isinstance(item, dict)]


def _attempt_number(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _preserve_generation_attempt_history(
    batch: MessageBatch,
    payload: dict[str, Any],
) -> dict[str, Any]:
    result = dict(payload)
    history = _generation_attempt_history(batch)
    if history:
        result["generation_attempt_history"] = history
    return result


def _record_generation_attempt(
    batch: MessageBatch,
    *,
    attempt: int,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Append one immutable provider result before terminal projection.

    ``ai_response_snapshot`` remains the current batch projection, while the
    bounded history keeps the raw result for every claimed generation attempt.
    This prevents retry/handoff projection from destroying the evidence needed
    to explain a no-visible-reply failure.
    """

    response = dict(payload)
    response.pop("generation_attempt_history", None)
    history = [
        item
        for item in _generation_attempt_history(batch)
        if _attempt_number(item.get("attempt")) != _attempt_number(attempt)
    ]
    history.append(
        {
            "attempt": _attempt_number(attempt),
            "recorded_at": utcnow().isoformat(),
            "response": response,
        }
    )
    history.sort(key=lambda item: _attempt_number(item.get("attempt")))
    result = dict(response)
    result["generation_attempt_history"] = history[-5:]
    return result


def _record_stale_generation_attempt_diagnostics(
    batch: MessageBatch,
    *,
    attempt: int,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Append stale-attempt evidence without changing the batch projection.

    A new message may supersede the batch while its provider call is running.
    The provider result must never revive that batch, but its bounded attempt
    history is still immutable diagnostic evidence and must survive the stale
    claim return.
    """

    current = (
        dict(batch.ai_response_snapshot)
        if isinstance(batch.ai_response_snapshot, dict)
        else {}
    )
    recorded = _record_generation_attempt(
        batch,
        attempt=attempt,
        payload=payload,
    )
    current["generation_attempt_history"] = recorded[
        "generation_attempt_history"
    ]
    return current


def _stale_generation_diagnostics_payload(
    decision: AIEngineDecision,
) -> dict[str, Any]:
    """Keep only provider progress; never retain a stale customer reply."""

    raw_payload = (
        decision.raw_payload
        if isinstance(decision.raw_payload, dict)
        else {}
    )
    diagnostics: dict[str, Any] = {}
    for container_name in ("provider_error", "omniauto_brain_result"):
        source = raw_payload.get(container_name)
        if not isinstance(source, dict):
            continue
        container = {
            key: source[key]
            for key in (
                "provider_progress_id",
                "provider_progress",
                "last_provider_progress",
            )
            if key in source
        }
        progress = container.get("provider_progress")
        if (
            "last_provider_progress" not in container
            and isinstance(progress, list)
            and progress
            and isinstance(progress[-1], dict)
        ):
            container["last_provider_progress"] = dict(progress[-1])
        if container:
            diagnostics[container_name] = container
    return {
        "decision": "discarded_stale",
        "reply_text": None,
        "confidence": None,
        "handoff_reason_code": None,
        "risk_flags": [],
        "evidence_refs": [],
        "guard_result": None,
        "rewrite_required": False,
        "error_code": decision.error_code
        or "MESSAGE_BATCH_GENERATION_CLAIM_STALE",
        "suggested_action": "use_current_batch_state",
        "hard_opt_out_evidence": {},
        "raw_payload": diagnostics,
    }


def open_handoff_events_for_conversation(
    db: Session,
    conversation_id: str,
    *,
    for_update: bool = False,
) -> list[HandoffEvent]:
    """Return the authoritative manual-takeover gate for one conversation."""

    statement = (
        select(HandoffEvent)
        .where(
            HandoffEvent.conversation_id == conversation_id,
            HandoffEvent.closed_at.is_(None),
            HandoffEvent.deleted_at.is_(None),
        )
        .order_by(HandoffEvent.created_at.asc(), HandoffEvent.id.asc())
    )
    if for_update:
        statement = statement.with_for_update()
    return list(db.scalars(statement).all())


RECOVERABLE_C2_HANDOFF_REASON_CODES = frozenset(
    {
        "MESSAGE_CROSS_ROUND_IDENTITY_AMBIGUOUS",
        "C2_MESSAGE_HISTORY_GAP",
    }
)


def _record_handoff_closed_best_effort(
    db: Session,
    event: HandoffEvent,
) -> None:
    from app.core.request_id import get_request_id
    from app.services.observability_service import (
        process_run_id_for_handoff_event,
        record_server_stage_best_effort,
    )

    process_run_id = process_run_id_for_handoff_event(db, event)
    trace_id = get_request_id()
    record_server_stage_best_effort(
        db,
        process_run_id=process_run_id,
        conversation_id=event.conversation_id,
        stage_name="handoff.wait_sales",
        component="backend",
        duration_ms=None,
        status="succeeded",
        trace_id=trace_id,
        stable_key=event.id,
    )
    record_server_stage_best_effort(
        db,
        process_run_id=process_run_id,
        conversation_id=event.conversation_id,
        stage_name="handoff.close",
        component="backend",
        duration_ms=0,
        status="succeeded",
        trace_id=trace_id,
        stable_key=event.id,
    )


def close_open_recoverable_c2_handoffs(
    db: Session,
    *,
    conversation_id: str,
    reason_codes: list[str],
    read_run_id: str,
) -> dict[str, Any]:
    """Close only temporary C2 gates proven healthy by a later full read."""

    requested = {
        str(value).strip()
        for value in reason_codes
        if str(value).strip() in RECOVERABLE_C2_HANDOFF_REASON_CODES
    }
    closed: list[HandoffEvent] = []
    closed_at = utcnow()
    for event in open_handoff_events_for_conversation(
        db,
        conversation_id,
        for_update=True,
    ):
        reason_code = str(event.handoff_reason_code or "").strip()
        if reason_code not in requested:
            continue
        evidence_refs = list(event.evidence_refs or [])
        recovery_ref = f"c2_recovery_read:{read_run_id}"
        if recovery_ref not in evidence_refs:
            evidence_refs.append(recovery_ref)
        event.evidence_refs = evidence_refs
        event.status = "auto_recovered_clean_read"
        event.closed_at = closed_at
        _record_handoff_closed_best_effort(
            db,
            event,
        )
        closed.append(event)
    db.flush()
    remaining = open_handoff_events_for_conversation(
        db,
        conversation_id,
        for_update=True,
    )
    return {
        "closed_count": len(closed),
        "closed_handoff_ids": [event.id for event in closed],
        "closed_reason_codes": sorted(
            {str(event.handoff_reason_code) for event in closed}
        ),
        "remaining_open_count": len(remaining),
    }


def enforce_open_handoff_gate(
    db: Session,
    conversation: Conversation,
    *,
    for_update: bool = False,
) -> list[HandoffEvent]:
    """Make persisted handoff state win over a stale conversation projection."""

    events = open_handoff_events_for_conversation(
        db,
        conversation.conversation_id,
        for_update=for_update,
    )
    if events and str(conversation.status or "") not in {
        "closed",
        "rejected",
    }:
        conversation.status = "waiting_sales_reply"
    elif not events and conversation.status == "waiting_sales_reply":
        logger.error(
            "handoff projection inconsistent conversation_id=%s error_code=%s",
            conversation.conversation_id,
            "HANDOFF_EVENT_MISSING_FOR_WAITING_SALES_REPLY",
        )
    return events


def close_open_handoffs_for_human_sales(
    db: Session,
    *,
    conversation_id: str,
    sales_message: MessageEvent,
    visible_message_orders: dict[str, int] | None = None,
    sales_screen_order: int | None = None,
) -> dict[str, Any]:
    """Close only handoffs that predate the confirmed human sales reply."""

    # observed_at only proves when Worker saw the bubble. A historical sales
    # message discovered in a fresh scan must not close a newer handoff.
    occurred_at = sales_message.occurred_at
    visible_orders = visible_message_orders or {}

    def comparable(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    closed: list[HandoffEvent] = []
    proof_sources: set[str] = set()
    resume_ai = False
    closed_at = utcnow()
    for event in open_handoff_events_for_conversation(
        db,
        conversation_id,
        for_update=True,
    ):
        batch = db.get(MessageBatch, event.batch_id) if event.batch_id else None
        proof_source = ""
        trigger_event_id = str(
            (batch.trigger_message_event_id if batch else None)
            or next(
                (
                    event_id
                    for event_id in reversed(event.trigger_message_event_ids or [])
                    if str(event_id or "").strip()
                ),
                "",
            )
        ).strip()
        trigger_screen_order = visible_orders.get(trigger_event_id)
        if (
            trigger_event_id
            and trigger_screen_order is not None
            and sales_screen_order is not None
        ):
            if int(sales_screen_order) <= int(trigger_screen_order):
                continue
            proof_source = "same_final_frame_order"
        else:
            time_evidence = (
                sales_message.raw_payload.get("occurred_at_evidence")
                if isinstance(sales_message.raw_payload, dict)
                and isinstance(
                    sales_message.raw_payload.get("occurred_at_evidence"),
                    dict,
                )
                else {}
            )
            time_is_reliable = (
                str(time_evidence.get("source") or "")
                == "wechat_message_timestamp"
                and float(time_evidence.get("confidence") or 0) >= 0.9
            )
            if (
                occurred_at is None
                or not time_is_reliable
                or (
                    event.created_at
                    and comparable(event.created_at) > comparable(occurred_at)
                )
            ):
                continue
            proof_source = "reliable_occurred_at"

        if batch and (batch.status == "paused" or batch.decision == "pause"):
            resume_ai = True
        evidence_refs = list(event.evidence_refs or [])
        sales_ref = f"sales_message_event:{sales_message.id}"
        if sales_ref not in evidence_refs:
            evidence_refs.append(sales_ref)
        proof_ref = f"sales_reply_order_proof:{proof_source}"
        if proof_ref not in evidence_refs:
            evidence_refs.append(proof_ref)
        event.evidence_refs = evidence_refs
        event.status = "sales_replied"
        event.closed_at = closed_at
        _record_handoff_closed_best_effort(
            db,
            event,
        )
        closed.append(event)
        proof_sources.add(proof_source)
    db.flush()
    remaining = open_handoff_events_for_conversation(
        db,
        conversation_id,
        for_update=True,
    )
    return {
        "closed_count": len(closed),
        "closed_handoff_ids": [event.id for event in closed],
        "remaining_open_count": len(remaining),
        "resume_ai": resume_ai,
        "time_order_proven": bool(closed),
        "reason": (
            "sales_message_after_handoff_proven"
            if closed
            else "sales_message_order_unproven"
        ),
        "proof_sources": sorted(proof_sources),
    }


def _create_handoff(
    db: Session,
    *,
    binding: WechatSessionBinding,
    conversation: Conversation,
    batch: MessageBatch,
    decision: AIEngineDecision,
    handoff_reason_code: str,
) -> HandoffEvent:
    handoff_payload = _preserve_generation_attempt_history(
        batch,
        _decision_payload(decision),
    )
    event, _created = _create_or_reuse_open_handoff(
        db,
        conversation=conversation,
        batch_id=batch.id,
        handoff_reason_code=handoff_reason_code,
        reason_detail=decision.handoff_reason_code or handoff_reason_code,
        trigger_message_event_ids=list(batch.message_event_ids or []),
        risk_flags=decision.risk_flags or [],
        evidence_refs=decision.evidence_refs or [],
        ai_payload=handoff_payload,
    )
    conversation.status = "waiting_sales_reply"
    conversation.recall_origin_status = None
    conversation.recall_cycle_id = None
    conversation.handoff_reason_code = handoff_reason_code
    conversation.handoff_at = utcnow()
    batch.status = "handoff_created"
    batch.active = False
    batch.retryable = False
    batch.decision = "handoff"
    batch.error_code = handoff_reason_code
    batch.suggested_action = "handoff"
    batch.ai_response_snapshot = handoff_payload
    batch.generated_at = utcnow()
    batch.generation_started_at = None
    return event


def _create_or_reuse_open_handoff(
    db: Session,
    *,
    conversation: Conversation,
    batch_id: str | None,
    handoff_reason_code: str,
    reason_detail: str | None,
    trigger_message_event_ids: list[str],
    risk_flags: list[str],
    evidence_refs: list[str],
    ai_payload: dict[str, Any],
) -> tuple[HandoffEvent, bool]:
    # PostgreSQL serializes concurrent handoff creation on the conversation
    # row. The partial unique index remains the final database guard.
    db.scalar(
        select(Conversation.conversation_id)
        .where(Conversation.conversation_id == conversation.conversation_id)
        .with_for_update()
    )
    existing = db.scalar(
        select(HandoffEvent)
        .where(
            HandoffEvent.conversation_id == conversation.conversation_id,
            HandoffEvent.closed_at.is_(None),
            HandoffEvent.deleted_at.is_(None),
        )
        .order_by(HandoffEvent.created_at.asc(), HandoffEvent.id.asc())
        .with_for_update()
    )
    if existing:
        existing_reason = str(existing.handoff_reason_code or "").strip()
        if (
            existing_reason in RECOVERABLE_C2_HANDOFF_REASON_CODES
            and handoff_reason_code not in RECOVERABLE_C2_HANDOFF_REASON_CODES
        ):
            existing.handoff_reason_code = handoff_reason_code
            existing.reason_detail = reason_detail
            existing.trigger_message_event_ids = list(
                dict.fromkeys(
                    list(existing.trigger_message_event_ids or [])
                    + trigger_message_event_ids
                )
            )
            existing.risk_flags = list(
                dict.fromkeys(list(existing.risk_flags or []) + risk_flags)
            )
            existing.evidence_refs = list(
                dict.fromkeys(list(existing.evidence_refs or []) + evidence_refs)
            )
            existing.ai_payload = ai_payload
        if conversation.status not in {"closed", "rejected"}:
            conversation.status = "waiting_sales_reply"
        return existing, False

    event = HandoffEvent(
        conversation_id=conversation.conversation_id,
        batch_id=batch_id,
        status="created",
        handoff_reason_code=handoff_reason_code,
        reason_detail=reason_detail,
        trigger_message_event_ids=trigger_message_event_ids,
        risk_flags=risk_flags,
        evidence_refs=evidence_refs,
        ai_payload=ai_payload,
        notify_status="pending",
    )
    db.add(event)
    db.flush()
    from app.services.observability_service import (
        process_run_id_for_handoff_event,
        record_server_stage_best_effort,
    )
    from app.core.request_id import get_request_id

    process_run_id = process_run_id_for_handoff_event(db, event)
    handoff_trace_id = get_request_id()
    record_server_stage_best_effort(
        db,
        process_run_id=process_run_id,
        conversation_id=event.conversation_id,
        stage_name="handoff.event_create",
        component="backend",
        duration_ms=0,
        status="succeeded",
        trace_id=handoff_trace_id,
        stable_key=event.id,
    )
    record_server_stage_best_effort(
        db,
        process_run_id=process_run_id,
        conversation_id=event.conversation_id,
        stage_name="handoff.wait_sales",
        component="backend",
        duration_ms=None,
        status="running",
        trace_id=handoff_trace_id,
        stable_key=event.id,
    )
    enqueue_handoff_notification(db, handoff_event_id=event.id)
    return event, True


def _pause_conversation_for_manual(
    db: Session,
    *,
    binding: WechatSessionBinding,
    conversation: Conversation,
    batch: MessageBatch,
    decision: AIEngineDecision,
) -> HandoffEvent:
    """Turn Brain pause into one explicit manual-takeover terminal state."""

    reason_code = str(
        decision.error_code
        or decision.handoff_reason_code
        or "AI_ENGINE_PAUSED_FOR_MANUAL_REVIEW"
    )[:64]
    event = _create_handoff(
        db,
        binding=binding,
        conversation=conversation,
        batch=batch,
        decision=decision,
        handoff_reason_code=reason_code,
    )
    conversation.ai_enabled = False
    batch.status = "paused"
    batch.decision = "pause"
    batch.error_code = reason_code
    batch.suggested_action = "sales_handoff"
    batch.ai_response_snapshot = _preserve_generation_attempt_history(
        batch,
        _decision_payload(decision),
    )
    return event


def _create_send_failure_handoff(
    db: Session,
    *,
    action: ReplyAction,
    conversation: Conversation,
    error_code: str,
    send_result: str,
) -> HandoffEvent:
    batch = db.get(MessageBatch, action.batch_id)
    event, _created = _create_or_reuse_open_handoff(
        db,
        conversation=conversation,
        batch_id=action.batch_id,
        handoff_reason_code=error_code,
        reason_detail=(
            UNKNOWN_SEND_TERMINAL_REMARK
            if send_result == "unknown"
            else FAILED_SEND_TERMINAL_REMARK
        ),
        trigger_message_event_ids=list(batch.message_event_ids or []) if batch else [],
        risk_flags=[f"send_{send_result}"],
        evidence_refs=[f"reply_action:{action.id}"],
        ai_payload={
            "reply_action_id": action.id,
            "send_result": send_result,
            "error_code": error_code,
        },
    )
    conversation.status = "waiting_sales_reply"
    conversation.handoff_reason_code = error_code
    conversation.handoff_at = utcnow()
    if batch:
        batch.status = "handoff_created"
        batch.active = False
        batch.retryable = False
        batch.decision = "handoff"
        batch.error_code = error_code
        batch.suggested_action = "handoff"
        batch.generated_at = utcnow()
        batch.generation_started_at = None
    return event


def handoff_unsent_reply_recovery_failure(
    db: Session,
    *,
    reply_action_id: str,
    error_code: str,
) -> HandoffEvent | None:
    action = db.get(ReplyAction, reply_action_id)
    if (
        not action
        or action.deleted_at
        or action.status not in {"draft", "guarding", "queued"}
    ):
        return None
    batch = db.get(MessageBatch, action.batch_id)
    if not batch or batch.deleted_at:
        return None
    existing = db.scalar(
        select(HandoffEvent).where(
            HandoffEvent.batch_id == batch.id,
            HandoffEvent.deleted_at.is_(None),
        )
    )
    if existing:
        return existing
    binding = _binding_or_404(db, action.conversation_id)
    conversation = _conversation_for_binding(db, binding)
    action.status = "cancelled"
    action.current = False
    action.error_code = error_code
    action.suggested_action = "handoff"
    decision = AIEngineDecision(
        decision="handoff",
        handoff_reason_code=error_code,
        risk_flags=["c2_reply_context_recovery_failed"],
        evidence_refs=[f"reply_action:{action.id}"],
        guard_result="handoff",
        error_code=error_code,
        suggested_action="handoff",
        raw_payload={"reply_action_id": action.id, "recovery_error_code": error_code},
    )
    return _create_handoff(
        db,
        binding=binding,
        conversation=conversation,
        batch=batch,
        decision=decision,
        handoff_reason_code=error_code,
    )


def create_deterministic_handoff_for_ingest(
    db: Session,
    *,
    conversation_id: str,
    message_event_ids: list[str],
    reason_codes: list[str],
    trigger_key: str,
    trace_id: str | None = None,
) -> dict[str, Any]:
    clean_reasons = list(dict.fromkeys(str(value).strip() for value in reason_codes if str(value).strip()))
    primary_reason = clean_reasons[0] if clean_reasons else "C2_FACT_FLOW_INCOMPLETE"
    if message_event_ids:
        batch_result = collect_customer_message_batch(
            db,
            conversation_id=conversation_id,
            message_event_ids=message_event_ids,
            trace_id=trace_id,
        )
    else:
        batch_result = create_control_message_batch(
            db,
            conversation_id=conversation_id,
            trigger_type="c2_safety_handoff",
            trigger_key=trigger_key,
            trace_id=trace_id,
        )
    if not batch_result or not batch_result.get("batch_id"):
        raise AppError("MESSAGE_BATCH_NOT_CREATED", "C2 安全门禁无法创建人工接管批次", 409)
    batch = db.scalar(
        select(MessageBatch)
        .where(MessageBatch.id == str(batch_result["batch_id"]), MessageBatch.deleted_at.is_(None))
        .with_for_update()
    )
    if not batch:
        raise AppError("MESSAGE_BATCH_NOT_FOUND", "C2 安全门禁批次不存在", 404)
    existing = db.scalar(
        select(HandoffEvent).where(HandoffEvent.batch_id == batch.id, HandoffEvent.deleted_at.is_(None))
    )
    if not existing:
        binding = _binding_or_404(db, conversation_id)
        conversation = _conversation_for_binding(db, binding)
        decision = AIEngineDecision(
            decision="handoff",
            handoff_reason_code=primary_reason,
            risk_flags=[value.lower() for value in clean_reasons],
            evidence_refs=[f"c2_flow_gate:{value}" for value in clean_reasons],
            guard_result="handoff",
            error_code=primary_reason,
            suggested_action="handoff",
            raw_payload={"flow_gate_errors": clean_reasons},
        )
        _create_handoff(
            db,
            binding=binding,
            conversation=conversation,
            batch=batch,
            decision=decision,
            handoff_reason_code=primary_reason,
        )
    db.flush()
    return {
        "batch_id": batch.id,
        "batch_status": batch.status,
        "next_step": "handoff",
        "batch": _batch_to_dict(batch),
        "error_code": primary_reason,
        "suggested_action": "handoff",
    }


def _create_chat_reply_task(db: Session, *, binding: WechatSessionBinding, action: ReplyAction) -> Task:
    existing = db.scalar(select(Task).where(Task.reply_action_id == action.id, Task.deleted_at.is_(None)))
    if existing:
        return existing
    task = Task(
        task_type=TaskType.chat_reply.value,
        status=TaskStatus.pending.value,
        lead_id=binding.lead_id,
        sales_id=binding.sales_id,
        worker_id=binding.worker_id,
        reply_action_id=action.id,
        remark="C3 AI 回复发送任务",
        created_by="system",
        updated_by="system",
    )
    db.add(task)
    db.flush()
    _write_event(db, task, TaskEventType.created, to_status=task.status, remark="reply_action 已通过 Guard，创建 chat_reply 任务")
    return task


def _aware_datetime(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def message_batch_generation_is_stale(batch: MessageBatch, *, now: datetime | None = None) -> bool:
    if batch.status != "generating":
        return False
    current = _aware_datetime(now or utcnow())
    started = _aware_datetime(batch.generation_started_at or batch.updated_at or batch.created_at)
    if current is None or started is None:
        return False
    stale_after = max(1.0, float(get_settings().c3_batch_stale_after_seconds))
    return (current - started).total_seconds() >= stale_after


def message_batch_retry_is_due(batch: MessageBatch, *, now: datetime | None = None) -> bool:
    if batch.status != "retry_wait" or not batch.retryable:
        return False
    current = _aware_datetime(now or utcnow())
    generated = _aware_datetime(batch.generated_at or batch.updated_at or batch.created_at)
    if current is None or generated is None:
        return False
    retry_delay = max(0.0, float(get_settings().c3_batch_retry_delay_seconds))
    return (current - generated).total_seconds() >= retry_delay


def _handoff_exhausted_generation(
    db: Session,
    *,
    batch: MessageBatch,
    last_error_code: str | None,
) -> HandoffEvent:
    existing = db.scalar(
        select(HandoffEvent).where(
            HandoffEvent.batch_id == batch.id,
            HandoffEvent.deleted_at.is_(None),
        )
    )
    if existing:
        return existing
    binding = _binding_or_404(db, batch.conversation_id)
    conversation = _conversation_for_binding(db, binding)
    decision = AIEngineDecision(
        decision="handoff",
        handoff_reason_code="AI_ENGINE_RETRY_EXHAUSTED",
        risk_flags=["ai_engine_retry_exhausted"],
        evidence_refs=[f"last_error:{last_error_code or 'unknown'}"],
        guard_result="handoff",
        error_code="AI_ENGINE_RETRY_EXHAUSTED",
        suggested_action="handoff",
        raw_payload={
            "last_error_code": last_error_code,
            "generation_attempt_count": int(batch.generation_attempt_count or 0),
        },
    )
    return _create_handoff(
        db,
        binding=binding,
        conversation=conversation,
        batch=batch,
        decision=decision,
        handoff_reason_code="AI_ENGINE_RETRY_EXHAUSTED",
    )


def _schedule_retry_or_handoff(
    db: Session,
    *,
    batch: MessageBatch,
    decision: AIEngineDecision,
) -> dict[str, Any]:
    if decision.error_code == "AI_CONTEXT_BUILD_FAILED":
        # A frozen-snapshot validation failure is deterministic.  Retrying the
        # same code immediately cannot repair it, and exhausting normal model
        # retries must never turn this technical fault into a sales handoff.
        batch.status = "failed"
        batch.active = False
        batch.retryable = True
        batch.decision = "retry_later"
        batch.error_code = "AI_CONTEXT_BUILD_FAILED"
        batch.suggested_action = "repair_context_bridge_then_retry_same_batch"
        batch.ai_response_snapshot = _preserve_generation_attempt_history(
            batch,
            _decision_payload(decision),
        )
        batch.generated_at = utcnow()
        batch.generation_started_at = None
        db.flush()
        return {
            "decision": "retry_later",
            "batch": _batch_to_dict(batch),
            "error_code": "AI_CONTEXT_BUILD_FAILED",
            "suggested_action": batch.suggested_action,
        }
    attempts = int(batch.generation_attempt_count or 0)
    max_attempts = max(1, int(get_settings().c3_batch_recovery_max_attempts))
    if attempts >= max_attempts:
        handoff = _handoff_exhausted_generation(
            db,
            batch=batch,
            last_error_code=decision.error_code,
        )
        db.flush()
        return {
            "decision": "handoff",
            "batch": _batch_to_dict(batch),
            "handoff_event_id": handoff.id,
            "handoff_event": _handoff_to_dict(handoff),
            "error_code": "AI_ENGINE_RETRY_EXHAUSTED",
            "suggested_action": "handoff",
        }

    batch.status = "retry_wait"
    batch.active = True
    batch.retryable = True
    batch.decision = "retry_later"
    batch.error_code = decision.error_code
    batch.suggested_action = "retry_later"
    batch.ai_response_snapshot = _preserve_generation_attempt_history(
        batch,
        _decision_payload(decision),
    )
    batch.generated_at = utcnow()
    batch.generation_started_at = None
    db.flush()
    return {
        "decision": "retry_later",
        "batch": _batch_to_dict(batch),
        "error_code": decision.error_code,
        "suggested_action": "retry_later",
    }


def claim_message_batch_generation(
    db: Session,
    *,
    batch_id: str,
    stale_only: bool = False,
    force: bool = False,
) -> dict[str, Any]:
    """Persist generation ownership before dispatching non-durable background work."""

    batch = db.scalar(
        select(MessageBatch)
        .where(MessageBatch.id == batch_id, MessageBatch.deleted_at.is_(None))
        .with_for_update()
    )
    if not batch:
        raise AppError("MESSAGE_BATCH_NOT_FOUND", "消息批次不存在", 404)
    if batch.status not in ACTIVE_BATCH_STATUSES and not force:
        return {"run": False, "terminal": True, "batch": _batch_to_dict(batch)}

    stale = message_batch_generation_is_stale(batch)
    retry_due = message_batch_retry_is_due(batch)
    if stale_only and not stale and not retry_due:
        return {"run": False, "terminal": False, "batch": _batch_to_dict(batch)}
    if batch.status == "retry_wait" and not retry_due and not force:
        return {"run": False, "terminal": False, "batch": _batch_to_dict(batch)}
    if batch.status == "generating" and not stale and not force:
        return {"run": False, "terminal": False, "batch": _batch_to_dict(batch)}

    max_attempts = max(1, int(get_settings().c3_batch_recovery_max_attempts))
    attempts = int(batch.generation_attempt_count or 0)
    if stale and attempts >= max_attempts and not force:
        _handoff_exhausted_generation(
            db,
            batch=batch,
            last_error_code=batch.error_code or "MESSAGE_BATCH_RECOVERY_EXHAUSTED",
        )
        db.flush()
        return {
            "run": False,
            "terminal": True,
            "error_code": "AI_ENGINE_RETRY_EXHAUSTED",
            "batch": _batch_to_dict(batch),
        }

    batch.status = "generating"
    batch.active = True
    batch.generation_attempt_count = attempts + 1
    batch.generation_started_at = utcnow()
    batch.retryable = False
    batch.error_code = None
    batch.suggested_action = "generate"
    from app.services.observability_service import (
        process_run_id_for_batch,
        record_server_stage_best_effort,
    )

    if batch.trigger_type != "recall":
        from app.core.request_id import get_request_id

        record_server_stage_best_effort(
            db,
            process_run_id=process_run_id_for_batch(db, batch),
            conversation_id=batch.conversation_id,
            stage_name="c3.brain_queued",
            component="backend",
            duration_ms=None,
            queued_at=batch.created_at,
            started_at=batch.generation_started_at,
            ended_at=batch.generation_started_at,
            trace_id=get_request_id(),
            stable_key=f"{batch.id}:{batch.generation_attempt_count}",
            attempt=batch.generation_attempt_count,
        )
    db.flush()
    return {
        "run": True,
        "terminal": False,
        "attempt": batch.generation_attempt_count,
        "recovery": stale,
        "batch": _batch_to_dict(batch),
    }


def generate_for_batch(
    db: Session,
    *,
    batch_id: str,
    force: bool = False,
    expected_generation_attempt: int | None = None,
) -> dict[str, Any]:
    batch = db.scalar(select(MessageBatch).where(MessageBatch.id == batch_id, MessageBatch.deleted_at.is_(None)).with_for_update())
    if not batch:
        raise AppError("MESSAGE_BATCH_NOT_FOUND", "消息批次不存在", 404)
    if expected_generation_attempt is not None and (
        batch.status != "generating"
        or int(batch.generation_attempt_count or 0) != int(expected_generation_attempt)
    ):
        return {
            "decision": batch.decision,
            "batch": _batch_to_dict(batch),
            "error_code": "MESSAGE_BATCH_GENERATION_CLAIM_STALE",
            "suggested_action": "use_current_batch_state",
        }

    existing_action = db.scalar(select(ReplyAction).where(ReplyAction.batch_id == batch.id, ReplyAction.current.is_(True)))
    existing_handoff = db.scalar(select(HandoffEvent).where(HandoffEvent.batch_id == batch.id, HandoffEvent.deleted_at.is_(None)))
    if not force and batch.status in {"reply_action_created", "handoff_created", "no_action", "failed", "rejected"}:
        existing_task = db.scalar(select(Task).where(Task.reply_action_id == existing_action.id, Task.deleted_at.is_(None))) if existing_action else None
        return {
            "decision": batch.decision,
            "batch": _batch_to_dict(batch),
            "reply_action_id": existing_action.id if existing_action else None,
            "reply_action": _reply_action_to_dict(existing_action),
            "task_id": existing_task.id if existing_task else None,
            "task": task_to_detail(get_task_or_404(db, existing_task.id)) if existing_task else None,
            "handoff_event_id": existing_handoff.id if existing_handoff else None,
            "handoff_event": _handoff_to_dict(existing_handoff),
            "error_code": batch.error_code,
            "suggested_action": batch.suggested_action,
        }

    binding = _binding_or_404(db, batch.conversation_id)
    conversation = _conversation_for_binding(db, binding)
    try:
        _ensure_conversation_eligible(binding, conversation)
    except AppError as exc:
        batch.status = "no_action"
        batch.active = False
        batch.decision = "no_action"
        batch.error_code = exc.code
        batch.suggested_action = "wait_for_business_gate"
        batch.generated_at = utcnow()
        _restore_conversation_after_no_action(conversation, batch)
        db.flush()
        return {
            "decision": "no_action",
            "batch": _batch_to_dict(batch),
            "error_code": exc.code,
            "suggested_action": "wait_for_business_gate",
        }

    batch.status = "generating"
    try:
        context = _build_ai_context(db, binding, conversation, batch)
    except AppError as exc:
        if exc.code != "AI_CONTEXT_BUILD_FAILED":
            raise
        return _schedule_retry_or_handoff(
            db,
            batch=batch,
            decision=AIEngineDecision(
                decision="retry_later",
                risk_flags=["ai_context_build_failed"],
                guard_result="failed",
                error_code="AI_CONTEXT_BUILD_FAILED",
                suggested_action="repair_context_bridge_then_retry_same_batch",
                raw_payload={
                    "context_error": {
                        "exception_type": type(exc).__name__,
                        "reason": str(exc.code),
                    }
                },
            ),
        )
    except Exception as exc:
        # Snapshot normalization is a pre-Provider contract boundary.  A
        # malformed persisted fact or serialization bug must never leave the
        # already-claimed batch in ``generating`` for the recovery loop to
        # redispatch indefinitely.
        return _schedule_retry_or_handoff(
            db,
            batch=batch,
            decision=AIEngineDecision(
                decision="retry_later",
                risk_flags=["ai_context_build_failed"],
                guard_result="failed",
                error_code="AI_CONTEXT_BUILD_FAILED",
                suggested_action="repair_context_bridge_then_retry_same_batch",
                raw_payload={
                    "context_error": {
                        "exception_type": type(exc).__name__,
                    }
                },
            ),
        )
    batch.ai_request_snapshot = context
    messages = context["messages"]
    if not messages and batch.trigger_type == "customer_message":
        batch.status = "no_action"
        batch.active = False
        batch.decision = "no_action"
        batch.error_code = "MESSAGE_BATCH_EMPTY"
        batch.suggested_action = "wait_more"
        batch.generated_at = utcnow()
        _restore_conversation_after_no_action(conversation, batch)
        db.flush()
        return {"decision": "no_action", "batch": _batch_to_dict(batch), "error_code": "MESSAGE_BATCH_EMPTY", "suggested_action": "wait_more"}

    # Persist the exact Brain input and release database row locks before the
    # provider network call. New customer/sales facts can then supersede this
    # generation while the model is thinking instead of waiting on our lock.
    generation_attempt = int(batch.generation_attempt_count or 0)
    previous_ai_response_snapshot = (
        dict(batch.ai_response_snapshot)
        if isinstance(batch.ai_response_snapshot, dict)
        else {}
    )
    prepared_batch_id = batch.id
    prepared_trigger_type = batch.trigger_type
    prepared_recall_cycle_id = batch.recall_cycle_id
    from app.services.observability_service import process_run_id_for_batch

    generation_process_run_id = process_run_id_for_batch(db, batch)
    db.flush()
    db.commit()

    generation_started_monotonic = time.perf_counter()
    try:
        decision = get_ai_engine_adapter().generate_reply_decision(
            conversation_context={
                **context["conversation"],
                "brain_context_snapshot": context["brain_context_snapshot"],
            },
            message_batch={
                "id": prepared_batch_id,
                "messages": messages,
                "trigger_type": prepared_trigger_type,
                "recall_cycle_id": prepared_recall_cycle_id,
                "generation_attempt": generation_attempt,
                "previous_ai_response_snapshot": previous_ai_response_snapshot,
            },
        )
    except AppError as exc:
        decision = AIEngineDecision(
            decision="retry_later",
            risk_flags=[exc.code.lower()],
            guard_result="failed",
            error_code=exc.code,
            suggested_action="retry_later",
            raw_payload={"provider_error": exc.data},
        )
    except Exception as exc:
        decision = AIEngineDecision(
            decision="retry_later",
            risk_flags=["ai_engine_exception"],
            guard_result="failed",
            error_code="AI_ENGINE_PROVIDER_FAILED",
            suggested_action="retry_later",
            raw_payload={"exception_type": type(exc).__name__},
        )
    generation_duration_ms = int(
        round((time.perf_counter() - generation_started_monotonic) * 1000)
    )

    batch = db.scalar(
        select(MessageBatch)
        .where(
            MessageBatch.id == prepared_batch_id,
            MessageBatch.deleted_at.is_(None),
        )
        .with_for_update()
    )
    if not batch:
        raise AppError("MESSAGE_BATCH_NOT_FOUND", "消息批次不存在", 404)
    from app.services.observability_service import record_server_stage_best_effort

    record_server_stage_best_effort(
        db,
        process_run_id=generation_process_run_id,
        conversation_id=batch.conversation_id,
        stage_name=(
            "c4.brain_generate"
            if prepared_trigger_type == "recall"
            else "c3.brain_generate"
        ),
        component="brain",
        attempt=max(1, generation_attempt),
        duration_ms=generation_duration_ms,
        status=(
            "failed"
            if decision.decision == "retry_later"
            and bool(decision.error_code)
            else "succeeded"
        ),
        error_code=decision.error_code,
        trace_id=str(uuid.uuid4()),
        stable_key=f"{batch.id}:{generation_attempt}",
    )
    if (
        batch.status != "generating"
        or int(batch.generation_attempt_count or 0) != generation_attempt
    ):
        batch.ai_response_snapshot = _record_stale_generation_attempt_diagnostics(
            batch,
            attempt=generation_attempt,
            payload=_stale_generation_diagnostics_payload(decision),
        )
        db.flush()
        return {
            "decision": batch.decision,
            "batch": _batch_to_dict(batch),
            "error_code": "MESSAGE_BATCH_GENERATION_CLAIM_STALE",
            "suggested_action": "use_current_batch_state",
        }
    binding = _binding_or_404(db, batch.conversation_id)
    conversation = _conversation_for_binding(db, binding)

    payload = _decision_payload(decision)
    batch.ai_response_snapshot = _record_generation_attempt(
        batch,
        attempt=generation_attempt,
        payload=payload,
    )

    if decision.decision == "hard_opt_out":
        evidence_event = _validated_hard_opt_out_event(
            db,
            batch=batch,
            evidence=decision.hard_opt_out_evidence,
        )
        if evidence_event is not None:
            return _reject_conversation_for_hard_opt_out(
                db,
                binding=binding,
                conversation=conversation,
                batch=batch,
                decision=decision,
                evidence_event=evidence_event,
            )
        decision = AIEngineDecision(
            decision="retry_later",
            risk_flags=["hard_opt_out_evidence_invalid"],
            guard_result="failed",
            error_code="AI_ENGINE_HARD_OPT_OUT_EVIDENCE_INVALID",
            suggested_action="retry_later",
            raw_payload=payload,
        )
        payload = _decision_payload(decision)
        batch.ai_response_snapshot = _preserve_generation_attempt_history(
            batch,
            payload,
        )

    if decision.decision in {"send_reply", "reply_then_handoff"}:
        reply_then_handoff = decision.decision == "reply_then_handoff"
        handoff_decision = decision if reply_then_handoff else None
        final_reply_text = _final_send_text(decision.reply_text)
        valid_guard_results = (
            {"handoff", "pass", "rewrite_passed"}
            if reply_then_handoff
            else {"pass", "rewrite_passed"}
        )
        if not final_reply_text or decision.guard_result not in valid_guard_results:
            decision = AIEngineDecision(
                decision="retry_later",
                risk_flags=["ai_contract_invalid"],
                guard_result="failed",
                error_code="AI_ENGINE_CONTRACT_INVALID",
                suggested_action="retry_later",
                raw_payload=payload,
            )
        else:
            _supersede_open_actions(db, binding.conversation_id, reason="生成新的当前回复动作")
            expire_at = utcnow() + timedelta(seconds=get_settings().c3_reply_action_ttl_seconds)
            action = ReplyAction(
                batch_id=batch.id,
                conversation_id=binding.conversation_id,
                status="queued",
                current=True,
                generation_no=batch.generation_no,
                decision=(
                    "reply_then_handoff"
                    if reply_then_handoff
                    else "send_reply"
                ),
                reply_text=final_reply_text,
                reply_text_hash=_hash_text(final_reply_text),
                confidence=decision.confidence,
                risk_flags=decision.risk_flags or [],
                evidence_refs=decision.evidence_refs or [],
                guard_result=decision.guard_result,
                expire_at=expire_at,
                ai_payload=payload,
            )
            db.add(action)
            db.flush()
            try:
                _snapshot_action_vehicle_facts(db, action=action, payload=payload)
            except AppError as exc:
                db.delete(action)
                db.flush()
                if handoff_decision is not None:
                    handoff_reason_code = (
                        handoff_decision.error_code
                        or handoff_decision.handoff_reason_code
                        or "HANDOFF_REQUIRED"
                    )
                    handoff = _create_handoff(
                        db,
                        binding=binding,
                        conversation=conversation,
                        batch=batch,
                        decision=handoff_decision,
                        handoff_reason_code=handoff_reason_code,
                    )
                    db.flush()
                    return {
                        "decision": "handoff",
                        "batch": _batch_to_dict(batch),
                        "handoff_event_id": handoff.id,
                        "handoff_event": _handoff_to_dict(handoff),
                        "error_code": handoff_reason_code,
                        "suggested_action": "handoff",
                    }
                decision = AIEngineDecision(
                    decision="retry_later",
                    risk_flags=["vehicle_fact_stale"],
                    guard_result="failed",
                    error_code=exc.code,
                    suggested_action="retry_later",
                    raw_payload={"brain_decision": payload, "vehicle_ids": exc.data.get("vehicle_ids", [])},
                )
                payload = _decision_payload(decision)
                batch.ai_response_snapshot = _preserve_generation_attempt_history(
                    batch,
                    payload,
                )
            else:
                task = _create_chat_reply_task(db, binding=binding, action=action)
                batch.status = "reply_action_created"
                batch.active = False
                batch.retryable = False
                batch.decision = action.decision
                batch.error_code = (
                    decision.handoff_reason_code
                    if reply_then_handoff
                    else None
                )
                batch.suggested_action = "claim_send"
                batch.generated_at = utcnow()
                handoff = None
                if reply_then_handoff:
                    handoff_reason_code = (
                        decision.error_code
                        or decision.handoff_reason_code
                        or "HANDOFF_REQUIRED"
                    )
                    handoff = _create_handoff(
                        db,
                        binding=binding,
                        conversation=conversation,
                        batch=batch,
                        decision=decision,
                        handoff_reason_code=handoff_reason_code,
                    )
                    # The handoff is already authoritative, but its one approved
                    # boundary reply must remain claimable and sendable.
                    batch.status = "reply_action_created"
                    batch.active = False
                    batch.retryable = False
                    batch.decision = "reply_then_handoff"
                    batch.error_code = handoff_reason_code
                    batch.suggested_action = "claim_send"
                    batch.generated_at = utcnow()
                db.flush()
                result = {
                    "decision": action.decision,
                    "batch": _batch_to_dict(batch),
                    "reply_action_id": action.id,
                    "reply_action": _reply_action_to_dict(action),
                    "task_id": task.id,
                    "task": task_to_detail(get_task_or_404(db, task.id)),
                    "error_code": batch.error_code,
                    "suggested_action": "claim_send",
                }
                if handoff is not None:
                    result["handoff_event_id"] = handoff.id
                    result["handoff_event"] = _handoff_to_dict(handoff)
                return result

    if decision.decision in {"handoff", "handoff_for_approval"}:
        handoff_reason_code = decision.error_code or decision.handoff_reason_code or "HANDOFF_REQUIRED"
        handoff = _create_handoff(
            db,
            binding=binding,
            conversation=conversation,
            batch=batch,
            decision=decision,
            handoff_reason_code=handoff_reason_code,
        )
        db.flush()
        return {
            "decision": "handoff",
            "batch": _batch_to_dict(batch),
            "handoff_event_id": handoff.id,
            "handoff_event": _handoff_to_dict(handoff),
            "error_code": handoff_reason_code,
            "suggested_action": "handoff",
        }

    if decision.decision in {"no_action", "pause", "retry_later"}:
        if decision.decision == "retry_later":
            return _schedule_retry_or_handoff(db, batch=batch, decision=decision)
        if decision.decision == "pause":
            handoff = _pause_conversation_for_manual(
                db,
                binding=binding,
                conversation=conversation,
                batch=batch,
                decision=decision,
            )
            db.flush()
            return {
                "decision": "pause",
                "batch": _batch_to_dict(batch),
                "handoff_event_id": handoff.id,
                "handoff_event": _handoff_to_dict(handoff),
                "error_code": batch.error_code,
                "suggested_action": "sales_handoff",
            }
        batch.status = "no_action" if decision.decision == "no_action" else "failed"
        batch.active = False
        batch.retryable = False
        batch.decision = decision.decision
        batch.error_code = decision.error_code
        batch.suggested_action = decision.suggested_action or decision.decision
        batch.ai_response_snapshot = _preserve_generation_attempt_history(
            batch,
            payload,
        )
        batch.generated_at = utcnow()
        if decision.decision == "no_action":
            _restore_conversation_after_no_action(conversation, batch)
        db.flush()
        return {
            "decision": decision.decision,
            "batch": _batch_to_dict(batch),
            "error_code": decision.error_code,
            "suggested_action": batch.suggested_action,
        }

    decision = AIEngineDecision(
        decision="retry_later",
        risk_flags=["ai_contract_invalid"],
        guard_result="failed",
        error_code="AI_ENGINE_CONTRACT_INVALID",
        suggested_action="retry_later",
        raw_payload=payload,
    )
    return _schedule_retry_or_handoff(db, batch=batch, decision=decision)


def validate_chat_reply_task_claim(
    db: Session,
    task: Task,
    worker: Worker,
    *,
    claim_source: str | None,
    conversation_id: str | None,
) -> None:
    if task.task_type != TaskType.chat_reply.value:
        return
    if not task.reply_action_id:
        raise AppError("REPLY_ACTION_NOT_FOUND", "chat_reply 任务缺少 reply_action_id", 409, {"suggested_action": "cancel_task"})
    action = db.get(ReplyAction, task.reply_action_id)
    if not action or action.deleted_at:
        raise AppError("REPLY_ACTION_NOT_FOUND", "reply_action 不存在", 404, {"suggested_action": "cancel_task"})
    if str(claim_source or "") != "c2_conversation_flow" or str(conversation_id or "") != action.conversation_id:
        raise AppError(
            "C2_REPLY_TASK_FLOW_OWNERSHIP_REQUIRED",
            "chat_reply 只能由持有当前会话 UI 锁的 C2 流程领取",
            409,
            {"conversation_id": action.conversation_id, "suggested_action": "claim_from_c2_conversation_flow"},
        )
    if action.status != "queued":
        raise AppError("REPLY_ACTION_CLAIM_CONFLICT", "reply_action 当前状态不允许领取任务", 409, {"status": action.status, "suggested_action": "do_not_send"})
    if _is_past(action.expire_at):
        action.status = "expired"
        action.current = False
        task.status = TaskStatus.cancelled.value
        task.cancel_reason = "reply_action 已过期"
        task.cancelled_at = utcnow()
        finish_task_and_release_worker(task)
        raise AppError("REPLY_ACTION_EXPIRED", "回复动作已过期", 409, {"suggested_action": "do_not_send"})
    if task.worker_id and task.worker_id != worker.id:
        raise AppError("TASK_WORKER_MISMATCH", "该 chat_reply 任务已指定其他 Worker", 409, {"suggested_action": "do_not_send"})


def claim_send(
    db: Session,
    *,
    reply_action_id: str,
    task_id: str,
    worker_id: str,
    client_instance_id: str | None,
    lease_fencing_token: int | None,
) -> dict[str, Any]:
    # Local import avoids the c3 <-> wechat service module cycle while keeping
    # the authorization algorithm owned by exactly one backend implementation.
    from app.services.wechat_service import _authorization_revision

    # Vehicle mutations lock Product Master first and then dependent actions.
    # Keep the same order here so a concurrent unlist/update cannot deadlock
    # with the final send authorization check.
    locked_vehicles = _lock_vehicle_facts_for_action(db, reply_action_id)
    action = db.scalar(select(ReplyAction).where(ReplyAction.id == reply_action_id, ReplyAction.deleted_at.is_(None)).with_for_update())
    if not action:
        raise AppError("REPLY_ACTION_NOT_FOUND", "reply_action 不存在", 404)
    batch = db.get(MessageBatch, action.batch_id)
    if not batch or batch.deleted_at:
        raise AppError("MESSAGE_BATCH_NOT_FOUND", "reply_action 批次不存在", 404)
    task = db.scalar(select(Task).where(Task.id == task_id, Task.deleted_at.is_(None)).with_for_update())
    if not task or task.reply_action_id != action.id:
        raise AppError("TASK_NOT_FOUND", "chat_reply 任务不存在或不匹配", 404, {"suggested_action": "do_not_send"})
    if task.task_type != TaskType.chat_reply.value:
        raise AppError("TASK_TYPE_NOT_SUPPORTED", "仅 chat_reply 任务支持 claim-send", 400)
    if task.status != TaskStatus.running.value:
        raise AppError("REPLY_ACTION_CLAIM_CONFLICT", "Worker 必须先领取 chat_reply 任务再 claim-send", 409, {"suggested_action": "claim_task_first"})
    if task.worker_id != worker_id:
        raise AppError("TASK_WORKER_MISMATCH", "任务 Worker 不匹配", 409, {"suggested_action": "do_not_send"})
    from app.services.task_service import validate_task_lease

    validate_task_lease(
        task,
        worker_id=worker_id,
        client_instance_id=client_instance_id,
        lease_fencing_token=lease_fencing_token,
    )
    if (
        action.status == "sending"
        and action.claimed_task_id == task.id
        and action.claimed_by_worker_id == worker_id
        and action.send_token
    ):
        binding = _binding_or_404(db, action.conversation_id)
        return {
            "reply_action_id": action.id,
            "task_id": task.id,
            "send_token": action.send_token,
            "reply_text": action.reply_text,
            "reply_text_hash": action.reply_text_hash,
            "conversation_id": action.conversation_id,
            "rpa_session_key": binding.rpa_session_key,
            "remark_code": binding.remark_code,
            "authorization_revision": _authorization_revision(binding),
            "expire_at": action.expire_at,
            "duplicated": True,
            "suggested_action": "reconcile_sent_ack_without_resend",
            **_pre_send_fact_checkpoint_response(batch, action),
        }
    if action.status != "queued":
        raise AppError("REPLY_ACTION_CLAIM_CONFLICT", "reply_action 已被领取或不可发送", 409, {"status": action.status, "suggested_action": "do_not_send"})
    stale_vehicle_ids = _stale_action_vehicle_ids(db, action, locked_vehicles=locked_vehicles)
    if stale_vehicle_ids:
        _reject_stale_vehicle_reply(db, action, stale_vehicle_ids=stale_vehicle_ids)
    if _is_past(action.expire_at):
        action.status = "expired"
        action.current = False
        task.status = TaskStatus.cancelled.value
        task.cancel_reason = "reply_action 已过期"
        task.cancelled_at = utcnow()
        finish_task_and_release_worker(task)
        raise AppError("REPLY_ACTION_EXPIRED", "回复动作已过期", 409, {"suggested_action": "do_not_send"})
    binding = _binding_or_404(db, action.conversation_id)
    conversation = _conversation_for_binding(db, binding)
    _ensure_reply_action_send_eligible(
        db,
        binding=binding,
        conversation=conversation,
        action=action,
    )
    send_token = secrets.token_urlsafe(32)
    action.status = "sending"
    action.send_token = send_token
    action.claimed_by_worker_id = worker_id
    action.claimed_task_id = task.id
    action.sending_claimed_at = utcnow()
    task.current_step = "reply_action_claimed"
    _write_event(db, task, TaskEventType.step_updated, from_status=task.status, to_status=task.status, worker_id=worker_id, remark="claim-send 成功")
    db.flush()
    return {
        "reply_action_id": action.id,
        "task_id": task.id,
        "send_token": send_token,
        "reply_text": action.reply_text,
        "reply_text_hash": action.reply_text_hash,
        "conversation_id": action.conversation_id,
        "rpa_session_key": binding.rpa_session_key,
        "remark_code": binding.remark_code,
        "display_name": binding.display_name,
        "authorization_revision": _authorization_revision(binding),
        "expire_at": action.expire_at,
        "suggested_action": "send_via_worker",
        **_pre_send_fact_checkpoint_response(batch, action),
    }


def sent_ack(db: Session, *, reply_action_id: str, payload: Any) -> dict[str, Any]:
    existing = db.scalar(select(SentAck).where(SentAck.reply_action_id == reply_action_id))
    if existing:
        return {"duplicated": True, "ack": _sent_ack_to_dict(existing), "error_code": "SEND_ACK_DUPLICATED", "suggested_action": "use_existing_ack"}

    action = db.scalar(select(ReplyAction).where(ReplyAction.id == reply_action_id, ReplyAction.deleted_at.is_(None)).with_for_update())
    if not action:
        raise AppError("REPLY_ACTION_NOT_FOUND", "reply_action 不存在", 404)
    task = db.scalar(select(Task).where(Task.id == payload.task_id, Task.deleted_at.is_(None)).with_for_update())
    if not task or task.reply_action_id != action.id:
        raise AppError("TASK_NOT_FOUND", "chat_reply 任务不存在或不匹配", 404)
    if action.status == "unknown_send_result":
        if payload.send_token != action.send_token:
            raise AppError(
                "REPLY_ACTION_CLAIM_CONFLICT",
                "send_token 不匹配",
                409,
                {"suggested_action": "do_not_retry_send"},
            )
        ack = SentAck(
            reply_action_id=action.id,
            task_id=task.id,
            worker_id=payload.worker_id,
            client_instance_id=payload.client_instance_id,
            send_token=payload.send_token,
            send_result="unknown",
            action_phase="trigger_attempted",
            reply_text_hash=payload.reply_text_hash,
            sidecar_run_id=payload.sidecar_run_id,
            evidence={
                **(payload.evidence or {}),
                "backend_terminal_reconciled": True,
                "reported_send_result": payload.send_result,
                "reported_action_phase": payload.action_phase,
            },
            error_code=action.error_code or payload.error_code or "SEND_RESULT_UNKNOWN",
            remark=UNKNOWN_SEND_TERMINAL_REMARK,
            sent_at=payload.sent_at,
        )
        db.add(ack)
        db.flush()
        return {
            "duplicated": True,
            "ack": _sent_ack_to_dict(ack),
            "reply_action": _reply_action_to_dict(action),
            "task": task_to_detail(get_task_or_404(db, task.id)),
            "error_code": "SEND_ACK_RECONCILED_TO_UNKNOWN_TERMINAL",
            "suggested_action": "confirm_ack_without_resend",
        }
    if action.status != "sending":
        raise AppError("REPLY_ACTION_CLAIM_CONFLICT", "reply_action 未处于 sending 状态，不能回执", 409, {"status": action.status, "suggested_action": "do_not_retry_send"})
    if payload.send_token != action.send_token:
        raise AppError("REPLY_ACTION_CLAIM_CONFLICT", "send_token 不匹配", 409, {"suggested_action": "do_not_retry_send"})
    if payload.reply_text_hash and action.reply_text_hash and payload.reply_text_hash != action.reply_text_hash:
        payload.error_code = payload.error_code or "SEND_TEXT_HASH_MISMATCH"
        payload.send_result = "unknown"
        payload.action_phase = "trigger_attempted"

    ack = SentAck(
        reply_action_id=action.id,
        task_id=task.id,
        worker_id=payload.worker_id,
        client_instance_id=payload.client_instance_id,
        send_token=payload.send_token,
        send_result=payload.send_result,
        action_phase=payload.action_phase,
        reply_text_hash=payload.reply_text_hash,
        sidecar_run_id=payload.sidecar_run_id,
        evidence=payload.evidence or {},
        error_code=payload.error_code,
        remark=payload.remark,
        sent_at=payload.sent_at,
    )
    db.add(ack)

    before = task.status
    binding = _binding_or_404(db, action.conversation_id)
    conversation = _conversation_for_binding(db, binding)
    if payload.send_result == "sent":
        action.status = "sent"
        action.sent_at = payload.sent_at or utcnow()
        pending_ai_events = db.scalars(
            select(MessageEvent).where(
                MessageEvent.conversation_id == action.conversation_id,
                MessageEvent.sender_role.in_({"self", "sales", "sales_candidate"}),
            )
        ).all()
        for event in pending_ai_events:
            raw = event.raw_payload if isinstance(event.raw_payload, dict) else {}
            if (
                str(raw.get("ai_reply_action_id") or "") == action.id
                and str(raw.get("sender_source") or "") == "ai_pending_ack"
            ):
                event.raw_payload = {
                    **raw,
                    "sender_source": "ai",
                }
        task.status = TaskStatus.completed.value
        task.result_code = TaskResultCode.chat_reply_sent.value
        task.error_code = None
        task.completed_at = action.sent_at
        conversation.reply_count = (conversation.reply_count or 0) + 1
        conversation.last_outbound_at = action.sent_at
        conversation.last_ai_reply_at = action.sent_at
        batch = db.get(MessageBatch, action.batch_id)
        claim_boundary = _aware_datetime(action.sending_claimed_at)
        last_inbound = _aware_datetime(conversation.last_inbound_at)
        last_sales = _aware_datetime(conversation.last_sales_reply_at)
        newer_customer_turn = bool(
            claim_boundary and last_inbound and last_inbound > claim_boundary
        )
        newer_sales_turn = bool(
            claim_boundary and last_sales and last_sales > claim_boundary
        )
        if action.decision == "reply_then_handoff":
            open_handoffs = open_handoff_events_for_conversation(
                db,
                action.conversation_id,
                for_update=True,
            )
            if open_handoffs:
                conversation.status = "waiting_sales_reply"
                conversation.next_recall_at = None
        elif not newer_customer_turn and not newer_sales_turn:
            conversation.status = "waiting_user_reply"
            if batch and batch.trigger_type == "recall":
                conversation.status = "recalled_waiting_user"
                conversation.recall_origin_status = None
                conversation.recall_cycle_id = None
                local_today = action.sent_at.astimezone(
                    ZoneInfo("Asia/Shanghai")
                ).date()
                if conversation.recall_daily_date != local_today:
                    conversation.recall_daily_date = local_today
                    conversation.recall_daily_count = 0
                conversation.recall_count = int(conversation.recall_count or 0) + 1
                conversation.recall_daily_count = int(
                    conversation.recall_daily_count or 0
                ) + 1
            conversation.next_recall_at = action.sent_at + timedelta(
                hours=get_settings().c3_recall_after_hours
            )
        finish_task_and_release_worker(task)
        _write_event(db, task, TaskEventType.completed, from_status=before, to_status=task.status, worker_id=payload.worker_id, remark=payload.remark)
    elif payload.send_result == "failed":
        action.status = "failed"
        action.error_code = payload.error_code or "RPA_SEND_REPLY_FAILED"
        task.status = TaskStatus.failed.value
        task.error_code = action.error_code
        task.failure_step = "send_reply"
        task.failure_remark = payload.remark
        task.failed_at = utcnow()
        finish_task_and_release_worker(task)
        _write_event(db, task, TaskEventType.failed, from_status=before, to_status=task.status, worker_id=payload.worker_id, remark=payload.remark)
        if action.error_code not in TECHNICAL_SEND_FAILURE_NO_HANDOFF_CODES:
            _create_send_failure_handoff(
                db,
                action=action,
                conversation=conversation,
                error_code=action.error_code,
                send_result="failed",
            )
    else:
        action.status = "unknown_send_result"
        action.error_code = payload.error_code or "SEND_RESULT_UNKNOWN"
        task.status = TaskStatus.failed.value
        task.error_code = action.error_code
        task.failure_step = "send_reply_unknown"
        task.failure_remark = (
            payload.remark or UNKNOWN_SEND_TERMINAL_REMARK
        )
        task.failed_at = utcnow()
        finish_task_and_release_worker(task)
        _write_event(db, task, TaskEventType.failed, from_status=before, to_status=task.status, worker_id=payload.worker_id, remark=task.failure_remark)
        _create_send_failure_handoff(
            db,
            action=action,
            conversation=conversation,
            error_code=action.error_code,
            send_result="unknown",
        )

    db.flush()
    return {"duplicated": False, "ack": _sent_ack_to_dict(ack), "reply_action": _reply_action_to_dict(action), "task": task_to_detail(get_task_or_404(db, task.id))}


def recover_stale_sending_reply_action(
    db: Session,
    *,
    reply_action_id: str,
    now: datetime | None = None,
) -> bool:
    action = db.scalar(
        select(ReplyAction)
        .where(
            ReplyAction.id == reply_action_id,
            ReplyAction.deleted_at.is_(None),
        )
        .with_for_update()
    )
    if not action or action.status != "sending":
        return False
    claimed_at = _aware_datetime(action.sending_claimed_at or action.updated_at)
    current = _aware_datetime(now or utcnow())
    if not claimed_at or not current:
        return False
    stale_after = max(
        1.0,
        float(get_settings().c3_send_ack_stale_after_seconds),
    )
    if (current - claimed_at).total_seconds() < stale_after:
        return False
    existing_ack = db.scalar(
        select(SentAck).where(SentAck.reply_action_id == action.id)
    )
    if existing_ack:
        return False
    task = db.scalar(
        select(Task)
        .where(
            Task.reply_action_id == action.id,
            Task.deleted_at.is_(None),
        )
        .with_for_update()
    )
    binding = _binding_or_404(db, action.conversation_id)
    conversation = _conversation_for_binding(db, binding)
    action.status = "unknown_send_result"
    action.error_code = "SEND_ACK_TIMEOUT"
    if task and task.status == TaskStatus.running.value:
        before = task.status
        task.status = TaskStatus.failed.value
        task.error_code = "SEND_ACK_TIMEOUT"
        task.failure_step = "send_reply_unknown"
        task.failure_remark = (
            "客户端长时间未返回发送结果；"
            + UNKNOWN_SEND_TERMINAL_REMARK
        )
        task.failed_at = utcnow()
        finish_task_and_release_worker(task)
        _write_event(
            db,
            task,
            TaskEventType.failed,
            from_status=before,
            to_status=task.status,
            worker_id=action.claimed_by_worker_id,
            remark=task.failure_remark,
        )
    _create_send_failure_handoff(
        db,
        action=action,
        conversation=conversation,
        error_code="SEND_ACK_TIMEOUT",
        send_result="unknown",
    )
    db.flush()
    return True


def get_message_batch_for_worker(db: Session, *, worker: Worker, batch_id: str) -> dict[str, Any]:
    batch = db.scalar(
        select(MessageBatch).where(MessageBatch.id == batch_id, MessageBatch.deleted_at.is_(None))
    )
    if not batch:
        raise AppError("MESSAGE_BATCH_NOT_FOUND", "消息批次不存在", 404)
    binding = db.scalar(
        select(WechatSessionBinding).where(
            WechatSessionBinding.conversation_id == batch.conversation_id,
            WechatSessionBinding.worker_id == worker.id,
            WechatSessionBinding.deleted_at.is_(None),
        )
    )
    if not binding:
        raise AppError("MESSAGE_BATCH_WORKER_MISMATCH", "消息批次不属于当前 Worker 会话", 403)

    action = db.scalar(
        select(ReplyAction).where(
            ReplyAction.batch_id == batch.id,
            ReplyAction.current.is_(True),
            ReplyAction.deleted_at.is_(None),
        )
    )
    task = db.scalar(select(Task).where(Task.reply_action_id == action.id, Task.deleted_at.is_(None))) if action else None
    handoff = db.scalar(
        select(HandoffEvent).where(HandoffEvent.batch_id == batch.id, HandoffEvent.deleted_at.is_(None))
    )
    processing = batch.status in ACTIVE_BATCH_STATUSES
    continuation = message_batch_continuation_authorization(
        db,
        worker=worker,
        batch=batch,
        binding=binding,
    )

    return {
        "batch_id": batch.id,
        "batch_status": batch.status,
        "processing": processing,
        "terminal": not processing,
        "conversation_id": batch.conversation_id,
        "trigger_type": batch.trigger_type,
        "decision": batch.decision,
        "retryable": batch.retryable,
        "error_code": batch.error_code,
        "suggested_action": batch.suggested_action,
        "reply_action": _reply_action_to_dict(action),
        "task": task_to_detail(get_task_or_404(db, task.id)) if task else None,
        "handoff_event": _handoff_to_dict(handoff),
        "trace_id": batch.trace_id,
        "updated_at": batch.updated_at,
        "authorization": continuation,
        "continuation": continuation,
        **_pre_send_fact_checkpoint_response(batch, action),
    }
