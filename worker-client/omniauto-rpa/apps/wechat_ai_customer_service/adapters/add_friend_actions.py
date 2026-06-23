"""Action result helpers for add_friend RPA."""

from __future__ import annotations

from typing import Any


ACTION_MOUSE_CLICK = "mouse_click"
ACTION_MOUSE_HOVER = "mouse_hover"
ACTION_KEYBOARD_HOTKEY = "keyboard_hotkey"
ACTION_KEYBOARD_KEY = "keyboard_key"
ACTION_CLIPBOARD_PASTE_TEXT = "clipboard_paste_text"
ACTION_COMPOSITE_INPUT = "composite_input"


def make_action_result(
    *,
    action_id: str,
    action_type: str,
    status: str,
    method: str = "",
    target: dict[str, Any] | None = None,
    text: str = "",
    timing_ms: int | float | None = None,
    result: dict[str, Any] | None = None,
    error: str = "",
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "action_id": str(action_id or ""),
        "action_type": str(action_type or ""),
        "status": normalize_action_status(status),
        "method": str(method or ""),
        "target": action_target_metadata(target or {}),
        "input": redacted_text_metadata(text),
        "timing_ms": int(round(float(timing_ms or 0))),
        "result": dict(result or {}),
        "error": str(error or ""),
        "metadata": dict(metadata or {}),
    }


def normalize_action_status(status: str) -> str:
    clean = str(status or "").strip().lower()
    if clean in {"pending", "running", "completed", "failed", "skipped"}:
        return clean
    if clean in {"ok", "success", "passed", "pass"}:
        return "completed"
    if clean in {"fail", "error"}:
        return "failed"
    return "unknown"


def action_target_metadata(target: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(target, dict):
        target = {}
    point = target.get("point")
    if not (isinstance(point, list) and len(point) >= 2):
        point = [target.get("x", target.get("screen_x", 0)), target.get("y", target.get("screen_y", 0))]
    bounds = target.get("bounds")
    if not (isinstance(bounds, list) and len(bounds) >= 4):
        bounds = target.get("click_bounds") if isinstance(target.get("click_bounds"), list) else []
    return {
        "name": str(target.get("name") or ""),
        "label": str(target.get("label") or target.get("name") or ""),
        "strategy": str(target.get("strategy") or ""),
        "region": str(target.get("region") or ""),
        "point": normalize_point(point),
        "bounds": normalize_bounds(bounds),
        "confidence": safe_float(target.get("confidence"), default=0.0),
    }


def redacted_text_metadata(text: str) -> dict[str, Any]:
    clean = str(text or "")
    return {
        "text_length": len(clean),
        "is_empty": not bool(clean),
    }


def normalize_point(point: Any) -> list[int]:
    if not isinstance(point, (list, tuple)) or len(point) < 2:
        return [0, 0]
    return [int(point[0] or 0), int(point[1] or 0)]


def normalize_bounds(bounds: Any) -> list[int]:
    if not isinstance(bounds, (list, tuple)) or len(bounds) < 4:
        return []
    values = [int(value or 0) for value in list(bounds)[:4]]
    left, top, right, bottom = values
    return [min(left, right), min(top, bottom), max(left, right), max(top, bottom)]


def safe_float(value: Any, *, default: float = 0.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed
