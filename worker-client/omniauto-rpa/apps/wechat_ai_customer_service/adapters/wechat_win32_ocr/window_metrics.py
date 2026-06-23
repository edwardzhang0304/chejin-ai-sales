"""Read-only window metric helpers for the Windows WeChat Win32/OCR adapter."""

from __future__ import annotations

import ctypes
from typing import Any


def get_window_geometry(hwnd: int, *, win32gui_module: Any) -> dict[str, int]:
    left, top, right, bottom = win32gui_module.GetWindowRect(hwnd)
    return {
        "left": int(left),
        "top": int(top),
        "right": int(right),
        "bottom": int(bottom),
        "width": int(right - left),
        "height": int(bottom - top),
    }


def get_window_client_geometry(hwnd: int, *, win32gui_module: Any) -> dict[str, int]:
    try:
        left, top, right, bottom = win32gui_module.GetClientRect(hwnd)
        screen_left, screen_top = win32gui_module.ClientToScreen(hwnd, (left, top))
        screen_right, screen_bottom = win32gui_module.ClientToScreen(hwnd, (right, bottom))
        return {
            "left": int(left),
            "top": int(top),
            "right": int(right),
            "bottom": int(bottom),
            "width": int(right - left),
            "height": int(bottom - top),
            "screen_left": int(screen_left),
            "screen_top": int(screen_top),
            "screen_right": int(screen_right),
            "screen_bottom": int(screen_bottom),
        }
    except Exception as exc:
        return {"error": repr(exc)}


def window_dpi_scale(hwnd: int, *, user32: Any | None = None, windll: Any | None = None) -> float:
    try:
        user32_api = user32
        if user32_api is None:
            windll_api = windll if windll is not None else ctypes.windll
            user32_api = windll_api.user32
        dpi = int(user32_api.GetDpiForWindow(hwnd))
        if dpi > 0:
            return max(1.0, float(dpi) / 96.0)
    except Exception:
        pass
    return 1.0
