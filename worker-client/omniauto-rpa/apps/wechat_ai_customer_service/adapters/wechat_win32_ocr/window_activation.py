"""Window activation execution helpers for the Windows WeChat Win32/OCR adapter."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from apps.wechat_ai_customer_service.adapters.wechat_win32_ocr import window_action_state


@dataclass(frozen=True)
class ActivateWindowDependencies:
    user32: Any
    win32gui: Any
    win32process: Any
    win32api: Any
    win32con: Any
    foreground_window_matches_target: Callable[[int], dict[str, Any]]
    require_active_ui_action_budget: Callable[..., dict[str, Any]]
    humanized_action_sleep: Callable[[int, int | None], float]
    coordinate_rpa_action: Callable[..., dict[str, Any]]
    focus_click_fallback_enabled: Callable[[], bool]
    click: Callable[[int, int], None]
    monotonic: Callable[[], float]


def activate_window_with_dependencies(
    hwnd: int,
    *,
    settings: dict[str, Any],
    last_activate_monotonic_by_hwnd: dict[int, float],
    deps: ActivateWindowDependencies,
) -> None:
    if not hwnd:
        return
    aggressive_focus = bool(settings.get("aggressive_focus"))
    attach_thread_input = bool(settings.get("attach_thread_input"))
    user32 = deps.user32
    try:
        if int(user32.IsIconic(hwnd)):
            user32.ShowWindow(hwnd, 9)
        elif not bool(user32.IsWindowVisible(hwnd)):
            user32.ShowWindow(hwnd, 5)
    except Exception:
        pass
    try:
        focus_match = deps.foreground_window_matches_target(hwnd)
        if window_action_state.foreground_guard_ready(focus_match):
            return
    except Exception:
        pass
    debounce_seconds = float(settings.get("debounce_seconds") or 0.0)
    if debounce_seconds > 0:
        now_monotonic = deps.monotonic()
        last_monotonic = float(last_activate_monotonic_by_hwnd.get(int(hwnd)) or 0.0)
        if window_action_state.activate_debounce_active(
            now_monotonic=now_monotonic,
            last_monotonic=last_monotonic,
            debounce_seconds=debounce_seconds,
        ):
            try:
                if bool(user32.IsWindow(hwnd)) and bool(user32.IsWindowVisible(hwnd)) and not bool(user32.IsIconic(hwnd)):
                    focus_match = deps.foreground_window_matches_target(hwnd)
                    if window_action_state.foreground_guard_ready(focus_match):
                        return
            except Exception:
                pass
    deps.require_active_ui_action_budget("activate_window", metadata={"hwnd": int(hwnd or 0)})
    if aggressive_focus:
        try:
            user32.BringWindowToTop(hwnd)
        except Exception:
            pass
    try:
        user32.SetForegroundWindow(hwnd)
    except Exception:
        pass
    fg_tid = 0
    target_tid = 0
    current_tid = 0
    attached_fg = False
    attached_current = False
    try:
        fg_hwnd = deps.win32gui.GetForegroundWindow()
        fg_tid = deps.win32process.GetWindowThreadProcessId(fg_hwnd)[0] if fg_hwnd else 0
        target_tid = deps.win32process.GetWindowThreadProcessId(hwnd)[0]
        current_tid = deps.win32api.GetCurrentThreadId()
        if attach_thread_input and fg_tid and target_tid and fg_tid != target_tid:
            deps.win32process.AttachThreadInput(fg_tid, target_tid, True)
            attached_fg = True
        if attach_thread_input and current_tid and target_tid and current_tid != target_tid:
            deps.win32process.AttachThreadInput(current_tid, target_tid, True)
            attached_current = True
        deps.win32gui.SetForegroundWindow(hwnd)
        deps.win32gui.SetActiveWindow(hwnd)
        if aggressive_focus:
            deps.win32gui.SetFocus(hwnd)
        if aggressive_focus:
            try:
                flags = deps.win32con.SWP_NOMOVE | deps.win32con.SWP_NOSIZE | deps.win32con.SWP_SHOWWINDOW
                deps.win32gui.SetWindowPos(hwnd, deps.win32con.HWND_TOPMOST, 0, 0, 0, 0, flags)
                deps.win32gui.SetWindowPos(hwnd, deps.win32con.HWND_NOTOPMOST, 0, 0, 0, 0, flags)
            except Exception:
                pass
        if aggressive_focus:
            deps.humanized_action_sleep(70, 125)
        else:
            deps.humanized_action_sleep(55, 95)
    except Exception:
        pass
    finally:
        last_activate_monotonic_by_hwnd[int(hwnd)] = deps.monotonic()
        if attached_fg:
            try:
                deps.win32process.AttachThreadInput(fg_tid, target_tid, False)
            except Exception:
                pass
        if attached_current:
            try:
                deps.win32process.AttachThreadInput(current_tid, target_tid, False)
            except Exception:
                pass
    if aggressive_focus:
        try:
            final_match = deps.foreground_window_matches_target(hwnd)
        except Exception:
            final_match = {}
        if not window_action_state.foreground_guard_ready(final_match):
            try:
                deps.coordinate_rpa_action(
                    "key_press",
                    metadata={"key": int(deps.win32con.VK_MENU), "context": "focus_alt_down"},
                )
                deps.win32api.keybd_event(deps.win32con.VK_MENU, 0, 0, 0)
                deps.humanized_action_sleep(25, 55)
                user32.SetForegroundWindow(hwnd)
                deps.win32gui.SetForegroundWindow(hwnd)
                deps.win32gui.SetActiveWindow(hwnd)
            except Exception:
                pass
            finally:
                try:
                    deps.coordinate_rpa_action(
                        "key_press",
                        metadata={"key": int(deps.win32con.VK_MENU), "context": "focus_alt_up"},
                    )
                    deps.win32api.keybd_event(deps.win32con.VK_MENU, 0, deps.win32con.KEYEVENTF_KEYUP, 0)
                except Exception:
                    pass
            deps.humanized_action_sleep(60, 120)
            try:
                final_match = deps.foreground_window_matches_target(hwnd)
            except Exception:
                final_match = {}
            if not window_action_state.foreground_guard_ready(final_match) and deps.focus_click_fallback_enabled():
                try:
                    left, top, right, _bottom = deps.win32gui.GetWindowRect(hwnd)
                    width = max(120, int(right - left))
                    title_x = int(left + min(max(88, width // 3), max(88, width - 88)))
                    title_y = int(top + 18)
                    deps.click(title_x, title_y)
                    deps.humanized_action_sleep(65, 130)
                except Exception:
                    pass
