from __future__ import annotations

import re
from typing import Any


IMAGE_PREVIEW_TOKENS = ("[图片]", "[照片]", "[Image]", "图片", "照片", "发送了一张图片")
IMAGE_MESSAGE_TYPES = {"image", "picture", "photo"}
IMAGE_CAPTURE_SIGNAL_KINDS = {"image_capture", "media_capture"}


def image_preview_text(value: Any) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    compact = re.sub(r"\s+", "", text).lower()
    return any(re.sub(r"\s+", "", token).lower() in compact for token in IMAGE_PREVIEW_TOKENS)


def pending_image_signal_was_processed(
    target_state: dict[str, Any] | None,
    signal_id: str,
) -> bool:
    state = target_state if isinstance(target_state, dict) else {}
    clean_signal_id = str(signal_id or "").strip()
    if not clean_signal_id:
        return False
    processed_ids = state.get("processed_visual_pending_signal_ids")
    if isinstance(processed_ids, list) and clean_signal_id in {
        str(item or "").strip() for item in processed_ids
    }:
        return True
    context = state.get("conversation_context") if isinstance(state.get("conversation_context"), dict) else {}
    sources: list[Any] = []
    for container in (state, context):
        for key in ("ledger_recent_messages", "recent_messages", "visual_recent_messages"):
            value = container.get(key) if isinstance(container, dict) else None
            if isinstance(value, list):
                sources.extend(value)
    # A captured placeholder is not a completed visual turn.  Marking it as
    # processed here would prevent the very first planner pass from performing
    # the required current-clipboard transaction.  Only a prior textual vision
    # result (or the explicit processed-id ledger above) can suppress a retry.
    return any(
        isinstance(item, dict)
        and str(item.get("pending_signal_id") or "").strip() == clean_signal_id
        and bool(
            (
                item.get("image_understanding")
                if isinstance(item.get("image_understanding"), dict)
                else {}
            ).get("vision_summary")
            or item.get("vision_summary")
        )
        for item in sources
    )


def customer_image_capture_trigger(
    *,
    payload: dict[str, Any] | None,
    pending_signal: dict[str, Any] | None = None,
    pending_signal_kind: str = "",
    target_state: dict[str, Any] | None = None,
    recent_message_limit: int = 6,
) -> dict[str, Any]:
    """Cheap metadata-only gate; never performs RPA, OCR, or LLM work."""

    source = payload if isinstance(payload, dict) else {}
    signal = pending_signal if isinstance(pending_signal, dict) else {}
    signal_kind = str(
        pending_signal_kind or signal.get("pending_signal_kind") or ""
    ).strip().lower()
    signal_id = str(signal.get("pending_signal_id") or "").strip()
    if signal_id and pending_image_signal_was_processed(target_state, signal_id):
        return {
            "should_run": False,
            "reason": "pending_image_signal_already_processed",
            "pending_signal_kind": signal_kind,
            "pending_signal_id": signal_id,
            "evidence_count": 0,
        }
    if signal_kind in IMAGE_CAPTURE_SIGNAL_KINDS:
        return {
            "should_run": True,
            "reason": "pending_signal_is_image",
            "pending_signal_kind": signal_kind,
            "pending_signal_id": signal_id,
            "evidence_count": 1,
        }

    signal_text = str(
        signal.get("pending_signal_text") or signal.get("preview_content") or ""
    ).strip()
    if image_preview_text(signal_text):
        return {
            "should_run": True,
            "reason": "pending_signal_preview_is_image",
            "pending_signal_kind": signal_kind,
            "pending_signal_id": signal_id,
            "evidence_count": 1,
        }

    return {
        "should_run": False,
        "reason": "current_image_pending_signal_missing",
        "pending_signal_kind": signal_kind,
        "pending_signal_id": signal_id,
        "evidence_count": 0,
    }
