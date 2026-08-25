"""Pure projection shared by the Win32 Sidecar and the Worker guard.

The projection describes only what is visible in one immutable message
viewport.  It must never create, inherit, or compare durable message identity.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from typing import Any


SEND_CONTEXT_ROW_KINDS = {
    "text_bubble",
    "voice_bubble",
    "voice_transcript",
    "image_bubble",
    "system_message",
}
MESSAGE_VIEWPORT_DIGEST_SCHEMA_VERSION = 2
MESSAGE_VIEWPORT_BOUNDS_QUANTIZATION = 64

_OCR_PUNCTUATION_TRANSLATION = str.maketrans(
    {
        "。": ".",
        "｡": ".",
        "、": ",",
        "､": ",",
        "“": '"',
        "”": '"',
        "„": '"',
        "‟": '"',
        "「": '"',
        "」": '"',
        "『": '"',
        "』": '"',
        "‘": "'",
        "’": "'",
        "‚": "'",
        "‛": "'",
        "—": "-",
        "–": "-",
        "―": "-",
        "−": "-",
        "‐": "-",
        "‑": "-",
        "…": "...",
        "‥": "..",
        "【": "[",
        "】": "]",
        "〔": "[",
        "〕": "]",
    }
)


def normalized_projection_text(value: Any) -> str:
    """Normalize OCR presentation without inventing message content."""

    normalized = unicodedata.normalize("NFKC", str(value or ""))
    normalized = normalized.translate(_OCR_PUNCTUATION_TRANSLATION)
    normalized = "".join(
        character
        for character in normalized
        if not character.isspace()
        and unicodedata.category(character) != "Cf"
        and ord(character) not in {0xFE0E, 0xFE0F}
    )
    normalized = re.sub(r"\.{2,}", "...", normalized)
    return normalized.casefold()


def _message_rect_values(value: Any) -> list[float] | None:
    if isinstance(value, dict):
        raw = [
            value.get("left"),
            value.get("top"),
            value.get("right"),
            value.get("bottom"),
        ]
    elif isinstance(value, (list, tuple)) and len(value) >= 4:
        raw = list(value[:4])
    else:
        return None
    try:
        left, top, right, bottom = [float(item) for item in raw]
    except (TypeError, ValueError):
        return None
    if right <= left or bottom <= top:
        return None
    return [left, top, right, bottom]


def normalized_relative_message_bounds(
    value: Any,
    *,
    viewport_bounds: Any,
) -> list[int]:
    """Quantize a bubble relative to the viewport to absorb OCR jitter."""

    rect = _message_rect_values(value)
    viewport = _message_rect_values(viewport_bounds)
    if rect is None or viewport is None:
        return []
    left, top, right, bottom = rect
    view_left, view_top, view_right, view_bottom = viewport
    width = max(1.0, view_right - view_left)
    height = max(1.0, view_bottom - view_top)
    scale = MESSAGE_VIEWPORT_BOUNDS_QUANTIZATION

    def bucket(current: float, origin: float, extent: float) -> int:
        return max(
            0,
            min(scale, int(round((current - origin) / extent * scale))),
        )

    return [
        bucket(left, view_left, width),
        bucket(top, view_top, height),
        bucket(right, view_left, width),
        bucket(bottom, view_top, height),
    ]


def _content_signature(observation: dict[str, Any]) -> str:
    row_kind = str(observation.get("row_kind") or "").strip().lower()
    if row_kind in {"text_bubble", "voice_transcript", "system_message"}:
        normalized = normalized_projection_text(
            observation.get("content_clean")
        )
        normalized = "".join(
            character
            for character in normalized
            if not unicodedata.category(character).startswith("P")
        )
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    if row_kind == "voice_bubble":
        duration = str(observation.get("voice_duration") or "").strip()
        if not duration:
            duration_text = str(
                observation.get("voice_duration_text") or ""
            ).replace("\u3000", " ").strip()
            duration_match = re.search(
                r"\d{1,3}",
                re.sub(r"\s+", "", duration_text),
            )
            duration = duration_match.group(0) if duration_match else ""
        return hashlib.sha256(
            json.dumps(
                {"duration": duration, "row_kind": row_kind},
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
    return hashlib.sha256(row_kind.encode("utf-8")).hexdigest()


def _media_state(observation: dict[str, Any]) -> str:
    row_kind = str(observation.get("row_kind") or "").strip().lower()
    if row_kind == "voice_transcript":
        return "transcribed"
    if row_kind == "voice_bubble":
        return "untranscribed"
    if row_kind == "image_bubble":
        return "image"
    return ""


def _observation_rect(observation: dict[str, Any]) -> list[float] | None:
    return _message_rect_values(observation.get("bubble_rect"))


def _is_visual_voice_hint(observation: dict[str, Any]) -> bool:
    quality_flags = {
        str(value or "").strip().lower()
        for value in (observation.get("quality_flags") or [])
    }
    observation_id = str(observation.get("observation_id") or "").lower()
    return "visual_voice_hint" in quality_flags or observation_id.startswith(
        "voice-hint:"
    )


def _same_voice_row_geometry(
    first: dict[str, Any],
    second: dict[str, Any],
) -> bool:
    first_rect = _observation_rect(first)
    second_rect = _observation_rect(second)
    if first_rect is None or second_rect is None:
        return False
    first_left, first_top, first_right, first_bottom = first_rect
    second_left, second_top, second_right, second_bottom = second_rect
    first_center_y = (first_top + first_bottom) / 2.0
    second_center_y = (second_top + second_bottom) / 2.0
    first_height = first_bottom - first_top
    second_height = second_bottom - second_top
    allowed_y = max(8.0, min(first_height, second_height) * 0.6)
    horizontal_overlap = min(first_right, second_right) - max(
        first_left, second_left
    )
    return (
        abs(first_center_y - second_center_y) <= allowed_y
        and horizontal_overlap > 0
    )


def _prefer_observation(
    first: dict[str, Any],
    second: dict[str, Any],
) -> dict[str, Any]:
    def score(value: dict[str, Any]) -> tuple[int, int, int]:
        source_message = (
            value.get("source_message")
            if isinstance(value.get("source_message"), dict)
            else {}
        )
        return (
            0 if _is_visual_voice_hint(value) else 1,
            1 if str(value.get("voice_duration") or "").strip() else 0,
            1 if str(source_message.get("id") or "").strip() else 0,
        )

    return second if score(second) > score(first) else first


def canonical_message_viewport_observations(
    observations: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """Order visible facts and collapse duplicate visual voice hints."""

    eligible = [
        observation
        for observation in (observations or [])
        if isinstance(observation, dict)
        and str(observation.get("row_kind") or "").strip().lower()
        in SEND_CONTEXT_ROW_KINDS
    ]

    def sort_key(observation: dict[str, Any]) -> tuple[Any, ...]:
        rect = _observation_rect(observation) or [0, 0, 0, 0]
        return (rect[1], rect[0], rect[3], str(observation.get("observation_id") or ""))

    eligible.sort(key=sort_key)
    canonical: list[dict[str, Any]] = []
    for observation in eligible:
        row_kind = str(observation.get("row_kind") or "").strip().lower()
        role = str(observation.get("sender_role") or "unknown").strip().lower()
        duplicate_index: int | None = None
        if row_kind == "voice_bubble":
            for index, existing in enumerate(canonical):
                if (
                    str(existing.get("row_kind") or "").strip().lower()
                    == "voice_bubble"
                    and str(existing.get("sender_role") or "unknown").strip().lower()
                    == role
                    and (
                        _is_visual_voice_hint(existing)
                        or _is_visual_voice_hint(observation)
                    )
                    and _same_voice_row_geometry(existing, observation)
                ):
                    duplicate_index = index
                    break
        if duplicate_index is None:
            canonical.append(observation)
        else:
            canonical[duplicate_index] = _prefer_observation(
                canonical[duplicate_index], observation
            )
    canonical.sort(key=sort_key)
    return canonical


def normalized_message_viewport_sequence(
    observations: list[dict[str, Any]] | None,
    *,
    message_viewport_bounds: Any,
) -> list[dict[str, Any]]:
    """Project one frame into ordered, non-identity visual business facts."""

    sequence: list[dict[str, Any]] = []
    for observation in canonical_message_viewport_observations(observations):
        sequence.append(
            {
                "screen_order": len(sequence),
                "sender_role": str(
                    observation.get("sender_role") or "unknown"
                ).strip().lower(),
                "message_type": str(
                    observation.get("message_type") or "unknown"
                ).strip().lower(),
                "relative_quantized_bounds": normalized_relative_message_bounds(
                    observation.get("bubble_rect"),
                    viewport_bounds=message_viewport_bounds,
                ),
                "stable_content_signature": _content_signature(observation),
                "media_state": _media_state(observation),
            }
        )
    return sequence
