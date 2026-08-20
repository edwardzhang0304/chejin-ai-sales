"""Pure geometry helpers for the Windows WeChat Win32/OCR adapter."""

from __future__ import annotations

from typing import Any


MIN_SEND_CLIENT_WIDTH = 700
MIN_SEND_CLIENT_HEIGHT = 720
MIN_CAPTURE_WINDOW_WIDTH = 420
MIN_CAPTURE_WINDOW_HEIGHT = 260
OFFSCREEN_GEOMETRY_BOUNDARY = -30000


def bounded_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def bounded_float(value: Any, *, default: float, minimum: float, maximum: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def center_of_bounds(bounds: list[int]) -> tuple[int, int]:
    if len(bounds) < 4:
        return 0, 0
    return int((int(bounds[0]) + int(bounds[2])) / 2), int((int(bounds[1]) + int(bounds[3])) / 2)


def point_in_bounds(x: int, y: int, bounds: list[int]) -> bool:
    left, top, right, bottom = [int(value) for value in bounds]
    return left <= x <= right and top <= y <= bottom


def clamp_point_to_bounds(x: int, y: int, bounds: list[int]) -> tuple[int, int]:
    left, top, right, bottom = [int(value) for value in bounds]
    return (
        bounded_int(x, default=x, minimum=min(left, right), maximum=max(left, right)),
        bounded_int(y, default=y, minimum=min(top, bottom), maximum=max(top, bottom)),
    )


def rect_overlaps_region(rect: dict[str, int], bounds: tuple[int, int, int, int]) -> bool:
    left, top, right, bottom = bounds
    return int(rect.get("right") or 0) > left and int(rect.get("left") or 0) < right and int(rect.get("bottom") or 0) > top and int(rect.get("top") or 0) < bottom


def relative_rect(rect: dict[str, int], geometry: dict[str, Any]) -> dict[str, int]:
    left = int(rect.get("left") or 0) - int(geometry.get("left") or 0)
    top = int(rect.get("top") or 0) - int(geometry.get("top") or 0)
    right = int(rect.get("right") or 0) - int(geometry.get("left") or 0)
    bottom = int(rect.get("bottom") or 0) - int(geometry.get("top") or 0)
    return {
        "left": left,
        "top": top,
        "right": right,
        "bottom": bottom,
        "width": max(0, right - left),
        "height": max(0, bottom - top),
    }


def validate_send_geometry(geometry: dict[str, Any]) -> dict[str, Any]:
    width = int(geometry.get("width") or 0)
    height = int(geometry.get("height") or 0)
    if width < MIN_SEND_CLIENT_WIDTH or height < MIN_SEND_CLIENT_HEIGHT:
        return {
            "ok": False,
            "reason": "window_too_small_for_safe_send",
            "geometry": geometry,
            "error": f"WeChat window is too small for safe send: {width}x{height}.",
        }
    return {"ok": True, "reason": "geometry_ok", "geometry": geometry}


def validate_capture_geometry(geometry: dict[str, Any]) -> dict[str, Any]:
    left = int(geometry.get("left") or 0)
    top = int(geometry.get("top") or 0)
    width = int(geometry.get("width") or 0)
    height = int(geometry.get("height") or 0)
    if left <= OFFSCREEN_GEOMETRY_BOUNDARY or top <= OFFSCREEN_GEOMETRY_BOUNDARY:
        return {
            "ok": False,
            "reason": "window_offscreen_or_minimized",
            "geometry": geometry,
            "error": f"WeChat window is offscreen/minimized: left={left}, top={top}, size={width}x{height}.",
        }
    if width < MIN_CAPTURE_WINDOW_WIDTH or height < MIN_CAPTURE_WINDOW_HEIGHT:
        return {
            "ok": False,
            "reason": "window_too_small_for_capture",
            "geometry": geometry,
            "error": f"WeChat window is too small for reliable capture: {width}x{height}.",
        }
    return {"ok": True, "reason": "capture_geometry_ok", "geometry": geometry}
