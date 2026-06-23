"""Pure window action state helpers for the Windows WeChat Win32/OCR adapter."""

from __future__ import annotations

from typing import Any


FOREGROUND_READY_REASONS = frozenset({"foreground_matches_target", "foreground_root_matches_target"})


def foreground_guard_ready(guard: dict[str, Any] | None) -> bool:
    if not isinstance(guard, dict):
        return False
    return bool(guard.get("ok")) and str(guard.get("reason") or "") in FOREGROUND_READY_REASONS


def tray_hidden_from_counts(*, main_count: Any, visible_main_count: Any) -> bool:
    try:
        parsed_main_count = int(main_count or 0)
        parsed_visible_main_count = int(visible_main_count or 0)
    except Exception:
        return False
    return bool(parsed_main_count > 0 and parsed_visible_main_count <= 0)


def tray_hidden_from_probe(probe: dict[str, Any]) -> bool:
    return tray_hidden_from_counts(
        main_count=(probe or {}).get("main_count"),
        visible_main_count=(probe or {}).get("visible_main_count"),
    )


def activate_window_settings(
    *,
    aggressive_focus: bool,
    attach_thread_input: bool,
    debounce_seconds: Any,
) -> dict[str, Any]:
    try:
        parsed_debounce_seconds = float(debounce_seconds)
    except (TypeError, ValueError):
        parsed_debounce_seconds = 2.5
    return {
        "aggressive_focus": bool(aggressive_focus),
        "attach_thread_input": bool(aggressive_focus) or bool(attach_thread_input),
        "debounce_seconds": max(0.0, min(10.0, parsed_debounce_seconds)),
    }


def activate_debounce_active(
    *,
    now_monotonic: Any,
    last_monotonic: Any,
    debounce_seconds: Any,
) -> bool:
    try:
        now_value = float(now_monotonic)
        last_value = float(last_monotonic or 0.0)
        debounce_value = float(debounce_seconds)
    except (TypeError, ValueError):
        return False
    return bool(debounce_value > 0 and last_value > 0 and now_value - last_value <= debounce_value)
