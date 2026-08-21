"""Pure session targeting helpers for the Win32/OCR adapter."""

from __future__ import annotations

import random
from typing import Any

from apps.wechat_ai_customer_service.adapters.wechat_win32_ocr.geometry import bounded_int


def session_row_click_x(
    session: dict[str, Any],
    geometry: dict[str, Any],
    *,
    default_x: int,
) -> int:
    reference_point = session.get("reference_click_point")
    if isinstance(reference_point, (list, tuple)) and len(reference_point) >= 2:
        click_bounds = session.get("click_bounds")
        if isinstance(click_bounds, (list, tuple)) and len(click_bounds) >= 4:
            left, _top, right, _bottom = [int(float(value or 0)) for value in click_bounds[:4]]
            reference_x = int(float(reference_point[0] or 0))
            if left <= reference_x <= right:
                return reference_x
    click_bounds = session.get("click_bounds")
    if isinstance(click_bounds, (list, tuple)) and len(click_bounds) >= 4:
        left, _top, right, _bottom = [int(float(value or 0)) for value in click_bounds[:4]]
        if right > left:
            return int(left + (right - left) * 0.55)
    left = int(float(session.get("left") or 0))
    right = int(float(session.get("right") or 0))
    if right > left:
        text_center = int((left + right) / 2)
        preferred = max(text_center, left + 22)
    else:
        preferred = int(default_x)
    if right > left:
        return bounded_int(preferred, default=default_x, minimum=left, maximum=right)
    width = int(geometry.get("width") or 0)
    return bounded_int(preferred, default=default_x, minimum=0, maximum=max(0, width - 1))


def session_row_click_candidate_points(
    session: dict[str, Any],
    geometry: dict[str, Any],
    *,
    default_x: int,
    min_points: int = 10,
    random_module: Any = random,
) -> list[tuple[int, int]]:
    """Return a spread of safe points inside one sidebar session row."""
    height = int(geometry.get("height") or 0)
    center_y_raw = session.get("center_y")
    if center_y_raw is None:
        return []
    center_y = int(float(center_y_raw))
    click_bounds = session.get("click_bounds")
    if not isinstance(click_bounds, (list, tuple)) or len(click_bounds) < 4:
        return []
    row_left, top, row_right, bottom = [int(float(value or 0)) for value in click_bounds[:4]]
    if row_right <= row_left or bottom <= top:
        return []
    reference_point = session.get("reference_click_point")
    if isinstance(reference_point, (list, tuple)) and len(reference_point) >= 2:
        reference_x = int(float(reference_point[0] or 0))
        if row_left <= reference_x <= row_right:
            # The startup map owns X; the current business frame owns row Y.
            return [(reference_x, bounded_int(center_y, default=center_y, minimum=top, maximum=bottom))]
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
        return 0, 0, {"candidate_count": 0, "candidate_index": -1, "candidates": []}
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
    if state in {
        "blank_render_detected",
        "login_window_detected",
        "auxiliary_shell_window_detected",
        "wrong_target_service_container_detected",
    }:
        return True
    return reason in {"blank_render", "login_or_qr", "auxiliary_shell_window", "service_container_wrong_target"}
