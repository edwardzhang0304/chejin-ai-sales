from __future__ import annotations

import hashlib
import json
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .models import WechatReadTarget
from .c2_contract import (
    c2_contract_v3,
    contract_revision,
    contract_row_rules,
    contract_sha256,
    contract_values,
    observation_role_is_trusted,
)
from .storage import save_c2_state


OMNIAUTO_ROOT = Path(__file__).resolve().parents[1] / "omniauto-rpa"
if str(OMNIAUTO_ROOT) not in sys.path:
    sys.path.insert(0, str(OMNIAUTO_ROOT))

from apps.wechat_ai_customer_service.adapters.wechat_win32_ocr.text_normalization import (  # noqa: E402
    classify_c2_conversation_title,
    extract_c2_remark_codes,
)


IMAGE_PERSISTENCE_POLICY = dict(c2_contract_v3().get("image_persistence_policy") or {})
IMAGE_RUNTIME_FIELDS = set(IMAGE_PERSISTENCE_POLICY.get("forbidden_field_names") or [])

IMAGE_RUNTIME_FIELD_PREFIXES = (
    "provider_response",
    "raw_provider_response",
    "retry_response",
    "initial_response",
)


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


def normalized_content_hash(value: Any) -> str:
    text = re.sub(r"\s+", " ", str(value or "").strip())
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]


def extract_remark_codes(*values: Any) -> list[str]:
    return extract_c2_remark_codes(*values)


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
    for item in sessions:
        if not isinstance(item, dict):
            continue
        display_name = str(item.get("name") or item.get("title") or item.get("display_name") or "").strip()
        if not display_name:
            continue
        rpa_session_key = str(item.get("session_key") or "").strip()
        if not rpa_session_key:
            missing_session_key_excluded_count += 1
            continue
        raw_title = str(item.get("raw_title") or display_name).strip()
        detected_codes = extract_remark_codes(raw_title)
        admitted_codes: list[str] = []
        admission_type = "unknown"
        if len(detected_codes) == 1:
            admission = classify_c2_conversation_title(raw_title, detected_codes[0])
            admission_type = str(admission.get("conversation_type") or "unknown")
            if admission.get("admission_allowed"):
                admitted_codes = detected_codes
        admission_counts[admission_type if admission_type in admission_counts else "unknown"] += 1
        preview = str(item.get("content") or item.get("preview") or item.get("last_message_preview") or "")
        fingerprint = row_fingerprint(item.get("row_fingerprint"))
        mapped.append(
            {
                "rpa_session_key": rpa_session_key,
                "display_name": display_name[:255],
                # A preview may quote another contact or group member name. Only
                # the session title is authoritative enough for automatic binding.
                "remark_code_candidates": admitted_codes,
                "row_fingerprint": fingerprint or None,
                "unread_hint": bool(item.get("unread_signal") or item.get("unread") or item.get("unread_badge")),
                "last_message_preview": preview[:1000] or None,
                "ocr_confidence": item.get("ocr_confidence"),
            }
        )
    payload = {
        "scan_id": f"scan-{uuid.uuid4()}",
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
                "rule": "valid_remark_code_and_private_title_only",
            },
        },
        "scan_failed": not bool(sidecar_payload.get("ok")),
        "error_code": error_code or (None if sidecar_payload.get("ok") else str(sidecar_payload.get("error_code") or sidecar_payload.get("state") or "SESSION_SCAN_FAILED")),
    }
    save_c2_state("last_scan", {"scan_id": payload["scan_id"], "sidecar_run_id": payload["sidecar_run_id"], "session_count": len(mapped), "finished_at": payload["finished_at"]})
    return payload


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


def observation_identity_signature(observation: dict[str, Any]) -> str:
    row_kind = str(observation.get("row_kind") or "").strip().lower()
    role = str(observation.get("sender_role") or "").strip().lower()
    message_type_value = str(observation.get("message_type") or "").strip().lower()
    if row_kind == "image_bubble":
        source = observation.get("source_message") if isinstance(observation.get("source_message"), dict) else {}
        anchor = observation.get("image_physical_anchor")
        if not isinstance(anchor, dict):
            anchor = source.get("image_physical_anchor")
        anchor = anchor if isinstance(anchor, dict) else {}
        basis = {
            "row_kind": row_kind,
            "sender_role": role,
            "message_type": message_type_value,
            "preceding_stable_message": anchor.get("preceding_stable_message"),
            "following_stable_message": anchor.get("following_stable_message"),
            "bubble_visual_fingerprint": anchor.get("bubble_visual_fingerprint"),
            "occurrence_index": anchor.get("occurrence_index"),
        }
    else:
        basis = {
            "row_kind": row_kind,
            "sender_role": role,
            "message_type": message_type_value,
            "content_hash": normalized_content_hash(observation.get("content_clean") or ""),
        }
    return stable_digest(basis, length=40)


def observation_alignment_signature(observation: dict[str, Any]) -> str:
    """Return the coordinate-free signature used only for viewport alignment."""

    row_kind = str(observation.get("row_kind") or "").strip().lower()
    if row_kind != "image_bubble":
        return observation_identity_signature(observation)
    role = str(observation.get("sender_role") or "").strip().lower()
    message_type_value = str(
        observation.get("message_type") or ""
    ).strip().lower()
    source = (
        observation.get("source_message")
        if isinstance(observation.get("source_message"), dict)
        else {}
    )
    anchor = observation.get("image_physical_anchor")
    if not isinstance(anchor, dict):
        anchor = source.get("image_physical_anchor")
    anchor = anchor if isinstance(anchor, dict) else {}
    return stable_digest(
        {
            "row_kind": row_kind,
            "sender_role": role,
            "message_type": message_type_value,
            "bubble_visual_fingerprint": anchor.get(
                "bubble_visual_fingerprint"
            ),
        },
        length=40,
    )


def _stored_frame_signature_matches(
    stored_item: dict[str, Any],
    *,
    current_index: int,
    current_identity_signatures: list[str],
    current_alignment_signatures: list[str],
) -> bool:
    """Compare one stored slot with the matching current contract version."""

    stored_alignment = str(
        stored_item.get("alignment_signature") or ""
    ).strip()
    stored_signature = str(stored_item.get("signature") or "").strip()
    if stored_alignment:
        return stored_alignment == current_alignment_signatures[current_index]
    return stored_signature == current_identity_signatures[current_index]


def _viewport_overlap_matches(
    previous_frame: list[dict[str, Any]],
    current_identity_signatures: list[str],
    current_alignment_signatures: list[str],
) -> tuple[dict[int, str], bool]:
    """Align the previous visible suffix with the current visible prefix."""

    max_overlap = min(
        len(previous_frame),
        len(current_alignment_signatures),
    )
    overlap_lengths = [
        length
        for length in range(1, max_overlap + 1)
        if all(
            _stored_frame_signature_matches(
                previous_frame[
                    len(previous_frame) - length + offset
                ],
                current_index=offset,
                current_identity_signatures=current_identity_signatures,
                current_alignment_signatures=current_alignment_signatures,
            )
            for offset in range(length)
        )
    ]
    if len(overlap_lengths) > 1:
        return {}, True
    if not overlap_lengths:
        return {}, False
    overlap = overlap_lengths[0]
    previous_start = len(previous_frame) - overlap
    return (
        {
            current_index: str(
                previous_frame[previous_start + current_index]["stable_id"]
            )
            for current_index in range(overlap)
        },
        False,
    )


def reconcile_cross_round_observation_identities(
    observations: list[Any],
    previous_state: dict[str, Any] | None = None,
) -> tuple[list[Any], dict[str, Any], list[dict[str, Any]]]:
    """Assign Worker-owned stable IDs by aligning consecutive visible sequences."""

    state = previous_state if isinstance(previous_state, dict) else {}
    previous_frame = [
        item
        for item in (state.get("last_frame") or [])
        if isinstance(item, dict) and item.get("signature") and item.get("stable_id")
    ]
    recent_frames = [
        [
            item
            for item in frame
            if isinstance(item, dict) and item.get("signature") and item.get("stable_id")
        ]
        for frame in (state.get("recent_frames") or [])
        if isinstance(frame, list)
    ]
    recent_frames = [frame for frame in recent_frames if frame]
    catalog = {
        str(signature): [str(value) for value in values if str(value)]
        for signature, values in (state.get("catalog") or {}).items()
        if isinstance(values, list)
    }
    original_catalog = {key: list(values) for key, values in catalog.items()}
    legacy_dedupe_overrides = {
        str(stable_id): str(dedupe_key)
        for stable_id, dedupe_key in (state.get("legacy_dedupe_overrides") or {}).items()
        if str(stable_id) and str(dedupe_key)
    }
    next_sequence = max(1, int(state.get("next_sequence") or 1))
    enriched = [dict(item) if isinstance(item, dict) else item for item in observations]
    slots: list[dict[str, Any]] = []
    for index, observation in enumerate(enriched):
        if not isinstance(observation, dict):
            continue
        row_kind = str(observation.get("row_kind") or "").strip().lower()
        if row_kind not in {"text_bubble", "image_bubble", "system_message"}:
            continue
        if not observation_role_is_trusted(observation):
            continue
        slots.append(
            {
                "authority_index": index,
                "rect": message_rect({"bubble_rect": observation.get("bubble_rect")}),
                "signature": observation_identity_signature(observation),
                "alignment_signature": observation_alignment_signature(
                    observation
                ),
            }
        )
    ordered = order_authoritative_slots(slots)
    current_signatures = [str(item["signature"]) for item in ordered]
    current_alignment_signatures = [
        str(item["alignment_signature"]) for item in ordered
    ]
    matches, overlap_ambiguous = _viewport_overlap_matches(
        previous_frame,
        current_signatures,
        current_alignment_signatures,
    )
    if overlap_ambiguous:
        return (
            enriched,
            state,
            [
                {
                    "observation_id": "frame",
                    "row_kind": "message_sequence",
                    "error_code": "MESSAGE_CROSS_ROUND_IDENTITY_AMBIGUOUS",
                    "signature": (
                        current_alignment_signatures[0]
                        if current_alignment_signatures
                        else ""
                    ),
                    "reason": "multiple_viewport_suffix_prefix_overlaps",
                }
            ],
        )

    # A temporarily unrelated viewport must not erase recoverable identity
    # evidence. Restore an older frame only when the complete ordered
    # signature sequence matches; a single repeated message remains
    # ambiguous and is never guessed from the catalog.
    if not matches and len(current_alignment_signatures) > 1:
        exact_historical_frames = [
            frame
            for frame in [previous_frame, *recent_frames]
            if len(frame) == len(current_alignment_signatures)
            and all(
                _stored_frame_signature_matches(
                    item,
                    current_index=index,
                    current_identity_signatures=current_signatures,
                    current_alignment_signatures=(
                        current_alignment_signatures
                    ),
                )
                for index, item in enumerate(frame)
            )
        ]
        unique_identity_sequences = {
            tuple(str(item["stable_id"]) for item in frame)
            for frame in exact_historical_frames
        }
        if len(unique_identity_sequences) == 1:
            stable_ids = next(iter(unique_identity_sequences))
            matches = {index: stable_id for index, stable_id in enumerate(stable_ids)}

    used_ids = set(matches.values())
    errors: list[dict[str, Any]] = []
    frame: list[dict[str, str]] = []
    for ordered_index, slot in enumerate(ordered):
        observation_index = int(slot["authority_index"])
        observation = enriched[observation_index]
        signature = str(slot["signature"])
        alignment_signature = str(slot["alignment_signature"])
        stable_id = matches.get(ordered_index, "")
        if not stable_id:
            historical = [value for value in catalog.get(signature, []) if value not in used_ids]
            if historical:
                errors.append(
                    {
                        "observation_id": str(observation.get("observation_id") or f"observation-{observation_index}"),
                        "row_kind": str(observation.get("row_kind") or ""),
                        "error_code": "MESSAGE_CROSS_ROUND_IDENTITY_AMBIGUOUS",
                        "signature": signature,
                    }
                )
                frame.append(
                    {
                        "signature": signature,
                        "alignment_signature": alignment_signature,
                        "stable_id": "",
                    }
                )
                continue
            stable_id = f"worker-message-{next_sequence}"
            next_sequence += 1
        used_ids.add(stable_id)
        observation["_worker_stable_id"] = stable_id
        if stable_id in legacy_dedupe_overrides:
            observation["_worker_legacy_dedupe_key"] = legacy_dedupe_overrides[stable_id]
        known = catalog.setdefault(signature, [])
        if stable_id not in known:
            known.append(stable_id)
            del known[:-50]
        frame.append(
            {
                "signature": signature,
                "alignment_signature": alignment_signature,
                "stable_id": stable_id,
            }
        )

    if errors:
        return (
            enriched,
            {
                "version": int(state.get("version") or 2),
                "next_sequence": max(1, int(state.get("next_sequence") or 1)),
                "last_frame": previous_frame,
                "recent_frames": recent_frames,
                "catalog": original_catalog,
                "legacy_dedupe_overrides": legacy_dedupe_overrides,
            },
            errors,
        )
    trimmed_catalog = dict(list(catalog.items())[-500:])
    frame_history = [previous_frame, *recent_frames] if previous_frame else recent_frames
    unique_frame_history: list[list[dict[str, str]]] = []
    seen_frame_sequences: set[tuple[tuple[str, str], ...]] = set()
    for historical_frame in frame_history:
        sequence = tuple(
            (
                str(
                    item.get("alignment_signature")
                    or item.get("signature")
                    or ""
                ),
                str(item["stable_id"]),
            )
            for item in historical_frame
        )
        if not sequence or sequence in seen_frame_sequences:
            continue
        seen_frame_sequences.add(sequence)
        unique_frame_history.append(historical_frame)
        if len(unique_frame_history) >= 5:
            break
    new_state = {
        "version": 3,
        "next_sequence": next_sequence,
        "last_frame": frame,
        "recent_frames": unique_frame_history,
        "catalog": trimmed_catalog,
        "legacy_dedupe_overrides": legacy_dedupe_overrides,
    }
    return enriched, new_state, errors


def reconcile_v16104_identity_transition(
    target: WechatReadTarget,
    observations: list[Any],
    previous_state: dict[str, Any] | None,
) -> tuple[list[Any], dict[str, Any], list[dict[str, Any]]]:
    """Bridge legacy dedupe keys once, then keep only Worker sequence identities."""

    existing_state = dict(previous_state) if isinstance(previous_state, dict) else {}
    checkpoint = (
        target.raw.get("identity_checkpoint")
        if isinstance(target.raw, dict)
        and isinstance(target.raw.get("identity_checkpoint"), dict)
        else {}
    )
    try:
        sequence_floor = max(
            1,
            int(checkpoint.get("next_sequence_floor") or 1),
        )
    except (TypeError, ValueError):
        sequence_floor = 1
    existing_state["next_sequence"] = max(
        sequence_floor,
        int(existing_state.get("next_sequence") or 1),
    )
    recent_messages = [
        item
        for item in (checkpoint.get("recent_messages") or [])
        if isinstance(item, dict)
        and str(item.get("stable_id") or "").strip()
        and str(item.get("alignment_signature") or "").strip()
    ]
    checkpoint_frame = [
        {
            "signature": str(item["alignment_signature"]),
            "alignment_signature": str(item["alignment_signature"]),
            "stable_id": str(item["stable_id"]),
        }
        for item in recent_messages
    ]
    checkpoint_digest = hashlib.sha256(
        json.dumps(
            {
                "version": checkpoint.get("version"),
                "next_sequence_floor": sequence_floor,
                "recent_messages": recent_messages,
            },
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        ).encode("utf-8")
    ).hexdigest()
    if (
        checkpoint_frame
        and existing_state.get("server_checkpoint_digest")
        != checkpoint_digest
    ):
        previous_frame = [
            item
            for item in (existing_state.get("last_frame") or [])
            if isinstance(item, dict)
        ]
        local_next_sequence = int(
            existing_state.get("next_sequence") or 1
        )
        if not previous_frame or sequence_floor >= local_next_sequence:
            if previous_frame:
                existing_state["recent_frames"] = [
                    previous_frame,
                    *(existing_state.get("recent_frames") or []),
                ][:5]
            existing_state["last_frame"] = checkpoint_frame
        else:
            existing_state["recent_frames"] = [
                checkpoint_frame,
                *(existing_state.get("recent_frames") or []),
            ][:5]
    existing_state["server_checkpoint_digest"] = checkpoint_digest
    existing_state["server_next_sequence_floor"] = sequence_floor

    def attach_checkpoint_state(
        result: tuple[list[Any], dict[str, Any], list[dict[str, Any]]],
    ) -> tuple[list[Any], dict[str, Any], list[dict[str, Any]]]:
        enriched, state, errors = result
        state = dict(state)
        state["server_checkpoint_digest"] = checkpoint_digest
        state["server_next_sequence_floor"] = sequence_floor
        return enriched, state, errors
    if existing_state.get("legacy_transition_completed") is True:
        return attach_checkpoint_state(
            reconcile_cross_round_observation_identities(
                observations,
                existing_state,
            )
        )
    transition = target.raw.get("identity_transition") if isinstance(target.raw, dict) else {}
    try:
        transition_version = (
            int(transition.get("version") or 0)
            if isinstance(transition, dict)
            else 0
        )
    except (TypeError, ValueError):
        transition_version = 0
    enriched, state, errors = attach_checkpoint_state(
        reconcile_cross_round_observation_identities(
            observations,
            existing_state,
        )
    )
    if errors or transition_version != 1:
        return enriched, state, errors
    legacy_messages = transition.get("legacy_messages") if isinstance(transition, dict) else []
    legacy_keys = {
        str(item.get("dedupe_key") or "").strip()
        for item in legacy_messages
        if isinstance(item, dict) and str(item.get("dedupe_key") or "").strip()
    }
    if not legacy_keys:
        state["legacy_transition_completed"] = True
        state["legacy_transition_source"] = str(
            transition.get("source_version") or "v16.104"
        )
        return enriched, state, []

    slots: list[dict[str, Any]] = []
    for index, observation in enumerate(enriched):
        if not isinstance(observation, dict):
            continue
        if str(observation.get("row_kind") or "").strip().lower() not in {
            "text_bubble",
            "system_message",
        }:
            continue
        if not observation_role_is_trusted(observation):
            continue
        slots.append(
            {
                "authority_index": index,
                "rect": message_rect({"bubble_rect": observation.get("bubble_rect")}),
            }
        )
    ordered_slots = order_authoritative_slots(slots)
    ordered_messages = [enriched[int(slot["authority_index"])] for slot in ordered_slots]
    legacy_messages_for_identity: list[dict[str, Any]] = []
    for item in ordered_messages:
        clean_item = dict(item)
        clean_item.pop("_worker_stable_id", None)
        clean_item.pop("_worker_legacy_dedupe_key", None)
        clean_item["content"] = str(
            clean_item.get("content_clean") or clean_item.get("content") or ""
        ).strip()
        clean_item["type"] = str(
            clean_item.get("message_type") or clean_item.get("type") or ""
        ).strip()
        legacy_messages_for_identity.append(clean_item)
    matched_positions: list[int] = []
    overrides = dict(state.get("legacy_dedupe_overrides") or {})
    for position, observation in enumerate(ordered_messages):
        legacy_observation = legacy_messages_for_identity[position]
        try:
            legacy_key, _, _ = message_dedupe_metadata(
                target,
                legacy_observation,
                position,
                messages=legacy_messages_for_identity,
            )
        except ValueError:
            continue
        if legacy_key not in legacy_keys:
            continue
        stable_id = str(observation.get("_worker_stable_id") or "").strip()
        if not stable_id:
            continue
        matched_positions.append(position)
        overrides[stable_id] = legacy_key
        observation["_worker_legacy_dedupe_key"] = legacy_key

    if matched_positions and matched_positions != list(range(len(matched_positions))):
        return (
            enriched,
            state,
            [
                {
                    "observation_id": "frame",
                    "row_kind": "message_sequence",
                    "error_code": "MESSAGE_LEGACY_IDENTITY_TRANSITION_AMBIGUOUS",
                    "matched_positions": matched_positions,
                    "reason": "legacy_messages_are_not_a_contiguous_visible_prefix",
                }
            ],
        )
    state["legacy_dedupe_overrides"] = overrides
    state["legacy_transition_completed"] = True
    state["legacy_transition_source"] = str(transition.get("source_version") or "v16.104")
    return enriched, state, []


def sender_role_group(message: dict[str, Any]) -> str:
    role = sender_role_hint(message)
    if role in {"self", "sales", "sales_candidate"}:
        return "self"
    if role in {"customer", "contact"}:
        return "customer"
    return role or "unknown"


def worker_source_message_key(
    target: WechatReadTarget,
    *,
    identity_kind: str,
    identity: Any,
) -> str:
    """Create the only cross-round source identity owned by Worker."""

    clean_kind = str(identity_kind or "").strip().lower()
    if not clean_kind or identity in (None, "", {}, []):
        raise ValueError("MESSAGE_SOURCE_IDENTITY_MISSING")
    return (
        "source:"
        + stable_digest(
            {
                "conversation_id": target.conversation_id,
                "identity_kind": clean_kind,
                "identity": identity,
            },
            length=40,
        )
    )[:255]


def source_message_key_from_dedupe(target: WechatReadTarget, dedupe_key: str) -> str:
    clean = str(dedupe_key or "").strip()
    if not clean:
        raise ValueError("MESSAGE_DEDUPE_KEY_MISSING")
    return worker_source_message_key(
        target,
        identity_kind="worker_dedupe_key",
        identity=clean,
    )


def image_observation_source_key(target: WechatReadTarget, observation: dict[str, Any]) -> str:
    worker_stable_id = str(observation.get("_worker_stable_id") or "").strip()
    if worker_stable_id:
        return worker_source_message_key(
            target,
            identity_kind="worker_sequence",
            identity=worker_stable_id,
        )
    source = observation.get("source_message") if isinstance(observation.get("source_message"), dict) else {}
    physical_anchor = observation.get("image_physical_anchor")
    if not isinstance(physical_anchor, dict):
        physical_anchor = source.get("image_physical_anchor")
    if not isinstance(physical_anchor, dict):
        raise ValueError("MESSAGE_SOURCE_IDENTITY_MISSING")
    stable_anchor = {
        key: physical_anchor.get(key)
        for key in (
            "sender_role",
            "preceding_stable_message",
            "following_stable_message",
            "bubble_visual_fingerprint",
            "occurrence_index",
        )
        if physical_anchor.get(key) not in (None, "")
    }
    if not stable_anchor:
        raise ValueError("MESSAGE_SOURCE_IDENTITY_MISSING")
    return worker_source_message_key(
        target,
        identity_kind="image_physical_anchor",
        identity=stable_anchor,
    )


def voice_observation_anchor_key(observation: dict[str, Any]) -> str:
    source = observation.get("source_message") if isinstance(observation.get("source_message"), dict) else {}
    action_target = (
        observation.get("action_target")
        if isinstance(observation.get("action_target"), dict)
        else {}
    )
    anchor = source.get("voice_anchor") if isinstance(source.get("voice_anchor"), dict) else {}
    # ``stable`` and ``structural`` are aliases of one physical bubble. The
    # structural key is viewport-shift invariant and is therefore the only
    # canonical identity whenever OmniAuto provides it. A transcript may keep
    # the stable alias in parent_voice_anchor_key, so parent must not win.
    for value in (
        observation.get("_voice_canonical_anchor_key"),
        observation.get("voice_anchor_structural_key"),
        source.get("voice_anchor_structural_key"),
        action_target.get("anchor_structural_key"),
        anchor.get("anchor_structural_key"),
        observation.get("voice_anchor_stable_key"),
        source.get("voice_anchor_stable_key"),
        action_target.get("anchor_stable_key"),
        anchor.get("anchor_stable_key"),
        observation.get("parent_voice_anchor_key"),
        observation.get("voice_anchor_key"),
        source.get("parent_voice_anchor_key"),
        source.get("voice_anchor_key"),
        action_target.get("anchor_key"),
        anchor.get("anchor_key"),
    ):
        clean = str(value or "").strip()
        if clean:
            return clean
    return ""


def voice_observation_source_key(target: WechatReadTarget, observation: dict[str, Any]) -> str:
    anchor_key = voice_observation_anchor_key(observation)
    if not anchor_key:
        raise ValueError("MESSAGE_SOURCE_IDENTITY_MISSING")
    return worker_source_message_key(
        target,
        identity_kind="voice_physical_anchor",
        identity=anchor_key,
    )


def apply_image_terminal_result(observation: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    enriched = dict(observation)
    state = str(result.get("state") or "failed").strip().lower()
    if state not in {"completed", "failed"}:
        state = "failed"
    enriched["item_state"] = state
    error_code = str(result.get("reason") or "").strip()
    reason_detail = str(
        result.get("reason_detail")
        or (
            result.get("transaction", {}).get("status")
            if isinstance(result.get("transaction"), dict)
            else ""
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
    transaction = result.get("transaction") if isinstance(result.get("transaction"), dict) else {}
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
    source_message_key: str,
) -> dict[str, Any]:
    """Freeze one terminal image fact without image bytes or runtime paths."""

    projected = _drop_image_runtime_fields(dict(observation))
    source = (
        projected.get("source_message")
        if isinstance(projected.get("source_message"), dict)
        else {}
    )
    projected["source_message"] = {
        **_drop_image_runtime_fields(dict(source)),
        "source_message_key": str(source_message_key or "").strip(),
    }
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


def ocr_message_identity_context(messages: list[Any], message: dict[str, Any], index: int) -> dict[str, Any]:
    role = sender_role_group(message)
    msg_type = message_type(message)
    content_hash = normalized_content_hash(message.get("content") or message.get("content_raw_ocr") or "")
    occurrence_index = 0
    previous_same_role = ""
    next_same_role = ""
    for candidate_index, candidate in enumerate(messages):
        if not isinstance(candidate, dict):
            continue
        candidate_role = sender_role_group(candidate)
        candidate_type = message_type(candidate)
        candidate_content_hash = normalized_content_hash(candidate.get("content") or candidate.get("content_raw_ocr") or "")
        if candidate_index < index and candidate_role == role:
            previous_same_role = stable_digest(
                {"type": candidate_type, "content_hash": candidate_content_hash},
                length=20,
            )
        elif candidate_index > index and candidate_role == role and not next_same_role:
            next_same_role = stable_digest(
                {"type": candidate_type, "content_hash": candidate_content_hash},
                length=20,
            )
        if (
            candidate_index < index
            and candidate_role == role
            and candidate_type == msg_type
            and candidate_content_hash == content_hash
        ):
            occurrence_index += 1
    return {
        "sender": role,
        "type": msg_type,
        "content_hash": content_hash,
        "occurrence_index": occurrence_index,
        "previous_same_role": previous_same_role,
        "next_same_role": next_same_role,
    }


def message_dedupe_metadata(
    target: WechatReadTarget,
    message: dict[str, Any],
    index: int,
    *,
    messages: list[Any] | None = None,
) -> tuple[str, str, dict[str, Any]]:
    worker_stable_id = str(message.get("_worker_stable_id") or "").strip()
    if worker_stable_id:
        base = {
            "conversation_id": target.conversation_id,
            "worker_stable_id": worker_stable_id,
        }
        return (
            f"{target.conversation_id}:{stable_digest(base, length=32)}"[:255],
            "high",
            {"source": "worker_cross_round_sequence", **base},
        )
    content = str(message.get("content") or message.get("content_raw_ocr") or "")
    voice_anchor_id = voice_anchor_identity(message)
    if message_type(message) == "voice" and content.strip() and not content_looks_like_untranscribed_voice_placeholder(content):
        identity = ocr_message_identity_context(messages or [message], message, index if messages else 0)
        occurrence_index = int(message.get("_voice_occurrence_index") or identity["occurrence_index"] or 0)
        base = {
            "conversation_id": target.conversation_id,
            "remark_code": target.remark_code,
            "sender": sender_role_group(message),
            "type": "voice",
            "content_hash": normalized_content_hash(content),
            "voice_duration": voice_duration_seconds(message),
            "occurrence_index": occurrence_index,
        }
        return (
            f"{target.conversation_id}:{stable_digest(base, length=32)}"[:255],
            "high",
            {"source": "voice_semantic_identity", **base},
        )
    if message_type(message) == "voice" and voice_anchor_id:
        base = {
            "conversation_id": target.conversation_id,
            "remark_code": target.remark_code,
            "sender": sender_role_group(message),
            "type": "voice",
            "voice_anchor_id": voice_anchor_id,
        }
        return (
            f"{target.conversation_id}:{stable_digest(base, length=32)}"[:255],
            "high",
            {"source": "voice_anchor_identity", **base},
        )
    if message_type(message) == "image":
        observation = message.get("observation") if isinstance(message.get("observation"), dict) else {}
        physical_source_key = image_observation_source_key(target, observation)
        base = {
            "conversation_id": target.conversation_id,
            "remark_code": target.remark_code,
            "sender": sender_role_group(message),
            "type": "image",
            "physical_source_key": physical_source_key,
        }
        return (
            f"{target.conversation_id}:{stable_digest(base, length=32)}"[:255],
            "high",
            {"source": "worker_image_physical_identity", **base},
        )
    if content.strip():
        identity = ocr_message_identity_context(messages or [message], message, index if messages else 0)
        base = {
            "conversation_id": target.conversation_id,
            "remark_code": target.remark_code,
            "sender": identity["sender"],
            "type": identity["type"],
            "content_hash": identity["content_hash"],
            "occurrence_index": identity["occurrence_index"],
        }
        return (
            f"{target.conversation_id}:{stable_digest(base, length=32)}"[:255],
            "medium",
            {"source": "worker_structural_identity", **base, "context": identity},
        )
    raise ValueError("MESSAGE_DEDUPE_IDENTITY_UNCONFIRMED")


def voice_anchor_identity(message: dict[str, Any]) -> str:
    for key in ("voice_anchor_stable_key", "voice_anchor_key"):
        value = str(message.get(key) or "").strip()
        if value:
            return value
    anchor = message.get("voice_anchor")
    if isinstance(anchor, dict):
        for key in ("anchor_stable_key", "anchor_key"):
            value = str(anchor.get(key) or "").strip()
            if value:
                return value
    return ""


def voice_duration_seconds(message: dict[str, Any]) -> int | None:
    for key in ("voice_duration", "voice_seconds", "audio_duration", "audio_seconds"):
        value = message.get(key)
        if value is None:
            continue
        try:
            return int(float(value))
        except (TypeError, ValueError):
            continue
    text = str(message.get("voice_duration_text") or "")
    match = re.search(r"\d{1,3}", text)
    return int(match.group(0)) if match else None


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
) -> dict[str, Any]:
    if not target.authorization_revision:
        raise ValueError("C2_TARGET_AUTHORIZATION_REVISION_MISSING")
    if int(sidecar_payload.get("observation_schema_version") or 0) != 3:
        raise ValueError("C2_OBSERVATION_SCHEMA_VERSION_REQUIRED")
    observations = sidecar_payload.get("observations")
    if not isinstance(observations, list):
        raise ValueError("C2 V3 payload is missing observations")
    worker_stable_ids: dict[str, str] = {}
    worker_legacy_dedupe_keys: dict[str, str] = {}
    worker_ai_reply_receipts: dict[str, dict[str, Any]] = {}
    sanitized_observations: list[Any] = []
    for item in observations:
        if not isinstance(item, dict):
            sanitized_observations.append(item)
            continue
        observation = _drop_image_runtime_fields(dict(item))
        observation_id = str(observation.get("observation_id") or "").strip()
        worker_stable_id = str(observation.pop("_worker_stable_id", "") or "").strip()
        worker_legacy_dedupe_key = str(
            observation.pop("_worker_legacy_dedupe_key", "") or ""
        ).strip()
        worker_ai_reply_receipt = observation.pop(
            "_worker_ai_reply_receipt", None
        )
        if observation_id and worker_stable_id:
            worker_stable_ids[observation_id] = worker_stable_id
        if observation_id and worker_legacy_dedupe_key:
            worker_legacy_dedupe_keys[observation_id] = worker_legacy_dedupe_key
        if observation_id and isinstance(worker_ai_reply_receipt, dict):
            worker_ai_reply_receipts[observation_id] = dict(
                worker_ai_reply_receipt
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
        identity_index: int,
        identity_sources: list[dict[str, Any]],
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
        dedupe_key, confidence, basis = message_dedupe_metadata(
            target,
            normalized_source,
            identity_index,
            messages=identity_sources,
        )
        legacy_dedupe_key = str(source.get("_worker_legacy_dedupe_key") or "").strip()
        if legacy_dedupe_key:
            dedupe_key = legacy_dedupe_key
            confidence = "high"
            basis = {
                "source": "v16104_identity_transition",
                "legacy_dedupe_key": legacy_dedupe_key,
                "worker_stable_id": str(source.get("_worker_stable_id") or ""),
            }
        source_observation = source.get("observation") if isinstance(source.get("observation"), dict) else {}
        worker_stable_id = str(source.get("_worker_stable_id") or "").strip()
        if msg_type == "voice":
            canonical_source_key = voice_observation_source_key(target, source_observation)
        elif msg_type == "image":
            canonical_source_key = (
                worker_source_message_key(
                    target,
                    identity_kind="worker_sequence",
                    identity=worker_stable_id,
                )
                if worker_stable_id
                else image_observation_source_key(target, source_observation)
            )
        else:
            canonical_source_key = source_message_key_from_dedupe(target, dedupe_key)
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
                if key not in {"_worker_stable_id", "_worker_legacy_dedupe_key"}
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
                    else (flow_state if msg_type == "voice" else "completed")
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
            "_worker_legacy_dedupe_key": worker_legacy_dedupe_keys.get(
                str(observation.get("observation_id") or "").strip(),
                "",
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
        if int(observation.get("schema_version") or 0) != 3:
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
                    "source_message_key": voice_observation_source_key(target, observation),
                }
                candidate = {
                    "source": voice_source,
                    "role": role,
                    "msg_type": "voice",
                    "content": content,
                    "source_index": index,
                    "voice_meta": voice_transcription_meta(voice_summary, message=source),
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
                        "source_message_key": voice_observation_source_key(
                            target,
                            observation,
                        ),
                    },
                    "role": role,
                    "msg_type": "voice",
                    "content": None,
                    "source_index": index,
                    "voice_meta": None,
                    "item_state": "failed",
                }
            elif row_kind == "image_bubble" and item_state in {"completed", "failed"}:
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

    candidates = [slot["candidate"] for slot in ordered_slots if isinstance(slot.get("candidate"), dict)]
    identity_sources = [
        {
            **candidate["source"],
            "sender_role": candidate["role"],
            "sender": candidate["role"],
            "type": candidate["msg_type"],
            "content": candidate["content"],
        }
        for candidate in candidates
    ]
    identity_index = 0
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
            identity_index=identity_index,
            identity_sources=identity_sources,
            message_position=message_position,
            voice_meta=candidate["voice_meta"],
            item_state=str(candidate.get("item_state") or "completed"),
        )
        identity_index += 1

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
    flow_gate_details = [
        dict(item)
        for item in (sidecar_payload.get("flow_gate_details") or [])
        if isinstance(item, dict)
    ]
    return {
        "contract_version": 3,
        "contract_revision": contract_revision(),
        "contract_sha256": contract_sha256(),
        "observation_schema_version": 3,
        "read_run_id": f"read-{uuid.uuid4()}",
        "conversation_id": target.conversation_id,
        "remark_code": target.remark_code,
        "rpa_session_key": target.rpa_session_key,
        "authorization_revision": target.authorization_revision,
        "messages": mapped,
        "evidence": {
            "contract_version": 3,
            "contract_revision": contract_revision(),
            "contract_sha256": contract_sha256(),
            "observation_schema_version": 3,
            "authoritative_frame_source": authoritative_frame_source,
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
            "slot_ledger_states": list(sidecar_payload.get("slot_ledger_states") or []),
            "historical_warnings": list(
                sidecar_payload.get("historical_warnings") or []
            ),
            "recoverable_handoff_resolution": sidecar_payload.get(
                "recoverable_handoff_resolution"
            ),
        },
    }


def build_message_ingest_payload(target: WechatReadTarget, sidecar_payload: dict[str, Any]) -> dict[str, Any]:
    if int(sidecar_payload.get("observation_schema_version") or 0) != 3:
        raise ValueError("C2_OBSERVATION_SCHEMA_VERSION_REQUIRED")
    return _build_message_ingest_payload_v3(target, sidecar_payload)


def build_flow_gate_ingest_payload(
    target: WechatReadTarget,
    *,
    error_code: str,
    evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not target.authorization_revision:
        raise ValueError("C2_TARGET_AUTHORIZATION_REVISION_MISSING")
    clean_code = str(error_code or "").strip()
    if not clean_code:
        raise ValueError("C2_FLOW_GATE_ERROR_CODE_MISSING")
    clean_evidence = dict(evidence or {})
    stable_gate_key = str(clean_evidence.get("flow_gate_identity_key") or "").strip()
    read_run_id = (
        f"flow-gate-{stable_gate_key[:48]}"
        if stable_gate_key
        else f"read-{uuid.uuid4()}"
    )
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
        "read_run_id": read_run_id,
        "conversation_id": target.conversation_id,
        "remark_code": target.remark_code,
        "rpa_session_key": target.rpa_session_key,
        "authorization_revision": target.authorization_revision,
        "messages": [],
        "evidence": {
            **clean_evidence,
            "contract_version": 3,
            "contract_revision": contract_revision(),
            "contract_sha256": contract_sha256(),
            "observation_schema_version": 3,
            "authoritative_frame_source": "initial_read",
            "observations": [],
            "read_reason": target.read_reason,
            "authorization_read_reason": authorization_read_reason,
            "continuation_batch_id": (
                str(continuation.get("batch_id") or "").strip() or None
            ),
            "continuation_token": (
                str(continuation.get("token") or "").strip() or None
            ),
            "finished_at": now_iso(),
            "flow_gate_errors": [clean_code],
            "flow_gate_details": flow_gate_details,
        },
    }
