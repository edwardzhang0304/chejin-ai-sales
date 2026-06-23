"""Window visibility orchestration helpers for the Windows WeChat Win32/OCR adapter."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from apps.wechat_ai_customer_service.adapters.wechat_win32_ocr import window_action_planning


@dataclass(frozen=True)
class EnsureVisibleDependencies:
    probe_wechat_windows: Callable[[], dict[str, Any]]
    focus_wechat_window: Callable[[dict[str, Any]], dict[str, Any] | None]
    restore_wechat_window: Callable[[dict[str, Any]], dict[str, Any] | None]
    humanized_action_sleep: Callable[[int, int | None], float]


def ensure_visible_wechat_window_with_dependencies(
    probe: dict[str, Any],
    *,
    plan: dict[str, Any],
    deps: EnsureVisibleDependencies,
) -> dict[str, Any]:
    action = str(plan.get("action") or "")
    if probe.get("visible_main_windows"):
        if bool(plan.get("visible_main_window_geometry_invalid")):
            probe["visible_main_window_geometry_invalid"] = True
        if action == window_action_planning.ENSURE_VISIBLE_ACTION_FOCUS:
            focused = deps.focus_wechat_window(probe)
            if focused:
                deps.humanized_action_sleep(150, 280)
                probe = deps.probe_wechat_windows()
                probe["focused_window"] = focused
        elif action == window_action_planning.ENSURE_VISIBLE_ACTION_RESTORE:
            restored = deps.restore_wechat_window(probe)
            if restored:
                deps.humanized_action_sleep(650, 980)
                probe = deps.probe_wechat_windows()
                probe["restored_window"] = restored
                focused = deps.focus_wechat_window(probe)
                if focused:
                    deps.humanized_action_sleep(150, 280)
                    probe = deps.probe_wechat_windows()
                    probe["focused_window"] = focused
        return probe
    if action == window_action_planning.ENSURE_VISIBLE_ACTION_RETURN:
        return probe
    if action == window_action_planning.ENSURE_VISIBLE_ACTION_MANUAL_TRAY:
        probe.update(dict(plan.get("probe_updates") or {}))
        return probe
    restored = deps.restore_wechat_window(probe)
    if restored:
        deps.humanized_action_sleep(650, 980)
        probe = deps.probe_wechat_windows()
        probe["restored_window"] = restored
        focused = deps.focus_wechat_window(probe)
        if focused:
            deps.humanized_action_sleep(150, 280)
            probe = deps.probe_wechat_windows()
            probe["focused_window"] = focused
    return probe
