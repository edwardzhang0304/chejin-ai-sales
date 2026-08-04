"""Text-only projection from the independent Vision module into Brain/history."""

from __future__ import annotations

import copy
from datetime import datetime
from typing import Any


_IMAGE_PREVIEW_MARKERS = ("[图片]", "[image]", "图片", "image")


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def sanitize_name(value: str) -> str:
    keep = [char if char.isalnum() else "_" for char in str(value or "").strip()]
    compact = "".join(keep).strip("_")
    return compact or "target"


def image_preview_text(value: Any) -> bool:
    text = str(value or "").strip().lower()
    return bool(text and any(marker in text for marker in _IMAGE_PREVIEW_MARKERS))


def parse_preview_speaker(source_preview: str, explicit_speaker: str = "") -> str:
    explicit = str(explicit_speaker or "").strip()
    if explicit:
        return explicit
    text = str(source_preview or "").strip()
    for separator in (":", "："):
        if separator in text:
            candidate = text.split(separator, 1)[0].strip()
            if candidate and not image_preview_text(candidate):
                return candidate
    return ""


def _image_pending_signal(value: Any) -> dict[str, Any]:
    source = value if isinstance(value, dict) else {}
    preview = str(source.get("pending_signal_text") or source.get("preview_content") or source.get("content") or "").strip()
    if not image_preview_text(preview) and str(source.get("pending_signal_kind") or "").strip().lower() not in {
        "image_capture",
        "media_capture",
    }:
        return {}
    return source


def target_state_image_pending_signal(target_state: dict[str, Any] | None) -> dict[str, Any]:
    state = target_state if isinstance(target_state, dict) else {}
    for key in (
        "pending_signal",
        "latest_pending_signal",
        "session_monitor_pending_signal",
        "_session_monitor_pending_signal",
    ):
        signal = _image_pending_signal(state.get(key))
        if signal:
            return copy.deepcopy(signal)
    signal = _image_pending_signal(state)
    return copy.deepcopy(signal) if signal else {}


def payload_image_pending_signal(
    payload: dict[str, Any] | None,
    target_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    source = payload if isinstance(payload, dict) else {}
    for key in ("pending_signal", "session_monitor_pending_signal", "_session_monitor_pending_signal"):
        signal = _image_pending_signal(source.get(key))
        if signal:
            return copy.deepcopy(signal)
    return target_state_image_pending_signal(target_state)


def build_brain_safe_image_proxy_message(
    source: dict[str, Any] | None,
    *,
    target_name: str = "",
    session_key: str = "",
    content: str = "",
) -> dict[str, Any]:
    """Build text-only image context; never attach a file or image payload."""

    item = source if isinstance(source, dict) else {}
    pending_signal = _image_pending_signal(item) or _image_pending_signal(item.get("pending_signal"))
    pending_signal_id = str(
        item.get("pending_signal_id")
        or pending_signal.get("pending_signal_id")
        or item.get("source_message_id")
        or item.get("message_id")
        or item.get("id")
        or ""
    ).strip()
    message_id = pending_signal_id or f"clipboard_image_pending:{sanitize_name(session_key or target_name)}"
    source_preview = str(
        item.get("content")
        or item.get("source_preview")
        or pending_signal.get("pending_signal_text")
        or pending_signal.get("preview_content")
        or "[图片]"
    ).strip()
    speaker_name = parse_preview_speaker(
        source_preview,
        str(item.get("speaker_name") or item.get("group_member_name") or pending_signal.get("speaker_name") or ""),
    )
    proxy_content = str(content or "").strip() or "客户发送了一张图片"
    return {
        "id": f"clipboard_image_pending:{message_id}",
        "message_id": f"clipboard_image_pending:{message_id}",
        "source_message_id": message_id,
        # Brain and the scheduler consume only the text consequence of the
        # current clipboard transaction.  Preserve the original media kind as
        # metadata, but never project a second live image message downstream.
        "type": "text",
        "source_message_type": "image",
        "sender": "customer",
        "sender_role": "customer",
        "content": proxy_content,
        "original_content": source_preview,
        "target_name": str(target_name or item.get("target_name") or ""),
        "session_key": str(session_key or item.get("session_key") or pending_signal.get("session_key") or ""),
        "speaker_name": speaker_name,
        "group_member_name": speaker_name,
        "is_customer_image_proxy": True,
        "visual_turn_kind": "customer_image",
        "image_capture_pending": True,
        "pending_signal_id": pending_signal_id,
        "pending_signal_kind": "image_capture",
        "quality_flags": ["synthetic_visual_turn", "clipboard_current_transaction_required"],
        "source": "clipboard_current_transaction",
    }


def build_brain_safe_image_proxy_messages(
    sources: list[dict[str, Any]] | None,
    *,
    target_name: str = "",
    session_key: str = "",
    content: str = "",
) -> list[dict[str, Any]]:
    return [
        build_brain_safe_image_proxy_message(
            source,
            target_name=target_name,
            session_key=session_key,
            content=content,
        )
        for source in (sources or [])
        if isinstance(source, dict)
    ]
