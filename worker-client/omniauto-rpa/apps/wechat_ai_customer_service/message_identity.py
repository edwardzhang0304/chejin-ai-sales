"""Canonical message identity helpers for OCR/RPA customer-service flows.

This module belongs to the code mechanism layer.  It provides stable message
identity fields for scheduling, ledgers and send-safety checks; it must never
author customer-visible reply text.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any


OCR_RPA_ADAPTERS = {"win32_ocr", "wechat_win32_ocr", "rpa_ocr", "ocr_rpa"}
OCR_LIKE_ID_PREFIXES = ("win32_ocr:", "ocr:", "screen_ocr:", "uia_ocr:", "wechat_win32_ocr:")
STRONG_OCCURRENCE_KEYS = (
    "pending_signal_id",
    "pending_since",
    "last_detected_at",
    "last_message_time",
    "screen_time_text",
)
REPEATABLE_OCCURRENCE_KEYS = (
    *STRONG_OCCURRENCE_KEYS,
    "message_time",
    "time",
    "captured_at",
    "created_at",
)


def stable_hash(*parts: Any, length: int = 20) -> str:
    seed = json.dumps([_json_safe_part(item) for item in parts], ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[: max(8, int(length or 20))]


def stable_id(prefix: str, *parts: Any, length: int = 20) -> str:
    return f"{prefix}_" + stable_hash(*parts, length=length)


def _json_safe_part(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_safe_part(value[key]) for key in sorted(value)}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe_part(item) for item in value]
    return str(value)


def existing_envelope(message: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(message, dict):
        return {}
    envelope = message.get("message_envelope")
    return envelope if isinstance(envelope, dict) else {}


def envelope_or_message_value(message: dict[str, Any], key: str, *fallback_keys: str) -> str:
    envelope = existing_envelope(message)
    for source in (message, envelope):
        for item_key in (key, *fallback_keys):
            value = str(source.get(item_key) or "").strip()
            if value:
                return value
    return ""


def normalize_identity_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def normalize_repeatable_probe_text(text: Any) -> str:
    compact = re.sub(r"[\s，。,.！？!、~～：:；;“”\"'（）()]+", "", str(text or "")).lower()
    return compact.strip()


def message_has_repeatable_probe_content(message: dict[str, Any]) -> bool:
    compact = normalize_repeatable_probe_text(
        message.get("content")
        or message.get("content_body")
        or message.get("text")
        or existing_envelope(message).get("content_body")
    )
    if not compact:
        return False
    if len(compact) <= 7:
        return True
    if compact in {"你好", "您好", "在吗", "有人吗", "老板在吗", "hello", "hi", "哈喽", "嗨", "在", "在不", "在么", "在嘛", "在呢"}:
        return True
    return compact.startswith("在") and len(compact) <= 3


def normalize_rect(rect: Any) -> dict[str, int]:
    if not isinstance(rect, dict):
        return {}
    result: dict[str, int] = {}
    for key in ("left", "top", "right", "bottom"):
        try:
            result[key] = int(round(float(rect.get(key) or 0)))
        except (TypeError, ValueError, OSError):
            result[key] = 0
    return {key: value for key, value in result.items() if value}


def source_adapter(message: dict[str, Any]) -> str:
    return envelope_or_message_value(message, "source_adapter").lower()


def is_ocr_rpa_message(message: dict[str, Any]) -> bool:
    adapter = source_adapter(message)
    if adapter in OCR_RPA_ADAPTERS:
        return True
    raw_id = envelope_or_message_value(message, "id", "message_id").lower()
    return raw_id.startswith(OCR_LIKE_ID_PREFIXES)


def raw_message_id(message: dict[str, Any]) -> str:
    return envelope_or_message_value(message, "id", "message_id")


def non_ocr_raw_message_id(message: dict[str, Any]) -> str:
    raw_id = raw_message_id(message)
    if not raw_id:
        return ""
    if is_ocr_rpa_message(message):
        return ""
    if raw_id.lower().startswith(OCR_LIKE_ID_PREFIXES):
        return ""
    return raw_id


def canonical_visual_message_id(
    message: dict[str, Any] | None,
    *,
    target_name: Any = "",
    conversation_type: Any = "",
) -> str:
    if not isinstance(message, dict):
        return ""
    explicit = envelope_or_message_value(message, "canonical_visual_id")
    if explicit:
        return explicit
    legacy_non_ocr_id = non_ocr_raw_message_id(message)
    if legacy_non_ocr_id:
        return legacy_non_ocr_id
    envelope = existing_envelope(message)
    content = normalize_identity_text(
        message.get("content_body")
        or message.get("content")
        or message.get("text")
        or envelope.get("content_body")
        or envelope.get("content_raw_ocr")
    )
    rect = normalize_rect(message.get("bubble_rect") or envelope.get("bubble_rect"))
    target = str(
        target_name
        or message.get("target_name")
        or envelope.get("target_name")
        or ""
    ).strip()
    ctype = str(
        conversation_type
        or message.get("conversation_type")
        or envelope.get("conversation_type")
        or ""
    ).strip()
    seed = {
        "source_adapter": source_adapter(message),
        "conversation_id": envelope_or_message_value(message, "conversation_id"),
        "target_name": target,
        "conversation_type": ctype,
        "sender": envelope_or_message_value(message, "sender", "sender_role"),
        "sender_role": envelope_or_message_value(message, "sender_role"),
        "speaker_name": envelope_or_message_value(message, "speaker_name", "group_member_name"),
        "message_id": raw_message_id(message),
        "bubble_id": envelope_or_message_value(message, "bubble_id"),
        "content": content,
        "rect": rect,
    }
    if not any(str(value or "").strip() for value in seed.values() if not isinstance(value, dict)) and not rect:
        return ""
    return stable_id("canonical_visual", seed, length=24)


def occurrence_marker_for_message(message: dict[str, Any], *, allow_repeatable_fallback: bool = True) -> str:
    keys = REPEATABLE_OCCURRENCE_KEYS if allow_repeatable_fallback and message_has_repeatable_probe_content(message) else STRONG_OCCURRENCE_KEYS
    for key in keys:
        value = envelope_or_message_value(message, key)
        if value:
            return f"{key}:{value}"
    return ""


def canonical_input_message_id(
    message: dict[str, Any] | None,
    *,
    target_name: Any = "",
    conversation_type: Any = "",
    force_recompute: bool = False,
) -> str:
    if not isinstance(message, dict):
        return ""
    explicit = envelope_or_message_value(message, "canonical_input_id")
    if explicit and not force_recompute:
        return explicit
    legacy_non_ocr_id = non_ocr_raw_message_id(message)
    if legacy_non_ocr_id:
        return legacy_non_ocr_id
    visual_id = canonical_visual_message_id(message, target_name=target_name, conversation_type=conversation_type)
    marker = occurrence_marker_for_message(message)
    if visual_id and marker:
        return stable_id("canonical_input", visual_id, marker, length=24)
    if visual_id:
        return visual_id
    raw_id = raw_message_id(message)
    if raw_id:
        return raw_id
    content = normalize_identity_text(
        message.get("content")
        or message.get("content_body")
        or message.get("text")
        or existing_envelope(message).get("content_body")
    )
    if not content:
        return ""
    return stable_id(
        "canonical_input",
        envelope_or_message_value(message, "sender", "sender_role"),
        envelope_or_message_value(message, "type"),
        content,
        occurrence_marker_for_message(message, allow_repeatable_fallback=True),
        length=24,
    )


def apply_canonical_identity_fields(
    message: dict[str, Any],
    *,
    target_name: Any = "",
    conversation_type: Any = "",
    force_recompute: bool = False,
) -> dict[str, Any]:
    next_message = dict(message or {})
    visual_id = canonical_visual_message_id(next_message, target_name=target_name, conversation_type=conversation_type)
    if visual_id:
        next_message["canonical_visual_id"] = visual_id
    input_id = canonical_input_message_id(
        next_message,
        target_name=target_name,
        conversation_type=conversation_type,
        force_recompute=force_recompute,
    )
    if input_id:
        next_message["canonical_input_id"] = input_id
    envelope = existing_envelope(next_message)
    if envelope:
        envelope = dict(envelope)
        if visual_id:
            envelope["canonical_visual_id"] = visual_id
        if input_id:
            envelope["canonical_input_id"] = input_id
        next_message["message_envelope"] = envelope
    return next_message
