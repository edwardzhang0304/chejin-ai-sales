"""Pure humanized-input helpers for the Windows WeChat Win32/OCR adapter."""

from __future__ import annotations

import os
import random
import re
from typing import Any

from apps.wechat_ai_customer_service.adapters.wechat_win32_ocr.env_config import (
    DEFAULT_HUMANIZED_INPUT_METHOD,
    env_flag,
    env_float,
    normalize_humanized_input_method,
)
from apps.wechat_ai_customer_service.adapters.wechat_win32_ocr.geometry import bounded_int


DEFAULT_HUMANIZED_INPUT_ENABLED = True
DEFAULT_HUMANIZED_TYPING_CHUNK_MIN_CHARS = 2
DEFAULT_HUMANIZED_TYPING_CHUNK_MAX_CHARS = 6
DEFAULT_HUMANIZED_TYPING_CHAR_DELAY_MIN_MS = 50
DEFAULT_HUMANIZED_TYPING_CHAR_DELAY_MAX_MS = 180
DEFAULT_HUMANIZED_TYPING_MICRO_PAUSE_EVERY_CHARS = 18
DEFAULT_HUMANIZED_TYPING_MICRO_PAUSE_MIN_MS = 220
DEFAULT_HUMANIZED_TYPING_MICRO_PAUSE_MAX_MS = 650
DEFAULT_HUMANIZED_TYPING_TYPO_PROBABILITY = 0.22
DEFAULT_HUMANIZED_TYPING_TYPO_MAX = 1
DEFAULT_HUMANIZED_SEND_PRE_DELAY_MIN_MS = 280
DEFAULT_HUMANIZED_SEND_PRE_DELAY_MAX_MS = 1300
DEFAULT_HUMANIZED_SEND_POST_INPUT_DELAY_MIN_MS = 120
DEFAULT_HUMANIZED_SEND_POST_INPUT_DELAY_MAX_MS = 460
DEFAULT_HUMANIZED_SEND_TRIGGER_DELAY_MIN_MS = 420
DEFAULT_HUMANIZED_SEND_TRIGGER_DELAY_MAX_MS = 1350
DEFAULT_HUMANIZED_SEND_AFTER_TRIGGER_DELAY_MIN_MS = 220
DEFAULT_HUMANIZED_SEND_AFTER_TRIGGER_DELAY_MAX_MS = 760
DEFAULT_HUMANIZED_ADAPTIVE_SPEED_ENABLED = True
DEFAULT_HUMANIZED_SHORT_TEXT_CHARS = 90
DEFAULT_HUMANIZED_LONG_TEXT_CHARS = 240
HUMANIZED_TYPO_CANDIDATES = "asdfjkl;,.?/[]"


def sendinput_safe_text(text: str) -> str:
    clean = str(text or "")
    clean = clean.replace("\r\n", " ").replace("\n", " ").replace("\r", " ").replace("\t", " ")
    return re.sub(r" {2,}", " ", clean).strip()


def humanized_input_settings() -> dict[str, Any]:
    enabled = env_flag("WECHAT_WIN32_OCR_HUMANIZED_INPUT_ENABLED", default=DEFAULT_HUMANIZED_INPUT_ENABLED)
    method = normalize_humanized_input_method(
        os.getenv("WECHAT_WIN32_OCR_HUMANIZED_INPUT_METHOD", DEFAULT_HUMANIZED_INPUT_METHOD),
        default=DEFAULT_HUMANIZED_INPUT_METHOD,
    )
    chunk_min = bounded_int(
        os.getenv("WECHAT_WIN32_OCR_HUMANIZED_TYPING_CHUNK_MIN_CHARS"),
        default=DEFAULT_HUMANIZED_TYPING_CHUNK_MIN_CHARS,
        minimum=1,
        maximum=24,
    )
    chunk_max = bounded_int(
        os.getenv("WECHAT_WIN32_OCR_HUMANIZED_TYPING_CHUNK_MAX_CHARS"),
        default=DEFAULT_HUMANIZED_TYPING_CHUNK_MAX_CHARS,
        minimum=chunk_min,
        maximum=36,
    )
    char_delay_min_ms = bounded_int(
        os.getenv("WECHAT_WIN32_OCR_HUMANIZED_TYPING_CHAR_DELAY_MIN_MS"),
        default=DEFAULT_HUMANIZED_TYPING_CHAR_DELAY_MIN_MS,
        minimum=0,
        maximum=1200,
    )
    char_delay_max_ms = bounded_int(
        os.getenv("WECHAT_WIN32_OCR_HUMANIZED_TYPING_CHAR_DELAY_MAX_MS"),
        default=DEFAULT_HUMANIZED_TYPING_CHAR_DELAY_MAX_MS,
        minimum=char_delay_min_ms,
        maximum=1600,
    )
    micro_pause_every_chars = bounded_int(
        os.getenv("WECHAT_WIN32_OCR_HUMANIZED_TYPING_MICRO_PAUSE_EVERY_CHARS"),
        default=DEFAULT_HUMANIZED_TYPING_MICRO_PAUSE_EVERY_CHARS,
        minimum=0,
        maximum=300,
    )
    micro_pause_min_ms = bounded_int(
        os.getenv("WECHAT_WIN32_OCR_HUMANIZED_TYPING_MICRO_PAUSE_MIN_MS"),
        default=DEFAULT_HUMANIZED_TYPING_MICRO_PAUSE_MIN_MS,
        minimum=0,
        maximum=5000,
    )
    micro_pause_max_ms = bounded_int(
        os.getenv("WECHAT_WIN32_OCR_HUMANIZED_TYPING_MICRO_PAUSE_MAX_MS"),
        default=DEFAULT_HUMANIZED_TYPING_MICRO_PAUSE_MAX_MS,
        minimum=micro_pause_min_ms,
        maximum=7000,
    )
    typo_probability = max(
        0.0,
        min(
            1.0,
            env_float(
                "WECHAT_WIN32_OCR_HUMANIZED_TYPING_TYPO_PROBABILITY",
                default=DEFAULT_HUMANIZED_TYPING_TYPO_PROBABILITY,
            ),
        ),
    )
    typo_max = bounded_int(
        os.getenv("WECHAT_WIN32_OCR_HUMANIZED_TYPING_TYPO_MAX"),
        default=DEFAULT_HUMANIZED_TYPING_TYPO_MAX,
        minimum=0,
        maximum=6,
    )
    pre_delay_min_ms = bounded_int(
        os.getenv("WECHAT_WIN32_OCR_HUMANIZED_SEND_PRE_DELAY_MIN_MS"),
        default=DEFAULT_HUMANIZED_SEND_PRE_DELAY_MIN_MS,
        minimum=0,
        maximum=6000,
    )
    pre_delay_max_ms = bounded_int(
        os.getenv("WECHAT_WIN32_OCR_HUMANIZED_SEND_PRE_DELAY_MAX_MS"),
        default=DEFAULT_HUMANIZED_SEND_PRE_DELAY_MAX_MS,
        minimum=pre_delay_min_ms,
        maximum=8000,
    )
    post_delay_min_ms = bounded_int(
        os.getenv("WECHAT_WIN32_OCR_HUMANIZED_SEND_POST_INPUT_DELAY_MIN_MS"),
        default=DEFAULT_HUMANIZED_SEND_POST_INPUT_DELAY_MIN_MS,
        minimum=0,
        maximum=4000,
    )
    post_delay_max_ms = bounded_int(
        os.getenv("WECHAT_WIN32_OCR_HUMANIZED_SEND_POST_INPUT_DELAY_MAX_MS"),
        default=DEFAULT_HUMANIZED_SEND_POST_INPUT_DELAY_MAX_MS,
        minimum=post_delay_min_ms,
        maximum=6000,
    )
    trigger_delay_min_ms = bounded_int(
        os.getenv("WECHAT_WIN32_OCR_HUMANIZED_SEND_TRIGGER_DELAY_MIN_MS"),
        default=DEFAULT_HUMANIZED_SEND_TRIGGER_DELAY_MIN_MS,
        minimum=0,
        maximum=6000,
    )
    trigger_delay_max_ms = bounded_int(
        os.getenv("WECHAT_WIN32_OCR_HUMANIZED_SEND_TRIGGER_DELAY_MAX_MS"),
        default=DEFAULT_HUMANIZED_SEND_TRIGGER_DELAY_MAX_MS,
        minimum=trigger_delay_min_ms,
        maximum=8000,
    )
    after_trigger_delay_min_ms = bounded_int(
        os.getenv("WECHAT_WIN32_OCR_HUMANIZED_SEND_AFTER_TRIGGER_DELAY_MIN_MS"),
        default=DEFAULT_HUMANIZED_SEND_AFTER_TRIGGER_DELAY_MIN_MS,
        minimum=0,
        maximum=4000,
    )
    after_trigger_delay_max_ms = bounded_int(
        os.getenv("WECHAT_WIN32_OCR_HUMANIZED_SEND_AFTER_TRIGGER_DELAY_MAX_MS"),
        default=DEFAULT_HUMANIZED_SEND_AFTER_TRIGGER_DELAY_MAX_MS,
        minimum=after_trigger_delay_min_ms,
        maximum=6000,
    )
    inter_chunk_delay_scale = max(
        0.35,
        min(
            1.2,
            env_float("WECHAT_WIN32_OCR_HUMANIZED_INTER_CHUNK_DELAY_SCALE", default=1.0),
        ),
    )
    return {
        "enabled": enabled,
        "method": method,
        "chunk_min_chars": chunk_min,
        "chunk_max_chars": chunk_max,
        "char_delay_min_ms": char_delay_min_ms,
        "char_delay_max_ms": char_delay_max_ms,
        "micro_pause_every_chars": micro_pause_every_chars,
        "micro_pause_min_ms": micro_pause_min_ms,
        "micro_pause_max_ms": micro_pause_max_ms,
        "typo_probability": typo_probability,
        "typo_max": typo_max,
        "send_pre_delay_min_ms": pre_delay_min_ms,
        "send_pre_delay_max_ms": pre_delay_max_ms,
        "send_post_input_delay_min_ms": post_delay_min_ms,
        "send_post_input_delay_max_ms": post_delay_max_ms,
        "send_trigger_delay_min_ms": trigger_delay_min_ms,
        "send_trigger_delay_max_ms": trigger_delay_max_ms,
        "send_after_trigger_delay_min_ms": after_trigger_delay_min_ms,
        "send_after_trigger_delay_max_ms": after_trigger_delay_max_ms,
        "inter_chunk_delay_scale": inter_chunk_delay_scale,
        "adaptive_speed_enabled": env_flag(
            "WECHAT_WIN32_OCR_HUMANIZED_ADAPTIVE_SPEED_ENABLED",
            default=DEFAULT_HUMANIZED_ADAPTIVE_SPEED_ENABLED,
        ),
    }


def adapt_humanized_input_settings(settings: dict[str, Any], text: str) -> dict[str, Any]:
    active = dict(settings or {})
    if not active.get("enabled") or not active.get("adaptive_speed_enabled", True):
        return active
    text_len = len(sendinput_safe_text(text))
    if text_len <= DEFAULT_HUMANIZED_SHORT_TEXT_CHARS:
        profile = {
            "speed_profile": "short_natural",
            "chunk_min_chars": 5,
            "chunk_max_chars": 12,
            "char_delay_min_ms": 32,
            "char_delay_max_ms": 72,
            "micro_pause_every_chars": 48,
            "micro_pause_min_ms": 70,
            "micro_pause_max_ms": 180,
            "typo_probability": 0.08,
            "typo_max": 1,
            "send_pre_delay_min_ms": 90,
            "send_pre_delay_max_ms": 260,
            "send_post_input_delay_min_ms": 140,
            "send_post_input_delay_max_ms": 300,
            "send_trigger_delay_min_ms": 360,
            "send_trigger_delay_max_ms": 1050,
            "send_after_trigger_delay_min_ms": 180,
            "send_after_trigger_delay_max_ms": 520,
            "inter_chunk_delay_scale": 0.46,
        }
    elif text_len <= DEFAULT_HUMANIZED_LONG_TEXT_CHARS:
        profile = {
            "speed_profile": "medium_natural",
            "chunk_min_chars": 5,
            "chunk_max_chars": 12,
            "char_delay_min_ms": 30,
            "char_delay_max_ms": 68,
            "micro_pause_every_chars": 54,
            "micro_pause_min_ms": 65,
            "micro_pause_max_ms": 170,
            "typo_probability": 0.06,
            "typo_max": 1,
            "send_pre_delay_min_ms": 90,
            "send_pre_delay_max_ms": 240,
            "send_post_input_delay_min_ms": 130,
            "send_post_input_delay_max_ms": 280,
            "send_trigger_delay_min_ms": 420,
            "send_trigger_delay_max_ms": 1200,
            "send_after_trigger_delay_min_ms": 200,
            "send_after_trigger_delay_max_ms": 650,
            "inter_chunk_delay_scale": 0.50,
        }
    else:
        profile = {
            "speed_profile": "long_natural_capped",
            "chunk_min_chars": 6,
            "chunk_max_chars": 12,
            "char_delay_min_ms": 28,
            "char_delay_max_ms": 62,
            "micro_pause_every_chars": 60,
            "micro_pause_min_ms": 60,
            "micro_pause_max_ms": 160,
            "typo_probability": 0.05,
            "typo_max": 1,
            "send_pre_delay_min_ms": 90,
            "send_pre_delay_max_ms": 240,
            "send_post_input_delay_min_ms": 130,
            "send_post_input_delay_max_ms": 280,
            "send_trigger_delay_min_ms": 560,
            "send_trigger_delay_max_ms": 1600,
            "send_after_trigger_delay_min_ms": 260,
            "send_after_trigger_delay_max_ms": 850,
            "inter_chunk_delay_scale": 0.58,
        }
    active["speed_profile"] = profile["speed_profile"]
    for key in (
        "char_delay_min_ms",
        "char_delay_max_ms",
        "micro_pause_min_ms",
        "micro_pause_max_ms",
        "send_pre_delay_min_ms",
        "send_pre_delay_max_ms",
        "send_post_input_delay_min_ms",
        "send_post_input_delay_max_ms",
        "send_trigger_delay_min_ms",
        "send_trigger_delay_max_ms",
        "send_after_trigger_delay_min_ms",
        "send_after_trigger_delay_max_ms",
    ):
        current = int(active.get(key) or 0)
        target = int(profile[key])
        if key.startswith("send_post_input") or key.startswith("send_trigger") or key.startswith("send_after_trigger"):
            active[key] = target
        else:
            active[key] = 0 if current <= 0 else min(current, target)
    active["chunk_min_chars"] = max(int(active.get("chunk_min_chars") or 1), int(profile["chunk_min_chars"]))
    active["chunk_max_chars"] = max(int(active.get("chunk_max_chars") or 1), int(profile["chunk_max_chars"]))
    current_micro_every = int(active.get("micro_pause_every_chars") or 0)
    active["micro_pause_every_chars"] = (
        max(current_micro_every, int(profile["micro_pause_every_chars"])) if current_micro_every > 0 else 0
    )
    if active["chunk_max_chars"] < active["chunk_min_chars"]:
        active["chunk_max_chars"] = active["chunk_min_chars"]
    if int(active.get("char_delay_max_ms") or 0) < int(active.get("char_delay_min_ms") or 0):
        active["char_delay_max_ms"] = active["char_delay_min_ms"]
    if int(active.get("micro_pause_max_ms") or 0) < int(active.get("micro_pause_min_ms") or 0):
        active["micro_pause_max_ms"] = active["micro_pause_min_ms"]
    if int(active.get("send_trigger_delay_max_ms") or 0) < int(active.get("send_trigger_delay_min_ms") or 0):
        active["send_trigger_delay_max_ms"] = active["send_trigger_delay_min_ms"]
    if int(active.get("send_after_trigger_delay_max_ms") or 0) < int(active.get("send_after_trigger_delay_min_ms") or 0):
        active["send_after_trigger_delay_max_ms"] = active["send_after_trigger_delay_min_ms"]
    active["typo_probability"] = min(float(active.get("typo_probability") or 0.0), float(profile["typo_probability"]))
    active["typo_max"] = min(int(active.get("typo_max") or 0), int(profile["typo_max"]))
    try:
        current_scale = float(active.get("inter_chunk_delay_scale") or 1.0)
    except (TypeError, ValueError):
        current_scale = 1.0
    active["inter_chunk_delay_scale"] = max(
        0.35,
        min(
            1.2,
            min(current_scale, float(profile["inter_chunk_delay_scale"])),
        ),
    )
    active["adaptive_text_chars"] = text_len
    return active


def humanized_chunk_text(text: str, *, min_chars: int, max_chars: int) -> list[str]:
    clean = str(text or "")
    if not clean:
        return []
    chunks: list[str] = []
    cursor = 0
    lower = max(1, int(min_chars))
    upper = max(lower, int(max_chars))
    while cursor < len(clean):
        step = random.randint(lower, upper)
        next_cursor = min(len(clean), cursor + step)
        chunks.append(clean[cursor:next_cursor])
        cursor = next_cursor
    return chunks


def choose_humanized_typo_char() -> str:
    return random.choice(HUMANIZED_TYPO_CANDIDATES)


def typed_text_delay_ms(segment: str, settings: dict[str, Any]) -> tuple[int, int]:
    char_count = max(1, len(str(segment or "")))
    per_char_low = int(settings.get("char_delay_min_ms") or DEFAULT_HUMANIZED_TYPING_CHAR_DELAY_MIN_MS)
    per_char_high = int(settings.get("char_delay_max_ms") or DEFAULT_HUMANIZED_TYPING_CHAR_DELAY_MAX_MS)
    low = max(0, per_char_low * char_count)
    high = max(low, per_char_high * char_count)
    try:
        delay_scale = float(settings.get("inter_chunk_delay_scale") or 1.0)
    except (TypeError, ValueError):
        delay_scale = 1.0
    delay_scale = max(0.35, min(1.2, delay_scale))
    if abs(delay_scale - 1.0) > 1e-6:
        low = max(0, int(round(low * delay_scale)))
        high = max(low, int(round(high * delay_scale)))
    return low, high


def maybe_humanized_typo_allowed(settings: dict[str, Any], *, typo_count: int, text: str) -> bool:
    if typo_count >= int(settings.get("typo_max") or 0):
        return False
    if len(str(text or "")) < 6:
        return False
    probability = float(settings.get("typo_probability") or 0.0)
    return random.random() < max(0.0, min(1.0, probability))
