"""Pure window action planners for the Windows WeChat Win32/OCR adapter."""

from __future__ import annotations

from typing import Any

from apps.wechat_ai_customer_service.adapters.wechat_win32_ocr.geometry import bounded_int


ENSURE_VISIBLE_ACTION_RETURN = "return_probe"
ENSURE_VISIBLE_ACTION_FOCUS = "focus_visible"
ENSURE_VISIBLE_ACTION_RESTORE = "restore_then_focus"
ENSURE_VISIBLE_ACTION_MANUAL_TRAY = "manual_open_tray"
WINDOW_SELECTION_EMPTY_SCORE = (-1, -1, -1, -1, -1)


def _geometry_int(geometry: dict[str, Any], key: str) -> int:
    try:
        return int(geometry.get(key) or 0)
    except (TypeError, ValueError):
        return 0


def plan_normalize_wechat_window(
    before: dict[str, Any],
    *,
    dpi_scale: float,
    work_area: dict[str, Any],
    minimum_client_width: int = 700,
    minimum_client_height: int = 720,
) -> dict[str, Any]:
    """Plan the single v0.9.28 startup placement on the HWND monitor.

    The values are outer-window targets.  Actual client geometry is read and
    validated only after MoveWindow; the planner never guesses it from screen
    resolution or a primary-monitor profile.
    """

    before_geometry = dict(before or {})
    try:
        scale = max(0.5, float(dpi_scale or 1.0))
    except (TypeError, ValueError):
        scale = 1.0
    left = int(work_area.get("left") or 0)
    top = int(work_area.get("top") or 0)
    width = int(work_area.get("width") or 0)
    height = int(work_area.get("height") or 0)
    if width <= 0 or height <= 0:
        return {
            "ok": False,
            "move": False,
            "before": before_geometry,
            "dpi_scale": scale,
            "work_area": dict(work_area or {}),
            "reason": "current_monitor_work_area_unavailable",
        }

    if abs(scale - 1.0) <= 0.01:
        requested_width, requested_height, bucket = 800, 852, "100%"
    elif abs(scale - 1.25) <= 0.01:
        requested_width, requested_height, bucket = 1000, 1065, "125%"
    elif abs(scale - 1.5) <= 0.01:
        requested_width, requested_height, bucket = 1200, 1278, "150%"
    else:
        requested_width = int(round(800 * scale))
        requested_height = int(round(852 * scale))
        bucket = "scaled"

    margin = max(0, int(round(12 * scale)))
    available_width = max(0, width - (2 * margin))
    available_height = max(0, height - (2 * margin))
    fit_scale = min(
        1.0,
        available_width / max(1, requested_width),
        available_height / max(1, requested_height),
    )
    target_width = max(1, int(requested_width * fit_scale))
    target_height = max(1, int(requested_height * fit_scale))
    if target_width < int(minimum_client_width) or target_height < int(minimum_client_height):
        return {
            "ok": False,
            "move": False,
            "before": before_geometry,
            "dpi_scale": scale,
            "dpi_bucket": bucket,
            "work_area": dict(work_area or {}),
            "requested_target": {"width": requested_width, "height": requested_height},
            "target": {"width": target_width, "height": target_height},
            "reason": "work_area_too_small_for_minimum_client_surface",
        }

    target_left = left + margin
    target_top = top + margin
    target_right = target_left + target_width
    target_bottom = target_top + target_height
    safe_right = left + width - margin
    safe_bottom = top + height - margin
    if target_right > safe_right or target_bottom > safe_bottom:
        return {
            "ok": False,
            "move": False,
            "before": before_geometry,
            "dpi_scale": scale,
            "dpi_bucket": bucket,
            "work_area": dict(work_area or {}),
            "requested_target": {"width": requested_width, "height": requested_height},
            "target": {"width": target_width, "height": target_height},
            "reason": "startup_target_exceeds_safe_work_area",
        }
    near = (
        abs(_geometry_int(before_geometry, "left") - target_left) <= 4
        and abs(_geometry_int(before_geometry, "top") - target_top) <= 4
        and abs(_geometry_int(before_geometry, "width") - target_width) <= 6
        and abs(_geometry_int(before_geometry, "height") - target_height) <= 6
    )
    return {
        "ok": True,
        "move": not near,
        "before": before_geometry,
        "dpi_scale": scale,
        "dpi_bucket": bucket,
        "work_area": dict(work_area or {}),
        "requested_target": {"width": requested_width, "height": requested_height},
        "target": {"width": target_width, "height": target_height},
        "fit_scale": round(fit_scale, 6),
        "left": target_left,
        "top": target_top,
        "width": target_width,
        "height": target_height,
        "right": target_right,
        "bottom": target_bottom,
        "safe_right": safe_right,
        "safe_bottom": safe_bottom,
        "reason": "already_at_startup_profile" if near else "needs_startup_profile",
    }


def plan_ensure_visible_wechat_window(
    probe: dict[str, Any],
    *,
    interactive: bool,
    usable_visible: bool,
    tray_hidden: bool,
) -> dict[str, Any]:
    visible_main_windows = (probe or {}).get("visible_main_windows") or []
    has_visible_main_window = bool(visible_main_windows)
    if has_visible_main_window:
        if usable_visible and interactive:
            return {
                "action": ENSURE_VISIBLE_ACTION_FOCUS,
                "return_probe": False,
                "visible_main_window_geometry_invalid": False,
            }
        if not usable_visible:
            return {
                "action": ENSURE_VISIBLE_ACTION_RESTORE if interactive else ENSURE_VISIBLE_ACTION_RETURN,
                "return_probe": not interactive,
                "visible_main_window_geometry_invalid": True,
            }
        return {
            "action": ENSURE_VISIBLE_ACTION_RETURN,
            "return_probe": True,
            "visible_main_window_geometry_invalid": False,
        }
    if not interactive:
        return {
            "action": ENSURE_VISIBLE_ACTION_RETURN,
            "return_probe": True,
            "visible_main_window_geometry_invalid": False,
        }
    if tray_hidden:
        return {
            "action": ENSURE_VISIBLE_ACTION_MANUAL_TRAY,
            "return_probe": True,
            "visible_main_window_geometry_invalid": False,
            "probe_updates": {
                "main_window_in_tray": True,
                "manual_action_required": "open_wechat_main_window",
                "restore_skipped_reason": "manual_tray_restore_required",
            },
        }
    return {
        "action": ENSURE_VISIBLE_ACTION_RESTORE,
        "return_probe": False,
        "visible_main_window_geometry_invalid": False,
    }


def visible_window_candidate_score(
    geometry: dict[str, Any],
    *,
    capture_ready: bool,
    content_health_score: Any,
    min_send_width: int,
    min_send_height: int,
    title_score: int,
) -> tuple[int, int, int, int, int]:
    width = max(0, _geometry_int(geometry, "width"))
    height = max(0, _geometry_int(geometry, "height"))
    area = width * height
    try:
        parsed_content_score = int(content_health_score or 0)
    except (TypeError, ValueError):
        parsed_content_score = 0
    safe_action_size = 1 if width >= int(min_send_width) and height >= int(min_send_height) else 0
    capture_rank = 0 if parsed_content_score < 0 else (1 if capture_ready else 0)
    return (
        capture_rank,
        parsed_content_score,
        safe_action_size,
        area,
        int(title_score or 0),
    )


def select_best_visible_window_candidate(candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
    selected: dict[str, Any] | None = None
    selected_score = WINDOW_SELECTION_EMPTY_SCORE
    for candidate in candidates:
        item = candidate.get("item") if isinstance(candidate.get("item"), dict) else {}
        if not item:
            continue
        score = tuple(candidate.get("score") or WINDOW_SELECTION_EMPTY_SCORE)
        if selected is None or score > selected_score:
            selected = {
                **dict(item),
                "geometry_hint": dict(candidate.get("geometry") or {}),
                "content_health_score": int(candidate.get("content_health_score") or 0),
            }
            selected_score = score  # type: ignore[assignment]
    return selected
