"""Structural image occurrence observation owned by the vision module."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from .wechat import detect_visual_image_bubbles, extract_chat_time_markers


def visual_image_envelopes_from_bubbles(
    bubbles: list[dict[str, Any]] | None,
    existing_messages: list[dict[str, Any]] | None,
    *,
    target: str,
) -> list[dict[str, Any]]:
    """Project structural media occurrences into the frozen message contract."""

    def message_identity(item: dict[str, Any]) -> str:
        for key in (
            "message_id",
            "id",
            "legacy_message_id",
            "original_message_id",
            "canonical_input_id",
        ):
            value = str(item.get(key) or "").strip()
            if value:
                return value
        return ""

    def message_vertical_bounds(item: dict[str, Any]) -> tuple[int, int] | None:
        rect = item.get("bubble_rect") if isinstance(item.get("bubble_rect"), dict) else {}
        if not rect:
            return None
        try:
            top = int(float(rect.get("top") or 0))
            bottom = int(float(rect.get("bottom") or 0))
        except (TypeError, ValueError):
            return None
        if bottom <= top:
            return None
        return top, bottom

    text_rows: list[tuple[int, int, str]] = []
    for message in existing_messages or []:
        if not isinstance(message, dict):
            continue
        message_type = str(message.get("type") or "text").strip().lower() or "text"
        if message_type != "text":
            continue
        identity = message_identity(message)
        vertical = message_vertical_bounds(message)
        if not identity or vertical is None:
            continue
        text_rows.append((vertical[0], vertical[1], identity))
    text_rows.sort(key=lambda item: (item[0], item[1], item[2]))

    def bubble_top(item: dict[str, Any]) -> int:
        bounds = item.get("bounds")
        if not isinstance(bounds, (list, tuple)) or len(bounds) < 2:
            return 0
        try:
            return int(bounds[1] or 0)
        except (TypeError, ValueError):
            return 0

    ordered = sorted(
        [item for item in (bubbles or []) if isinstance(item, dict)],
        key=lambda item: (
            bubble_top(item),
            0 if str(item.get("side") or "").strip().lower() == "customer" else 1,
        ),
    )
    known_ids = {
        str(item.get("id") or item.get("message_id") or "").strip()
        for item in (existing_messages or [])
        if isinstance(item, dict)
    }
    occurrence_counts: dict[tuple[str, str], int] = {}
    result: list[dict[str, Any]] = []
    for bubble in ordered:
        side = str(bubble.get("side") or "").strip().lower()
        if side not in {"customer", "self"}:
            continue
        observed_time = str(bubble.get("wechat_message_time") or "").strip()
        bounds = bubble.get("bounds")
        try:
            bubble_top_value, bubble_bottom_value = (
                int(float(bounds[1])),
                int(float(bounds[3])),
            ) if isinstance(bounds, (list, tuple)) and len(bounds) >= 4 else (0, 0)
        except (TypeError, ValueError):
            bubble_top_value, bubble_bottom_value = 0, 0
        preceding_rows = [item for item in text_rows if item[1] <= bubble_top_value + 6]
        following_rows = [item for item in text_rows if item[0] >= bubble_bottom_value - 6]
        preceding_text_id = preceding_rows[-1][2] if preceding_rows else ""
        following_text_id = following_rows[0][2] if following_rows else ""
        # Keep the established occurrence-id contract byte-for-byte stable.
        # Neighbor ids are private current-turn binding evidence only; adding
        # them to the persisted id seed would reinterpret already-recorded
        # image occurrences after an upgrade.
        occurrence_key = (side, observed_time)
        occurrence_index = occurrence_counts.get(occurrence_key, 0)
        occurrence_counts[occurrence_key] = occurrence_index + 1
        identity_seed = json.dumps(
            {
                "target": str(target or ""),
                "side": side,
                "time": observed_time,
                "occurrence_index": occurrence_index,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        digest = hashlib.sha256(identity_seed.encode("utf-8")).hexdigest()[:20]
        message_id = f"visual_{side}_context_{digest}"
        if message_id in known_ids:
            continue
        known_ids.add(message_id)
        result.append(
            {
                "id": message_id,
                "message_id": message_id,
                "type": "image",
                "message_type": "image",
                "sender": side,
                "sender_role": side,
                "visual_side": side,
                "visual_turn_kind": f"{side}_image",
                **({"is_self_image": True} if side == "self" else {}),
                "content": "[图片]",
                "bubble_rect": [int(float(value)) for value in (bounds or [])[:4]],
                "time": observed_time,
                "source_adapter": "win32_ocr_structural_image_observer",
                **(
                    {"_vision_preceding_text_id": preceding_text_id}
                    if preceding_text_id
                    else {}
                ),
                **(
                    {"_vision_following_text_id": following_text_id}
                    if following_text_id
                    else {}
                ),
            }
        )
    return result


def visual_image_messages_from_current_surface(
    screenshot: Any,
    ocr_items: list[dict[str, Any]] | None,
    existing_messages: list[dict[str, Any]] | None,
    *,
    target: str,
    side_filter: str,
    max_images: int,
) -> list[dict[str, Any]]:
    if screenshot is None:
        return []
    try:
        bubbles = detect_visual_image_bubbles(
            screenshot,
            messages=list(existing_messages or []),
            max_images=max_images,
            side_filter=side_filter,
            time_markers=extract_chat_time_markers(
                list(ocr_items or []),
                tuple(getattr(screenshot, "size", (0, 0))),
            ),
        )
    except Exception:
        return []
    return visual_image_envelopes_from_bubbles(bubbles, existing_messages, target=target)


def self_visual_image_messages_from_current_surface(
    screenshot: Any,
    ocr_items: list[dict[str, Any]] | None,
    existing_messages: list[dict[str, Any]] | None,
    *,
    target: str,
) -> list[dict[str, Any]]:
    return visual_image_messages_from_current_surface(
        screenshot,
        ocr_items,
        existing_messages,
        target=target,
        side_filter="self",
        max_images=1,
    )
