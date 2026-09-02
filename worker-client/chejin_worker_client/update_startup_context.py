from __future__ import annotations

from typing import Any


_CONTEXT: dict[str, Any] | None = None


def set_update_startup_context(context: dict[str, Any] | None) -> None:
    global _CONTEXT
    _CONTEXT = dict(context) if isinstance(context, dict) else None


def take_update_startup_context() -> dict[str, Any] | None:
    global _CONTEXT
    context = _CONTEXT
    _CONTEXT = None
    return dict(context) if isinstance(context, dict) else None
