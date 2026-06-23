"""Pure send-rate and UI action risk helpers for the Win32/OCR adapter."""

from __future__ import annotations

from typing import Any, Callable


DEFAULT_SEND_MIN_INTERVAL_SECONDS = 30
DEFAULT_SEND_BURST_WINDOW_SECONDS = 600
DEFAULT_SEND_BURST_LIMIT = 5
DEFAULT_UI_ACTION_KEYBOARD_MIN_GAP_MS = 34
DEFAULT_UI_ACTION_MOUSE_MIN_GAP_MS = 110
DEFAULT_UI_ACTION_SCROLL_MIN_GAP_MS = 140
DEFAULT_UI_ACTION_FOCUS_MIN_GAP_MS = 180
DEFAULT_UI_ACTION_KIND_SWITCH_GAP_MS = 170
DEFAULT_UI_ACTION_NEAR_POINT_RADIUS_PX = 7
DEFAULT_UI_ACTION_NEAR_POINT_GAP_MS = 720
DEFAULT_UI_ACTION_NEAR_POINT_SOFT_LIMIT = 2


def send_rate_decision(
    state: dict[str, Any],
    *,
    target: str,
    now_ts: float,
    min_interval_seconds: int,
    burst_window_seconds: int,
    burst_limit: int,
) -> dict[str, Any]:
    events = [item for item in state.get("events", []) if isinstance(item, dict)]
    target_events = [item for item in events if str(item.get("target") or "") == target]
    if min_interval_seconds > 0 and target_events:
        last_at = max(float(item.get("at") or 0) for item in target_events)
        elapsed = now_ts - last_at
        if elapsed < min_interval_seconds:
            return {
                "ok": False,
                "reason": "min_interval_not_elapsed",
                "min_interval_seconds": min_interval_seconds,
                "wait_seconds": round(min_interval_seconds - elapsed, 2),
                "error": "win32_ocr fallback send is too frequent for this target.",
            }
    if burst_window_seconds > 0 and burst_limit > 0:
        recent = [
            item
            for item in target_events
            if now_ts - float(item.get("at") or 0) <= burst_window_seconds
        ]
        if len(recent) >= burst_limit:
            oldest = min(float(item.get("at") or 0) for item in recent)
            return {
                "ok": False,
                "reason": "burst_limit_reached",
                "burst_limit": burst_limit,
                "burst_window_seconds": burst_window_seconds,
                "wait_seconds": round(max(0.0, burst_window_seconds - (now_ts - oldest)), 2),
                "error": "win32_ocr fallback send burst limit reached.",
            }
    return {
        "ok": True,
        "reason": "rate_ok",
        "min_interval_seconds": min_interval_seconds,
        "burst_window_seconds": burst_window_seconds,
        "burst_limit": burst_limit,
    }


def ui_action_kind(action: str) -> str:
    name = str(action or "").strip().lower()
    if name in {"key_press", "hotkey", "sendinput_unicode_unit"} or name.startswith("keyboard_"):
        return "keyboard"
    if "scroll" in name or "wheel" in name:
        return "scroll"
    if "activate" in name or "focus" in name:
        return "focus"
    if "click" in name or "mouse" in name:
        return "mouse"
    return "other"


def ui_action_point(metadata: dict[str, Any] | None) -> tuple[int, int] | None:
    if not isinstance(metadata, dict):
        return None
    if isinstance(metadata.get("point"), list) and len(metadata["point"]) >= 2:
        try:
            return int(metadata["point"][0]), int(metadata["point"][1])
        except (TypeError, ValueError):
            return None
    jitter = metadata.get("jitter") if isinstance(metadata.get("jitter"), dict) else {}
    final = jitter.get("final") if isinstance(jitter.get("final"), list) else None
    if final and len(final) >= 2:
        try:
            return int(final[0]), int(final[1])
        except (TypeError, ValueError):
            return None
    if "x" in metadata and "y" in metadata:
        try:
            return int(metadata.get("x")), int(metadata.get("y"))
        except (TypeError, ValueError):
            return None
    return None


def ui_action_min_gap_ms(
    kind: str,
    *,
    keyboard_min_gap_ms: int = DEFAULT_UI_ACTION_KEYBOARD_MIN_GAP_MS,
    scroll_min_gap_ms: int = DEFAULT_UI_ACTION_SCROLL_MIN_GAP_MS,
    focus_min_gap_ms: int = DEFAULT_UI_ACTION_FOCUS_MIN_GAP_MS,
    mouse_min_gap_ms: int = DEFAULT_UI_ACTION_MOUSE_MIN_GAP_MS,
    default_min_gap_ms: int = 70,
) -> int:
    if kind == "keyboard":
        return max(0, int(keyboard_min_gap_ms))
    if kind == "scroll":
        return max(0, int(scroll_min_gap_ms))
    if kind == "focus":
        return max(0, int(focus_min_gap_ms))
    if kind == "mouse":
        return max(0, int(mouse_min_gap_ms))
    return max(0, int(default_min_gap_ms))


def count_recent_near_point_actions(
    events: list[dict[str, Any]],
    *,
    point: tuple[int, int],
    now_ts: float,
    radius: int,
    window_seconds: float,
) -> int:
    px, py = point
    count = 0
    cutoff = now_ts - max(0.1, float(window_seconds))
    for item in events:
        try:
            ts = float(item.get("ts") or 0.0)
        except (TypeError, ValueError):
            continue
        if ts < cutoff:
            continue
        meta = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
        candidate = ui_action_point(meta)
        if candidate is None:
            continue
        cx, cy = candidate
        if abs(cx - px) <= radius and abs(cy - py) <= radius:
            count += 1
    return count


def plan_rpa_action_pacing(
    action: str,
    *,
    metadata: dict[str, Any] | None,
    recent_events: list[dict[str, Any]],
    last_state: dict[str, Any],
    now_ts: float,
    min_gap_ms: int,
    kind_switch_gap_ms: int,
    near_point_radius_px: int,
    near_point_gap_ms: int,
    near_point_soft_limit: int,
    extra_delay_ms: Callable[[str], int] | None = None,
) -> dict[str, Any]:
    kind = ui_action_kind(action)
    point = ui_action_point(metadata)
    delay_ms = 0
    reasons: list[str] = []
    extra = extra_delay_ms or (lambda _reason: 0)
    last_ts = float(last_state.get("ts") or 0.0)
    last_kind = str(last_state.get("kind") or "")
    if last_ts > 0:
        elapsed_ms = max(0.0, (now_ts - last_ts) * 1000.0)
        effective_min_gap = int(min_gap_ms)
        if kind != last_kind and {kind, last_kind} & {"mouse", "keyboard", "scroll"}:
            effective_min_gap = max(effective_min_gap, int(kind_switch_gap_ms))
            reasons.append(f"kind_switch:{last_kind}->{kind}")
        if elapsed_ms < effective_min_gap:
            delay_ms = max(delay_ms, int(effective_min_gap - elapsed_ms) + int(extra("min_gap")))
            if not reasons:
                reasons.append(f"{kind}_min_gap")
    if kind in {"mouse", "scroll"} and point is not None:
        radius = max(0, int(near_point_radius_px))
        gap_ms = max(0, int(near_point_gap_ms))
        soft_limit = max(1, int(near_point_soft_limit))
        near_count = count_recent_near_point_actions(
            recent_events,
            point=point,
            now_ts=now_ts,
            radius=radius,
            window_seconds=max(1.0, gap_ms / 1000.0 * 3.0),
        )
        last_point = last_state.get("point")
        if (
            isinstance(last_point, list)
            and len(last_point) >= 2
            and abs(int(last_point[0]) - point[0]) <= radius
            and abs(int(last_point[1]) - point[1]) <= radius
        ):
            delay_ms = max(delay_ms, gap_ms + int(extra("near_point_repeat")))
            reasons.append("near_point_repeat")
        if near_count >= soft_limit:
            delay_ms = max(delay_ms, gap_ms + int(extra("near_point_soft_limit")))
            reasons.append(f"near_point_soft_limit:{near_count}")
    return {
        "enabled": True,
        "kind": kind,
        "delay_ms": delay_ms,
        "reasons": reasons,
        "point": list(point) if point is not None else None,
    }
