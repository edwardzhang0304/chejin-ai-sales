"""Pure window action planners for the Windows WeChat Win32/OCR adapter."""

from __future__ import annotations

from typing import Any

from apps.wechat_ai_customer_service.adapters.wechat_win32_ocr.geometry import bounded_int


ENSURE_VISIBLE_ACTION_RETURN = "return_probe"
ENSURE_VISIBLE_ACTION_FOCUS = "focus_visible"
ENSURE_VISIBLE_ACTION_RESTORE = "restore_then_focus"
ENSURE_VISIBLE_ACTION_MANUAL_TRAY = "manual_open_tray"
WINDOW_SELECTION_EMPTY_SCORE = (-1, -1, -1, -1, -1)


def recommended_window_scale_for_screen(screen_width: int, screen_height: int, *, screen_metrics_available: bool) -> float:
    if not screen_metrics_available:
        return 1.0
    width = max(0, int(screen_width or 0))
    height = max(0, int(screen_height or 0))
    if width >= 3200 and height >= 1800:
        return 1.5
    if width >= 2400 and height >= 1350:
        return 1.25
    return 1.0


def _geometry_int(geometry: dict[str, Any], key: str) -> int:
    try:
        return int(geometry.get(key) or 0)
    except (TypeError, ValueError):
        return 0


def plan_normalize_wechat_window(
    before: dict[str, Any],
    *,
    enabled: bool,
    dpi_scale: float,
    requested_width: Any,
    requested_height: Any,
    requested_left: Any,
    requested_top: Any,
    enforce_recommended: bool,
    fixed_origin: bool,
    screen_width: int,
    screen_height: int,
    screen_metrics_available: bool,
    default_width: int,
    default_height: int,
    min_width: int,
    min_height: int,
    max_width: int,
    max_height: int,
) -> dict[str, Any]:
    before_geometry = dict(before or {})
    if not enabled:
        return {"ok": True, "enabled": False, "applied": False, "before": before_geometry}

    safe_max_width = max(1, int(max_width or 1))
    safe_max_height = max(1, int(max_height or 1))
    try:
        normalized_dpi_scale = max(1.0, float(dpi_scale or 1.0))
    except (TypeError, ValueError):
        normalized_dpi_scale = 1.0
    resolution_scale = recommended_window_scale_for_screen(
        screen_width,
        screen_height,
        screen_metrics_available=screen_metrics_available,
    )
    scaled_default_width = min(safe_max_width, max(1, int(round(default_width * resolution_scale))))
    scaled_default_height = min(safe_max_height, max(1, int(round(default_height * resolution_scale))))
    base_min_width = min(safe_max_width, max(1, int(min_width or 1)))
    base_min_height = min(safe_max_height, max(1, int(min_height or 1)))
    target_width = bounded_int(requested_width, default=scaled_default_width, minimum=base_min_width, maximum=safe_max_width)
    target_height = bounded_int(requested_height, default=scaled_default_height, minimum=base_min_height, maximum=safe_max_height)
    requested_target = {"width": target_width, "height": target_height}
    recommended_floor_applied = False
    if enforce_recommended:
        if target_width < scaled_default_width:
            target_width = scaled_default_width
            recommended_floor_applied = True
        if target_height < scaled_default_height:
            target_height = scaled_default_height
            recommended_floor_applied = True
    effective_target = {"width": target_width, "height": target_height}

    safe_screen_width = int(screen_width or 0) if screen_metrics_available else 0
    safe_screen_height = int(screen_height or 0) if screen_metrics_available else 0
    if screen_metrics_available:
        screen_width_limit = max(1, safe_screen_width - 12) if safe_screen_width > 0 else 0
        screen_height_limit = max(1, safe_screen_height - 48) if safe_screen_height > 0 else 0
        safe_width = min(target_width, max(640, screen_width_limit))
        safe_height = min(target_height, max(640, screen_height_limit))
        if 0 < safe_screen_width < safe_width:
            safe_width = safe_screen_width
        if 0 < safe_screen_height < safe_height:
            safe_height = safe_screen_height
        if fixed_origin:
            left = bounded_int(
                requested_left,
                default=0,
                minimum=0,
                maximum=max(0, safe_screen_width - safe_width),
            )
            top = bounded_int(
                requested_top,
                default=0,
                minimum=0,
                maximum=max(0, safe_screen_height - safe_height),
            )
        else:
            left = min(max(0, _geometry_int(before_geometry, "left")), max(0, safe_screen_width - safe_width))
            top = min(max(0, _geometry_int(before_geometry, "top")), max(0, safe_screen_height - safe_height))
    else:
        safe_width = target_width
        safe_height = target_height
        if fixed_origin:
            left = bounded_int(requested_left, default=0, minimum=0, maximum=max_width)
            top = bounded_int(requested_top, default=0, minimum=0, maximum=max_height)
        else:
            left = max(0, _geometry_int(before_geometry, "left"))
            top = max(0, _geometry_int(before_geometry, "top"))

    width_diff = abs(_geometry_int(before_geometry, "width") - safe_width)
    height_diff = abs(_geometry_int(before_geometry, "height") - safe_height)
    left_diff = abs(_geometry_int(before_geometry, "left") - left)
    top_diff = abs(_geometry_int(before_geometry, "top") - top)
    already_near_target = width_diff <= 6 and height_diff <= 6 and left_diff <= 4 and top_diff <= 4

    return {
        "ok": True,
        "enabled": True,
        "move": not already_near_target,
        "before": before_geometry,
        "target": effective_target,
        "requested_target": requested_target,
        "enforce_recommended": bool(enforce_recommended),
        "recommended_floor_applied": bool(recommended_floor_applied),
        "fixed_origin": bool(fixed_origin),
        "screen": {"width": safe_screen_width, "height": safe_screen_height},
        "dpi_scale": normalized_dpi_scale,
        "resolution_scale": resolution_scale,
        "left": int(left),
        "top": int(top),
        "width": int(safe_width),
        "height": int(safe_height),
        "reason": "needs_normalize" if not already_near_target else "already_near_target",
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
