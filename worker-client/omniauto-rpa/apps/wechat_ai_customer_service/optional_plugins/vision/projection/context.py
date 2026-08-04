"""Pure compatibility merge for vision-projected conversation context."""

from __future__ import annotations

from typing import Any


_PRODUCT_SCOPED_CONTEXT_KEYS = (
    "last_product_name",
    "last_product_unit",
    "last_product_source",
    "last_product_price",
    "last_unit_price",
    "last_quantity",
    "last_total",
    "vehicle_image_match",
)


def merge_conversation_context_patch(
    existing: dict[str, Any] | None,
    update: dict[str, Any] | None,
) -> dict[str, Any]:
    merged = dict(existing) if isinstance(existing, dict) else {}
    patch = dict(update) if isinstance(update, dict) else {}
    previous_product_id = str(merged.get("last_product_id") or "").strip()
    next_product_id = str(patch.get("last_product_id") or "").strip()
    if previous_product_id and next_product_id and previous_product_id != next_product_id:
        for key in _PRODUCT_SCOPED_CONTEXT_KEYS:
            merged.pop(key, None)
    merged.update(patch)
    return merged
