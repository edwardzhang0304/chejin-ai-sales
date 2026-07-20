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
from .c2_contract import contract_values
from .storage import save_c2_state


OMNIAUTO_ROOT = Path(__file__).resolve().parents[1] / "omniauto-rpa"
if str(OMNIAUTO_ROOT) not in sys.path:
    sys.path.insert(0, str(OMNIAUTO_ROOT))

from apps.wechat_ai_customer_service.adapters.wechat_win32_ocr.text_normalization import (  # noqa: E402
    classify_c2_conversation_title,
    extract_c2_remark_codes,
)



def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def stable_digest(payload: Any, length: int = 16) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:length]


def normalized_content_hash(value: Any) -> str:
    text = re.sub(r"\s+", " ", str(value or "").strip())
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]


def file_digest(value: Any) -> str:
    path = Path(str(value or ""))
    if not path.exists() or not path.is_file():
        return ""
    hasher = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()[:32]


def occurred_at_bucket(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return "unknown"
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed.replace(second=0, microsecond=0).isoformat()
    except ValueError:
        return text[:16]


def visual_position_fingerprint(message: dict[str, Any], index: int) -> str:
    rect = message.get("bubble_rect") or message.get("rect") or message.get("bounds") or {}
    if isinstance(rect, dict):
        return stable_digest(
            {
                "left": int(float(rect.get("left") or 0)),
                "top": int(float(rect.get("top") or 0)),
                "right": int(float(rect.get("right") or 0)),
                "bottom": int(float(rect.get("bottom") or 0)),
                "index": index,
            },
            length=20,
        )
    return stable_digest({"rect": rect, "index": index}, length=20)


def extract_remark_codes(*values: Any) -> list[str]:
    return extract_c2_remark_codes(*values)


def row_fingerprint(value: Any, fallback: str) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()[:255]
    if value:
        return stable_digest(value, length=24)
    return stable_digest(fallback, length=24)


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
    for index, item in enumerate(sessions):
        if not isinstance(item, dict):
            continue
        display_name = str(item.get("name") or item.get("title") or item.get("display_name") or "").strip()
        if not display_name:
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
        rpa_session_key = str(item.get("session_key") or "").strip() or f"session:{stable_digest([display_name, index], length=20)}"
        preview = str(item.get("content") or item.get("preview") or item.get("last_message_preview") or "")
        fingerprint = row_fingerprint(item.get("row_fingerprint"), fallback=f"{display_name}:{rpa_session_key}:{index}")
        mapped.append(
            {
                "rpa_session_key": rpa_session_key,
                "display_name": display_name[:255],
                # A preview may quote another contact or group member name. Only
                # the session title is authoritative enough for automatic binding.
                "remark_code_candidates": admitted_codes,
                "row_fingerprint": fingerprint,
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
    if value in {"self", "sales", "sales_candidate"}:
        return "self"
    if value in {"customer", "unknown"}:
        return value
    if value == "contact":
        return "customer"
    if value == "system":
        return "system"
    return "unknown"


def message_type(message: dict[str, Any]) -> str:
    value = str(message.get("type") or message.get("message_type") or "").strip().lower()
    if value == "audio":
        return "voice"
    if value in {"text", "image", "system", "voice", "file", "unknown"}:
        return value
    if value == "video":
        return "unknown"
    if any(message.get(key) for key in ("voice_duration", "audio_duration", "voice_seconds", "audio_seconds")):
        return "voice"
    if message.get("image_local_path"):
        return "image"
    return "text"


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


def sender_role_group(message: dict[str, Any]) -> str:
    role = sender_role_hint(message)
    if role in {"self", "sales", "sales_candidate"}:
        return "self"
    if role in {"customer", "contact"}:
        return "customer"
    return role or "unknown"


def source_identity_aliases(message: dict[str, Any]) -> set[str]:
    aliases: set[str] = set()
    envelope = message.get("message_envelope") if isinstance(message.get("message_envelope"), dict) else {}
    for source in (message, envelope):
        for key in ("source_message_key", "canonical_input_id", "canonical_visual_id", "id", "message_id", "bubble_id"):
            value = str(source.get(key) or "").strip().lower()
            if value:
                aliases.add(f"{key}:{value}")
    anchor = voice_anchor_identity(message)
    if anchor:
        aliases.add(f"voice_anchor:{anchor.lower()}")
    return aliases


def source_message_key(
    target: WechatReadTarget,
    message: dict[str, Any],
    *,
    sidecar_id: str,
    fallback_index: int,
) -> str:
    aliases = source_identity_aliases(message)
    preferred_order = (
        "source_message_key:",
        "canonical_visual_id:",
        "canonical_input_id:",
        "id:",
        "message_id:",
        "voice_anchor:",
        "bubble_id:",
    )
    chosen = ""
    for prefix in preferred_order:
        chosen = next((value for value in sorted(aliases) if value.startswith(prefix)), "")
        if chosen:
            break
    seed = chosen or f"observation:{sidecar_id}:{fallback_index}"
    return f"source:{stable_digest({'conversation_id': target.conversation_id, 'identity': seed}, length=40)}"


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
    raw_message_id = str(message.get("id") or message.get("message_id") or "").strip().lower()
    source_adapter = str(message.get("source_adapter") or "").strip().lower()
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
    if content.strip() and (source_adapter == "win32_ocr" or raw_message_id.startswith("win32_ocr:")):
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
            {"source": "ocr_structural_identity", **base, "context": identity},
        )
    for key in ("dedupe_key", "id", "message_id"):
        value = str(message.get(key) or "").strip()
        if value:
            return f"{target.conversation_id}:{value}"[:255], "high", {"source": key, "remark_code": target.remark_code}
    msg_type = message_type(message)
    if msg_type == "image":
        image_hash = str(message.get("image_hash") or message.get("file_hash") or "").strip()
        if not image_hash:
            image_hash = file_digest(message.get("image_local_path"))
        if image_hash:
            base = {
                "conversation_id": target.conversation_id,
                "remark_code": target.remark_code,
                "sender": sender_role_hint(message),
                "type": msg_type,
                "image_hash": image_hash,
                "time_bucket": occurred_at_bucket(message.get("occurred_at") or message.get("time")),
            }
            return f"{target.conversation_id}:{stable_digest(base, length=32)}"[:255], "medium", {"source": "image_hash", "time_bucket": base["time_bucket"]}
    if content.strip():
        base = {
            "conversation_id": target.conversation_id,
            "remark_code": target.remark_code,
            "sender": sender_role_hint(message),
            "type": msg_type,
            "content_hash": normalized_content_hash(content),
            "time_bucket": occurred_at_bucket(message.get("occurred_at") or message.get("time")),
            "visual_position_fingerprint": visual_position_fingerprint(message, index),
        }
        return f"{target.conversation_id}:{stable_digest(base, length=32)}"[:255], "medium", {"source": "content_visual_bucket", **base}
    base = {
        "conversation_id": target.conversation_id,
        "remark_code": target.remark_code,
        "raw_payload_hash": stable_digest(message, length=32),
        "index": index,
    }
    return f"{target.conversation_id}:{stable_digest(base, length=32)}"[:255], "low", {"source": "raw_payload_hash", **base}


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


def voice_transcription_entries(sidecar_payload: dict[str, Any]) -> list[dict[str, Any]]:
    transcription = sidecar_payload.get("voice_transcription")
    if not isinstance(transcription, dict):
        return []
    transcribed = transcription.get("transcribed_messages")
    if not isinstance(transcribed, list):
        return []
    entries: list[dict[str, Any]] = []
    occurrence_counts: dict[tuple[str, str, int | None], int] = {}
    for item in transcribed:
        if not isinstance(item, dict):
            continue
        content = clean_voice_transcribed_content(item)
        if not content:
            continue
        content_hash = normalized_content_hash(content)
        role = sender_role_group(item)
        duration = voice_duration_seconds(item)
        occurrence_key = (role, content_hash, duration)
        occurrence_index = occurrence_counts.get(occurrence_key, 0)
        occurrence_counts[occurrence_key] = occurrence_index + 1
        entries.append(
            {
                "state": "voice_transcribe_completed",
                "flow_state": transcription.get("state"),
                "attempt_count": transcription.get("attempt_count"),
                "quality_flags": transcription.get("quality_flags") if isinstance(transcription.get("quality_flags"), list) else [],
                "sidecar_run_id": transcription.get("sidecar_run_id"),
                "artifact_dir": transcription.get("artifact_dir"),
                "before_screenshot_path": transcription.get("before_screenshot_path"),
                "after_screenshot_path": transcription.get("after_screenshot_path"),
                "screenshot_path": transcription.get("screenshot_path"),
                "review_path": transcription.get("review_path"),
                "target_mode": transcription.get("target_mode"),
                "remark_code": transcription.get("remark_code"),
                "message": item,
                "content": content,
                "content_hash": content_hash,
                "sender_role": role,
                "voice_duration": duration,
                "occurrence_index": occurrence_index,
                "voice_anchor_id": voice_anchor_identity(item),
                "source_aliases": source_identity_aliases(item),
                "matched": False,
            },
        )
    return entries


def voice_transcription_index(sidecar_payload: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    indexed: dict[str, list[dict[str, Any]]] = {}
    for entry in voice_transcription_entries(sidecar_payload):
        indexed.setdefault(str(entry.get("content_hash") or ""), []).append(entry)
    return indexed


def matching_voice_transcription(
    entries: list[dict[str, Any]],
    message: dict[str, Any],
    content: str,
) -> dict[str, Any] | None:
    aliases = source_identity_aliases(message)
    if aliases:
        exact = [
            entry
            for entry in entries
            if not entry.get("matched") and aliases & set(entry.get("source_aliases") or set())
        ]
        if exact:
            return exact[0]
    if message_type(message) != "voice":
        return None
    role = sender_role_group(message)
    duration = voice_duration_seconds(message)
    content_hash = normalized_content_hash(content)
    candidates = [
        entry
        for entry in entries
        if not entry.get("matched")
        and entry.get("content_hash") == content_hash
    ]
    same_role = [entry for entry in candidates if entry.get("sender_role") in {role, "unknown"}]
    if same_role:
        candidates = same_role
    if duration is not None:
        same_duration = [entry for entry in candidates if entry.get("voice_duration") in {None, duration}]
        if same_duration:
            candidates = same_duration
    return candidates[0] if candidates else None


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
    observations = sidecar_payload.get("observations")
    if not isinstance(observations, list):
        raise ValueError("C2 V3 payload is missing observations")
    allowed_roles = contract_values("sender_roles")
    allowed_types = contract_values("message_types")
    ingestible_row_kinds = contract_values("ingestible_row_kinds")
    chat_message_row_kinds = contract_values("chat_message_row_kinds")
    chat_message_role_sources = contract_values("chat_message_role_sources")
    voice_summary = sidecar_payload.get("voice_transcription") if isinstance(sidecar_payload.get("voice_transcription"), dict) else {}
    flow_state = normalized_voice_flow_state(voice_summary.get("state")) if voice_summary else "completed"
    message_sidecar_id = sidecar_run_id(sidecar_payload, "messages")
    mapped: list[dict[str, Any]] = []
    source_keys: set[str] = set()
    slots: list[dict[str, Any]] = []
    voice_entries = voice_transcription_entries(sidecar_payload)
    authoritative_frame_source = str(sidecar_payload.get("authoritative_frame_source") or "").strip()
    if authoritative_frame_source not in {"initial_read", "final_read"}:
        authoritative_frame_source = "final_read" if voice_summary else "initial_read"

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
    ) -> None:
        if role not in allowed_roles or role == "unknown" or msg_type not in allowed_types:
            return
        if msg_type in {"text", "system", "voice"} and not str(content or "").strip():
            return
        normalized_source = {**source, "sender_role": role, "sender": role, "type": msg_type, "content": content}
        dedupe_key, confidence, basis = message_dedupe_metadata(
            target,
            normalized_source,
            identity_index,
            messages=identity_sources,
        )
        canonical_source_key = source_message_key(
            target,
            normalized_source,
            sidecar_id=message_sidecar_id,
            fallback_index=source_index,
        )
        if canonical_source_key in source_keys:
            return
        source_keys.add(canonical_source_key)
        raw_payload = {
            **normalized_source,
            "contract_version": 3,
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
        mapped.append(
            {
                "dedupe_key": dedupe_key,
                "source_message_key": canonical_source_key,
                "sender_role_hint": role,
                "message_type": msg_type,
                "content": str(content).strip() if content is not None else None,
                "image_local_path": normalized_source.get("image_local_path"),
                "occurred_at": normalized_source.get("occurred_at") or None,
                "ocr_confidence": normalized_source.get("ocr_confidence"),
                "item_state": "completed",
                "flow_state": flow_state if msg_type == "voice" else "completed",
                "message_position": message_position,
                "raw_payload": raw_payload,
            }
        )

    for index, observation in enumerate(observations):
        if not isinstance(observation, dict):
            continue
        row_kind = str(observation.get("row_kind") or "")
        voice_state = str(observation.get("voice_state") or "")
        source = observation.get("source_message") if isinstance(observation.get("source_message"), dict) else {}
        source = {**source, "observation": {key: value for key, value in observation.items() if key != "source_message"}}
        rect = message_rect({"bubble_rect": observation.get("bubble_rect") or source.get("bubble_rect")})
        candidate: dict[str, Any] | None = None
        if row_kind not in {"voice_bubble", "voice_transcript", "image_bubble"} and voice_state == "not_voice":
            role_source = str(observation.get("sender_role_source") or "")
            if row_kind in ingestible_row_kinds and not (
                row_kind in chat_message_row_kinds and role_source not in chat_message_role_sources
            ):
                candidate = {
                    "source": source,
                    "role": str(observation.get("sender_role") or "unknown"),
                    "msg_type": str(observation.get("message_type") or "unknown"),
                    "content": str(observation.get("content_clean") or "").strip() or None,
                    "source_index": index,
                    "voice_meta": None,
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
                "order_source": "visual_top" if rect else "observation_index_fallback",
            }
        )

    def match_voice_entry(slot: dict[str, Any]) -> dict[str, Any] | None:
        observation = slot["observation"]
        source = slot["source"]
        slot_aliases = source_identity_aliases(source)
        for key in ("voice_anchor_key", "parent_voice_anchor_key"):
            value = str(observation.get(key) or "").strip().lower()
            if value:
                slot_aliases.add(f"voice_anchor:{value}")
        exact = [
            entry
            for entry in voice_entries
            if not entry.get("matched") and slot_aliases & set(entry.get("source_aliases") or set())
        ]
        if exact:
            return exact[0]
        content_hash = normalized_content_hash(observation.get("content_clean") or "")
        role = str(observation.get("sender_role") or "unknown")
        duration = voice_duration_seconds(observation)
        semantic = [
            entry
            for entry in voice_entries
            if not entry.get("matched")
            and entry.get("content_hash") == content_hash
            and entry.get("sender_role") in {role, "unknown"}
            and (duration is None or entry.get("voice_duration") in {None, duration})
        ]
        if not semantic:
            return None
        slot_rect = slot.get("rect")
        if slot_rect:
            return min(
                semantic,
                key=lambda entry: abs(
                    float((message_rect(entry.get("message") or {}) or {}).get("center_y") or slot_rect["center_y"])
                    - float(slot_rect["center_y"])
                ),
            )
        return semantic[0]

    # Final-frame voice observations reserve their original visual slots. The
    # completed result keeps its stable anchor identity but adopts the final
    # frame geometry, so bottom-up UI actions cannot reorder the V3 messages.
    for slot in slots:
        if slot["row_kind"] != "voice_transcript" or slot["voice_state"] != "transcribed":
            continue
        entry = match_voice_entry(slot)
        if not entry:
            continue
        entry["matched"] = True
        entry_source = entry.get("message")
        content = str(entry.get("content") or "").strip()
        if not isinstance(entry_source, dict) or not content or not voice_anchor_identity(entry_source):
            continue
        final_rect = slot.get("rect")
        voice_source = {
            **entry_source,
            "type": "voice",
            "content": content,
            "voice_anchor_stable_key": voice_anchor_identity(entry_source),
            "_voice_occurrence_index": int(entry.get("occurrence_index") or 0),
            "observation": {key: value for key, value in slot["observation"].items() if key != "source_message"},
        }
        if final_rect:
            voice_source["bubble_rect"] = {
                key: int(final_rect[key]) for key in ("left", "top", "right", "bottom")
            }
        slot["candidate"] = {
            "source": voice_source,
            "role": str(entry.get("sender_role") or "unknown"),
            "msg_type": "voice",
            "content": content,
            "source_index": int(slot["authority_index"]),
            "voice_meta": voice_transcription_meta(voice_summary, message=entry_source),
        }

    # A completed, anchor-bound voice should not be lost merely because the
    # final OCR omitted its transcript observation. Its bound bubble geometry
    # is used only as an ordering fallback and never as message identity.
    for entry_index, entry in enumerate(voice_entries, start=len(observations)):
        if entry.get("matched"):
            continue
        source = entry.get("message")
        content = str(entry.get("content") or "").strip()
        if not isinstance(source, dict) or not content or not voice_anchor_identity(source):
            continue
        role = str(entry.get("sender_role") or "unknown")
        voice_source = {
            **source,
            "type": "voice",
            "content": content,
            "voice_anchor_stable_key": voice_anchor_identity(source),
            "_voice_occurrence_index": int(entry.get("occurrence_index") or 0),
        }
        rect = message_rect(voice_source)
        slots.append(
            {
                "authority_index": entry_index,
                "rect": rect,
                "row_kind": "voice_transcript",
                "voice_state": "transcribed",
                "observation": {},
                "source": voice_source,
                "order_source": "voice_anchor_geometry_fallback" if rect else "observation_index_fallback",
                "candidate": {
                    "source": voice_source,
                    "role": role,
                    "msg_type": "voice",
                    "content": content,
                    "source_index": entry_index,
                    "voice_meta": voice_transcription_meta(voice_summary, message=source),
                },
            }
        )

    if slots and all(slot.get("rect") for slot in slots):
        ordered_slots = sorted(
            slots,
            key=lambda slot: (
                float(slot["rect"]["top"]),
                float(slot["rect"]["bottom"]),
                int(slot["authority_index"]),
            ),
        )
    else:
        # Observations are already emitted top-to-bottom. If any slot has no
        # usable geometry, keeping that authoritative sequence is safer than
        # pushing the unknown slot to either end.
        ordered_slots = sorted(slots, key=lambda slot: int(slot["authority_index"]))

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
        }
        if rect:
            message_position.update(
                {
                    "visual_top": int(rect["top"]),
                    "visual_bottom": int(rect["bottom"]),
                }
            )
        if slot.get("order_source") != "visual_top":
            message_position["order_source"] = slot.get("order_source")
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
        )
        identity_index += 1

    finished_at = now_iso()
    return {
        "contract_version": 3,
        "read_run_id": f"read-{uuid.uuid4()}",
        "sidecar_run_id": message_sidecar_id,
        "conversation_id": target.conversation_id,
        "remark_code": target.remark_code,
        "rpa_session_key": target.rpa_session_key,
        "authorization_revision": target.authorization_revision,
        "messages": mapped,
        "evidence": {
            "contract_version": 3,
            "observation_schema_version": 3,
            "sidecar_run_id": message_sidecar_id,
            "artifact_dir": sidecar_payload.get("artifact_dir"),
            "review_path": sidecar_payload.get("review_path"),
            "screenshot": sidecar_payload.get("screenshot_path"),
            "adapter": sidecar_payload.get("adapter"),
            "state": sidecar_payload.get("state"),
            "remark_code": target.remark_code,
            "read_reason": target.read_reason,
            "finished_at": finished_at,
            "voice_transcription": voice_transcription_meta(voice_summary) if voice_summary else None,
        },
    }


def build_message_ingest_payload(target: WechatReadTarget, sidecar_payload: dict[str, Any]) -> dict[str, Any]:
    if int(sidecar_payload.get("observation_schema_version") or 0) == 3:
        return _build_message_ingest_payload_v3(target, sidecar_payload)
    messages = sidecar_payload.get("messages") if isinstance(sidecar_payload.get("messages"), list) else []
    voice_transcription_summary = sidecar_payload.get("voice_transcription") if isinstance(sidecar_payload.get("voice_transcription"), dict) else {}
    voice_entries = voice_transcription_entries(sidecar_payload)
    mapped: list[dict[str, Any]] = []
    mapped_source_indexes: dict[str, int] = {}
    message_sidecar_id = sidecar_run_id(sidecar_payload, "messages")
    flow_state = normalized_voice_flow_state(voice_transcription_summary.get("state")) if voice_transcription_summary else "completed"

    def append_canonical(item: dict[str, Any]) -> None:
        key = str(item.get("source_message_key") or "")
        existing_index = mapped_source_indexes.get(key)
        if existing_index is None:
            mapped_source_indexes[key] = len(mapped)
            mapped.append(item)
            return
        existing = mapped[existing_index]
        if existing.get("message_type") != "voice" and item.get("message_type") == "voice":
            mapped[existing_index] = item
    for index, item in enumerate(messages):
        if not isinstance(item, dict):
            continue
        content = str(item.get("content") or "").strip()
        image_local_path = str(item.get("image_local_path") or "").strip() or None
        msg_type = message_type(item)
        voice_prefix_removed = False
        quality_flags = item.get("quality_flags") if isinstance(item.get("quality_flags"), list) else []
        if "untranscribed_voice_placeholder" in quality_flags:
            continue
        raw_ocr_voice_like = raw_ocr_looks_like_voice_transcript(item)
        if raw_ocr_voice_like:
            cleaned_content, voice_prefix_removed = strip_voice_ocr_duration_prefix(content)
            if not cleaned_content:
                continue
            content = cleaned_content
            msg_type = "voice"
        payload_item = item
        # A plain text bubble is never promoted to voice only because its text
        # happens to equal a transcript. The sidecar must first provide voice
        # structure (duration/icon + transcript layout), then the anchor binds it.
        matched_voice_transcription = matching_voice_transcription(voice_entries, item, content) if content else None
        if matched_voice_transcription and msg_type not in {"voice", "text"}:
            matched_voice_transcription = None
        if matched_voice_transcription:
            matched_voice_transcription["matched"] = True
            matched_message = matched_voice_transcription.get("message")
            if isinstance(matched_message, dict):
                content = str(matched_voice_transcription.get("content") or content).strip()
                msg_type = "voice"
                role = sender_role_hint(matched_message)
                if role:
                    payload_item = {
                        **item,
                        **{
                            key: matched_message.get(key)
                            for key in ("voice_anchor", "voice_anchor_key", "voice_anchor_stable_key")
                            if matched_message.get(key)
                        },
                        "sender_role": role,
                        "sender": role,
                    }
                    envelope = payload_item.get("message_envelope")
                    if isinstance(envelope, dict):
                        payload_item["message_envelope"] = {
                            **envelope,
                            "sender_role": role,
                            "sender": role,
                        }
        if raw_ocr_voice_like and not matched_voice_transcription:
            continue
        if content_looks_like_untranscribed_voice_placeholder(content) and not matched_voice_transcription:
            continue
        if msg_type == "voice" and not matched_voice_transcription and content_looks_like_untranscribed_voice_placeholder(content):
            continue
        if not content and not image_local_path and msg_type in {"text", "system"}:
            continue
        dedupe_key, dedupe_confidence, dedupe_basis = message_dedupe_metadata(
            target,
            payload_item,
            index,
            messages=messages,
        )
        raw_payload = {**payload_item, "dedupe_confidence": dedupe_confidence, "dedupe_basis": dedupe_basis}
        canonical_source_key = source_message_key(
            target,
            payload_item,
            sidecar_id=message_sidecar_id,
            fallback_index=index,
        )
        raw_payload["source_message_key"] = canonical_source_key
        if voice_prefix_removed:
            raw_payload["voice_duration_prefix_removed"] = True
        if matched_voice_transcription:
            raw_payload["voice_transcription"] = content
            raw_payload["voice_transcription_meta"] = voice_transcription_meta(
                voice_transcription_summary,
                message=matched_voice_transcription.get("message") if isinstance(matched_voice_transcription.get("message"), dict) else None,
            )
        append_canonical(
            {
                "dedupe_key": dedupe_key,
                "source_message_key": canonical_source_key,
                "sender_role_hint": sender_role_hint(payload_item),
                "message_type": msg_type,
                "content": content or None,
                "image_local_path": image_local_path,
                "occurred_at": payload_item.get("occurred_at") or None,
                "ocr_confidence": payload_item.get("ocr_confidence"),
                "item_state": "completed",
                "flow_state": flow_state if msg_type == "voice" else "completed",
                "raw_payload": raw_payload,
            }
        )
    if str(voice_transcription_summary.get("state") or "") in {"voice_transcribe_completed", "voice_transcribe_partial"}:
        for entry in voice_entries:
            if entry.get("matched"):
                continue
            item = entry.get("message")
            content = str(entry.get("content") or "").strip()
            if not isinstance(item, dict) or not content:
                continue
            voice_item = {
                **item,
                "type": "voice",
                "content": content,
                "_voice_occurrence_index": int(entry.get("occurrence_index") or 0),
            }
            dedupe_key, dedupe_confidence, dedupe_basis = message_dedupe_metadata(target, voice_item, len(mapped))
            canonical_source_key = source_message_key(
                target,
                voice_item,
                sidecar_id=str(voice_transcription_summary.get("sidecar_run_id") or message_sidecar_id),
                fallback_index=len(mapped),
            )
            append_canonical(
                {
                    "dedupe_key": dedupe_key,
                    "source_message_key": canonical_source_key,
                    "sender_role_hint": sender_role_hint(voice_item),
                    "message_type": "voice",
                    "content": content,
                    "image_local_path": None,
                    "occurred_at": voice_item.get("occurred_at") or None,
                    "ocr_confidence": voice_item.get("ocr_confidence"),
                    "item_state": "completed",
                    "flow_state": flow_state,
                    "raw_payload": {
                        **voice_item,
                        "source_message_key": canonical_source_key,
                        "voice_transcription": content,
                        "voice_transcription_meta": voice_transcription_meta(voice_transcription_summary, message=item),
                        "dedupe_confidence": dedupe_confidence,
                        "dedupe_basis": dedupe_basis,
                    },
                }
            )
    finished_at = now_iso()
    payload = {
        "contract_version": 2,
        "read_run_id": f"read-{uuid.uuid4()}",
        "sidecar_run_id": sidecar_run_id(sidecar_payload, "messages"),
        "conversation_id": target.conversation_id,
        "remark_code": target.remark_code,
        "rpa_session_key": target.rpa_session_key,
        "messages": mapped,
        "evidence": {
            "sidecar_run_id": sidecar_run_id(sidecar_payload, "messages"),
            "artifact_dir": sidecar_payload.get("artifact_dir"),
            "review_path": sidecar_payload.get("review_path"),
            "screenshot": sidecar_payload.get("screenshot_path"),
            "adapter": sidecar_payload.get("adapter"),
            "state": sidecar_payload.get("state"),
            "ocr_items_count": sidecar_payload.get("ocr_items_count"),
            "remark_code": target.remark_code,
            "target_display_name": target.display_name,
            "target_row_fingerprint": target.row_fingerprint,
            "read_reason": target.read_reason,
            "finished_at": finished_at,
            "voice_transcription": {
                "state": voice_transcription_summary.get("state"),
                "sidecar_run_id": voice_transcription_summary.get("sidecar_run_id"),
                "attempt_count": voice_transcription_summary.get("attempt_count"),
                "quality_flags": voice_transcription_summary.get("quality_flags"),
                "artifact_dir": voice_transcription_summary.get("artifact_dir"),
                "before_screenshot_path": voice_transcription_summary.get("before_screenshot_path"),
                "after_screenshot_path": voice_transcription_summary.get("after_screenshot_path"),
                "screenshot_path": voice_transcription_summary.get("screenshot_path"),
                "review_path": voice_transcription_summary.get("review_path"),
            } if voice_transcription_summary else None,
        },
    }
    save_c2_state(
        "last_message_read",
        {
            "read_run_id": payload["read_run_id"],
            "sidecar_run_id": payload["sidecar_run_id"],
            "conversation_id": target.conversation_id,
            "rpa_session_key": target.rpa_session_key,
            "remark_code": target.remark_code,
            "message_count": len(mapped),
            "finished_at": finished_at,
        },
    )
    return payload
