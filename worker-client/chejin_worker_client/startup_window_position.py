from __future__ import annotations

from typing import Any


WECHAT_WINDOW_GAP = 12


def position_to_right_of_wechat(
    status_payload: dict[str, Any] | None,
    *,
    gap: int = WECHAT_WINDOW_GAP,
) -> tuple[int, int] | None:
    """Return a one-time Worker position from the existing WeChat probe."""

    payload = status_payload if isinstance(status_payload, dict) else {}
    if payload.get("ok") is not True:
        return None
    geometry = payload.get("geometry")
    if not isinstance(geometry, dict):
        return None
    try:
        right = int(geometry["right"])
        top = int(geometry["top"])
        width = int(geometry["width"])
        height = int(geometry["height"])
        normalized_gap = max(0, int(gap))
    except (KeyError, TypeError, ValueError):
        return None
    if width <= 0 or height <= 0:
        return None
    return right + normalized_gap, top
