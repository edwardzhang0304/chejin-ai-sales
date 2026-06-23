"""Managed loop for the local WeChat customer-service listener."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import psutil


APP_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = APP_ROOT.parents[1]
WIN32_OCR_SIDECAR_SCRIPT = APP_ROOT / "adapters" / "wechat_win32_ocr_sidecar.py"
WIN32_OCR_SIDECAR_PYTHON = PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"
for path in (PROJECT_ROOT, APP_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from apps.wechat_ai_customer_service.admin_backend.services.customer_service_runtime import (  # noqa: E402
    atomic_write_json,
    runtime_dir,
    runtime_log_path,
    runtime_operator_control_path,
    runtime_operator_guard_pid_path,
    runtime_operator_guard_state_path,
    stop_customer_service_support_processes,
    summarize_listener_result,
    write_runtime_status,
)
from apps.wechat_ai_customer_service.admin_backend.services.customer_service_scheduler import (  # noqa: E402
    ManagedListenerSchedulerBridge,
)
from apps.wechat_ai_customer_service.cloud_gate import cloud_gate_status, cloud_required_enabled  # noqa: E402
from apps.wechat_ai_customer_service.sync import VpsLocalSyncService  # noqa: E402


TRANSPORT_RISK_DEFAULTS = {
    "enabled": True,
    "counter_window_seconds": 600,
    "login_detect_stop_threshold": 1,
    "hard_block_stop_threshold": 1,
    "send_input_not_ready_stop_threshold": 3,
    "warning_cooldown_seconds": 120,
    "cooldown_near_threshold": 2,
    "loop_jitter_seconds": 0.6,
    "passive_logout_probe_enabled": True,
    "passive_logout_probe_interval_seconds": 45,
    "passive_logout_probe_fail_stop_threshold": 1,
    "passive_logout_probe_timeout_seconds": 12,
    "interactive_calibration_timeout_seconds": 24,
    "interactive_calibration_enabled": True,
    "startup_interactive_calibration_enabled": True,
    "resume_interactive_calibration_enabled": True,
    "passive_probe_recalibrate_enabled": True,
    "passive_probe_recalibrate_fail_threshold": 2,
    "passive_probe_recalibrate_cooldown_seconds": 45,
    "passive_probe_empty_ocr_fail_enabled": True,
    "passive_probe_empty_ocr_min_count": 1,
}

TRANSPORT_LOGIN_STATES = {"login_window_detected", "wechat_not_ready", "main_window_not_found"}
TRANSPORT_SEND_INPUT_STATES = {"send_input_not_ready"}
TRANSPORT_SEND_INPUT_REASONS = {"paste_not_confirmed", "input_token_not_detected_after_paste"}
TRANSPORT_ABNORMAL_WINDOW_STATES = {
    "blank_render_detected",
    "auxiliary_shell_window_detected",
    "main_window_geometry_invalid",
}
TRANSPORT_ABNORMAL_WINDOW_REASONS = {
    "blank_render",
    "auxiliary_shell_window",
    "window_offscreen_or_minimized",
}
TRANSPORT_HARD_BLOCK_TOKENS = {"操作频繁", "登录环境异常", "账号安全", "安全验证"}
TRANSPORT_INVALID_WINDOW_TOKENS = {"GetWindowRect", "无效的窗口句柄", "invalid window handle"}
TRANSPORT_RISK_HANDOFF_STOP_REASONS = {
    "wechat_login_window_detected",
    "wechat_logout_detected_by_passive_probe",
    "wechat_blank_render_detected_by_passive_probe",
    "wechat_auxiliary_shell_detected_by_passive_probe",
    "wechat_abnormal_window_detected",
    "wechat_hard_block_detected",
    "wechat_send_input_not_ready_repeated",
    "runtime_disallowed_target_detected",
    "wechat_interactive_calibration_failed",
    "sandbox_wechat_login_or_qr_detected",
    "sandbox_wechat_security_relogin_detected",
}
RPA_HUMANIZED_SEND_DEFAULTS = {
    "enabled": True,
    "input_method": "sendinput_unicode",
    "typing_chunk_min_chars": 2,
    "typing_chunk_max_chars": 6,
    "typing_char_delay_min_ms": 50,
    "typing_char_delay_max_ms": 180,
    "typing_micro_pause_every_chars": 18,
    "typing_micro_pause_min_ms": 220,
    "typing_micro_pause_max_ms": 650,
    "typing_typo_probability": 0.22,
    "typing_typo_max": 1,
    "send_pre_delay_min_ms": 280,
    "send_pre_delay_max_ms": 1300,
    "send_post_input_delay_min_ms": 120,
    "send_post_input_delay_max_ms": 460,
    "send_trigger_delay_min_ms": 420,
    "send_trigger_delay_max_ms": 1350,
    "send_after_trigger_delay_min_ms": 220,
    "send_after_trigger_delay_max_ms": 760,
    "adaptive_speed_enabled": True,
    "fast_send_confirmation_enabled": True,
    "input_fast_visual_confirm_enabled": True,
    "send_trigger_mode": "enter_only",
    "send_input_confirm_attempts": 3,
    "send_rate_min_interval_seconds": 0,
    "send_rate_burst_window_seconds": 600,
    "send_rate_burst_limit": 20,
}
OPERATOR_GUARD_DEFAULTS = {
    "enabled": False,
    "block_manual_input": True,
    "floating_indicator_enabled": True,
    "control_hotkey": "f8",
    "esc_double_press_window_ms": 420,
    "pause_poll_interval_ms": 550,
    "bootstrap_timeout_seconds": 15.0,
}
OPERATOR_COMMAND_ACTIONS = {"pause", "resume", "stop"}
OPERATOR_CONTROL_MODES = {"running", "paused", "stopped"}
RPA_HUMANIZED_ALLOW_CLIPBOARD_ONCE_DEFAULT = False
RPA_HUMANIZED_ENFORCE_INTERMITTENT_DEFAULT = True


def env_bool(name: str, *, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return str(raw).strip().lower() not in {"0", "false", "no", "off"}


def positive_int(value: Any, default: int, *, minimum: int = 1) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, parsed)


def non_negative_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(0, parsed)


def normalize_humanized_input_method(method: Any) -> str:
    raw = str(method or RPA_HUMANIZED_SEND_DEFAULTS["input_method"]).strip().lower()
    if raw not in {"auto", "sendinput_unicode", "uia_chunks", "clipboard_chunks", "clipboard_once"}:
        raw = str(RPA_HUMANIZED_SEND_DEFAULTS["input_method"])
    enforce_intermittent = env_bool(
        "WECHAT_WIN32_OCR_ENFORCE_INTERMITTENT_TYPING",
        default=RPA_HUMANIZED_ENFORCE_INTERMITTENT_DEFAULT,
    )
    allow_clipboard_once = env_bool(
        "WECHAT_WIN32_OCR_ALLOW_CLIPBOARD_ONCE",
        default=RPA_HUMANIZED_ALLOW_CLIPBOARD_ONCE_DEFAULT,
    )
    if enforce_intermittent and raw == "clipboard_once" and not allow_clipboard_once:
        return "clipboard_chunks"
    return raw


def normalize_send_trigger_mode(method: Any) -> str:
    raw = str(method or RPA_HUMANIZED_SEND_DEFAULTS["send_trigger_mode"]).strip().lower()
    if raw not in {"click_only", "enter_only", "enter_then_click"}:
        return str(RPA_HUMANIZED_SEND_DEFAULTS["send_trigger_mode"])
    if raw == "enter_then_click":
        return "enter_only"
    if raw == "click_only" and not env_bool("WECHAT_WIN32_OCR_ALLOW_CLICK_SEND_TRIGGER", default=False):
        return "enter_only"
    return raw


def non_negative_float(value: Any, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    return max(0.0, parsed)


def load_transport_risk_settings(config_path: Path) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception:
        raw = {}
    if isinstance(raw, dict):
        candidate = raw.get("transport_risk_guard")
        if isinstance(candidate, dict):
            payload = dict(candidate)
    settings = normalize_transport_risk_settings(payload)
    settings["enabled"] = env_bool("WECHAT_TRANSPORT_RISK_GUARD_ENABLED", default=bool(settings.get("enabled", True)))
    settings["counter_window_seconds"] = positive_int(
        os.getenv("WECHAT_TRANSPORT_RISK_COUNTER_WINDOW_SECONDS"),
        int(settings.get("counter_window_seconds") or 600),
        minimum=60,
    )
    settings["login_detect_stop_threshold"] = positive_int(
        os.getenv("WECHAT_TRANSPORT_RISK_LOGIN_THRESHOLD"),
        int(settings.get("login_detect_stop_threshold") or 1),
    )
    settings["hard_block_stop_threshold"] = positive_int(
        os.getenv("WECHAT_TRANSPORT_RISK_HARD_BLOCK_THRESHOLD"),
        int(settings.get("hard_block_stop_threshold") or 1),
    )
    settings["send_input_not_ready_stop_threshold"] = positive_int(
        os.getenv("WECHAT_TRANSPORT_RISK_SEND_INPUT_THRESHOLD"),
        int(settings.get("send_input_not_ready_stop_threshold") or 3),
    )
    settings["warning_cooldown_seconds"] = non_negative_int(
        os.getenv("WECHAT_TRANSPORT_RISK_WARNING_COOLDOWN_SECONDS"),
        int(settings.get("warning_cooldown_seconds") or TRANSPORT_RISK_DEFAULTS["warning_cooldown_seconds"]),
    )
    settings["cooldown_near_threshold"] = positive_int(
        os.getenv("WECHAT_TRANSPORT_RISK_COOLDOWN_NEAR_THRESHOLD"),
        int(settings.get("cooldown_near_threshold") or TRANSPORT_RISK_DEFAULTS["cooldown_near_threshold"]),
    )
    settings["loop_jitter_seconds"] = non_negative_float(
        os.getenv("WECHAT_TRANSPORT_RISK_LOOP_JITTER_SECONDS"),
        float(settings.get("loop_jitter_seconds") or TRANSPORT_RISK_DEFAULTS["loop_jitter_seconds"]),
    )
    settings["passive_logout_probe_enabled"] = env_bool(
        "WECHAT_TRANSPORT_RISK_PASSIVE_LOGOUT_PROBE_ENABLED",
        default=bool(settings.get("passive_logout_probe_enabled", True)),
    )
    settings["passive_logout_probe_interval_seconds"] = non_negative_int(
        os.getenv("WECHAT_TRANSPORT_RISK_PASSIVE_LOGOUT_PROBE_INTERVAL_SECONDS"),
        int(
            settings.get("passive_logout_probe_interval_seconds")
            or TRANSPORT_RISK_DEFAULTS["passive_logout_probe_interval_seconds"]
        ),
    )
    settings["passive_logout_probe_fail_stop_threshold"] = positive_int(
        os.getenv("WECHAT_TRANSPORT_RISK_PASSIVE_LOGOUT_PROBE_FAIL_STOP_THRESHOLD"),
        int(
            settings.get("passive_logout_probe_fail_stop_threshold")
            or TRANSPORT_RISK_DEFAULTS["passive_logout_probe_fail_stop_threshold"]
        ),
    )
    settings["passive_logout_probe_timeout_seconds"] = positive_int(
        os.getenv("WECHAT_TRANSPORT_RISK_PASSIVE_LOGOUT_PROBE_TIMEOUT_SECONDS"),
        int(
            settings.get("passive_logout_probe_timeout_seconds")
            or TRANSPORT_RISK_DEFAULTS["passive_logout_probe_timeout_seconds"]
        ),
    )
    settings["interactive_calibration_timeout_seconds"] = positive_int(
        os.getenv("WECHAT_RPA_INTERACTIVE_CALIBRATION_TIMEOUT_SECONDS"),
        int(
            settings.get("interactive_calibration_timeout_seconds")
            or TRANSPORT_RISK_DEFAULTS["interactive_calibration_timeout_seconds"]
        ),
    )
    settings["interactive_calibration_enabled"] = env_bool(
        "WECHAT_RPA_INTERACTIVE_CALIBRATION_ENABLED",
        default=bool(settings.get("interactive_calibration_enabled", True)),
    )
    settings["startup_interactive_calibration_enabled"] = env_bool(
        "WECHAT_RPA_STARTUP_INTERACTIVE_CALIBRATION_ENABLED",
        default=bool(settings.get("startup_interactive_calibration_enabled", True)),
    )
    settings["resume_interactive_calibration_enabled"] = env_bool(
        "WECHAT_RPA_RESUME_INTERACTIVE_CALIBRATION_ENABLED",
        default=bool(settings.get("resume_interactive_calibration_enabled", True)),
    )
    settings["passive_probe_recalibrate_enabled"] = env_bool(
        "WECHAT_RPA_PASSIVE_PROBE_RECALIBRATE_ENABLED",
        default=bool(settings.get("passive_probe_recalibrate_enabled", True)),
    )
    settings["passive_probe_recalibrate_fail_threshold"] = positive_int(
        os.getenv("WECHAT_RPA_PASSIVE_PROBE_RECALIBRATE_FAIL_THRESHOLD"),
        int(
            settings.get("passive_probe_recalibrate_fail_threshold")
            or TRANSPORT_RISK_DEFAULTS["passive_probe_recalibrate_fail_threshold"]
        ),
    )
    settings["passive_probe_recalibrate_cooldown_seconds"] = non_negative_int(
        os.getenv("WECHAT_RPA_PASSIVE_PROBE_RECALIBRATE_COOLDOWN_SECONDS"),
        int(
            settings.get("passive_probe_recalibrate_cooldown_seconds")
            or TRANSPORT_RISK_DEFAULTS["passive_probe_recalibrate_cooldown_seconds"]
        ),
    )
    settings["passive_probe_empty_ocr_fail_enabled"] = env_bool(
        "WECHAT_RPA_PASSIVE_PROBE_EMPTY_OCR_FAIL_ENABLED",
        default=bool(settings.get("passive_probe_empty_ocr_fail_enabled", True)),
    )
    settings["passive_probe_empty_ocr_min_count"] = non_negative_int(
        os.getenv("WECHAT_RPA_PASSIVE_PROBE_EMPTY_OCR_MIN_COUNT"),
        int(settings.get("passive_probe_empty_ocr_min_count") or TRANSPORT_RISK_DEFAULTS["passive_probe_empty_ocr_min_count"]),
    )
    return settings


def load_concurrency_scheduler_enabled(config_path: Path) -> bool:
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception:
        return False
    if not isinstance(raw, dict):
        return False
    scheduler = raw.get("concurrency_scheduler")
    if isinstance(scheduler, dict):
        if scheduler.get("enabled") is True:
            return True
        if scheduler.get("enabled") is False:
            return False
    guard = raw.get("live_safety_guard")
    if not isinstance(guard, dict) or guard.get("enabled") is not True:
        return False
    if guard.get("backend_state_scheduler_enabled") is False:
        return False
    multi_target = raw.get("multi_target")
    if not isinstance(multi_target, dict) or multi_target.get("enabled") is not True:
        return False
    return multi_target.get("rpa_low_risk_mode", True) is not False


def load_managed_poll_interval_settings(config_path: Path, *, fallback_seconds: float = 3.0) -> dict[str, float]:
    """Return the managed listener's idle polling window.

    The live RPA loop should not tick at an exact fixed cadence. We keep a
    short 3-5s default window, then sample a fresh value each idle loop.
    """

    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception:
        raw = {}
    poll = raw.get("poll") if isinstance(raw, dict) else {}
    if not isinstance(poll, dict):
        poll = {}

    def read_float(*keys: str, default: float) -> float:
        for key in keys:
            if key in poll:
                try:
                    return float(poll.get(key))
                except (TypeError, ValueError):
                    continue
        return float(default)

    center = read_float("interval_seconds", default=fallback_seconds)
    min_seconds = read_float("interval_min_seconds", "min_interval_seconds", default=center)
    max_seconds = read_float("interval_max_seconds", "max_interval_seconds", default=max(center, min_seconds))
    min_seconds = max(0.5, min(10.0, min_seconds))
    max_seconds = max(0.5, min(10.0, max_seconds))
    if max_seconds < min_seconds:
        max_seconds = min_seconds
    return {"min_seconds": min_seconds, "max_seconds": max_seconds}


def _deduped_names(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    names: list[str] = []
    seen: set[str] = set()
    for raw in values:
        name = str(raw or "").strip()
        if not name or name in seen:
            continue
        names.append(name)
        seen.add(name)
    return names


def normalize_runtime_target_guard_settings(settings: dict[str, Any] | None) -> dict[str, Any]:
    source = settings if isinstance(settings, dict) else {}
    enabled = bool(source.get("enabled")) and bool(source.get("enforce_runtime_targets", True))
    return {
        "enabled": enabled,
        "allowed_targets": _deduped_names(source.get("allowed_targets") or source.get("targets")),
        "reason": "live_safety_guard_runtime_targets" if enabled else "disabled",
    }


def load_runtime_target_guard_settings(config_path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception:
        raw = {}
    guard = raw.get("live_safety_guard") if isinstance(raw, dict) else {}
    return normalize_runtime_target_guard_settings(guard if isinstance(guard, dict) else {})


def normalize_transport_risk_settings(settings: dict[str, Any] | None) -> dict[str, Any]:
    source = settings if isinstance(settings, dict) else {}
    return {
        "enabled": bool(source.get("enabled", TRANSPORT_RISK_DEFAULTS["enabled"])),
        "counter_window_seconds": positive_int(
            source.get("counter_window_seconds"),
            TRANSPORT_RISK_DEFAULTS["counter_window_seconds"],
            minimum=60,
        ),
        "login_detect_stop_threshold": positive_int(
            source.get("login_detect_stop_threshold"),
            TRANSPORT_RISK_DEFAULTS["login_detect_stop_threshold"],
        ),
        "hard_block_stop_threshold": positive_int(
            source.get("hard_block_stop_threshold"),
            TRANSPORT_RISK_DEFAULTS["hard_block_stop_threshold"],
        ),
        "send_input_not_ready_stop_threshold": positive_int(
            source.get("send_input_not_ready_stop_threshold"),
            TRANSPORT_RISK_DEFAULTS["send_input_not_ready_stop_threshold"],
        ),
        "warning_cooldown_seconds": non_negative_int(
            source.get("warning_cooldown_seconds"),
            TRANSPORT_RISK_DEFAULTS["warning_cooldown_seconds"],
        ),
        "cooldown_near_threshold": positive_int(
            source.get("cooldown_near_threshold"),
            TRANSPORT_RISK_DEFAULTS["cooldown_near_threshold"],
        ),
        "loop_jitter_seconds": non_negative_float(
            source.get("loop_jitter_seconds"),
            TRANSPORT_RISK_DEFAULTS["loop_jitter_seconds"],
        ),
        "passive_logout_probe_enabled": bool(
            source.get("passive_logout_probe_enabled", TRANSPORT_RISK_DEFAULTS["passive_logout_probe_enabled"])
        ),
        "passive_logout_probe_interval_seconds": non_negative_int(
            source.get("passive_logout_probe_interval_seconds"),
            TRANSPORT_RISK_DEFAULTS["passive_logout_probe_interval_seconds"],
        ),
        "passive_logout_probe_fail_stop_threshold": positive_int(
            source.get("passive_logout_probe_fail_stop_threshold"),
            TRANSPORT_RISK_DEFAULTS["passive_logout_probe_fail_stop_threshold"],
        ),
        "passive_logout_probe_timeout_seconds": positive_int(
            source.get("passive_logout_probe_timeout_seconds"),
            TRANSPORT_RISK_DEFAULTS["passive_logout_probe_timeout_seconds"],
        ),
        "interactive_calibration_timeout_seconds": positive_int(
            source.get("interactive_calibration_timeout_seconds"),
            TRANSPORT_RISK_DEFAULTS["interactive_calibration_timeout_seconds"],
        ),
        "interactive_calibration_enabled": bool(
            source.get("interactive_calibration_enabled", TRANSPORT_RISK_DEFAULTS["interactive_calibration_enabled"])
        ),
        "startup_interactive_calibration_enabled": bool(
            source.get(
                "startup_interactive_calibration_enabled",
                TRANSPORT_RISK_DEFAULTS["startup_interactive_calibration_enabled"],
            )
        ),
        "resume_interactive_calibration_enabled": bool(
            source.get(
                "resume_interactive_calibration_enabled",
                TRANSPORT_RISK_DEFAULTS["resume_interactive_calibration_enabled"],
            )
        ),
        "passive_probe_recalibrate_enabled": bool(
            source.get("passive_probe_recalibrate_enabled", TRANSPORT_RISK_DEFAULTS["passive_probe_recalibrate_enabled"])
        ),
        "passive_probe_recalibrate_fail_threshold": positive_int(
            source.get("passive_probe_recalibrate_fail_threshold"),
            TRANSPORT_RISK_DEFAULTS["passive_probe_recalibrate_fail_threshold"],
        ),
        "passive_probe_recalibrate_cooldown_seconds": non_negative_int(
            source.get("passive_probe_recalibrate_cooldown_seconds"),
            TRANSPORT_RISK_DEFAULTS["passive_probe_recalibrate_cooldown_seconds"],
        ),
        "passive_probe_empty_ocr_fail_enabled": bool(
            source.get("passive_probe_empty_ocr_fail_enabled", TRANSPORT_RISK_DEFAULTS["passive_probe_empty_ocr_fail_enabled"])
        ),
        "passive_probe_empty_ocr_min_count": non_negative_int(
            source.get("passive_probe_empty_ocr_min_count"),
            TRANSPORT_RISK_DEFAULTS["passive_probe_empty_ocr_min_count"],
        ),
    }


def load_rpa_humanized_send_settings(config_path: Path) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception:
        raw = {}
    if isinstance(raw, dict):
        candidate = raw.get("rpa_humanized_send")
        if isinstance(candidate, dict):
            payload = dict(candidate)
    settings = normalize_rpa_humanized_send_settings(payload)
    settings["enabled"] = env_bool(
        "WECHAT_WIN32_OCR_HUMANIZED_INPUT_ENABLED",
        default=bool(settings.get("enabled", RPA_HUMANIZED_SEND_DEFAULTS["enabled"])),
    )
    method = normalize_humanized_input_method(
        os.getenv("WECHAT_WIN32_OCR_HUMANIZED_INPUT_METHOD")
        or settings.get("input_method")
        or RPA_HUMANIZED_SEND_DEFAULTS["input_method"]
    )
    settings["input_method"] = method
    settings["typing_chunk_min_chars"] = positive_int(
        os.getenv("WECHAT_WIN32_OCR_HUMANIZED_TYPING_CHUNK_MIN_CHARS"),
        int(settings.get("typing_chunk_min_chars") or RPA_HUMANIZED_SEND_DEFAULTS["typing_chunk_min_chars"]),
    )
    settings["typing_chunk_max_chars"] = positive_int(
        os.getenv("WECHAT_WIN32_OCR_HUMANIZED_TYPING_CHUNK_MAX_CHARS"),
        int(settings.get("typing_chunk_max_chars") or RPA_HUMANIZED_SEND_DEFAULTS["typing_chunk_max_chars"]),
    )
    if settings["typing_chunk_max_chars"] < settings["typing_chunk_min_chars"]:
        settings["typing_chunk_max_chars"] = settings["typing_chunk_min_chars"]
    settings["typing_char_delay_min_ms"] = non_negative_int(
        os.getenv("WECHAT_WIN32_OCR_HUMANIZED_TYPING_CHAR_DELAY_MIN_MS"),
        int(settings.get("typing_char_delay_min_ms") or RPA_HUMANIZED_SEND_DEFAULTS["typing_char_delay_min_ms"]),
    )
    settings["typing_char_delay_max_ms"] = non_negative_int(
        os.getenv("WECHAT_WIN32_OCR_HUMANIZED_TYPING_CHAR_DELAY_MAX_MS"),
        int(settings.get("typing_char_delay_max_ms") or RPA_HUMANIZED_SEND_DEFAULTS["typing_char_delay_max_ms"]),
    )
    if settings["typing_char_delay_max_ms"] < settings["typing_char_delay_min_ms"]:
        settings["typing_char_delay_max_ms"] = settings["typing_char_delay_min_ms"]
    settings["typing_micro_pause_every_chars"] = non_negative_int(
        os.getenv("WECHAT_WIN32_OCR_HUMANIZED_TYPING_MICRO_PAUSE_EVERY_CHARS"),
        int(
            settings.get("typing_micro_pause_every_chars")
            or RPA_HUMANIZED_SEND_DEFAULTS["typing_micro_pause_every_chars"]
        ),
    )
    settings["typing_micro_pause_min_ms"] = non_negative_int(
        os.getenv("WECHAT_WIN32_OCR_HUMANIZED_TYPING_MICRO_PAUSE_MIN_MS"),
        int(
            settings.get("typing_micro_pause_min_ms")
            or RPA_HUMANIZED_SEND_DEFAULTS["typing_micro_pause_min_ms"]
        ),
    )
    settings["typing_micro_pause_max_ms"] = non_negative_int(
        os.getenv("WECHAT_WIN32_OCR_HUMANIZED_TYPING_MICRO_PAUSE_MAX_MS"),
        int(
            settings.get("typing_micro_pause_max_ms")
            or RPA_HUMANIZED_SEND_DEFAULTS["typing_micro_pause_max_ms"]
        ),
    )
    if settings["typing_micro_pause_max_ms"] < settings["typing_micro_pause_min_ms"]:
        settings["typing_micro_pause_max_ms"] = settings["typing_micro_pause_min_ms"]
    settings["typing_typo_probability"] = max(
        0.0,
        min(
            1.0,
            non_negative_float(
                os.getenv("WECHAT_WIN32_OCR_HUMANIZED_TYPING_TYPO_PROBABILITY"),
                float(
                    settings.get("typing_typo_probability")
                    or RPA_HUMANIZED_SEND_DEFAULTS["typing_typo_probability"]
                ),
            ),
        ),
    )
    settings["typing_typo_max"] = non_negative_int(
        os.getenv("WECHAT_WIN32_OCR_HUMANIZED_TYPING_TYPO_MAX"),
        int(settings.get("typing_typo_max") or RPA_HUMANIZED_SEND_DEFAULTS["typing_typo_max"]),
    )
    settings["send_pre_delay_min_ms"] = non_negative_int(
        os.getenv("WECHAT_WIN32_OCR_HUMANIZED_SEND_PRE_DELAY_MIN_MS"),
        int(settings.get("send_pre_delay_min_ms") or RPA_HUMANIZED_SEND_DEFAULTS["send_pre_delay_min_ms"]),
    )
    settings["send_pre_delay_max_ms"] = non_negative_int(
        os.getenv("WECHAT_WIN32_OCR_HUMANIZED_SEND_PRE_DELAY_MAX_MS"),
        int(settings.get("send_pre_delay_max_ms") or RPA_HUMANIZED_SEND_DEFAULTS["send_pre_delay_max_ms"]),
    )
    if settings["send_pre_delay_max_ms"] < settings["send_pre_delay_min_ms"]:
        settings["send_pre_delay_max_ms"] = settings["send_pre_delay_min_ms"]
    settings["send_post_input_delay_min_ms"] = non_negative_int(
        os.getenv("WECHAT_WIN32_OCR_HUMANIZED_SEND_POST_INPUT_DELAY_MIN_MS"),
        int(
            settings.get("send_post_input_delay_min_ms")
            or RPA_HUMANIZED_SEND_DEFAULTS["send_post_input_delay_min_ms"]
        ),
    )
    settings["send_post_input_delay_max_ms"] = non_negative_int(
        os.getenv("WECHAT_WIN32_OCR_HUMANIZED_SEND_POST_INPUT_DELAY_MAX_MS"),
        int(
            settings.get("send_post_input_delay_max_ms")
            or RPA_HUMANIZED_SEND_DEFAULTS["send_post_input_delay_max_ms"]
        ),
    )
    if settings["send_post_input_delay_max_ms"] < settings["send_post_input_delay_min_ms"]:
        settings["send_post_input_delay_max_ms"] = settings["send_post_input_delay_min_ms"]
    settings["send_trigger_delay_min_ms"] = non_negative_int(
        os.getenv("WECHAT_WIN32_OCR_HUMANIZED_SEND_TRIGGER_DELAY_MIN_MS"),
        int(settings.get("send_trigger_delay_min_ms") or RPA_HUMANIZED_SEND_DEFAULTS["send_trigger_delay_min_ms"]),
    )
    settings["send_trigger_delay_max_ms"] = non_negative_int(
        os.getenv("WECHAT_WIN32_OCR_HUMANIZED_SEND_TRIGGER_DELAY_MAX_MS"),
        int(settings.get("send_trigger_delay_max_ms") or RPA_HUMANIZED_SEND_DEFAULTS["send_trigger_delay_max_ms"]),
    )
    if settings["send_trigger_delay_max_ms"] < settings["send_trigger_delay_min_ms"]:
        settings["send_trigger_delay_max_ms"] = settings["send_trigger_delay_min_ms"]
    settings["send_after_trigger_delay_min_ms"] = non_negative_int(
        os.getenv("WECHAT_WIN32_OCR_HUMANIZED_SEND_AFTER_TRIGGER_DELAY_MIN_MS"),
        int(settings.get("send_after_trigger_delay_min_ms") or RPA_HUMANIZED_SEND_DEFAULTS["send_after_trigger_delay_min_ms"]),
    )
    settings["send_after_trigger_delay_max_ms"] = non_negative_int(
        os.getenv("WECHAT_WIN32_OCR_HUMANIZED_SEND_AFTER_TRIGGER_DELAY_MAX_MS"),
        int(settings.get("send_after_trigger_delay_max_ms") or RPA_HUMANIZED_SEND_DEFAULTS["send_after_trigger_delay_max_ms"]),
    )
    if settings["send_after_trigger_delay_max_ms"] < settings["send_after_trigger_delay_min_ms"]:
        settings["send_after_trigger_delay_max_ms"] = settings["send_after_trigger_delay_min_ms"]
    settings["adaptive_speed_enabled"] = env_bool(
        "WECHAT_WIN32_OCR_HUMANIZED_ADAPTIVE_SPEED_ENABLED",
        default=bool(settings.get("adaptive_speed_enabled", RPA_HUMANIZED_SEND_DEFAULTS["adaptive_speed_enabled"])),
    )
    settings["fast_send_confirmation_enabled"] = env_bool(
        "WECHAT_WIN32_OCR_FAST_SEND_CONFIRMATION",
        default=bool(
            settings.get(
                "fast_send_confirmation_enabled",
                RPA_HUMANIZED_SEND_DEFAULTS["fast_send_confirmation_enabled"],
            )
        ),
    )
    settings["input_fast_visual_confirm_enabled"] = env_bool(
        "WECHAT_WIN32_OCR_INPUT_FAST_VISUAL_CONFIRM",
        default=bool(
            settings.get(
                "input_fast_visual_confirm_enabled",
                RPA_HUMANIZED_SEND_DEFAULTS["input_fast_visual_confirm_enabled"],
            )
        ),
    )
    settings["send_trigger_mode"] = normalize_send_trigger_mode(
        os.getenv("WECHAT_WIN32_OCR_SEND_TRIGGER_MODE")
        or settings.get("send_trigger_mode")
        or RPA_HUMANIZED_SEND_DEFAULTS["send_trigger_mode"]
    )
    settings["send_input_confirm_attempts"] = positive_int(
        os.getenv("WECHAT_WIN32_OCR_SEND_INPUT_CONFIRM_ATTEMPTS"),
        int(settings.get("send_input_confirm_attempts") or RPA_HUMANIZED_SEND_DEFAULTS["send_input_confirm_attempts"]),
    )
    configured_min_interval = settings.get("send_rate_min_interval_seconds")
    if configured_min_interval in (None, ""):
        configured_min_interval = RPA_HUMANIZED_SEND_DEFAULTS["send_rate_min_interval_seconds"]
    settings["send_rate_min_interval_seconds"] = non_negative_int(
        os.getenv("WECHAT_WIN32_OCR_SEND_MIN_INTERVAL_SECONDS"),
        int(configured_min_interval),
    )
    configured_burst_window = settings.get("send_rate_burst_window_seconds")
    if configured_burst_window in (None, ""):
        configured_burst_window = RPA_HUMANIZED_SEND_DEFAULTS["send_rate_burst_window_seconds"]
    settings["send_rate_burst_window_seconds"] = positive_int(
        os.getenv("WECHAT_WIN32_OCR_SEND_BURST_WINDOW_SECONDS"),
        int(configured_burst_window),
    )
    configured_burst_limit = settings.get("send_rate_burst_limit")
    if configured_burst_limit in (None, ""):
        configured_burst_limit = RPA_HUMANIZED_SEND_DEFAULTS["send_rate_burst_limit"]
    settings["send_rate_burst_limit"] = positive_int(
        os.getenv("WECHAT_WIN32_OCR_SEND_BURST_LIMIT"),
        int(configured_burst_limit),
    )
    return settings


def normalize_rpa_humanized_send_settings(settings: dict[str, Any] | None) -> dict[str, Any]:
    source = settings if isinstance(settings, dict) else {}
    method = normalize_humanized_input_method(source.get("input_method"))
    minimum_chunk = positive_int(
        source.get("typing_chunk_min_chars"),
        RPA_HUMANIZED_SEND_DEFAULTS["typing_chunk_min_chars"],
    )
    maximum_chunk = positive_int(
        source.get("typing_chunk_max_chars"),
        RPA_HUMANIZED_SEND_DEFAULTS["typing_chunk_max_chars"],
    )
    if maximum_chunk < minimum_chunk:
        maximum_chunk = minimum_chunk
    normalized = {
        "enabled": bool(source.get("enabled", RPA_HUMANIZED_SEND_DEFAULTS["enabled"])),
        "input_method": method,
        "typing_chunk_min_chars": minimum_chunk,
        "typing_chunk_max_chars": maximum_chunk,
        "typing_char_delay_min_ms": non_negative_int(
            source.get("typing_char_delay_min_ms"),
            RPA_HUMANIZED_SEND_DEFAULTS["typing_char_delay_min_ms"],
        ),
        "typing_char_delay_max_ms": non_negative_int(
            source.get("typing_char_delay_max_ms"),
            RPA_HUMANIZED_SEND_DEFAULTS["typing_char_delay_max_ms"],
        ),
        "typing_micro_pause_every_chars": non_negative_int(
            source.get("typing_micro_pause_every_chars"),
            RPA_HUMANIZED_SEND_DEFAULTS["typing_micro_pause_every_chars"],
        ),
        "typing_micro_pause_min_ms": non_negative_int(
            source.get("typing_micro_pause_min_ms"),
            RPA_HUMANIZED_SEND_DEFAULTS["typing_micro_pause_min_ms"],
        ),
        "typing_micro_pause_max_ms": non_negative_int(
            source.get("typing_micro_pause_max_ms"),
            RPA_HUMANIZED_SEND_DEFAULTS["typing_micro_pause_max_ms"],
        ),
        "typing_typo_probability": max(
            0.0,
            min(
                1.0,
                non_negative_float(
                    source.get("typing_typo_probability"),
                    RPA_HUMANIZED_SEND_DEFAULTS["typing_typo_probability"],
                ),
            ),
        ),
        "typing_typo_max": non_negative_int(
            source.get("typing_typo_max"),
            RPA_HUMANIZED_SEND_DEFAULTS["typing_typo_max"],
        ),
        "send_pre_delay_min_ms": non_negative_int(
            source.get("send_pre_delay_min_ms"),
            RPA_HUMANIZED_SEND_DEFAULTS["send_pre_delay_min_ms"],
        ),
        "send_pre_delay_max_ms": non_negative_int(
            source.get("send_pre_delay_max_ms"),
            RPA_HUMANIZED_SEND_DEFAULTS["send_pre_delay_max_ms"],
        ),
        "send_post_input_delay_min_ms": non_negative_int(
            source.get("send_post_input_delay_min_ms"),
            RPA_HUMANIZED_SEND_DEFAULTS["send_post_input_delay_min_ms"],
        ),
        "send_post_input_delay_max_ms": non_negative_int(
            source.get("send_post_input_delay_max_ms"),
            RPA_HUMANIZED_SEND_DEFAULTS["send_post_input_delay_max_ms"],
        ),
        "send_trigger_delay_min_ms": non_negative_int(
            source.get("send_trigger_delay_min_ms"),
            RPA_HUMANIZED_SEND_DEFAULTS["send_trigger_delay_min_ms"],
        ),
        "send_trigger_delay_max_ms": non_negative_int(
            source.get("send_trigger_delay_max_ms"),
            RPA_HUMANIZED_SEND_DEFAULTS["send_trigger_delay_max_ms"],
        ),
        "send_after_trigger_delay_min_ms": non_negative_int(
            source.get("send_after_trigger_delay_min_ms"),
            RPA_HUMANIZED_SEND_DEFAULTS["send_after_trigger_delay_min_ms"],
        ),
        "send_after_trigger_delay_max_ms": non_negative_int(
            source.get("send_after_trigger_delay_max_ms"),
            RPA_HUMANIZED_SEND_DEFAULTS["send_after_trigger_delay_max_ms"],
        ),
        "adaptive_speed_enabled": bool(
            source.get("adaptive_speed_enabled", RPA_HUMANIZED_SEND_DEFAULTS["adaptive_speed_enabled"])
        ),
        "fast_send_confirmation_enabled": bool(
            source.get(
                "fast_send_confirmation_enabled",
                RPA_HUMANIZED_SEND_DEFAULTS["fast_send_confirmation_enabled"],
            )
        ),
        "input_fast_visual_confirm_enabled": bool(
            source.get(
                "input_fast_visual_confirm_enabled",
                RPA_HUMANIZED_SEND_DEFAULTS["input_fast_visual_confirm_enabled"],
            )
        ),
        "send_trigger_mode": normalize_send_trigger_mode(source.get("send_trigger_mode")),
        "send_input_confirm_attempts": positive_int(
            source.get("send_input_confirm_attempts"),
            RPA_HUMANIZED_SEND_DEFAULTS["send_input_confirm_attempts"],
        ),
        "send_rate_min_interval_seconds": non_negative_int(
            source.get("send_rate_min_interval_seconds"),
            RPA_HUMANIZED_SEND_DEFAULTS["send_rate_min_interval_seconds"],
        ),
        "send_rate_burst_window_seconds": positive_int(
            source.get("send_rate_burst_window_seconds"),
            RPA_HUMANIZED_SEND_DEFAULTS["send_rate_burst_window_seconds"],
        ),
        "send_rate_burst_limit": positive_int(
            source.get("send_rate_burst_limit"),
            RPA_HUMANIZED_SEND_DEFAULTS["send_rate_burst_limit"],
        ),
    }
    if normalized["typing_char_delay_max_ms"] < normalized["typing_char_delay_min_ms"]:
        normalized["typing_char_delay_max_ms"] = normalized["typing_char_delay_min_ms"]
    if normalized["typing_micro_pause_max_ms"] < normalized["typing_micro_pause_min_ms"]:
        normalized["typing_micro_pause_max_ms"] = normalized["typing_micro_pause_min_ms"]
    if normalized["send_pre_delay_max_ms"] < normalized["send_pre_delay_min_ms"]:
        normalized["send_pre_delay_max_ms"] = normalized["send_pre_delay_min_ms"]
    if normalized["send_post_input_delay_max_ms"] < normalized["send_post_input_delay_min_ms"]:
        normalized["send_post_input_delay_max_ms"] = normalized["send_post_input_delay_min_ms"]
    if normalized["send_trigger_delay_max_ms"] < normalized["send_trigger_delay_min_ms"]:
        normalized["send_trigger_delay_max_ms"] = normalized["send_trigger_delay_min_ms"]
    if normalized["send_after_trigger_delay_max_ms"] < normalized["send_after_trigger_delay_min_ms"]:
        normalized["send_after_trigger_delay_max_ms"] = normalized["send_after_trigger_delay_min_ms"]
    return normalized


def apply_rpa_humanized_send_env(env: dict[str, str], settings: dict[str, Any]) -> dict[str, str]:
    merged = dict(env)
    mapping = {
        "WECHAT_WIN32_OCR_HUMANIZED_INPUT_ENABLED": "enabled",
        "WECHAT_WIN32_OCR_HUMANIZED_INPUT_METHOD": "input_method",
        "WECHAT_WIN32_OCR_HUMANIZED_TYPING_CHUNK_MIN_CHARS": "typing_chunk_min_chars",
        "WECHAT_WIN32_OCR_HUMANIZED_TYPING_CHUNK_MAX_CHARS": "typing_chunk_max_chars",
        "WECHAT_WIN32_OCR_HUMANIZED_TYPING_CHAR_DELAY_MIN_MS": "typing_char_delay_min_ms",
        "WECHAT_WIN32_OCR_HUMANIZED_TYPING_CHAR_DELAY_MAX_MS": "typing_char_delay_max_ms",
        "WECHAT_WIN32_OCR_HUMANIZED_TYPING_MICRO_PAUSE_EVERY_CHARS": "typing_micro_pause_every_chars",
        "WECHAT_WIN32_OCR_HUMANIZED_TYPING_MICRO_PAUSE_MIN_MS": "typing_micro_pause_min_ms",
        "WECHAT_WIN32_OCR_HUMANIZED_TYPING_MICRO_PAUSE_MAX_MS": "typing_micro_pause_max_ms",
        "WECHAT_WIN32_OCR_HUMANIZED_TYPING_TYPO_PROBABILITY": "typing_typo_probability",
        "WECHAT_WIN32_OCR_HUMANIZED_TYPING_TYPO_MAX": "typing_typo_max",
        "WECHAT_WIN32_OCR_HUMANIZED_SEND_PRE_DELAY_MIN_MS": "send_pre_delay_min_ms",
        "WECHAT_WIN32_OCR_HUMANIZED_SEND_PRE_DELAY_MAX_MS": "send_pre_delay_max_ms",
        "WECHAT_WIN32_OCR_HUMANIZED_SEND_POST_INPUT_DELAY_MIN_MS": "send_post_input_delay_min_ms",
        "WECHAT_WIN32_OCR_HUMANIZED_SEND_POST_INPUT_DELAY_MAX_MS": "send_post_input_delay_max_ms",
        "WECHAT_WIN32_OCR_HUMANIZED_SEND_TRIGGER_DELAY_MIN_MS": "send_trigger_delay_min_ms",
        "WECHAT_WIN32_OCR_HUMANIZED_SEND_TRIGGER_DELAY_MAX_MS": "send_trigger_delay_max_ms",
        "WECHAT_WIN32_OCR_HUMANIZED_SEND_AFTER_TRIGGER_DELAY_MIN_MS": "send_after_trigger_delay_min_ms",
        "WECHAT_WIN32_OCR_HUMANIZED_SEND_AFTER_TRIGGER_DELAY_MAX_MS": "send_after_trigger_delay_max_ms",
        "WECHAT_WIN32_OCR_HUMANIZED_ADAPTIVE_SPEED_ENABLED": "adaptive_speed_enabled",
        "WECHAT_WIN32_OCR_FAST_SEND_CONFIRMATION": "fast_send_confirmation_enabled",
        "WECHAT_WIN32_OCR_INPUT_FAST_VISUAL_CONFIRM": "input_fast_visual_confirm_enabled",
        "WECHAT_WIN32_OCR_SEND_TRIGGER_MODE": "send_trigger_mode",
        "WECHAT_WIN32_OCR_SEND_INPUT_CONFIRM_ATTEMPTS": "send_input_confirm_attempts",
        "WECHAT_WIN32_OCR_SEND_MIN_INTERVAL_SECONDS": "send_rate_min_interval_seconds",
        "WECHAT_WIN32_OCR_SEND_BURST_WINDOW_SECONDS": "send_rate_burst_window_seconds",
        "WECHAT_WIN32_OCR_SEND_BURST_LIMIT": "send_rate_burst_limit",
    }
    for env_name, key in mapping.items():
        value = settings.get(key)
        if isinstance(value, bool):
            merged[env_name] = "1" if value else "0"
        else:
            merged[env_name] = str(value)
    return merged


def apply_rpa_humanized_send_runtime_env(settings: dict[str, Any]) -> None:
    """Keep in-process scheduler sends consistent with listener subprocess env."""
    runtime_env = apply_rpa_humanized_send_env({}, settings)
    for env_name, value in runtime_env.items():
        os.environ[env_name] = str(value)


def normalize_operator_guard_settings(settings: dict[str, Any] | None) -> dict[str, Any]:
    source = settings if isinstance(settings, dict) else {}
    control_hotkey = str(source.get("control_hotkey") or OPERATOR_GUARD_DEFAULTS["control_hotkey"]).strip().lower()
    if control_hotkey not in {"f8", "esc"}:
        control_hotkey = str(OPERATOR_GUARD_DEFAULTS["control_hotkey"])
    normalized = {
        "enabled": bool(source.get("enabled", OPERATOR_GUARD_DEFAULTS["enabled"])),
        "block_manual_input": bool(source.get("block_manual_input", OPERATOR_GUARD_DEFAULTS["block_manual_input"])),
        "floating_indicator_enabled": bool(
            source.get("floating_indicator_enabled", OPERATOR_GUARD_DEFAULTS["floating_indicator_enabled"])
        ),
        "control_hotkey": control_hotkey,
        "esc_double_press_window_ms": bounded_int(
            source.get("esc_double_press_window_ms"),
            default=int(OPERATOR_GUARD_DEFAULTS["esc_double_press_window_ms"]),
            minimum=180,
            maximum=1200,
        ),
        "pause_poll_interval_ms": bounded_int(
            source.get("pause_poll_interval_ms"),
            default=int(OPERATOR_GUARD_DEFAULTS["pause_poll_interval_ms"]),
            minimum=120,
            maximum=3000,
        ),
        "bootstrap_timeout_seconds": min(
            60.0,
            max(
                3.0,
                float(
                    non_negative_float(
                        source.get("bootstrap_timeout_seconds"),
                        float(OPERATOR_GUARD_DEFAULTS["bootstrap_timeout_seconds"]),
                    )
                ),
            ),
        ),
    }
    return normalized


def load_operator_guard_settings(config_path: Path) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception:
        raw = {}
    if isinstance(raw, dict):
        candidate = raw.get("rpa_operator_guard")
        if isinstance(candidate, dict):
            payload = dict(candidate)
    settings = normalize_operator_guard_settings(payload)
    settings["enabled"] = env_bool(
        "WECHAT_RPA_OPERATOR_GUARD_ENABLED",
        default=bool(settings.get("enabled", OPERATOR_GUARD_DEFAULTS["enabled"])),
    )
    settings["block_manual_input"] = env_bool(
        "WECHAT_RPA_OPERATOR_GUARD_BLOCK_MANUAL_INPUT",
        default=bool(settings.get("block_manual_input", OPERATOR_GUARD_DEFAULTS["block_manual_input"])),
    )
    settings["floating_indicator_enabled"] = env_bool(
        "WECHAT_RPA_OPERATOR_GUARD_FLOATING_INDICATOR_ENABLED",
        default=bool(
            settings.get("floating_indicator_enabled", OPERATOR_GUARD_DEFAULTS["floating_indicator_enabled"])
        ),
    )
    control_hotkey = str(
        os.getenv("WECHAT_RPA_OPERATOR_GUARD_CONTROL_HOTKEY")
        or settings.get("control_hotkey")
        or OPERATOR_GUARD_DEFAULTS["control_hotkey"]
    ).strip().lower()
    if control_hotkey not in {"f8", "esc"}:
        control_hotkey = str(OPERATOR_GUARD_DEFAULTS["control_hotkey"])
    settings["control_hotkey"] = control_hotkey
    settings["esc_double_press_window_ms"] = bounded_int(
        os.getenv("WECHAT_RPA_OPERATOR_GUARD_ESC_DOUBLE_WINDOW_MS"),
        default=int(settings.get("esc_double_press_window_ms") or OPERATOR_GUARD_DEFAULTS["esc_double_press_window_ms"]),
        minimum=180,
        maximum=1200,
    )
    settings["pause_poll_interval_ms"] = bounded_int(
        os.getenv("WECHAT_RPA_OPERATOR_GUARD_PAUSE_POLL_INTERVAL_MS"),
        default=int(settings.get("pause_poll_interval_ms") or OPERATOR_GUARD_DEFAULTS["pause_poll_interval_ms"]),
        minimum=120,
        maximum=3000,
    )
    settings["bootstrap_timeout_seconds"] = min(
        60.0,
        max(
            3.0,
            float(
                non_negative_float(
                    os.getenv("WECHAT_RPA_OPERATOR_GUARD_BOOTSTRAP_TIMEOUT_SECONDS"),
                    float(
                        settings.get("bootstrap_timeout_seconds")
                        or OPERATOR_GUARD_DEFAULTS["bootstrap_timeout_seconds"]
                    ),
                )
            ),
        ),
    )
    return settings


def empty_operator_control_state(tenant_id: str, *, mode: str = "running") -> dict[str, Any]:
    normalized_mode = mode if mode in OPERATOR_CONTROL_MODES else "running"
    return {
        "version": 1,
        "tenant_id": tenant_id,
        "mode": normalized_mode,
        "command": {
            "id": 0,
            "action": "none",
            "status": "idle",
            "source": "",
            "requested_at": "",
            "applied_at": "",
            "message": "",
        },
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }


def read_operator_control_state(path: Path, *, tenant_id: str) -> dict[str, Any]:
    fallback = empty_operator_control_state(tenant_id)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return fallback
    if not isinstance(payload, dict):
        return fallback
    merged = dict(fallback)
    merged.update(payload)
    merged["tenant_id"] = tenant_id
    mode = str(merged.get("mode") or "running").strip().lower()
    if mode not in OPERATOR_CONTROL_MODES:
        mode = "running"
    merged["mode"] = mode
    command = merged.get("command") if isinstance(merged.get("command"), dict) else {}
    normalized_command = dict(fallback["command"])
    normalized_command.update(command)
    try:
        command_id = int(normalized_command.get("id") or 0)
    except (TypeError, ValueError):
        command_id = 0
    normalized_command["id"] = max(0, command_id)
    action = str(normalized_command.get("action") or "none").strip().lower()
    if action not in OPERATOR_COMMAND_ACTIONS and action != "none":
        action = "none"
    normalized_command["action"] = action
    status = str(normalized_command.get("status") or "idle").strip().lower()
    if status not in {"idle", "pending", "applied", "ignored", "rejected"}:
        status = "idle"
    normalized_command["status"] = status
    merged["command"] = normalized_command
    return merged


def write_operator_control_state(path: Path, payload: dict[str, Any]) -> None:
    output = dict(payload)
    output["updated_at"] = datetime.now().isoformat(timespec="seconds")
    atomic_write_json(path, output)


def sync_operator_mode(path: Path, *, tenant_id: str, mode: str, message: str = "") -> dict[str, Any]:
    payload = read_operator_control_state(path, tenant_id=tenant_id)
    payload["mode"] = mode if mode in OPERATOR_CONTROL_MODES else "running"
    command = payload.get("command") if isinstance(payload.get("command"), dict) else {}
    if message:
        status = str(command.get("status") or "").strip().lower()
        if status in {"", "idle", "ignored", "rejected"} or not str(command.get("message") or "").strip():
            command["message"] = message
    payload["command"] = command
    write_operator_control_state(path, payload)
    return payload


def apply_operator_command(
    payload: dict[str, Any],
    *,
    action: str,
    message: str,
) -> dict[str, Any]:
    updated = dict(payload)
    command = updated.get("command") if isinstance(updated.get("command"), dict) else {}
    command["status"] = "applied"
    command["applied_at"] = datetime.now().isoformat(timespec="seconds")
    command["message"] = message
    normalized_action = action if action in OPERATOR_COMMAND_ACTIONS else "pause"
    if normalized_action == "pause":
        updated["mode"] = "paused"
    elif normalized_action == "resume":
        updated["mode"] = "running"
    elif normalized_action == "stop":
        updated["mode"] = "stopped"
    updated["command"] = command
    return updated


def bounded_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def launch_operator_guard(
    *,
    tenant_id: str,
    settings: dict[str, Any],
    control_path: Path,
    status_path: Path,
    state_path: Path,
    pid_path: Path,
    parent_pid: int | None = None,
    local_safety_stop_path: str | None = None,
) -> dict[str, Any]:
    if os.name != "nt":
        return {"ok": False, "enabled": False, "reason": "windows_only"}
    script_path = APP_ROOT / "scripts" / "run_rpa_operator_guard.py"
    if not script_path.exists():
        return {"ok": False, "enabled": True, "reason": "guard_script_missing", "script_path": str(script_path)}
    command = [
        str(sys.executable),
        str(script_path),
        "--tenant-id",
        tenant_id,
        "--control-path",
        str(control_path),
        "--status-path",
        str(status_path),
        "--guard-state-path",
        str(state_path),
        "--parent-pid",
        str(int(parent_pid or os.getpid())),
        "--control-key",
        str(settings.get("control_hotkey") or OPERATOR_GUARD_DEFAULTS["control_hotkey"]),
        "--esc-double-window-ms",
        str(int(settings.get("esc_double_press_window_ms") or OPERATOR_GUARD_DEFAULTS["esc_double_press_window_ms"])),
        "--pause-poll-interval-ms",
        str(int(settings.get("pause_poll_interval_ms") or OPERATOR_GUARD_DEFAULTS["pause_poll_interval_ms"])),
    ]
    if local_safety_stop_path:
        command.extend(["--local-safety-stop-path", str(local_safety_stop_path)])
    if settings.get("block_manual_input", True):
        command.append("--block-manual-input")
    else:
        command.append("--allow-manual-input")
    if settings.get("floating_indicator_enabled", True):
        command.append("--floating-indicator")
    else:
        command.append("--no-floating-indicator")
    creationflags = 0
    if os.name == "nt":
        creationflags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        creationflags |= getattr(subprocess, "CREATE_NO_WINDOW", 0)
    stdout_log_path = pid_path.with_name("operator_guard.stdout.log")
    stderr_log_path = pid_path.with_name("operator_guard.stderr.log")
    stdout_log_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        stdout_handle = stdout_log_path.open("ab")
        stderr_handle = stderr_log_path.open("ab")
    except OSError as exc:
        return {
            "ok": False,
            "enabled": True,
            "reason": "guard_log_open_failed",
            "error": repr(exc),
            "stdout_log_path": str(stdout_log_path),
            "stderr_log_path": str(stderr_log_path),
        }
    try:
        proc = subprocess.Popen(
            command,
            cwd=str(PROJECT_ROOT),
            env=dict(os.environ),
            stdin=subprocess.DEVNULL,
            stdout=stdout_handle,
            stderr=stderr_handle,
            creationflags=creationflags,
        )
    except Exception as exc:
        return {
            "ok": False,
            "enabled": True,
            "reason": "guard_process_launch_failed",
            "error": repr(exc),
            "stdout_log_path": str(stdout_log_path),
            "stderr_log_path": str(stderr_log_path),
        }
    finally:
        try:
            stdout_handle.close()
        except Exception:
            pass
        try:
            stderr_handle.close()
        except Exception:
            pass
    record = {
        "pid": proc.pid,
        "tenant_id": tenant_id,
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "control_path": str(control_path),
        "status_path": str(status_path),
        "state_path": str(state_path),
        "stdout_log_path": str(stdout_log_path),
        "stderr_log_path": str(stderr_log_path),
        "parent_pid": int(parent_pid or os.getpid()),
    }
    atomic_write_json(pid_path, record)
    return {
        "ok": True,
        "enabled": True,
        "pid": proc.pid,
        "script_path": str(script_path),
        "stdout_log_path": str(stdout_log_path),
        "stderr_log_path": str(stderr_log_path),
    }


def read_operator_guard_pid(path: Path) -> int:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0
    if not isinstance(payload, dict):
        return 0
    try:
        return max(0, int(payload.get("pid") or 0))
    except (TypeError, ValueError):
        return 0


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        proc = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return False
    if not proc.is_running():
        return False
    try:
        return proc.status() != psutil.STATUS_ZOMBIE
    except Exception:
        return True


def pid_is_descendant_of(pid: int, ancestor_pid: int) -> bool:
    if pid <= 0 or ancestor_pid <= 0 or pid == ancestor_pid:
        return pid == ancestor_pid and pid > 0
    try:
        current = psutil.Process(pid)
        return any(parent.pid == ancestor_pid for parent in current.parents())
    except Exception:
        return False


def read_operator_guard_state(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def verify_operator_guard_bootstrap(
    pid: int,
    state_path: Path,
    *,
    timeout_seconds: float = float(OPERATOR_GUARD_DEFAULTS["bootstrap_timeout_seconds"]),
    expected_parent_pid: int | None = None,
) -> dict[str, Any]:
    def _state_matches_current_launch(snapshot: dict[str, Any]) -> tuple[bool, int, int]:
        try:
            state_pid = int(snapshot.get("pid") or 0)
        except (TypeError, ValueError):
            state_pid = 0
        try:
            state_parent_pid = int(snapshot.get("parent_pid") or 0)
        except (TypeError, ValueError):
            state_parent_pid = 0
        expected_parent = int(expected_parent_pid or 0)
        matches = (
            state_pid == pid
            or (expected_parent > 0 and state_parent_pid == expected_parent)
            or pid_is_descendant_of(state_pid, pid)
        )
        return matches, state_pid, state_parent_pid

    started = time.monotonic()
    last_state: dict[str, Any] = {}
    while time.monotonic() - started <= max(0.2, float(timeout_seconds)):
        snapshot = read_operator_guard_state(state_path)
        if snapshot:
            last_state = snapshot
            matches_launch, _, _ = _state_matches_current_launch(snapshot)
            if not matches_launch:
                time.sleep(0.08)
                continue
            phase = str(snapshot.get("phase") or "").strip().lower()
            hooks_installed = bool(snapshot.get("hooks_installed"))
            if phase == "failed" or hooks_installed:
                break
        if not pid_alive(pid):
            matches_launch, _, _ = _state_matches_current_launch(snapshot) if snapshot else (False, 0, 0)
            if not matches_launch:
                return {"ok": False, "reason": "guard_process_exited_early", "pid": pid, "state": last_state}
        time.sleep(0.08)
    if not last_state:
        return {
            "ok": False,
            "reason": "guard_state_missing",
            "pid": pid,
            "state": last_state,
            "process_alive": pid_alive(pid),
            "timeout_seconds": float(timeout_seconds),
        }
    matches_launch, final_state_pid, final_parent_pid = _state_matches_current_launch(last_state)
    if not matches_launch:
        return {
            "ok": False,
            "reason": "guard_state_pid_mismatch",
            "pid": pid,
            "state_pid": final_state_pid,
            "state_parent_pid": final_parent_pid,
            "expected_parent_pid": int(expected_parent_pid or 0),
            "state": last_state,
        }
    phase = str(last_state.get("phase") or "").strip().lower()
    hooks_installed = bool(last_state.get("hooks_installed"))
    if phase == "failed" or not hooks_installed:
        return {
            "ok": False,
            "reason": "guard_hook_not_ready",
            "pid": pid,
            "state": last_state,
            "timeout_seconds": float(timeout_seconds),
        }
    return {
        "ok": True,
        "reason": "guard_ready",
        "pid": pid,
        "state_pid": final_state_pid,
        "state_parent_pid": final_parent_pid,
        "state": last_state,
    }


def terminate_pid(pid: int) -> None:
    if pid <= 0:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return
    try:
        os.kill(pid, 15)
    except OSError:
        pass


def clear_file(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def transport_risk_state_path(tenant_id: str) -> Path:
    return runtime_dir(tenant_id) / "transport_risk_guard_state.json"


def workflow_phase_log_path(tenant_id: str) -> Path:
    return runtime_dir(tenant_id) / "listener_phase_heartbeat.jsonl"


def read_latest_phase_heartbeat(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return {}
    for line in reversed(lines[-80:]):
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    return {}


def empty_transport_risk_guard_state() -> dict[str, Any]:
    return {
        "login_hits": [],
        "hard_block_hits": [],
        "send_input_not_ready_hits": [],
        "abnormal_window_hits": [],
        "passive_logout_probe_hits": [],
        "passive_logout_probe_last_at": 0.0,
        "passive_logout_probe_failures": [],
    }


def read_transport_risk_guard_state(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return empty_transport_risk_guard_state()
    if not isinstance(payload, dict):
        return empty_transport_risk_guard_state()
    normalized = empty_transport_risk_guard_state()
    normalized.update(payload)
    return normalized


def write_transport_risk_guard_state(path: Path, payload: dict[str, Any]) -> None:
    atomic_write_json(path, payload)


def iter_dict_nodes(value: Any) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []
    if isinstance(value, dict):
        nodes.append(value)
        for child in value.values():
            nodes.extend(iter_dict_nodes(child))
    elif isinstance(value, list):
        for item in value:
            nodes.extend(iter_dict_nodes(item))
    return nodes


def extract_transport_risk_signals(result: dict[str, Any]) -> dict[str, Any]:
    nodes = iter_dict_nodes(result)
    states: set[str] = set()
    reasons: set[str] = set()
    login_detected = False
    hard_block_detected = False
    send_input_not_ready = False
    abnormal_window_detected = False
    for node in nodes:
        state = str(node.get("state") or "").strip()
        reason = str(node.get("reason") or "").strip()
        send_state = str(node.get("send_state") or "").strip()
        blocking_reason = str(node.get("blocking_reason") or "").strip()
        error_text = str(node.get("error") or "").strip()
        quick_login = node.get("quick_login")
        risk_stop_recommended = bool(node.get("risk_stop_recommended") or node.get("stop_recommended"))
        if state:
            states.add(state)
        if reason:
            reasons.add(reason)
        if send_state:
            states.add(send_state)
        if isinstance(quick_login, dict) and quick_login.get("detected"):
            login_detected = True
        if state in TRANSPORT_ABNORMAL_WINDOW_STATES or send_state in TRANSPORT_ABNORMAL_WINDOW_STATES:
            abnormal_window_detected = True
        if reason in TRANSPORT_ABNORMAL_WINDOW_REASONS or blocking_reason in TRANSPORT_ABNORMAL_WINDOW_REASONS:
            abnormal_window_detected = True
        if state in TRANSPORT_LOGIN_STATES or send_state in TRANSPORT_LOGIN_STATES:
            login_detected = True
        if reason == "login_or_qr" or blocking_reason == "login_or_qr":
            login_detected = True
        if "quick-login view detected" in error_text.lower():
            login_detected = True
        if state in TRANSPORT_SEND_INPUT_STATES or send_state in TRANSPORT_SEND_INPUT_STATES:
            send_input_not_ready = True
        if reason in TRANSPORT_SEND_INPUT_REASONS:
            send_input_not_ready = True
        if reason.startswith("blocking_text:"):
            token = reason.split(":", 1)[1]
            if token in TRANSPORT_HARD_BLOCK_TOKENS:
                hard_block_detected = True
        if any(token in error_text for token in TRANSPORT_HARD_BLOCK_TOKENS):
            hard_block_detected = True
        if risk_stop_recommended:
            hard_block_detected = True
        if state == "win32_ocr_failed" and any(token.lower() in error_text.lower() for token in TRANSPORT_INVALID_WINDOW_TOKENS):
            hard_block_detected = True
    return {
        "states": sorted(states),
        "reasons": sorted(reasons),
        "login_detected": login_detected,
        "hard_block_detected": hard_block_detected,
        "send_input_not_ready": send_input_not_ready,
        "abnormal_window_detected": abnormal_window_detected,
    }


def runtime_target_observations(result: dict[str, Any]) -> list[dict[str, Any]]:
    observations: list[dict[str, Any]] = []
    if not isinstance(result, dict):
        return observations
    for index, event in enumerate(result.get("events", []) or []):
        if not isinstance(event, dict):
            continue
        target = str(event.get("target") or event.get("target_name") or "").strip()
        if target:
            observations.append(
                {
                    "source": "events",
                    "index": index,
                    "target": target,
                    "action": event.get("action"),
                    "reason": event.get("reason"),
                }
            )
    for index, signal in enumerate(result.get("active_session_signals", []) or []):
        if not isinstance(signal, dict):
            continue
        target = str(signal.get("target") or signal.get("target_name") or signal.get("name") or "").strip()
        if target:
            observations.append(
                {
                    "source": "active_session_signals",
                    "index": index,
                    "target": target,
                    "action": signal.get("action"),
                    "reason": signal.get("reason"),
                }
            )
    return observations


def evaluate_runtime_target_guard(
    result: dict[str, Any],
    *,
    settings: dict[str, Any],
) -> dict[str, Any]:
    active = normalize_runtime_target_guard_settings(settings)
    observations = runtime_target_observations(result if isinstance(result, dict) else {})
    if not active.get("enabled"):
        return {"enabled": False, "ok": True, "stop": False, "observations": observations}
    allowed = set(active.get("allowed_targets") or [])
    if not allowed:
        return {
            "enabled": True,
            "ok": False,
            "stop": True,
            "reason": "runtime_disallowed_target_detected",
            "message": "监听运行时目标白名单为空，已自动停机保护，避免误入非测试会话。",
            "allowed_targets": [],
            "observations": observations,
            "disallowed_targets": [],
        }
    disallowed = [item for item in observations if str(item.get("target") or "").strip() not in allowed]
    if disallowed:
        names = sorted({str(item.get("target") or "").strip() for item in disallowed if str(item.get("target") or "").strip()})
        return {
            "enabled": True,
            "ok": False,
            "stop": True,
            "reason": "runtime_disallowed_target_detected",
            "message": "检测到监听运行中进入非白名单会话，已自动停机保护，避免错会话回复或继续触发微信风控。",
            "allowed_targets": sorted(allowed),
            "observations": observations,
            "disallowed_targets": names,
            "disallowed_observations": disallowed,
        }
    return {
        "enabled": True,
        "ok": True,
        "stop": False,
        "allowed_targets": sorted(allowed),
        "observations": observations,
        "disallowed_targets": [],
    }


def _prune_hits(values: list[Any], *, now_ts: float, window_seconds: int) -> list[float]:
    normalized: list[float] = []
    for item in values:
        try:
            ts = float(item)
        except (TypeError, ValueError):
            continue
        if now_ts - ts <= float(window_seconds):
            normalized.append(ts)
    return normalized


def evaluate_transport_risk(
    result: dict[str, Any],
    *,
    guard_state: dict[str, Any] | None,
    settings: dict[str, Any],
    now_ts: float | None = None,
) -> dict[str, Any]:
    active_settings = normalize_transport_risk_settings(settings)
    if active_settings.get("enabled") is False:
        return {
            "enabled": False,
            "stop": False,
            "state": guard_state or {},
            "signals": extract_transport_risk_signals(result if isinstance(result, dict) else {}),
        }
    tick = float(now_ts or time.time())
    signals = extract_transport_risk_signals(result if isinstance(result, dict) else {})
    state = dict(guard_state) if isinstance(guard_state, dict) else {}
    window_seconds = int(active_settings.get("counter_window_seconds") or 600)
    login_hits = _prune_hits(state.get("login_hits", []), now_ts=tick, window_seconds=window_seconds)
    hard_block_hits = _prune_hits(state.get("hard_block_hits", []), now_ts=tick, window_seconds=window_seconds)
    send_hits = _prune_hits(state.get("send_input_not_ready_hits", []), now_ts=tick, window_seconds=window_seconds)
    abnormal_window_hits = _prune_hits(state.get("abnormal_window_hits", []), now_ts=tick, window_seconds=window_seconds)
    if signals.get("login_detected"):
        login_hits.append(tick)
    if signals.get("hard_block_detected"):
        hard_block_hits.append(tick)
    if signals.get("send_input_not_ready"):
        send_hits.append(tick)
    if signals.get("abnormal_window_detected"):
        abnormal_window_hits.append(tick)
    state.update(
        {
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "window_seconds": window_seconds,
            "login_hits": login_hits[-50:],
            "hard_block_hits": hard_block_hits[-50:],
            "send_input_not_ready_hits": send_hits[-200:],
            "abnormal_window_hits": abnormal_window_hits[-50:],
            "last_signals": signals,
        }
    )
    login_threshold = int(active_settings.get("login_detect_stop_threshold") or 1)
    hard_block_threshold = int(active_settings.get("hard_block_stop_threshold") or 1)
    send_threshold = int(active_settings.get("send_input_not_ready_stop_threshold") or 3)
    warning_cooldown_seconds = non_negative_int(
        active_settings.get("warning_cooldown_seconds"),
        TRANSPORT_RISK_DEFAULTS["warning_cooldown_seconds"],
    )
    cooldown_near_threshold = positive_int(
        active_settings.get("cooldown_near_threshold"),
        TRANSPORT_RISK_DEFAULTS["cooldown_near_threshold"],
    )
    login_signal = bool(signals.get("login_detected"))
    hard_block_signal = bool(signals.get("hard_block_detected"))
    send_input_signal = bool(signals.get("send_input_not_ready"))
    abnormal_window_signal = bool(signals.get("abnormal_window_detected"))
    stop = False
    stop_reason = ""
    stop_message = ""
    if abnormal_window_signal:
        stop = True
        stop_reason = "wechat_abnormal_window_detected"
        stop_message = "检测到微信窗口白屏、辅助壳、离屏或异常渲染，已自动停机保护。请人工恢复正常微信主窗口后再启动。"
    elif hard_block_signal and len(hard_block_hits) >= hard_block_threshold:
        stop = True
        stop_reason = "wechat_hard_block_detected"
        stop_message = "检测到微信安全阻塞（如操作频繁/登录环境异常），已自动停机保护，避免触发更高风控。"
    elif login_signal and len(login_hits) >= login_threshold:
        stop = True
        stop_reason = "wechat_login_window_detected"
        stop_message = "检测到微信掉线或登录页，已自动停机保护。请人工重新登录后再启动自动客服。"
    elif send_input_signal and len(send_hits) >= send_threshold:
        stop = True
        stop_reason = "wechat_send_input_not_ready_repeated"
        stop_message = "连续多次发送前输入确认失败，已自动停机保护，避免重复操作触发微信风控。"
    cooldown_seconds = 0
    if not stop:
        if send_input_signal:
            near_threshold = max(1, send_threshold - max(1, cooldown_near_threshold) + 1)
            if len(send_hits) >= near_threshold:
                cooldown_seconds = max(cooldown_seconds, warning_cooldown_seconds)
            elif warning_cooldown_seconds > 0:
                cooldown_seconds = max(cooldown_seconds, min(60, max(15, warning_cooldown_seconds // 2)))
        if login_signal or hard_block_signal:
            cooldown_seconds = max(cooldown_seconds, warning_cooldown_seconds)
    verdict = {
        "enabled": True,
        "stop": stop,
        "reason": stop_reason,
        "message": stop_message,
        "cooldown_seconds": cooldown_seconds,
        "loop_jitter_seconds": non_negative_float(
            active_settings.get("loop_jitter_seconds"),
            TRANSPORT_RISK_DEFAULTS["loop_jitter_seconds"],
        ),
        "signals": signals,
        "state": state,
        "thresholds": {
            "login": login_threshold,
            "hard_block": hard_block_threshold,
            "send_input_not_ready": send_threshold,
        },
    }
    return verdict


def run_passive_logout_probe(
    *,
    env: dict[str, str],
    timeout_seconds: float,
) -> dict[str, Any]:
    probe_started = time.time()
    python_bin = WIN32_OCR_SIDECAR_PYTHON if WIN32_OCR_SIDECAR_PYTHON.exists() else Path(sys.executable)
    if not WIN32_OCR_SIDECAR_SCRIPT.exists():
        return {
            "attempted": False,
            "ok": False,
            "reason": "sidecar_script_missing",
            "sidecar_script": str(WIN32_OCR_SIDECAR_SCRIPT),
        }
    probe_env = dict(env)
    probe_env["PYTHONUTF8"] = "1"
    probe_env["PYTHONIOENCODING"] = "utf-8"
    probe_env["WECHAT_WIN32_OCR_PASSIVE_PROBE"] = "1"
    probe_env["WECHAT_WIN32_OCR_WINDOW_NORMALIZE"] = "0"
    probe_env["WECHAT_WIN32_OCR_QUICK_LOGIN_AUTO_ENTER"] = "0"
    command = [str(python_bin), str(WIN32_OCR_SIDECAR_SCRIPT), "status"]
    process = subprocess.Popen(
        command,
        cwd=str(PROJECT_ROOT),
        env=probe_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    timed_out = False
    try:
        stdout_value, stderr_value = process.communicate(timeout=max(1.0, float(timeout_seconds)))
    except subprocess.TimeoutExpired:
        timed_out = True
        terminate_process_tree(process)
        stdout_value, stderr_value = process.communicate()
    stdout = str(stdout_value or "").strip()
    stderr = str(stderr_value or "").strip()
    payload = parse_last_json(stdout)
    if not isinstance(payload, dict):
        payload = {}
    state = str(payload.get("state") or "").strip()
    reason = str(payload.get("reason") or "").strip()
    error_text = str(payload.get("error") or "").strip()
    quick_login = payload.get("window_probe", {}).get("quick_login") if isinstance(payload.get("window_probe"), dict) else {}
    quick_login_detected = isinstance(quick_login, dict) and bool(quick_login.get("detected"))
    login_detected = False
    blank_detected = False
    auxiliary_shell_detected = False
    detection_reason = ""
    if state in {"login_window_detected", "wechat_not_ready", "main_window_not_found"}:
        login_detected = True
        detection_reason = state
    elif state in {"blank_render_detected"}:
        blank_detected = True
        detection_reason = state
    elif state in {"auxiliary_shell_window_detected"}:
        auxiliary_shell_detected = True
        detection_reason = state
    elif reason == "login_or_qr":
        login_detected = True
        detection_reason = "login_or_qr"
    elif reason == "blank_render":
        blank_detected = True
        detection_reason = "blank_render"
    elif reason == "auxiliary_shell_window":
        auxiliary_shell_detected = True
        detection_reason = "auxiliary_shell_window"
    elif quick_login_detected:
        login_detected = True
        detection_reason = "quick_login_detected"
    elif "quick-login view detected" in error_text.lower():
        login_detected = True
        detection_reason = "quick_login_error_text"
    login_detected = bool(login_detected or blank_detected or auxiliary_shell_detected)
    return {
        "attempted": True,
        "ok": bool(payload.get("ok")) and not timed_out,
        "timed_out": timed_out,
        "duration_seconds": round(time.time() - probe_started, 3),
        "command": command,
        "status_payload": payload,
        "stderr_tail": stderr[-600:],
        "stdout_tail": stdout[-600:],
        "login_detected": login_detected,
        "blank_detected": blank_detected,
        "auxiliary_shell_detected": auxiliary_shell_detected,
        "detection_reason": detection_reason,
    }


def run_interactive_rpa_calibration(
    *,
    env: dict[str, str],
    timeout_seconds: float,
    reason: str,
) -> dict[str, Any]:
    """Run one foreground-safe WeChat window calibration pass.

    This is intentionally event-triggered rather than loop-triggered: startup,
    resume, or passive-listener failure. Normal idle polling stays passive.
    """
    started = time.time()
    python_bin = WIN32_OCR_SIDECAR_PYTHON if WIN32_OCR_SIDECAR_PYTHON.exists() else Path(sys.executable)
    if not WIN32_OCR_SIDECAR_SCRIPT.exists():
        return {
            "attempted": False,
            "ok": False,
            "reason": "sidecar_script_missing",
            "calibration_reason": reason,
            "sidecar_script": str(WIN32_OCR_SIDECAR_SCRIPT),
        }
    probe_env = dict(env)
    probe_env["PYTHONUTF8"] = "1"
    probe_env["PYTHONIOENCODING"] = "utf-8"
    probe_env["WECHAT_WIN32_OCR_PASSIVE_PROBE"] = "0"
    probe_env["WECHAT_WIN32_OCR_WINDOW_NORMALIZE"] = "1"
    probe_env["WECHAT_WIN32_OCR_QUICK_LOGIN_AUTO_ENTER"] = "0"
    probe_env["WECHAT_WIN32_OCR_AGGRESSIVE_FOCUS"] = "1"
    probe_env["WECHAT_WIN32_OCR_ATTACH_THREAD_INPUT"] = "1"
    probe_env["WECHAT_WIN32_OCR_ACTIVATE_DEBOUNCE_SECONDS"] = "0"
    command = [str(python_bin), str(WIN32_OCR_SIDECAR_SCRIPT), "status"]
    process = subprocess.Popen(
        command,
        cwd=str(PROJECT_ROOT),
        env=probe_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    timed_out = False
    try:
        stdout_value, stderr_value = process.communicate(timeout=max(1.0, float(timeout_seconds)))
    except subprocess.TimeoutExpired:
        timed_out = True
        terminate_process_tree(process)
        stdout_value, stderr_value = process.communicate()
    stdout = str(stdout_value or "").strip()
    stderr = str(stderr_value or "").strip()
    payload = parse_last_json(stdout)
    if not isinstance(payload, dict):
        payload = {}
    focus_guard = payload.get("focus_guard") if isinstance(payload.get("focus_guard"), dict) else {}
    focus_reason = str(focus_guard.get("reason") or "")
    focus_ok = bool(focus_guard.get("ok")) and focus_reason in {"foreground_matches_target", "foreground_root_matches_target"}
    ok = bool(payload.get("ok")) and bool(payload.get("online")) and not timed_out and focus_ok
    return {
        "attempted": True,
        "ok": ok,
        "timed_out": timed_out,
        "duration_seconds": round(time.time() - started, 3),
        "command": command,
        "calibration_reason": reason,
        "status_payload": payload,
        "focus_guard": focus_guard,
        "focus_ok": focus_ok,
        "stderr_tail": stderr[-600:],
        "stdout_tail": stdout[-600:],
    }


def status_payload_confirms_wechat_readable(payload: dict[str, Any], *, settings: dict[str, Any]) -> bool:
    """Return True only when status is enough to keep startup alive safely.

    Startup calibration can be slower than passive OCR. A readable main window is
    enough to start idle listening, but not enough to bypass send-time focus checks.
    """
    if not isinstance(payload, dict) or not payload:
        return False
    signals = extract_transport_risk_signals(payload)
    if signals.get("login_detected") or signals.get("hard_block_detected") or signals.get("abnormal_window_detected"):
        return False
    if payload.get("ok") is not True or payload.get("online") is not True:
        return False
    state = str(payload.get("state") or "").strip()
    reason = str(payload.get("reason") or "").strip()
    if state in TRANSPORT_LOGIN_STATES or state in TRANSPORT_ABNORMAL_WINDOW_STATES:
        return False
    if reason in {"login_or_qr", "blank_render", "auxiliary_shell_window"}:
        return False
    try:
        ocr_count = int(payload.get("ocr_count") or 0)
    except (TypeError, ValueError):
        ocr_count = 0
    min_ocr_count = int(normalize_transport_risk_settings(settings).get("passive_probe_empty_ocr_min_count") or 1)
    return ocr_count >= min_ocr_count


def startup_interactive_calibration_can_defer(
    calibration: dict[str, Any],
    *,
    passive_probe: dict[str, Any] | None,
    settings: dict[str, Any],
) -> dict[str, Any]:
    """Decide whether a startup-only interactive calibration failure can be deferred.

    This avoids stopping the listener when WeChat is readable but foreground
    activation/normalization was just slow. Dangerous states still hard-stop via
    the passive probe and transport-risk verdict.
    """
    calibration_payload = calibration.get("status_payload") if isinstance(calibration.get("status_payload"), dict) else {}
    if status_payload_confirms_wechat_readable(calibration_payload, settings=settings):
        return {"ok": True, "reason": "interactive_status_readable"}
    probe_payload = (
        passive_probe.get("status_payload")
        if isinstance(passive_probe, dict) and isinstance(passive_probe.get("status_payload"), dict)
        else {}
    )
    if isinstance(passive_probe, dict) and passive_probe.get("ok") is True and status_payload_confirms_wechat_readable(
        probe_payload,
        settings=settings,
    ):
        return {"ok": True, "reason": "passive_probe_readable"}
    return {"ok": False, "reason": "wechat_not_readable"}


def evaluate_passive_logout_probe(
    probe: dict[str, Any],
    *,
    guard_state: dict[str, Any] | None,
    settings: dict[str, Any],
    now_ts: float | None = None,
) -> dict[str, Any]:
    active_settings = normalize_transport_risk_settings(settings)
    state = dict(guard_state) if isinstance(guard_state, dict) else empty_transport_risk_guard_state()
    tick = float(now_ts or time.time())
    window_seconds = int(active_settings.get("counter_window_seconds") or 600)
    login_hits = _prune_hits(state.get("passive_logout_probe_hits", []), now_ts=tick, window_seconds=window_seconds)
    failure_hits = _prune_hits(state.get("passive_logout_probe_failures", []), now_ts=tick, window_seconds=window_seconds)
    empty_ocr_failure = False
    if bool(probe.get("attempted")):
        state["passive_logout_probe_last_at"] = tick
        status_payload = probe.get("status_payload") if isinstance(probe.get("status_payload"), dict) else {}
        try:
            ocr_count = int(status_payload.get("ocr_count") or 0)
        except (TypeError, ValueError):
            ocr_count = 0
        empty_ocr_failure = (
            bool(active_settings.get("passive_probe_empty_ocr_fail_enabled", True))
            and probe.get("ok") is True
            and str(status_payload.get("state") or "") == "main_window_compat"
            and ocr_count < int(active_settings.get("passive_probe_empty_ocr_min_count") or 1)
        )
        if bool(probe.get("timed_out")) or probe.get("ok") is False or empty_ocr_failure:
            failure_hits.append(tick)
        if bool(probe.get("login_detected")):
            login_hits.append(tick)
    state["passive_logout_probe_hits"] = login_hits[-50:]
    state["passive_logout_probe_failures"] = failure_hits[-50:]
    state["updated_at"] = datetime.now().isoformat(timespec="seconds")
    threshold = int(active_settings.get("passive_logout_probe_fail_stop_threshold") or 1)
    stop = bool(probe.get("login_detected")) and len(login_hits) >= threshold
    message = ""
    reason = ""
    blank_detected = bool(probe.get("blank_detected"))
    auxiliary_shell_detected = bool(probe.get("auxiliary_shell_detected"))
    if stop:
        if auxiliary_shell_detected:
            reason = "wechat_auxiliary_shell_detected_by_passive_probe"
            message = "被动探针检测到微信辅助壳/白屏风险窗口，已自动停机保护，避免继续操作触发风控。请关闭异常微信窗口并确认主窗口正常后再启动监听。"
        elif blank_detected:
            reason = "wechat_blank_render_detected_by_passive_probe"
            message = "被动探针检测到微信界面白屏/空渲染，已自动停机保护，避免继续操作触发风控。请重启微信窗口后再启动监听。"
        else:
            reason = "wechat_logout_detected_by_passive_probe"
            message = "被动探针检测到微信掉线或登录页，已自动停机保护，避免继续操作触发风控。请人工登录后重启监听。"
    return {
        "enabled": bool(active_settings.get("passive_logout_probe_enabled", True)),
        "stop": stop,
        "reason": reason,
        "message": message,
        "threshold": threshold,
        "hits": len(login_hits),
        "failures": len(failure_hits),
        "empty_ocr_failure": bool(empty_ocr_failure),
        "probe": probe,
        "state": state,
    }


def passive_probe_recalibration_due(
    verdict: dict[str, Any],
    *,
    settings: dict[str, Any],
    now_ts: float | None = None,
) -> dict[str, Any]:
    active_settings = normalize_transport_risk_settings(settings)
    if not bool(active_settings.get("passive_probe_recalibrate_enabled", True)):
        return {"due": False, "reason": "disabled"}
    if bool(verdict.get("stop")):
        return {"due": False, "reason": "stop_verdict"}
    state = verdict.get("state") if isinstance(verdict.get("state"), dict) else {}
    failures = int(verdict.get("failures") or len(state.get("passive_logout_probe_failures") or []))
    threshold = int(active_settings.get("passive_probe_recalibrate_fail_threshold") or 2)
    if failures < threshold:
        return {"due": False, "reason": "below_threshold", "failures": failures, "threshold": threshold}
    tick = float(now_ts or time.time())
    try:
        last_at = float(state.get("last_interactive_calibration_at") or 0.0)
    except (TypeError, ValueError):
        last_at = 0.0
    cooldown = float(active_settings.get("passive_probe_recalibrate_cooldown_seconds") or 0.0)
    if last_at > 0 and tick - last_at < cooldown:
        return {
            "due": False,
            "reason": "cooldown",
            "failures": failures,
            "threshold": threshold,
            "cooldown_seconds": cooldown,
            "elapsed_seconds": round(tick - last_at, 3),
        }
    return {
        "due": True,
        "reason": "passive_probe_failure_threshold",
        "failures": failures,
        "threshold": threshold,
        "cooldown_seconds": cooldown,
    }


def runtime_handoff_case_id(*, tenant_id: str, source: str, reason: str, now_text: str | None = None) -> str:
    timestamp = str(now_text or datetime.now().isoformat(timespec="seconds"))
    bucket = timestamp[:13]
    seed = f"{tenant_id}:{source}:{reason}:{bucket}"
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:20]
    return f"handoff_runtime_transport_{digest}"


def create_runtime_transport_handoff_case(
    *,
    tenant_id: str,
    reason: str,
    message: str,
    source: str,
    verdict: dict[str, Any],
    listener_result: dict[str, Any] | None = None,
    target: str = "",
    handoff_path: Path | None = None,
    now_text: str | None = None,
    dispatch_handoff_fn: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Create a durable handoff case for unrecoverable WeChat transport states."""
    normalized_reason = str(reason or "").strip()
    normalized_source = str(source or "transport_risk").strip() or "transport_risk"
    if not normalized_reason:
        return {"ok": False, "enabled": False, "reason": "missing_handoff_reason"}
    if normalized_reason not in TRANSPORT_RISK_HANDOFF_STOP_REASONS:
        return {
            "ok": False,
            "enabled": False,
            "reason": "transport_stop_reason_not_handoff",
            "stop_reason": normalized_reason,
        }
    created_at = str(now_text or datetime.now().isoformat(timespec="seconds"))
    summary = listener_result if isinstance(listener_result, dict) else {}
    target_name = str(target or summary.get("target") or summary.get("last_target") or "__wechat_runtime__")
    dispatch_stub = {
        "enabled": False,
        "status": "not_configured",
        "adapter": "feishu",
        "reason": "feishu_handoff_notify_disabled",
    }
    item = {
        "case_id": runtime_handoff_case_id(
            tenant_id=tenant_id,
            source=normalized_source,
            reason=normalized_reason,
            now_text=created_at,
        ),
        "target": target_name,
        "status": "open",
        "priority": 5,
        "reason": normalized_reason,
        "message_ids": [f"runtime:{normalized_source}:{normalized_reason}:{created_at[:13]}"],
        "message_contents": [message or normalized_reason],
        "reply_text": "",
        "operator_alert": {
            "enabled": False,
            "dispatch": dispatch_stub,
            "source": normalized_source,
            "reason": normalized_reason,
            "message": message,
            "created_at": created_at,
        },
        "payload": {
            "kind": "runtime_transport_risk_handoff",
            "unreachable_rule": True,
            "requires_handoff": True,
            "dispatch": dispatch_stub,
            "transport_risk": verdict if isinstance(verdict, dict) else {},
            "listener_result": summary,
        },
        "created_at": created_at,
    }
    try:
        from apps.wechat_ai_customer_service.admin_backend.services.handoff_store import HandoffStore
        from apps.wechat_ai_customer_service.admin_backend.services.feishu_integration import dispatch_handoff_case_to_feishu

        case = HandoffStore(tenant_id=tenant_id, path=handoff_path).create_case(item)
        if bool(case.get("deduped")):
            dispatch = {
                "enabled": False,
                "status": "deduped_skip",
                "adapter": "feishu",
                "reason": "handoff_case_already_exists",
                "case_id": case.get("case_id"),
            }
        else:
            dispatch_fn = dispatch_handoff_fn or dispatch_handoff_case_to_feishu
            dispatch = dispatch_fn(case)
        operator_alert = case.get("operator_alert") if isinstance(case.get("operator_alert"), dict) else {}
        operator_alert["dispatch"] = dispatch
        case["operator_alert"] = operator_alert
        return {"ok": True, "enabled": True, "case": case, "dispatch": dispatch}
    except Exception as exc:  # noqa: BLE001 - handoff failure must not mask the original risk stop.
        return {
            "ok": False,
            "enabled": True,
            "reason": "handoff_case_create_failed",
            "error": repr(exc),
            "dispatch": dispatch_stub,
            "item": item,
        }


def _ancestor_pids(pid: int) -> set[int]:
    """Return all ancestor PIDs of the given process."""
    ancestors: set[int] = set()
    try:
        proc = psutil.Process(pid)
        while True:
            parent = proc.parent()
            if parent is None:
                break
            ancestors.add(parent.pid)
            proc = parent
    except psutil.NoSuchProcess:
        pass
    return ancestors


def _cmdline_has_listener_script(cmdline: list[str]) -> bool:
    target = "run_customer_service_listener.py"
    for arg in cmdline:
        arg_norm = os.path.normpath(str(arg))
        if os.path.basename(arg_norm) == target:
            return True
    return False


def _already_running(tenant_id: str) -> bool:
    """Check if another managed listener for the same tenant is already running."""
    my_pid = os.getpid()
    ancestor_pids = _ancestor_pids(my_pid)
    for proc in psutil.process_iter(["pid", "cmdline", "name"]):
        try:
            pid = proc.info.get("pid")
            cmdline = proc.info.get("cmdline") or []
            name = str(proc.info.get("name") or "").lower()
            if (
                pid != my_pid
                and pid not in ancestor_pids
                and "python" in name
                and _cmdline_has_listener_script(cmdline)
                and f"--tenant-id {tenant_id}" in " ".join(str(item) for item in cmdline)
            ):
                return True
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return False


def scheduler_bridge_has_active_work(scheduler_bridge: Any) -> bool:
    if scheduler_bridge is None or not bool(getattr(scheduler_bridge, "enabled", False)):
        return False
    store = getattr(scheduler_bridge, "store", None)
    if store is None or not hasattr(store, "load"):
        return False
    try:
        state = store.load()
    except Exception:
        return False
    if not isinstance(state, dict):
        return False
    sessions = [item for item in (state.get("sessions", {}) or {}).values() if isinstance(item, dict)]
    if any(item.get("pending_capture") or str(item.get("status") or "") in {"pending", "capturing"} for item in sessions):
        return True
    planner_tasks = [item for item in (state.get("llm_tasks", {}) or {}).values() if isinstance(item, dict)]
    polish_tasks = [item for item in (state.get("polish_tasks", {}) or {}).values() if isinstance(item, dict)]
    if any(str(item.get("status") or "") in {"queued", "running"} for item in planner_tasks + polish_tasks):
        return True
    replies = [item for item in (state.get("ready_replies", {}) or {}).values() if isinstance(item, dict)]
    if any(str(item.get("status") or "") in {"ready", "sending"} for item in replies):
        return True
    monitor = getattr(scheduler_bridge, "session_monitor", None)
    connector = getattr(scheduler_bridge, "connector", None)
    if monitor is not None and connector is not None:
        try:
            monitor.poll(connector)
            pending = monitor.pending_targets(limit=1)
            if pending:
                return True
        except Exception:
            return False
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--interval-seconds", type=float, default=3.0)
    parser.add_argument("--send", action="store_true")
    parser.add_argument("--write-data", action="store_true")
    args = parser.parse_args()

    tenant_id = str(args.tenant_id).strip()
    config_path = args.config.resolve()
    poll_interval_settings = load_managed_poll_interval_settings(
        config_path,
        fallback_seconds=max(0.5, float(args.interval_seconds)),
    )

    if _already_running(tenant_id):
        print(f"Managed listener for {tenant_id} is already running; exiting.", file=sys.stderr)
        return 0
    print(f"Managed listener for {tenant_id} starting with PID={os.getpid()}", file=sys.stderr)

    env = dict(os.environ)
    # Bind the current listener process as well as child environments. Several
    # workflow paths resolve tenant-scoped knowledge in-process.
    os.environ["WECHAT_KNOWLEDGE_TENANT"] = tenant_id
    env["WECHAT_KNOWLEDGE_TENANT"] = tenant_id
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    humanized_send_settings = load_rpa_humanized_send_settings(config_path)
    env = apply_rpa_humanized_send_env(env, humanized_send_settings)
    apply_rpa_humanized_send_runtime_env(humanized_send_settings)
    log_path = runtime_log_path(tenant_id)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    phase_log_path = workflow_phase_log_path(tenant_id)
    phase_log_path.parent.mkdir(parents=True, exist_ok=True)
    env["WECHAT_LISTENER_PHASE_LOG_PATH"] = str(phase_log_path)
    workflow = APP_ROOT / "workflows" / "listen_and_reply.py"
    operator_settings = load_operator_guard_settings(config_path)
    operator_control_file = runtime_operator_control_path(tenant_id)
    operator_guard_pid_file = runtime_operator_guard_pid_path(tenant_id)
    operator_guard_state_file = runtime_operator_guard_state_path(tenant_id)
    operator_guard_enabled = bool(operator_settings.get("enabled")) and os.name == "nt"
    last_operator_command_id = 0
    listener_paused = False
    scheduler_bridge: ManagedListenerSchedulerBridge | None = None

    def _shutdown_operator_guard(reason: str) -> None:
        if not operator_guard_enabled:
            return
        try:
            sync_operator_mode(
                operator_control_file,
                tenant_id=tenant_id,
                mode="stopped",
                message=reason,
            )
        except Exception:
            pass
        guard_pid = read_operator_guard_pid(operator_guard_pid_file)
        if guard_pid > 0:
            terminate_pid(guard_pid)
        clear_file(operator_guard_pid_file)
        clear_file(operator_guard_state_file)

    # --- VPS console is required only in cloud-authoritative mode ---
    from apps.wechat_ai_customer_service.auth.vps_client import discover_vps_base_url

    vps_url = discover_vps_base_url()
    if cloud_required_enabled():
        if not vps_url:
            message = "VPS控制台未配置或未启动，自动客服监听无法启动。请先启动VPS控制台。"
            write_runtime_status("stopped", message, tenant_id=tenant_id)
            append_log(log_path, {"event": "managed_listener_vps_missing", "tenant_id": tenant_id})
            print(message, file=sys.stderr)
            return 3
        if not vps_health_ok(vps_url):
            message = "VPS控制台不可达，自动客服监听无法启动。请检查VPS控制台是否正常运行。"
            write_runtime_status("stopped", message, tenant_id=tenant_id)
            append_log(log_path, {"event": "managed_listener_vps_unhealthy", "tenant_id": tenant_id, "vps_url": vps_url})
            print(message, file=sys.stderr)
            return 3
    elif vps_url and not vps_health_ok(vps_url):
        append_log(
            log_path,
            {
                "event": "managed_listener_vps_unhealthy_non_blocking",
                "tenant_id": tenant_id,
                "vps_url": vps_url,
                "reason": "cloud_required_disabled",
            },
        )

    # --- Write PID record so admin backend can detect this listener ---
    from apps.wechat_ai_customer_service.admin_backend.services.customer_service_runtime import runtime_pid_path

    pid_record = {
        "pid": os.getpid(),
        "tenant_id": tenant_id,
        "config_path": str(config_path),
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "log_path": str(log_path),
    }
    pid_path = runtime_pid_path(tenant_id)
    atomic_write_json(pid_path, pid_record)

    import atexit

    def _cleanup_pid():
        try:
            pid_path.unlink()
        except FileNotFoundError:
            pass
        if scheduler_bridge is not None:
            scheduler_bridge.shutdown()
        _shutdown_operator_guard("listener_process_exit")

    atexit.register(_cleanup_pid)

    if operator_guard_enabled:
        clear_file(operator_guard_state_file)
        clear_file(operator_guard_pid_file)
        control_payload = empty_operator_control_state(tenant_id, mode="running")
        write_operator_control_state(operator_control_file, control_payload)
        guard_launch = launch_operator_guard(
            tenant_id=tenant_id,
            settings=operator_settings,
            control_path=operator_control_file,
            status_path=runtime_dir(tenant_id) / "runtime_status.json",
            state_path=operator_guard_state_file,
            pid_path=operator_guard_pid_file,
        )
        append_log(
            log_path,
            {
                "event": "managed_listener_operator_guard_launch",
                "tenant_id": tenant_id,
                "operator_guard": {
                    "enabled": True,
                    "settings": operator_settings,
                    "launch": guard_launch,
                },
            },
        )
        if not guard_launch.get("ok"):
            message = "RPA防误触守护启动失败，已停止监听启动。请检查 run_rpa_operator_guard.py。"
            write_runtime_status("stopped", message, tenant_id=tenant_id)
            print(message, file=sys.stderr)
            return 5
        guard_verify = verify_operator_guard_bootstrap(
            int(guard_launch.get("pid") or 0),
            operator_guard_state_file,
            timeout_seconds=float(
                operator_settings.get("bootstrap_timeout_seconds")
                or OPERATOR_GUARD_DEFAULTS["bootstrap_timeout_seconds"]
            ),
            expected_parent_pid=os.getpid(),
        )
        append_log(
            log_path,
            {
                "event": "managed_listener_operator_guard_verify",
                "tenant_id": tenant_id,
                "operator_guard_verify": guard_verify,
            },
        )
        if guard_verify.get("ok") is not True:
            message = "RPA防误触守护未就绪（悬浮窗/键鼠拦截初始化失败），已停止监听启动。请检查守护状态。"
            write_runtime_status(
                "stopped",
                message,
                tenant_id=tenant_id,
                operator_guard=guard_verify,
            )
            _shutdown_operator_guard("operator_guard_verify_failed")
            print(message, file=sys.stderr)
            return 5
        try:
            guard_state_pid = int(guard_verify.get("state_pid") or 0)
        except (TypeError, ValueError):
            guard_state_pid = 0
        if guard_state_pid > 0:
            atomic_write_json(
                operator_guard_pid_file,
                {
                    "pid": guard_state_pid,
                    "launcher_pid": int(guard_launch.get("pid") or 0),
                    "tenant_id": tenant_id,
                    "started_at": datetime.now().isoformat(timespec="seconds"),
                    "control_path": str(operator_control_file),
                    "status_path": str(runtime_dir(tenant_id) / "runtime_status.json"),
                    "state_path": str(operator_guard_state_file),
                    "parent_pid": os.getpid(),
                },
            )
    else:
        clear_file(operator_guard_pid_file)
        if operator_control_file.exists():
            try:
                sync_operator_mode(
                    operator_control_file,
                    tenant_id=tenant_id,
                    mode="stopped",
                    message="operator_guard_disabled",
                )
            except Exception:
                pass

    scheduler_requested = load_concurrency_scheduler_enabled(config_path)
    if scheduler_requested:
        try:
            scheduler_bridge = ManagedListenerSchedulerBridge(
                tenant_id=tenant_id,
                config_path=config_path,
                allow_send=bool(args.send),
                write_data=bool(args.write_data),
            )
        except Exception as exc:  # noqa: BLE001 - startup guards should surface in runtime status
            message = f"微信自动客服启动前安全护栏未通过：{exc}"
            write_runtime_status("stopped", message, tenant_id=tenant_id, last_error=repr(exc))
            append_log(
                log_path,
                {
                    "event": "managed_listener_scheduler_startup_guard_failed",
                    "tenant_id": tenant_id,
                    "error": repr(exc),
                },
            )
            print(message, file=sys.stderr)
            return 3

    append_log(
        log_path,
        {
            "event": "managed_listener_start",
            "tenant_id": tenant_id,
            "config": str(config_path),
            "rpa_humanized_send": humanized_send_settings,
            "operator_guard": {
                "enabled": operator_guard_enabled,
                "settings": operator_settings if operator_guard_enabled else {"enabled": False},
            },
            "concurrency_scheduler": {
                "enabled": bool(scheduler_bridge and scheduler_bridge.enabled),
                "requested": bool(scheduler_requested),
            },
        },
    )
    write_runtime_status("idle", "自动客服监听已启动，等待微信消息。", tenant_id=tenant_id)
    sync_service = VpsLocalSyncService()
    risk_settings = load_transport_risk_settings(config_path)
    runtime_target_guard_settings = load_runtime_target_guard_settings(config_path)
    risk_state_file = transport_risk_state_path(tenant_id)
    risk_guard_state = read_transport_risk_guard_state(risk_state_file)
    passive_probe_enabled = bool(risk_settings.get("passive_logout_probe_enabled", True))
    passive_probe_interval_seconds = max(5.0, float(risk_settings.get("passive_logout_probe_interval_seconds") or 45.0))
    passive_probe_timeout_seconds = max(1.0, float(risk_settings.get("passive_logout_probe_timeout_seconds") or 12.0))
    interactive_calibration_timeout_seconds = max(
        passive_probe_timeout_seconds,
        float(
            risk_settings.get("interactive_calibration_timeout_seconds")
            or TRANSPORT_RISK_DEFAULTS["interactive_calibration_timeout_seconds"]
        ),
    )
    passive_probe_last_at = float((risk_guard_state or {}).get("passive_logout_probe_last_at") or 0.0)
    last_passive_probe_verdict: dict[str, Any] = {}

    def _interactive_calibration_or_stop(reason: str, *, trigger: dict[str, Any] | None = None) -> int | None:
        nonlocal risk_guard_state
        if not bool(risk_settings.get("interactive_calibration_enabled", True)):
            return None
        calibration = run_interactive_rpa_calibration(
            env=env,
            timeout_seconds=interactive_calibration_timeout_seconds,
            reason=reason,
        )
        now_for_calibration = time.time()
        status_payload = calibration.get("status_payload") if isinstance(calibration.get("status_payload"), dict) else {}
        risk_input = status_payload if isinstance(status_payload, dict) else {}
        if not risk_input:
            risk_input = {
                "ok": False,
                "state": "interactive_calibration_failed",
                "error": calibration.get("stderr_tail") or calibration.get("stdout_tail") or "interactive calibration failed",
            }
        risk_verdict = evaluate_transport_risk(
            risk_input,
            guard_state=risk_guard_state,
            settings=risk_settings,
            now_ts=now_for_calibration,
        )
        risk_guard_state = risk_verdict.get("state") if isinstance(risk_verdict.get("state"), dict) else risk_guard_state
        risk_guard_state["last_interactive_calibration_at"] = now_for_calibration
        risk_guard_state["last_interactive_calibration_reason"] = reason
        risk_guard_state["last_interactive_calibration_ok"] = bool(calibration.get("ok"))
        if calibration.get("ok"):
            risk_guard_state["passive_logout_probe_failures"] = []
        write_transport_risk_guard_state(risk_state_file, risk_guard_state)
        append_log(
            log_path,
            {
                "event": "managed_listener_interactive_calibration",
                "tenant_id": tenant_id,
                "reason": reason,
                "ok": bool(calibration.get("ok")),
                "calibration": calibration,
                "trigger": trigger or {},
                "transport_risk": {
                    "stop": bool(risk_verdict.get("stop")),
                    "reason": risk_verdict.get("reason"),
                    "signals": risk_verdict.get("signals"),
                },
            },
        )
        if calibration.get("ok"):
            return None
        startup_passive_probe: dict[str, Any] = {}
        startup_passive_verdict: dict[str, Any] = {}
        if reason == "startup" and passive_probe_enabled:
            startup_passive_probe = run_passive_logout_probe(
                env=env,
                timeout_seconds=passive_probe_timeout_seconds,
            )
            startup_passive_verdict = evaluate_passive_logout_probe(
                startup_passive_probe,
                guard_state=risk_guard_state,
                settings=risk_settings,
                now_ts=time.time(),
            )
            if isinstance(startup_passive_verdict.get("state"), dict):
                risk_guard_state = startup_passive_verdict["state"]
            defer_decision = startup_interactive_calibration_can_defer(
                calibration,
                passive_probe=startup_passive_probe,
                settings=risk_settings,
            )
            append_log(
                log_path,
                {
                    "event": "managed_listener_startup_interactive_calibration_defer_check",
                    "tenant_id": tenant_id,
                    "defer": bool(defer_decision.get("ok")) and not bool(startup_passive_verdict.get("stop")),
                    "decision": defer_decision,
                    "passive_probe": startup_passive_probe,
                    "passive_transport_risk": {
                        "stop": bool(startup_passive_verdict.get("stop")),
                        "reason": startup_passive_verdict.get("reason"),
                        "failures": startup_passive_verdict.get("failures"),
                        "hits": startup_passive_verdict.get("hits"),
                    },
                },
            )
            if bool(defer_decision.get("ok")) and not bool(startup_passive_verdict.get("stop")):
                risk_guard_state["last_interactive_calibration_deferred_at"] = time.time()
                risk_guard_state["last_interactive_calibration_deferred_reason"] = str(defer_decision.get("reason") or "")
                risk_guard_state["last_interactive_calibration_ok"] = False
                write_transport_risk_guard_state(risk_state_file, risk_guard_state)
                deferred_verdict = {
                    "enabled": True,
                    "stop": False,
                    "reason": "startup_interactive_calibration_deferred",
                    "message": "微信窗口可读，启动交互校准已延后，监听继续运行。",
                    "calibration": calibration,
                    "passive_probe": startup_passive_probe,
                    "defer_decision": defer_decision,
                    "trigger": trigger or {},
                }
                write_runtime_status(
                    "idle",
                    "微信窗口可读，启动交互校准已延后，监听继续运行。",
                    tenant_id=tenant_id,
                    transport_risk={"interactive_calibration": deferred_verdict},
                )
                return None
        stop_reason = str(risk_verdict.get("reason") or "wechat_interactive_calibration_failed")
        stop_message = str(
            risk_verdict.get("message")
            or "微信窗口校准失败，已停止监听。请确认微信主窗口未被遮挡、未白屏且已登录后再启动。"
        )
        if bool(startup_passive_verdict.get("stop")):
            stop_reason = str(startup_passive_verdict.get("reason") or stop_reason)
            stop_message = str(startup_passive_verdict.get("message") or stop_message)
        stop_verdict = risk_verdict if risk_verdict.get("stop") else {
            "enabled": True,
            "stop": True,
            "reason": stop_reason,
            "message": stop_message,
            "calibration": calibration,
            "passive_probe": startup_passive_probe,
            "passive_transport_risk": startup_passive_verdict,
            "trigger": trigger or {},
        }
        runtime_handoff = create_runtime_transport_handoff_case(
            tenant_id=tenant_id,
            reason=stop_reason,
            message=stop_message,
            source="interactive_calibration",
            verdict=stop_verdict,
        )
        write_runtime_status(
            "stopped",
            stop_message,
            tenant_id=tenant_id,
            transport_risk={"interactive_calibration": stop_verdict},
            runtime_handoff=runtime_handoff,
        )
        _shutdown_operator_guard(f"interactive_calibration_failed:{reason}")
        print(stop_message, file=sys.stderr)
        return 4

    cloud_sync_token = str(os.getenv("WECHAT_RUNTIME_SYNC_TOKEN") or "").strip()
    try:
        cloud_refresh_interval = max(5.0, float(os.getenv("WECHAT_CLOUD_REFRESH_INTERVAL_SECONDS") or "20"))
    except ValueError:
        cloud_refresh_interval = 20.0
    last_cloud_refresh_at = 0.0
    if bool(risk_settings.get("startup_interactive_calibration_enabled", True)):
        startup_stop = _interactive_calibration_or_stop("startup")
        if startup_stop is not None:
            return startup_stop
    fast_followup_ticks_remaining = 0
    while True:
        if operator_guard_enabled:
            hotkey_label = str(operator_settings.get("control_hotkey") or OPERATOR_GUARD_DEFAULTS["control_hotkey"]).upper()
            operator_control = read_operator_control_state(operator_control_file, tenant_id=tenant_id)
            operator_command = operator_control.get("command") if isinstance(operator_control.get("command"), dict) else {}
            try:
                command_id = int(operator_command.get("id") or 0)
            except (TypeError, ValueError):
                command_id = 0
            command_status = str(operator_command.get("status") or "").strip().lower()
            command_action = str(operator_command.get("action") or "").strip().lower()
            if command_id > last_operator_command_id and command_status == "pending" and command_action in OPERATOR_COMMAND_ACTIONS:
                if command_action == "pause":
                    listener_paused = True
                    message = f"{hotkey_label} 已暂停，单击恢复，双击停止。"
                    operator_control = apply_operator_command(operator_control, action="pause", message=message)
                    write_operator_control_state(operator_control_file, operator_control)
                    write_runtime_status("paused", message, tenant_id=tenant_id)
                    append_log(
                        log_path,
                        {
                            "event": "managed_listener_operator_guard_command_applied",
                            "tenant_id": tenant_id,
                            "action": "pause",
                            "command_id": command_id,
                        },
                    )
                elif command_action == "resume":
                    listener_paused = False
                    message = f"已恢复（{hotkey_label} 双击可停止）。"
                    operator_control = apply_operator_command(operator_control, action="resume", message=message)
                    write_operator_control_state(operator_control_file, operator_control)
                    write_runtime_status("idle", message, tenant_id=tenant_id)
                    append_log(
                        log_path,
                        {
                            "event": "managed_listener_operator_guard_command_applied",
                            "tenant_id": tenant_id,
                            "action": "resume",
                            "command_id": command_id,
                        },
                    )
                    if bool(risk_settings.get("resume_interactive_calibration_enabled", True)):
                        resume_stop = _interactive_calibration_or_stop("resume")
                        if resume_stop is not None:
                            return resume_stop
                elif command_action == "stop":
                    message = "已停止。"
                    operator_control = apply_operator_command(operator_control, action="stop", message=message)
                    write_operator_control_state(operator_control_file, operator_control)
                    write_runtime_status("stopped", message, tenant_id=tenant_id)
                    stop_customer_service_support_processes(tenant_id, include_operator_guard=False)
                    append_log(
                        log_path,
                        {
                            "event": "managed_listener_operator_guard_command_applied",
                            "tenant_id": tenant_id,
                            "action": "stop",
                            "command_id": command_id,
                        },
                    )
                    _shutdown_operator_guard("operator_guard_stop_hotkey")
                    return 0
                last_operator_command_id = command_id
            elif command_id > last_operator_command_id:
                last_operator_command_id = command_id
            current_mode = "paused" if listener_paused else "running"
            if (
                str(operator_control.get("mode") or "").strip().lower() != current_mode
                and command_status != "pending"
            ):
                sync_operator_mode(
                    operator_control_file,
                    tenant_id=tenant_id,
                    mode=current_mode,
                    message="listener_mode_sync",
                )
        now_wall = time.time()
        if passive_probe_enabled and (
            passive_probe_last_at <= 0.0 or (now_wall - passive_probe_last_at) >= passive_probe_interval_seconds
        ):
            if scheduler_bridge_has_active_work(scheduler_bridge):
                passive_probe_last_at = now_wall
                risk_guard_state["passive_logout_probe_last_at"] = now_wall
                risk_guard_state["passive_logout_probe_deferred_reason"] = "scheduler_active_work"
                write_transport_risk_guard_state(risk_state_file, risk_guard_state)
                append_log(
                    log_path,
                    {
                        "event": "managed_listener_passive_logout_probe_deferred",
                        "tenant_id": tenant_id,
                        "reason": "scheduler_active_work",
                    },
                )
            else:
                passive_probe = run_passive_logout_probe(
                    env=env,
                    timeout_seconds=passive_probe_timeout_seconds,
                )
                passive_probe_last_at = now_wall
                passive_verdict = evaluate_passive_logout_probe(
                    passive_probe,
                    guard_state=risk_guard_state,
                    settings=risk_settings,
                    now_ts=now_wall,
                )
                last_passive_probe_verdict = passive_verdict
                risk_guard_state = passive_verdict.get("state") if isinstance(passive_verdict.get("state"), dict) else risk_guard_state
                write_transport_risk_guard_state(risk_state_file, risk_guard_state)
                append_log(
                    log_path,
                    {
                        "event": "managed_listener_passive_logout_probe",
                        "tenant_id": tenant_id,
                        "passive_probe": passive_probe,
                        "verdict": {
                            "stop": bool(passive_verdict.get("stop")),
                            "reason": passive_verdict.get("reason"),
                            "hits": passive_verdict.get("hits"),
                            "failures": passive_verdict.get("failures"),
                            "threshold": passive_verdict.get("threshold"),
                        },
                    },
                )
                if passive_verdict.get("stop"):
                    message = str(passive_verdict.get("message") or "检测到微信掉线风险，已自动停机。")
                    runtime_handoff = create_runtime_transport_handoff_case(
                        tenant_id=tenant_id,
                        reason=str(passive_verdict.get("reason") or "wechat_logout_detected_by_passive_probe"),
                        message=message,
                        source="passive_logout_probe",
                        verdict=passive_verdict,
                    )
                    append_log(
                        log_path,
                        {
                            "event": "managed_listener_passive_logout_stop",
                            "tenant_id": tenant_id,
                            "passive_probe": passive_probe,
                            "transport_risk": passive_verdict,
                            "runtime_handoff": runtime_handoff,
                        },
                    )
                    write_runtime_status(
                        "stopped",
                        message,
                        tenant_id=tenant_id,
                        transport_risk={"passive_probe": passive_verdict},
                        runtime_handoff=runtime_handoff,
                    )
                    _shutdown_operator_guard("passive_logout_stop")
                    print(message, file=sys.stderr)
                    return 4
                calibration_due = passive_probe_recalibration_due(
                    passive_verdict,
                    settings=risk_settings,
                    now_ts=now_wall,
                )
                if calibration_due.get("due"):
                    calibration_stop = _interactive_calibration_or_stop(
                        "passive_probe_failure",
                        trigger={"passive_probe": passive_probe, "verdict": passive_verdict, "due": calibration_due},
                    )
                    if calibration_stop is not None:
                        return calibration_stop
        if cloud_required_enabled():
            now_ts = time.monotonic()
            if now_ts - last_cloud_refresh_at >= cloud_refresh_interval:
                refresh = sync_service.fetch_shared_knowledge_snapshot(
                    token=cloud_sync_token,
                    tenant_id=tenant_id,
                    force=False,
                )
                last_cloud_refresh_at = now_ts
                if refresh.get("ok") is not True:
                    message = "共享行业知识库续租失败。请恢复服务端连接并重新启动。"
                    append_log(
                        log_path,
                        {
                            "event": "managed_listener_cloud_refresh_failed",
                            "tenant_id": tenant_id,
                            "cloud_sync": refresh,
                        },
                    )
                    write_runtime_status("stopped", message, tenant_id=tenant_id, cloud_sync=refresh)
                    _shutdown_operator_guard("cloud_refresh_failed")
                    return 2
            gate = cloud_gate_status()
            if not gate.get("ok"):
                message = "云端授权未通过。请连接服务端并刷新共享行业知识库。"
                append_log(log_path, {"event": "managed_listener_cloud_gate_stop", "tenant_id": tenant_id, "cloud_gate": gate})
                write_runtime_status("stopped", message, tenant_id=tenant_id, cloud_gate=gate)
                _shutdown_operator_guard("cloud_gate_stop")
                return 2
        if listener_paused:
            pause_sleep = max(0.12, float(operator_settings.get("pause_poll_interval_ms") or 550) / 1000.0)
            time.sleep(pause_sleep)
            continue
        started = time.time()
        if scheduler_bridge is not None and scheduler_bridge.enabled:
            write_runtime_status(
                "thinking",
                "正在并发调度微信会话：RPA串行读取/发送，LLM并发思考。",
                tenant_id=tenant_id,
            )
            try:
                result = scheduler_bridge.tick(allow_send=bool(args.send))
            except Exception as exc:  # noqa: BLE001 - keep listener alive enough for risk guard/status
                result = {
                    "ok": False,
                    "error": repr(exc),
                    "events": [],
                    "scheduler_enabled": True,
                }
                append_log(
                    log_path,
                    {
                        "event": "managed_listener_scheduler_tick_exception",
                        "tenant_id": tenant_id,
                        "error": repr(exc),
                    },
                )
            append_log(
                log_path,
                {
                    "event": "managed_listener_scheduler_tick",
                    "tenant_id": tenant_id,
                    "ok": bool(result.get("ok")),
                    "summary": result.get("summary"),
                    "event_count": len(result.get("events") or []),
                },
            )
        else:
            command = [sys.executable, str(workflow), "--config", str(config_path), "--once"]
            if args.send:
                command.append("--send")
            if args.write_data:
                command.append("--write-data")
            write_runtime_status("thinking", "正在读取微信消息并准备回复。", tenant_id=tenant_id)
            result = run_once(
                command,
                env=env,
                cwd=PROJECT_ROOT,
                log_path=log_path,
                phase_log_path=phase_log_path,
                timeout_seconds=managed_once_timeout_seconds(config_path),
            )
        duration = round(time.time() - started, 2)
        summary = summarize_listener_result(result) if isinstance(result, dict) else {}
        scheduler_tick_activity = summarize_scheduler_tick_activity(result if isinstance(result, dict) else None)
        if scheduler_tick_activity.get("urgent_followup"):
            fast_followup_ticks_remaining = max(fast_followup_ticks_remaining, 2)
        if isinstance(result, dict) and result.get("scheduler_enabled"):
            scheduler_summary = result.get("summary") if isinstance(result.get("summary"), dict) else {}
            summary["scheduler_enabled"] = True
            summary["scheduler_summary"] = scheduler_summary
            summary["scheduler_active_session_signals"] = result.get("active_session_signals", [])
            summary["scheduler_loop_busy"] = bool(scheduler_tick_activity.get("busy"))
            summary["scheduler_loop_urgent_followup"] = bool(scheduler_tick_activity.get("urgent_followup"))
            summary["scheduler_fast_followup_ticks_remaining"] = int(fast_followup_ticks_remaining)
        target_guard_verdict = evaluate_runtime_target_guard(
            result if isinstance(result, dict) else {},
            settings=runtime_target_guard_settings,
        )
        if target_guard_verdict.get("stop"):
            message = str(target_guard_verdict.get("message") or "检测到运行时非白名单目标，已自动停机。")
            runtime_handoff = create_runtime_transport_handoff_case(
                tenant_id=tenant_id,
                reason=str(target_guard_verdict.get("reason") or "runtime_disallowed_target_detected"),
                message=message,
                source="runtime_target_guard",
                verdict=target_guard_verdict,
                listener_result=summary,
                target=",".join(target_guard_verdict.get("disallowed_targets") or []),
            )
            append_log(
                log_path,
                {
                    "event": "managed_listener_runtime_target_guard_stop",
                    "tenant_id": tenant_id,
                    "target_guard": target_guard_verdict,
                    "listener_result": summary,
                    "runtime_handoff": runtime_handoff,
                },
            )
            write_runtime_status(
                "stopped",
                message,
                tenant_id=tenant_id,
                last_run_seconds=duration,
                target_guard=target_guard_verdict,
                runtime_handoff=runtime_handoff,
                **summary,
            )
            _shutdown_operator_guard("runtime_target_guard_stop")
            print(message, file=sys.stderr)
            return 4
        risk_verdict = evaluate_transport_risk(
            result if isinstance(result, dict) else {},
            guard_state=risk_guard_state,
            settings=risk_settings,
        )
        risk_guard_state = risk_verdict.get("state") if isinstance(risk_verdict.get("state"), dict) else risk_guard_state
        write_transport_risk_guard_state(risk_state_file, risk_guard_state)
        signals = risk_verdict.get("signals", {}) if isinstance(risk_verdict.get("signals"), dict) else {}
        if signals.get("send_input_not_ready") and not risk_verdict.get("stop"):
            append_log(
                log_path,
                {
                    "event": "managed_listener_transport_risk_warning",
                    "tenant_id": tenant_id,
                    "risk_reason": "send_input_not_ready",
                    "signals": signals,
                    "send_input_not_ready_hits": len((risk_guard_state or {}).get("send_input_not_ready_hits") or []),
                },
            )
        if risk_verdict.get("stop"):
            message = str(risk_verdict.get("message") or "检测到微信风险状态，已自动停机。")
            runtime_handoff = create_runtime_transport_handoff_case(
                tenant_id=tenant_id,
                reason=str(risk_verdict.get("reason") or "transport_risk_stop"),
                message=message,
                source="transport_risk_stop",
                verdict=risk_verdict,
                listener_result=summary,
                target=str(summary.get("target") or ""),
            )
            append_log(
                log_path,
                {
                    "event": "managed_listener_transport_risk_stop",
                    "tenant_id": tenant_id,
                    "transport_risk": risk_verdict,
                    "listener_result": summary,
                    "runtime_handoff": runtime_handoff,
                },
            )
            write_runtime_status(
                "stopped",
                message,
                tenant_id=tenant_id,
                last_run_seconds=duration,
                transport_risk=risk_verdict,
                runtime_handoff=runtime_handoff,
                **summary,
            )
            _shutdown_operator_guard("transport_risk_stop")
            print(message, file=sys.stderr)
            return 4
        message = status_message_from_result(result, duration)
        write_runtime_status(
            "idle",
            message,
            tenant_id=tenant_id,
            last_run_seconds=duration,
            transport_risk={
                "signals": risk_verdict.get("signals", {}),
                "thresholds": risk_verdict.get("thresholds", {}),
                "stop": bool(risk_verdict.get("stop")),
                "passive_probe": {
                    "enabled": passive_probe_enabled,
                    "last_stop": bool(last_passive_probe_verdict.get("stop")),
                    "last_reason": last_passive_probe_verdict.get("reason"),
                    "hits": last_passive_probe_verdict.get("hits"),
                    "threshold": last_passive_probe_verdict.get("threshold"),
                    "last_at": passive_probe_last_at,
                },
            },
            target_guard={
                "enabled": bool(target_guard_verdict.get("enabled")),
                "ok": bool(target_guard_verdict.get("ok", True)),
                "allowed_targets": target_guard_verdict.get("allowed_targets", []),
                "observed_targets": sorted(
                    {
                        str(item.get("target") or "").strip()
                        for item in target_guard_verdict.get("observations", []) or []
                        if str(item.get("target") or "").strip()
                    }
                ),
            },
            **summary,
        )
        idle_min_sleep = max(0.5, float(poll_interval_settings.get("min_seconds") or args.interval_seconds))
        idle_max_sleep = max(idle_min_sleep, float(poll_interval_settings.get("max_seconds") or idle_min_sleep))
        base_sleep = random.uniform(idle_min_sleep, idle_max_sleep)
        busy_sleep = min(base_sleep, 1.15)
        quick_followup_sleep = min(busy_sleep, 0.68)
        sleep_mode = "idle"
        selected_sleep = base_sleep
        if scheduler_tick_activity.get("busy"):
            selected_sleep = busy_sleep
            sleep_mode = "busy"
        if scheduler_tick_activity.get("scheduler_enabled") and fast_followup_ticks_remaining > 0:
            selected_sleep = min(selected_sleep, quick_followup_sleep)
            sleep_mode = "fast_followup"
            fast_followup_ticks_remaining = max(0, fast_followup_ticks_remaining - 1)
        elif not scheduler_tick_activity.get("busy"):
            fast_followup_ticks_remaining = 0
        cooldown_sleep = max(0.0, float(risk_verdict.get("cooldown_seconds") or 0.0))
        jitter_cap = max(0.0, float(risk_verdict.get("loop_jitter_seconds") or 0.0))
        jitter_multiplier = 0.0
        if sleep_mode == "busy":
            jitter_multiplier = 0.7
        elif sleep_mode == "fast_followup":
            jitter_multiplier = 0.45
        jitter = random.uniform(0.0, jitter_cap * jitter_multiplier) if jitter_cap > 0 else 0.0
        time.sleep(selected_sleep + cooldown_sleep + jitter)


def run_once(
    command: list[str],
    *,
    env: dict[str, str],
    cwd: Path,
    log_path: Path,
    phase_log_path: Path | None = None,
    timeout_seconds: float | None = None,
) -> dict:
    timeout = max(1.0, float(timeout_seconds or 45.0))
    started = time.time()
    process = subprocess.Popen(
        command,
        cwd=str(cwd),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    try:
        stdout_value, stderr_value = process.communicate(timeout=timeout)
        timed_out = False
    except subprocess.TimeoutExpired:
        timed_out = True
        terminate_process_tree(process)
        stdout_value, stderr_value = process.communicate()
    stdout = (stdout_value or "").strip()
    stderr = (stderr_value or "").strip()
    if timed_out:
        duration = round(time.time() - started, 2)
        phase_heartbeat = read_latest_phase_heartbeat(phase_log_path) if phase_log_path else {}
        append_log(
            log_path,
            {
                "event": "managed_listener_watchdog_timeout",
                "timeout_seconds": timeout,
                "duration_seconds": duration,
                "command": command,
                "phase_heartbeat": phase_heartbeat,
                "stdout_tail": stdout[-1000:],
                "stderr_tail": stderr[-1000:],
            },
        )
        return {
            "ok": False,
            "error": "watchdog_timeout",
            "watchdog_timeout": True,
            "timeout_seconds": timeout,
            "duration_seconds": duration,
            "phase_heartbeat": phase_heartbeat,
            "events": [],
        }
    payload = parse_last_json(stdout)
    phase_heartbeat = read_latest_phase_heartbeat(phase_log_path) if phase_log_path else {}
    append_log(
        log_path,
        {
            "event": "listen_once_exit",
            "returncode": process.returncode,
            "phase_heartbeat": phase_heartbeat,
            "stdout_tail": stdout[-3000:],
            "stderr_tail": stderr[-3000:],
        },
    )
    if payload:
        payload.setdefault("ok", process.returncode == 0)
        return payload
    return {"ok": process.returncode == 0, "error": stderr[-1000:] or stdout[-1000:] or f"exit={process.returncode}", "events": []}


def vps_health_ok(vps_url: str) -> bool:
    try:
        req = urllib.request.Request(vps_url.rstrip("/") + "/v1/health", headers={"Accept": "application/json"}, method="GET")
        with urllib.request.urlopen(req, timeout=3) as resp:
            return 200 <= int(getattr(resp, "status", 0)) < 300
    except Exception:
        return False


def managed_once_timeout_seconds(config_path: Path) -> float:
    env_value = str(os.getenv("WECHAT_LISTENER_ONCE_TIMEOUT_SECONDS") or "").strip()
    if env_value:
        try:
            return max(1.0, float(env_value))
        except ValueError:
            pass
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception:
        return 45.0
    return estimate_managed_once_timeout_seconds(payload)


def _positive_float(value: Any, default: float, *, minimum: float = 0.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, parsed)


def _section(payload: dict[str, Any], name: str) -> dict[str, Any]:
    section = payload.get(name)
    return section if isinstance(section, dict) else {}


def _section_enabled(section: dict[str, Any], *, default: bool = True) -> bool:
    raw = section.get("enabled")
    if raw is None:
        return default
    return str(raw).strip().lower() not in {"0", "false", "no", "off"}


def estimate_managed_once_timeout_seconds(payload: dict[str, Any]) -> float:
    """Estimate a safe per-round watchdog for the full RPA + reasoning path.

    The historical `realtime_reply.watchdog_timeout_seconds` was tuned for a
    lightweight foreground router. A real WeChat RPA round can also include
    window recovery, OCR, history backfill, LLM synthesis, final polish and
    humanized typing. Use the configured watchdog as a floor, then expand it
    when heavier runtime features are enabled.
    """

    realtime = payload.get("realtime_reply") if isinstance(payload.get("realtime_reply"), dict) else {}
    configured_timeout = _positive_float(realtime.get("watchdog_timeout_seconds"), 45.0, minimum=5.0)

    # Baseline covers process startup, WeChat window focus/open-chat and OCR reads.
    estimate = 30.0

    history_backfill = _section(payload, "history_backfill")
    if _section_enabled(history_backfill, default=False):
        load_times = positive_int(history_backfill.get("load_times"), 0, minimum=0)
        estimate += min(load_times, 5) * 6.0

    intent_llm = _section(_section(payload, "intent_router"), "llm")
    if _section_enabled(intent_llm, default=False):
        estimate += _positive_float(intent_llm.get("timeout_seconds"), 2.0, minimum=0.0)

    if _section_enabled(realtime, default=True) and realtime.get("allow_foreground_llm", True) is not False:
        estimate += _positive_float(realtime.get("foreground_llm_timeout_seconds"), 8.0, minimum=0.0)

    llm_synthesis = _section(payload, "llm_reply_synthesis")
    if _section_enabled(llm_synthesis, default=False):
        estimate += _positive_float(llm_synthesis.get("timeout_seconds"), 12.0, minimum=0.0)

    final_polish = _section(payload, "final_visible_llm_polish")
    if _section_enabled(final_polish, default=False):
        estimate += _positive_float(final_polish.get("timeout_seconds"), 4.0, minimum=0.0)

    humanized_send = _section(payload, "rpa_humanized_send")
    humanized_enabled = _section_enabled(humanized_send, default=True)
    if humanized_enabled:
        send_settings = normalize_rpa_humanized_send_settings(humanized_send)
        max_chars = max(
            positive_int(_section(payload, "reply_style_adapter").get("max_reply_chars"), 0, minimum=0),
            positive_int(final_polish.get("max_reply_chars"), 0, minimum=0),
            positive_int(llm_synthesis.get("max_reply_chars"), 0, minimum=0),
            120,
        )
        bounded_chars = min(max_chars, 900)
        char_delay_ms = _positive_float(send_settings.get("typing_char_delay_max_ms"), 180.0, minimum=0.0)
        micro_pause_ms = _positive_float(send_settings.get("typing_micro_pause_max_ms"), 650.0, minimum=0.0)
        micro_every = positive_int(send_settings.get("typing_micro_pause_every_chars"), 18, minimum=1)
        pre_delay_ms = _positive_float(send_settings.get("send_pre_delay_max_ms"), 1300.0, minimum=0.0)
        post_input_delay_ms = _positive_float(send_settings.get("send_post_input_delay_max_ms"), 460.0, minimum=0.0)
        trigger_delay_ms = _positive_float(send_settings.get("send_trigger_delay_max_ms"), 1350.0, minimum=0.0)
        after_trigger_delay_ms = _positive_float(send_settings.get("send_after_trigger_delay_max_ms"), 760.0, minimum=0.0)
        typo_budget = positive_int(send_settings.get("typing_typo_max"), 0, minimum=0)
        estimated_typing = bounded_chars * char_delay_ms / 1000.0
        estimated_typing += math.ceil(bounded_chars / micro_every) * micro_pause_ms / 1000.0
        estimated_typing += (pre_delay_ms + post_input_delay_ms + trigger_delay_ms + after_trigger_delay_ms) / 1000.0
        estimated_typing += typo_budget * 1.6
        # Humanized RPA can spend most of a round inside SendInput typing.
        # Keep the watchdog above the configured worst-case typing path instead
        # of killing a healthy slow send and forcing a duplicate retry.
        estimate += 18.0 + min(240.0, estimated_typing)

    if _section_enabled(_section(payload, "semantic_batch_planner"), default=False):
        estimate += 4.0

    estimate += 10.0
    minimum_floor = 75.0 if humanized_enabled else 45.0
    return round(max(configured_timeout, estimate, minimum_floor), 2)


def terminate_process_tree(process: subprocess.Popen[str]) -> None:
    try:
        parent = psutil.Process(process.pid)
    except psutil.NoSuchProcess:
        return
    children = parent.children(recursive=True)
    for child in children:
        try:
            child.terminate()
        except psutil.NoSuchProcess:
            pass
    try:
        parent.terminate()
    except psutil.NoSuchProcess:
        pass
    gone, alive = psutil.wait_procs([parent, *children], timeout=3)
    for proc in alive:
        try:
            proc.kill()
        except psutil.NoSuchProcess:
            pass


def parse_last_json(text: str) -> dict:
    if not text:
        return {}
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        payload = None
    if isinstance(payload, dict):
        return payload
    for line in reversed([item.strip() for item in text.splitlines() if item.strip()]):
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    start = text.rfind("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            payload = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return {}
        return payload if isinstance(payload, dict) else {}
    return {}


def summarize_scheduler_tick_activity(result: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(result, dict) or not result.get("scheduler_enabled"):
        return {
            "scheduler_enabled": False,
            "busy": False,
            "urgent_followup": False,
        }
    summary = result.get("summary") if isinstance(result.get("summary"), dict) else {}
    events = [item for item in result.get("events", []) or [] if isinstance(item, dict)]
    pending = int(summary.get("pending_sessions") or 0)
    running = int(summary.get("llm_running") or 0)
    ready = int(summary.get("reply_ready") or 0)
    sent = int(summary.get("reply_sent") or 0)
    event_names = {str(item.get("event") or "") for item in events}
    llm_completed = "llm_task_completed" in event_names
    send_completed = "send_completed" in event_names
    send_failed = "send_failed" in event_names
    busy = bool(pending or running or ready or sent or events)
    unread_or_capture_changed = bool(
        event_names
        & {
            "signal_pending",
            "capture_completed",
            "capture_empty",
            "reply_stale",
            "state_cleanup",
        }
    )
    urgent_followup = bool(
        ready > 0
        or pending > 0
        or llm_completed
        or send_completed
        or send_failed
        or unread_or_capture_changed
    )
    return {
        "scheduler_enabled": True,
        "busy": busy,
        "urgent_followup": urgent_followup,
        "pending_sessions": pending,
        "llm_running": running,
        "reply_ready": ready,
        "reply_sent": sent,
        "event_names": sorted(event_names),
    }


def status_message_from_result(result: dict, duration: float) -> str:
    if not isinstance(result, dict) or result.get("ok") is False:
        return f"本轮处理没有成功，已等待下一轮自动重试。耗时 {duration} 秒。"
    if result.get("scheduler_enabled"):
        summary = result.get("summary") if isinstance(result.get("summary"), dict) else {}
        sent = int(summary.get("reply_sent") or 0)
        ready = int(summary.get("reply_ready") or 0)
        running = int(summary.get("llm_running") or 0)
        pending = int(summary.get("pending_sessions") or 0)
        if sent:
            return f"并发调度已发送回复，队列：待读 {pending}，思考中 {running}，待发 {ready}。耗时 {duration} 秒。"
        if ready or running or pending:
            return f"并发调度运行中，队列：待读 {pending}，思考中 {running}，待发 {ready}。耗时 {duration} 秒。"
        return f"并发调度本轮未发现新消息。耗时 {duration} 秒。"
    events = [item for item in result.get("events", []) or [] if isinstance(item, dict)]
    if not events:
        return f"本轮未发现需要回复的新消息。耗时 {duration} 秒。"
    actions = {str(item.get("action") or "") for item in events}
    if "sent" in actions or "handoff_sent" in actions:
        return f"本轮已处理并发送回复。耗时 {duration} 秒。"
    if "skipped" in actions and actions <= {"skipped"}:
        return f"本轮没有可自动回复的新消息。耗时 {duration} 秒。"
    return f"本轮微信消息检查完成。耗时 {duration} 秒。"


def append_log(path: Path, payload: dict) -> None:
    record = {"created_at": datetime.now().isoformat(timespec="seconds"), **payload}
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as exc:
        import traceback
        crash_log = APP_ROOT / "logs" / "listener_crash.log"
        crash_log.parent.mkdir(parents=True, exist_ok=True)
        with crash_log.open("a", encoding="utf-8") as f:
            f.write(
                json.dumps(
                    {
                        "created_at": datetime.now().isoformat(timespec="seconds"),
                        "event": "managed_listener_crash",
                        "error": str(exc),
                        "traceback": traceback.format_exc(),
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
        raise
