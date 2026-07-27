"""Private request/result normalization for the independent vision service."""

from __future__ import annotations

from typing import Any


def vision_context(value: dict[str, Any] | None) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def unavailable_result(reason: str, *, context_only: bool = False) -> dict[str, Any]:
    return {
        "enabled": False,
        "applied": False,
        "adoptable": False,
        "context_only": bool(context_only),
        "reason": str(reason or "vision_capability_unavailable"),
    }


def require_mapping(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TypeError(f"{name} must be a mapping")
    return value
