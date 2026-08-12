from __future__ import annotations

from typing import Any


WECHAT_WINDOW_GAP = 12


def position_to_right_of_wechat(
    status_payload: dict[str, Any] | None,
    *,
    window_size: tuple[int, int],
    screen_bounds: tuple[int, int, int, int],
    gap: int = WECHAT_WINDOW_GAP,
) -> tuple[int, int] | None:
    """Place Worker beside WeChat while keeping it inside one display."""

    payload = status_payload if isinstance(status_payload, dict) else {}
    if payload.get("ok") is not True:
        return None
    geometry = payload.get("geometry")
    if not isinstance(geometry, dict):
        return None
    try:
        right = int(geometry["right"])
        left = int(geometry["left"])
        top = int(geometry["top"])
        width = int(geometry["width"])
        height = int(geometry["height"])
        window_width = int(window_size[0])
        window_height = int(window_size[1])
        screen_left, screen_top, screen_right, screen_bottom = (
            int(value) for value in screen_bounds
        )
        normalized_gap = max(0, int(gap))
    except (IndexError, KeyError, TypeError, ValueError):
        return None
    if (
        width <= 0
        or height <= 0
        or window_width <= 0
        or window_height <= 0
        or screen_right <= screen_left
        or screen_bottom <= screen_top
    ):
        return None

    preferred_right = right + normalized_gap
    preferred_left = left - normalized_gap - window_width
    maximum_x = max(screen_left, screen_right - window_width)
    maximum_y = max(screen_top, screen_bottom - window_height)
    if preferred_right + window_width <= screen_right:
        x = preferred_right
    elif preferred_left >= screen_left:
        x = preferred_left
    else:
        x = min(max(preferred_right, screen_left), maximum_x)
    x = min(max(x, screen_left), maximum_x)
    y = min(max(top, screen_top), maximum_y)
    return x, y
