"""Environment and mode helpers for the Windows WeChat Win32/OCR adapter."""

from __future__ import annotations

import os

from apps.wechat_ai_customer_service.adapters.wechat_win32_ocr.geometry import bounded_int


DEFAULT_HUMANIZED_INPUT_METHOD = "sendinput_unicode"
DEFAULT_HUMANIZED_INPUT_ENFORCE_INTERMITTENT = True
DEFAULT_HUMANIZED_ALLOW_CLIPBOARD_ONCE = False
DEFAULT_SEND_INPUT_CONFIRM_ATTEMPTS = 3
DEFAULT_SEND_TRIGGER_MODE = "enter_only"
DEFAULT_STRICT_SEND_FOCUS_GUARD = True
DEFAULT_FOCUS_CLICK_FALLBACK = True
DEFAULT_ALLOW_UNKNOWN_FOREGROUND_GUARD = True


def env_int(name: str, default: int) -> int:
    try:
        return int(str(os.getenv(name) or "").strip() or default)
    except ValueError:
        return default


def env_float(name: str, default: float) -> float:
    try:
        return float(str(os.getenv(name) or "").strip() or default)
    except ValueError:
        return default


def env_flag(name: str, *, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return str(raw).strip().lower() not in {"0", "false", "no", "off"}


def normalize_humanized_input_method(
    raw_method: str | None,
    *,
    default: str = DEFAULT_HUMANIZED_INPUT_METHOD,
) -> str:
    method = str(raw_method or default).strip().lower()
    if method not in {"auto", "sendinput_unicode", "uia_chunks", "clipboard_chunks", "clipboard_once"}:
        method = default
    enforce_intermittent = env_flag(
        "WECHAT_WIN32_OCR_ENFORCE_INTERMITTENT_TYPING",
        default=DEFAULT_HUMANIZED_INPUT_ENFORCE_INTERMITTENT,
    )
    allow_clipboard_once = env_flag(
        "WECHAT_WIN32_OCR_ALLOW_CLIPBOARD_ONCE",
        default=DEFAULT_HUMANIZED_ALLOW_CLIPBOARD_ONCE,
    )
    if enforce_intermittent and method == "clipboard_once" and not allow_clipboard_once:
        return "clipboard_chunks"
    return method


def normalize_send_trigger_mode(raw_mode: str | None, *, default: str = DEFAULT_SEND_TRIGGER_MODE) -> str:
    mode = str(raw_mode or default).strip().lower()
    if mode not in {"click_only", "enter_only", "enter_then_click"}:
        return default
    if mode == "enter_then_click":
        return "enter_only"
    if mode == "click_only" and not env_flag("WECHAT_WIN32_OCR_ALLOW_CLICK_SEND_TRIGGER", default=False):
        return "enter_only"
    return mode


def strict_send_focus_guard_enabled() -> bool:
    return env_flag("WECHAT_WIN32_OCR_STRICT_SEND_FOCUS_GUARD", default=DEFAULT_STRICT_SEND_FOCUS_GUARD)


def focus_click_fallback_enabled() -> bool:
    return env_flag("WECHAT_WIN32_OCR_FOCUS_CLICK_FALLBACK", default=DEFAULT_FOCUS_CLICK_FALLBACK)


def allow_unknown_foreground_guard() -> bool:
    return env_flag("WECHAT_WIN32_OCR_ALLOW_UNKNOWN_FOREGROUND", default=DEFAULT_ALLOW_UNKNOWN_FOREGROUND_GUARD)


def send_input_confirm_attempt_count(total_attempts: int) -> int:
    requested = bounded_int(
        os.getenv("WECHAT_WIN32_OCR_SEND_INPUT_CONFIRM_ATTEMPTS"),
        default=DEFAULT_SEND_INPUT_CONFIRM_ATTEMPTS,
        minimum=1,
        maximum=max(1, int(total_attempts)),
    )
    if (
        requested == 1
        and total_attempts > 1
        and env_flag("WECHAT_WIN32_OCR_BLANK_INPUT_FOCUS_RETRY", default=True)
    ):
        return 2
    return requested


def rpa_action_pacing_enabled() -> bool:
    return env_flag("WECHAT_WIN32_OCR_UI_ACTION_PACING_ENABLED", default=True)
