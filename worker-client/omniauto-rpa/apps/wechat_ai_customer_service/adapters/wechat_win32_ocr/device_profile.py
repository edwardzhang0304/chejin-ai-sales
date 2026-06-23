"""Device profile diagnostics for the Windows WeChat Win32/OCR adapter."""

from __future__ import annotations

from typing import Any


PROFILE_VERSION = "wechat_win32_ocr_profile.v1"


def build_device_profile(
    *,
    route: str = "",
    geometry: dict[str, Any] | None = None,
    screenshot_size: tuple[int, int] | list[int] | None = None,
    client_rect: dict[str, Any] | None = None,
    dpi_scale: float = 1.0,
    screen: dict[str, Any] | None = None,
    virtual_screen: dict[str, Any] | None = None,
    monitors: list[dict[str, Any]] | None = None,
    errors: dict[str, Any] | None = None,
) -> dict[str, Any]:
    profile: dict[str, Any] = {
        "platform": "windows",
        "route": str(route or ""),
        "window_rect": dict(geometry or {}),
        "client_rect": dict(client_rect or {}),
        "screenshot_size": list(screenshot_size) if screenshot_size else [],
        "dpi_scale": round(float(dpi_scale), 4),
        "dpi": int(round(float(dpi_scale) * 96)),
        "screen": dict(screen or {}),
        "virtual_screen": dict(virtual_screen or {}),
        "monitors": list(monitors or []),
    }
    profile["monitor_count"] = len(profile["monitors"])
    for key, value in dict(errors or {}).items():
        if value:
            profile[str(key)] = value
    return profile


def profile_summary(profile: dict[str, Any]) -> dict[str, Any]:
    window = profile.get("window_rect") if isinstance(profile.get("window_rect"), dict) else {}
    client = profile.get("client_rect") if isinstance(profile.get("client_rect"), dict) else {}
    screen = profile.get("screen") if isinstance(profile.get("screen"), dict) else {}
    return {
        "platform": profile.get("platform") or "windows",
        "route": profile.get("route") or "",
        "window_size": [
            int(window.get("width") or 0),
            int(window.get("height") or 0),
        ],
        "client_size": [
            int(client.get("width") or 0),
            int(client.get("height") or 0),
        ],
        "screenshot_size": list(profile.get("screenshot_size") or []),
        "screen_size": [
            int(screen.get("width") or 0),
            int(screen.get("height") or 0),
        ],
        "dpi_scale": profile.get("dpi_scale", 1.0),
        "monitor_count": int(profile.get("monitor_count") or 0),
        "profile_version": PROFILE_VERSION,
    }


def profile_changed(old: dict[str, Any] | None, new: dict[str, Any] | None) -> bool:
    if not old and not new:
        return False
    if not old or not new:
        return True
    return profile_summary(old) != profile_summary(new)
