"""Shared WeChat message envelope contract for OCR/RPA captures.

The envelope keeps the legacy ``content`` surface body-only while preserving
OCR evidence and excluded fragments for recorder export, customer service and
learning guards.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime
from typing import Any

from apps.wechat_ai_customer_service.wechat_message_normalizer import (
    normalize_text_for_speaker_check,
    split_wechat_ocr_speaker_prefix,
)
from apps.wechat_ai_customer_service.message_identity import (
    canonical_input_message_id,
    canonical_visual_message_id,
)


OCR_RPA_ADAPTERS = {"win32_ocr", "wechat_win32_ocr", "rpa_ocr", "ocr_rpa"}
QUOTE_MARKER_RE = re.compile(r"\[(?:引用|回复)[^\]]*\]")
LONG_PRESS_OVERLAY_RE = re.compile(r"^(?:复制|转发|收藏|删除|多选|引用|提醒|翻译|搜一搜)(?:\s+|$)")
SCREEN_TIME_RE = re.compile(r"^(?:星期[一二三四五六日天]|昨天|今天|前天)?\s*\d{1,2}:\d{2}(?::\d{2})?$")
HIGH_RISK_CAPTURE_FLAGS = {
    "bubble_boundary_ambiguous",
    "multi_bubble_possible_merge",
    "quote_contamination",
    "long_press_overlay_detected",
    "sender_name_in_content",
}
LEARNING_BLOCKING_QUALITY_FLAGS = HIGH_RISK_CAPTURE_FLAGS | {
    "quote_preview_removed",
    "ocr_low_confidence",
    "visual_ocr_non_text",
}
VISUAL_OCR_BLOCKING_QUALITY_FLAGS = {
    "visual_ocr_non_text",
    "visual_media_ocr",
    "image_ocr_text",
    "media_ocr_text",
    "non_text_visual_ocr",
    "ocr_from_image",
    "ocr_visual_only",
}
NON_TEXT_MESSAGE_TYPES = {
    "image",
    "picture",
    "photo",
    "video",
    "audio",
    "voice",
    "file",
    "attachment",
    "sticker",
    "emoji",
    "card",
    "link_card",
    "mini_program",
    "location",
    "contact_card",
    "media",
}
VISUAL_OCR_SOURCE_VALUES = {
    "image_ocr",
    "photo_ocr",
    "picture_ocr",
    "media_ocr",
    "visual_ocr",
    "visual_media_ocr",
    "screenshot_ocr",
    "poster_ocr",
    "thumbnail_ocr",
    "attachment_ocr",
    "card_ocr",
}
VISUAL_OCR_SOURCE_KEYS = (
    "source_type",
    "source_kind",
    "content_source",
    "ocr_source",
    "capture_source",
    "payload_type",
    "message_source",
    "semantic_source",
    "surface_type",
)
VISUAL_OCR_NESTED_SOURCE_KEYS = (
    "source",
    "metadata",
    "capture_metadata",
    "message_metadata",
    "ocr_metadata",
    "classification",
    "source_payload",
)


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def stable_digest(value: Any, length: int = 16) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:length]


def visual_ocr_noise_reason(record: dict[str, Any]) -> str:
    """Return why a record is non-text visual OCR, or an empty string.

    The rule is source/metadata based. It must not depend on product words such
    as a specific brand, model, SKU, or industry term.
    """

    if not isinstance(record, dict):
        return ""
    flags = message_quality_flag_set(record)
    if flags & VISUAL_OCR_BLOCKING_QUALITY_FLAGS:
        return "visual_ocr_non_text"
    for payload in iter_message_payload_layers(record):
        for key in ("type", "content_type", "message_type", "media_type"):
            value = normalize_marker_value(payload.get(key))
            if value in NON_TEXT_MESSAGE_TYPES:
                return "non_text_message"
        for key in VISUAL_OCR_SOURCE_KEYS:
            value = normalize_marker_value(payload.get(key))
            if value in VISUAL_OCR_SOURCE_VALUES:
                return "visual_ocr_non_text"
        nested_reason = nested_visual_ocr_source_reason(payload)
        if nested_reason:
            return nested_reason
        ocr_items_reason = visual_ocr_items_noise_reason(payload.get("ocr_items"))
        if ocr_items_reason:
            return ocr_items_reason
        if bool(payload.get("visual_ocr_non_text") or payload.get("visual_media_ocr") or payload.get("media_ocr")):
            return "visual_ocr_non_text"
    return ""


def nested_visual_ocr_source_reason(payload: dict[str, Any], *, _depth: int = 0) -> str:
    if _depth > 2 or not isinstance(payload, dict):
        return ""
    for container_key in VISUAL_OCR_NESTED_SOURCE_KEYS:
        nested = payload.get(container_key)
        if not isinstance(nested, dict):
            continue
        for key in VISUAL_OCR_SOURCE_KEYS:
            if normalize_marker_value(nested.get(key)) in VISUAL_OCR_SOURCE_VALUES:
                return "visual_ocr_non_text"
        flags = set(normalize_flags(nested.get("quality_flags")))
        if flags & VISUAL_OCR_BLOCKING_QUALITY_FLAGS:
            return "visual_ocr_non_text"
        if bool(nested.get("visual_ocr_non_text") or nested.get("visual_media_ocr") or nested.get("media_ocr")):
            return "visual_ocr_non_text"
        reason = nested_visual_ocr_source_reason(nested, _depth=_depth + 1)
        if reason:
            return reason
    return ""


def visual_ocr_items_noise_reason(items: Any) -> str:
    if not isinstance(items, list):
        return ""
    text_items = [
        item
        for item in items
        if isinstance(item, dict) and str(item.get("text") or item.get("content") or "").strip()
    ]
    if not text_items:
        return ""
    marked = [item for item in text_items if ocr_item_has_visual_source_marker(item)]
    if marked and len(marked) == len(text_items):
        return "visual_ocr_non_text"
    return ""


def ocr_item_has_visual_source_marker(item: dict[str, Any]) -> bool:
    flags = set(normalize_flags(item.get("quality_flags")))
    if flags & VISUAL_OCR_BLOCKING_QUALITY_FLAGS:
        return True
    for key in VISUAL_OCR_SOURCE_KEYS:
        if normalize_marker_value(item.get(key)) in VISUAL_OCR_SOURCE_VALUES:
            return True
    return bool(item.get("visual_ocr_non_text") or item.get("visual_media_ocr") or item.get("media_ocr"))


def message_is_visual_or_media_ocr_noise(record: dict[str, Any]) -> bool:
    return bool(visual_ocr_noise_reason(record))


def message_quality_flag_set(record: dict[str, Any]) -> set[str]:
    flags = set(normalize_flags(record.get("quality_flags")))
    envelope = existing_message_envelope(record)
    flags.update(normalize_flags(envelope.get("quality_flags")))
    raw_payload = record.get("raw_payload") if isinstance(record.get("raw_payload"), dict) else {}
    flags.update(normalize_flags(raw_payload.get("quality_flags")))
    raw_envelope = raw_payload.get("message_envelope") if isinstance(raw_payload.get("message_envelope"), dict) else {}
    flags.update(normalize_flags(raw_envelope.get("quality_flags")))
    return flags


def iter_message_payload_layers(record: dict[str, Any]) -> list[dict[str, Any]]:
    layers: list[dict[str, Any]] = []
    for payload in (record, existing_message_envelope(record)):
        if isinstance(payload, dict) and payload:
            layers.append(payload)
    raw_payload = record.get("raw_payload") if isinstance(record.get("raw_payload"), dict) else {}
    if raw_payload:
        layers.append(raw_payload)
        original = raw_payload.get("_original_raw_payload") if isinstance(raw_payload.get("_original_raw_payload"), dict) else {}
        if original:
            layers.append(original)
        raw_envelope = raw_payload.get("message_envelope") if isinstance(raw_payload.get("message_envelope"), dict) else {}
        if raw_envelope:
            layers.append(raw_envelope)
    deduped: list[dict[str, Any]] = []
    seen: set[int] = set()
    for payload in layers:
        marker = id(payload)
        if marker in seen:
            continue
        seen.add(marker)
        deduped.append(payload)
    return deduped


def normalize_marker_value(value: Any) -> str:
    return re.sub(r"[^a-z0-9_]+", "_", str(value or "").strip().lower()).strip("_")


def build_message_envelope(
    record: dict[str, Any],
    *,
    source_adapter: str = "",
    conversation: dict[str, Any] | None = None,
    captured_at: str | None = None,
    ocr_items: list[dict[str, Any]] | None = None,
    bubble_rect: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a normalized envelope while preserving caller compatibility."""

    if not isinstance(record, dict):
        record = {}
    conversation = conversation if isinstance(conversation, dict) else {}
    existing = existing_message_envelope(record)
    source_adapter = str(source_adapter or record.get("source_adapter") or existing.get("source_adapter") or "wxauto4")
    target_name = str(
        conversation.get("target_name")
        or conversation.get("display_name")
        or record.get("target_name")
        or existing.get("target_name")
        or ""
    )
    conversation_type = str(
        conversation.get("conversation_type")
        or conversation.get("type")
        or record.get("conversation_type")
        or existing.get("conversation_type")
        or "unknown"
    )
    conversation_id = str(conversation.get("conversation_id") or record.get("conversation_id") or existing.get("conversation_id") or "")
    raw_ocr_text = str(
        record.get("content_raw_ocr")
        or record.get("raw_ocr_text")
        or existing.get("content_raw_ocr")
        or record.get("original_content")
        or record.get("content")
        or record.get("text")
        or ""
    )
    body_source = str(
        record.get("content_body")
        or record.get("content_clean")
        or existing.get("content_body")
        or record.get("content")
        or record.get("text")
        or ""
    ).strip()
    original_content = str(record.get("original_content") or existing.get("original_content") or raw_ocr_text or body_source)
    speaker_name = str(record.get("speaker_name") or record.get("group_member_name") or existing.get("speaker_name") or "").strip()
    excluded_fragments = list_fragments(record.get("excluded_fragments") or existing.get("excluded_fragments"))
    quoted_fragments = list_fragments(record.get("quoted_fragments") or existing.get("quoted_fragments"))
    quality_flags = normalize_flags(record.get("quality_flags") or existing.get("quality_flags"))
    visual_reason = visual_ocr_noise_reason(record)
    if visual_reason == "visual_ocr_non_text":
        quality_flags.append("visual_ocr_non_text")

    if body_source and source_adapter in OCR_RPA_ADAPTERS:
        split = split_wechat_ocr_speaker_prefix(
            body_source,
            conversation_type=conversation_type,
            target_name=target_name,
            known_speakers=known_speaker_names(conversation, record),
            allow_unlisted_name_like_prefix=conversation_type == "group",
        )
        if split.get("changed"):
            prefix = str(split.get("speaker_name") or "").strip()
            cleaned = str(split.get("content") or "").strip()
            if prefix and cleaned:
                speaker_name = speaker_name or prefix
                excluded_fragments.append(
                    {
                        "text": prefix,
                        "reason": "speaker_name_removed",
                        "source": "ocr_speaker_prefix",
                    }
                )
                quality_flags.append("speaker_prefix_split_from_ocr_text")
                body_source = cleaned

    body_source, quote_meta = remove_quote_fragments(body_source)
    if quote_meta:
        quoted_fragments.extend(quote_meta)
        excluded_fragments.extend({"text": item["text"], "reason": "quote_preview_removed"} for item in quote_meta)
        quality_flags.extend(["quote_preview_removed", "quote_contamination"])

    body_source, excluded_meta, extra_flags, screen_time_text = remove_non_body_fragments(body_source)
    excluded_fragments.extend(excluded_meta)
    quality_flags.extend(extra_flags)

    confidence = coerce_float(record.get("ocr_confidence", existing.get("ocr_confidence")))
    if confidence and confidence < 0.75:
        quality_flags.append("ocr_low_confidence")
    if screen_time_text:
        quality_flags.append("screen_time_detected_but_ignored")
    clean_body = normalize_body_text(body_source)
    if speaker_name and speaker_name_in_content(speaker_name, clean_body):
        quality_flags.append("sender_name_in_content")

    rect = normalize_rect(bubble_rect or record.get("bubble_rect") or existing.get("bubble_rect"))
    normalized_ocr_items = summarize_ocr_items(ocr_items or record.get("ocr_items") or existing.get("ocr_items") or [])
    capture_time = str(
        captured_at
        or record.get("captured_at")
        or existing.get("captured_at")
        or record.get("observed_at")
        or ""
    ).strip() or now_iso()
    message_id = str(record.get("id") or record.get("message_id") or existing.get("message_id") or "").strip()
    bubble_id = str(record.get("bubble_id") or existing.get("bubble_id") or "").strip()
    if not bubble_id:
        bubble_id = "bubble_" + stable_digest(
            {
                "target_name": target_name,
                "sender": record.get("sender") or record.get("sender_role") or "",
                "content": clean_body,
                "rect": rect,
                "message_id": message_id,
            },
            20,
        )
    if not message_id:
        message_id = str(record.get("id") or "") or f"{source_adapter}:{stable_digest(f'{target_name}|{bubble_id}|{clean_body}', 20)}"
    sender_role = str(record.get("sender_role") or existing.get("sender_role") or "").strip().lower()
    sender = str(record.get("sender") or existing.get("sender") or "").strip()
    if not sender_role:
        sender_role = "self" if sender == "self" or record.get("is_self") else "unknown"
    if conversation_type == "group" and speaker_name and sender_role == "unknown":
        sender_role = "group_member"

    identity_record = {
        **dict(record),
        "id": message_id,
        "message_id": message_id,
        "bubble_id": bubble_id,
        "target_name": target_name,
        "conversation_type": conversation_type,
        "conversation_id": conversation_id,
        "sender": sender or ("self" if sender_role == "self" else "unknown"),
        "sender_role": sender_role,
        "speaker_name": speaker_name,
        "group_member_name": str(record.get("group_member_name") or speaker_name),
        "content": clean_body,
        "content_body": clean_body,
        "bubble_rect": rect,
        "source_adapter": source_adapter,
        "captured_at": capture_time,
        "time": record.get("time") or "",
        "message_time": record.get("message_time") or "",
        "pending_signal_id": record.get("pending_signal_id") or "",
        "pending_since": record.get("pending_since") or "",
        "last_detected_at": record.get("last_detected_at") or "",
        "last_message_time": record.get("last_message_time") or "",
        "screen_time_text": str(record.get("screen_time_text") or existing.get("screen_time_text") or screen_time_text or ""),
    }
    canonical_visual_id = canonical_visual_message_id(
        identity_record,
        target_name=target_name,
        conversation_type=conversation_type,
    )
    canonical_input_id = canonical_input_message_id(
        identity_record,
        target_name=target_name,
        conversation_type=conversation_type,
    )

    envelope = {
        "schema_version": 1,
        "message_id": message_id,
        "canonical_visual_id": canonical_visual_id,
        "canonical_input_id": canonical_input_id,
        "bubble_id": bubble_id,
        "conversation_id": conversation_id,
        "target_name": target_name,
        "conversation_type": conversation_type,
        "sender_role": sender_role,
        "sender": sender or ("self" if sender_role == "self" else "unknown"),
        "speaker_name": speaker_name,
        "group_member_name": str(record.get("group_member_name") or speaker_name),
        "content_body": clean_body,
        "content_raw_ocr": raw_ocr_text or original_content or clean_body,
        "original_content": original_content,
        "quoted_fragments": dedupe_fragments(quoted_fragments),
        "excluded_fragments": dedupe_fragments(excluded_fragments),
        "bubble_rect": rect,
        "ocr_items": normalized_ocr_items,
        "ocr_confidence": confidence,
        "quality_flags": sorted(set(flag for flag in normalize_flags(quality_flags) if flag)),
        "captured_at": capture_time,
        "screen_time_text": str(record.get("screen_time_text") or existing.get("screen_time_text") or screen_time_text or ""),
        "source_adapter": source_adapter,
        "source_payload": record.get("source_payload") if isinstance(record.get("source_payload"), dict) else {},
    }
    return envelope


def apply_message_envelope_to_record(record: dict[str, Any], envelope: dict[str, Any]) -> dict[str, Any]:
    """Return a legacy-compatible record enriched with envelope fields."""

    next_record = dict(record or {})
    content_body = str(envelope.get("content_body") or "").strip()
    next_record["id"] = str(next_record.get("id") or envelope.get("message_id") or "")
    next_record["message_id"] = str(next_record.get("message_id") or envelope.get("message_id") or "")
    next_record["canonical_visual_id"] = str(envelope.get("canonical_visual_id") or next_record.get("canonical_visual_id") or "")
    next_record["canonical_input_id"] = str(envelope.get("canonical_input_id") or next_record.get("canonical_input_id") or "")
    next_record["bubble_id"] = str(envelope.get("bubble_id") or "")
    next_record["content"] = content_body
    next_record["content_body"] = content_body
    next_record["content_clean"] = content_body
    if "text" in next_record:
        next_record["text"] = content_body
    next_record["content_raw_ocr"] = str(envelope.get("content_raw_ocr") or "")
    next_record["original_content"] = str(envelope.get("original_content") or next_record.get("original_content") or "")
    next_record["quoted_fragments"] = list(envelope.get("quoted_fragments") or [])
    next_record["excluded_fragments"] = list(envelope.get("excluded_fragments") or [])
    next_record["quality_flags"] = list(envelope.get("quality_flags") or [])
    next_record["captured_at"] = str(envelope.get("captured_at") or "")
    next_record["screen_time_text"] = str(envelope.get("screen_time_text") or "")
    next_record["bubble_rect"] = envelope.get("bubble_rect") if isinstance(envelope.get("bubble_rect"), dict) else {}
    next_record["ocr_items"] = envelope.get("ocr_items") if isinstance(envelope.get("ocr_items"), list) else []
    next_record["source_adapter"] = str(envelope.get("source_adapter") or next_record.get("source_adapter") or "")
    next_record["speaker_name"] = str(envelope.get("speaker_name") or "")
    next_record["group_member_name"] = str(envelope.get("group_member_name") or "")
    next_record["sender_role"] = str(envelope.get("sender_role") or next_record.get("sender_role") or "unknown")
    next_record["sender"] = str(envelope.get("sender") or next_record.get("sender") or "")
    if envelope.get("ocr_confidence") not in (None, ""):
        next_record["ocr_confidence"] = envelope.get("ocr_confidence")
    if envelope.get("source_adapter") in OCR_RPA_ADAPTERS or str(next_record.get("source_adapter") or "") in OCR_RPA_ADAPTERS:
        next_record["time"] = str(envelope.get("captured_at") or next_record.get("time") or "")
        next_record["message_time"] = str(envelope.get("captured_at") or next_record.get("message_time") or "")
    elif not str(next_record.get("time") or "").strip():
        next_record["time"] = str(envelope.get("captured_at") or "")
    next_record["message_envelope"] = envelope
    return next_record


def recorder_view_from_message(message: dict[str, Any]) -> dict[str, Any]:
    envelope = build_message_envelope(message, source_adapter=str(message.get("source_adapter") or ""))
    return {
        "content_for_export": str(envelope.get("content_body") or ""),
        "captured_at": str(envelope.get("captured_at") or message.get("observed_at") or message.get("message_time") or ""),
        "quality_flags": list(envelope.get("quality_flags") or []),
        "risk_flags": export_risk_flags(envelope),
        "quoted_fragments": list(envelope.get("quoted_fragments") or []),
        "excluded_fragments": list(envelope.get("excluded_fragments") or []),
        "speaker_name": str(envelope.get("speaker_name") or ""),
        "canonical_input_id": str(envelope.get("canonical_input_id") or ""),
        "canonical_visual_id": str(envelope.get("canonical_visual_id") or ""),
        "bubble_id": str(envelope.get("bubble_id") or ""),
    }


def customer_service_view_from_message(message: dict[str, Any]) -> dict[str, Any]:
    envelope = build_message_envelope(message, source_adapter=str(message.get("source_adapter") or ""))
    return {
        "content_for_reply": str(envelope.get("content_body") or ""),
        "referenced_context": [fragment.get("text", "") for fragment in envelope.get("quoted_fragments", []) if isinstance(fragment, dict)],
        "speaker_name": str(envelope.get("speaker_name") or ""),
        "captured_at": str(envelope.get("captured_at") or ""),
        "quality_flags": list(envelope.get("quality_flags") or []),
        "message_id": str(envelope.get("message_id") or ""),
        "canonical_input_id": str(envelope.get("canonical_input_id") or ""),
        "canonical_visual_id": str(envelope.get("canonical_visual_id") or ""),
        "bubble_id": str(envelope.get("bubble_id") or ""),
    }


def export_risk_flags(envelope: dict[str, Any]) -> list[str]:
    flags = set(str(item) for item in envelope.get("quality_flags", []) if str(item))
    risk: set[str] = set()
    if "quote_preview_removed" in flags or envelope.get("quoted_fragments"):
        risk.add("quote_contamination")
    for flag in flags:
        if flag in {
            "bubble_boundary_ambiguous",
            "multi_bubble_possible_merge",
            "long_press_overlay_detected",
            "ocr_low_confidence",
            "speaker_prefix_split_from_ocr_text",
            "sender_name_in_content",
        }:
            risk.add(flag)
    return sorted(risk)


def message_has_learning_blocking_quality(message: dict[str, Any]) -> str:
    envelope = build_message_envelope(message, source_adapter=str(message.get("source_adapter") or ""))
    flags = set(str(item) for item in envelope.get("quality_flags", []) if str(item))
    if flags & LEARNING_BLOCKING_QUALITY_FLAGS:
        return "message_envelope_quality_risk:" + ",".join(sorted(flags & LEARNING_BLOCKING_QUALITY_FLAGS))
    return ""


def existing_message_envelope(record: dict[str, Any]) -> dict[str, Any]:
    candidates = [
        record.get("message_envelope"),
        record.get("envelope"),
    ]
    raw_payload = record.get("raw_payload") if isinstance(record.get("raw_payload"), dict) else {}
    candidates.append(raw_payload.get("message_envelope"))
    nested_original = raw_payload.get("_original_raw_payload") if isinstance(raw_payload.get("_original_raw_payload"), dict) else {}
    candidates.append(nested_original.get("message_envelope"))
    for candidate in candidates:
        if isinstance(candidate, dict):
            return dict(candidate)
    return {}


def remove_quote_fragments(text: str) -> tuple[str, list[dict[str, str]]]:
    fragments: list[dict[str, str]] = []
    clean = str(text or "")
    for match in list(QUOTE_MARKER_RE.finditer(clean)):
        quoted = match.group(0).strip("[]")
        fragments.append({"text": quoted, "reason": "quote_marker"})
    clean = QUOTE_MARKER_RE.sub(" ", clean)
    lines: list[str] = []
    for raw_line in clean.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        compact = re.sub(r"\s+", "", line)
        if compact.startswith(("引用", "回复")) and ("：" in line or ":" in line or len(compact) <= 80):
            fragments.append({"text": line, "reason": "quote_line"})
            continue
        lines.append(line)
    return "\n".join(lines).strip(), fragments


def remove_non_body_fragments(text: str) -> tuple[str, list[dict[str, str]], list[str], str]:
    excluded: list[dict[str, str]] = []
    flags: list[str] = []
    screen_time_text = ""
    lines: list[str] = []
    for raw_line in str(text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        compact = re.sub(r"\s+", "", line)
        if SCREEN_TIME_RE.match(compact):
            screen_time_text = screen_time_text or line
            excluded.append({"text": line, "reason": "screen_time_removed"})
            continue
        if LONG_PRESS_OVERLAY_RE.match(line):
            excluded.append({"text": line, "reason": "long_press_overlay_removed"})
            flags.append("long_press_overlay_detected")
            continue
        lines.append(line)
    return "\n".join(lines).strip(), excluded, flags, screen_time_text


def known_speaker_names(conversation: dict[str, Any], record: dict[str, Any]) -> list[str]:
    names: list[str] = []
    for payload in (conversation, record):
        for key in ("target_name", "display_name", "group_name", "sender", "speaker_name", "group_member_name"):
            text = str(payload.get(key) or "").strip()
            if text:
                names.append(text)
    raw_payload = conversation.get("raw_payload") if isinstance(conversation.get("raw_payload"), dict) else {}
    for key in ("members", "participants", "member_names"):
        for item in raw_payload.get(key, []) or []:
            if isinstance(item, dict):
                text = str(item.get("name") or item.get("display_name") or item.get("remark") or "").strip()
            else:
                text = str(item or "").strip()
            if text:
                names.append(text)
    deduped: list[str] = []
    seen: set[str] = set()
    for name in names:
        key = normalize_text_for_speaker_check(name)
        if not key or key in seen:
            continue
        seen.add(key)
        deduped.append(name)
    return deduped


def normalize_body_text(text: str) -> str:
    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    return "\n".join(lines).strip()


def speaker_name_in_content(speaker_name: str, content: str) -> bool:
    speaker = normalize_text_for_speaker_check(speaker_name)
    body = normalize_text_for_speaker_check(content)
    return bool(speaker and body.startswith(speaker) and len(body) > len(speaker))


def normalize_flags(value: Any) -> list[str]:
    if isinstance(value, str):
        raw = re.split(r"[,，;；\s]+", value)
    elif isinstance(value, (list, tuple, set)):
        raw = list(value)
    else:
        raw = []
    flags: list[str] = []
    for item in raw:
        text = re.sub(r"[^A-Za-z0-9_]+", "_", str(item or "").strip()).strip("_").lower()
        if text and text not in flags:
            flags.append(text)
    return flags


def list_fragments(value: Any) -> list[dict[str, str]]:
    fragments: list[dict[str, str]] = []
    if isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                text = str(item.get("text") or item.get("content") or "").strip()
                reason = str(item.get("reason") or "unknown").strip() or "unknown"
            else:
                text = str(item or "").strip()
                reason = "unknown"
            if text:
                fragments.append({"text": text, "reason": reason})
    elif isinstance(value, str) and value.strip():
        fragments.append({"text": value.strip(), "reason": "unknown"})
    return fragments


def dedupe_fragments(value: list[dict[str, Any]]) -> list[dict[str, str]]:
    output: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in value:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or item.get("content") or "").strip()
        reason = str(item.get("reason") or "unknown").strip() or "unknown"
        if not text:
            continue
        key = (text, reason)
        if key in seen:
            continue
        seen.add(key)
        output.append({"text": text, "reason": reason})
    return output


def normalize_rect(value: Any) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    output: dict[str, int] = {}
    for key in ("left", "top", "right", "bottom"):
        try:
            output[key] = int(float(value.get(key)))
        except (TypeError, ValueError):
            continue
    if {"left", "top", "right", "bottom"} <= set(output):
        output["width"] = max(0, output["right"] - output["left"])
        output["height"] = max(0, output["bottom"] - output["top"])
    return output


def summarize_ocr_items(value: Any) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    if not isinstance(value, list):
        return items
    for item in value[:20]:
        if not isinstance(item, dict):
            continue
        summary: dict[str, Any] = {"text": str(item.get("text") or "")[:160]}
        for key in ("left", "top", "right", "bottom", "center_x", "center_y", "confidence"):
            if key in item:
                try:
                    summary[key] = round(float(item.get(key)), 4)
                except (TypeError, ValueError):
                    summary[key] = item.get(key)
        items.append(summary)
    return items


def coerce_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
