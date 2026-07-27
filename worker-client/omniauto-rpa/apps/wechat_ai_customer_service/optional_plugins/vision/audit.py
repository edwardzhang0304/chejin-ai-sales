"""Safe, text-only audit normalization owned by the vision module."""

from __future__ import annotations

from typing import Any


_FORBIDDEN_KEYS = {
    "image",
    "image_bytes",
    "image_path",
    "saved_image_path",
    "screenshot",
    "screenshot_path",
    "base64",
}


def safe_audit_payload(value: dict[str, Any] | None) -> dict[str, Any]:
    source = value if isinstance(value, dict) else {}
    return {
        str(key): item
        for key, item in source.items()
        if str(key).strip().lower() not in _FORBIDDEN_KEYS
        and not isinstance(item, (bytes, bytearray, memoryview))
    }
