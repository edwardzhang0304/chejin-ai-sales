from __future__ import annotations

from typing import Any


DEFAULT_IMAGE_SOURCE_LIMITS = {
    "max_encoded_source_bytes": 12 * 1024 * 1024,
    "max_decoded_pixels": 20_000_000,
    "max_decoded_rgba_bytes": 80 * 1024 * 1024,
    "max_provider_payload_bytes": 3 * 1024 * 1024,
    "max_provider_edge_px": 2048,
    "max_visible_image_candidates": 64,
    "clipboard_no_progress_timeout_seconds": 15,
}


def resolve_image_source_limits(
    config_or_limits: dict[str, Any] | None,
) -> dict[str, int]:
    value = config_or_limits if isinstance(config_or_limits, dict) else {}
    image_contract = (
        value.get("image_contract")
        if isinstance(value.get("image_contract"), dict)
        else {}
    )
    declared = (
        image_contract.get("source_limits")
        if isinstance(image_contract.get("source_limits"), dict)
        else value
    )
    result: dict[str, int] = {}
    for key, fallback in DEFAULT_IMAGE_SOURCE_LIMITS.items():
        try:
            parsed = int(declared.get(key) or fallback)
        except (TypeError, ValueError):
            parsed = fallback
        result[key] = max(1, parsed)
    return result
