"""Pure session targeting helpers for the Win32/OCR adapter."""

from __future__ import annotations

import random
from typing import Any

from apps.wechat_ai_customer_service.adapters.wechat_win32_ocr.geometry import bounded_int, session_split_x


def session_row_click_x(
    session: dict[str, Any],
    geometry: dict[str, Any],
    *,
    default_x: int,
) -> int:
    width = int(geometry.get("width") or 0)
    split_x = session_split_x(width)
    left = int(float(session.get("left") or 0))
    right = int(float(session.get("right") or 0))
    if right > left:
        text_center = int((left + right) / 2)
        preferred = max(text_center, left + 22)
    else:
        preferred = int(default_x)
    return bounded_int(preferred, default=default_x, minimum=170, maximum=max(210, split_x - 18))


def session_row_click_candidate_points(
    session: dict[str, Any],
    geometry: dict[str, Any],
    *,
    default_x: int,
    min_points: int = 10,
    random_module: Any = random,
) -> list[tuple[int, int]]:
    """Return a spread of safe points inside one sidebar session row."""
    width = int(geometry.get("width") or 0)
    height = int(geometry.get("height") or 0)
    split_x = session_split_x(width)
    center_y_raw = session.get("center_y")
    if center_y_raw is None:
        return []
    center_y = int(float(center_y_raw))
    text_left = int(float(session.get("left") or 0))
    text_right = int(float(session.get("right") or 0))
    if text_right > text_left:
        row_left = max(74, min(text_left - 56, split_x - 230))
        row_right = min(split_x - 52, max(text_right + 26, row_left + 132))
    else:
        base_x = session_row_click_x(session, geometry, default_x=default_x)
        row_left = max(74, base_x - 82)
        row_right = min(split_x - 52, base_x + 84)
    if row_right <= row_left:
        row_left = max(74, min(int(default_x) - 70, split_x - 180))
        row_right = min(split_x - 52, row_left + 140)
    top = bounded_int(center_y - 16, default=max(88, center_y - 16), minimum=88, maximum=max(88, height - 28))
    bottom = bounded_int(center_y + 18, default=center_y + 18, minimum=top + 8, maximum=max(top + 8, height - 18))
    x_fracs = (0.10, 0.20, 0.32, 0.44, 0.56, 0.68, 0.80, 0.90, 0.38, 0.74)
    y_fracs = (0.24, 0.50, 0.78, 0.34, 0.68, 0.42, 0.82, 0.58, 0.18, 0.72)
    points: list[tuple[int, int]] = []
    for x_frac, y_frac in zip(x_fracs, y_fracs):
        x = int(row_left + (row_right - row_left) * x_frac)
        y = int(top + (bottom - top) * y_frac)
        point = (
            bounded_int(x, default=int(default_x), minimum=row_left, maximum=row_right),
            bounded_int(y, default=center_y, minimum=top, maximum=bottom),
        )
        if point not in points:
            points.append(point)
    while len(points) < max(1, int(min_points or 1)):
        point = (random_module.randint(row_left, row_right), random_module.randint(top, bottom))
        if point not in points:
            points.append(point)
    random_module.shuffle(points)
    return points


def choose_session_row_click_point(
    session: dict[str, Any],
    geometry: dict[str, Any],
    *,
    default_x: int,
    random_module: Any = random,
) -> tuple[int, int, dict[str, Any]]:
    points = session_row_click_candidate_points(
        session,
        geometry,
        default_x=default_x,
        min_points=10,
        random_module=random_module,
    )
    if not points:
        fallback = (
            session_row_click_x(session, geometry, default_x=default_x),
            int(float(session.get("center_y") or 0)),
        )
        return fallback[0], fallback[1], {"candidate_count": 0, "candidate_index": -1, "candidates": [list(fallback)]}
    index = random_module.randrange(len(points))
    x, y = points[index]
    return x, y, {
        "candidate_count": len(points),
        "candidate_index": index,
        "candidates": [list(point) for point in points],
    }


def target_switch_validation_is_hard_stop(validation: dict[str, Any] | None) -> bool:
    if not isinstance(validation, dict):
        return False
    state = str(validation.get("state") or "")
    reason = str(validation.get("reason") or "")
    if state in {"blank_render_detected", "login_window_detected", "auxiliary_shell_window_detected"}:
        return True
    return reason in {"blank_render", "login_or_qr", "auxiliary_shell_window"}
