"""Pure geometry helpers for the Windows WeChat Win32/OCR adapter."""

from __future__ import annotations

import random
from typing import Any


SEARCH_BOX_REL = (110, 70)
SESSION_CLICK_X = 260
CHAT_HEADER_MAX_Y = 90
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


def session_split_x(width: int) -> int:
    return max(300, min(370, int(width * 0.52)))


def chat_header_cutoff_y(height: int) -> int:
    return max(CHAT_HEADER_MAX_Y, min(150, int(height * 0.12)))


def active_chat_title_cutoff_y(height: int) -> int:
    return max(120, min(170, int(height * 0.18)))


def active_chat_title_top_cutoff_y(height: int) -> int:
    return max(92, min(122, int(height * 0.14)))


def active_chat_title_left_x(width: int) -> int:
    split_x = session_split_x(width)
    return max(300, min(split_x + 24, int(width * 0.37)))


def active_chat_title_right_x(width: int) -> int:
    return min(width - 90, session_split_x(width) + max(330, int(width * 0.48)))


def active_chat_title_top_y(height: int) -> int:
    return max(44, min(68, int(height * 0.075)))


def active_chat_title_bottom_y(height: int) -> int:
    return max(102, min(140, int(height * 0.155)))


def search_box_point_for_geometry(geometry: dict[str, Any]) -> tuple[int, int]:
    width = int(geometry.get("width") or 0)
    height = int(geometry.get("height") or 0)
    split_x = session_split_x(width)
    fallback_x, fallback_y = SEARCH_BOX_REL
    search_x = bounded_int(int(split_x * 0.33), default=fallback_x, minimum=90, maximum=min(170, max(100, split_x - 48)))
    search_y = bounded_int(int(height * 0.075), default=fallback_y, minimum=48, maximum=130)
    return search_x, search_y


def session_click_x_for_geometry(geometry: dict[str, Any]) -> int:
    width = int(geometry.get("width") or 0)
    split_x = session_split_x(width)
    center_hint = int(split_x * 0.72)
    return bounded_int(center_hint, default=SESSION_CLICK_X, minimum=180, maximum=max(220, split_x - 40))


def input_text_region_bounds(geometry: dict[str, Any]) -> tuple[int, int, int, int]:
    width = int(geometry.get("width") or 0)
    height = int(geometry.get("height") or 0)
    left = max(session_split_x(width) + 24, int(width * 0.36))
    top = max(int(height * 0.79), height - 180)
    right = max(left + 20, width - 95)
    bottom = min(height - 58, height)
    if bottom <= top:
        top = max(0, int(height * 0.84))
        bottom = max(top + 1, height - 58)
    return (left, top, right, bottom)


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


def rect_in_input_area(rect: dict[str, int], geometry: dict[str, Any]) -> bool:
    rel = relative_rect(rect, geometry)
    width = int(geometry.get("width") or 0)
    height = int(geometry.get("height") or 0)
    if rel["width"] <= 80 or rel["height"] <= 12:
        return False
    if rel["right"] <= session_split_x(width) + 100:
        return False
    bounds = input_text_region_bounds(geometry)
    left, top, right, bottom = bounds
    draft_top = min(top, max(int(height * 0.79), height - 180))
    center_y = (rel["top"] + rel["bottom"]) / 2.0
    horizontal_overlap = min(rel["right"], right) - max(rel["left"], left)
    if horizontal_overlap <= 0:
        return False
    return rel["top"] >= draft_top - 6 and rel["bottom"] <= bottom + 10 and draft_top <= center_y <= bottom


def rect_in_input_toolbar(rect: dict[str, int], geometry: dict[str, Any]) -> bool:
    rel = relative_rect(rect, geometry)
    width = int(geometry.get("width") or 0)
    height = int(geometry.get("height") or 0)
    if rel["width"] <= 20 or rel["height"] <= 15:
        return False
    if rel["right"] <= session_split_x(width) + 60:
        return False
    return rel["top"] >= int(height * 0.78) and rel["bottom"] <= height + 8


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


def calculate_send_points(geometry: dict[str, Any]) -> dict[str, Any]:
    geometry_check = validate_send_geometry(geometry)
    if not geometry_check.get("ok"):
        return geometry_check
    client_width = int(geometry["width"])
    client_height = int(geometry["height"])
    input_x = int(client_width * 0.65)
    input_y = client_height - 96
    send_x = client_width - 62
    send_y = client_height - 44
    if input_y < client_height * 0.80 or send_y < client_height * 0.82:
        return {
            "ok": False,
            "reason": "send_points_outside_input_area",
            "geometry": geometry,
            "error": "Calculated send points are outside the expected input area.",
        }
    if input_x <= session_split_x(client_width) or send_x <= session_split_x(client_width):
        return {
            "ok": False,
            "reason": "send_points_inside_session_list",
            "geometry": geometry,
            "error": "Calculated send points overlap the session list.",
        }
    input_candidates = input_click_candidate_points(geometry, min_points=10)
    send_candidates = send_click_candidate_points(geometry, min_points=10)
    return {
        "ok": True,
        "input_point": [input_x, input_y],
        "send_point": [send_x, send_y],
        "input_candidate_points": [list(point) for point in input_candidates],
        "send_candidate_points": [list(point) for point in send_candidates],
        "geometry": geometry,
    }


def _spread_points_in_rect(
    left: int,
    top: int,
    right: int,
    bottom: int,
    *,
    min_points: int = 10,
) -> list[tuple[int, int]]:
    if right <= left or bottom <= top:
        return []
    x_fracs = (0.12, 0.24, 0.38, 0.52, 0.66, 0.80, 0.90, 0.30, 0.46, 0.72)
    y_fracs = (0.22, 0.48, 0.74, 0.34, 0.62, 0.82, 0.42, 0.68, 0.18, 0.56)
    points: list[tuple[int, int]] = []
    for x_frac, y_frac in zip(x_fracs, y_fracs):
        point = (
            bounded_int(int(left + (right - left) * x_frac), default=left, minimum=left, maximum=right),
            bounded_int(int(top + (bottom - top) * y_frac), default=top, minimum=top, maximum=bottom),
        )
        if point not in points:
            points.append(point)
    while len(points) < max(1, int(min_points or 1)):
        point = (random.randint(left, right), random.randint(top, bottom))
        if point not in points:
            points.append(point)
    random.shuffle(points)
    return points


def input_click_candidate_points(geometry: dict[str, Any], *, min_points: int = 10) -> list[tuple[int, int]]:
    width = int(geometry.get("width") or 0)
    height = int(geometry.get("height") or 0)
    if width <= 0 or height <= 0:
        return []
    split_x = session_split_x(width)
    left = max(split_x + 64, int(width * 0.55) + 1)
    right = min(width - 96, max(left + 120, int(width * 0.88)))
    top = max(int(height * 0.84), height - 126)
    bottom = min(height - 76, max(top + 30, height - 86))
    return _spread_points_in_rect(left, top, right, bottom, min_points=min_points)


def send_click_candidate_points(geometry: dict[str, Any], *, min_points: int = 10) -> list[tuple[int, int]]:
    width = int(geometry.get("width") or 0)
    height = int(geometry.get("height") or 0)
    if width <= 0 or height <= 0:
        return []
    split_x = session_split_x(width)
    left = max(split_x + 80, width - 132)
    right = max(left + 24, width - 24)
    top = max(int(height * 0.80), height - 88)
    bottom = max(top + 18, height - 18)
    return _spread_points_in_rect(left, top, right, bottom, min_points=min_points)
