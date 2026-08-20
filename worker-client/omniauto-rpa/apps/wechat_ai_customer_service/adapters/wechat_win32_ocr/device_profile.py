"""Device profile diagnostics for the Windows WeChat Win32/OCR adapter."""

from __future__ import annotations

import json
import os
from typing import Any


PROFILE_VERSION = "wechat_win32_ocr_profile.v1"
LEGACY_PROFILE_ENV = "WECHAT_WIN32_OCR_LEGACY_DEVICE_PROFILE"


def dynamic_layout_enabled() -> bool:
    """Return the single production compatibility switch.

    The safe path is enabled by default. Disabling it is only useful for a
    previously accepted device profile and never re-enables unverified fixed
    coordinates on an unknown machine.
    """
    value = str(os.getenv("WECHAT_WIN32_OCR_DYNAMIC_LAYOUT_ENABLED", "1") or "1").strip().lower()
    return value not in {"0", "false", "no", "off"}


def configured_legacy_profile() -> dict[str, Any] | None:
    raw = str(os.getenv(LEGACY_PROFILE_ENV, "") or "").strip()
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


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
    sidebar_bounds: list[int] | tuple[int, int, int, int] | None = None,
    wechat_version: str = "",
    window_structure: str = "",
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
        "sidebar_bounds": list(sidebar_bounds or []),
        "wechat_version": str(wechat_version or ""),
        "window_structure": str(window_structure or ""),
    }
    profile["monitor_count"] = len(profile["monitors"])
    for key, value in dict(errors or {}).items():
        if value:
            profile[str(key)] = value
    return profile


def legacy_profile_matches(current: dict[str, Any] | None, expected: dict[str, Any] | None) -> tuple[bool, list[str]]:
    """Require an exact, complete device profile for the legacy path."""
    current = current if isinstance(current, dict) else {}
    expected = expected if isinstance(expected, dict) else {}
    required = (
        "screen_size",
        "dpi_scale",
        "window_size",
        "window_position",
        "sidebar_bounds",
        "wechat_version",
        "window_structure",
    )

    def value(profile: dict[str, Any], key: str) -> Any:
        if key == "screen_size":
            screen = profile.get("screen") if isinstance(profile.get("screen"), dict) else {}
            return [int(screen.get("width") or 0), int(screen.get("height") or 0)]
        if key == "window_size":
            window = profile.get("window_rect") if isinstance(profile.get("window_rect"), dict) else {}
            return [int(window.get("width") or 0), int(window.get("height") or 0)]
        if key == "window_position":
            window = profile.get("window_rect") if isinstance(profile.get("window_rect"), dict) else {}
            return [int(window.get("left") or 0), int(window.get("top") or 0)]
        if key == "dpi_scale":
            return round(float(profile.get(key) or 0.0), 4)
        return profile.get(key)

    mismatches: list[str] = []
    for key in required:
        expected_value = value(expected, key)
        current_value = value(current, key)
        if expected_value in (None, "", [], [0, 0], 0, 0.0):
            mismatches.append(f"{key}_missing_in_expected_profile")
        elif current_value != expected_value:
            mismatches.append(f"{key}_mismatch")
    return not mismatches, mismatches


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
