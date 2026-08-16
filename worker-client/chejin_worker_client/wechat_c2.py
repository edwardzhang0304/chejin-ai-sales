from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from typing import Any

from .models import WechatReadTarget
from .message_identity_commit import (
    CommittedMessage,
    IdentityCommitRejection,
    MessageCommitBasis,
    committed_identity_record,
    commit_message_identity,
    require_committed_message,
)
from .c2_contract import (
    c2_contract_v3,
    contract_revision,
    contract_row_rules,
    contract_sha256,
    contract_values,
    observation_role_is_trusted,
    validate_slot_ledger_states,
)
from .storage import save_c2_state


IMAGE_PERSISTENCE_POLICY = dict(c2_contract_v3().get("image_persistence_policy") or {})
IMAGE_RUNTIME_FIELDS = set(IMAGE_PERSISTENCE_POLICY.get("forbidden_field_names") or [])

IMAGE_RUNTIME_FIELD_PREFIXES = (
    "provider_response",
    "raw_provider_response",
    "retry_response",
    "initial_response",
)

FORMAL_C2_REMARK_CODE_RE = re.compile(r"CJ[A-Z0-9]{6}")


def project_final_slot_flow_gates(
    incremental_plan: dict[str, Any],
    *,
    failed_voice_source_roles: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Project one incremental plan into the only Worker-owned flow gates."""

    errors: list[str] = []
    if incremental_plan.get("history_gap"):
        errors.append("C2_MESSAGE_HISTORY_GAP")
    if incremental_plan.get("identity_errors"):
        errors.append("MESSAGE_IDENTITY_UNCONFIRMED")
    details = [
        dict(item)
        for item in (incremental_plan.get("flow_gate_details") or [])
        if isinstance(item, dict)
    ]
    failed_roles = {
        str(source_key): str(role)
        for source_key, role in (failed_voice_source_roles or {}).items()
        if str(source_key).strip() and str(role) in {"customer", "self"}
    }
    if failed_roles:
        errors.append("C2_VOICE_TRANSCRIBE_FAILED")
        slots_by_source = {
            str(item.get("source_message_key") or ""): item
            for item in (
                incremental_plan.get("slot_ledger_states") or []
            )
            if isinstance(item, dict)
        }
        for role in ("customer", "self"):
            role_keys = sorted(
                source_key
                for source_key, sender_role in failed_roles.items()
                if sender_role == role
            )
            if not role_keys:
                continue
            role_slots = [
                slots_by_source.get(source_key)
                for source_key in role_keys
            ]
            role_slots = [
                item for item in role_slots if isinstance(item, dict)
            ]
            orders = sorted(
                {
                    int(item.get("screen_order") or 0)
                    for item in role_slots
                    if int(item.get("screen_order") or 0) > 0
                }
            )
            has_visual_order_proof = (
                len(role_slots) == len(role_keys)
                and bool(orders)
                and all(
                    item.get("order_source") == "visual_top"
                    for item in role_slots
                )
            )
            detail: dict[str, Any] = {
                "error_code": "C2_VOICE_TRANSCRIBE_FAILED",
                "position_source": (
                    "failed_voice_visual_top"
                    if has_visual_order_proof
                    else "position_unavailable"
                ),
                "subject_sender_role": role,
            }
            if has_visual_order_proof:
                detail["min_screen_order"] = orders[0]
                detail["max_screen_order"] = orders[-1]
            details.append(detail)
    return {
        "history_gap": bool(incremental_plan.get("history_gap")),
        "flow_gate_errors": errors,
        "flow_gate_details": details,
        "slot_ledger_states": list(
            incremental_plan.get("slot_ledger_states") or []
        ),
        "historical_warnings": list(
            incremental_plan.get("historical_warnings") or []
        ),
        "recoverable_handoff_resolution": incremental_plan.get(
            "recoverable_handoff_resolution"
        ),
    }


def _drop_image_runtime_fields(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _drop_image_runtime_fields(child)
            for key, child in value.items()
            if str(key).strip().lower() not in IMAGE_RUNTIME_FIELDS
            and not str(key).strip().lower().startswith(IMAGE_RUNTIME_FIELD_PREFIXES)
        }
    if isinstance(value, list):
        return [_drop_image_runtime_fields(item) for item in value]
    return value


def _image_text_list(value: Any, *, limit: int) -> list[str]:
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, list):
        values = value
    else:
        values = []
    return [str(item).strip() for item in values[:limit] if str(item).strip()]


def _image_float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _image_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _project_customer_image_understanding(value: Any) -> dict[str, Any]:
    data = value if isinstance(value, dict) else {}
    classification = data.get("classification") if isinstance(data.get("classification"), dict) else {}
    entities = data.get("entities") if isinstance(data.get("entities"), dict) else {}
    intent_hints = data.get("intent_hints") if isinstance(data.get("intent_hints"), dict) else {}
    bridge = data.get("bridge") if isinstance(data.get("bridge"), dict) else {}
    catalog = data.get("catalog_alignment") if isinstance(data.get("catalog_alignment"), dict) else {}
    audit = data.get("audit") if isinstance(data.get("audit"), dict) else {}
    projected = {
        "schema_version": int(data.get("schema_version") or 0),
        "enabled": bool(data.get("enabled", True)),
        "applied": bool(data.get("applied", False)),
        "adoptable": bool(data.get("adoptable", False)),
        "reason": str(data.get("reason") or "")[:160],
        "provider": str(data.get("provider") or "")[:300],
        "request_style": str(data.get("request_style") or "")[:80],
        "model": str(data.get("model") or "")[:160],
        "vision_summary": str(data.get("vision_summary") or "").strip()[:2000],
        "image_ocr_text": _image_text_list(data.get("image_ocr_text"), limit=20),
        "classification": {
            "is_vehicle": bool(classification.get("is_vehicle", False)),
            "vehicle_confidence": _image_float(classification.get("vehicle_confidence")),
            "unknown": bool(classification.get("unknown", False)),
            "non_vehicle_reason": str(classification.get("non_vehicle_reason") or "")[:300],
        },
        "entities": {
            "brand_candidates": _image_text_list(entities.get("brand_candidates"), limit=8),
            "series_candidates": _image_text_list(entities.get("series_candidates"), limit=8),
            "model_clues": _image_text_list(entities.get("model_clues"), limit=12),
            "body_type": str(entities.get("body_type") or "")[:120],
            "color": str(entities.get("color") or "")[:120],
            "year_clues": _image_text_list(entities.get("year_clues"), limit=8),
        },
        "intent_hints": {
            "wants_catalog_match": bool(intent_hints.get("wants_catalog_match", False)),
            "wants_similar_recommendation": bool(intent_hints.get("wants_similar_recommendation", False)),
            "wants_general_chat": bool(intent_hints.get("wants_general_chat", False)),
            "needs_clarification": bool(intent_hints.get("needs_clarification", False)),
        },
        "bridge": {
            "normalized_vehicle_query": str(bridge.get("normalized_vehicle_query") or "")[:500],
            "brain_mode": str(bridge.get("brain_mode") or "")[:120],
            "catalog_lookup_mode": str(bridge.get("catalog_lookup_mode") or "")[:120],
        },
        "catalog_alignment": {
            "selected_product_id": str(catalog.get("selected_product_id") or "")[:128],
            "selected_product_name": str(catalog.get("selected_product_name") or "")[:200],
            "alignment_confidence": _image_float(catalog.get("alignment_confidence")),
            "alignment_reason": str(catalog.get("alignment_reason") or "")[:500],
            "uncertain_reason": str(catalog.get("uncertain_reason") or "")[:500],
        },
        "audit": {
            "latency_ms": _image_int(audit.get("latency_ms")),
            "used_fallback": bool(audit.get("used_fallback", False)),
            "provider_error": str(audit.get("provider_error") or "")[:300],
            "retry_error": str(audit.get("retry_error") or "")[:300],
            "retry_after_non_json": bool(audit.get("retry_after_non_json", False)),
            "catalog_identity_candidate_count": _image_int(audit.get("catalog_identity_candidate_count")),
        },
    }
    return _drop_image_runtime_fields(projected)


def _project_visual_bridge_input(value: Any) -> dict[str, Any]:
    from apps.wechat_ai_customer_service.optional_plugins.vision.projection.brain import (
        compact_customer_image_brain_bridge,
    )

    return {
        "schema_version": 1,
        **_drop_image_runtime_fields(
            compact_customer_image_brain_bridge(
                value if isinstance(value, dict) else {}
            )
        ),
    }



def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def stable_digest(payload: Any, length: int = 16) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:length]


def is_formal_c2_remark_code(value: Any) -> bool:
    return isinstance(value, str) and bool(FORMAL_C2_REMARK_CODE_RE.fullmatch(value))


def row_fingerprint(value: Any) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()[:255]
    if value:
        return stable_digest(value, length=24)
    return ""


def sidecar_run_id(payload: dict[str, Any], prefix: str) -> str:
    for key in ("sidecar_run_id", "run_id", "trace_id"):
        value = str(payload.get(key) or "").strip()
        if value:
            return value[:128]
    return f"{prefix}-{stable_digest(payload, length=20)}"


def build_scan_result_payload(
    sidecar_payload: dict[str, Any],
    *,
    error_code: str | None = None,
) -> dict[str, Any]:
    started_at = now_iso()
    sessions = sidecar_payload.get("sessions") if isinstance(sidecar_payload.get("sessions"), list) else []
    mapped: list[dict[str, Any]] = []
    admission_counts = {"private": 0, "group": 0, "unknown": 0}
    missing_session_key_excluded_count = 0
    contract_rejections: list[dict[str, str]] = []
    for item in sessions:
        if not isinstance(item, dict):
            continue
        display_name = str(item.get("name") or item.get("title") or item.get("display_name") or "").strip()
        if not display_name:
            contract_rejections.append(
                {
                    "rpa_session_key": str(item.get("session_key") or "")[:255],
                    "reason": "display_name_missing",
                }
            )
            continue
        rpa_session_key = str(item.get("session_key") or "").strip()
        if not rpa_session_key:
            missing_session_key_excluded_count += 1
            contract_rejections.append(
                {
                    "rpa_session_key": "",
                    "reason": "session_key_missing",
                }
            )
            continue
        admitted_codes, admission_type, contract_error = validate_sidecar_c2_identity(item)
        if contract_error:
            contract_rejections.append(
                {
                    "rpa_session_key": rpa_session_key[:255],
                    "reason": contract_error,
                }
            )
        counted_type = (
            "private"
            if admitted_codes
            else admission_type
            if admission_type in {"group", "unknown"}
            else "unknown"
        )
        admission_counts[counted_type] += 1
        preview = str(item.get("content") or item.get("preview") or item.get("last_message_preview") or "")
        fingerprint = row_fingerprint(item.get("row_fingerprint"))
        mapped.append(
            {
                "rpa_session_key": rpa_session_key,
                "display_name": display_name[:255],
                # Copy only the identity accepted by the Sidecar contract. Worker
                # must not derive a replacement identity from display/preview text.
                "remark_code_candidates": admitted_codes,
                "row_fingerprint": fingerprint or None,
                "unread_hint": bool(item.get("unread_signal") or item.get("unread") or item.get("unread_badge")),
                "last_message_preview": preview[:1000] or None,
                "last_message_preview_time": str(
                    item.get("time")
                    or item.get("last_message_preview_time")
                    or ""
                )[:64]
                or None,
                "last_message_observation_id": str(
                    item.get("last_message_observation_id")
                    or ""
                )[:255]
                or None,
                "ocr_confidence": item.get("ocr_confidence"),
            }
        )
    payload = {
        "scan_id": str(sidecar_payload.get("scan_id") or "").strip()
        or f"scan-{uuid.uuid4()}",
        "sidecar_run_id": sidecar_run_id(sidecar_payload, "sessions"),
        "wechat_account_hint": str(((sidecar_payload.get("window_probe") or {}).get("title") if isinstance(sidecar_payload.get("window_probe"), dict) else "") or "")[:128] or None,
        "started_at": started_at,
        "finished_at": now_iso(),
        "sessions": mapped,
        "evidence": {
            "screenshot": sidecar_payload.get("screenshot_path"),
            "artifact_dir": sidecar_payload.get("artifact_dir"),
            "adapter": sidecar_payload.get("adapter"),
            "state": sidecar_payload.get("state"),
            "ocr_items_count": sidecar_payload.get("ocr_items_count"),
            "c2_conversation_admission": {
                "rule_owner": "omniauto.win32_ocr.text_normalization",
                "private_candidate_count": admission_counts["private"],
                "group_excluded_count": admission_counts["group"],
                "unknown_excluded_count": admission_counts["unknown"],
                "missing_session_key_excluded_count": missing_session_key_excluded_count,
                "contract_rejected_count": len(contract_rejections),
                "contract_rejections": contract_rejections[:20],
                "rule": "omniauto_authoritative_identity_contract_only",
            },
        },
        "scan_failed": not bool(sidecar_payload.get("ok")),
        "error_code": error_code or (None if sidecar_payload.get("ok") else str(sidecar_payload.get("error_code") or sidecar_payload.get("state") or "SESSION_SCAN_FAILED")),
    }
    save_c2_state("last_scan", {"scan_id": payload["scan_id"], "sidecar_run_id": payload["sidecar_run_id"], "session_count": len(mapped), "finished_at": payload["finished_at"]})
    return payload


def validate_sidecar_c2_identity(
    item: dict[str, Any],
) -> tuple[list[str], str, str | None]:
    candidates_raw = item.get("c2_remark_code_candidates")
    admission = item.get("c2_conversation_admission")
    if not isinstance(candidates_raw, list):
        return [], "unknown", "remark_code_candidates_missing"
    if not isinstance(admission, dict):
        return [], "unknown", "conversation_admission_missing"

    conversation_type = str(admission.get("conversation_type") or "")
    allowed = admission.get("admission_allowed")
    reason = str(admission.get("reason") or "").strip()
    if conversation_type not in {"private", "group", "unknown"}:
        return [], "unknown", "conversation_type_invalid"
    if not isinstance(allowed, bool):
        return [], conversation_type, "admission_allowed_invalid"
    if not reason:
        return [], conversation_type, "admission_reason_missing"

    if any(not isinstance(value, str) for value in candidates_raw):
        return [], conversation_type, "remark_code_candidates_invalid"
    candidates = list(candidates_raw)
    if any(not value for value in candidates) or len(candidates) != len(set(candidates)):
        return [], conversation_type, "remark_code_candidates_invalid"
    if allowed is not True:
        if candidates:
            return [], conversation_type, "disallowed_identity_has_candidates"
        return [], conversation_type, None
    if conversation_type != "private":
        return [], conversation_type, "allowed_identity_not_private"
    if len(candidates) != 1 or not is_formal_c2_remark_code(candidates[0]):
        return [], conversation_type, "formal_remark_code_invalid"
    admitted_code = admission.get("remark_code")
    if not isinstance(admitted_code, str):
        return [], conversation_type, "admission_remark_code_invalid"
    if admitted_code != candidates[0]:
        return [], conversation_type, "admission_remark_code_mismatch"
    return candidates, conversation_type, None


def sender_role_hint(message: dict[str, Any]) -> str:
    value = str(message.get("sender_role") or message.get("sender") or "").strip().lower()
    if value in {"customer", "self", "system", "unknown"}:
        return value
    return "unknown"


def message_type(message: dict[str, Any]) -> str:
    value = str(message.get("type") or message.get("message_type") or "").strip().lower()
    if value in {"text", "image", "system", "voice", "file", "unknown"}:
        return value
    return "unknown"


def content_looks_like_untranscribed_voice_placeholder(content: str) -> bool:
    compact = re.sub(r"\s+", "", str(content or ""))
    if not compact:
        return False
    if compact.startswith("[语音]") or compact.startswith("【语音】") or compact.startswith("(语音)") or compact.startswith("（语音）"):
        tail = re.sub(r"^[\[【(（]语音[\]】)）]?", "", compact)
        if not tail or re.fullmatch(r"[^A-Za-z\u4e00-\u9fff]{0,6}\d{1,3}[\"秒sS]?[^\u4e00-\u9fffA-Za-z]{0,8}", tail):
            return True
    if re.fullmatch(r"[\[【(（]?(语音|音频|voice)[\]】)）]?\d{0,3}[\"秒sS]?", compact, re.IGNORECASE):
        return True
    if re.fullmatch(r"[\)\]）>》!|lI(（]{0,3}\d{1,3}[\"秒sS][^\u4e00-\u9fff]{0,8}", compact):
        return True
    if re.fullmatch(r"\d{1,3}[\"']?[\(\[（]{1,2}", compact):
        return True
    return bool(re.fullmatch(r"\d{1,3}[\"秒sS](转文字|语音转文字|转为文字|转写)", compact))


def raw_ocr_looks_like_voice_transcript(message: dict[str, Any]) -> bool:
    raw = str(message.get("content_raw_ocr") or "")
    content = str(message.get("content") or "")
    if not raw or not content:
        return False
    if content_looks_like_untranscribed_voice_placeholder(content):
        return False
    compact_raw = re.sub(r"\s+", "", raw)
    if not re.match(r"^[^\u4e00-\u9fffA-Za-z]{0,4}\d{1,3}[\"秒sS]", compact_raw):
        return False
    return bool(re.sub(r"^[^\u4e00-\u9fffA-Za-z]{0,4}\d{1,3}[\"秒sS]", "", compact_raw).strip())


def strip_voice_ocr_duration_prefix(content: str) -> tuple[str, bool]:
    lines = [line.strip() for line in str(content or "").splitlines() if line.strip()]
    if len(lines) < 2:
        return str(content or "").strip(), False
    first = re.sub(r"\s+", "", lines[0])
    if not re.fullmatch(r"[^\u4e00-\u9fffA-Za-z]{0,4}\d{1,3}[\"秒sS]", first):
        return str(content or "").strip(), False
    return "\n".join(lines[1:]).strip(), True


def message_rect(message: dict[str, Any]) -> dict[str, float] | None:
    raw = message.get("bubble_rect") or message.get("rect") or message.get("bounds")
    if isinstance(raw, dict):
        try:
            left = float(raw.get("left"))
            top = float(raw.get("top"))
            right = float(raw.get("right"))
            bottom = float(raw.get("bottom"))
        except (TypeError, ValueError):
            return None
    elif isinstance(raw, (list, tuple)) and len(raw) >= 4:
        try:
            left, top, right, bottom = [float(value) for value in raw[:4]]
        except (TypeError, ValueError):
            return None
    else:
        return None
    if right <= left or bottom <= top:
        return None
    return {"left": left, "top": top, "right": right, "bottom": bottom, "center_x": (left + right) / 2.0, "center_y": (top + bottom) / 2.0}


def order_authoritative_slots(slots: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Apply the single C2 final-frame ordering rule."""

    if slots and all(slot.get("rect") for slot in slots):
        return sorted(
            slots,
            key=lambda slot: (
                float(slot["rect"]["top"]),
                float(slot["rect"]["bottom"]),
                int(slot["authority_index"]),
            ),
        )
    return sorted(slots, key=lambda slot: int(slot["authority_index"]))


def authoritative_order_source(slots: list[dict[str, Any]]) -> str:
    """Describe whether the complete frame order is physically proven."""

    return (
        "visual_top"
        if slots and all(slot.get("rect") for slot in slots)
        else "observation_index_fallback"
    )







def sender_role_group(message: dict[str, Any]) -> str:
    role = sender_role_hint(message)
    if role in {"self", "sales", "sales_candidate"}:
        return "self"
    if role in {"customer", "contact"}:
        return "customer"
    return role or "unknown"


def validate_committed_image_identity(
    observation: dict[str, Any],
    *,
    conversation_id: str,
    require_formalization_proof: bool = False,
) -> dict[str, Any]:
    """Validate the only image identity allowed to cross a read boundary.

    This is deliberately a whitelist.  Missing, empty, unknown and
    provisional scopes are all non-identities.  Consumers that create a
    formal message additionally require the exact action receipt that
    committed the reserved sequence number.
    """

    if str(observation.get("row_kind") or "").strip().lower() != "image_bubble":
        raise ValueError("C2_IMAGE_IDENTITY_CONTRACT_INVALID")
    clean_conversation_id = str(conversation_id or "").strip()
    if not clean_conversation_id:
        raise ValueError("C2_IMAGE_IDENTITY_CONTRACT_INVALID")
    try:
        committed = require_committed_message(
            conversation_id=clean_conversation_id,
            observation=observation,
        )
    except ValueError as exc:
        raise ValueError("C2_IMAGE_IDENTITY_CONTRACT_INVALID") from exc
    if committed.message_type != "image":
        raise ValueError("C2_IMAGE_IDENTITY_CONTRACT_INVALID")
    worker_stable_id = committed.worker_stable_id

    normalized: dict[str, Any] = {
        "worker_stable_id": worker_stable_id,
        "formalization_proof": None,
    }
    if not require_formalization_proof:
        return normalized

    observation_id = str(observation.get("observation_id") or "").strip()
    image_anchor = (
        observation.get("image_physical_anchor")
        if isinstance(observation.get("image_physical_anchor"), dict)
        else {}
    )
    fingerprint = str(
        image_anchor.get("bubble_visual_fingerprint") or ""
    ).strip()
    summary = observation.get("_worker_image_action_summary")
    mapping = (
        summary.get("confirmed_action_mapping")
        if isinstance(summary, dict)
        and isinstance(summary.get("confirmed_action_mapping"), dict)
        else {}
    )
    action_id = str(mapping.get("canonical_action_id") or "").strip()
    if not all((observation_id, fingerprint, action_id)):
        raise ValueError("C2_IMAGE_IDENTITY_CONTRACT_INVALID")
    if (
        str(mapping.get("reserved_worker_stable_id") or "").strip()
        != worker_stable_id
        or not str(mapping.get("pre_observation_id") or "").strip()
        or str(mapping.get("post_observation_id") or "").strip()
        != observation_id
        or mapping.get("binding_confirmed") is not True
        or str(summary.get("image_visual_fingerprint") or "").strip()
        != fingerprint
    ):
        raise ValueError("C2_IMAGE_IDENTITY_CONTRACT_INVALID")
    normalized["formalization_proof"] = {
        "canonical_action_id": action_id,
        "reserved_worker_stable_id": worker_stable_id,
        "pre_observation_id": str(
            mapping.get("pre_observation_id") or ""
        ).strip(),
        "post_observation_id": observation_id,
        "binding_confirmed": True,
        "image_visual_fingerprint": fingerprint,
    }
    return normalized


def image_observation_source_key(target: WechatReadTarget, observation: dict[str, Any]) -> str:
    try:
        committed = require_committed_message(
            conversation_id=target.conversation_id,
            observation=observation,
        )
    except ValueError as exc:
        raise ValueError("C2_IMAGE_IDENTITY_CONTRACT_INVALID") from exc
    if committed.message_type != "image":
        raise ValueError("C2_IMAGE_IDENTITY_CONTRACT_INVALID")
    return committed.source_message_key


def confirmed_image_identity_receipt(
    observation: dict[str, Any],
    result: dict[str, Any],
) -> dict[str, Any] | None:
    """Validate the sole provisional-to-committed image transition.

    A Vision success is only business content.  It cannot commit the message
    identity unless the exact selected action, reservation, observation and
    stable image fingerprint are all confirmed by the same action receipt.
    """

    if str(
        observation.get("_worker_identity_scope") or ""
    ).strip() != "current_read_provisional":
        return None
    receipt = result.get("_confirmed_image_action_receipt")
    if not isinstance(receipt, dict):
        return None
    stable_id = str(observation.get("_worker_stable_id") or "").strip()
    observation_id = str(observation.get("observation_id") or "").strip()
    image_anchor = (
        observation.get("image_physical_anchor")
        if isinstance(observation.get("image_physical_anchor"), dict)
        else {}
    )
    fingerprint = str(
        image_anchor.get("bubble_visual_fingerprint") or ""
    ).strip()
    action_id = str(receipt.get("canonical_action_id") or "").strip()
    if not all((stable_id, observation_id, fingerprint, action_id)):
        return None
    if (
        str(receipt.get("reserved_worker_stable_id") or "").strip()
        != stable_id
        or not str(receipt.get("pre_observation_id") or "").strip()
        or str(receipt.get("post_observation_id") or "").strip()
        != observation_id
        or receipt.get("binding_confirmed") is not True
        or str(receipt.get("image_visual_fingerprint") or "").strip()
        != fingerprint
    ):
        return None
    return {
        "canonical_action_id": action_id,
        "reserved_worker_stable_id": stable_id,
        "pre_observation_id": str(
            receipt.get("pre_observation_id") or ""
        ).strip(),
        "post_observation_id": observation_id,
        "binding_confirmed": True,
        "image_visual_fingerprint": fingerprint,
    }


def voice_observation_source_key(target: WechatReadTarget, observation: dict[str, Any]) -> str:
    try:
        committed = require_committed_message(
            conversation_id=target.conversation_id,
            observation=observation,
        )
    except ValueError as exc:
        raise ValueError("C2_VOICE_IDENTITY_CONTRACT_INVALID") from exc
    if committed.message_type != "voice":
        raise ValueError("C2_VOICE_IDENTITY_CONTRACT_INVALID")
    return committed.source_message_key


def apply_image_terminal_result(observation: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    enriched = dict(observation)
    transaction = (
        result.get("transaction")
        if isinstance(result.get("transaction"), dict)
        else {}
    )
    action_phase = str(
        result.get("action_phase")
        or transaction.get("action_phase")
        or "not_attempted"
    ).strip()
    enriched["action_phase"] = action_phase
    state = str(result.get("state") or "failed").strip().lower()
    if state not in {"completed", "failed"}:
        state = "failed"
    identity_scope = str(
        enriched.get("_worker_identity_scope") or ""
    ).strip()
    if identity_scope == "current_read_provisional":
        receipt = confirmed_image_identity_receipt(enriched, result)
        if receipt is None:
            state = "failed"
            result = {
                **result,
                "state": "failed",
                "reason": "C2_IMAGE_IDENTITY_CONTRACT_INVALID",
                "reason_detail": "confirmed_image_identity_receipt_missing_or_invalid",
            }
        else:
            enriched["_worker_identity_scope"] = "committed"
            enriched["_worker_image_action_summary"] = {
                "confirmed_action_mapping": {
                    key: receipt[key]
                    for key in (
                        "canonical_action_id",
                        "reserved_worker_stable_id",
                        "pre_observation_id",
                        "post_observation_id",
                        "binding_confirmed",
                    )
                },
                "image_visual_fingerprint": receipt[
                    "image_visual_fingerprint"
                ],
            }
            enriched["_worker_committed_message"] = (
                committed_identity_record(
                    worker_stable_id=str(
                        receipt["reserved_worker_stable_id"]
                    ),
                    commit_basis=(
                        MessageCommitBasis.CONFIRMED_IMAGE_ACTION
                    ),
                    observation_id=str(receipt["post_observation_id"]),
                    sender_role=str(enriched.get("sender_role") or ""),
                    message_type="image",
                    proof=dict(receipt),
                )
            )
    elif identity_scope != "committed":
        state = "failed"
        result = {
            **result,
            "state": "failed",
            "reason": "C2_IMAGE_IDENTITY_CONTRACT_INVALID",
            "reason_detail": "committed_image_identity_missing",
        }
    enriched["item_state"] = state
    error_code = str(result.get("reason") or "").strip()
    reason_detail = str(
        result.get("reason_detail")
        or (
            transaction.get("status")
        )
        or ""
    ).strip()
    if reason_detail:
        enriched["reason_detail"] = reason_detail
    if error_code:
        enriched["error_code"] = error_code
    enriched.pop("contract_errors", None)
    if state != "completed":
        return enriched
    understanding = _project_customer_image_understanding(result.get("customer_image_understanding") or {})
    bridge = _project_visual_bridge_input(result.get("visual_bridge_input") or {})
    image_sha256 = str(transaction.get("image_sha256") or "").strip().lower()
    if image_sha256:
        audit = understanding.get("audit") if isinstance(understanding.get("audit"), dict) else {}
        understanding["audit"] = {**audit, "image_sha256": image_sha256}
    summary = str(understanding.get("vision_summary") or "").strip()
    enriched["content_clean"] = summary
    enriched["customer_image_understanding"] = understanding
    enriched["visual_bridge_input"] = bridge
    source = enriched.get("source_message") if isinstance(enriched.get("source_message"), dict) else {}
    enriched["source_message"] = {
        **source,
        "type": "image",
        "message_type": "image",
        "content": summary,
    }
    return enriched


def replayable_image_observation(
    observation: dict[str, Any],
    *,
    source_message_key: str | None = None,
) -> dict[str, Any]:
    """Freeze one terminal image fact without image bytes or runtime paths."""

    projected = _drop_image_runtime_fields(dict(observation))
    source = (
        projected.get("source_message")
        if isinstance(projected.get("source_message"), dict)
        else {}
    )
    projected_source = _drop_image_runtime_fields(dict(source))
    committed_source_key = str(source_message_key or "").strip()
    if committed_source_key:
        projected_source["source_message_key"] = committed_source_key
    else:
        projected_source.pop("source_message_key", None)
    projected["source_message"] = projected_source
    return json.loads(
        json.dumps(
            projected,
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )


def normalized_voice_flow_state(value: Any) -> str:
    state = str(value or "").strip().lower()
    if state == "voice_transcribe_completed":
        return "completed"
    if state == "voice_transcribe_partial":
        return "partial"
    if state == "voice_transcribe_cancelled":
        return "cancelled"
    if state:
        return "failed"
    return "completed"


def unified_message_dedupe_metadata(
    target: WechatReadTarget,
    committed: CommittedMessage,
) -> tuple[str, str, dict[str, Any]]:
    """Build V3 dedupe metadata only from committed durable identity."""

    if committed.conversation_id != target.conversation_id:
        raise ValueError("MESSAGE_IDENTITY_CONTRACT_INVALID")
    base = {
        "conversation_id": target.conversation_id,
        "worker_stable_id": committed.worker_stable_id,
    }
    return (
        f"{target.conversation_id}:{stable_digest(base, length=32)}"[:255],
        "high",
        {"source": "worker_cross_round_sequence", **base},
    )


def voice_text_looks_like_payload(value: str) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    compact = text[:4000]
    if text.startswith("{'") or text.startswith('{"') or text.startswith("[{"):
        return True
    payload_tokens = (
        "voice_transcribe_completed",
        "voice_transcribe_review",
        "before_screenshot_path",
        "after_screenshot_path",
        "transcribed_messages",
        "context_menu_attempt",
    )
    return len(text) > 1000 and any(token in compact for token in payload_tokens)


def clean_voice_transcribed_content(item: dict[str, Any]) -> str:
    for key in ("content_clean", "text", "transcript", "transcribed_text"):
        value = item.get(key)
        if isinstance(value, str) and value.strip() and not voice_text_looks_like_payload(value):
            return value.strip()
    content = item.get("content")
    if isinstance(content, str):
        stripped = content.strip()
        if voice_text_looks_like_payload(stripped):
            return ""
        return stripped
    if isinstance(content, dict):
        for key in ("content_clean", "text", "transcript", "transcribed_text"):
            value = content.get(key)
            if isinstance(value, str) and value.strip() and not voice_text_looks_like_payload(value):
                return value.strip()
    return ""


def voice_transcription_meta(
    voice_transcription_summary: dict[str, Any],
    *,
    message: dict[str, Any] | None = None,
) -> dict[str, Any]:
    meta = {
        "state": voice_transcription_summary.get("state"),
        "action_phase": voice_transcription_summary.get("action_phase"),
        "ui_action_performed": voice_transcription_summary.get(
            "ui_action_performed"
        ),
        "business_state": voice_transcription_summary.get(
            "business_state"
        ),
        "business_result_confirmed": voice_transcription_summary.get(
            "business_result_confirmed"
        ),
        "attempt_count": voice_transcription_summary.get("attempt_count"),
        "quality_flags": voice_transcription_summary.get("quality_flags") if isinstance(voice_transcription_summary.get("quality_flags"), list) else [],
        "sidecar_run_id": voice_transcription_summary.get("sidecar_run_id"),
        "artifact_dir": voice_transcription_summary.get("artifact_dir"),
        "before_screenshot_path": voice_transcription_summary.get("before_screenshot_path"),
        "after_screenshot_path": voice_transcription_summary.get("after_screenshot_path"),
        "screenshot_path": voice_transcription_summary.get("screenshot_path"),
        "review_path": voice_transcription_summary.get("review_path"),
        "target_mode": voice_transcription_summary.get("target_mode"),
        "remark_code": voice_transcription_summary.get("remark_code"),
        # The backend must validate the same immutable action-to-message
        # binding that the Worker accepted before it admits the final frame.
        # Keep these fields as a read-only projection of the OmniAuto execute
        # result; never reconstruct them from content, coordinates or anchors.
        "canonical_voice_action_id": voice_transcription_summary.get(
            "canonical_voice_action_id"
        ),
        "voice_action_stage": voice_transcription_summary.get(
            "voice_action_stage"
        ),
        "pre_frame_id": voice_transcription_summary.get("pre_frame_id"),
        "post_frame_id": voice_transcription_summary.get("post_frame_id"),
        "selected_pre_observation_id": voice_transcription_summary.get(
            "selected_pre_observation_id"
        ),
        "selected_action_token": voice_transcription_summary.get(
            "selected_action_token"
        ),
        "selected_target_fingerprint": voice_transcription_summary.get(
            "selected_target_fingerprint"
        ),
        "reserved_worker_stable_id": voice_transcription_summary.get(
            "reserved_worker_stable_id"
        ),
        "transcript_binding_status": voice_transcription_summary.get(
            "transcript_binding_status"
        ),
        "transcript_binding_method": voice_transcription_summary.get(
            "transcript_binding_method"
        ),
        "binding_candidate_count": voice_transcription_summary.get(
            "binding_candidate_count"
        ),
        "tracking_frame_ids": list(
            voice_transcription_summary.get("tracking_frame_ids") or []
        ),
        "tracking_edges": [
            dict(item)
            for item in (
                voice_transcription_summary.get("tracking_edges") or []
            )
            if isinstance(item, dict)
        ],
        "matched_neighbor_pairs": [
            dict(item)
            for item in (
                voice_transcription_summary.get("matched_neighbor_pairs")
                or []
            )
            if isinstance(item, dict)
        ],
        "native_source_message_id": voice_transcription_summary.get(
            "native_source_message_id"
        ),
        "confirmed_action_mapping": dict(
            voice_transcription_summary.get("confirmed_action_mapping") or {}
        ),
    }
    if isinstance(message, dict):
        meta["message"] = {
            key: message.get(key)
            for key in (
                "id",
                "type",
                "sender",
                "sender_role",
                "sender_role_algorithm",
                "voice_duration",
                "voice_duration_text",
                "quality_flags",
                "bubble_rect",
                "message_envelope",
                "voice_anchor",
                "voice_anchor_key",
                "voice_anchor_stable_key",
                "avatar_alignment",
            )
            if key in message
        }
    return meta


def _build_message_ingest_payload_v3(
    target: WechatReadTarget,
    sidecar_payload: dict[str, Any],
    *,
    read_run_id: str,
    allow_provisional_discovery: bool = False,
) -> dict[str, Any]:
    if not target.authorization_revision:
        raise ValueError("C2_TARGET_AUTHORIZATION_REVISION_MISSING")
    if int(sidecar_payload.get("observation_schema_version") or 0) != 3:
        raise ValueError("C2_OBSERVATION_SCHEMA_VERSION_REQUIRED")
    clean_read_run_id = str(read_run_id or "").strip()
    if not clean_read_run_id:
        raise ValueError("C2_READ_RUN_ID_MISSING")
    observations = sidecar_payload.get("observations")
    if not isinstance(observations, list):
        raise ValueError("C2 V3 payload is missing observations")
    worker_stable_ids: dict[str, str] = {}
    worker_committed_messages: dict[str, dict[str, Any]] = {}
    worker_ai_reply_receipts: dict[str, dict[str, Any]] = {}
    worker_voice_action_summaries: dict[str, dict[str, Any]] = {}
    worker_identity_scopes: dict[str, str] = {}
    worker_image_action_summaries: dict[str, dict[str, Any]] = {}
    invalid_image_observation_ids: set[str] = set()
    historical_image_observation_ids: set[str] = set()
    backend_confirmed_source_keys: set[str] | None = None
    sanitized_observations: list[Any] = []
    for item in observations:
        if not isinstance(item, dict):
            sanitized_observations.append(item)
            continue
        observation = _drop_image_runtime_fields(dict(item))
        observation_id = str(observation.get("observation_id") or "").strip()
        row_kind = str(observation.get("row_kind") or "").strip().lower()
        original_identity_observation = dict(observation)
        worker_stable_id = str(observation.get("_worker_stable_id") or "").strip()
        worker_identity_scope = str(
            observation.get("_worker_identity_scope") or ""
        ).strip()
        image_action_summary = observation.get("_worker_image_action_summary")
        committed_message_record = observation.get(
            "_worker_committed_message"
        )
        if observation_id and isinstance(committed_message_record, dict):
            worker_committed_messages[observation_id] = dict(
                committed_message_record
            )
        if observation_id:
            worker_identity_scopes[observation_id] = worker_identity_scope
        if row_kind == "image_bubble" and observation_id:
            if isinstance(image_action_summary, dict):
                worker_image_action_summaries[observation_id] = dict(
                    image_action_summary
                )
        if row_kind == "image_bubble" and worker_identity_scope == "current_read_provisional":
            provisional_discovery_allowed = bool(
                allow_provisional_discovery
                and str(observation.get("item_state") or "discovered")
                .strip()
                .lower()
                == "discovered"
            )
            if not provisional_discovery_allowed:
                raise ValueError("C2_IMAGE_IDENTITY_CONTRACT_INVALID")
            # A preliminary slot plan may inspect this row's geometry and
            # role, but the provisional number is not a message identity.
            worker_stable_id = ""
        elif row_kind == "image_bubble":
            try:
                validate_committed_image_identity(
                    original_identity_observation,
                    conversation_id=target.conversation_id,
                )
            except ValueError:
                if not allow_provisional_discovery:
                    raise ValueError("C2_IMAGE_IDENTITY_CONTRACT_INVALID")
                invalid_image_observation_ids.add(observation_id)
                worker_stable_id = ""
            else:
                if not allow_provisional_discovery:
                    try:
                        validate_committed_image_identity(
                            original_identity_observation,
                            conversation_id=target.conversation_id,
                            require_formalization_proof=True,
                        )
                    except ValueError:
                        source_key = image_observation_source_key(
                            target,
                            original_identity_observation,
                        )
                        if backend_confirmed_source_keys is None:
                            backend_confirmed_source_keys = {
                                str(
                                    checkpoint.get("source_message_key")
                                    or ""
                                ).strip()
                                for checkpoint in (
                                    (
                                        target.raw.get(
                                            "identity_checkpoint"
                                        )
                                        or {}
                                    ).get("recent_messages")
                                    if isinstance(target.raw, dict)
                                    and isinstance(
                                        target.raw.get(
                                            "identity_checkpoint"
                                        ),
                                        dict,
                                    )
                                    else []
                                )
                                if isinstance(checkpoint, dict)
                                and str(
                                    checkpoint.get("source_message_key")
                                    or ""
                                ).strip()
                            }
                        if source_key not in backend_confirmed_source_keys:
                            raise ValueError(
                                "C2_IMAGE_IDENTITY_CONTRACT_INVALID"
                            )
                        # A backend-confirmed historical image may be used to
                        # identify/query the old fact, but it must never be
                        # regenerated as a new formal image message without
                        # its terminal action proof.
                        historical_image_observation_ids.add(observation_id)
        committed_identity = None
        if observation_id and worker_stable_id:
            # The unique commit gate must run while every Worker-only proof is
            # still present.  Sanitization is only allowed after the identity
            # has become a typed CommittedMessage.
            committed_identity = require_committed_message(
                conversation_id=target.conversation_id,
                observation=original_identity_observation,
            )
        observation.pop("_worker_stable_id", None)
        # Worker-only identity bookkeeping must never leak into the
        # Sidecar/backend observation contract.  The durable stable id is
        # projected below as source_message_key; provisional scope and action
        # receipts remain local alignment evidence.
        observation.pop("_worker_identity_scope", None)
        observation.pop("_worker_image_action_summary", None)
        observation.pop("_worker_committed_message", None)
        if "_worker_legacy_dedupe_key" in observation:
            raise ValueError("C2_LEGACY_IDENTITY_FIELD_FORBIDDEN")
        worker_ai_reply_receipt = observation.pop(
            "_worker_ai_reply_receipt", None
        )
        worker_voice_action_summary = observation.pop(
            "_worker_voice_action_summary", None
        )
        if observation_id and committed_identity is not None:
            worker_stable_ids[observation_id] = worker_stable_id
            source_message = (
                observation.get("source_message")
                if isinstance(observation.get("source_message"), dict)
                else {}
            )
            durable_source_key = committed_identity.source_message_key
            observation["source_message"] = {
                **source_message,
                "source_message_key": durable_source_key,
            }
        if observation_id and isinstance(worker_ai_reply_receipt, dict):
            worker_ai_reply_receipts[observation_id] = dict(
                worker_ai_reply_receipt
            )
        if observation_id and isinstance(
            worker_voice_action_summary,
            dict,
        ):
            worker_voice_action_summaries[observation_id] = dict(
                worker_voice_action_summary
            )
        if str(observation.get("row_kind") or "").strip().lower() == "image_bubble":
            if isinstance(observation.get("customer_image_understanding"), dict):
                observation["customer_image_understanding"] = _project_customer_image_understanding(
                    observation.get("customer_image_understanding")
                )
            if isinstance(observation.get("visual_bridge_input"), dict):
                observation["visual_bridge_input"] = _project_visual_bridge_input(
                    observation.get("visual_bridge_input")
                )
        sanitized_observations.append(observation)
    observations = sanitized_observations
    allowed_roles = contract_values("sender_roles")
    allowed_types = contract_values("message_types")
    row_rules = contract_row_rules()
    voice_summary = sidecar_payload.get("voice_transcription") if isinstance(sidecar_payload.get("voice_transcription"), dict) else {}
    flow_state = normalized_voice_flow_state(voice_summary.get("state")) if voice_summary else "completed"
    message_sidecar_id = sidecar_run_id(sidecar_payload, "messages")
    mapped: list[dict[str, Any]] = []
    source_keys: set[str] = set()
    slots: list[dict[str, Any]] = []
    observation_validation_errors: list[dict[str, Any]] = []
    authoritative_frame_source = str(sidecar_payload.get("authoritative_frame_source") or "").strip()
    if authoritative_frame_source not in {
        "initial_read",
        "final_read",
        "action_journal_recovery",
    }:
        raise ValueError("C2_AUTHORITATIVE_FRAME_SOURCE_INVALID")

    def append_item(
        source: dict[str, Any],
        *,
        role: str,
        msg_type: str,
        content: str | None,
        source_index: int,
        message_position: dict[str, Any],
        voice_meta: dict[str, Any] | None = None,
        item_state: str = "completed",
    ) -> None:
        if role not in allowed_roles or role == "unknown" or msg_type not in allowed_types:
            raise RuntimeError("validated C2 observation became invalid during canonical assembly")
        if (
            msg_type in {"text", "system", "voice"}
            and item_state != "failed"
            and not str(content or "").strip()
        ):
            raise RuntimeError("validated C2 observation lost content during canonical assembly")
        normalized_source = {**source, "sender_role": role, "sender": role, "type": msg_type, "content": content}
        source_observation = source.get("observation") if isinstance(source.get("observation"), dict) else {}
        worker_stable_id = str(source.get("_worker_stable_id") or "").strip()
        identity_observation = dict(source_observation)
        identity_observation["_worker_stable_id"] = worker_stable_id
        identity_observation["_worker_identity_scope"] = str(
            source.get("_worker_identity_scope") or ""
        ).strip()
        identity_observation["_worker_committed_message"] = (
            dict(source.get("_worker_committed_message") or {})
            if isinstance(source.get("_worker_committed_message"), dict)
            else {}
        )
        for summary_key in (
            "_worker_image_action_summary",
            "_worker_voice_action_summary",
            "_worker_ai_reply_receipt",
        ):
            summary = source.get(summary_key)
            if isinstance(summary, dict):
                identity_observation[summary_key] = dict(summary)
        committed = require_committed_message(
            conversation_id=target.conversation_id,
            observation=identity_observation,
        )
        if committed.message_type != msg_type or committed.sender_role != role:
            raise ValueError("MESSAGE_IDENTITY_CONTRACT_INVALID")
        dedupe_key, confidence, basis = unified_message_dedupe_metadata(
            target,
            committed,
        )
        canonical_source_key = committed.source_message_key
        if canonical_source_key in source_keys:
            observation_validation_errors.append(
                {
                    "observation_id": str((source.get("observation") or {}).get("observation_id") or f"observation-{source_index}"),
                    "row_kind": str((source.get("observation") or {}).get("row_kind") or ""),
                    "sender_role_source": str((source.get("observation") or {}).get("sender_role_source") or ""),
                    "error_code": "MESSAGE_SOURCE_CONFLICT",
                    "source_message_key": canonical_source_key,
                }
            )
            return
        source_keys.add(canonical_source_key)
        raw_payload = {
            **{
                key: value
                for key, value in normalized_source.items()
                if key not in {
                    "_worker_stable_id",
                    "_worker_voice_action_summary",
                    "_worker_identity_scope",
                    "_worker_image_action_summary",
                    "_worker_committed_message",
                }
            },
            "contract_version": 3,
            "contract_revision": contract_revision(),
            "contract_sha256": contract_sha256(),
            "observation_schema_version": 3,
            "source_message_key": canonical_source_key,
            "dedupe_confidence": confidence,
            "dedupe_basis": basis,
            # Position is attached only after identity generation. Screen
            # coordinates must never change dedupe or source identity.
            "message_position": message_position,
        }
        if voice_meta:
            raw_payload["voice_transcription"] = content
            raw_payload["voice_transcription_meta"] = voice_meta
        observation_id = str(source_observation.get("observation_id") or "").strip()
        ai_reply_receipt = worker_ai_reply_receipts.get(observation_id)
        if ai_reply_receipt and role == "self" and msg_type == "text":
            raw_payload["ai_reply_receipt"] = {
                "reply_action_id": str(
                    ai_reply_receipt.get("reply_action_id") or ""
                ),
                "reply_text_hash": str(
                    ai_reply_receipt.get("reply_text_hash") or ""
                ),
                "worker_stable_id": str(
                    ai_reply_receipt.get("worker_stable_id") or ""
                ),
                "confirmed_at": str(ai_reply_receipt.get("confirmed_at") or ""),
                "reconciliation_state": str(
                    ai_reply_receipt.get("reconciliation_state") or "confirmed"
                ),
                "source_message_key": canonical_source_key,
            }
        if msg_type == "image":
            observation = source.get("observation") if isinstance(source.get("observation"), dict) else {}
            raw_payload["error_code"] = str(
                observation.get("error_code") or ""
            )
            raw_payload["reason_detail"] = str(
                observation.get("reason_detail") or ""
            )
            if item_state == "completed":
                raw_payload["customer_image_understanding"] = _project_customer_image_understanding(
                    observation.get("customer_image_understanding")
                )
                raw_payload["visual_bridge_input"] = _project_visual_bridge_input(
                    observation.get("visual_bridge_input")
                )
            raw_payload = _drop_image_runtime_fields(raw_payload)
        elif msg_type == "voice" and item_state == "failed":
            observation = (
                source.get("observation")
                if isinstance(source.get("observation"), dict)
                else {}
            )
            raw_payload["error_code"] = str(
                observation.get("error_code") or "VOICE_TRANSCRIBE_FAILED"
            )
            raw_payload["reason_detail"] = str(
                observation.get("reason_detail")
                or raw_payload["error_code"]
            )
        mapped.append(
            {
                "dedupe_key": dedupe_key,
                "source_message_key": canonical_source_key,
                "sender_role_hint": role,
                "message_type": msg_type,
                "content": str(content).strip() if content is not None else None,
                "occurred_at": normalized_source.get("occurred_at") or None,
                "ocr_confidence": normalized_source.get("ocr_confidence"),
                "item_state": item_state,
                "flow_state": (
                    "failed"
                    if msg_type == "voice" and item_state == "failed"
                    else "completed"
                ),
                "message_position": message_position,
                "raw_payload": raw_payload,
            }
        )

    for index, observation in enumerate(observations):
        if not isinstance(observation, dict):
            observation_validation_errors.append(
                {
                    "observation_id": f"observation-{index}",
                    "row_kind": "",
                    "sender_role_source": "",
                    "error_code": "OBSERVATION_NOT_OBJECT",
                }
            )
            continue
        row_kind = str(observation.get("row_kind") or "").strip().lower()
        voice_state = str(observation.get("voice_state") or "").strip().lower()
        role_source = str(observation.get("sender_role_source") or "").strip().lower()
        role = str(observation.get("sender_role") or "unknown").strip().lower()
        msg_type = str(observation.get("message_type") or "unknown").strip().lower()
        content = str(observation.get("content_clean") or "").strip()
        source = observation.get("source_message") if isinstance(observation.get("source_message"), dict) else {}
        # Keep the complete OmniAuto observation as the immutable evidence.
        # The backend must be able to re-run the same contract checks instead
        # of trusting only Worker's canonical interpretation.
        source = {
            **source,
            "observation": dict(observation),
            "_worker_stable_id": worker_stable_ids.get(
                str(observation.get("observation_id") or "").strip(),
                "",
            ),
            "_worker_voice_action_summary": (
                worker_voice_action_summaries.get(
                    str(observation.get("observation_id") or "").strip(),
                    {},
                )
            ),
            "_worker_identity_scope": worker_identity_scopes.get(
                str(observation.get("observation_id") or "").strip(),
                "",
            ),
            "_worker_image_action_summary": (
                worker_image_action_summaries.get(
                    str(observation.get("observation_id") or "").strip(),
                    {},
                )
            ),
            "_worker_committed_message": (
                worker_committed_messages.get(
                    str(observation.get("observation_id") or "").strip(),
                    {},
                )
            ),
            "_worker_ai_reply_receipt": (
                worker_ai_reply_receipts.get(
                    str(observation.get("observation_id") or "").strip(),
                    {},
                )
            ),
        }
        rect = message_rect({"bubble_rect": observation.get("bubble_rect") or source.get("bubble_rect")})
        candidate: dict[str, Any] | None = None
        rule = row_rules.get(row_kind)
        validation_code = ""
        item_state = str(
            observation.get("item_state")
            or ("discovered" if row_kind == "image_bubble" else "completed")
        ).strip().lower()
        if (
            row_kind == "image_bubble"
            and str(observation.get("observation_id") or "").strip()
            in invalid_image_observation_ids
        ):
            validation_code = "C2_IMAGE_IDENTITY_CONTRACT_INVALID"
        elif int(observation.get("schema_version") or 0) != 3:
            validation_code = "OBSERVATION_SCHEMA_VERSION_MISMATCH"
        elif not isinstance(rule, dict):
            validation_code = "OBSERVATION_ROW_KIND_UNKNOWN"
        elif row_kind == "image_bubble" and item_state == "discovered":
            validation_code = ""
        else:
            required_fields = (
                rule.get("failed_required_fields")
                if item_state == "failed"
                else rule.get("required_fields")
            )
            for field in required_fields or []:
                value = observation.get(str(field))
                if value is None or (isinstance(value, str) and not value.strip()):
                    validation_code = f"OBSERVATION_REQUIRED_FIELD_MISSING:{field}"
                    break
            if not validation_code and msg_type != str(rule.get("message_type") or ""):
                validation_code = "MESSAGE_ROW_TYPE_MISMATCH"
            elif not validation_code and role not in {
                str(value) for value in rule.get("allowed_sender_roles") or []
            }:
                validation_code = "MESSAGE_ROW_SENDER_ROLE_INVALID"
            elif not validation_code and role_source not in {
                str(value) for value in rule.get("allowed_sender_role_sources") or []
            }:
                validation_code = "MESSAGE_ROW_ROLE_SOURCE_UNTRUSTED"
            elif not validation_code and voice_state not in {
                str(value) for value in rule.get("allowed_voice_states") or []
            }:
                validation_code = "MESSAGE_ROW_VOICE_STATE_INVALID"
            elif not validation_code and observation.get("contract_errors"):
                validation_code = "OMNIAUTO_OBSERVATION_CONTRACT_INVALID"
        if validation_code:
            observation_validation_errors.append(
                {
                    "observation_id": str(observation.get("observation_id") or f"observation-{index}"),
                    "row_kind": row_kind,
                    "sender_role_source": role_source,
                    "error_code": validation_code,
                }
            )
        elif isinstance(rule, dict) and (
            bool(rule.get("ingestible"))
            or (item_state == "failed" and bool(rule.get("failed_ingestible")))
        ):
            if row_kind == "voice_transcript":
                parent_anchor_key = str(
                    observation.get("parent_voice_anchor_key") or observation.get("voice_anchor_key") or ""
                ).strip()
                voice_source = {
                    **source,
                    "type": "voice",
                    "content": content,
                    "voice_anchor_stable_key": parent_anchor_key,
                }
                candidate = {
                    "source": voice_source,
                    "role": role,
                    "msg_type": "voice",
                    "content": content,
                    "source_index": index,
                    "voice_meta": voice_transcription_meta(
                        (
                            source.get("_worker_voice_action_summary")
                            if isinstance(
                                source.get("_worker_voice_action_summary"),
                                dict,
                            )
                            and source.get("_worker_voice_action_summary")
                            else voice_summary
                        ),
                        message=source,
                    ),
                }
            elif row_kind == "voice_bubble" and item_state == "failed":
                candidate = {
                    "source": {
                        **source,
                        "type": "voice",
                        "content": None,
                        "voice_anchor_stable_key": str(
                            observation.get("voice_anchor_key") or ""
                        ),
                    },
                    "role": role,
                    "msg_type": "voice",
                    "content": None,
                    "source_index": index,
                    "voice_meta": None,
                    "item_state": "failed",
                }
            elif (
                row_kind == "image_bubble"
                and item_state in {"completed", "failed"}
                and str(observation.get("observation_id") or "").strip()
                not in historical_image_observation_ids
            ):
                candidate = {
                    "source": source,
                    "role": role,
                    "msg_type": "image",
                    "content": content if item_state == "completed" else None,
                    "source_index": index,
                    "voice_meta": None,
                    "item_state": item_state,
                }
            elif row_kind != "image_bubble":
                candidate = {
                    "source": source,
                    "role": role,
                    "msg_type": msg_type,
                    "content": content or None,
                    "source_index": index,
                    "voice_meta": None,
                    "item_state": "completed",
                }
        slots.append(
            {
                "authority_index": index,
                "rect": rect,
                "row_kind": row_kind,
                "voice_state": voice_state,
                "observation": observation,
                "source": source,
                "candidate": candidate,
            }
        )

    frame_order_source = authoritative_order_source(slots)
    ordered_slots = order_authoritative_slots(slots)

    for screen_order, slot in enumerate(ordered_slots, start=1):
        candidate = slot.get("candidate")
        if not isinstance(candidate, dict):
            continue
        rect = slot.get("rect")
        message_position: dict[str, Any] = {
            "screen_order": screen_order,
            "frame_source": authoritative_frame_source,
            "order_source": frame_order_source,
        }
        if rect:
            message_position.update(
                {
                    "visual_top": int(rect["top"]),
                    "visual_bottom": int(rect["bottom"]),
                }
            )
        append_item(
            candidate["source"],
            role=candidate["role"],
            msg_type=candidate["msg_type"],
            content=candidate["content"],
            source_index=candidate["source_index"],
            message_position=message_position,
            voice_meta=candidate["voice_meta"],
            item_state=str(candidate.get("item_state") or "completed"),
        )

    finished_at = now_iso()
    authorization_read_reason = ""
    if isinstance(target.raw, dict):
        authorization_read_reason = str(
            target.raw.get("authorization_read_reason") or ""
        ).strip()
    authorization_read_reason = authorization_read_reason or str(
        target.read_reason or ""
    ).strip()
    continuation = (
        target.raw.get("batch_continuation")
        if isinstance(target.raw, dict)
        and isinstance(target.raw.get("batch_continuation"), dict)
        else {}
    )
    ai_reply_boundary = (
        dict(target.raw.get("ai_reply_boundary"))
        if isinstance(target.raw, dict)
        and isinstance(target.raw.get("ai_reply_boundary"), dict)
        else {}
    )
    flow_gate_details = [
        dict(item)
        for item in (sidecar_payload.get("flow_gate_details") or [])
        if isinstance(item, dict)
    ]
    slot_ledger_states = validate_slot_ledger_states(
        list(sidecar_payload.get("slot_ledger_states") or []),
        read_run_id=clean_read_run_id,
    )
    sequence_alignment_evidence = sidecar_payload.get(
        "sequence_alignment_evidence"
    )
    if not isinstance(sequence_alignment_evidence, dict):
        raise ValueError("C2_SEQUENCE_ALIGNMENT_EVIDENCE_MISSING")
    required_alignment_fields = {
        "pre_sequence_source",
        "pre_frame_id",
        "post_frame_id",
        "alignment_status",
        "candidate_alignment_count",
        "matched_pairs",
        "old_tail_fully_consumed",
        "new_suffix_observation_ids",
    }
    if not required_alignment_fields.issubset(sequence_alignment_evidence):
        raise ValueError("C2_SEQUENCE_ALIGNMENT_EVIDENCE_INVALID")
    return {
        "contract_version": 3,
        "contract_revision": contract_revision(),
        "contract_sha256": contract_sha256(),
        "observation_schema_version": 3,
        "read_run_id": clean_read_run_id,
        "conversation_id": target.conversation_id,
        "remark_code": target.remark_code,
        "rpa_session_key": target.rpa_session_key,
        "authorization_revision": target.authorization_revision,
        "unread_generation": int(target.unread_generation or 0),
        "messages": mapped,
        "evidence": {
            "contract_version": 3,
            "contract_revision": contract_revision(),
            "contract_sha256": contract_sha256(),
            "observation_schema_version": 3,
            "authoritative_frame_source": authoritative_frame_source,
            "ui_frame_invalidated": bool(
                sidecar_payload.get("ui_frame_invalidated")
            ),
            "observations": [dict(item) if isinstance(item, dict) else item for item in observations],
            "sidecar_run_id": message_sidecar_id,
            "artifact_dir": sidecar_payload.get("artifact_dir"),
            "review_path": sidecar_payload.get("review_path"),
            "screenshot": sidecar_payload.get("screenshot_path"),
            "adapter": sidecar_payload.get("adapter"),
            "state": sidecar_payload.get("state"),
            "remark_code": target.remark_code,
            "target_display_name": target.display_name,
            "target_row_fingerprint": target.row_fingerprint,
            "read_reason": target.read_reason,
            "authorization_read_reason": authorization_read_reason,
            "ai_reply_boundary": ai_reply_boundary or None,
            "continuation_batch_id": (
                str(continuation.get("batch_id") or "").strip() or None
            ),
            "continuation_token": (
                str(continuation.get("token") or "").strip() or None
            ),
            "finished_at": finished_at,
            "voice_transcription": voice_transcription_meta(voice_summary) if voice_summary else None,
            "observation_validation_errors": observation_validation_errors,
            "history_gap": bool(sidecar_payload.get("history_gap")),
            "flow_gate_errors": list(sidecar_payload.get("flow_gate_errors") or []),
            "flow_gate_details": flow_gate_details,
            "failed_voice_source_keys": list(
                sidecar_payload.get("failed_voice_source_keys") or []
            ),
            "slot_ledger_states": slot_ledger_states,
            "sequence_alignment_evidence": dict(
                sequence_alignment_evidence
            ),
            "historical_warnings": list(
                sidecar_payload.get("historical_warnings") or []
            ),
            "recoverable_handoff_resolution": sidecar_payload.get(
                "recoverable_handoff_resolution"
            ),
        },
    }


def build_message_ingest_payload(
    target: WechatReadTarget,
    sidecar_payload: dict[str, Any],
    *,
    read_run_id: str,
) -> dict[str, Any]:
    if int(sidecar_payload.get("observation_schema_version") or 0) != 3:
        raise ValueError("C2_OBSERVATION_SCHEMA_VERSION_REQUIRED")
    return _build_message_ingest_payload_v3(
        target,
        sidecar_payload,
        read_run_id=read_run_id,
    )


def build_preliminary_slot_payload(
    target: WechatReadTarget,
    sidecar_payload: dict[str, Any],
    *,
    read_run_id: str,
) -> dict[str, Any]:
    """Build read-only canonical evidence without formalizing images.

    This helper exists only for the current-frame incremental planner.  It may
    observe a discovered provisional image, but it deliberately withholds the
    reserved sequence number and therefore cannot create a durable source key.
    """

    if int(sidecar_payload.get("observation_schema_version") or 0) != 3:
        raise ValueError("C2_OBSERVATION_SCHEMA_VERSION_REQUIRED")
    return _build_message_ingest_payload_v3(
        target,
        sidecar_payload,
        read_run_id=read_run_id,
        allow_provisional_discovery=True,
    )


def build_flow_gate_ingest_payload(
    target: WechatReadTarget,
    *,
    read_run_id: str,
    error_code: str,
    evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not target.authorization_revision:
        raise ValueError("C2_TARGET_AUTHORIZATION_REVISION_MISSING")
    clean_code = str(error_code or "").strip()
    if not clean_code:
        raise ValueError("C2_FLOW_GATE_ERROR_CODE_MISSING")
    clean_evidence = dict(evidence or {})
    authoritative_frame_source = str(
        clean_evidence.pop("authoritative_frame_source", "initial_read")
        or "initial_read"
    ).strip()
    if authoritative_frame_source not in {
        "initial_read",
        "final_read",
        "action_journal_recovery",
    }:
        raise ValueError("C2_AUTHORITATIVE_FRAME_SOURCE_INVALID")
    ui_frame_invalidated = clean_evidence.pop(
        "ui_frame_invalidated",
        False,
    )
    if not isinstance(ui_frame_invalidated, bool):
        raise ValueError("C2_UI_FRAME_INVALIDATED_INVALID")
    if ui_frame_invalidated and authoritative_frame_source != "final_read":
        raise ValueError("C2_AUTHORITATIVE_FRAME_SOURCE_INVALID")
    clean_read_run_id = str(read_run_id or "").strip()
    if not clean_read_run_id:
        raise ValueError("C2_READ_RUN_ID_MISSING")
    authorization_read_reason = ""
    if isinstance(target.raw, dict):
        authorization_read_reason = str(
            target.raw.get("authorization_read_reason") or ""
        ).strip()
    authorization_read_reason = authorization_read_reason or str(
        target.read_reason or ""
    ).strip()
    continuation = (
        target.raw.get("batch_continuation")
        if isinstance(target.raw, dict)
        and isinstance(target.raw.get("batch_continuation"), dict)
        else {}
    )
    ai_reply_boundary = (
        dict(target.raw.get("ai_reply_boundary"))
        if isinstance(target.raw, dict)
        and isinstance(target.raw.get("ai_reply_boundary"), dict)
        else {}
    )
    flow_gate_details = [
        dict(item)
        for item in (clean_evidence.get("flow_gate_details") or [])
        if isinstance(item, dict)
    ]
    if not flow_gate_details:
        flow_gate_details = [
            {
                "error_code": clean_code,
                "position_source": "position_unavailable",
            }
        ]
    return {
        "contract_version": 3,
        "contract_revision": contract_revision(),
        "contract_sha256": contract_sha256(),
        "observation_schema_version": 3,
        "read_run_id": clean_read_run_id,
        "conversation_id": target.conversation_id,
        "remark_code": target.remark_code,
        "rpa_session_key": target.rpa_session_key,
        "authorization_revision": target.authorization_revision,
        "unread_generation": int(target.unread_generation or 0),
        "messages": [],
        "evidence": {
            **clean_evidence,
            "contract_version": 3,
            "contract_revision": contract_revision(),
            "contract_sha256": contract_sha256(),
            "observation_schema_version": 3,
            "authoritative_frame_source": authoritative_frame_source,
            "ui_frame_invalidated": ui_frame_invalidated,
            "observations": [],
            "read_reason": target.read_reason,
            "authorization_read_reason": authorization_read_reason,
            "ai_reply_boundary": ai_reply_boundary or None,
            "continuation_batch_id": (
                str(continuation.get("batch_id") or "").strip() or None
            ),
            "continuation_token": (
                str(continuation.get("token") or "").strip() or None
            ),
            "finished_at": now_iso(),
            "flow_gate_errors": [clean_code],
            "flow_gate_details": flow_gate_details,
            "slot_ledger_states": [],
        },
    }
