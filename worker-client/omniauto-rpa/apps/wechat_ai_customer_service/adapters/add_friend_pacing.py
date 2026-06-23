"""Pacing tiers for add_friend RPA."""

from __future__ import annotations

import os
from typing import Any


DEFAULT_ADD_FRIEND_PACING_PROFILE = "balanced"


DEFAULT_ADD_FRIEND_PACING_TIERS: dict[str, tuple[int, int]] = {
    "critical_click": (1000, 2000),
    "input": (180, 520),
    "verify": (350, 900),
    "report": (0, 0),
    "default": (1000, 2000),
}


def pacing_range(tier: str, *, default_tier: str = "default") -> tuple[int, int]:
    clean_tier = normalize_pacing_tier(tier, default_tier=default_tier)
    low, high = DEFAULT_ADD_FRIEND_PACING_TIERS[clean_tier]
    env_prefix = f"WECHAT_WIN32_OCR_ADD_FRIEND_PACE_{clean_tier.upper()}"
    low = bounded_int(os.getenv(f"{env_prefix}_MIN_MS"), default=low, minimum=0, maximum=10000)
    high = bounded_int(os.getenv(f"{env_prefix}_MAX_MS"), default=high, minimum=0, maximum=15000)
    return min(low, high), max(low, high)


def normalize_pacing_tier(tier: str, *, default_tier: str = "default") -> str:
    clean = str(tier or "").strip().lower()
    if clean in DEFAULT_ADD_FRIEND_PACING_TIERS:
        return clean
    fallback = str(default_tier or "default").strip().lower()
    return fallback if fallback in DEFAULT_ADD_FRIEND_PACING_TIERS else "default"


def pacing_metadata(tier: str, *, reason: str = "") -> dict[str, Any]:
    clean_tier = normalize_pacing_tier(tier)
    low, high = pacing_range(clean_tier)
    return {
        "tier": clean_tier,
        "reason": str(reason or ""),
        "min_ms": low,
        "max_ms": high,
        "profile": str(os.getenv("WECHAT_WIN32_OCR_ADD_FRIEND_PACE_PROFILE") or DEFAULT_ADD_FRIEND_PACING_PROFILE),
    }


def bounded_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))
