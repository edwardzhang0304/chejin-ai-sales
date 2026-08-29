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
MESSAGE_VIEWPORT_DIGEST_SCHEMA_VERSION = 3
SEND_CONTEXT_BUSINESS_DIGEST_SCHEMA_VERSION = 3

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


def stable_business_content_signature(
    observation: dict[str, Any],
) -> str:
    """Return content/state evidence without geometry or frame identity."""

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


def business_media_state(observation: dict[str, Any]) -> str:
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


def ordered_message_viewport_observations(
    observations: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """Return eligible rows in current-frame screen order without merging.

    Sidecar owns the single OCR/visual same-row merge before this shared
    projection is called.  The shared projection must not silently repair a
    duplicate/conflicting Sidecar contract for the Worker.
    """

    eligible = [
        (input_index, observation)
        for input_index, observation in enumerate(observations or [])
        if isinstance(observation, dict)
        and str(observation.get("row_kind") or "").strip().lower()
        in SEND_CONTEXT_ROW_KINDS
    ]

    def sort_key(
        indexed_observation: tuple[int, dict[str, Any]],
    ) -> tuple[Any, ...]:
        input_index, observation = indexed_observation
        rect = _observation_rect(observation) or [0, 0, 0, 0]
        return (
            rect[1],
            rect[0],
            rect[3],
            input_index,
        )

    eligible.sort(key=sort_key)
    return [observation for _, observation in eligible]


def _normalized_business_sequence(
    observations: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """The sole five-field business projection implementation."""

    sequence: list[dict[str, Any]] = []
    for observation in ordered_message_viewport_observations(observations):
        sequence.append(
            {
                "screen_order": len(sequence),
                "sender_role": str(
                    observation.get("sender_role") or "unknown"
                ).strip().lower(),
                "message_type": str(
                    observation.get("message_type") or "unknown"
                ).strip().lower(),
                "normalized_content_signature": (
                    stable_business_content_signature(observation)
                ),
                "media_state": business_media_state(observation),
            }
        )
    return sequence


def normalized_message_viewport_sequence(
    observations: list[dict[str, Any]] | None,
    *,
    message_viewport_bounds: Any,
) -> list[dict[str, Any]]:
    """Compatibility entry to the sole geometry-free business projection."""

    _ = message_viewport_bounds
    return _normalized_business_sequence(observations)


def normalized_business_message_sequence(
    observations: list[dict[str, Any]] | None,
    *,
    message_viewport_bounds: Any,
) -> list[dict[str, Any]]:
    """Project ordered business facts without cross-frame geometry.

    C2 business rereads, media-action admission and C3 pre-send/S0/S1/S2 all
    consume this projection. Coordinates still locate a target in the latest
    frame, but they cannot veto an already-authorized action, declare that the
    conversation changed, or create durable message identity.
    """

    # Bounds remain required by the caller's layout contract but are not
    # consumed by this cross-frame business projection.
    _ = message_viewport_bounds
    return _normalized_business_sequence(observations)
