"""Windows Win32/OCR sidecar for the WeChat desktop recorder.

This adapter is designed as the primary transport because it relies only on
the top-level Win32 window, screenshots, OCR, clipboard paste, and guarded
click/input flows. It is the Windows adaptation of WeChat control. Windows 1920x1080
WeChat has different UI geometry and should use a separate platform adapter
rather than reusing these coordinates blindly.
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import io
import json
import os
import random
import re
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path
from typing import Any, Callable

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

try:
    import pyperclip as _pyperclip
except Exception:  # pragma: no cover - optional clipboard convenience package.
    _pyperclip = None

try:
    import win32api
    import win32con
    import win32gui
    import win32process
    import win32ui
    _WIN32_IMPORT_ERROR = ""
except Exception as exc:  # pragma: no cover - allows pure parser tests without pywin32.
    win32api = None  # type: ignore[assignment]
    win32gui = None  # type: ignore[assignment]
    win32process = None  # type: ignore[assignment]
    win32ui = None  # type: ignore[assignment]
    _WIN32_IMPORT_ERROR = repr(exc)

    class _Win32ConFallback:
        VK_CONTROL = 0x11
        VK_ESCAPE = 0x1B
        VK_BACK = 0x08
        VK_DELETE = 0x2E
        VK_DOWN = 0x28
        VK_RETURN = 0x0D
        VK_LBUTTON = 0x01
        KEYEVENTF_KEYUP = 0x0002
        MOUSEEVENTF_MOVE = 0x0001
        MOUSEEVENTF_LEFTDOWN = 0x0002
        MOUSEEVENTF_LEFTUP = 0x0004
        MOUSEEVENTF_RIGHTDOWN = 0x0008
        MOUSEEVENTF_RIGHTUP = 0x0010
        MOUSEEVENTF_WHEEL = 0x0800
        WM_MOUSEWHEEL = 0x020A

    win32con = _Win32ConFallback()  # type: ignore[assignment]
from PIL import Image, ImageDraw, ImageFont, ImageGrab, ImageStat

from apps.wechat_ai_customer_service.adapters.add_friend_actions import (
    ACTION_COMPOSITE_INPUT,
    make_action_result,
)
from apps.wechat_ai_customer_service.adapters.add_friend_artifacts import (
    ADD_FRIEND_ENTRY_CLICK_PLAN_JSON,
    add_friend_route_artifact_root,
)
from apps.wechat_ai_customer_service.adapters.add_friend_contract import (
    normalize_add_friend_query,
    validate_add_friend_entry_click_contract,
)
from apps.wechat_ai_customer_service.adapters.add_friend_diagnostics import (
    step_events_from_review_rows,
    write_step_event_report,
)
from apps.wechat_ai_customer_service.adapters.add_friend_flow_events import add_friend_entry_click_events_from_payload
from apps.wechat_ai_customer_service.adapters.add_friend_flow_context import AddFriendFlowContext
from apps.wechat_ai_customer_service.adapters.add_friend_flow import (
    add_friend_entry_click_task_outcome,
    run_add_friend_entry_click_plan_flow,
)
from apps.wechat_ai_customer_service.adapters.add_friend_layout import (
    invite_form_field_verification,
    plus_entry_target as layout_plus_entry_target,
    semantic_invite_form_targets,
    windows_1080p_reference_plus_point,
    windows_plus_point,
)
from apps.wechat_ai_customer_service.adapters.add_friend_locator import (
    LOCATOR_RESULT_FIELDS,
    fixed_geometry_locator,
    geometry_fallback_locator,
    make_locator_result,
    ocr_item_locator,
)
from apps.wechat_ai_customer_service.adapters.add_friend_operator_guard import (
    add_friend_operator_guard_checkpoint,
    start_add_friend_operator_guard,
    stop_add_friend_operator_guard,
)
from apps.wechat_ai_customer_service.adapters.add_friend_ocr import (
    compact_ocr_text as mapped_compact_ocr_text,
    ocr_item_text as mapped_ocr_item_text,
    ocr_surface_text as mapped_ocr_surface_text,
    ocr_text_has_any as mapped_ocr_text_has_any,
)
from apps.wechat_ai_customer_service.adapters.add_friend_pacing import pacing_metadata, pacing_range
from apps.wechat_ai_customer_service.adapters.add_friend_payloads import (
    add_friend_add_contact_entry_not_found_payload,
    add_friend_after_confirm_payload,
    add_friend_invite_form_window_not_found_payload,
    add_friend_phone_not_found_payload,
    add_friend_task_payload_invalid,
)
from apps.wechat_ai_customer_service.adapters.add_friend_result_mapping import (
    ERROR_ACCOUNT_RESTRICTED,
    ERROR_INVITE_FIELD_VERIFICATION_FAILED,
    ERROR_OPERATOR_GUARD_NOT_READY,
    ERROR_PHONE_NOT_FOUND,
    ERROR_TASK_PAYLOAD_INVALID,
    ERROR_WECHAT_WINDOW_NOT_READY,
    RESULT_ALREADY_FRIEND,
    RESULT_INVITE_SENT,
    add_friend_completed_result as mapped_add_friend_completed_result,
    add_friend_failed_result as mapped_add_friend_failed_result,
    add_friend_server_report_payload as mapped_add_friend_server_report_payload,
)
from apps.wechat_ai_customer_service.adapters.add_friend_routes import (
    ADD_FRIEND_MAIN_ROUTE,
    ADD_FRIEND_ROUTES,
    ADD_FRIEND_WINDOWS_ROUTE,
    add_friend_route_accepts_formal_fields,
    add_friend_route_accepts_query,
    add_friend_route_uses_passive_probe,
)
from apps.wechat_ai_customer_service.adapters.add_friend_screenshot import save_screenshot_artifact
from apps.wechat_ai_customer_service.wechat_message_envelope import (
    apply_message_envelope_to_record,
    build_message_envelope,
)
from apps.wechat_ai_customer_service.adapters.wechat_win32_ocr import geometry as win32_ocr_geometry
from apps.wechat_ai_customer_service.adapters.wechat_win32_ocr import capture as win32_ocr_capture
from apps.wechat_ai_customer_service.adapters.wechat_win32_ocr import env_config as win32_ocr_env
from apps.wechat_ai_customer_service.adapters.wechat_win32_ocr import humanized_input as win32_ocr_humanized
from apps.wechat_ai_customer_service.adapters.wechat_win32_ocr import device_profile as win32_ocr_device_profile
from apps.wechat_ai_customer_service.adapters.wechat_win32_ocr import ocr_engine as win32_ocr_engine
from apps.wechat_ai_customer_service.adapters.wechat_win32_ocr import render_diagnostics as win32_ocr_render
from apps.wechat_ai_customer_service.adapters.wechat_win32_ocr import send_action_risk as win32_ocr_send_action_risk
from apps.wechat_ai_customer_service.adapters.wechat_win32_ocr import session_targeting as win32_ocr_session_targeting
from apps.wechat_ai_customer_service.adapters.wechat_win32_ocr import text_normalization as win32_ocr_text
from apps.wechat_ai_customer_service.adapters.wechat_win32_ocr import window_activation as win32_ocr_window_activation
from apps.wechat_ai_customer_service.adapters.wechat_win32_ocr import window_action_planning as win32_ocr_window_actions
from apps.wechat_ai_customer_service.adapters.wechat_win32_ocr import window_action_state as win32_ocr_window_state
from apps.wechat_ai_customer_service.adapters.wechat_win32_ocr import window_visibility as win32_ocr_window_visibility
from apps.wechat_ai_customer_service.adapters.wechat_win32_ocr import window_metrics as win32_ocr_window_metrics
from apps.wechat_ai_customer_service.adapters.wechat_win32_ocr import windowing as win32_ocr_windowing
from apps.wechat_ai_customer_service.adapters.wechat_win32_ocr import add_friend_windows as win32_ocr_add_friend_windows

try:
    from rapidocr_onnxruntime import RapidOCR
    _OCR_IMPORT_ERROR = ""
except Exception as exc:  # pragma: no cover - OCR is only needed for live sidecar actions.
    RapidOCR = None  # type: ignore[assignment]
    _OCR_IMPORT_ERROR = repr(exc)


SEARCH_BOX_REL = (110, 70)
SESSION_CLICK_X = 260
CHAT_HEADER_MAX_Y = 90
CHAT_INPUT_BOTTOM_OFFSET = 52
DEFAULT_MESSAGE_BOTTOM_EXCLUDE_PX = 95
OCR_MIN_CONFIDENCE = 0.45
SIDECAR_BASE_ACTIONS = ("status", "capabilities", "sessions", "messages", "send", "recover-render", "voice-transcribe")
SIDECAR_ACTION_CHOICES = (*SIDECAR_BASE_ACTIONS, *ADD_FRIEND_ROUTES)
SEND_GUARD_PATH = PROJECT_ROOT / "runtime" / "wechat_win32_ocr_send_guard.json"
UI_ACTION_GUARD_PATH = PROJECT_ROOT / "runtime" / "wechat_win32_ocr_ui_action_guard.json"
UI_ACTION_AUDIT_PATH = PROJECT_ROOT / "runtime" / "wechat_win32_ocr_ui_actions.jsonl"
_LAST_ACTIVATE_MONOTONIC_BY_HWND: dict[int, float] = {}
_LAST_RPA_ACTION_STATE: dict[str, Any] = {}
_LAST_OPEN_CHAT_TIMING: dict[str, Any] = {}
_LAST_SESSION_ACTIVATION_TIMING: dict[str, Any] = {}
RENDER_RECOVERY_GUARD_PATH = PROJECT_ROOT / "runtime" / "wechat_win32_ocr_render_recovery_guard.json"
MIN_SEND_CLIENT_WIDTH = 700
MIN_SEND_CLIENT_HEIGHT = 720
LOGIN_WINDOW_MAX_WIDTH = 560
LOGIN_WINDOW_MAX_HEIGHT = 680
DEFAULT_SAFE_WINDOW_WIDTH = 980
DEFAULT_SAFE_WINDOW_HEIGHT = 860
MIN_SAFE_WINDOW_WIDTH = MIN_SEND_CLIENT_WIDTH
MIN_SAFE_WINDOW_HEIGHT = MIN_SEND_CLIENT_HEIGHT
MAX_SAFE_WINDOW_WIDTH = 2560
MAX_SAFE_WINDOW_HEIGHT = 1600
MIN_CAPTURE_WINDOW_WIDTH = 420
MIN_CAPTURE_WINDOW_HEIGHT = 260
OFFSCREEN_GEOMETRY_BOUNDARY = -30000
DEFAULT_SEND_MIN_INTERVAL_SECONDS = 30
DEFAULT_SEND_BURST_WINDOW_SECONDS = 600
DEFAULT_SEND_BURST_LIMIT = 5
DEFAULT_SEND_MODE = "uia_first"
DEFAULT_UI_ACTION_BUDGET_WINDOW_SECONDS = 60
DEFAULT_UI_ACTION_BUDGET_LIMIT = 80
DEFAULT_UI_ACTION_KEYBOARD_MIN_GAP_MS = 34
DEFAULT_UI_ACTION_MOUSE_MIN_GAP_MS = 110
DEFAULT_UI_ACTION_SCROLL_MIN_GAP_MS = 140
DEFAULT_UI_ACTION_FOCUS_MIN_GAP_MS = 180
DEFAULT_UI_ACTION_KIND_SWITCH_GAP_MS = 170
DEFAULT_UI_ACTION_NEAR_POINT_RADIUS_PX = 7
DEFAULT_UI_ACTION_NEAR_POINT_GAP_MS = 720
DEFAULT_UI_ACTION_NEAR_POINT_SOFT_LIMIT = 2
VOICE_TRANSCRIBE_TEXT_TOKENS = ("转文字", "语音转文字", "转为文字", "转写")
DEFAULT_RENDER_RECOVERY_MIN_INTERVAL_SECONDS = 180
DEFAULT_QUICK_LOGIN_AUTO_ENTER = False
DEFAULT_TARGET_READY_MAX_ATTEMPTS = 1
DEFAULT_TARGET_READY_SWITCH_VALIDATION_CACHE_SECONDS = 4.0
DEFAULT_TARGET_READY_PREVALIDATION_OCR_SEED_SECONDS = 1.5
DEFAULT_ACTIVE_SEND_TARGET_ROI_OCR = False
DEFAULT_INPUT_REGION_PRECHECK_OCR_SEED_SECONDS = 3.0
BLANK_RENDER_BRIGHT_MIN = 238.0
BLANK_RENDER_DARK_MAX = 18.0
BLANK_RENDER_STDDEV_MAX = 8.0
BLANK_RENDER_DENSE_RATIO_MIN = 0.93
BLANK_RENDER_BORDERED_BRIGHT_MIN = 245.0
BLANK_RENDER_BORDERED_DENSE_RATIO_MIN = 0.965
DEFAULT_HUMANIZED_INPUT_ENABLED = True
DEFAULT_HUMANIZED_INPUT_METHOD = "sendinput_unicode"
DEFAULT_HUMANIZED_INPUT_ENFORCE_INTERMITTENT = True
DEFAULT_HUMANIZED_ALLOW_CLIPBOARD_ONCE = False
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
DEFAULT_INPUT_COPYBACK_STRONG_CONFIRM = False
DEFAULT_SEND_INPUT_CONFIRM_ATTEMPTS = 3
DEFAULT_INPUT_FAST_VISUAL_CONFIRM = False
DEFAULT_INPUT_CONFIRM_ROI_OCR = True
DEFAULT_POST_SEND_STRICT_CONFIRM = False
DEFAULT_SEND_TRIGGER_MODE = "enter_only"
DEFAULT_STRICT_SEND_FOCUS_GUARD = True
DEFAULT_FOCUS_CLICK_FALLBACK = True
DEFAULT_ALLOW_UNKNOWN_FOREGROUND_GUARD = True
INPUT_TEXT_DARK_RATIO_MIN = 0.0025
INPUT_TEXT_SOFT_BLANK_DARK_RATIO_MAX = 0.035
INPUT_TEXT_SOFT_BLANK_MEAN_MIN = 242.0
HUMANIZED_TYPO_CANDIDATES = "asdfjkl;,.?/[]"
SENDINPUT_INPUT_KEYBOARD = 1
SENDINPUT_KEYEVENTF_KEYUP = 0x0002
SENDINPUT_KEYEVENTF_UNICODE = 0x0004
HARD_BLOCKING_SCREEN_TOKENS = (
    "文件名无效",
    "存储空间已满",
    "无法继续使用微信",
    "清理出足够存储空间",
)
SOFT_BLOCKING_SCREEN_TOKENS = (
    "选择文件",
    "安全验证",
    "账号安全",
    "登录环境异常",
    "操作频繁",
    "拖拽",
)
WECHAT_LOGIN_OR_SECURITY_BLOCK_TOKENS = (
    "请重新登录",
    "重新登录",
    "登录已过期",
    "登录失效",
    "退出登录",
    "无法继续使用微信",
    "账号安全",
    "安全验证",
    "登录环境异常",
    "操作频繁",
    "账号异常",
    "被限制",
    "限制使用",
)
FOREIGN_CAPTURE_TOKENS = (
    "apps/wechat_ai_customer_servic",
    "new project",
    "展开显示",
    "文件已更改",
    "serverchan",
    "要求后续变更",
)

_OCR_ENGINE: RapidOCR | None = None
_TARGET_READY_PREVALIDATION_OCR_SEED: dict[str, Any] = {}
_INPUT_REGION_PRECHECK_OCR_SEED: dict[str, Any] = {}
_OCR_TRACE_STACK: list[list[dict[str, Any]]] = []


def _sidecar_timing_merge_prefixed(timing: dict[str, Any], prefix: str, nested: dict[str, Any]) -> None:
    for key, value in dict(nested or {}).items():
        merged_key = f"{prefix}_{key}"
        if merged_key in timing:
            continue
        timing[merged_key] = value


def _sidecar_timing_merge_validation(timing: dict[str, Any], prefix: str, validation: dict[str, Any] | None) -> None:
    if not isinstance(validation, dict):
        return
    nested = validation.get("timing")
    if isinstance(nested, dict):
        _sidecar_timing_merge_prefixed(timing, prefix, nested)


def _sidecar_timing_merge_ocr_trace(timing: dict[str, Any], prefix: str, trace: list[dict[str, Any]] | None) -> None:
    if not trace:
        return
    calls = [dict(item) for item in trace if isinstance(item, dict)]
    if not calls:
        return
    timing[f"{prefix}_ocr_call_count"] = len(calls)
    timing[f"{prefix}_ocr_total_duration_seconds"] = round(
        sum(float(item.get("duration_seconds") or 0.0) for item in calls),
        4,
    )
    timing[f"{prefix}_ocr_calls"] = calls


def _ocr_trace_start() -> int:
    _OCR_TRACE_STACK.append([])
    return len(_OCR_TRACE_STACK) - 1


def _ocr_trace_finish(token: int) -> list[dict[str, Any]]:
    if not _OCR_TRACE_STACK:
        return []
    if token != len(_OCR_TRACE_STACK) - 1:
        return list(_OCR_TRACE_STACK[token]) if 0 <= token < len(_OCR_TRACE_STACK) else []
    return _OCR_TRACE_STACK.pop()


def _ocr_image_size(image: Any) -> tuple[int, int]:
    size = getattr(image, "size", (0, 0))
    try:
        return int(size[0] or 0), int(size[1] or 0)
    except Exception:
        return 0, 0


def _ocr_trace_record(
    *,
    purpose: str,
    image: Any,
    duration_seconds: float,
    count: int,
    region: str = "full",
    source: str = "",
) -> None:
    if not _OCR_TRACE_STACK:
        return
    width, height = _ocr_image_size(image)
    record = {
        "purpose": str(purpose or "unspecified"),
        "region": str(region or "full"),
        "source": str(source or ""),
        "width": width,
        "height": height,
        "duration_seconds": round(max(0.0, float(duration_seconds or 0.0)), 4),
        "count": int(count or 0),
    }
    _OCR_TRACE_STACK[-1].append(record)


def run_ocr_traced(image: Any, purpose: str, *, region: str = "full", source: str = "") -> list[dict[str, Any]]:
    started = time.perf_counter()
    items = run_ocr(image)
    _ocr_trace_record(
        purpose=purpose,
        image=image,
        duration_seconds=time.perf_counter() - started,
        count=len(items),
        region=region,
        source=source,
    )
    return items


def _sidecar_timing_now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


def _sidecar_timing_start(timing: dict[str, Any], prefix: str) -> float:
    timing[f"{prefix}_started_at"] = _sidecar_timing_now_iso()
    return time.perf_counter()


def _sidecar_timing_finish(timing: dict[str, Any], prefix: str, started_perf: float | None) -> None:
    if started_perf is None:
        return
    timing[f"{prefix}_finished_at"] = _sidecar_timing_now_iso()
    timing[f"{prefix}_duration_seconds"] = round(max(0.0, time.perf_counter() - started_perf), 4)


def clipboard_copy(text: str) -> None:
    if _pyperclip is not None:
        _pyperclip.copy(text)
        return
    try:
        import tkinter as tk

        root = tk.Tk()
        root.withdraw()
        root.clipboard_clear()
        root.clipboard_append(text)
        root.update()
        root.destroy()
        return
    except Exception as exc:
        raise RuntimeError("clipboard_copy_unavailable: install pyperclip or enable tkinter clipboard support") from exc


def clipboard_read() -> str:
    if _pyperclip is not None:
        return str(_pyperclip.paste() or "")
    try:
        import tkinter as tk

        root = tk.Tk()
        root.withdraw()
        value = root.clipboard_get()
        root.destroy()
        return str(value or "")
    except Exception as exc:
        raise RuntimeError("clipboard_read_unavailable: install pyperclip or enable tkinter clipboard support") from exc


def main() -> int:
    configure_dpi_awareness()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=SIDECAR_ACTION_CHOICES, nargs="?")
    parser.add_argument("--sidecar-run-id", default="", help="Correlation id for one Worker-to-sidecar run.")
    parser.add_argument("--target", help="Chat name for messages/send.")
    parser.add_argument("--session-key", default="", help="Internal session key for row-level RPA targeting.")
    parser.add_argument("--target-mode", default="", help="Targeting mode for messages, e.g. search_by_remark_code.")
    parser.add_argument("--text", help="Message text for send.")
    parser.add_argument("--phone", default="", help="Phone number for add-friend.")
    parser.add_argument("--wechat", default="", help="WeChat ID for add-friend fallback.")
    parser.add_argument("--verify-message", default="", help="Required add-friend verification message for the entry-click route.")
    parser.add_argument("--remark-name", default="", help="Required WeChat remark name for the entry-click route.")
    parser.add_argument("--remark-code", default="", help="Required system remark code that must be included in remark-name.")
    parser.add_argument("--calibration-only", action="store_true", help="For add-friend routes, capture/OCR/locate/report without clicking.")
    parser.add_argument("--exact", action="store_true", help="Use exact chat name matching.")
    parser.add_argument(
        "--skip-send-rate-guard",
        action="store_true",
        help="Skip rate guard reservation for controlled loopback simulation only.",
    )
    parser.add_argument("--history-load-times", type=int, default=0, help="Scroll upward this many times before reading messages.")
    parser.add_argument("--history-mode", default="", help="History loading strategy, e.g. anchor_until_found.")
    parser.add_argument("--anchor-id", action="append", default=[], help="Message id anchor to stop bounded history search.")
    parser.add_argument("--anchor-content-key", action="append", default=[], help="Normalized customer message content key anchor.")
    parser.add_argument("--reply-content-key", action="append", default=[], help="Normalized self reply content key anchor.")
    parser.add_argument("--max-scroll-steps", type=int, default=6, help="Maximum bounded upward scroll steps for anchor history search.")
    parser.add_argument("--max-duration-seconds", type=int, default=12, help="Maximum bounded anchor history search duration.")
    parser.add_argument("--max-snapshots", type=int, default=8, help="Maximum screenshots during anchor history search.")
    parser.add_argument("--min-delay-ms", type=int, default=180, help="Minimum pause between bounded anchor search scrolls.")
    parser.add_argument("--max-delay-ms", type=int, default=650, help="Maximum pause between bounded anchor search scrolls.")
    parser.add_argument("--restore-to-latest", dest="restore_to_latest", action="store_true", default=None)
    parser.add_argument("--no-restore-to-latest", dest="restore_to_latest", action="store_false")
    parser.add_argument("--artifact-dir", help="Optional directory for debug screenshots.")
    args = parser.parse_args()

    captured = io.StringIO()
    try:
        payload = run_action(args)
    except Exception as exc:
        payload = exception_payload_for_sidecar(exc, state="win32_ocr_failed")

    logs = captured.getvalue().strip()
    if logs:
        payload["library_stdout"] = logs
    # This JSON is consumed by parent processes over stdout on Windows.
    # Keep it ASCII-safe so Chinese OCR/window text round-trips after json.loads.
    print(json.dumps(payload, ensure_ascii=True, indent=2))
    return 0 if payload.get("ok") else 1


def run_action(args: argparse.Namespace) -> dict[str, Any]:
    action = str(args.action or "").strip().lower()
    if action in ADD_FRIEND_ROUTES:
        validation = validate_add_friend_entry_click_contract(
            phone=str(args.phone or ""),
            wechat=str(args.wechat or ""),
            verify_message=str(args.verify_message or ""),
            remark_name=str(args.remark_name or ""),
            remark_code=str(args.remark_code or ""),
        )
        if not validation.get("ok"):
            return add_friend_entry_click_validation_failure_payload(
                phone=str(args.phone or ""),
                wechat=str(args.wechat or ""),
                verify_message=str(args.verify_message or ""),
                remark_name=str(args.remark_name or ""),
                remark_code=str(args.remark_code or ""),
                artifact_dir=args.artifact_dir,
                probe={"skipped": True, "reason": "task_payload_invalid_before_window_probe"},
            )
    if _WIN32_IMPORT_ERROR:
        return {
            "ok": False,
            "online": False,
            "adapter": "win32_ocr",
            "state": "pywin32_unavailable",
            "error": _WIN32_IMPORT_ERROR,
        }
    passive_probe = use_passive_probe_mode(action)
    probe = ensure_visible_wechat_window(interactive=not passive_probe)
    if not probe.get("visible_main_windows"):
        if wechat_main_window_is_tray_hidden(probe):
            return {
                "ok": False,
                "online": False,
                "adapter": "win32_ocr",
                "scheme": "win32_ocr_window_in_tray",
                "state": "main_window_in_tray",
                "reason": "wechat_window_in_tray",
                "window_probe": probe,
                "receive": {"ok": False},
                "send": {"ok": False},
                "manual_action_required": "open_wechat_main_window",
                "error": "WeChat is running but its main window is hidden in tray. Open the main window manually before automation.",
            }
        return {
            "ok": False,
            "online": False,
            "adapter": "win32_ocr",
            "state": "main_window_not_found",
            "window_probe": probe,
            "error": "No visible WeChat main window was found.",
        }
    window = select_primary_visible_main_window(probe)
    if not window:
        return {
            "ok": False,
            "online": False,
            "adapter": "win32_ocr",
            "state": "main_window_not_found",
            "window_probe": probe,
            "error": "No visible WeChat main window was selected.",
        }
    probe["selected_main_window"] = dict(window)
    hwnd = int(window.get("hwnd") or 0)
    if not hwnd:
        return {
            "ok": False,
            "online": False,
            "adapter": "win32_ocr",
            "state": "main_window_not_found",
            "window_probe": probe,
            "error": "Visible WeChat window did not expose an hwnd.",
        }

    probe["passive_probe"] = passive_probe
    if not passive_probe:
        foreground_blank_dismissal = dismiss_blank_foreground_window_before_activation(hwnd, artifact_dir=args.artifact_dir)
        if foreground_blank_dismissal.get("attempted"):
            probe["foreground_blank_dismissal"] = foreground_blank_dismissal
        activate_window(hwnd)
        normalized_window = normalize_wechat_window(hwnd)
        probe["window_normalization"] = normalized_window
        if normalized_window.get("applied"):
            humanized_action_sleep(210, 330)
        quick_login = ensure_quick_login_if_available(
            hwnd,
            artifact_dir=args.artifact_dir,
            auto_enter=env_flag("WECHAT_WIN32_OCR_QUICK_LOGIN_AUTO_ENTER", default=DEFAULT_QUICK_LOGIN_AUTO_ENTER),
        )
        probe["quick_login"] = quick_login
        if quick_login.get("attempted"):
            humanized_action_sleep(380, 560)
        humanized_action_sleep(140, 260)
    else:
        probe["window_normalization"] = {
            "ok": True,
            "enabled": False,
            "applied": False,
            "reason": "passive_probe_mode",
        }
        probe["quick_login"] = {
            "attempted": False,
            "detected": False,
            "reason": "passive_probe_mode",
        }
        humanized_action_sleep(35, 80)
    if action == "status":
        return status_payload(hwnd, probe, artifact_dir=args.artifact_dir)
    if action == "capabilities":
        return capabilities_payload(hwnd, probe, artifact_dir=args.artifact_dir)
    if action == "recover-render":
        return recover_blank_render_payload(hwnd, probe, artifact_dir=args.artifact_dir)
    if action == "sessions":
        return sessions_payload(hwnd, probe, artifact_dir=args.artifact_dir)
    if action == "messages":
        clean_sidecar_run_id = str(getattr(args, "sidecar_run_id", "") or "").strip()
        if args.target:
            targeting: dict[str, Any] = {}
            clean_session_key = str(args.session_key or "").strip()
            clean_remark_code = str(args.remark_code or "").strip()
            target_mode = str(args.target_mode or "").strip().lower()
            opened = False
            if target_mode == "search_by_remark_code":
                targeting = open_chat_by_remark_code_search(
                    hwnd,
                    target=args.target,
                    remark_code=clean_remark_code,
                    artifact_dir=args.artifact_dir,
                    sidecar_run_id=clean_sidecar_run_id,
                )
                opened = bool(targeting.get("ok"))
                validation = targeting.get("validation") if isinstance(targeting.get("validation"), dict) else {
                    "ok": bool(opened),
                    "reason": str(targeting.get("reason") or ""),
                }
            else:
                validation = (
                    {"ok": False, "reason": "session_key_requires_row_activation"}
                    if clean_session_key
                    else validate_active_send_target(
                        hwnd,
                        args.target,
                        exact=bool(args.exact),
                        artifact_dir=args.artifact_dir,
                    )
                )
                if not validation.get("ok"):
                    opened = open_chat(
                        hwnd,
                        args.target,
                        exact=bool(args.exact),
                        artifact_dir=args.artifact_dir,
                        session_key=clean_session_key,
                    )
                    humanized_action_sleep(380, 620)
                    validation = validate_active_send_target(
                        hwnd,
                        args.target,
                        exact=bool(args.exact),
                        artifact_dir=args.artifact_dir,
                    )
                if validation.get("ok") and clean_remark_code:
                    remark_validation = validate_active_send_target(
                        hwnd,
                        clean_remark_code,
                        exact=False,
                        artifact_dir=args.artifact_dir,
                    )
                    targeting = {
                        "ok": bool(remark_validation.get("ok")),
                        "target_mode": "visible",
                        "remark_code": clean_remark_code,
                        "display_name_validation": validation,
                        "validation": remark_validation,
                    }
                    validation = remark_validation
            if not validation.get("ok"):
                targeting_review_path = targeting.get("review_path") if isinstance(targeting, dict) else None
                targeting_evidence_path = targeting.get("evidence_path") if isinstance(targeting, dict) else None
                return {
                    "ok": False,
                    "online": bool(validation.get("online", True)),
                    "adapter": "win32_ocr",
                    "state": "target_not_confirmed_for_messages",
                    "sidecar_run_id": clean_sidecar_run_id,
                    "error_code": str(targeting.get("error_code") or validation.get("error_code") or "TARGET_NOT_CONFIRMED_FOR_MESSAGES"),
                    "window_probe": probe,
                    "target": args.target,
                    "remark_code": clean_remark_code,
                    "target_mode": target_mode or "visible",
                    "opened": bool(opened),
                    "guard": validation,
                    "targeting": targeting,
                    "step_events": targeting.get("step_events") if isinstance(targeting, dict) else None,
                    "review_path": targeting_review_path,
                    "evidence_path": targeting_evidence_path,
                    "open_chat_timing": dict(_LAST_OPEN_CHAT_TIMING),
                    "error": "The target chat was not confirmed before reading messages.",
                }
            if scroll_to_latest_before_read_enabled():
                scroll_chat_to_latest(hwnd)
        load_times = bounded_int(args.history_load_times, default=0, minimum=0, maximum=16)
        payload = messages_payload(
            hwnd,
            probe,
            target=args.target or "",
            history_load_times=load_times,
            history_mode=str(args.history_mode or ""),
            anchor_ids=[str(item) for item in args.anchor_id or []],
            anchor_content_keys=[str(item) for item in args.anchor_content_key or []],
            reply_content_keys=[str(item) for item in args.reply_content_key or []],
            max_scroll_steps=bounded_int(args.max_scroll_steps, default=6, minimum=0, maximum=16),
            max_duration_seconds=bounded_int(args.max_duration_seconds, default=12, minimum=1, maximum=60),
            max_snapshots=bounded_int(args.max_snapshots, default=8, minimum=1, maximum=24),
            min_delay_ms=bounded_int(args.min_delay_ms, default=180, minimum=0, maximum=5000),
            max_delay_ms=bounded_int(args.max_delay_ms, default=650, minimum=0, maximum=10000),
            restore_to_latest=True if args.restore_to_latest is None else bool(args.restore_to_latest),
            artifact_dir=args.artifact_dir,
        )
        if args.target:
            payload["sidecar_run_id"] = clean_sidecar_run_id
            payload["target_mode"] = str(args.target_mode or "").strip().lower() or "visible"
            payload["remark_code"] = str(args.remark_code or "").strip()
            payload["targeting"] = targeting if isinstance(targeting, dict) else {}
            if isinstance(targeting, dict) and targeting.get("step_events"):
                payload["step_events"] = targeting.get("step_events")
            if isinstance(targeting, dict) and targeting.get("review_path"):
                payload["review_path"] = targeting.get("review_path")
                payload["evidence_path"] = targeting.get("evidence_path") or targeting.get("review_path")
        return payload
    if action == "voice-transcribe":
        if args.target:
            clean_session_key = str(args.session_key or "").strip()
            validation = (
                {"ok": False, "reason": "session_key_requires_row_activation"}
                if clean_session_key
                else validate_active_send_target(
                    hwnd,
                    args.target,
                    exact=bool(args.exact),
                    artifact_dir=args.artifact_dir,
                )
            )
            opened = False
            if not validation.get("ok"):
                opened = open_chat(
                    hwnd,
                    args.target,
                    exact=bool(args.exact),
                    artifact_dir=args.artifact_dir,
                    session_key=clean_session_key,
                )
                humanized_action_sleep(380, 620)
                validation = validate_active_send_target(
                    hwnd,
                    args.target,
                    exact=bool(args.exact),
                    artifact_dir=args.artifact_dir,
                )
            if not validation.get("ok"):
                return {
                    "ok": False,
                    "online": bool(validation.get("online", True)),
                    "adapter": "win32_ocr",
                    "state": "target_not_confirmed_for_voice_transcribe",
                    "window_probe": probe,
                    "target": args.target,
                    "opened": bool(opened),
                    "guard": validation,
                    "open_chat_timing": dict(_LAST_OPEN_CHAT_TIMING),
                    "error": "The target chat was not confirmed before clicking voice transcription.",
                }
            if scroll_to_latest_before_read_enabled():
                scroll_chat_to_latest(hwnd)
        return voice_transcribe_payload(
            hwnd,
            probe,
            target=args.target or "",
            artifact_dir=args.artifact_dir,
        )
    if action == "send":
        if not args.target:
            raise ValueError("--target is required for send")
        if args.text is None:
            raise ValueError("--text is required for send")
        target_ready_timing: dict[str, Any] = {}
        target_ready_started = _sidecar_timing_start(target_ready_timing, "target_ready")
        continuation_fast_path = same_target_continuation_fast_path_enabled()
        if continuation_fast_path:
            target_ready = {
                "ok": True,
                "attempts": 0,
                "validation": None,
                "timing": {
                    "target_ready_continuation_fast_path": True,
                    "target_ready_skipped_for_continuation": True,
                },
            }
        else:
            target_ready = ensure_target_ready_for_send(
                hwnd,
                args.target,
                exact=bool(args.exact),
                artifact_dir=args.artifact_dir,
                session_key=str(args.session_key or ""),
            )
        _sidecar_timing_finish(target_ready_timing, "target_ready", target_ready_started)
        if isinstance(target_ready.get("timing"), dict):
            for key, value in target_ready["timing"].items():
                target_ready_timing.setdefault(str(key), value)
        if not target_ready.get("ok"):
            validation = target_ready.get("validation") or validate_active_send_target(
                hwnd,
                args.target,
                exact=bool(args.exact),
                artifact_dir=args.artifact_dir,
            )
            return {
                "ok": False,
                "online": True,
                "adapter": "win32_ocr",
                "state": "target_not_confirmed",
                "window_probe": probe,
                "target": args.target,
                "attempts": target_ready.get("attempts"),
                "guard": validation,
                "timing": target_ready_timing,
                "error": "The target chat was not confirmed before sending.",
            }
        send_result_payload = send_payload(
            hwnd,
            probe,
            target=args.target,
            text=args.text,
            exact=bool(args.exact),
            skip_send_rate_guard=bool(args.skip_send_rate_guard),
            artifact_dir=args.artifact_dir,
            validated_guard=None
            if continuation_fast_path
            else target_ready.get("validation")
            if isinstance(target_ready.get("validation"), dict)
            else None,
        )
        if isinstance(send_result_payload, dict):
            if continuation_fast_path:
                send_result_payload.setdefault("same_target_continuation_fast_path", True)
            existing_timing = send_result_payload.get("timing")
            merged_timing = dict(target_ready_timing)
            if isinstance(existing_timing, dict):
                merged_timing.update(existing_timing)
            send_result_payload["timing"] = merged_timing
        return send_result_payload
    if action in ADD_FRIEND_ROUTES:
        return add_friend_entry_click_plan_payload(
            hwnd,
            probe,
            route=action,
            phone=str(args.phone or ""),
            wechat=str(args.wechat or ""),
            verify_message=str(args.verify_message or ""),
            remark_name=str(args.remark_name or ""),
            remark_code=str(args.remark_code or ""),
            artifact_dir=args.artifact_dir,
            calibration_only=bool(getattr(args, "calibration_only", False)),
        )
    return {"ok": False, "online": False, "adapter": "win32_ocr", "state": "unsupported_action"}


def use_passive_probe_mode(action: str) -> bool:
    if action in {"status", "capabilities", "sessions"}:
        return env_flag("WECHAT_WIN32_OCR_PASSIVE_PROBE", default=True)
    if not add_friend_route_uses_passive_probe(action):
        return False
    return env_flag("WECHAT_WIN32_OCR_PASSIVE_PROBE", default=True)


def scroll_to_latest_before_read_enabled() -> bool:
    return env_flag("WECHAT_WIN32_OCR_SCROLL_TO_LATEST_BEFORE_READ", default=False)


def same_target_continuation_fast_path_enabled() -> bool:
    return env_flag("WECHAT_WIN32_OCR_CONTINUATION_SEND_FAST_PATH", default=False)


def detect_blank_render(
    screenshot: Any,
    ocr_items: list[dict[str, Any]],
    *,
    geometry: dict[str, Any],
) -> dict[str, Any]:
    return win32_ocr_render.detect_blank_render(screenshot, ocr_items, geometry=geometry)


def auxiliary_wechat_shell_like(ocr_items: list[dict[str, Any]], *, geometry: dict[str, Any]) -> dict[str, Any]:
    """Detect a Tencent/Qt shell window that is not the actual chat surface."""
    texts = [normalize_ocr_text(item.get("text")) for item in ocr_items if normalize_ocr_text(item.get("text"))]
    compact = [re.sub(r"\s+", "", text).lower() for text in texts]
    chat_surface_tokens = (
        "搜索",
        "文件传输助手",
        "发送",
        "聊天",
        "通讯录",
        "订阅号",
        "朋友圈",
        "小程序",
        "视频号",
    )
    if any(token in text for text in texts for token in chat_surface_tokens):
        return {"detected": False, "reason": "", "ocr_count": len(texts)}
    title_only_tokens = {"weixin", "wechat", "微信"}
    title_only = bool(texts) and len(texts) <= 2 and all(text in title_only_tokens for text in compact)
    too_sparse_for_chat = len(texts) <= 1 and int(geometry.get("width") or 0) >= MIN_CAPTURE_WINDOW_WIDTH
    detected = bool(title_only or too_sparse_for_chat)
    if title_only:
        reason = "title_only_shell"
    elif too_sparse_for_chat:
        reason = "sparse_auxiliary_shell"
    else:
        reason = ""
    return {
        "detected": detected,
        "reason": reason,
        "ocr_count": len(texts),
        "texts": texts[:5],
        "geometry": {
            "width": int(geometry.get("width") or 0),
            "height": int(geometry.get("height") or 0),
        },
    }


def service_container_name(text: str) -> str:
    compact = normalize_ocr_text(text).replace(" ", "")
    if not compact:
        return ""
    for token in ("服务号", "订阅号", "公众号"):
        if token in compact:
            return token
    return ""


def target_is_service_container(target: str) -> bool:
    return bool(service_container_name(target))


def active_service_container_wrong_target(
    ocr_items: list[dict[str, Any]],
    image_size: tuple[int, int],
    *,
    target: str,
) -> dict[str, Any]:
    if target_is_service_container(target):
        return {"detected": False}
    width, height = image_size
    if width <= 0 or height <= 0:
        return {"detected": False}
    split_x = session_split_x(width)
    header_bottom = chat_header_cutoff_y(height) + max(58, int(height * 0.08))
    matches: list[dict[str, Any]] = []
    for item in ocr_items:
        text = normalize_ocr_text(item.get("text"))
        token = service_container_name(text)
        if not token:
            continue
        center_y = float(item.get("center_y") or 0)
        right = float(item.get("right") or 0)
        compact = text.replace(" ", "")
        has_back_arrow = compact.startswith(("<", "〈", "‹", "＜"))
        in_service_back_header = has_back_arrow and center_y <= header_bottom and right <= split_x + 72
        in_active_title = (
            center_y <= active_chat_title_bottom_y(height) + 24
            and right > split_x + 8
            and float(item.get("center_x") or 0) >= active_chat_title_left_x(width) - 24
        )
        if not (in_service_back_header or in_active_title):
            continue
        matches.append(
            {
                "text": text,
                "container": token,
                "center_y": center_y,
                "right": right,
                "role": "service_back_header" if in_service_back_header else "active_title",
                "has_back_arrow": has_back_arrow,
            }
        )
    if not matches:
        return {"detected": False}
    return {
        "detected": True,
        "reason": "service_container_wrong_target",
        "requested_target": str(target or ""),
        "matches": matches[:3],
    }


def session_candidate_is_service_container_wrong_target(session: dict[str, Any], target: str) -> bool:
    if target_is_service_container(target):
        return False
    return bool(service_container_name(str(session.get("name") or "")))


def recover_blank_render_payload(hwnd: int, probe: dict[str, Any], *, artifact_dir: str | None = None) -> dict[str, Any]:
    initial = status_payload(hwnd, probe, artifact_dir=artifact_dir)
    initial_snapshot = sidecar_payload_snapshot(initial)
    if initial.get("ok") and initial.get("online"):
        initial["render_recovery"] = {
            "ok": True,
            "attempted": False,
            "reason": "wechat_render_already_ready",
        }
        return initial
    if not sidecar_payload_needs_render_recovery(initial):
        initial["render_recovery"] = {
            "ok": False,
            "attempted": False,
            "reason": "not_recoverable_render_state",
        }
        return initial
    if not env_flag("WECHAT_WIN32_OCR_RENDER_RECOVERY_AUTO", default=False):
        initial["render_recovery"] = {
            "ok": False,
            "attempted": False,
            "reason": "auto_render_recovery_disabled",
            "initial_status": initial_snapshot,
            "suggested_action": "stop_and_report_manual_tray_restore",
        }
        return initial

    reservation = reserve_render_recovery()
    if not reservation.get("ok"):
        initial["render_recovery"] = {
            **reservation,
            "attempted": False,
            "reason": reservation.get("reason") or "render_recovery_rate_limited",
        }
        return initial

    redraw = trigger_wechat_tray_redraw(hwnd, probe)
    humanized_action_sleep(1300, 1900)
    recovered_probe = probe_wechat_windows()
    quick_login_recovery = enter_quick_login_from_visible_windows(recovered_probe, artifact_dir=artifact_dir)
    if quick_login_recovery.get("attempted"):
        humanized_action_sleep(900, 1400)
    recovered_probe = ensure_visible_wechat_window(interactive=True)
    if quick_login_recovery:
        recovered_probe["recovery_quick_login"] = quick_login_recovery
    recovered_window = select_primary_visible_main_window(recovered_probe)
    if not recovered_window:
        initial["render_recovery"] = {
            "ok": False,
            "attempted": True,
            "reason": "main_window_not_found_after_redraw",
            "reservation": reservation,
            "redraw": redraw,
            "window_probe": recovered_probe,
        }
        return initial
    recovered_probe["selected_main_window"] = dict(recovered_window)
    recovered_hwnd = int(recovered_window.get("hwnd") or hwnd)
    if recovered_hwnd:
        activate_window(recovered_hwnd)
    final = status_payload(recovered_hwnd or hwnd, recovered_probe, artifact_dir=artifact_dir)
    final["render_recovery"] = {
        "ok": bool(final.get("ok") and final.get("online")),
        "attempted": True,
        "reason": "tray_redraw_reopen",
        "reservation": reservation,
        "redraw": redraw,
        "quick_login": quick_login_recovery,
        "initial_status": initial_snapshot,
    }
    if final.get("ok") and final.get("online"):
        return final
    initial["render_recovery"] = sidecar_payload_snapshot(final["render_recovery"])
    initial["recovered_status"] = sidecar_payload_snapshot(final)
    return initial


def enter_quick_login_from_visible_windows(probe: dict[str, Any], *, artifact_dir: str | None = None) -> dict[str, Any]:
    """Click a visible quick-login card during explicit render recovery only."""
    visible = list(probe.get("visible_main_windows") or [])
    candidates: list[tuple[int, dict[str, Any], dict[str, Any]]] = []
    for item in visible:
        hwnd = int((item or {}).get("hwnd") or 0)
        if not hwnd:
            continue
        try:
            geometry = get_window_geometry(hwnd)
        except Exception:
            continue
        width = int(geometry.get("width") or 0)
        height = int(geometry.get("height") or 0)
        if width <= 0 or height <= 0:
            continue
        if width > LOGIN_WINDOW_MAX_WIDTH or height > LOGIN_WINDOW_MAX_HEIGHT:
            continue
        candidates.append((width * height, dict(item), geometry))
    candidates.sort(key=lambda row: row[0])
    for _area, item, geometry in candidates:
        hwnd = int(item.get("hwnd") or 0)
        try:
            activate_window(hwnd)
            humanized_action_sleep(160, 280)
            result = ensure_quick_login_if_available(hwnd, artifact_dir=artifact_dir, auto_enter=True)
        except Exception as exc:
            result = {"attempted": False, "detected": False, "error": repr(exc)}
        if result.get("detected"):
            return {
                **result,
                "hwnd": hwnd,
                "window": item,
                "geometry": geometry,
                "mode": "render_recovery_quick_login",
            }
    return {"attempted": False, "detected": False, "reason": "quick_login_window_not_found"}


def sidecar_payload_snapshot(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    return json.loads(json.dumps(payload, ensure_ascii=True, default=str))


def exception_payload_for_sidecar(exc: Exception, *, state: str = "win32_ocr_failed") -> dict[str, Any]:
    error = repr(exc)
    lower_error = error.lower()
    invalid_handle = (
        "getwindowrect" in lower_error
        or "无效的窗口句柄" in error
        or "invalid window handle" in lower_error
    )
    payload = {"ok": False, "online": False, "state": state, "error": error}
    if invalid_handle:
        payload.update(
            {
                "adapter": "win32_ocr",
                "reason": "window_handle_invalid",
                "risk_stop_recommended": True,
                "risk_stop_reason": "win32_invalid_window_handle",
                "manual_action_required": "reopen_or_restore_wechat_main_window",
            }
        )
    return payload


def sidecar_payload_is_blank_render(payload: dict[str, Any]) -> bool:
    if not isinstance(payload, dict):
        return False
    return str(payload.get("state") or "") == "blank_render_detected" or str(payload.get("reason") or "") == "blank_render"


def sidecar_payload_needs_render_recovery(payload: dict[str, Any]) -> bool:
    if not isinstance(payload, dict):
        return False
    if sidecar_payload_is_blank_render(payload):
        return True
    state = str(payload.get("state") or "")
    reason = str(payload.get("reason") or "")
    scheme = str(payload.get("scheme") or "")
    shell_probe = payload.get("shell_probe") if isinstance(payload.get("shell_probe"), dict) else {}
    shell_reason = str(shell_probe.get("reason") or "")
    sparse_shell = (
        state == "auxiliary_shell_window_detected"
        or reason == "auxiliary_shell_window"
        or scheme == "win32_ocr_auxiliary_shell"
    )
    if sparse_shell and shell_reason in {"sparse_auxiliary_shell", "title_only_shell"}:
        return True
    primary = payload.get("primary_status") if isinstance(payload.get("primary_status"), dict) else {}
    if primary:
        return sidecar_payload_needs_render_recovery(primary)
    return False


def reserve_render_recovery() -> dict[str, Any]:
    if env_flag("WECHAT_WIN32_OCR_RENDER_RECOVERY_GUARD", default=True) is False:
        return {"ok": True, "guard_enabled": False, "reason": "render_recovery_guard_disabled"}
    min_interval = bounded_int(
        os.getenv("WECHAT_WIN32_OCR_RENDER_RECOVERY_MIN_INTERVAL_SECONDS"),
        default=DEFAULT_RENDER_RECOVERY_MIN_INTERVAL_SECONDS,
        minimum=30,
        maximum=3600,
    )
    now = time.time()
    previous: dict[str, Any] = {}
    if RENDER_RECOVERY_GUARD_PATH.exists():
        try:
            previous = json.loads(RENDER_RECOVERY_GUARD_PATH.read_text(encoding="utf-8"))
        except Exception:
            previous = {}
    last_at = float(previous.get("last_at") or 0)
    remaining = int(max(0, min_interval - (now - last_at)))
    if last_at > 0 and remaining > 0:
        return {
            "ok": False,
            "guard_enabled": True,
            "reason": "render_recovery_rate_limited",
            "retry_after_seconds": remaining,
            "min_interval_seconds": min_interval,
        }
    RENDER_RECOVERY_GUARD_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = RENDER_RECOVERY_GUARD_PATH.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(
            {
                "last_at": now,
                "last_at_iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now)),
                "min_interval_seconds": min_interval,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    os.replace(tmp, RENDER_RECOVERY_GUARD_PATH)
    return {"ok": True, "guard_enabled": True, "min_interval_seconds": min_interval}


def trigger_wechat_tray_redraw(hwnd: int, probe: dict[str, Any]) -> dict[str, Any]:
    selected = probe.get("selected_main_window") if isinstance(probe.get("selected_main_window"), dict) else {}
    exe_path = str(selected.get("path") or "").strip()
    if not exe_path:
        for item in probe.get("windows") or []:
            candidate = str((item or {}).get("path") or "").strip()
            if candidate.lower().endswith("weixin.exe"):
                exe_path = candidate
                break
    close_posted = False
    launch_attempted = False
    launch_error = ""
    try:
        if hwnd:
            ensure_left_button_released()
            win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
            close_posted = True
            humanized_action_sleep(800, 1200)
    except Exception:
        close_posted = False
    if exe_path and Path(exe_path).exists():
        try:
            subprocess.Popen([exe_path], cwd=str(Path(exe_path).parent))
            launch_attempted = True
        except Exception as exc:
            launch_error = repr(exc)
    else:
        launch_error = "weixin_exe_path_missing"
    return {
        "ok": bool(close_posted and launch_attempted and not launch_error),
        "method": "wm_close_to_tray_then_launch_weixin",
        "close_posted": close_posted,
        "launch_attempted": launch_attempted,
        "exe_path": exe_path,
        "error": launch_error,
    }


def status_payload(hwnd: int, probe: dict[str, Any], *, artifact_dir: str | None = None) -> dict[str, Any]:
    geometry = get_window_geometry(hwnd)
    geometry_check = validate_capture_geometry(geometry)
    focus_guard = foreground_window_matches_target(hwnd)
    if not geometry_check.get("ok"):
        return {
            "ok": False,
            "online": False,
            "adapter": "win32_ocr",
            "state": "main_window_geometry_invalid",
            "reason": str(geometry_check.get("reason") or ""),
            "window_probe": probe,
            "geometry": geometry,
            "focus_guard": focus_guard,
            "screenshot_path": "",
            "ocr_count": 0,
            "compat_reason": "rpa_primary",
            "error": str(geometry_check.get("error") or "WeChat window geometry is not ready for capture."),
        }
    screenshot, path = capture_wechat(hwnd, artifact_dir=artifact_dir, label="status")
    ocr_items = run_ocr(screenshot)
    login_like = quick_login_like(ocr_items, geometry=geometry)
    if login_like:
        return {
            "ok": False,
            "online": False,
            "adapter": "win32_ocr",
            "state": "login_window_detected",
            "window_probe": probe,
            "geometry": geometry,
            "focus_guard": focus_guard,
            "screenshot_path": path,
            "ocr_count": len(ocr_items),
            "compat_reason": "rpa_primary",
            "error": "WeChat is still in quick-login view. Enter WeChat first before running automation.",
        }
    blank_render = detect_blank_render(screenshot, ocr_items, geometry=geometry)
    if blank_render.get("detected"):
        return {
            "ok": False,
            "online": False,
            "adapter": "win32_ocr",
            "state": "blank_render_detected",
            "reason": "blank_render",
            "window_probe": probe,
            "geometry": geometry,
            "focus_guard": focus_guard,
            "screenshot_path": path,
            "ocr_count": len(ocr_items),
            "render_probe": blank_render,
            "compat_reason": "rpa_primary",
            "error": "WeChat window appears blank (render stalled); restart WeChat window before automation.",
        }
    auxiliary_shell = auxiliary_wechat_shell_like(ocr_items, geometry=geometry)
    if auxiliary_shell.get("detected"):
        return {
            "ok": False,
            "online": False,
            "adapter": "win32_ocr",
            "state": "auxiliary_shell_window_detected",
            "reason": "auxiliary_shell_window",
            "window_probe": probe,
            "geometry": geometry,
            "focus_guard": focus_guard,
            "screenshot_path": path,
            "ocr_count": len(ocr_items),
            "shell_probe": auxiliary_shell,
            "compat_reason": "rpa_primary",
            "error": "Selected WeChat window looks like an auxiliary shell, not the logged-in chat window.",
        }
    return {
        "ok": True,
        "online": True,
        "adapter": "win32_ocr",
        "state": "main_window_compat",
        "window_probe": probe,
        "geometry": geometry,
        "focus_guard": focus_guard,
        "screenshot_path": path,
        "ocr_count": len(ocr_items),
        "compat_reason": "rpa_primary",
    }
def capabilities_payload(hwnd: int, probe: dict[str, Any], *, artifact_dir: str | None = None) -> dict[str, Any]:
    geometry = get_window_geometry(hwnd)
    geometry_check = validate_capture_geometry(geometry)
    focus_guard = foreground_window_matches_target(hwnd)
    if not geometry_check.get("ok"):
        return {
            "ok": False,
            "online": False,
            "adapter": "win32_ocr",
            "scheme": "win32_ocr_window_geometry_invalid",
            "state": "main_window_geometry_invalid",
            "reason": str(geometry_check.get("reason") or ""),
            "window_probe": probe,
            "screenshot_path": "",
            "ocr_count": 0,
            "geometry": geometry,
            "focus_guard": focus_guard,
            "receive": {"ok": False},
            "send": {"ok": False},
            "compat_reason": "rpa_primary",
            "error": str(geometry_check.get("error") or "WeChat window geometry is not ready for capture."),
        }
    screenshot, path = capture_wechat(hwnd, artifact_dir=artifact_dir, label="capabilities")
    ocr_items = run_ocr(screenshot)
    if quick_login_like(ocr_items, geometry=geometry):
        return {
            "ok": False,
            "online": False,
            "adapter": "win32_ocr",
            "scheme": "wechat_not_online",
            "state": "login_window_detected",
            "window_probe": probe,
            "screenshot_path": path,
            "ocr_count": len(ocr_items),
            "geometry": geometry,
            "focus_guard": focus_guard,
            "receive": {"ok": False},
            "send": {"ok": False},
            "compat_reason": "rpa_primary",
            "error": "WeChat quick-login view detected; enter WeChat before automation.",
        }
    blank_render = detect_blank_render(screenshot, ocr_items, geometry=geometry)
    if blank_render.get("detected"):
        return {
            "ok": False,
            "online": False,
            "adapter": "win32_ocr",
            "scheme": "win32_ocr_blank_render",
            "state": "blank_render_detected",
            "reason": "blank_render",
            "window_probe": probe,
            "screenshot_path": path,
            "ocr_count": len(ocr_items),
            "geometry": geometry,
            "focus_guard": focus_guard,
            "render_probe": blank_render,
            "receive": {"ok": False, "blocked_by": "blank_render"},
            "send": {"ok": False},
            "compat_reason": "rpa_primary",
            "error": "WeChat window appears blank (render stalled); restart WeChat window before automation.",
        }
    auxiliary_shell = auxiliary_wechat_shell_like(ocr_items, geometry=geometry)
    if auxiliary_shell.get("detected"):
        return {
            "ok": False,
            "online": False,
            "adapter": "win32_ocr",
            "scheme": "win32_ocr_auxiliary_shell",
            "state": "auxiliary_shell_window_detected",
            "reason": "auxiliary_shell_window",
            "window_probe": probe,
            "screenshot_path": path,
            "ocr_count": len(ocr_items),
            "geometry": geometry,
            "focus_guard": focus_guard,
            "shell_probe": auxiliary_shell,
            "receive": {"ok": False, "blocked_by": "auxiliary_shell_window"},
            "send": {"ok": False},
            "compat_reason": "rpa_primary",
            "error": "Selected WeChat window looks like an auxiliary shell, not the logged-in chat window.",
        }
    blocking_reason = blocking_screen_reason(ocr_items)
    online = blocking_reason != "login_or_qr"
    geometry_check = validate_send_geometry(geometry)
    points = calculate_send_points(geometry) if geometry_check.get("ok") else geometry_check
    uia = inspect_uia_send_capability(hwnd, geometry) if geometry_check.get("ok") else {
        "ok": False,
        "reason": "geometry_unavailable_for_uia",
        "geometry": geometry,
    }
    receive = {
        "ok": bool(online and not blocking_reason),
        "method": "win32.screenshot+rapidocr",
        "blocked_by": blocking_reason,
    }
    guarded_click = {
        "ok": bool(online and not blocking_reason and geometry_check.get("ok") and points.get("ok")),
        "method": "win32.human_click_input+rpa_text_entry+human_click_send",
        "geometry": geometry_check,
        "points": points,
        "rate_guard": {
            "enabled": env_flag("WECHAT_WIN32_OCR_SEND_RATE_GUARD", default=True),
            "min_interval_seconds": env_int("WECHAT_WIN32_OCR_SEND_MIN_INTERVAL_SECONDS", DEFAULT_SEND_MIN_INTERVAL_SECONDS),
            "burst_window_seconds": env_int("WECHAT_WIN32_OCR_SEND_BURST_WINDOW_SECONDS", DEFAULT_SEND_BURST_WINDOW_SECONDS),
            "burst_limit": env_int("WECHAT_WIN32_OCR_SEND_BURST_LIMIT", DEFAULT_SEND_BURST_LIMIT),
        },
        "humanized_input": humanized_input_settings(),
    }
    if not online:
        scheme = "wechat_not_online"
    elif blocking_reason:
        scheme = "win32_ocr_blocked"
    elif uia.get("ok"):
        scheme = "win32_ocr_uia"
    elif guarded_click.get("ok"):
        scheme = "win32_ocr_guarded_click"
    elif receive.get("ok"):
        scheme = "win32_ocr_receive_only"
    else:
        scheme = "win32_ocr_unavailable"
    send_ok = bool(uia.get("ok") or guarded_click.get("ok"))
    return {
        "ok": bool(online and receive.get("ok")),
        "online": bool(online),
        "adapter": "win32_ocr",
        "scheme": scheme,
        "state": "capabilities_ocr",
        "window_probe": probe,
        "screenshot_path": path,
        "ocr_count": len(ocr_items),
        "geometry": geometry,
        "focus_guard": focus_guard,
        "blocking_reason": blocking_reason,
        "receive": receive,
        "send": {
            "ok": send_ok,
            "preferred_mode": "uia" if uia.get("ok") else ("guarded_human_click" if guarded_click.get("ok") else ""),
            "uia": uia,
            "guarded_click": guarded_click,
        },
        "compat_reason": "rpa_primary",
    }
def sessions_payload(hwnd: int, probe: dict[str, Any], *, artifact_dir: str | None = None) -> dict[str, Any]:
    screenshot, path = capture_wechat(hwnd, artifact_dir=artifact_dir, label="sessions")
    items = run_ocr(screenshot)
    geometry = get_window_geometry(hwnd)
    page_fingerprint = ocr_page_fingerprint(items, geometry=geometry)
    if quick_login_like(items, geometry=geometry):
        return {
            "ok": False,
            "online": False,
            "adapter": "win32_ocr",
            "state": "login_window_detected",
            "window_probe": probe,
            "screenshot_path": path,
            "ocr_items_count": len(items),
            "error": "WeChat quick-login view detected; enter WeChat before reading sessions.",
        }
    blocking_reason = blocking_screen_reason(items)
    if blocking_reason:
        return {
            "ok": False,
            "online": False if blocking_reason == "login_or_qr" else True,
            "adapter": "win32_ocr",
            "state": "sessions_blocked",
            "window_probe": probe,
            "screenshot_path": path,
            "ocr_items_count": len(items),
            "reason": blocking_reason,
            "error": f"WeChat session list is blocked by: {blocking_reason}",
        }
    sessions = parse_sessions_from_ocr(items, screenshot.size, screenshot=screenshot)
    return {
        "ok": True,
        "online": True,
        "adapter": "win32_ocr",
            "state": "sessions_ocr",
            "window_probe": probe,
            "screenshot_path": path,
            "page_fingerprint": page_fingerprint,
            "passive_probe": bool(probe.get("passive_probe")),
            "sessions": [
            {
                "name": item["name"],
                "title": item["name"],
                "session_key": item.get("session_key", ""),
                "row_fingerprint": item.get("row_fingerprint", {}),
                "duplicate_name_index": item.get("duplicate_name_index", 0),
                "ambiguous_display_name": bool(item.get("ambiguous_display_name")),
                "content": item.get("preview", ""),
                "time": item.get("time", ""),
                "unread_badge": item.get("unread_badge", ""),
                "unread": item.get("unread_badge", ""),
                "unread_signal": bool(item.get("unread_badge")),
                "conversation_type": item.get("conversation_type") or infer_conversation_type(item["name"]),
                "source_adapter": "win32_ocr",
                "ocr_confidence": item.get("confidence"),
            }
            for item in sessions
        ],
        "ocr_items_count": len(items),
    }


def messages_payload(
    hwnd: int,
    probe: dict[str, Any],
    *,
    target: str,
    history_load_times: int,
    history_mode: str = "",
    anchor_ids: list[str] | None = None,
    anchor_content_keys: list[str] | None = None,
    reply_content_keys: list[str] | None = None,
    max_scroll_steps: int = 6,
    max_duration_seconds: int = 12,
    max_snapshots: int = 8,
    min_delay_ms: int = 180,
    max_delay_ms: int = 650,
    restore_to_latest: bool = True,
    artifact_dir: str | None = None,
) -> dict[str, Any]:
    mode = str(history_mode or "").strip().lower()
    if mode == "anchor_until_found":
        snapshots, history_load = capture_message_history_snapshots_until_anchor(
            hwnd,
            target=target,
            anchor_ids=anchor_ids or [],
            anchor_content_keys=anchor_content_keys or [],
            reply_content_keys=reply_content_keys or [],
            max_scroll_steps=max_scroll_steps,
            max_duration_seconds=max_duration_seconds,
            max_snapshots=max_snapshots,
            min_delay_ms=min_delay_ms,
            max_delay_ms=max_delay_ms,
            restore_to_latest=restore_to_latest,
            artifact_dir=artifact_dir,
        )
    else:
        snapshots = capture_message_history_snapshots(
            hwnd,
            target=target,
            history_load_times=history_load_times,
            artifact_dir=artifact_dir,
        )
        history_load = {
            "ok": True,
            "mode": "fixed_load_times",
            "requested_load_times": history_load_times,
            "mechanism": "win32_ocr.WheelUp+ScreenshotOCR",
            "snapshot_count": len(snapshots),
        }
    latest = snapshots[-1] if snapshots else {}
    ocr_items = latest.get("ocr_items", []) if isinstance(latest.get("ocr_items"), list) else []
    geometry = get_window_geometry(hwnd)
    page_fingerprint = ocr_page_fingerprint(ocr_items, geometry=geometry)
    if quick_login_like(ocr_items, geometry=geometry):
        return {
            "ok": False,
            "online": False,
            "adapter": "win32_ocr",
            "state": "login_window_detected",
            "window_probe": probe,
            "screenshot_path": str(latest.get("screenshot_path") or ""),
            "chat_info": {"chat_name": target, "source_adapter": "win32_ocr"},
            "ocr_items_count": len(ocr_items),
            "error": "WeChat quick-login view detected; enter WeChat before reading messages.",
        }
    blocking_reason = blocking_screen_reason(ocr_items)
    if blocking_reason:
        return {
            "ok": False,
            "online": False if blocking_reason == "login_or_qr" else True,
            "adapter": "win32_ocr",
            "state": "messages_blocked",
            "window_probe": probe,
            "screenshot_path": str(latest.get("screenshot_path") or ""),
            "chat_info": {"chat_name": target, "source_adapter": "win32_ocr"},
            "ocr_items_count": len(ocr_items),
            "reason": blocking_reason,
            "error": f"WeChat messages view is blocked by: {blocking_reason}",
        }
    messages = merge_message_history_snapshots(snapshots)
    return {
        "ok": True,
        "online": True,
        "adapter": "win32_ocr",
        "state": "messages_ocr",
        "window_probe": probe,
        "screenshot_path": str(latest.get("screenshot_path") or ""),
        "page_fingerprint": page_fingerprint,
        "passive_probe": bool(probe.get("passive_probe")),
        "chat_info": {"chat_name": target, "source_adapter": "win32_ocr"},
        "history_load": history_load,
        "messages": messages,
        "ocr_items_count": len(ocr_items),
    }


def voice_transcribe_payload(
    hwnd: int,
    probe: dict[str, Any],
    *,
    target: str,
    artifact_dir: str | None = None,
) -> dict[str, Any]:
    before_screenshot, before_path = capture_wechat(hwnd, artifact_dir=artifact_dir, label="voice_transcribe_before")
    before_items = run_ocr(before_screenshot)
    geometry = get_window_geometry(hwnd)
    image_size = getattr(before_screenshot, "size", (int(geometry.get("width") or 0), int(geometry.get("height") or 0)))
    before_messages = parse_messages_from_ocr(before_items, image_size, target=target)
    click_target = find_voice_transcribe_target(before_items, image_size)
    hover_attempt: dict[str, Any] | None = None
    context_menu_attempt: dict[str, Any] | None = None
    if not click_target:
        return {
            "ok": False,
            "online": True,
            "adapter": "win32_ocr",
            "state": "voice_transcribe_target_not_found",
            "window_probe": probe,
            "target": target,
            "screenshot_path": before_path,
            "ocr_items_count": len(before_items),
            "messages": before_messages,
            "error": "No visible WeChat voice-to-text affordance was found.",
        }

    if str(click_target.get("source") or "") == "inferred_from_voice_duration":
        hover_attempt = hover_voice_transcribe_button(
            hwnd,
            click_target,
            image_size=image_size,
            artifact_dir=artifact_dir,
        )
        hover_target = hover_attempt.get("click_target") if isinstance(hover_attempt, dict) else None
        if isinstance(hover_target, dict):
            click_target = hover_target

    click_x, click_y, jitter_meta = jitter_voice_transcribe_click_point(click_target, geometry)
    click_bounds = [int(value) for value in click_target.get("click_bounds") or []]
    click_result = human_window_image_click_in_bounds(
        hwnd,
        click_x,
        click_y,
        bounds=click_bounds,
        action_name="voice_transcribe_click",
    )
    wait_ms = bounded_int(
        os.getenv("WECHAT_WIN32_OCR_VOICE_TRANSCRIBE_WAIT_MS"),
        default=2600,
        minimum=500,
        maximum=15000,
    )
    humanized_action_sleep(max(200, wait_ms - 500), wait_ms + 900)

    after_screenshot, after_path = capture_wechat(hwnd, artifact_dir=artifact_dir, label="voice_transcribe_after")
    after_items = run_ocr(after_screenshot)
    after_size = getattr(after_screenshot, "size", image_size)
    after_messages = parse_messages_from_ocr(after_items, after_size, target=target)
    before_keys = {sidecar_message_content_key(message) for message in before_messages}
    new_messages = [
        message
        for message in after_messages
        if sidecar_message_content_key(message) not in before_keys
    ]
    transcribed_messages = [
        message
        for message in new_messages
        if not voice_duration_text_like(str(message.get("content_clean") or message.get("content") or ""))
    ]
    return {
        "ok": bool(click_result.get("ok")),
        "online": True,
        "adapter": "win32_ocr",
        "state": "voice_transcribe_clicked" if click_result.get("ok") else "voice_transcribe_click_failed",
        "window_probe": probe,
        "target": target,
        "before_screenshot_path": before_path,
        "after_screenshot_path": after_path,
        "click_target": click_target,
        "hover_attempt": hover_attempt or {},
        "context_menu_attempt": context_menu_attempt or {},
        "click": click_result,
        "planned_click_point": [click_x, click_y],
        "click_jitter": jitter_meta,
        "wait_ms": wait_ms,
        "before_messages": before_messages,
        "messages": after_messages,
        "new_messages": new_messages,
        "transcribed_messages": transcribed_messages,
        "ocr_items_count": len(after_items),
    }


def voice_transcribe_compact_text(text: str) -> str:
    return re.sub(r"\s+", "", normalize_ocr_text(text))


def voice_transcribe_button_text_like(text: str) -> bool:
    compact = voice_transcribe_compact_text(text)
    return bool(compact) and any(voice_transcribe_compact_text(token) in compact for token in VOICE_TRANSCRIBE_TEXT_TOKENS)


def voice_duration_text_like(text: str) -> bool:
    compact = voice_transcribe_compact_text(text).replace("“", '"').replace("”", '"').replace("″", '"')
    if not compact:
        return False
    if re.fullmatch(r"\d{1,3}\"", compact):
        return True
    if re.fullmatch(r"0\d{1,2}", compact):
        return True
    if re.fullmatch(r"[\)\]）>》!|lI]{1,2}\d{1,3}[\"']?", compact):
        return True
    return False


def voice_duration_item_like(item: dict[str, Any]) -> bool:
    text = str(item.get("text") or "")
    if voice_duration_text_like(text):
        return True
    compact = voice_transcribe_compact_text(text)
    if not re.fullmatch(r"\d{1,3}", compact):
        return False
    width = float(item.get("right") or 0) - float(item.get("left") or 0)
    height = float(item.get("bottom") or 0) - float(item.get("top") or 0)
    return 8.0 <= width <= 86.0 and 8.0 <= height <= 36.0


def voice_transcribe_item_is_in_chat_surface(item: dict[str, Any], image_size: tuple[int, int]) -> bool:
    width, height = image_size
    split_x = session_split_x(width)
    center_y = float(item.get("center_y") or 0)
    if float(item.get("left") or 0) < split_x + 20:
        return False
    if center_y < chat_header_cutoff_y(height):
        return False
    bottom_exclude_px = max(DEFAULT_MESSAGE_BOTTOM_EXCLUDE_PX, int(height * 0.10))
    if center_y > height - bottom_exclude_px:
        return False
    rect = {
        "left": int(float(item.get("left") or 0)),
        "top": int(float(item.get("top") or 0)),
        "right": int(float(item.get("right") or 0)),
        "bottom": int(float(item.get("bottom") or 0)),
    }
    return not rect_in_input_area(rect, {"width": width, "height": height})


def voice_duration_has_transcribed_text_below(
    duration_item: dict[str, Any],
    ocr_items: list[dict[str, Any]],
    image_size: tuple[int, int],
) -> bool:
    duration_bottom = float(duration_item.get("bottom") or 0)
    duration_left = float(duration_item.get("left") or 0)
    duration_right = float(duration_item.get("right") or 0)
    for item in ocr_items:
        if item is duration_item:
            continue
        text = str(item.get("text") or "").strip()
        if not text or voice_transcribe_button_text_like(text) or voice_duration_item_like(item):
            continue
        if is_message_noise(text):
            continue
        if not voice_transcribe_item_is_in_chat_surface(item, image_size):
            continue
        gap = float(item.get("top") or 0) - duration_bottom
        if gap < 8 or gap > 88:
            continue
        left = float(item.get("left") or 0)
        right = float(item.get("right") or 0)
        starts_near_voice = duration_left - 42 <= left <= duration_right + 42
        extends_like_transcript = right >= duration_right + 40 or len(voice_transcribe_compact_text(text)) >= 4
        if starts_near_voice and extends_like_transcript:
            return True
    return False


def voice_transcribe_click_target_from_bounds(
    *,
    source: str,
    label: str,
    bounds: list[int],
    item: dict[str, Any] | None = None,
    min_points: int = 10,
) -> dict[str, Any]:
    left, top, right, bottom = [int(value) for value in bounds[:4]]
    candidates = _spread_points_in_rect(left, top, right, bottom, min_points=min_points)
    return {
        "source": source,
        "label": label,
        "click_bounds": [left, top, right, bottom],
        "candidate_points": [list(point) for point in candidates],
        "candidate_count": len(candidates),
        "item": item or {},
    }


def voice_duration_context_click_bounds(item: dict[str, Any], image_size: tuple[int, int]) -> list[int]:
    width, height = image_size
    split_x = session_split_x(width)
    left = max(split_x + 16, int(float(item.get("left") or 0)) - 18)
    top = max(chat_header_cutoff_y(height), int(float(item.get("top") or 0)) - 16)
    right = min(width - 18, int(float(item.get("right") or 0)) + 78)
    bottom = min(height - DEFAULT_MESSAGE_BOTTOM_EXCLUDE_PX, int(float(item.get("bottom") or 0)) + 16)
    if right <= left:
        right = min(width - 18, left + 64)
    if bottom <= top:
        bottom = min(height - DEFAULT_MESSAGE_BOTTOM_EXCLUDE_PX, top + 28)
    return [left, top, right, bottom]


def voice_duration_context_click_target(duration_target: dict[str, Any], image_size: tuple[int, int]) -> dict[str, Any] | None:
    item = duration_target.get("item") if isinstance(duration_target, dict) else None
    if not isinstance(item, dict) or not item:
        return None
    bounds = voice_duration_context_click_bounds(item, image_size)
    return voice_transcribe_click_target_from_bounds(
        source="voice_duration_context_menu_anchor",
        label="Right-click anchor for WeChat voice bubble context menu",
        bounds=bounds,
        item=item,
    )


def hover_voice_transcribe_button(
    hwnd: int,
    duration_target: dict[str, Any],
    *,
    image_size: tuple[int, int],
    artifact_dir: str | None = None,
) -> dict[str, Any]:
    anchor = voice_duration_context_click_target(duration_target, image_size)
    if not anchor:
        return {"ok": False, "reason": "voice_duration_anchor_missing"}
    geometry = get_window_geometry(hwnd)
    anchor_x, anchor_y, anchor_jitter = jitter_voice_transcribe_click_point(anchor, geometry)
    hover = human_window_image_hover_in_bounds(
        hwnd,
        anchor_x,
        anchor_y,
        bounds=[int(value) for value in anchor.get("click_bounds") or []],
        action_name="voice_transcribe_duration_hover",
    )
    humanized_action_sleep(320, 620)
    hover_screenshot, hover_path = capture_wechat(hwnd, artifact_dir=artifact_dir, label="voice_transcribe_hover")
    hover_items = run_ocr(hover_screenshot)
    hover_size = getattr(hover_screenshot, "size", image_size)
    ocr_target = find_voice_transcribe_target(hover_items, hover_size, allow_inferred=False)
    visual_target = None if ocr_target else find_visual_voice_transcribe_hover_target(hover_screenshot, hover_items, hover_size)
    click_target = ocr_target or visual_target or {
        **duration_target,
        "source": "hover_inferred_from_voice_duration",
        "label": "Inferred WeChat voice-to-text hover button from voice bubble",
    }
    return {
        "ok": bool(hover.get("ok")),
        "hover": hover,
        "anchor": anchor,
        "anchor_point": [anchor_x, anchor_y],
        "anchor_jitter": anchor_jitter,
        "hover_screenshot_path": hover_path,
        "hover_ocr_items_count": len(hover_items),
        "click_target": click_target,
        "reason": "ocr_target_found" if ocr_target else ("visual_target_found" if visual_target else "using_hover_inferred_target"),
    }


def voice_transcribe_visual_button_score(image: Image.Image, bounds: list[int]) -> dict[str, Any]:
    if image is None or not bounds or len(bounds) < 4:
        return {"visible": False, "score": 0.0, "reason": "missing_image_or_bounds"}
    width, height = image.size
    left = max(0, min(width, int(bounds[0])))
    top = max(0, min(height, int(bounds[1])))
    right = max(left + 1, min(width, int(bounds[2])))
    bottom = max(top + 1, min(height, int(bounds[3])))
    crop = image.crop((left, top, right, bottom)).convert("RGB")
    pixels = list(crop.get_flattened_data() if hasattr(crop, "get_flattened_data") else crop.getdata())
    total = len(pixels)
    if not total:
        return {"visible": False, "score": 0.0, "reason": "empty_crop", "bounds": [left, top, right, bottom]}
    mid_gray = 0
    bright = 0
    dark = 0
    red = 0
    corner = pixels[0]
    different_from_corner = 0
    for r, g, b in pixels:
        avg = (r + g + b) / 3.0
        spread = max(r, g, b) - min(r, g, b)
        if 34.0 <= avg <= 132.0 and spread <= 34:
            mid_gray += 1
        if avg >= 135.0 and spread <= 86:
            bright += 1
        if avg <= 32.0:
            dark += 1
        if r >= 160 and g <= 96 and b <= 96:
            red += 1
        if abs(r - corner[0]) + abs(g - corner[1]) + abs(b - corner[2]) > 45:
            different_from_corner += 1
    mid_gray_ratio = mid_gray / total
    bright_ratio = bright / total
    dark_ratio = dark / total
    red_ratio = red / total
    diff_ratio = different_from_corner / total
    score = mid_gray_ratio + min(diff_ratio, 0.42) * 0.35 + bright_ratio * 0.15
    if red_ratio > 0.02 and mid_gray_ratio < 0.16:
        score *= 0.35
    visible = bool(mid_gray_ratio >= 0.18 and dark_ratio <= 0.88 and score >= 0.22)
    return {
        "visible": visible,
        "score": round(score, 6),
        "mid_gray_ratio": round(mid_gray_ratio, 6),
        "bright_ratio": round(bright_ratio, 6),
        "dark_ratio": round(dark_ratio, 6),
        "red_ratio": round(red_ratio, 6),
        "diff_ratio": round(diff_ratio, 6),
        "bounds": [left, top, right, bottom],
    }


def find_visual_voice_transcribe_hover_target(
    image: Image.Image,
    ocr_items: list[dict[str, Any]],
    image_size: tuple[int, int],
) -> dict[str, Any] | None:
    targets: list[dict[str, Any]] = []
    for item in ocr_items:
        if not voice_duration_item_like(item):
            continue
        if not voice_transcribe_item_is_in_chat_surface(item, image_size):
            continue
        if voice_duration_has_transcribed_text_below(item, ocr_items, image_size):
            continue
        center_y = int(float(item.get("center_y") or 0))
        voice_right = int(float(item.get("right") or 0))
        width, height = image_size
        left = max(session_split_x(width) + 86, voice_right + 70)
        right = min(width - 24, voice_right + 154)
        top = max(chat_header_cutoff_y(height), center_y - 18)
        bottom = min(height - DEFAULT_MESSAGE_BOTTOM_EXCLUDE_PX, center_y + 18)
        if right <= left or bottom <= top:
            continue
        visual = voice_transcribe_visual_button_score(image, [left, top, right, bottom])
        if not visual.get("visible"):
            continue
        target = voice_transcribe_click_target_from_bounds(
            source="visual_hover_button",
            label="Visually detected WeChat voice-to-text hover button",
            bounds=[left, top, right, bottom],
            item=item,
        )
        target["visual_score"] = visual
        targets.append(target)
    if not targets:
        return None
    return max(targets, key=lambda target: float((target.get("visual_score") or {}).get("score") or 0.0))


def open_voice_transcribe_context_menu(
    hwnd: int,
    duration_target: dict[str, Any],
    *,
    image_size: tuple[int, int],
    artifact_dir: str | None = None,
) -> dict[str, Any]:
    anchor = voice_duration_context_click_target(duration_target, image_size)
    if not anchor:
        return {"ok": False, "reason": "voice_duration_anchor_missing"}
    geometry = get_window_geometry(hwnd)
    anchor_x, anchor_y, anchor_jitter = jitter_voice_transcribe_click_point(anchor, geometry)
    right_click = human_window_image_right_click_in_bounds(
        hwnd,
        anchor_x,
        anchor_y,
        bounds=[int(value) for value in anchor.get("click_bounds") or []],
        action_name="voice_transcribe_context_right_click",
    )
    humanized_action_sleep(260, 520)
    menu_screenshot, menu_path = capture_wechat(hwnd, artifact_dir=artifact_dir, label="voice_transcribe_context_menu")
    menu_items = run_ocr(menu_screenshot)
    menu_size = getattr(menu_screenshot, "size", image_size)
    menu_target = find_voice_transcribe_target(menu_items, menu_size, allow_inferred=False)
    return {
        "ok": bool(right_click.get("ok") and menu_target),
        "right_click": right_click,
        "anchor": anchor,
        "anchor_point": [anchor_x, anchor_y],
        "anchor_jitter": anchor_jitter,
        "menu_screenshot_path": menu_path,
        "menu_ocr_items_count": len(menu_items),
        "click_target": menu_target,
        "reason": "menu_target_found" if menu_target else "menu_target_not_found",
    }


def dismiss_voice_transcribe_context_menu(hwnd: int) -> dict[str, Any]:
    try:
        activate_window(hwnd)
        key_press(win32con.VK_ESCAPE)
        humanized_action_sleep(120, 260)
        return {"ok": True, "method": "escape"}
    except Exception as exc:
        return {"ok": False, "method": "escape", "error": repr(exc)}


def find_voice_transcribe_target(
    ocr_items: list[dict[str, Any]],
    image_size: tuple[int, int],
    *,
    allow_inferred: bool = True,
) -> dict[str, Any] | None:
    width, height = image_size
    direct_targets: list[dict[str, Any]] = []
    for item in ocr_items:
        text = str(item.get("text") or "")
        if not voice_transcribe_button_text_like(text):
            continue
        if not voice_transcribe_item_is_in_chat_surface(item, image_size):
            continue
        left = max(session_split_x(width) + 16, int(float(item.get("left") or 0)) - 18)
        top = max(chat_header_cutoff_y(height), int(float(item.get("top") or 0)) - 12)
        right = min(width - 18, int(float(item.get("right") or 0)) + 18)
        bottom = min(height - DEFAULT_MESSAGE_BOTTOM_EXCLUDE_PX, int(float(item.get("bottom") or 0)) + 12)
        if right <= left or bottom <= top:
            continue
        direct_targets.append(
            voice_transcribe_click_target_from_bounds(
                source="ocr_transcribe_button",
                label="OCR matched WeChat voice-to-text button",
                bounds=[left, top, right, bottom],
                item=item,
            )
        )
    if direct_targets:
        return max(direct_targets, key=lambda target: float((target.get("item") or {}).get("center_y") or 0))

    if not allow_inferred:
        return None

    inferred_targets: list[dict[str, Any]] = []
    for item in ocr_items:
        text = str(item.get("text") or "")
        if not voice_duration_item_like(item):
            continue
        if not voice_transcribe_item_is_in_chat_surface(item, image_size):
            continue
        if voice_duration_has_transcribed_text_below(item, ocr_items, image_size):
            continue
        center_y = int(float(item.get("center_y") or 0))
        voice_right = int(float(item.get("right") or 0))
        left = max(session_split_x(width) + 86, voice_right + 70)
        right = min(width - 24, voice_right + 154)
        top = max(chat_header_cutoff_y(height), center_y - 18)
        bottom = min(height - DEFAULT_MESSAGE_BOTTOM_EXCLUDE_PX, center_y + 18)
        if right <= left or bottom <= top:
            continue
        inferred_targets.append(
            voice_transcribe_click_target_from_bounds(
                source="inferred_from_voice_duration",
                label="Inferred WeChat voice-to-text button from untranscribed voice bubble",
                bounds=[left, top, right, bottom],
                item=item,
            )
        )
    if inferred_targets:
        return max(inferred_targets, key=lambda target: float((target.get("item") or {}).get("center_y") or 0))
    return None


def voice_transcribe_click_candidate_points(target: dict[str, Any], *, min_points: int = 10) -> list[tuple[int, int]]:
    bounds = target.get("click_bounds") if isinstance(target, dict) else None
    if not isinstance(bounds, list) or len(bounds) < 4:
        return []
    return _spread_points_in_rect(
        int(bounds[0]),
        int(bounds[1]),
        int(bounds[2]),
        int(bounds[3]),
        min_points=min_points,
    )


def jitter_voice_transcribe_click_point(target: dict[str, Any], geometry: dict[str, Any]) -> tuple[int, int, dict[str, Any]]:
    candidates = [
        (int(point[0]), int(point[1]))
        for point in target.get("candidate_points", [])
        if isinstance(point, (list, tuple)) and len(point) >= 2
    ]
    if not candidates:
        candidates = voice_transcribe_click_candidate_points(target, min_points=10)
    bounds = [int(value) for value in target.get("click_bounds", [0, 0, 0, 0])[:4]]
    if len(bounds) < 4:
        bounds = [0, 0, int(geometry.get("width") or 0), int(geometry.get("height") or 0)]
    base_x, base_y = random.choice(candidates) if candidates else (
        int((bounds[0] + bounds[2]) / 2),
        int((bounds[1] + bounds[3]) / 2),
    )
    jitter_x = bounded_int(
        os.getenv("WECHAT_WIN32_OCR_VOICE_TRANSCRIBE_POINT_JITTER_X"),
        default=5,
        minimum=0,
        maximum=14,
    )
    jitter_y = bounded_int(
        os.getenv("WECHAT_WIN32_OCR_VOICE_TRANSCRIBE_POINT_JITTER_Y"),
        default=4,
        minimum=0,
        maximum=12,
    )
    final_x = bounded_int(
        base_x + random.randint(-jitter_x, jitter_x),
        default=base_x,
        minimum=min(bounds[0], bounds[2]),
        maximum=max(bounds[0], bounds[2]),
    )
    final_y = bounded_int(
        base_y + random.randint(-jitter_y, jitter_y),
        default=base_y,
        minimum=min(bounds[1], bounds[3]),
        maximum=max(bounds[1], bounds[3]),
    )
    return final_x, final_y, {
        "enabled": True,
        "role": "voice_transcribe_button",
        "source": str(target.get("source") or ""),
        "candidate_count": len(candidates),
        "base": [base_x, base_y],
        "final": [final_x, final_y],
        "bounds": bounds,
        "jitter": [jitter_x, jitter_y],
    }


def message_group_starts_with_voice_duration(group: list[dict[str, Any]]) -> bool:
    if len(group) < 2:
        return False
    first = group[0]
    second = group[1]
    if not voice_duration_item_like(first):
        return False
    first_bottom = float(first.get("bottom") or 0)
    second_top = float(second.get("top") or 0)
    gap = second_top - first_bottom
    if gap < 4 or gap > 92:
        return False
    first_left = float(first.get("left") or 0)
    second_left = float(second.get("left") or 0)
    return abs(first_left - second_left) <= 48.0


def strip_voice_duration_prefix_from_message_content(content: str, group: list[dict[str, Any]]) -> tuple[str, bool]:
    if not message_group_starts_with_voice_duration(group):
        return content, False
    lines = [line.strip() for line in str(content or "").splitlines() if line.strip()]
    if len(lines) < 2:
        return content, False
    return "\n".join(lines[1:]).strip(), True


FILE_CARD_FOOTER_TEXTS = {
    "微信电脑版",
    "微信Windows版",
    "微信Mac版",
    "WeChat for Windows",
    "WeChat for Mac",
}


def message_group_is_file_card_noise(group: list[dict[str, Any]], content: str) -> bool:
    lines = [str(line or "").strip() for line in str(content or "").splitlines() if str(line or "").strip()]
    if not lines:
        return False
    if len(lines) == 1 and lines[0] in FILE_CARD_FOOTER_TEXTS:
        return True
    has_footer = any(line in FILE_CARD_FOOTER_TEXTS for line in lines)
    if not has_footer:
        return False
    has_file_name = any(re.search(r"\.[A-Za-z0-9]{1,8}$", line) for line in lines)
    has_file_size = any(re.fullmatch(r"\d+(?:\.\d+)?\s*[KMGT]?B?", line, re.IGNORECASE) for line in lines)
    return bool(has_file_name or has_file_size)


def message_group_is_voice_duration_only(group: list[dict[str, Any]]) -> bool:
    if not group:
        return False
    return all(voice_duration_item_like(item) for item in group)


def sender_fields_for_message_side(side: str, *, target: str) -> tuple[str, str]:
    if side == "self":
        return "self", "self"
    conversation_type = infer_conversation_type(target)
    if conversation_type == "private":
        return "customer", "customer"
    return "unknown", "unknown"


def ocr_page_fingerprint(ocr_items: list[dict[str, Any]], *, geometry: dict[str, Any]) -> dict[str, Any]:
    normalized: list[str] = []
    for item in ocr_items or []:
        text = normalize_ocr_text(item.get("text"))
        if not text:
            continue
        normalized.append(
            "|".join(
                [
                    str(round(float(item.get("center_x") or 0) / 8.0)),
                    str(round(float(item.get("center_y") or 0) / 8.0)),
                    text,
                ]
            )
        )
    seed = "\n".join(normalized)
    digest = hashlib.sha1(seed.encode("utf-8")).hexdigest()[:16] if seed else ""
    return {
        "hash": digest,
        "ocr_count": len(normalized),
        "width": int(geometry.get("width") or 0),
        "height": int(geometry.get("height") or 0),
    }


def capture_message_history_snapshots(
    hwnd: int,
    *,
    target: str,
    history_load_times: int,
    artifact_dir: str | None = None,
) -> list[dict[str, Any]]:
    snapshots: list[dict[str, Any]] = []

    def capture(label: str) -> None:
        screenshot, path = capture_wechat(hwnd, artifact_dir=artifact_dir, label=label)
        ocr_items = run_ocr(screenshot)
        snapshots.append(
            {
                "label": label,
                "screenshot_path": path,
                "ocr_items": ocr_items,
                "messages": parse_messages_from_ocr(ocr_items, screenshot.size, target=target),
            }
        )

    capture("messages")
    for index in range(max(0, int(history_load_times or 0))):
        scroll_chat_history(hwnd, 1)
        humanized_action_sleep(70, 140)
        capture(f"messages_h{index + 1}")
    if history_load_times:
        scroll_chat_to_latest(hwnd, attempts=max(16, int(history_load_times or 0) * 6 + 6))
    return snapshots


def capture_message_history_snapshots_until_anchor(
    hwnd: int,
    *,
    target: str,
    anchor_ids: list[str],
    anchor_content_keys: list[str],
    reply_content_keys: list[str],
    max_scroll_steps: int,
    max_duration_seconds: int,
    max_snapshots: int,
    min_delay_ms: int,
    max_delay_ms: int,
    restore_to_latest: bool,
    artifact_dir: str | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    snapshots: list[dict[str, Any]] = []
    anchor_id_set = {str(item).strip() for item in anchor_ids or [] if str(item).strip()}
    anchor_content_set = {str(item).strip() for item in anchor_content_keys or [] if str(item).strip()}
    reply_content_set = {normalize_anchor_reply_key(item) for item in reply_content_keys or [] if normalize_anchor_reply_key(item)}
    start = time.monotonic()

    history_load: dict[str, Any] = {
        "ok": True,
        "mode": "anchor_until_found",
        "mechanism": "win32_ocr.AnchorSearch+WheelUp+ScreenshotOCR",
        "anchor_found": False,
        "anchor_index": -1,
        "anchor_type": "",
        "scroll_steps": 0,
        "snapshot_count": 0,
        "stopped_reason": "",
        "restored_to_latest": False,
        "max_scroll_steps": max_scroll_steps,
        "max_duration_seconds": max_duration_seconds,
        "max_snapshots": max_snapshots,
    }

    def capture(label: str) -> None:
        screenshot, path = capture_wechat(hwnd, artifact_dir=artifact_dir, label=label)
        ocr_items = run_ocr(screenshot)
        snapshots.append(
            {
                "label": label,
                "screenshot_path": path,
                "ocr_items": ocr_items,
                "messages": parse_messages_from_ocr(ocr_items, screenshot.size, target=target),
            }
        )
        history_load["snapshot_count"] = len(snapshots)

    def find_anchor() -> tuple[int, str]:
        merged = merge_message_history_snapshots(snapshots)
        latest_index = -1
        latest_type = ""
        for index, message in enumerate(merged):
            anchor_type = message_anchor_match_type(
                message,
                anchor_ids=anchor_id_set,
                anchor_content_keys=anchor_content_set,
                reply_content_keys=reply_content_set,
            )
            if anchor_type:
                latest_index = index
                latest_type = anchor_type
        return latest_index, latest_type

    try:
        capture("messages")
        anchor_index, anchor_type = find_anchor()
        if anchor_index >= 0:
            history_load.update(
                {
                    "anchor_found": True,
                    "anchor_index": anchor_index,
                    "anchor_type": anchor_type,
                    "stopped_reason": "visible_anchor_found_no_scroll",
                }
            )
            return snapshots, history_load

        if not (anchor_id_set or anchor_content_set or reply_content_set):
            history_load["stopped_reason"] = "no_anchor_candidates"
            return snapshots, history_load

        for step in range(max(0, int(max_scroll_steps or 0))):
            if len(snapshots) >= max(1, int(max_snapshots or 1)):
                history_load["stopped_reason"] = "max_snapshots_reached"
                break
            if time.monotonic() - start >= max(1, int(max_duration_seconds or 1)):
                history_load["stopped_reason"] = "max_duration_reached"
                break
            scroll_chat_history(
                hwnd,
                1,
                wheel_units=random.randint(3, 6),
                delay_seconds=random.uniform(0.12, 0.28),
            )
            history_load["scroll_steps"] = step + 1
            pause_min = max(0, int(min_delay_ms or 0)) / 1000.0
            pause_max = max(pause_min, int(max_delay_ms or 0) / 1000.0)
            time.sleep(random.uniform(pause_min, pause_max))
            capture(f"messages_anchor_h{step + 1}")
            anchor_index, anchor_type = find_anchor()
            if anchor_index >= 0:
                history_load.update(
                    {
                        "anchor_found": True,
                        "anchor_index": anchor_index,
                        "anchor_type": anchor_type,
                        "stopped_reason": "anchor_found",
                    }
                )
                break
        if not history_load.get("stopped_reason"):
            history_load["stopped_reason"] = "max_scroll_steps_reached"
    except Exception as exc:
        history_load.update({"ok": False, "stopped_reason": "exception", "error": repr(exc)})
    finally:
        if restore_to_latest and int(history_load.get("scroll_steps") or 0) > 0:
            scroll_chat_to_latest(hwnd, attempts=max(10, int(history_load.get("scroll_steps") or 0) * 5 + 5))
            history_load["restored_to_latest"] = True
    return snapshots, history_load


def merge_message_history_snapshots(snapshots: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for snapshot in reversed(snapshots):
        occurrence_counts: dict[str, int] = {}
        for message in snapshot.get("messages", []) or []:
            if not isinstance(message, dict):
                continue
            occurrence_hint = None
            base_key = message_history_dedupe_base_key(message)
            if base_key and message_history_requires_occurrence_hint(message, base_key=base_key):
                occurrence_counts[base_key] = occurrence_counts.get(base_key, 0) + 1
                occurrence_hint = occurrence_counts[base_key]
            key = message_history_dedupe_key(message, occurrence_hint=occurrence_hint, base_key=base_key)
            if key and key in seen:
                continue
            if key:
                seen.add(key)
            merged.append(message)
    return merged


def message_history_dedupe_base_key(message: dict[str, Any]) -> str:
    content = str(message.get("content") or "")
    compact = re.sub(r"[\s_\-:：，。,.；;\[\]（）()]+", "", content).lower()
    sender = str(message.get("sender") or "")
    if not compact:
        return ""
    return f"{sender}:{compact}"


def message_history_requires_occurrence_hint(
    message: dict[str, Any],
    *,
    base_key: str | None = None,
    short_len_threshold: int = 7,
) -> bool:
    key = str(base_key or message_history_dedupe_base_key(message))
    if not key:
        return False
    compact = key.split(":", 1)[1] if ":" in key else key
    return len(compact) <= max(1, int(short_len_threshold or 1))


def message_history_dedupe_key(
    message: dict[str, Any],
    *,
    occurrence_hint: int | None = None,
    base_key: str | None = None,
) -> str:
    key = str(base_key or message_history_dedupe_base_key(message))
    if not key:
        return ""
    if occurrence_hint and message_history_requires_occurrence_hint(message, base_key=key):
        return f"{key}#occ{int(occurrence_hint)}"
    return key


def normalize_anchor_message_content(text: Any) -> str:
    return re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "", str(text or "")).lower()


def normalize_anchor_reply_key(text: Any) -> str:
    return normalize_anchor_message_content(text)


def sidecar_message_content_key(message: dict[str, Any]) -> str:
    content = normalize_anchor_message_content(message.get("content"))
    if not content:
        return ""
    return "\x1f".join(
        [
            str(message.get("sender") or "").strip(),
            str(message.get("type") or "").strip(),
            content,
        ]
    )


ADD_FRIEND_STEP_SEQUENCE = [
    "checking_rpa",
    "wechat_window_found",
    "phone_search_started",
    "phone_search_finished",
    "add_friend_button_clicked",
    "invite_text_filled",
    "remark_written",
    "invite_sent",
]


def add_friend_ocr_compact(text: Any) -> str:
    return win32_ocr_add_friend_windows.add_friend_ocr_compact(text)


def add_friend_item_text(item: dict[str, Any]) -> str:
    return win32_ocr_add_friend_windows.add_friend_item_text(item)


def add_friend_surface_text(ocr_items: list[dict[str, Any]]) -> str:
    return win32_ocr_add_friend_windows.add_friend_surface_text(ocr_items)


def add_friend_blocking_prompt_region(item: dict[str, Any], *, geometry: dict[str, Any] | None = None, image_size: tuple[int, int] | None = None) -> dict[str, Any]:
    return win32_ocr_add_friend_windows.add_friend_blocking_prompt_region(item, geometry=geometry, image_size=image_size)


def add_friend_login_or_security_block(ocr_items: list[dict[str, Any]], *, geometry: dict[str, Any] | None = None, image_size: tuple[int, int] | None = None) -> dict[str, Any]:
    return win32_ocr_add_friend_windows.add_friend_login_or_security_block(ocr_items, geometry=geometry, image_size=image_size)


def add_friend_item_center(item: dict[str, Any]) -> tuple[int, int]:
    return win32_ocr_add_friend_windows.add_friend_item_center(item)


def center_of_bounds(bounds: list[int]) -> tuple[int, int]:
    return win32_ocr_geometry.center_of_bounds(bounds)


def add_friend_zone_bounds(image_size: tuple[int, int]) -> list[dict[str, Any]]:
    return win32_ocr_add_friend_windows.add_friend_zone_bounds(image_size)


def point_in_bounds(x: int, y: int, bounds: list[int]) -> bool:
    return win32_ocr_geometry.point_in_bounds(x, y, bounds)


def clamp_point_to_bounds(x: int, y: int, bounds: list[int]) -> tuple[int, int]:
    return win32_ocr_geometry.clamp_point_to_bounds(x, y, bounds)


def add_friend_region_for_point(x: int, y: int, image_size: tuple[int, int]) -> str:
    return win32_ocr_add_friend_windows.add_friend_region_for_point(x, y, image_size)


def add_friend_region_for_item(item: dict[str, Any], image_size: tuple[int, int]) -> str:
    return win32_ocr_add_friend_windows.add_friend_region_for_item(item, image_size)


def add_friend_windows_1080p_reference_plus_button_point_for_geometry(geometry: dict[str, Any]) -> tuple[int, int]:
    return win32_ocr_add_friend_windows.add_friend_windows_1080p_reference_plus_button_point_for_geometry(geometry)


def add_friend_windows_plus_button_point_for_geometry(geometry: dict[str, Any]) -> tuple[int, int]:
    return win32_ocr_add_friend_windows.add_friend_windows_plus_button_point_for_geometry(geometry)


def add_friend_plus_button_point_for_geometry(geometry: dict[str, Any]) -> tuple[int, int]:
    return win32_ocr_add_friend_windows.add_friend_plus_button_point_for_geometry(geometry)


def add_friend_plus_entry_safe_bounds(image_size: tuple[int, int]) -> list[int]:
    return win32_ocr_add_friend_windows.add_friend_plus_entry_safe_bounds(image_size)


def find_sidebar_search_anchor_item(ocr_items: list[dict[str, Any]], image_size: tuple[int, int]) -> dict[str, Any] | None:
    return win32_ocr_add_friend_windows.find_sidebar_search_anchor_item(ocr_items, image_size)


def add_friend_plus_entry_target(geometry: dict[str, Any], image_size: tuple[int, int], ocr_items: list[dict[str, Any]] | None = None, *, screenshot: Any | None = None, route_kind: str = 'windows') -> dict[str, Any]:
    return win32_ocr_add_friend_windows.add_friend_plus_entry_target(geometry, image_size, ocr_items, screenshot=screenshot, route_kind=route_kind)


def normalize_point_for_add_friend_target(point: Any) -> list[int]:
    return win32_ocr_add_friend_windows.normalize_point_for_add_friend_target(point)


def add_friend_text_has_any(text: str, tokens: tuple[str, ...]) -> bool:
    return win32_ocr_add_friend_windows.add_friend_text_has_any(text, tokens)


def add_friend_server_report_payload(*, task_status: str | None = None, result_code: str | None = None, error_code: str | None = None, current_step: str | None = None) -> dict[str, str]:
    return win32_ocr_add_friend_windows.add_friend_server_report_payload(task_status=task_status, result_code=result_code, error_code=error_code, current_step=current_step)


def add_friend_completed_result(*, state: str, result_code: str, current_step: str = 'task_completed', **extra: Any) -> dict[str, Any]:
    return win32_ocr_add_friend_windows.add_friend_completed_result(state=state, result_code=result_code, current_step=current_step, **extra)


def add_friend_failed_result(*, state: str, error_code: str, current_step: str, **extra: Any) -> dict[str, Any]:
    return win32_ocr_add_friend_windows.add_friend_failed_result(state=state, error_code=error_code, current_step=current_step, **extra)


def add_friend_entry_click_validation_failure_payload(
    *,
    phone: str,
    wechat: str,
    verify_message: str,
    remark_name: str,
    remark_code: str,
    artifact_dir: str | None = None,
    probe: dict[str, Any] | None = None,
) -> dict[str, Any]:
    validation = validate_add_friend_entry_click_contract(
        phone=phone,
        wechat=wechat,
        verify_message=verify_message,
        remark_name=remark_name,
        remark_code=remark_code,
    )
    flow = AddFriendFlowContext(
        project_root=PROJECT_ROOT,
        route=ADD_FRIEND_MAIN_ROUTE,
        artifact_dir=artifact_dir,
    )
    flow.add_event(
        step_id="payload_validation",
        title="字段契约校验",
        status="failed",
        state_before="task_received",
        state_after="task_payload_invalid",
        result={
            "ok": False,
            "task_status": "failed",
            "error_code": ERROR_TASK_PAYLOAD_INVALID,
            "verify_message": validation.get("verify_message"),
            "remark_name": validation.get("remark_name"),
            "remark_code": validation.get("remark_code"),
            "remark_code_valid": validation.get("remark_code_valid"),
            "validation_errors": validation.get("validation_errors") or [],
            "legacy_remark_fallback": False,
            "wechat_ui_action_attempted": False,
        },
    )
    payload = add_friend_task_payload_invalid(
        phone=phone,
        wechat=wechat,
        validation=validation,
        plan_path=str(flow.plan_path),
        probe=probe,
    )
    return flow.finalize_payload(payload, report_writer=write_add_friend_entry_click_review)


def find_add_friend_action_item(ocr_items: list[dict[str, Any]], tokens: tuple[str, ...], image_size: tuple[int, int], *, min_y_ratio: float = 0.0, max_y_ratio: float = 1.0) -> dict[str, Any] | None:
    return win32_ocr_add_friend_windows.find_add_friend_action_item(ocr_items, tokens, image_size, min_y_ratio=min_y_ratio, max_y_ratio=max_y_ratio)


def find_add_friend_search_result_item(ocr_items: list[dict[str, Any]], query: str, image_size: tuple[int, int]) -> dict[str, Any] | None:
    return win32_ocr_add_friend_windows.find_add_friend_search_result_item(ocr_items, query, image_size)


def classify_add_friend_ocr_surface(ocr_items: list[dict[str, Any]], image_size: tuple[int, int]) -> dict[str, Any]:
    return win32_ocr_add_friend_windows.classify_add_friend_ocr_surface(ocr_items, image_size)


def classify_add_friend_after_confirm_surface(ocr_items: list[dict[str, Any]], image_size: tuple[int, int], *, confirm_ok: bool) -> dict[str, Any]:
    return win32_ocr_add_friend_windows.classify_add_friend_after_confirm_surface(ocr_items, image_size, confirm_ok=confirm_ok)


def add_friend_item_snapshot(item: dict[str, Any] | None, image_size: tuple[int, int]) -> dict[str, Any] | None:
    return win32_ocr_add_friend_windows.add_friend_item_snapshot(item, image_size)


def add_friend_ocr_snapshots(ocr_items: list[dict[str, Any]], image_size: tuple[int, int]) -> list[dict[str, Any]]:
    return win32_ocr_add_friend_windows.add_friend_ocr_snapshots(ocr_items, image_size)


def draw_add_friend_screen_annotation(screenshot: Image.Image, *, ocr_items: list[dict[str, Any]], targets: list[dict[str, Any]], output_path: Path, window_rect: list[int] | None = None) -> str:
    return win32_ocr_add_friend_windows.draw_add_friend_screen_annotation(screenshot, ocr_items=ocr_items, targets=targets, output_path=output_path, window_rect=window_rect)


def draw_add_friend_layout_calibration_annotation(screenshot: Image.Image, *, layout_calibration: dict[str, Any] | None, output_path: Path) -> str:
    return win32_ocr_add_friend_windows.draw_add_friend_layout_calibration_annotation(screenshot, layout_calibration=layout_calibration, output_path=output_path)


def add_friend_popup_menu_bounds(image_size: tuple[int, int], *, plus_screen_x: int, plus_screen_y: int) -> list[int]:
    return win32_ocr_add_friend_windows.add_friend_popup_menu_bounds(image_size, plus_screen_x=plus_screen_x, plus_screen_y=plus_screen_y)


def run_ocr_on_screen_region(
    image: Image.Image,
    bounds: list[int],
    *,
    purpose: str = "screen_region",
    source: str = "run_ocr_on_screen_region",
) -> list[dict[str, Any]]:
    left, top, right, bottom = [int(value) for value in bounds[:4]]
    width, height = image.size
    left = max(0, min(width - 1, left))
    top = max(0, min(height - 1, top))
    right = max(left + 1, min(width, right))
    bottom = max(top + 1, min(height, bottom))
    cropped = image.crop((left, top, right, bottom))
    items = run_ocr_traced(cropped, purpose, region="roi", source=source)
    for item in items:
        for key in ("left", "right", "center_x"):
            item[key] = float(item.get(key) or 0.0) + left
        for key in ("top", "bottom", "center_y"):
            item[key] = float(item.get(key) or 0.0) + top
        box = item.get("box")
        if isinstance(box, list):
            item["box"] = [[float(point[0]) + left, float(point[1]) + top] for point in box if isinstance(point, (list, tuple)) and len(point) >= 2]
    return items


def active_send_target_roi_ocr_enabled() -> bool:
    return env_flag("WECHAT_WIN32_OCR_ACTIVE_SEND_TARGET_ROI_OCR", default=DEFAULT_ACTIVE_SEND_TARGET_ROI_OCR)


def input_confirm_roi_ocr_enabled() -> bool:
    return env_flag("WECHAT_WIN32_OCR_INPUT_CONFIRM_ROI_OCR", default=DEFAULT_INPUT_CONFIRM_ROI_OCR)


def run_ocr_for_input_region_probe(
    screenshot: Any,
    *,
    geometry: dict[str, Any],
    timing: dict[str, Any],
    prefix: str,
    purpose: str,
    roi_purpose: str,
) -> tuple[list[dict[str, Any]], str]:
    if not input_confirm_roi_ocr_enabled():
        timing[f"{prefix}_roi_enabled"] = False
        full_started = _sidecar_timing_start(timing, f"{prefix}_full_ocr")
        items = run_ocr_traced(
            screenshot,
            purpose,
            source="paste_text_with_confirmation",
        )
        _sidecar_timing_finish(timing, f"{prefix}_full_ocr", full_started)
        timing[f"{prefix}_source"] = "full"
        return items, "full"

    bounds = list(input_text_region_bounds(geometry))
    timing[f"{prefix}_roi_enabled"] = True
    timing[f"{prefix}_roi_bounds"] = list(bounds)
    roi_started = _sidecar_timing_start(timing, f"{prefix}_roi_ocr")
    items = run_ocr_on_screen_region(
        screenshot,
        bounds,
        purpose=roi_purpose,
        source="paste_text_with_confirmation",
    )
    _sidecar_timing_finish(timing, f"{prefix}_roi_ocr", roi_started)
    timing[f"{prefix}_roi_ocr_count"] = len(items)
    timing[f"{prefix}_source"] = "roi"
    return items, "roi"


def run_ocr_for_input_confirmation(
    screenshot: Any,
    *,
    geometry: dict[str, Any],
    timing: dict[str, Any],
    prefix: str,
) -> tuple[list[dict[str, Any]], str]:
    return run_ocr_for_input_region_probe(
        screenshot,
        geometry=geometry,
        timing=timing,
        prefix=prefix,
        purpose="input_after_token_confirm",
        roi_purpose="input_after_token_confirm_roi",
    )


def remember_input_region_precheck_ocr_seed(
    *,
    hwnd: int,
    target: str,
    exact: bool,
    screenshot: Any,
    ocr_items: list[dict[str, Any]],
    geometry: dict[str, Any],
    screenshot_path: str | None = None,
) -> None:
    global _INPUT_REGION_PRECHECK_OCR_SEED
    try:
        input_region = input_text_region_state(screenshot, ocr_items, geometry=geometry)
    except Exception:
        _INPUT_REGION_PRECHECK_OCR_SEED = {}
        return
    _INPUT_REGION_PRECHECK_OCR_SEED = {
        "hwnd": int(hwnd or 0),
        "target": str(target or ""),
        "exact": bool(exact),
        "geometry": dict(geometry or {}),
        "screenshot_size": list(getattr(screenshot, "size", (0, 0))),
        "input_region": dict(input_region or {}),
        "screenshot_path": str(screenshot_path or ""),
        "created_monotonic": time.monotonic(),
    }


def consume_input_region_precheck_ocr_seed(
    *,
    hwnd: int,
    target: str,
    exact: bool,
    geometry: dict[str, Any],
) -> dict[str, Any] | None:
    global _INPUT_REGION_PRECHECK_OCR_SEED
    seed = dict(_INPUT_REGION_PRECHECK_OCR_SEED or {})
    if not seed:
        return None
    _INPUT_REGION_PRECHECK_OCR_SEED = {}
    try:
        age = time.monotonic() - float(seed.get("created_monotonic") or 0.0)
    except Exception:
        return None
    if age < 0 or age > DEFAULT_INPUT_REGION_PRECHECK_OCR_SEED_SECONDS:
        return None
    if int(seed.get("hwnd") or 0) != int(hwnd or 0):
        return None
    if str(seed.get("target") or "") != str(target or ""):
        return None
    if bool(seed.get("exact")) != bool(exact):
        return None
    seed_geometry = seed.get("geometry") if isinstance(seed.get("geometry"), dict) else {}
    if int(seed_geometry.get("width") or 0) != int(geometry.get("width") or 0):
        return None
    if int(seed_geometry.get("height") or 0) != int(geometry.get("height") or 0):
        return None
    input_region = seed.get("input_region") if isinstance(seed.get("input_region"), dict) else {}
    if not input_region:
        return None
    seed["age_seconds"] = round(max(0.0, age), 4)
    return seed


def active_send_target_roi_bounds(image_size: tuple[int, int]) -> list[int]:
    width, height = [int(value or 0) for value in image_size[:2]]
    if width <= 0 or height <= 0:
        return [0, 0, 1, 1]
    left = max(0, min(width - 1, active_chat_title_left_x(width) - 32))
    top = 0
    right = width
    bottom = height
    return [left, top, right, bottom]


def active_send_target_roi_chat_surface_visible(ocr_items: list[dict[str, Any]]) -> bool:
    chat_surface_tokens = (
        "发送",
        "聊天",
        "按下enter",
        "文件传输助手",
    )
    texts = [normalize_ocr_text(item.get("text")) for item in ocr_items if normalize_ocr_text(item.get("text"))]
    return any(token in text.lower() for text in texts for token in chat_surface_tokens)


def active_send_target_roi_has_soft_blocking_text(ocr_items: list[dict[str, Any]]) -> bool:
    texts = [normalize_ocr_text(item.get("text")) for item in ocr_items if normalize_ocr_text(item.get("text"))]
    return any(token in text for text in texts for token in SOFT_BLOCKING_SCREEN_TOKENS)


def run_ocr_for_active_send_target(
    screenshot: Any,
    *,
    target: str,
    exact: bool,
    geometry: dict[str, Any],
    timing: dict[str, Any],
) -> tuple[list[dict[str, Any]], str, dict[str, Any] | None]:
    if not active_send_target_roi_ocr_enabled():
        timing["validate_active_send_target_roi_enabled"] = False
        full_started = _sidecar_timing_start(timing, "validate_active_send_target_full_ocr")
        items = run_ocr_traced(screenshot, "active_send_target_validation", source="validate_active_send_target")
        _sidecar_timing_finish(timing, "validate_active_send_target_full_ocr", full_started)
        return items, "full", None

    timing["validate_active_send_target_roi_enabled"] = True
    roi_bounds = active_send_target_roi_bounds(getattr(screenshot, "size", (0, 0)))
    timing["validate_active_send_target_roi_bounds"] = list(roi_bounds)
    roi_started = _sidecar_timing_start(timing, "validate_active_send_target_roi_ocr")
    roi_items = run_ocr_on_screen_region(
        screenshot,
        roi_bounds,
        purpose="active_send_target_validation_roi",
        source="validate_active_send_target",
    )
    _sidecar_timing_finish(timing, "validate_active_send_target_roi_ocr", roi_started)
    timing["validate_active_send_target_roi_ocr_count"] = len(roi_items)
    if not roi_items:
        blank_render = detect_blank_render(screenshot, roi_items, geometry=geometry)
        if blank_render.get("detected"):
            timing["validate_active_send_target_roi_decision"] = "blank_render_no_full_ocr"
            return roi_items, "roi", blank_render
        timing["validate_active_send_target_roi_decision"] = "fallback_empty_roi"
        full_started = _sidecar_timing_start(timing, "validate_active_send_target_full_ocr")
        items = run_ocr_traced(screenshot, "active_send_target_validation_fallback_full", source="validate_active_send_target")
        _sidecar_timing_finish(timing, "validate_active_send_target_full_ocr", full_started)
        return items, "full_fallback", None

    quick_login_detected = quick_login_like(roi_items, geometry=geometry)
    auxiliary_shell = auxiliary_wechat_shell_like(roi_items, geometry=geometry)
    blocking_reason = blocking_screen_reason(roi_items)
    active_match = active_chat_matches(roi_items, getattr(screenshot, "size", (0, 0)), target=target, exact=exact)
    chat_surface_visible = active_send_target_roi_chat_surface_visible(roi_items)
    soft_blocking_text = active_send_target_roi_has_soft_blocking_text(roi_items)
    timing["validate_active_send_target_roi_quick_login_detected"] = bool(quick_login_detected)
    timing["validate_active_send_target_roi_auxiliary_shell_detected"] = bool(auxiliary_shell.get("detected"))
    timing["validate_active_send_target_roi_blocking_detected"] = bool(blocking_reason)
    timing["validate_active_send_target_roi_active_match"] = bool(active_match)
    timing["validate_active_send_target_roi_chat_surface_visible"] = bool(chat_surface_visible)
    timing["validate_active_send_target_roi_soft_blocking_text"] = bool(soft_blocking_text)
    if active_match and chat_surface_visible and not soft_blocking_text and not quick_login_detected and not auxiliary_shell.get("detected") and not blocking_reason:
        timing["validate_active_send_target_roi_decision"] = "accepted"
        return roi_items, "roi", None
    if chat_surface_visible and not soft_blocking_text and not quick_login_detected and not auxiliary_shell.get("detected") and not blocking_reason:
        timing["validate_active_send_target_roi_decision"] = "rejected_without_full_fallback"
        return roi_items, "roi_rejected", None
    timing["validate_active_send_target_roi_decision"] = "fallback_uncertain"
    full_started = _sidecar_timing_start(timing, "validate_active_send_target_full_ocr")
    items = run_ocr_traced(screenshot, "active_send_target_validation_fallback_full", source="validate_active_send_target")
    _sidecar_timing_finish(timing, "validate_active_send_target_full_ocr", full_started)
    return items, "full_fallback", None


def add_friend_menu_text_matches(text: str, tokens: tuple[str, ...]) -> bool:
    return win32_ocr_add_friend_windows.add_friend_menu_text_matches(text, tokens)


def find_add_friend_menu_item(ocr_items: list[dict[str, Any]], tokens: tuple[str, ...], image_size: tuple[int, int], *, popup_bounds: list[int]) -> dict[str, Any] | None:
    return win32_ocr_add_friend_windows.find_add_friend_menu_item(ocr_items, tokens, image_size, popup_bounds=popup_bounds)


def add_friend_expected_menu_target(*, name: str, label: str, plus_screen_x: int, plus_screen_y: int, y_offset: int, image_size: tuple[int, int]) -> dict[str, Any]:
    return win32_ocr_add_friend_windows.add_friend_expected_menu_target(name=name, label=label, plus_screen_x=plus_screen_x, plus_screen_y=plus_screen_y, y_offset=y_offset, image_size=image_size)


def add_friend_popup_menu_item_click_bounds(item: dict[str, Any], popup_bounds: list[int]) -> list[int]:
    return win32_ocr_add_friend_windows.add_friend_popup_menu_item_click_bounds(item, popup_bounds)


def add_friend_expected_menu_click_bounds(*, image_size: tuple[int, int], plus_screen_x: int, plus_screen_y: int, y_offset: int) -> list[int]:
    return win32_ocr_add_friend_windows.add_friend_expected_menu_click_bounds(image_size=image_size, plus_screen_x=plus_screen_x, plus_screen_y=plus_screen_y, y_offset=y_offset)


def add_friend_menu_candidate_targets(ocr_items: list[dict[str, Any]], image_size: tuple[int, int], *, plus_screen_x: int | None = None, plus_screen_y: int | None = None, include_expected: bool = True) -> list[dict[str, Any]]:
    return win32_ocr_add_friend_windows.add_friend_menu_candidate_targets(ocr_items, image_size, plus_screen_x=plus_screen_x, plus_screen_y=plus_screen_y, include_expected=include_expected)


def plus_entry_popup_menu_detected(ocr_items: list[dict[str, Any]], targets: list[dict[str, Any]]) -> dict[str, Any]:
    return win32_ocr_add_friend_windows.plus_entry_popup_menu_detected(ocr_items, targets)


def add_friend_target_review_text(targets: list[dict[str, Any]]) -> str:
    return win32_ocr_add_friend_windows.add_friend_target_review_text(targets)


def add_friend_target_by_name(targets: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    return win32_ocr_add_friend_windows.add_friend_target_by_name(targets, name)


def add_friend_target_screen_point(target: dict[str, Any]) -> tuple[int, int]:
    return win32_ocr_add_friend_windows.add_friend_target_screen_point(target)


def add_click_screen_origin_to_targets(targets: list[dict[str, Any]], *, origin_x: int, origin_y: int) -> list[dict[str, Any]]:
    return win32_ocr_add_friend_windows.add_click_screen_origin_to_targets(targets, origin_x=origin_x, origin_y=origin_y)


def add_friend_page_search_region(image_size: tuple[int, int]) -> list[int]:
    return win32_ocr_add_friend_windows.add_friend_page_search_region(image_size)


def add_friend_search_result_region(image_size: tuple[int, int]) -> list[int]:
    return win32_ocr_add_friend_windows.add_friend_search_result_region(image_size)


def add_friend_phone_not_found_detected(ocr_items: list[dict[str, Any]]) -> dict[str, Any]:
    return win32_ocr_add_friend_windows.add_friend_phone_not_found_detected(ocr_items)


def add_friend_search_result_add_contact_target(ocr_items: list[dict[str, Any]], image_size: tuple[int, int]) -> dict[str, Any] | None:
    return win32_ocr_add_friend_windows.add_friend_search_result_add_contact_target(ocr_items, image_size)


def click_add_contact_entry_from_search_result(hwnd: int, output_dir: Path, *, result_shot: Image.Image, result_path: str, result_items: list[dict[str, Any]], query: str, verify_message: str = '', remark_name: str = '', remark_code: str = '') -> dict[str, Any]:
    win32_ocr_add_friend_windows.bind_sidecar_ops(sys.modules[__name__])
    return win32_ocr_add_friend_windows.click_add_contact_entry_from_search_result(hwnd, output_dir, result_shot=result_shot, result_path=result_path, result_items=result_items, query=query, verify_message=verify_message, remark_name=remark_name, remark_code=remark_code)


def add_friend_invite_form_targets(image_size: tuple[int, int], ocr_items: list[dict[str, Any]] | None = None) -> dict[str, dict[str, Any]]:
    return win32_ocr_add_friend_windows.add_friend_invite_form_targets(image_size, ocr_items)


def paste_invite_form_text(hwnd: int, target: dict[str, Any], text: str, *, action_name: str) -> dict[str, Any]:
    win32_ocr_add_friend_windows.bind_sidecar_ops(sys.modules[__name__])
    return win32_ocr_add_friend_windows.paste_invite_form_text(hwnd, target, text, action_name=action_name)


def fill_add_friend_invite_form_and_confirm(hwnd: int, output_dir: Path, *, verify_message: str, remark_name: str, remark_code: str) -> dict[str, Any]:
    win32_ocr_add_friend_windows.bind_sidecar_ops(sys.modules[__name__])
    return win32_ocr_add_friend_windows.fill_add_friend_invite_form_and_confirm(hwnd, output_dir, verify_message=verify_message, remark_name=remark_name, remark_code=remark_code)


def find_add_friend_page_search_targets(ocr_items: list[dict[str, Any]], image_size: tuple[int, int], screenshot: Image.Image | None = None) -> dict[str, Any]:
    return win32_ocr_add_friend_windows.find_add_friend_page_search_targets(ocr_items, image_size, screenshot)


def find_add_friend_search_placeholder_item(ocr_items: list[dict[str, Any]], image_size: tuple[int, int], *, search_region: list[int]) -> dict[str, Any] | None:
    return win32_ocr_add_friend_windows.find_add_friend_search_placeholder_item(ocr_items, image_size, search_region=search_region)


def find_add_friend_search_button_item(ocr_items: list[dict[str, Any]], image_size: tuple[int, int], *, search_region: list[int]) -> dict[str, Any] | None:
    return win32_ocr_add_friend_windows.find_add_friend_search_button_item(ocr_items, image_size, search_region=search_region)


def find_add_friend_search_button_by_visual(screenshot: Image.Image | None, image_size: tuple[int, int], *, search_region: list[int]) -> dict[str, Any] | None:
    return win32_ocr_add_friend_windows.find_add_friend_search_button_by_visual(screenshot, image_size, search_region=search_region)


def add_friend_query_visible_in_items(query: str, ocr_items: list[dict[str, Any]]) -> dict[str, Any]:
    return win32_ocr_add_friend_windows.add_friend_query_visible_in_items(query, ocr_items)


def add_friend_search_input_empty_in_items(ocr_items: list[dict[str, Any]], image_size: tuple[int, int]) -> dict[str, Any]:
    return win32_ocr_add_friend_windows.add_friend_search_input_empty_in_items(ocr_items, image_size)


def type_add_friend_query_like_human_for_entry(query: str) -> dict[str, Any]:
    win32_ocr_add_friend_windows.bind_sidecar_ops(sys.modules[__name__])
    return win32_ocr_add_friend_windows.type_add_friend_query_like_human_for_entry(query)


def backspace_add_friend_query_chars(count: int) -> dict[str, Any]:
    win32_ocr_add_friend_windows.bind_sidecar_ops(sys.modules[__name__])
    return win32_ocr_add_friend_windows.backspace_add_friend_query_chars(count)


def add_friend_dialog_surface_detected(ocr_items: list[dict[str, Any]]) -> dict[str, Any]:
    return win32_ocr_add_friend_windows.add_friend_dialog_surface_detected(ocr_items)


def is_add_friend_dialog_window_item(item: dict[str, Any], *, exclude_hwnd: int) -> bool:
    win32_ocr_add_friend_windows.bind_sidecar_ops(sys.modules[__name__])
    return win32_ocr_add_friend_windows.is_add_friend_dialog_window_item(item, exclude_hwnd=exclude_hwnd)


def wait_for_add_friend_dialog_window(*, exclude_hwnd: int, output_dir: Path, timeout_ms: int = 5000) -> dict[str, Any]:
    win32_ocr_add_friend_windows.bind_sidecar_ops(sys.modules[__name__])
    return win32_ocr_add_friend_windows.wait_for_add_friend_dialog_window(exclude_hwnd=exclude_hwnd, output_dir=output_dir, timeout_ms=timeout_ms)


def add_friend_invite_form_surface_detected(ocr_items: list[dict[str, Any]]) -> dict[str, Any]:
    return win32_ocr_add_friend_windows.add_friend_invite_form_surface_detected(ocr_items)


def is_add_friend_invite_form_window_item(item: dict[str, Any], *, exclude_hwnds: set[int]) -> bool:
    win32_ocr_add_friend_windows.bind_sidecar_ops(sys.modules[__name__])
    return win32_ocr_add_friend_windows.is_add_friend_invite_form_window_item(item, exclude_hwnds=exclude_hwnds)


def wait_for_add_friend_invite_form_window(*, exclude_hwnds: set[int], output_dir: Path, timeout_ms: int = 6000) -> dict[str, Any]:
    win32_ocr_add_friend_windows.bind_sidecar_ops(sys.modules[__name__])
    return win32_ocr_add_friend_windows.wait_for_add_friend_invite_form_window(exclude_hwnds=exclude_hwnds, output_dir=output_dir, timeout_ms=timeout_ms)


def click_add_friend_menu_entry_and_capture(hwnd: int, output_dir: Path, *, menu_targets: list[dict[str, Any]]) -> dict[str, Any]:
    win32_ocr_add_friend_windows.bind_sidecar_ops(sys.modules[__name__])
    return win32_ocr_add_friend_windows.click_add_friend_menu_entry_and_capture(hwnd, output_dir, menu_targets=menu_targets)


def input_add_friend_query_and_search(hwnd: int, output_dir: Path, *, query: str, verify_message: str = '', remark_name: str = '', remark_code: str = '') -> dict[str, Any]:
    win32_ocr_add_friend_windows.bind_sidecar_ops(sys.modules[__name__])
    return win32_ocr_add_friend_windows.input_add_friend_query_and_search(hwnd, output_dir, query=query, verify_message=verify_message, remark_name=remark_name, remark_code=remark_code)


def write_add_friend_entry_click_review(output_dir: Path, payload: dict[str, Any]) -> str:
    win32_ocr_add_friend_windows.bind_sidecar_ops(sys.modules[__name__])
    return win32_ocr_add_friend_windows.write_add_friend_entry_click_review(output_dir, payload)


def add_friend_entry_click_plan_payload(hwnd: int, probe: dict[str, Any], *, route: str = ADD_FRIEND_MAIN_ROUTE, phone: str = '', wechat: str = '', verify_message: str = '', remark_name: str = '', remark_code: str = '', artifact_dir: str | None = None, calibration_only: bool = False) -> dict[str, Any]:
    win32_ocr_add_friend_windows.bind_sidecar_ops(sys.modules[__name__])
    return win32_ocr_add_friend_windows.add_friend_entry_click_plan_payload(hwnd, probe, route=route, phone=phone, wechat=wechat, verify_message=verify_message, remark_name=remark_name, remark_code=remark_code, artifact_dir=artifact_dir, calibration_only=calibration_only)


ADD_FRIEND_FOREGROUND_READY_REASONS = {
    "foreground_matches_target",
    "foreground_root_matches_target",
}


def add_friend_focus_guard_ready(focus_guard: dict[str, Any]) -> dict[str, Any]:
    win32_ocr_add_friend_windows.bind_sidecar_ops(sys.modules[__name__])
    return win32_ocr_add_friend_windows.add_friend_focus_guard_ready(focus_guard)


def add_friend_pre_click_readiness_decision(*, focus_guard: dict[str, Any], surface_readiness: dict[str, Any]) -> dict[str, Any]:
    win32_ocr_add_friend_windows.bind_sidecar_ops(sys.modules[__name__])
    return win32_ocr_add_friend_windows.add_friend_pre_click_readiness_decision(focus_guard=focus_guard, surface_readiness=surface_readiness)


def add_friend_pre_click_main_window_readiness(hwnd: int, geometry: dict[str, Any], *, route: str, output_dir: Path) -> dict[str, Any]:
    win32_ocr_add_friend_windows.bind_sidecar_ops(sys.modules[__name__])
    return win32_ocr_add_friend_windows.add_friend_pre_click_main_window_readiness(hwnd, geometry, route=route, output_dir=output_dir)


def persist_add_friend_operator_guard_release(payload: dict[str, Any], release: dict[str, Any]) -> None:
    win32_ocr_add_friend_windows.bind_sidecar_ops(sys.modules[__name__])
    return win32_ocr_add_friend_windows.persist_add_friend_operator_guard_release(payload, release)


def add_friend_calibration_payload(hwnd: int, probe: dict[str, Any], *, geometry: dict[str, Any], route: str, phone: str, wechat: str, verify_message: str, remark_name: str, remark_code: str, output_dir: Path) -> dict[str, Any]:
    win32_ocr_add_friend_windows.bind_sidecar_ops(sys.modules[__name__])
    return win32_ocr_add_friend_windows.add_friend_calibration_payload(hwnd, probe, geometry=geometry, route=route, phone=phone, wechat=wechat, verify_message=verify_message, remark_name=remark_name, remark_code=remark_code, output_dir=output_dir)


def add_friend_failure_payload(*, error_code: str, message: str, steps: list[str], query: str, phone: str, wechat: str, probe: dict[str, Any], evidence: dict[str, Any] | None = None, state: str = 'add_friend_failed') -> dict[str, Any]:
    return win32_ocr_add_friend_windows.add_friend_failure_payload(error_code=error_code, message=message, steps=steps, query=query, phone=phone, wechat=wechat, probe=probe, evidence=evidence, state=state)


def add_friend_surface_readiness(screenshot: Image.Image, ocr_items: list[dict[str, Any]], geometry: dict[str, Any], *, stage: str, require_main_surface: bool | None = None) -> dict[str, Any]:
    win32_ocr_add_friend_windows.bind_sidecar_ops(sys.modules[__name__])
    return win32_ocr_add_friend_windows.add_friend_surface_readiness(screenshot, ocr_items, geometry, stage=stage, require_main_surface=require_main_surface)


def add_friend_main_entry_surface_evidence(ocr_items: list[dict[str, Any]], image_size: tuple[int, int]) -> dict[str, Any]:
    return win32_ocr_add_friend_windows.add_friend_main_entry_surface_evidence(ocr_items, image_size)


def add_friend_human_pause(min_ms: int, max_ms: int | None = None, *, reason: str = "") -> float:
    """Randomized add_friend pacing.

    The add_friend flow runs inside WeChat's sensitive contact-add surface.
    Keep mouse, keyboard and OCR phases strictly separated by visible human
    pauses so the flow does not look like a burst of synthetic operations.
    """
    checkpoint = add_friend_operator_guard_checkpoint(reason=f"pause:{reason or 'add_friend'}")
    multiplier = bounded_float(
        os.getenv("WECHAT_WIN32_OCR_ADD_FRIEND_HUMAN_PACE_MULTIPLIER"),
        default=1.0,
        minimum=0.6,
        maximum=4.0,
    )
    low = int(max(0, int(min_ms)) * multiplier)
    high_source = int(max_ms) if max_ms is not None else int(min_ms * 1.45)
    high = int(max(low, high_source * multiplier))
    delay = humanized_action_sleep(low, high)
    record_ui_action(
        "add_friend_human_pause",
        metadata={
            "reason": reason,
            "min_ms": low,
            "max_ms": high,
            "delay_seconds": delay,
            "pace_multiplier": multiplier,
            "operator_guard_checkpoint": checkpoint,
        },
    )
    return delay


def add_friend_paced_pause(tier: str, *, reason: str = "") -> float:
    low, high = pacing_range(tier)
    metadata = pacing_metadata(tier, reason=reason)
    if high <= 0:
        record_ui_action("add_friend_pacing_skip", metadata=metadata)
        return 0.0
    delay = add_friend_human_pause(low, high, reason=f"{metadata['tier']}:{reason}")
    record_ui_action(
        "add_friend_pacing_tier",
        metadata={
            **metadata,
            "delay_seconds": delay,
        },
    )
    return delay


def click_add_friend_ocr_item(hwnd: int, item: dict[str, Any]) -> None:
    win32_ocr_add_friend_windows.bind_sidecar_ops(sys.modules[__name__])
    return win32_ocr_add_friend_windows.click_add_friend_ocr_item(hwnd, item)


def add_friend_wait_before_ocr(reason: str) -> None:
    win32_ocr_add_friend_windows.bind_sidecar_ops(sys.modules[__name__])
    return win32_ocr_add_friend_windows.add_friend_wait_before_ocr(reason)


def clear_add_friend_sidebar_search_box(hwnd: int, search_x: int, search_y: int, *, target_hint: str = '') -> None:
    win32_ocr_add_friend_windows.bind_sidecar_ops(sys.modules[__name__])
    return win32_ocr_add_friend_windows.clear_add_friend_sidebar_search_box(hwnd, search_x, search_y, target_hint=target_hint)


def add_friend_virtual_key_for_digit(char: str) -> int:
    return win32_ocr_add_friend_windows.add_friend_virtual_key_for_digit(char)


def type_add_friend_phone_query_like_human(hwnd: int, query: str, *, key_press_func: Any | None = None, window_guard_func: Any | None = None) -> dict[str, Any]:
    win32_ocr_add_friend_windows.bind_sidecar_ops(sys.modules[__name__])
    return win32_ocr_add_friend_windows.type_add_friend_phone_query_like_human(hwnd, query, key_press_func=key_press_func, window_guard_func=window_guard_func)


def type_add_friend_search_query(hwnd: int, query: str) -> dict[str, Any]:
    win32_ocr_add_friend_windows.bind_sidecar_ops(sys.modules[__name__])
    return win32_ocr_add_friend_windows.type_add_friend_search_query(hwnd, query)


def add_friend_optional_field_fill_enabled() -> bool:
    return win32_ocr_add_friend_windows.add_friend_optional_field_fill_enabled()


def paste_add_friend_text_at_item(hwnd: int, item: dict[str, Any], text: str, image_size: tuple[int, int], *, x_offset: int = 150) -> dict[str, Any]:
    win32_ocr_add_friend_windows.bind_sidecar_ops(sys.modules[__name__])
    return win32_ocr_add_friend_windows.paste_add_friend_text_at_item(hwnd, item, text, image_size, x_offset=x_offset)


def fill_add_friend_optional_fields(hwnd: int, ocr_items: list[dict[str, Any]], image_size: tuple[int, int], *, remark: str, greeting: str) -> dict[str, Any]:
    win32_ocr_add_friend_windows.bind_sidecar_ops(sys.modules[__name__])
    return win32_ocr_add_friend_windows.fill_add_friend_optional_fields(hwnd, ocr_items, image_size, remark=remark, greeting=greeting)


def message_anchor_match_type(
    message: dict[str, Any],
    *,
    anchor_ids: set[str],
    anchor_content_keys: set[str],
    reply_content_keys: set[str],
) -> str:
    message_id = str(message.get("id") or "").strip()
    if message_id and message_id in anchor_ids:
        return "message_id"
    content_key = sidecar_message_content_key(message)
    if content_key and content_key in anchor_content_keys:
        return "message_content_key"
    reply_key = normalize_anchor_reply_key(message.get("content"))
    if reply_key and reply_key in reply_content_keys:
        return "reply_content_key"
    return ""


def send_payload(
    hwnd: int,
    probe: dict[str, Any],
    *,
    target: str,
    text: str,
    exact: bool,
    skip_send_rate_guard: bool = False,
    artifact_dir: str | None = None,
    validated_guard: dict[str, Any] | None = None,
) -> dict[str, Any]:
    timing: dict[str, Any] = {}
    ocr_trace_token = _ocr_trace_start()
    send_payload_started = _sidecar_timing_start(timing, "send_payload")

    def finish(payload: dict[str, Any]) -> dict[str, Any]:
        _sidecar_timing_finish(timing, "send_payload", send_payload_started)
        _sidecar_timing_merge_ocr_trace(timing, "send_payload", _ocr_trace_finish(ocr_trace_token))
        payload["timing"] = dict(timing)
        send_result = payload.get("send_result")
        if isinstance(send_result, dict):
            existing = send_result.get("timing")
            send_result_timing = dict(timing)
            if isinstance(existing, dict):
                send_result_timing.update(existing)
            send_result["timing"] = send_result_timing
        return payload

    reused_prevalidated_guard = bool(isinstance(validated_guard, dict) and validated_guard.get("ok"))
    pre_send_guard_started = _sidecar_timing_start(timing, "pre_send_guard")
    if reused_prevalidated_guard:
        validation = dict(validated_guard or {})
        # Re-check foreground/visibility quickly before using the cached target
        # confirmation, then re-run strict OCR target validation immediately
        # before typing.  Cached confirmation can become stale when the
        # scheduler switches between multiple chats; never let it authorize
        # customer-visible text by itself.
        focus_guard = recover_send_window_guard(hwnd, max_attempts=1)
        if not focus_guard.get("ok"):
            # Fallback to full active target validation to keep behavior robust
            # when foreground recovery is temporarily blocked.
            validation = validate_active_send_target(hwnd, target, exact=exact, artifact_dir=artifact_dir)
            _sidecar_timing_merge_validation(timing, "pre_send_guard_validation", validation)
            reused_prevalidated_guard = False
            if not validation.get("ok"):
                _sidecar_timing_finish(timing, "pre_send_guard", pre_send_guard_started)
                return finish({
                    "ok": False,
                    "online": bool(validation.get("online", True)),
                    "adapter": "win32_ocr",
                    "state": "send_guard_blocked",
                    "window_probe": probe,
                    "target": target,
                    "guard": {**validation, "window_guard": focus_guard},
                    "error": str(validation.get("error") or validation.get("reason") or "send guard blocked"),
                })
            geometry = validation["geometry"]
        else:
            strict_validation = validate_active_send_target(hwnd, target, exact=exact, artifact_dir=artifact_dir)
            _sidecar_timing_merge_validation(timing, "pre_send_guard_strict_validation", strict_validation)
            if not strict_validation.get("ok") or not active_send_guard_is_strong(strict_validation):
                _sidecar_timing_finish(timing, "pre_send_guard", pre_send_guard_started)
                return finish({
                    "ok": False,
                    "online": bool(strict_validation.get("online", True)),
                    "adapter": "win32_ocr",
                    "state": "send_guard_blocked",
                    "window_probe": probe,
                    "target": target,
                    "guard": {
                        **strict_validation,
                        "cached_prevalidated_guard": validation,
                        "window_guard": focus_guard,
                        "strict_recheck": True,
                    },
                    "error": str(strict_validation.get("error") or strict_validation.get("reason") or "send guard blocked"),
                })
            validation = {
                **strict_validation,
                "cached_prevalidated_guard": validation,
                "window_guard": focus_guard,
                "strict_recheck": True,
            }
            geometry = get_window_geometry(hwnd)
            geometry_check = validate_send_geometry(geometry)
            if not geometry_check.get("ok"):
                _sidecar_timing_finish(timing, "pre_send_guard", pre_send_guard_started)
                return finish({
                    "ok": False,
                    "online": True,
                    "adapter": "win32_ocr",
                    "state": "send_geometry_blocked",
                    "window_probe": probe,
                    "target": target,
                    "guard": {**validation, "geometry": geometry, "geometry_check": geometry_check},
                    "error": str(geometry_check.get("error") or "send geometry guard blocked"),
                })
            validation["geometry"] = geometry
    else:
        validation = validate_active_send_target(hwnd, target, exact=exact, artifact_dir=artifact_dir)
        _sidecar_timing_merge_validation(timing, "pre_send_guard_validation", validation)
        if not validation.get("ok") or not active_send_guard_is_strong(validation):
            _sidecar_timing_finish(timing, "pre_send_guard", pre_send_guard_started)
            return finish({
                "ok": False,
                "online": validation.get("online", True),
                "adapter": "win32_ocr",
                "state": "send_guard_blocked",
                "window_probe": probe,
                "target": target,
                "guard": validation,
                "error": str(validation.get("error") or validation.get("reason") or "send guard blocked"),
            })
        geometry = validation["geometry"]
    _sidecar_timing_finish(timing, "pre_send_guard", pre_send_guard_started)
    points = calculate_send_points(geometry)
    if not points.get("ok"):
        return finish({
            "ok": False,
            "online": True,
            "adapter": "win32_ocr",
            "state": "send_geometry_blocked",
            "window_probe": probe,
            "target": target,
            "guard": {**validation, "points": points},
            "error": str(points.get("error") or "send points were unsafe"),
        })
    input_region_seed = consume_input_region_precheck_ocr_seed(
        hwnd=hwnd,
        target=target,
        exact=exact,
        geometry=geometry,
    )
    timing["input_region_precheck_seed_reused"] = bool(input_region_seed)
    if isinstance(input_region_seed, dict):
        timing["input_region_precheck_seed_age_seconds"] = input_region_seed.get("age_seconds")
    requested_send_mode = str(os.getenv("WECHAT_WIN32_OCR_SEND_MODE") or DEFAULT_SEND_MODE).strip().lower()
    settings = adapt_humanized_input_settings(humanized_input_settings(), text)
    send_mode = requested_send_mode
    # When intermittent typing is enforced, keep send path on guarded-click flow
    # so we never downgrade to one-shot UIA SetValue in practice.
    if (
        settings.get("enabled")
        and str(settings.get("method") or "") in {"clipboard_chunks", "sendinput_unicode"}
        and requested_send_mode in {"uia_first", "uia_only"}
    ):
        send_mode = "click_only"
    if skip_send_rate_guard:
        rate_guard_started = _sidecar_timing_start(timing, "rate_guard")
        rate = {
            "ok": True,
            "reason": "rate_guard_skipped_for_loopback",
            "skip_send_rate_guard": True,
        }
        _sidecar_timing_finish(timing, "rate_guard", rate_guard_started)
    else:
        rate_guard_started = _sidecar_timing_start(timing, "rate_guard")
        rate = reserve_send_rate(target=target, text=text)
        _sidecar_timing_finish(timing, "rate_guard", rate_guard_started)
    if not rate.get("ok"):
        return finish({
            "ok": False,
            "online": True,
            "adapter": "win32_ocr",
            "state": "send_rate_limited",
            "window_probe": probe,
            "target": target,
            "guard": {**validation, "points": points, "rate": rate},
            "error": str(rate.get("error") or "win32_ocr fallback send is rate limited"),
        })
    uia_result = {"ok": False, "reason": "not_attempted", "mode": send_mode}
    click_result: dict[str, Any] = {"ok": False, "reason": "not_attempted", "mode": send_mode}
    if send_mode in {"uia_first", "uia_only"}:
        uia_send_started = _sidecar_timing_start(timing, "uia_send")
        uia_result = send_with_uia_controls(hwnd, text, geometry=geometry, settings=settings)
        _sidecar_timing_finish(timing, "uia_send", uia_send_started)
    if not uia_result.get("ok"):
        if send_mode == "uia_only":
            return finish({
                "ok": False,
                "online": True,
                "adapter": "win32_ocr",
                "state": "send_uia_unavailable",
                "window_probe": probe,
                "target": target,
                "guard": {**validation, "points": points, "rate": rate, "uia": uia_result},
                "error": str(uia_result.get("error") or "UIA controls are unavailable for safe send."),
            })
        guarded_click_started = _sidecar_timing_start(timing, "guarded_click_send")
        click_result = send_with_guarded_clicks(
            hwnd,
            text,
            points=points,
            geometry=geometry,
            allow_unconfirmed_paste=bool(validation.get("blind_send")),
            artifact_dir=artifact_dir,
            settings=settings,
            before_input_region_seed=input_region_seed,
        )
        _sidecar_timing_finish(timing, "guarded_click_send", guarded_click_started)
        if isinstance(click_result.get("timing"), dict):
            for key, value in click_result["timing"].items():
                timing.setdefault(str(key), value)
    if not uia_result.get("ok") and not click_result.get("ok"):
        return finish({
            "ok": False,
            "online": True,
            "adapter": "win32_ocr",
            "state": "send_input_not_ready",
            "window_probe": probe,
            "target": target,
            "guard": {
                **validation,
                "points": points,
                "rate": rate,
                "uia": uia_result,
                "click": click_result,
            },
            "error": str(click_result.get("error") or uia_result.get("error") or "send input could not be confirmed"),
        })
    humanized_action_sleep(200, 420)
    post_send_guard_started = _sidecar_timing_start(timing, "post_send_guard")
    post_validation = validate_post_send_target(hwnd, target, exact=exact, artifact_dir=artifact_dir)
    _sidecar_timing_finish(timing, "post_send_guard", post_send_guard_started)
    if str(post_validation.get("reason") or "") == "blank_render":
        return finish({
            "ok": False,
            "online": False,
            "adapter": "win32_ocr",
            "state": "send_post_guard_blank_render",
            "window_probe": probe,
            "target": target,
            "guard": {
                **validation,
                "points": points,
                "rate": rate,
                "uia": uia_result,
                "click": click_result,
                "post_send_guard": post_validation,
            },
            "error": "WeChat render became blank after input/send; stop before any further RPA action.",
        })
    active_result = uia_result if uia_result.get("ok") else click_result
    return finish({
        "ok": True,
        "online": True,
        "adapter": "win32_ocr",
        "state": "send_win32_rpa",
        "window_probe": probe,
        "target": target,
        "send_result": {
            "ok": bool(active_result.get("ok")),
            "method": active_result.get("method") or "win32.click_input+rpa_text_entry+click_send",
            "mode": send_mode,
            "requested_mode": requested_send_mode,
            "humanized_method": settings.get("method"),
            "validation_source": "prevalidated_guard_strict_recheck" if reused_prevalidated_guard else "active_send_guard",
            "pre_send_guard": validation,
            "geometry": geometry,
            "input_point": points["input_point"],
            "send_point": points["send_point"],
            "rate": rate,
            "uia": uia_result,
            "click": click_result,
            "post_send_guard": post_validation,
        },
    })


def normalize_humanized_input_method(raw_method: str | None, *, default: str = DEFAULT_HUMANIZED_INPUT_METHOD) -> str:
    return win32_ocr_env.normalize_humanized_input_method(raw_method, default=default)


def normalize_send_trigger_mode(raw_mode: str | None, *, default: str = DEFAULT_SEND_TRIGGER_MODE) -> str:
    return win32_ocr_env.normalize_send_trigger_mode(raw_mode, default=default)


def humanized_input_settings() -> dict[str, Any]:
    return win32_ocr_humanized.humanized_input_settings()


def adapt_humanized_input_settings(settings: dict[str, Any], text: str) -> dict[str, Any]:
    return win32_ocr_humanized.adapt_humanized_input_settings(settings, text)


def humanized_sleep_ms(min_ms: int, max_ms: int) -> float:
    low = max(0, int(min_ms))
    high = max(low, int(max_ms))
    if high <= 0:
        return 0.0
    delay = random.uniform(float(low) / 1000.0, float(high) / 1000.0)
    time.sleep(delay)
    return round(delay, 3)


def humanized_action_sleep(min_ms: int, max_ms: int | None = None) -> float:
    """Small randomized settle time for RPA UI actions."""
    low = max(0, int(min_ms))
    if max_ms is None:
        spread = max(8, int(low * 0.25))
        high = low + spread
        low = max(0, low - spread)
    else:
        high = max(low, int(max_ms))
    return humanized_sleep_ms(low, high)


def humanized_chunk_text(text: str, *, min_chars: int, max_chars: int) -> list[str]:
    return win32_ocr_humanized.humanized_chunk_text(text, min_chars=min_chars, max_chars=max_chars)


def choose_humanized_typo_char() -> str:
    return win32_ocr_humanized.choose_humanized_typo_char()


def typed_text_delay_ms(segment: str, settings: dict[str, Any]) -> tuple[int, int]:
    return win32_ocr_humanized.typed_text_delay_ms(segment, settings)


def maybe_humanized_typo_allowed(settings: dict[str, Any], *, typo_count: int, text: str) -> bool:
    return win32_ocr_humanized.maybe_humanized_typo_allowed(settings, typo_count=typo_count, text=text)


def message_probe_tokens(text: str) -> list[str]:
    first_line = str(text or "").splitlines()[0].strip()
    if not first_line:
        return []
    compact = re.sub(r"\s+", "", first_line)
    if not compact:
        return []
    tokens: list[str] = []

    def add_token(candidate: str) -> None:
        token = str(candidate or "").strip()
        if len(token) < 2:
            return
        if token not in tokens:
            tokens.append(token)

    semantic = compact
    # Live acceptance/customer-service messages often carry a bracketed marker
    # before the real customer text. OCR may split or drop that marker, so use
    # semantic body fragments first and keep the old prefix/suffix fallback.
    for _ in range(3):
        stripped = re.sub(r"^(?:【[^】]{1,80}】|\[[^\]]{1,80}\]|（[^）]{1,80}）|\([^)]{1,80}\))", "", semantic)
        if stripped == semantic:
            break
        semantic = stripped
    semantic = semantic.lstrip("：:，,。；;、 ")

    semantic_spans = re.findall(r"[\u4e00-\u9fffA-Za-z0-9]{3,18}", semantic)
    for span in semantic_spans:
        if not re.search(r"[\u4e00-\u9fff]", span):
            continue
        variants = [span]
        if len(span) >= 4 and span[0] in {"我", "想", "要", "请"}:
            variants.append(span[1:])
        for variant in variants:
            add_token(variant[:10])
            add_token(variant[:6])
            add_token(variant[-6:])
            if len(tokens) >= 8:
                break
        if len(tokens) >= 8:
            break

    for candidate in (compact[:10], compact[:6], compact[-6:]):
        add_token(candidate)
    return tokens


def message_probe_token(text: str) -> str:
    tokens = message_probe_tokens(text)
    return tokens[0] if tokens else ""


def input_area_contains_token(
    ocr_items: list[dict[str, Any]],
    *,
    geometry: dict[str, Any],
    token: str,
) -> bool:
    if not token:
        return False
    normalized_token = re.sub(r"\s+", "", token)
    for item in ocr_items:
        text = normalize_ocr_text(item.get("text"))
        if not text:
            continue
        rect = {
            "left": int(float(item.get("left") or 0)),
            "top": int(float(item.get("top") or 0)),
            "right": int(float(item.get("right") or 0)),
            "bottom": int(float(item.get("bottom") or 0)),
        }
        if not rect_in_input_area(rect, geometry):
            continue
        compact = re.sub(r"\s+", "", text)
        if normalized_token in compact or compact in normalized_token:
            return True
    return False


def input_area_contains_any_token(
    ocr_items: list[dict[str, Any]],
    *,
    geometry: dict[str, Any],
    tokens: list[str],
) -> bool:
    for token in tokens:
        if input_area_contains_token(ocr_items, geometry=geometry, token=token):
            return True
    return False


def input_text_region_bounds(geometry: dict[str, Any]) -> tuple[int, int, int, int]:
    return win32_ocr_geometry.input_text_region_bounds(geometry)


def rect_overlaps_region(rect: dict[str, int], bounds: tuple[int, int, int, int]) -> bool:
    return win32_ocr_geometry.rect_overlaps_region(rect, bounds)


def input_text_region_state(
    screenshot: Any,
    ocr_items: list[dict[str, Any]],
    *,
    geometry: dict[str, Any],
) -> dict[str, Any]:
    """Detect whether the input text area visibly contains text.

    This is deliberately conservative: if text-like pixels are present but OCR
    missed the probe token, we stop instead of retrying and risking a duplicate.
    """
    bounds = input_text_region_bounds(geometry)
    ocr_hits = 0
    for item in ocr_items:
        text = normalize_ocr_text(item.get("text"))
        if not text:
            continue
        rect = {
            "left": int(float(item.get("left") or 0)),
            "top": int(float(item.get("top") or 0)),
            "right": int(float(item.get("right") or 0)),
            "bottom": int(float(item.get("bottom") or 0)),
        }
        if rect_overlaps_region(rect, bounds):
            ocr_hits += 1
    try:
        gray = screenshot.convert("L")
        crop = gray.crop(bounds)
        histogram = crop.histogram()
        total = max(1, int(sum(histogram)))
        dark_ratio = float(sum(histogram[:180])) / float(total)
        bright_ratio = float(sum(histogram[200:])) / float(total)
        mean = float(sum(index * count for index, count in enumerate(histogram))) / float(total)
    except Exception as exc:
        return {
            "has_visible_text": bool(ocr_hits),
            "reason": "input_region_probe_failed",
            "error": repr(exc),
            "bounds": list(bounds),
            "ocr_hits": ocr_hits,
        }
    # In dark-mode WeChat the whole input region can be dark even when blank.
    # Treat a uniformly dark crop without OCR or bright text strokes as blank;
    # otherwise the send guard will repeatedly refuse to type into an empty box.
    dark_theme_blank_like = bool(
        dark_ratio >= 0.90
        and mean <= 90.0
        and bright_ratio <= 0.002
    )
    pixel_visible = dark_ratio >= INPUT_TEXT_DARK_RATIO_MIN and not dark_theme_blank_like
    # OCR boxes can drift into the lower chat/input boundary on fresh captures.
    # Treat OCR as draft evidence only when the crop is not a uniformly dark
    # blank input box; otherwise dark-mode backgrounds with boundary OCR noise
    # block safe typing in an empty box.
    ocr_visible = bool(ocr_hits > 0 and not dark_theme_blank_like and dark_ratio >= INPUT_TEXT_DARK_RATIO_MIN / 3.0)
    has_visible_text = bool(pixel_visible or ocr_visible)
    return {
        "has_visible_text": has_visible_text,
        "reason": "ocr_or_dark_pixels" if has_visible_text else "input_region_blank",
        "bounds": list(bounds),
        "ocr_hits": ocr_hits,
        "dark_ratio": round(dark_ratio, 6),
        "bright_ratio": round(bright_ratio, 6),
        "mean": round(mean, 3),
        "threshold": INPUT_TEXT_DARK_RATIO_MIN,
        "dark_theme_blank_like": dark_theme_blank_like,
    }


def input_region_visual_delta_confirms(
    before: dict[str, Any],
    after: dict[str, Any],
    input_result: dict[str, Any] | None,
) -> dict[str, Any]:
    """Confirm typed input by a conservative before/after visual delta."""
    if not input_result or not input_result.get("ok"):
        return {"ok": False, "reason": "input_operation_failed"}
    try:
        typed_chars = int(input_result.get("typed_chars") or 0)
    except Exception:
        typed_chars = 0
    method = str(input_result.get("method") or "")
    if typed_chars <= 0 and method not in {"clipboard_once", "clipboard_chunks"}:
        return {"ok": False, "reason": "no_typed_chars"}
    if bool(before.get("has_visible_text")):
        return {"ok": False, "reason": "input_region_not_blank_before_type"}
    if not bool(after.get("has_visible_text")):
        return {"ok": False, "reason": "input_region_still_blank_after_type"}
    try:
        before_dark = float(before.get("dark_ratio") or 0.0)
        after_dark = float(after.get("dark_ratio") or 0.0)
    except Exception:
        before_dark = 0.0
        after_dark = 0.0
    before_hits = int(before.get("ocr_hits") or 0)
    after_hits = int(after.get("ocr_hits") or 0)
    dark_delta = after_dark - before_dark
    min_delta = max(INPUT_TEXT_DARK_RATIO_MIN * 2.0, 0.006)
    if after_hits > before_hits or dark_delta >= min_delta:
        return {
            "ok": True,
            "reason": "input_area_visual_delta",
            "before": before,
            "after": after,
            "dark_delta": round(dark_delta, 6),
            "ocr_hit_delta": after_hits - before_hits,
        }
    return {
        "ok": False,
        "reason": "input_area_delta_too_small",
        "before": before,
        "after": after,
        "dark_delta": round(dark_delta, 6),
        "ocr_hit_delta": after_hits - before_hits,
    }


def input_region_soft_blank_noise(state: dict[str, Any]) -> bool:
    """Return True when a draft probe is likely toolbar/shadow noise, not text."""
    if not isinstance(state, dict):
        return False
    try:
        dark_ratio = float(state.get("dark_ratio") or 0.0)
    except Exception:
        dark_ratio = 0.0
    try:
        mean = float(state.get("mean") or 0.0)
    except Exception:
        mean = 0.0
    try:
        ocr_hits = int(state.get("ocr_hits") or 0)
    except Exception:
        ocr_hits = 0
    return bool(
        ocr_hits == 0
        and dark_ratio <= INPUT_TEXT_SOFT_BLANK_DARK_RATIO_MAX
        and mean >= INPUT_TEXT_SOFT_BLANK_MEAN_MIN
    )


def normalize_soft_blank_input_state(state: dict[str, Any], *, reason: str) -> dict[str, Any]:
    normalized = dict(state or {})
    normalized["has_visible_text"] = False
    normalized["reason"] = reason
    normalized["soft_blank_noise"] = True
    return normalized


def clear_existing_input_draft(
    hwnd: int,
    *,
    points: dict[str, Any],
    geometry: dict[str, Any],
    before_state: dict[str, Any],
    artifact_dir: str | None = None,
    attempt: int = 1,
) -> dict[str, Any]:
    """Clear a stale WeChat draft only when the input area is already non-empty."""
    if not before_state.get("has_visible_text"):
        return {"ok": True, "cleared": False, "reason": "input_region_already_blank", "before": before_state}
    if input_region_soft_blank_noise(before_state):
        blank = normalize_soft_blank_input_state(before_state, reason="input_region_soft_blank_noise")
        return {"ok": True, "cleared": False, "reason": "input_region_soft_blank_noise", "before": blank}
    input_x, input_y = jitter_input_click_point(
        int(points["input_point"][0]),
        int(points["input_point"][1]),
        geometry,
    )
    human_client_click(hwnd, input_x, input_y)
    time.sleep(random.uniform(0.08, 0.16))
    # Avoid Ctrl+A here: select-all artifacts can leak to chat history when
    # focus drifts. Use bounded backspace/delete bursts instead.
    key_press(win32con.VK_END)
    humanized_action_sleep(24, 70)
    backspaces = bounded_int(
        os.getenv("WECHAT_WIN32_OCR_INPUT_DRAFT_CLEAR_BACKSPACES"),
        default=96,
        minimum=24,
        maximum=160,
    )
    deletes = bounded_int(
        os.getenv("WECHAT_WIN32_OCR_INPUT_DRAFT_CLEAR_DELETES"),
        default=8,
        minimum=0,
        maximum=24,
    )
    for idx in range(backspaces):
        key_press(win32con.VK_BACK)
        humanized_action_sleep(8, 26)
        if idx > 0 and idx % 7 == 0:
            humanized_action_sleep(22, 66)
    for _ in range(deletes):
        key_press(win32con.VK_DELETE)
        humanized_action_sleep(10, 30)
    time.sleep(random.uniform(0.16, 0.32))
    screenshot, _path = capture_wechat(hwnd, artifact_dir=artifact_dir, label=f"send_input_clear_{attempt}")
    ocr_items = run_ocr_traced(screenshot, "input_after_clear_draft", source="clear_existing_input_draft")
    after_state = input_text_region_state(screenshot, ocr_items, geometry=geometry)
    if not after_state.get("has_visible_text") or input_region_soft_blank_noise(after_state):
        if input_region_soft_blank_noise(after_state):
            after_state = normalize_soft_blank_input_state(after_state, reason="input_region_soft_blank_after_clear")
        return {
            "ok": True,
            "cleared": True,
            "reason": "input_region_cleared",
            "before": before_state,
            "after": after_state,
        }
    return {
        "ok": False,
        "cleared": False,
        "reason": "input_region_clear_failed",
        "before": before_state,
        "after": after_state,
        "error": "Could not safely clear pre-existing WeChat draft text.",
    }


def paste_text_once(text: str) -> None:
    clipboard_copy(text)
    hotkey(win32con.VK_CONTROL, ord("V"))


def sendinput_safe_text(text: str) -> str:
    return win32_ocr_humanized.sendinput_safe_text(text)


def sendinput_utf16_units(text: str) -> list[int]:
    encoded = str(text or "").encode("utf-16-le", errors="surrogatepass")
    return [int.from_bytes(encoded[index:index + 2], "little") for index in range(0, len(encoded), 2)]


def sendinput_unicode_unit(unit: int) -> None:
    ULONG_PTR = getattr(wintypes, "ULONG_PTR", wintypes.WPARAM)

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [
            ("wVk", wintypes.WORD),
            ("wScan", wintypes.WORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ULONG_PTR),
        ]

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [
            ("dx", wintypes.LONG),
            ("dy", wintypes.LONG),
            ("mouseData", wintypes.DWORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ULONG_PTR),
        ]

    class HARDWAREINPUT(ctypes.Structure):
        _fields_ = [
            ("uMsg", wintypes.DWORD),
            ("wParamL", wintypes.WORD),
            ("wParamH", wintypes.WORD),
        ]

    class INPUTUNION(ctypes.Union):
        _fields_ = [
            ("ki", KEYBDINPUT),
            ("mi", MOUSEINPUT),
            ("hi", HARDWAREINPUT),
        ]

    class INPUT(ctypes.Structure):
        _anonymous_ = ("union",)
        _fields_ = [
            ("type", wintypes.DWORD),
            ("union", INPUTUNION),
        ]

    scan = int(unit) & 0xFFFF
    sequence = (INPUT * 2)(
        INPUT(
            type=SENDINPUT_INPUT_KEYBOARD,
            ki=KEYBDINPUT(
                wVk=0,
                wScan=scan,
                dwFlags=SENDINPUT_KEYEVENTF_UNICODE,
                time=0,
                dwExtraInfo=ULONG_PTR(0),
            ),
        ),
        INPUT(
            type=SENDINPUT_INPUT_KEYBOARD,
            ki=KEYBDINPUT(
                wVk=0,
                wScan=scan,
                dwFlags=SENDINPUT_KEYEVENTF_UNICODE | SENDINPUT_KEYEVENTF_KEYUP,
                time=0,
                dwExtraInfo=ULONG_PTR(0),
            ),
        ),
    )
    coordinate_rpa_action("sendinput_unicode_unit", metadata={"unit": int(unit)})
    sent = ctypes.windll.user32.SendInput(2, ctypes.byref(sequence), ctypes.sizeof(INPUT))
    if int(sent) != 2:
        raise RuntimeError(f"sendinput_unicode_failed: sent={int(sent)}")


def type_text_with_sendinput_unicode(
    text: str,
    settings: dict[str, Any],
    *,
    send_unit_func: Any | None = None,
    window_guard_func: Any | None = None,
) -> dict[str, Any]:
    safe_text = sendinput_safe_text(text)
    chunks = humanized_chunk_text(
        safe_text,
        min_chars=int(settings.get("chunk_min_chars") or DEFAULT_HUMANIZED_TYPING_CHUNK_MIN_CHARS),
        max_chars=int(settings.get("chunk_max_chars") or DEFAULT_HUMANIZED_TYPING_CHUNK_MAX_CHARS),
    )
    if not chunks:
        return {
            "ok": True,
            "method": "sendinput_unicode",
            "chunks": 0,
            "typo_count": 0,
            "typed_chars": 0,
            "normalized_newlines": safe_text != str(text or ""),
        }
    send_unit = send_unit_func or sendinput_unicode_unit
    typed_chars = 0
    typo_count = 0
    micro_every = int(settings.get("micro_pause_every_chars") or 0)
    micro_bucket = 0
    try:
        for chunk_index, chunk in enumerate(chunks, start=1):
            if window_guard_func is not None:
                guard = window_guard_func()
                if not guard.get("ok"):
                    return {
                        "ok": False,
                        "method": "sendinput_unicode",
                        "reason": "window_lost_during_sendinput",
                        "window_guard": guard,
                        "chunks": len(chunks),
                        "completed_chunks": chunk_index - 1,
                        "typed_chars": typed_chars,
                        "typo_count": typo_count,
                    }
            for unit in sendinput_utf16_units(chunk):
                if window_guard_func is not None:
                    guard = window_guard_func()
                    if not guard.get("ok"):
                        return {
                            "ok": False,
                            "method": "sendinput_unicode",
                            "reason": "window_lost_during_sendinput",
                            "window_guard": guard,
                            "chunks": len(chunks),
                            "completed_chunks": chunk_index - 1,
                            "typed_chars": typed_chars,
                            "typo_count": typo_count,
                        }
                send_unit(unit)
            typed_chars += len(chunk)
            delay_low, delay_high = typed_text_delay_ms(chunk, settings)
            humanized_sleep_ms(delay_low, delay_high)
            if maybe_humanized_typo_allowed(settings, typo_count=typo_count, text=safe_text):
                if window_guard_func is not None:
                    guard = window_guard_func()
                    if not guard.get("ok"):
                        return {
                            "ok": False,
                            "method": "sendinput_unicode",
                            "reason": "window_lost_during_sendinput",
                            "window_guard": guard,
                            "chunks": len(chunks),
                            "completed_chunks": chunk_index,
                            "typed_chars": typed_chars,
                            "typo_count": typo_count,
                        }
                typo = choose_humanized_typo_char()
                for unit in sendinput_utf16_units(typo):
                    send_unit(unit)
                humanized_sleep_ms(40, 120)
                if window_guard_func is not None:
                    guard = window_guard_func()
                    if not guard.get("ok"):
                        return {
                            "ok": False,
                            "method": "sendinput_unicode",
                            "reason": "window_lost_during_sendinput",
                            "window_guard": guard,
                            "chunks": len(chunks),
                            "completed_chunks": chunk_index,
                            "typed_chars": typed_chars,
                            "typo_count": typo_count,
                        }
                key_press(win32con.VK_BACK)
                typo_count += 1
                humanized_sleep_ms(50, 130)
            if micro_every > 0:
                current_bucket = typed_chars // micro_every
                if current_bucket > micro_bucket:
                    micro_bucket = current_bucket
                    humanized_sleep_ms(
                        int(settings.get("micro_pause_min_ms") or DEFAULT_HUMANIZED_TYPING_MICRO_PAUSE_MIN_MS),
                        int(settings.get("micro_pause_max_ms") or DEFAULT_HUMANIZED_TYPING_MICRO_PAUSE_MAX_MS),
                    )
    except Exception as exc:
        return {
            "ok": False,
            "method": "sendinput_unicode",
            "error": repr(exc),
            "chunks": len(chunks),
            "typed_chars": typed_chars,
            "typo_count": typo_count,
        }
    return {
        "ok": True,
        "method": "sendinput_unicode",
        "chunks": len(chunks),
        "typo_count": typo_count,
        "typed_chars": typed_chars,
        "normalized_newlines": safe_text != str(text or ""),
    }


def strict_send_focus_guard_enabled() -> bool:
    return win32_ocr_env.strict_send_focus_guard_enabled()


def focus_click_fallback_enabled() -> bool:
    return win32_ocr_env.env_flag("WECHAT_WIN32_OCR_FOCUS_CLICK_FALLBACK", default=DEFAULT_FOCUS_CLICK_FALLBACK)


def allow_unknown_foreground_guard() -> bool:
    return win32_ocr_env.allow_unknown_foreground_guard()


def process_executable_path(pid: int) -> str:
    if pid <= 0:
        return ""
    try:
        kernel32 = ctypes.windll.kernel32
        process_query_limited_information = 0x1000
        handle = kernel32.OpenProcess(process_query_limited_information, False, int(pid))
        if not handle:
            return ""
        try:
            size = wintypes.DWORD(32768)
            buffer = ctypes.create_unicode_buffer(size.value)
            if kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                return str(buffer.value or "")
            return ""
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return ""


def foreground_window_matches_target(hwnd: int) -> dict[str, Any]:
    def _window_brief(candidate_hwnd: int) -> dict[str, Any]:
        brief: dict[str, Any] = {"hwnd": int(candidate_hwnd or 0)}
        if not candidate_hwnd:
            return brief
        try:
            brief["title"] = str(win32gui.GetWindowText(candidate_hwnd) or "")
        except Exception:
            brief["title"] = ""
        try:
            brief["class_name"] = str(win32gui.GetClassName(candidate_hwnd) or "")
        except Exception:
            brief["class_name"] = ""
        try:
            pid = int(win32process.GetWindowThreadProcessId(candidate_hwnd)[1] or 0)
        except Exception:
            pid = 0
        brief["pid"] = pid
        if pid > 0:
            try:
                path = process_executable_path(pid)
            except Exception:
                path = ""
            if path:
                brief["path"] = path
        return brief

    if not hwnd or win32gui is None:
        return {"ok": True, "reason": "foreground_guard_unavailable"}
    try:
        foreground = int(win32gui.GetForegroundWindow() or 0)
    except Exception as exc:
        return {"ok": False, "reason": "foreground_probe_failed", "error": repr(exc), "hwnd": int(hwnd or 0)}
    if foreground == 0:
        if allow_unknown_foreground_guard():
            return {
                "ok": True,
                "reason": "foreground_unknown_guard_degraded",
                "hwnd": int(hwnd),
                "foreground_hwnd": 0,
                "foreground_root_hwnd": 0,
            }
        return {
            "ok": False,
            "reason": "foreground_not_wechat_target",
            "hwnd": int(hwnd),
            "foreground_hwnd": 0,
            "foreground_root_hwnd": 0,
        }
    if foreground == int(hwnd):
        return {"ok": True, "reason": "foreground_matches_target", "hwnd": int(hwnd), "foreground_hwnd": foreground}
    root = 0
    try:
        root = int(win32gui.GetAncestor(foreground, 2) or 0) if foreground else 0
    except Exception:
        root = 0
    if root == int(hwnd):
        return {
            "ok": True,
            "reason": "foreground_root_matches_target",
            "hwnd": int(hwnd),
            "foreground_hwnd": foreground,
            "foreground_root_hwnd": root,
        }
    return {
        "ok": False,
        "reason": "foreground_not_wechat_target",
        "hwnd": int(hwnd),
        "foreground_hwnd": foreground,
        "foreground_root_hwnd": root,
        "foreground_window": _window_brief(foreground),
        "foreground_root_window": _window_brief(root),
    }


def dismiss_blank_foreground_window_before_activation(hwnd: int, *, artifact_dir: str | None = None) -> dict[str, Any]:
    if not hwnd or win32gui is None:
        return {"attempted": False, "reason": "window_unavailable"}
    try:
        foreground = int(win32gui.GetForegroundWindow() or 0)
    except Exception as exc:
        return {"attempted": False, "reason": "foreground_probe_failed", "error": repr(exc)}
    if not foreground or foreground == int(hwnd):
        return {"attempted": False, "reason": "foreground_already_target_or_unknown", "foreground_hwnd": foreground}
    try:
        pid = int(win32process.GetWindowThreadProcessId(foreground)[1] or 0)
    except Exception:
        pid = 0
    path = process_executable_path(pid)
    if not path.lower().endswith("\\weixin.exe"):
        return {"attempted": False, "reason": "foreground_not_weixin", "foreground_hwnd": foreground, "pid": pid}
    try:
        geometry = get_window_geometry(foreground)
        screenshot, screenshot_path = capture_wechat(
            foreground,
            artifact_dir=artifact_dir,
            label="foreground_blank_dismissal_probe",
        )
        ocr_items = run_ocr(screenshot)
        blank_render = detect_blank_render(screenshot, ocr_items, geometry=geometry)
    except Exception as exc:
        return {
            "attempted": False,
            "reason": "foreground_blank_probe_failed",
            "foreground_hwnd": foreground,
            "pid": pid,
            "error": repr(exc),
        }
    if not blank_render.get("detected"):
        return {
            "attempted": False,
            "reason": "foreground_weixin_not_blank",
            "foreground_hwnd": foreground,
            "pid": pid,
            "ocr_count": len(ocr_items),
            "blank_render": blank_render,
            "screenshot_path": screenshot_path,
        }
    try:
        ensure_left_button_released()
        win32gui.ShowWindow(foreground, win32con.SW_MINIMIZE)
        humanized_action_sleep(180, 320)
        return {
            "attempted": True,
            "ok": True,
            "reason": "blank_foreground_minimized_before_activation",
            "foreground_hwnd": foreground,
            "pid": pid,
            "geometry": geometry,
            "ocr_count": len(ocr_items),
            "blank_render": blank_render,
            "screenshot_path": screenshot_path,
        }
    except Exception as exc:
        return {
            "attempted": True,
            "ok": False,
            "reason": "blank_foreground_minimize_failed",
            "foreground_hwnd": foreground,
            "pid": pid,
            "geometry": geometry,
            "ocr_count": len(ocr_items),
            "blank_render": blank_render,
            "screenshot_path": screenshot_path,
            "error": repr(exc),
        }


def non_retryable_input_failure(result: dict[str, Any] | None) -> bool:
    if not isinstance(result, dict):
        return False
    reason = str(result.get("reason") or "")
    if reason in {"window_lost_during_sendinput", "foreground_not_wechat_target", "foreground_probe_failed"}:
        return True
    guard = result.get("window_guard") if isinstance(result.get("window_guard"), dict) else {}
    guard_reason = str(guard.get("reason") or "")
    return guard_reason in {"foreground_not_wechat_target", "foreground_probe_failed", "window_handle_invalid", "window_not_visible"}


def basic_send_window_guard(hwnd: int) -> dict[str, Any]:
    try:
        if not bool(win32gui.IsWindow(hwnd)):
            return {"ok": False, "reason": "window_handle_invalid"}
        if not bool(win32gui.IsWindowVisible(hwnd)):
            return {"ok": False, "reason": "window_not_visible"}
        geometry = get_window_geometry(hwnd)
        send_geometry = validate_send_geometry(geometry)
        if not send_geometry.get("ok"):
            return {"ok": False, "reason": str(send_geometry.get("reason") or "send_geometry_invalid"), "geometry": geometry}
        if strict_send_focus_guard_enabled():
            focus_guard = foreground_window_matches_target(hwnd)
            if not focus_guard.get("ok"):
                return {"ok": False, **focus_guard, "geometry": geometry}
    except Exception as exc:
        return {"ok": False, "reason": "window_guard_failed", "error": repr(exc)}
    return {"ok": True, "reason": "window_valid"}


def send_window_guard_can_recover_by_activation(guard: dict[str, Any] | None) -> bool:
    if not isinstance(guard, dict):
        return False
    reason = str(guard.get("reason") or "")
    if reason in {"foreground_not_wechat_target", "foreground_probe_failed"}:
        return True
    geometry = guard.get("geometry") if isinstance(guard.get("geometry"), dict) else {}
    left = int(geometry.get("left") or 0)
    top = int(geometry.get("top") or 0)
    width = int(geometry.get("width") or 0)
    height = int(geometry.get("height") or 0)
    offscreen_or_minimized = left <= -30000 or top <= -30000 or width <= 200 or height <= 80
    return reason in {"window_too_small_for_safe_send", "send_geometry_invalid"} and offscreen_or_minimized


def recover_send_window_guard(hwnd: int, *, max_attempts: int = 1) -> dict[str, Any]:
    guard = basic_send_window_guard(hwnd)
    if guard.get("ok"):
        return guard
    reason = str(guard.get("reason") or "")
    if not send_window_guard_can_recover_by_activation(guard):
        return guard
    attempts = max(0, int(max_attempts))
    if attempts <= 0:
        return guard
    last_guard = guard
    for attempt in range(1, attempts + 1):
        activate_window(hwnd)
        time.sleep(random.uniform(0.06, 0.14))
        retry_guard = basic_send_window_guard(hwnd)
        if retry_guard.get("ok"):
            return {
                **retry_guard,
                "focus_recovered": True,
                "focus_recovery_attempts": attempt,
                "focus_recovery_from": reason,
            }
        last_guard = retry_guard
    return {
        **last_guard,
        "focus_recovered": False,
        "focus_recovery_attempts": attempts,
        "focus_recovery_from": reason,
    }


def paste_text_in_chunks_with_humanized_pacing(text: str, settings: dict[str, Any]) -> dict[str, Any]:
    chunks = humanized_chunk_text(
        text,
        min_chars=int(settings.get("chunk_min_chars") or DEFAULT_HUMANIZED_TYPING_CHUNK_MIN_CHARS),
        max_chars=int(settings.get("chunk_max_chars") or DEFAULT_HUMANIZED_TYPING_CHUNK_MAX_CHARS),
    )
    if not chunks:
        return {"ok": True, "method": "clipboard_chunks", "chunks": 0, "typo_count": 0}
    typed_chars = 0
    typo_count = 0
    micro_every = int(settings.get("micro_pause_every_chars") or 0)
    micro_bucket = 0
    for chunk in chunks:
        paste_text_once(chunk)
        typed_chars += len(chunk)
        delay_low, delay_high = typed_text_delay_ms(chunk, settings)
        humanized_sleep_ms(delay_low, delay_high)
        if maybe_humanized_typo_allowed(settings, typo_count=typo_count, text=text):
            typo = choose_humanized_typo_char()
            paste_text_once(typo)
            humanized_sleep_ms(40, 120)
            key_press(win32con.VK_BACK)
            typo_count += 1
            humanized_sleep_ms(50, 130)
        if micro_every > 0:
            current_bucket = typed_chars // micro_every
            if current_bucket > micro_bucket:
                micro_bucket = current_bucket
                humanized_sleep_ms(
                    int(settings.get("micro_pause_min_ms") or DEFAULT_HUMANIZED_TYPING_MICRO_PAUSE_MIN_MS),
                    int(settings.get("micro_pause_max_ms") or DEFAULT_HUMANIZED_TYPING_MICRO_PAUSE_MAX_MS),
                )
    return {
        "ok": True,
        "method": "clipboard_chunks",
        "chunks": len(chunks),
        "typo_count": typo_count,
        "typed_chars": typed_chars,
    }


def paste_text_with_confirmation(
    hwnd: int,
    text: str,
    *,
    points: dict[str, Any],
    geometry: dict[str, Any],
    artifact_dir: str | None = None,
    settings: dict[str, Any] | None = None,
    before_input_region_seed: dict[str, Any] | None = None,
) -> dict[str, Any]:
    timing: dict[str, Any] = {}
    ocr_trace_token = _ocr_trace_start()
    paste_started = _sidecar_timing_start(timing, "paste_text_with_confirmation")

    def finish(payload: dict[str, Any]) -> dict[str, Any]:
        _sidecar_timing_finish(timing, "paste_text_with_confirmation", paste_started)
        _sidecar_timing_merge_ocr_trace(timing, "paste_text_with_confirmation", _ocr_trace_finish(ocr_trace_token))
        payload["timing"] = dict(timing)
        return payload

    input_x = int(points["input_point"][0])
    input_y = int(points["input_point"][1])
    probe_tokens = message_probe_tokens(text)
    probe_token = probe_tokens[0] if probe_tokens else ""
    settings = settings or adapt_humanized_input_settings(humanized_input_settings(), text)
    attempts = [
        (input_x, input_y, "human"),
        (max(session_split_x(int(geometry.get("width") or 0)) + 24, input_x - 150), max(int(geometry.get("height") * 0.80), input_y - 18), "client"),
        (max(session_split_x(int(geometry.get("width") or 0)) + 36, input_x - 220), max(int(geometry.get("height") * 0.82), input_y - 8), "client"),
    ]
    confirm_attempts = send_input_confirm_attempt_count(len(attempts))
    allow_copyback = env_flag("WECHAT_WIN32_OCR_INPUT_COPYBACK_CONFIRM", default=False)
    fast_visual_confirm = env_flag(
        "WECHAT_WIN32_OCR_INPUT_FAST_VISUAL_CONFIRM",
        default=DEFAULT_INPUT_FAST_VISUAL_CONFIRM,
    )
    attempts = attempts[:confirm_attempts]
    input_method = "clipboard_chunks"
    last_input_result: dict[str, Any] | None = None
    last_input_region: dict[str, Any] = {}
    if settings.get("enabled"):
        requested = normalize_humanized_input_method(str(settings.get("method") or "auto"))
        if requested in {"sendinput_unicode", "clipboard_chunks", "clipboard_once"}:
            input_method = requested
        else:
            # Guarded-click mode is Win32-centric; prefer chunked pacing here.
            input_method = "clipboard_chunks"
    for attempt, (x, y, mode) in enumerate(attempts, start=1):
        timing["attempts_observed"] = attempt
        activate_started = _sidecar_timing_start(timing, "activate_input_window")
        activate_window(hwnd)
        time.sleep(random.uniform(0.08, 0.18))
        _sidecar_timing_finish(timing, "activate_input_window", activate_started)
        focus_guard_started = _sidecar_timing_start(timing, "focus_guard_before_input")
        focus_guard = recover_send_window_guard(hwnd, max_attempts=1)
        _sidecar_timing_finish(timing, "focus_guard_before_input", focus_guard_started)
        if not focus_guard.get("ok"):
            return finish({
                "ok": False,
                "reason": "send_focus_guard_failed_before_input",
                "probe_token": probe_token,
                "probe_tokens": probe_tokens,
                "attempts": attempt,
                "copyback_enabled": allow_copyback,
                "input_region": last_input_region,
                "input_mode": input_method,
                "input_result": last_input_result,
                "window_guard": focus_guard,
            })
        try:
            seed_region = before_input_region_seed.get("input_region") if isinstance(before_input_region_seed, dict) else None
            if attempt == 1 and isinstance(seed_region, dict) and seed_region:
                timing["before_ocr_seed_reused"] = True
                timing["before_ocr_seed_age_seconds"] = before_input_region_seed.get("age_seconds")
                timing["before_ocr_source"] = "pre_send_guard_seed"
                before_input_region = dict(seed_region)
            else:
                timing["before_ocr_seed_reused"] = False
                before_capture_started = _sidecar_timing_start(timing, "before_capture")
                before_screenshot, _before_path = capture_wechat(
                    hwnd,
                    artifact_dir=artifact_dir,
                    label=f"send_input_before_{attempt}",
                )
                _sidecar_timing_finish(timing, "before_capture", before_capture_started)
                before_ocr_started = _sidecar_timing_start(timing, "before_ocr")
                before_ocr_items, _before_ocr_source = run_ocr_for_input_region_probe(
                    before_screenshot,
                    geometry=geometry,
                    timing=timing,
                    prefix="before_ocr",
                    purpose="input_before_draft_check",
                    roi_purpose="input_before_draft_check_roi",
                )
                _sidecar_timing_finish(timing, "before_ocr", before_ocr_started)
                before_region_started = _sidecar_timing_start(timing, "before_region")
                before_input_region = input_text_region_state(before_screenshot, before_ocr_items, geometry=geometry)
                _sidecar_timing_finish(timing, "before_region", before_region_started)
        except Exception as exc:
            before_input_region = {
                "has_visible_text": True,
                "reason": "input_region_before_probe_failed",
                "error": repr(exc),
            }
        clear_draft_started = _sidecar_timing_start(timing, "clear_draft")
        clear_result = clear_existing_input_draft(
            hwnd,
            points=points,
            geometry=geometry,
            before_state=before_input_region,
            artifact_dir=artifact_dir,
            attempt=attempt,
        )
        _sidecar_timing_finish(timing, "clear_draft", clear_draft_started)
        if not clear_result.get("ok"):
            return finish({
                "ok": False,
                "reason": "input_region_not_clear_before_type",
                "probe_token": probe_token,
                "probe_tokens": probe_tokens,
                "attempts": attempt,
                "copyback_enabled": allow_copyback,
                "input_region": before_input_region,
                "clear_result": clear_result,
                "input_mode": input_method,
                "input_result": last_input_result,
            })
        before_input_region = clear_result.get("after") or before_input_region
        click_x, click_y = jitter_input_click_point(int(x), int(y), geometry)
        input_click_started = _sidecar_timing_start(timing, "input_click")
        if mode == "human":
            human_client_click(hwnd, click_x, click_y)
        else:
            client_click(hwnd, click_x, click_y)
        time.sleep(random.uniform(0.12, 0.28))
        _sidecar_timing_finish(timing, "input_click", input_click_started)
        focus_guard_started = _sidecar_timing_start(timing, "focus_guard_after_input_click")
        focus_guard = recover_send_window_guard(hwnd, max_attempts=1)
        _sidecar_timing_finish(timing, "focus_guard_after_input_click", focus_guard_started)
        if not focus_guard.get("ok"):
            return finish({
                "ok": False,
                "reason": "send_focus_guard_failed_after_input_click",
                "probe_token": probe_token,
                "probe_tokens": probe_tokens,
                "attempts": attempt,
                "copyback_enabled": allow_copyback,
                "input_region": last_input_region,
                "input_mode": input_method,
                "input_result": last_input_result,
                "window_guard": focus_guard,
            })
        input_result: dict[str, Any]
        input_operation_started = _sidecar_timing_start(timing, "input_operation")
        if settings.get("enabled") and input_method == "sendinput_unicode":
            input_result = type_text_with_sendinput_unicode(
                text,
                settings,
                window_guard_func=lambda hwnd=hwnd: basic_send_window_guard(hwnd),
            )
        elif settings.get("enabled") and input_method == "clipboard_chunks":
            focus_guard_started = _sidecar_timing_start(timing, "focus_guard_before_clipboard_input")
            focus_guard = recover_send_window_guard(hwnd, max_attempts=1)
            _sidecar_timing_finish(timing, "focus_guard_before_clipboard_input", focus_guard_started)
            if not focus_guard.get("ok"):
                _sidecar_timing_finish(timing, "input_operation", input_operation_started)
                return finish({
                    "ok": False,
                    "reason": "send_focus_guard_failed_before_clipboard_input",
                    "probe_token": probe_token,
                    "probe_tokens": probe_tokens,
                    "attempts": attempt,
                    "copyback_enabled": allow_copyback,
                    "input_region": last_input_region,
                    "input_mode": input_method,
                    "input_result": last_input_result,
                    "window_guard": focus_guard,
                })
            input_result = paste_text_in_chunks_with_humanized_pacing(text, settings)
        else:
            focus_guard_started = _sidecar_timing_start(timing, "focus_guard_before_clipboard_input")
            focus_guard = recover_send_window_guard(hwnd, max_attempts=1)
            _sidecar_timing_finish(timing, "focus_guard_before_clipboard_input", focus_guard_started)
            if not focus_guard.get("ok"):
                _sidecar_timing_finish(timing, "input_operation", input_operation_started)
                return finish({
                    "ok": False,
                    "reason": "send_focus_guard_failed_before_clipboard_input",
                    "probe_token": probe_token,
                    "probe_tokens": probe_tokens,
                    "attempts": attempt,
                    "copyback_enabled": allow_copyback,
                    "input_region": last_input_region,
                    "input_mode": input_method,
                    "input_result": last_input_result,
                    "window_guard": focus_guard,
                })
            paste_text_once(text)
            time.sleep(random.uniform(0.18, 0.42))
            input_result = {"ok": True, "method": "clipboard_once"}
        _sidecar_timing_finish(timing, "input_operation", input_operation_started)
        last_input_result = input_result
        if not input_result.get("ok"):
            if non_retryable_input_failure(input_result):
                return finish({
                    "ok": False,
                    "reason": "input_aborted_without_retry",
                    "probe_token": probe_token,
                    "probe_tokens": probe_tokens,
                    "attempts": attempt,
                    "copyback_enabled": allow_copyback,
                    "input_region": last_input_region,
                    "input_mode": input_method,
                    "input_result": last_input_result,
                })
            continue
        try:
            after_capture_started = _sidecar_timing_start(timing, "after_capture")
            screenshot, _path = capture_wechat(hwnd, artifact_dir=artifact_dir, label=f"send_input_probe_{attempt}")
            _sidecar_timing_finish(timing, "after_capture", after_capture_started)
        except Exception as exc:
            return finish({
                "ok": False,
                "reason": "window_lost_after_input",
                "error": repr(exc),
                "probe_token": probe_token,
                "probe_tokens": probe_tokens,
                "attempts": attempt,
                "copyback_enabled": allow_copyback,
                "input_mode": input_method,
                "input_result": last_input_result,
            })
        fast_region_started = _sidecar_timing_start(timing, "fast_region")
        fast_after_region = input_text_region_state(screenshot, [], geometry=geometry)
        _sidecar_timing_finish(timing, "fast_region", fast_region_started)
        fast_visual_confirm_started = _sidecar_timing_start(timing, "fast_visual_confirm")
        visual_confirm_fast = input_region_visual_delta_confirms(before_input_region, fast_after_region, input_result)
        _sidecar_timing_finish(timing, "fast_visual_confirm", fast_visual_confirm_started)
        if fast_visual_confirm and visual_confirm_fast.get("ok"):
            return finish({
                "ok": True,
                "attempt": attempt,
                "click_mode": mode,
                "point": [click_x, click_y],
                "probe_token": probe_token,
                "probe_tokens": probe_tokens,
                "confirmed_by": "input_area_visual_delta_fast",
                "input_visual_confirm": visual_confirm_fast,
                "input_clear": clear_result,
                "input_mode": input_method,
                "input_result": input_result,
            })
        after_ocr_started = _sidecar_timing_start(timing, "after_ocr")
        ocr_items, after_ocr_source = run_ocr_for_input_confirmation(
            screenshot,
            geometry=geometry,
            timing=timing,
            prefix="after_ocr",
        )
        _sidecar_timing_finish(timing, "after_ocr", after_ocr_started)
        if input_area_contains_any_token(ocr_items, geometry=geometry, tokens=probe_tokens):
            return finish({
                "ok": True,
                "attempt": attempt,
                "click_mode": mode,
                "point": [click_x, click_y],
                "probe_token": probe_token,
                "probe_tokens": probe_tokens,
                "confirmed_by": "ocr_input_area",
                "input_clear": clear_result,
                "input_mode": input_method,
                "input_result": input_result,
            })
        after_region_started = _sidecar_timing_start(timing, "after_region")
        last_input_region = input_text_region_state(screenshot, ocr_items, geometry=geometry)
        _sidecar_timing_finish(timing, "after_region", after_region_started)
        visual_confirm_started = _sidecar_timing_start(timing, "visual_confirm")
        visual_confirm = input_region_visual_delta_confirms(before_input_region, last_input_region, input_result)
        _sidecar_timing_finish(timing, "visual_confirm", visual_confirm_started)
        if visual_confirm.get("ok"):
            return finish({
                "ok": True,
                "attempt": attempt,
                "click_mode": mode,
                "point": [click_x, click_y],
                "probe_token": probe_token,
                "probe_tokens": probe_tokens,
                "confirmed_by": "input_area_visual_delta",
                "input_visual_confirm": visual_confirm,
                "input_clear": clear_result,
                "input_mode": input_method,
                "input_result": input_result,
            })
        if after_ocr_source == "roi":
            fallback_started = _sidecar_timing_start(timing, "after_ocr_full_fallback")
            full_ocr_items = run_ocr_traced(
                screenshot,
                "input_after_token_confirm_fallback_full",
                source="paste_text_with_confirmation",
            )
            _sidecar_timing_finish(timing, "after_ocr_full_fallback", fallback_started)
            timing["after_ocr_source"] = "roi_full_fallback"
            if input_area_contains_any_token(full_ocr_items, geometry=geometry, tokens=probe_tokens):
                return finish({
                    "ok": True,
                    "attempt": attempt,
                    "click_mode": mode,
                    "point": [click_x, click_y],
                    "probe_token": probe_token,
                    "probe_tokens": probe_tokens,
                    "confirmed_by": "ocr_input_area",
                    "input_clear": clear_result,
                    "input_mode": input_method,
                    "input_result": input_result,
                })
            full_after_region_started = _sidecar_timing_start(timing, "after_region_full_fallback")
            full_after_region = input_text_region_state(screenshot, full_ocr_items, geometry=geometry)
            _sidecar_timing_finish(timing, "after_region_full_fallback", full_after_region_started)
            full_visual_confirm_started = _sidecar_timing_start(timing, "visual_confirm_full_fallback")
            full_visual_confirm = input_region_visual_delta_confirms(before_input_region, full_after_region, input_result)
            _sidecar_timing_finish(timing, "visual_confirm_full_fallback", full_visual_confirm_started)
            if full_visual_confirm.get("ok"):
                return finish({
                    "ok": True,
                    "attempt": attempt,
                    "click_mode": mode,
                    "point": [click_x, click_y],
                    "probe_token": probe_token,
                    "probe_tokens": probe_tokens,
                    "confirmed_by": "input_area_visual_delta",
                    "input_visual_confirm": full_visual_confirm,
                    "input_clear": clear_result,
                    "input_mode": input_method,
                    "input_result": input_result,
                })
            last_input_region = full_after_region
        if allow_copyback:
            copyback_confirm_started = _sidecar_timing_start(timing, "copyback_confirm")
            clipboard_confirm = confirm_input_token_via_clipboard(probe_tokens)
            _sidecar_timing_finish(timing, "copyback_confirm", copyback_confirm_started)
            if clipboard_confirm.get("ok"):
                return finish({
                    "ok": True,
                    "attempt": attempt,
                    "click_mode": mode,
                    "point": [click_x, click_y],
                    "probe_token": probe_token,
                    "probe_tokens": probe_tokens,
                    "confirmed_by": "clipboard_copyback",
                    "clipboard_confirm": clipboard_confirm,
                    "input_clear": clear_result,
                    "input_mode": input_method,
                    "input_result": input_result,
                })
        if last_input_region.get("has_visible_text"):
            break
        retry_pause_started = _sidecar_timing_start(timing, "retry_pause")
        time.sleep(random.uniform(0.16, 0.34))
        _sidecar_timing_finish(timing, "retry_pause", retry_pause_started)
    return finish({
        "ok": False,
        "reason": "input_token_not_detected_after_paste",
        "probe_token": probe_token,
        "probe_tokens": probe_tokens,
        "attempts": len(attempts),
        "copyback_enabled": allow_copyback,
        "input_region": last_input_region,
        "input_mode": input_method,
        "input_result": last_input_result,
    })


def send_input_confirm_attempt_count(total_attempts: int) -> int:
    return win32_ocr_env.send_input_confirm_attempt_count(total_attempts)


def _probe_tokens_normalized(tokens: list[str] | str) -> list[str]:
    raw_tokens = tokens if isinstance(tokens, list) else [str(tokens or "")]
    normalized: list[str] = []
    for token in raw_tokens:
        compact = re.sub(r"\s+", "", str(token or ""))
        if len(compact) < 2:
            continue
        if compact not in normalized:
            normalized.append(compact)
    return normalized


def _clipboard_token_matches(copied_text: str, normalized_tokens: list[str]) -> tuple[bool, str]:
    compact = re.sub(r"\s+", "", str(copied_text or ""))
    if len(compact) < 3:
        return False, "clipboard_copyback_too_short"
    for token in normalized_tokens:
        if token in compact or compact in token:
            return True, "clipboard_copyback_token_match"
    return False, "clipboard_copyback_token_mismatch"


def confirm_input_token_via_clipboard(probe_tokens: list[str] | str) -> dict[str, Any]:
    normalized_tokens = _probe_tokens_normalized(probe_tokens)
    if not normalized_tokens:
        return {"ok": False, "reason": "empty_probe_token"}
    try:
        # Low-disturbance copyback probe: do not select-all to avoid visible
        # global selection artifacts when focus drifts.
        hotkey(win32con.VK_CONTROL, ord("C"))
        humanized_action_sleep(60, 110)
        copied = str(clipboard_read() or "")
    except Exception as exc:
        return {"ok": False, "reason": "clipboard_copyback_failed", "error": repr(exc)}
    matched, reason = _clipboard_token_matches(copied, normalized_tokens)
    if matched:
        return {
            "ok": True,
            "reason": reason,
            "mode": "copy",
            "captured_preview": copied[:80],
        }
    strong_confirm = env_flag(
        "WECHAT_WIN32_OCR_INPUT_COPYBACK_STRONG_CONFIRM",
        default=DEFAULT_INPUT_COPYBACK_STRONG_CONFIRM,
    )
    if strong_confirm:
        try:
            hotkey(win32con.VK_CONTROL, ord("A"))
            humanized_action_sleep(45, 90)
            hotkey(win32con.VK_CONTROL, ord("C"))
            humanized_action_sleep(60, 120)
            copied_all = str(clipboard_read() or "")
            matched_all, reason_all = _clipboard_token_matches(copied_all, normalized_tokens)
            if matched_all:
                return {
                    "ok": True,
                    "reason": reason_all,
                    "mode": "select_all_copy",
                    "captured_preview": copied_all[:80],
                }
        except Exception as exc:
            return {"ok": False, "reason": "clipboard_copyback_failed", "error": repr(exc)}
    return {
        "ok": False,
        "reason": reason,
        "captured_preview": copied[:80],
    }


def safe_send_trigger(
    hwnd: int,
    *,
    trigger_mode: str,
    send_point: tuple[int, int] | None = None,
    settings: dict[str, Any] | None = None,
    focus_guard_func: Any | None = None,
) -> dict[str, Any]:
    active_settings = settings or {}
    if active_settings.get("enabled"):
        humanized_sleep_ms(
            int(active_settings.get("send_trigger_delay_min_ms") or DEFAULT_HUMANIZED_SEND_TRIGGER_DELAY_MIN_MS),
            int(active_settings.get("send_trigger_delay_max_ms") or DEFAULT_HUMANIZED_SEND_TRIGGER_DELAY_MAX_MS),
        )
    guard = focus_guard_func() if focus_guard_func is not None else recover_send_window_guard(hwnd, max_attempts=1)
    if not guard.get("ok"):
        return {
            "ok": False,
            "reason": "send_focus_guard_failed_before_trigger",
            "error": "WeChat lost foreground focus before send trigger; abort without retrying.",
            "window_guard": guard,
            "send_trigger_mode": trigger_mode,
        }
    mode = normalize_send_trigger_mode(trigger_mode)
    if mode in {"enter_only", "enter_then_click"}:
        ensure_left_button_released()
        coordinate_rpa_action(
            "send_trigger_enter",
            metadata={"hwnd": int(hwnd or 0), "key": int(win32con.VK_RETURN), "trigger_mode": mode},
        )
        win32api.keybd_event(win32con.VK_RETURN, 0, 0, 0)
        humanized_action_sleep(54, 145)
        win32api.keybd_event(win32con.VK_RETURN, 0, win32con.KEYEVENTF_KEYUP, 0)
        if active_settings.get("enabled"):
            humanized_sleep_ms(
                int(active_settings.get("send_after_trigger_delay_min_ms") or DEFAULT_HUMANIZED_SEND_AFTER_TRIGGER_DELAY_MIN_MS),
                int(active_settings.get("send_after_trigger_delay_max_ms") or DEFAULT_HUMANIZED_SEND_AFTER_TRIGGER_DELAY_MAX_MS),
            )
        return {"ok": True, "method": "keyboard_enter", "send_trigger_mode": mode, "window_guard": guard}
    if mode == "click_only":
        if send_point is None:
            return {"ok": False, "reason": "send_click_point_missing", "send_trigger_mode": mode, "window_guard": guard}
        click_guard = focus_guard_func() if focus_guard_func is not None else recover_send_window_guard(hwnd, max_attempts=1)
        if not click_guard.get("ok"):
            return {
                "ok": False,
                "reason": "send_focus_guard_failed_before_click_trigger",
                "error": "WeChat lost foreground focus before clicking send; abort without retrying.",
                "window_guard": click_guard,
                "send_trigger_mode": mode,
            }
        human_client_click(hwnd, int(send_point[0]), int(send_point[1]))
        if active_settings.get("enabled"):
            humanized_sleep_ms(
                int(active_settings.get("send_after_trigger_delay_min_ms") or DEFAULT_HUMANIZED_SEND_AFTER_TRIGGER_DELAY_MIN_MS),
                int(active_settings.get("send_after_trigger_delay_max_ms") or DEFAULT_HUMANIZED_SEND_AFTER_TRIGGER_DELAY_MAX_MS),
            )
        return {"ok": True, "method": "human_click_send", "send_trigger_mode": mode, "window_guard": click_guard}
    return {"ok": False, "reason": "unsupported_send_trigger_mode", "send_trigger_mode": mode, "window_guard": guard}


def send_with_guarded_clicks(
    hwnd: int,
    text: str,
    *,
    points: dict[str, Any],
    geometry: dict[str, Any],
    allow_unconfirmed_paste: bool = False,
    artifact_dir: str | None = None,
    settings: dict[str, Any] | None = None,
    before_input_region_seed: dict[str, Any] | None = None,
) -> dict[str, Any]:
    # WeChat 4.1.x keeps the attachment toolbar near the bottom. Paste first
    # and confirm OCR can see the token in the input area before sending.
    timing: dict[str, Any] = {}

    def finish(payload: dict[str, Any]) -> dict[str, Any]:
        payload["timing"] = dict(timing)
        return payload

    send_x = int(points["send_point"][0])
    send_y = int(points["send_point"][1])
    send_click_x, send_click_y = jitter_send_click_point(send_x, send_y, geometry)
    settings = settings or adapt_humanized_input_settings(humanized_input_settings(), text)
    if settings.get("enabled"):
        humanized_sleep_ms(
            int(settings.get("send_pre_delay_min_ms") or DEFAULT_HUMANIZED_SEND_PRE_DELAY_MIN_MS),
            int(settings.get("send_pre_delay_max_ms") or DEFAULT_HUMANIZED_SEND_PRE_DELAY_MAX_MS),
        )
    input_focus_started = _sidecar_timing_start(timing, "input_focus")
    typing_started = _sidecar_timing_start(timing, "typing")
    paste_result = paste_text_with_confirmation(
        hwnd,
        text,
        points=points,
        geometry=geometry,
        artifact_dir=artifact_dir,
        settings=settings,
        before_input_region_seed=before_input_region_seed,
    )
    _sidecar_timing_finish(timing, "typing", typing_started)
    _sidecar_timing_finish(timing, "input_focus", input_focus_started)
    if isinstance(paste_result.get("timing"), dict):
        _sidecar_timing_merge_prefixed(timing, "paste", paste_result["timing"])
    if not paste_result.get("ok"):
        if allow_unconfirmed_paste and str(paste_result.get("reason") or "") == "input_token_not_detected_after_paste":
            paste_result = {
                **paste_result,
                "ok": True,
                "degraded": True,
                "degraded_reason": "blind_send_unconfirmed_input_allowed",
            }
        else:
            return finish({
                "ok": False,
                "reason": "paste_not_confirmed",
                "error": "Could not confirm pasted text in WeChat input box before send.",
                "paste": paste_result,
            })
    if settings.get("enabled"):
        humanized_sleep_ms(
            int(settings.get("send_post_input_delay_min_ms") or DEFAULT_HUMANIZED_SEND_POST_INPUT_DELAY_MIN_MS),
            int(settings.get("send_post_input_delay_max_ms") or DEFAULT_HUMANIZED_SEND_POST_INPUT_DELAY_MAX_MS),
        )
    focus_guard = recover_send_window_guard(hwnd, max_attempts=1)
    if not focus_guard.get("ok"):
        return finish({
            "ok": False,
            "reason": "send_focus_guard_failed_before_trigger",
            "error": "WeChat lost foreground focus before send trigger; abort without retrying.",
            "paste": paste_result,
            "window_guard": focus_guard,
        })
    input_refocus = {
        "skipped": True,
        "reason": "input_already_confirmed_before_send_trigger",
    }
    trigger_mode = normalize_send_trigger_mode(os.getenv("WECHAT_WIN32_OCR_SEND_TRIGGER_MODE"))
    send_trigger_started = _sidecar_timing_start(timing, "send_trigger")
    trigger_result = safe_send_trigger(
        hwnd,
        trigger_mode=trigger_mode,
        send_point=(send_click_x, send_click_y),
        settings=settings,
        focus_guard_func=lambda hwnd=hwnd: recover_send_window_guard(hwnd, max_attempts=1),
    )
    _sidecar_timing_finish(timing, "send_trigger", send_trigger_started)
    if not trigger_result.get("ok"):
        return finish({
            "ok": False,
            "reason": str(trigger_result.get("reason") or "send_trigger_failed"),
            "error": str(trigger_result.get("error") or "Could not safely trigger WeChat send."),
            "paste": paste_result,
            "window_guard": trigger_result.get("window_guard") if isinstance(trigger_result.get("window_guard"), dict) else focus_guard,
            "trigger": trigger_result,
        })
    paste_method = str(paste_result.get("input_mode") or paste_result.get("method") or "clipboard_once")
    return finish({
        "ok": True,
        "method": f"win32.human_click_input+{paste_method}+send_trigger:{trigger_mode}",
        "input_point": [int(points["input_point"][0]), int(points["input_point"][1])],
        "send_point": [send_click_x, send_click_y],
        "paste": paste_result,
        "send_trigger_mode": trigger_mode,
        "send_trigger": trigger_result,
        "input_refocus": input_refocus,
        "degraded": bool(paste_result.get("degraded")),
        "humanized_input": settings,
    })


def send_with_uia_controls(
    hwnd: int,
    text: str,
    *,
    geometry: dict[str, Any],
    settings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    try:
        import uiautomation as auto  # type: ignore
    except Exception as exc:
        return {"ok": False, "reason": "uiautomation_unavailable", "error": repr(exc)}

    try:
        root = auto.ControlFromHandle(hwnd)
        controls = collect_uia_controls(root, max_depth=8, max_count=900)
        edit = select_uia_edit_control(controls, geometry)
        send_button = select_uia_send_button(controls, geometry)
        if edit is None:
            return {"ok": False, "reason": "uia_edit_not_found", "control_count": len(controls)}
        if send_button is None:
            return {"ok": False, "reason": "uia_send_button_not_found", "control_count": len(controls)}

        edit.SetFocus()
        humanized_action_sleep(80, 160)
        settings = settings or adapt_humanized_input_settings(humanized_input_settings(), text)
        if settings.get("enabled"):
            humanized_sleep_ms(
                int(settings.get("send_pre_delay_min_ms") or DEFAULT_HUMANIZED_SEND_PRE_DELAY_MIN_MS),
                int(settings.get("send_pre_delay_max_ms") or DEFAULT_HUMANIZED_SEND_PRE_DELAY_MAX_MS),
            )
        pattern_result = set_uia_control_value(auto, edit, text, settings=settings)
        if not pattern_result.get("ok"):
            return {**pattern_result, "control_count": len(controls)}
        if settings.get("enabled"):
            humanized_sleep_ms(
                int(settings.get("send_post_input_delay_min_ms") or DEFAULT_HUMANIZED_SEND_POST_INPUT_DELAY_MIN_MS),
                int(settings.get("send_post_input_delay_max_ms") or DEFAULT_HUMANIZED_SEND_POST_INPUT_DELAY_MAX_MS),
            )
        humanized_action_sleep(260, 760)
        humanized_action_sleep(120, 230)
        invoke_result = invoke_uia_button(auto, send_button)
        if not invoke_result.get("ok"):
            return {**invoke_result, "control_count": len(controls)}
        input_method = str(pattern_result.get("method") or "ValuePattern.SetValue")
        return {
            "ok": True,
            "method": f"uia.{input_method}+InvokePattern.Invoke",
            "control_count": len(controls),
            "edit": describe_uia_control(edit, geometry),
            "send_button": describe_uia_control(send_button, geometry),
            "humanized_input": settings,
            "input_result": pattern_result,
        }
    except Exception as exc:
        return {"ok": False, "reason": "uia_send_failed", "error": repr(exc)}


def inspect_uia_send_capability(hwnd: int, geometry: dict[str, Any]) -> dict[str, Any]:
    try:
        import uiautomation as auto  # type: ignore
    except Exception as exc:
        return {"ok": False, "reason": "uiautomation_unavailable", "error": repr(exc)}

    try:
        root = auto.ControlFromHandle(hwnd)
        controls = collect_uia_controls(root, max_depth=8, max_count=900)
        edit = select_uia_edit_control(controls, geometry)
        send_button = select_uia_send_button(controls, geometry)
        missing: list[str] = []
        if edit is None:
            missing.append("edit")
        if send_button is None:
            missing.append("send_button")
        return {
            "ok": not missing,
            "reason": "uia_controls_ready" if not missing else "uia_controls_missing",
            "missing": missing,
            "control_count": len(controls),
            "edit": describe_uia_control(edit, geometry) if edit is not None else None,
            "send_button": describe_uia_control(send_button, geometry) if send_button is not None else None,
        }
    except Exception as exc:
        return {"ok": False, "reason": "uia_inspect_failed", "error": repr(exc)}


def collect_uia_controls(root: Any, *, max_depth: int, max_count: int) -> list[Any]:
    controls: list[Any] = []
    queue: list[tuple[Any, int]] = [(root, 0)]
    while queue and len(controls) < max_count:
        control, depth = queue.pop(0)
        if depth:
            controls.append(control)
        if depth >= max_depth:
            continue
        try:
            children = list(control.GetChildren())
        except Exception:
            children = []
        for child in children:
            if len(controls) + len(queue) >= max_count:
                break
            queue.append((child, depth + 1))
    return controls


def select_uia_edit_control(controls: list[Any], geometry: dict[str, Any]) -> Any | None:
    candidates: list[tuple[float, Any]] = []
    for control in controls:
        if "edit" not in str(safe_uia_attr(control, "ControlTypeName")).lower():
            continue
        rect = uia_rect_to_dict(safe_uia_attr(control, "BoundingRectangle"))
        if not rect_in_input_area(rect, geometry):
            continue
        rel = relative_rect(rect, geometry)
        area = max(1, rel["width"]) * max(1, rel["height"])
        score = area + rel["bottom"] * 2
        candidates.append((score, control))
    return max(candidates, key=lambda item: item[0])[1] if candidates else None


def select_uia_send_button(controls: list[Any], geometry: dict[str, Any]) -> Any | None:
    candidates: list[tuple[float, Any]] = []
    for control in controls:
        control_type = str(safe_uia_attr(control, "ControlTypeName")).lower()
        name = normalize_ocr_text(safe_uia_attr(control, "Name"))
        if "button" not in control_type:
            continue
        if "发送" not in name and name.lower() not in {"send"}:
            continue
        rect = uia_rect_to_dict(safe_uia_attr(control, "BoundingRectangle"))
        if not rect_in_input_toolbar(rect, geometry):
            continue
        rel = relative_rect(rect, geometry)
        score = rel["right"] + rel["bottom"] * 2
        candidates.append((score, control))
    return max(candidates, key=lambda item: item[0])[1] if candidates else None


def set_uia_control_value_humanized(pattern: Any, text: str, settings: dict[str, Any]) -> dict[str, Any]:
    chunks = humanized_chunk_text(
        text,
        min_chars=int(settings.get("chunk_min_chars") or DEFAULT_HUMANIZED_TYPING_CHUNK_MIN_CHARS),
        max_chars=int(settings.get("chunk_max_chars") or DEFAULT_HUMANIZED_TYPING_CHUNK_MAX_CHARS),
    )
    typo_count = 0
    typed_chars = 0
    value = ""
    micro_every = int(settings.get("micro_pause_every_chars") or 0)
    micro_bucket = 0
    pattern.SetValue("")
    humanized_sleep_ms(40, 120)
    for chunk in chunks:
        value += chunk
        pattern.SetValue(value)
        typed_chars += len(chunk)
        low, high = typed_text_delay_ms(chunk, settings)
        humanized_sleep_ms(low, high)
        if maybe_humanized_typo_allowed(settings, typo_count=typo_count, text=text):
            typo = choose_humanized_typo_char()
            pattern.SetValue(value + typo)
            humanized_sleep_ms(35, 110)
            pattern.SetValue(value)
            typo_count += 1
            humanized_sleep_ms(50, 130)
        if micro_every > 0:
            current_bucket = typed_chars // micro_every
            if current_bucket > micro_bucket:
                micro_bucket = current_bucket
                humanized_sleep_ms(
                    int(settings.get("micro_pause_min_ms") or DEFAULT_HUMANIZED_TYPING_MICRO_PAUSE_MIN_MS),
                    int(settings.get("micro_pause_max_ms") or DEFAULT_HUMANIZED_TYPING_MICRO_PAUSE_MAX_MS),
                )
    return {
        "ok": True,
        "method": "ValuePattern.SetValue.humanized_chunks",
        "chunks": len(chunks),
        "typed_chars": typed_chars,
        "typo_count": typo_count,
    }


def set_uia_control_value(auto: Any, control: Any, text: str, *, settings: dict[str, Any] | None = None) -> dict[str, Any]:
    try:
        pattern = control.GetPattern(auto.PatternId.ValuePattern)
        active_settings = settings or humanized_input_settings()
        method = normalize_humanized_input_method(str(active_settings.get("method") or "auto"))
        if active_settings.get("enabled") and method in {"auto", "uia_chunks", "clipboard_chunks"}:
            return set_uia_control_value_humanized(pattern, text, active_settings)
        pattern.SetValue("")
        humanized_action_sleep(35, 80)
        pattern.SetValue(text)
        return {"ok": True, "method": "ValuePattern.SetValue"}
    except Exception as exc:
        return {"ok": False, "reason": "uia_value_pattern_failed", "error": repr(exc)}


def invoke_uia_button(auto: Any, control: Any) -> dict[str, Any]:
    try:
        pattern = control.GetPattern(auto.PatternId.InvokePattern)
        pattern.Invoke()
        return {"ok": True, "method": "InvokePattern.Invoke"}
    except Exception:
        try:
            control.Click()
            return {"ok": True, "method": "Control.Click"}
        except Exception as exc:
            return {"ok": False, "reason": "uia_invoke_failed", "error": repr(exc)}


def describe_uia_control(control: Any, geometry: dict[str, Any]) -> dict[str, Any]:
    rect = uia_rect_to_dict(safe_uia_attr(control, "BoundingRectangle"))
    return {
        "name": normalize_ocr_text(safe_uia_attr(control, "Name")),
        "control_type": str(safe_uia_attr(control, "ControlTypeName") or ""),
        "class_name": str(safe_uia_attr(control, "ClassName") or ""),
        "rect": relative_rect(rect, geometry),
    }


def safe_uia_attr(control: Any, name: str) -> Any:
    try:
        value = getattr(control, name)
        return value() if callable(value) and name in {"Name", "ClassName", "ControlTypeName"} else value
    except Exception:
        return ""


def uia_rect_to_dict(rect: Any) -> dict[str, int]:
    return {
        "left": int(getattr(rect, "left", getattr(rect, "Left", 0)) or 0),
        "top": int(getattr(rect, "top", getattr(rect, "Top", 0)) or 0),
        "right": int(getattr(rect, "right", getattr(rect, "Right", 0)) or 0),
        "bottom": int(getattr(rect, "bottom", getattr(rect, "Bottom", 0)) or 0),
    }


def relative_rect(rect: dict[str, int], geometry: dict[str, Any]) -> dict[str, int]:
    return win32_ocr_geometry.relative_rect(rect, geometry)


def rect_in_input_area(rect: dict[str, int], geometry: dict[str, Any]) -> bool:
    return win32_ocr_geometry.rect_in_input_area(rect, geometry)


def rect_in_input_toolbar(rect: dict[str, int], geometry: dict[str, Any]) -> bool:
    return win32_ocr_geometry.rect_in_input_toolbar(rect, geometry)


def session_name_matches(name: str, target: str, *, exact: bool) -> bool:
    return win32_ocr_text.session_name_matches(name, target, exact=exact)


def strip_session_time_suffix(name: str) -> str:
    return win32_ocr_text.strip_session_time_suffix(name)


def session_row_click_x(
    session: dict[str, Any],
    geometry: dict[str, Any],
    *,
    default_x: int,
) -> int:
    return win32_ocr_session_targeting.session_row_click_x(session, geometry, default_x=default_x)


def session_row_click_candidate_points(
    session: dict[str, Any],
    geometry: dict[str, Any],
    *,
    default_x: int,
    min_points: int = 10,
) -> list[tuple[int, int]]:
    """Return a spread of safe points inside one sidebar session row.

    A single text-center click leaks an obvious RPA fingerprint.  Keep the
    points inside the row, away from the unread badge, and let the final click
    jitter add a second small random offset.
    """
    return win32_ocr_session_targeting.session_row_click_candidate_points(
        session,
        geometry,
        default_x=default_x,
        min_points=min_points,
        random_module=random,
    )


def choose_session_row_click_point(
    session: dict[str, Any],
    geometry: dict[str, Any],
    *,
    default_x: int,
) -> tuple[int, int, dict[str, Any]]:
    return win32_ocr_session_targeting.choose_session_row_click_point(
        session,
        geometry,
        default_x=default_x,
        random_module=random,
    )


def activate_session_candidate(
    hwnd: int,
    session: dict[str, Any],
    *,
    target: str,
    exact: bool,
    geometry: dict[str, Any],
    default_click_x: int,
    artifact_dir: str | None = None,
) -> bool:
    timing: dict[str, Any] = {}
    activation_started = _sidecar_timing_start(timing, "activation")

    def finish(opened: bool) -> bool:
        global _LAST_SESSION_ACTIVATION_TIMING
        _sidecar_timing_finish(timing, "activation", activation_started)
        timing["opened"] = bool(opened)
        _LAST_SESSION_ACTIVATION_TIMING = dict(timing)
        return opened

    center_y = session.get("center_y")
    if center_y is None:
        timing["reason"] = "missing_center_y"
        return finish(False)
    choose_started = _sidecar_timing_start(timing, "activation_choose_click")
    click_x, click_y, _click_meta = choose_session_row_click_point(
        session,
        geometry,
        default_x=default_click_x,
    )
    _sidecar_timing_finish(timing, "activation_choose_click", choose_started)
    timing["activation_candidate_name"] = str(session.get("name") or "")
    if session_candidate_is_service_container_wrong_target(session, target):
        timing["reason"] = "service_container_candidate_wrong_target"
        timing["hard_stop"] = True
        return finish(False)
    # Use exactly one human-like click per candidate. If the active-title
    # guard cannot confirm the switch, stop this RPA attempt and let the
    # scheduler cool down/re-capture instead of probing the same row again.
    pre_click_wait_started = _sidecar_timing_start(timing, "activation_pre_click_wait")
    humanized_action_sleep(260, 720)
    _sidecar_timing_finish(timing, "activation_pre_click_wait", pre_click_wait_started)
    click_started = _sidecar_timing_start(timing, "activation_click")
    human_client_click(hwnd, click_x, click_y)
    _sidecar_timing_finish(timing, "activation_click", click_started)
    for attempt in range(target_switch_passive_confirm_attempts()):
        timing["activation_confirm_attempts_observed"] = attempt + 1
        if attempt == 0:
            confirm_wait_started = _sidecar_timing_start(timing, f"activation_confirm_{attempt + 1}_wait")
            humanized_action_sleep(320, 620)
            _sidecar_timing_finish(timing, f"activation_confirm_{attempt + 1}_wait", confirm_wait_started)
        else:
            # Passive re-read only. Some WeChat builds need a short render
            # settle after switching chats; repeated row clicks are not needed.
            confirm_wait_started = _sidecar_timing_start(timing, f"activation_confirm_{attempt + 1}_wait")
            humanized_action_sleep(180, 360)
            _sidecar_timing_finish(timing, f"activation_confirm_{attempt + 1}_wait", confirm_wait_started)
        confirm_started = _sidecar_timing_start(timing, f"activation_confirm_{attempt + 1}_validation")
        validation = validate_active_send_target(hwnd, target, exact=exact, artifact_dir=artifact_dir)
        _sidecar_timing_finish(timing, f"activation_confirm_{attempt + 1}_validation", confirm_started)
        _sidecar_timing_merge_validation(timing, f"activation_confirm_{attempt + 1}_validation", validation)
        if active_send_guard_is_strong(validation):
            timing["activation_confirmed_by_attempt"] = attempt + 1
            remember_target_switch_validation(
                hwnd=hwnd,
                target=target,
                exact=exact,
                session_key=str(session.get("session_key") or ""),
                validation=validation,
                geometry=geometry,
            )
            return finish(True)
        if target_switch_validation_is_hard_stop(validation):
            timing["reason"] = "hard_stop"
            return finish(False)
    timing["reason"] = "target_not_confirmed"
    return finish(False)


def session_matches_key(session: dict[str, Any], session_key: str) -> bool:
    expected = str(session_key or "").strip()
    if not expected:
        return False
    actual = str(session.get("session_key") or "").strip()
    return bool(actual and actual == expected)


def find_session_candidate_by_key(sessions: list[dict[str, Any]], session_key: str) -> dict[str, Any] | None:
    expected = str(session_key or "").strip()
    if not expected:
        return None
    for item in sessions:
        if isinstance(item, dict) and session_matches_key(item, expected):
            return item
    return None


def visible_session_name_is_unambiguous(
    sessions: list[dict[str, Any]],
    target: str,
    *,
    exact: bool,
) -> bool:
    matches = [
        item
        for item in sessions
        if isinstance(item, dict) and session_name_matches(str(item.get("name") or ""), target, exact=exact)
    ]
    return len(matches) == 1


def detect_session_subview_back_target(
    ocr_items: list[dict[str, Any]],
    image_size: tuple[int, int],
) -> dict[str, int] | None:
    width, height = image_size
    split_x = session_split_x(width)
    header_limit = chat_header_cutoff_y(height) + max(42, int(height * 0.06))
    for item in ocr_items:
        text = normalize_ocr_text(item.get("text"))
        if not text:
            continue
        if item["center_y"] > header_limit:
            continue
        if item["right"] > split_x + 40:
            continue
        compact = text.replace(" ", "")
        has_arrow = compact.startswith("<") or compact.startswith("〈") or compact.startswith("‹") or compact.startswith("＜")
        if not has_arrow:
            continue
        if not any(keyword in compact for keyword in ("服务号", "订阅号", "公众号")):
            continue
        return {
            "x": bounded_int(int(float(item.get("left") or 0)) + 10, default=108, minimum=70, maximum=170),
            "y": bounded_int(int(float(item.get("center_y") or 0)), default=124, minimum=86, maximum=220),
        }
    return None


def ensure_main_session_list(
    hwnd: int,
    *,
    artifact_dir: str | None = None,
    max_hops: int = 2,
) -> tuple[Any, list[dict[str, Any]]]:
    screenshot, _path = capture_wechat(hwnd, artifact_dir=artifact_dir, label="open_chat")
    ocr_items = run_ocr_traced(screenshot, "open_chat_main_list", source="ensure_main_session_list")
    hops = max(0, int(max_hops))
    for _ in range(hops):
        back_target = detect_session_subview_back_target(ocr_items, screenshot.size)
        if not back_target:
            break
        client_click(hwnd, int(back_target["x"]), int(back_target["y"]))
        humanized_action_sleep(280, 480)
        screenshot, _path = capture_wechat(hwnd, artifact_dir=artifact_dir, label="open_chat_main_list")
        ocr_items = run_ocr_traced(screenshot, "open_chat_main_list_after_back", source="ensure_main_session_list")
    return screenshot, ocr_items


def target_switch_surface_state(
    screenshot: Any,
    ocr_items: list[dict[str, Any]],
    *,
    geometry: dict[str, Any],
    screenshot_path: str = "",
    target: str = "",
) -> dict[str, Any]:
    if not ocr_items:
        blank_render = detect_blank_render(screenshot, ocr_items, geometry=geometry)
        if blank_render.get("detected"):
            return {
                "ok": False,
                "online": False,
                "reason": "blank_render",
                "state": "blank_render_detected",
                "geometry": geometry,
                "screenshot_path": screenshot_path,
                "ocr_count": 0,
                "render_probe": blank_render,
                "error": "WeChat render is blank; stop cross-chat switching before further RPA action.",
            }
        return {"ok": True, "online": True, "reason": "surface_no_ocr_not_blank", "ocr_count": 0}
    if quick_login_like(ocr_items, geometry=geometry):
        return {
            "ok": False,
            "online": False,
            "reason": "login_or_qr",
            "state": "login_window_detected",
            "geometry": geometry,
            "screenshot_path": screenshot_path,
            "ocr_count": len(ocr_items),
            "error": "WeChat quick-login view detected; stop cross-chat switching.",
        }
    blank_render = detect_blank_render(screenshot, ocr_items, geometry=geometry)
    if blank_render.get("detected"):
        return {
            "ok": False,
            "online": False,
            "reason": "blank_render",
            "state": "blank_render_detected",
            "geometry": geometry,
            "screenshot_path": screenshot_path,
            "ocr_count": len(ocr_items),
            "render_probe": blank_render,
            "error": "WeChat render is blank; stop cross-chat switching before further RPA action.",
        }
    auxiliary_shell = auxiliary_wechat_shell_like(ocr_items, geometry=geometry)
    if auxiliary_shell.get("detected"):
        return {
            "ok": False,
            "online": False,
            "reason": "auxiliary_shell_window",
            "state": "auxiliary_shell_window_detected",
            "geometry": geometry,
            "screenshot_path": screenshot_path,
            "ocr_count": len(ocr_items),
            "shell_probe": auxiliary_shell,
            "error": "Selected WeChat window looks like an auxiliary shell; stop cross-chat switching.",
        }
    blocking_reason = blocking_screen_reason(ocr_items)
    if blocking_reason:
        return {
            "ok": False,
            "online": False if blocking_reason in {"login_or_qr"} else True,
            "reason": blocking_reason,
            "state": "blocking_screen_detected",
            "geometry": geometry,
            "screenshot_path": screenshot_path,
            "ocr_count": len(ocr_items),
            "error": f"WeChat cross-chat switch guard found blocking screen: {blocking_reason}",
        }
    if target:
        service_probe = active_service_container_wrong_target(
            ocr_items,
            getattr(screenshot, "size", (0, 0)),
            target=target,
        )
        if service_probe.get("detected"):
            return {
                "ok": False,
                "online": True,
                "reason": "service_container_wrong_target",
                "state": "wrong_target_service_container_detected",
                "geometry": geometry,
                "screenshot_path": screenshot_path,
                "ocr_count": len(ocr_items),
                "service_container_probe": service_probe,
                "error": "WeChat is on a service-account container/page, not the requested chat; stop before further RPA action.",
            }
    return {"ok": True, "online": True, "reason": "surface_ready", "ocr_count": len(ocr_items)}


def target_switch_validation_is_hard_stop(validation: dict[str, Any] | None) -> bool:
    return win32_ocr_session_targeting.target_switch_validation_is_hard_stop(validation)


def target_ready_attempt_count(max_attempts: int | None) -> int:
    if max_attempts is not None:
        return max(1, int(max_attempts))
    return bounded_int(
        os.getenv("WECHAT_WIN32_OCR_TARGET_READY_MAX_ATTEMPTS"),
        default=DEFAULT_TARGET_READY_MAX_ATTEMPTS,
        minimum=1,
        maximum=3,
    )


def target_ready_switch_validation_cache_seconds() -> float:
    return bounded_float(
        os.getenv("WECHAT_WIN32_OCR_TARGET_READY_SWITCH_VALIDATION_CACHE_SECONDS"),
        default=DEFAULT_TARGET_READY_SWITCH_VALIDATION_CACHE_SECONDS,
        minimum=0.0,
        maximum=12.0,
    )


def target_ready_prevalidation_ocr_seed_seconds() -> float:
    return bounded_float(
        os.getenv("WECHAT_WIN32_OCR_TARGET_READY_PREVALIDATION_OCR_SEED_SECONDS"),
        default=DEFAULT_TARGET_READY_PREVALIDATION_OCR_SEED_SECONDS,
        minimum=0.0,
        maximum=5.0,
    )


def target_ready_geometry_cache_key(geometry: dict[str, Any] | None) -> tuple[int, int, int, int]:
    data = geometry if isinstance(geometry, dict) else {}
    return (
        int(data.get("left") or 0),
        int(data.get("top") or 0),
        int(data.get("width") or 0),
        int(data.get("height") or 0),
    )


def remember_target_switch_validation(
    *,
    hwnd: int,
    target: str,
    exact: bool,
    session_key: str,
    validation: dict[str, Any],
    geometry: dict[str, Any] | None = None,
) -> None:
    if not active_send_guard_is_strong(validation):
        return
    cached_geometry = (
        validation.get("geometry")
        if isinstance(validation.get("geometry"), dict)
        else (geometry if isinstance(geometry, dict) else get_window_geometry(hwnd))
    )
    _LAST_RPA_ACTION_STATE["target_ready_last_switch_validation"] = {
        "ts": time.monotonic(),
        "hwnd": int(hwnd or 0),
        "target": str(target or ""),
        "exact": bool(exact),
        "session_key": str(session_key or ""),
        "geometry_key": list(target_ready_geometry_cache_key(cached_geometry)),
        "validation": dict(validation),
    }


def consume_recent_target_switch_validation(
    *,
    hwnd: int,
    target: str,
    exact: bool,
    session_key: str,
    ttl_seconds: float | None = None,
) -> dict[str, Any] | None:
    cached = _LAST_RPA_ACTION_STATE.get("target_ready_last_switch_validation")
    if not isinstance(cached, dict):
        return None
    ttl = target_ready_switch_validation_cache_seconds() if ttl_seconds is None else max(0.0, float(ttl_seconds))
    if ttl <= 0:
        return None
    age = max(0.0, time.monotonic() - float(cached.get("ts") or 0.0))
    if age > ttl:
        return None
    if int(cached.get("hwnd") or 0) != int(hwnd or 0):
        return None
    if str(cached.get("target") or "") != str(target or ""):
        return None
    if bool(cached.get("exact")) != bool(exact):
        return None
    clean_session_key = str(session_key or "").strip()
    cached_session_key = str(cached.get("session_key") or "").strip()
    if clean_session_key and cached_session_key and cached_session_key != clean_session_key:
        return None
    validation = cached.get("validation")
    if not isinstance(validation, dict) or not active_send_guard_is_strong(validation):
        return None
    geometry = validation.get("geometry") if isinstance(validation.get("geometry"), dict) else {}
    cached_geometry_key = list(cached.get("geometry_key") or [])
    if list(target_ready_geometry_cache_key(geometry)) != cached_geometry_key:
        return None
    current_geometry_key = list(target_ready_geometry_cache_key(get_window_geometry(hwnd)))
    if current_geometry_key != cached_geometry_key:
        return None
    reused = dict(validation)
    reused["target_ready_reused_switch_validation"] = True
    reused["target_ready_reused_switch_validation_age_seconds"] = round(age, 4)
    return reused


def remember_target_ready_prevalidation_ocr_seed(
    *,
    hwnd: int,
    target: str,
    exact: bool,
    screenshot: Any,
    ocr_items: list[dict[str, Any]],
    geometry: dict[str, Any] | None,
    screenshot_path: str = "",
) -> None:
    global _TARGET_READY_PREVALIDATION_OCR_SEED
    if not ocr_items:
        return
    _TARGET_READY_PREVALIDATION_OCR_SEED = {
        "ts": time.monotonic(),
        "hwnd": int(hwnd or 0),
        "target": str(target or ""),
        "exact": bool(exact),
        "geometry_key": list(target_ready_geometry_cache_key(geometry)),
        "screenshot": screenshot,
        "ocr_items": list(ocr_items),
        "screenshot_path": str(screenshot_path or ""),
    }


def consume_target_ready_prevalidation_ocr_seed(
    *,
    hwnd: int,
    target: str,
    exact: bool,
    geometry: dict[str, Any] | None,
    ttl_seconds: float | None = None,
) -> dict[str, Any] | None:
    global _TARGET_READY_PREVALIDATION_OCR_SEED
    cached = _TARGET_READY_PREVALIDATION_OCR_SEED
    if not isinstance(cached, dict):
        return None
    _TARGET_READY_PREVALIDATION_OCR_SEED = {}
    ttl = target_ready_prevalidation_ocr_seed_seconds() if ttl_seconds is None else max(0.0, float(ttl_seconds))
    if ttl <= 0:
        return None
    age = max(0.0, time.monotonic() - float(cached.get("ts") or 0.0))
    if age > ttl:
        return None
    if int(cached.get("hwnd") or 0) != int(hwnd or 0):
        return None
    if str(cached.get("target") or "") != str(target or ""):
        return None
    if bool(cached.get("exact")) != bool(exact):
        return None
    cached_geometry_key = list(cached.get("geometry_key") or [])
    if list(target_ready_geometry_cache_key(geometry)) != cached_geometry_key:
        return None
    current_geometry_key = list(target_ready_geometry_cache_key(get_window_geometry(hwnd)))
    if current_geometry_key != cached_geometry_key:
        return None
    screenshot = cached.get("screenshot")
    ocr_items = cached.get("ocr_items")
    if screenshot is None or not isinstance(ocr_items, list) or not ocr_items:
        return None
    return {
        "screenshot": screenshot,
        "ocr_items": list(ocr_items),
        "screenshot_path": str(cached.get("screenshot_path") or ""),
        "age_seconds": round(age, 4),
    }


def target_search_fallback_enabled() -> bool:
    # The search/header region is a high-risk path for live WeChat RPA. Prefer
    # visible-session and unread-badge switching; enable only for diagnostics.
    return env_flag("WECHAT_WIN32_OCR_TARGET_SEARCH_FALLBACK", default=False)


def target_search_enter_fallback_enabled() -> bool:
    return env_flag("WECHAT_WIN32_OCR_TARGET_SEARCH_ENTER_FALLBACK", default=False)


def target_search_retry_after_search_enabled() -> bool:
    return env_flag("WECHAT_WIN32_OCR_TARGET_SEARCH_RETRY_AFTER_SEARCH", default=False)


def sidebar_search_focus_indicator_detected(screenshot: Any, geometry: dict[str, Any] | None = None) -> bool:
    if screenshot is None:
        return False
    try:
        image = screenshot.convert("RGB")
    except Exception:
        return False
    data = geometry if isinstance(geometry, dict) else {}
    width = int(data.get("width") or getattr(image, "width", 0) or 0)
    if width <= 0:
        return False
    split_x = session_split_x(width)
    left = 88
    top = 48
    right = min(max(160, split_x - 62), getattr(image, "width", width))
    bottom = min(88, getattr(image, "height", 0) or 88)
    if right <= left or bottom <= top:
        return False
    active_pixels = 0
    for y in range(top, bottom):
        for x in range(left, right):
            red, green, blue = image.getpixel((x, y))
            if green >= 105 and green - red >= 45 and green - blue >= 25:
                active_pixels += 1
                if active_pixels >= 80:
                    return True
    return False


def sidebar_search_state_detected(
    screenshot: Any,
    ocr_items: list[dict[str, Any]],
    *,
    geometry: dict[str, Any] | None = None,
) -> dict[str, Any]:
    texts = [normalize_ocr_text(item.get("text")) for item in ocr_items or [] if normalize_ocr_text(item.get("text"))]
    compact = "".join(texts)
    if "搜一搜" in compact:
        return {"detected": True, "reason": "wechat_global_search_page_text"}
    if sidebar_search_focus_indicator_detected(screenshot, geometry):
        return {"detected": True, "reason": "sidebar_search_focus_indicator"}
    return {"detected": False, "reason": ""}


def sidebar_search_query_text(
    ocr_items: list[dict[str, Any]],
    image_size: tuple[int, int],
    *,
    geometry: dict[str, Any] | None = None,
) -> str:
    width, _height = image_size
    data = geometry if isinstance(geometry, dict) else {}
    split_x = session_split_x(int(data.get("width") or width or 0))
    left = 96
    right = min(max(170, split_x - 72), int(width or split_x))
    top = 48
    bottom = 92
    parts: list[str] = []
    for item in sorted(ocr_items or [], key=lambda row: (float(row.get("center_y") or 0), float(row.get("left") or 0))):
        center_x = float(item.get("center_x") or 0)
        center_y = float(item.get("center_y") or 0)
        if not (left <= center_x <= right and top <= center_y <= bottom):
            continue
        text = normalize_ocr_text(item.get("text"))
        if not text:
            continue
        compact = re.sub(r"\s+", "", text)
        compact_lower = compact.lower()
        # Empty focused search boxes often OCR the magnifier icon plus
        # placeholder as "Q搜索"/"O搜索"/"0搜索". Treat these as placeholder
        # text, not stale query content.
        if compact_lower in {"搜索", "搜素", "q搜索", "o搜索", "0搜索", "q搜素", "o搜素", "0搜素"}:
            continue
        parts.append(text)
    return normalize_session_name("".join(parts))


def sidebar_search_query_matches(query_text: str, expected: str) -> bool:
    query = re.sub(r"\s+", "", normalize_session_name(str(query_text or ""))).strip().lower()
    target = re.sub(r"\s+", "", normalize_session_name(str(expected or ""))).strip().lower()
    return bool(query and target and query == target)


def dismiss_sidebar_search_state(
    hwnd: int,
    *,
    target_hint: str = "",
    geometry: dict[str, Any] | None = None,
    artifact_dir: str | None = None,
) -> dict[str, Any]:
    """Exit the sidebar search mode after a diagnostic/search fallback pass."""
    guard = basic_send_window_guard(hwnd)
    if not guard.get("ok"):
        return {"ok": False, "reason": "window_guard_failed_before_search_dismiss", "window_guard": guard}
    active_geometry = geometry if isinstance(geometry, dict) else get_window_geometry(hwnd)
    result: dict[str, Any] = {"ok": True, "method": "guarded_escape_search_dismiss", "attempts": 0}
    max_attempts = 2 if artifact_dir else 1
    last_search_state: dict[str, Any] = {"detected": False, "reason": ""}
    for attempt in range(1, max_attempts + 1):
        result["attempts"] = attempt
        humanized_action_sleep(360, 920)
        key_press(win32con.VK_ESCAPE)
        humanized_action_sleep(620, 1400)
        after_guard = basic_send_window_guard(hwnd)
        result["window_guard"] = after_guard
        if not after_guard.get("ok"):
            return {"ok": False, "reason": "window_guard_failed_after_search_dismiss", "window_guard": after_guard}
        if not artifact_dir:
            return result
        shot, shot_path = capture_wechat(hwnd, artifact_dir=artifact_dir, label="open_chat_search_dismiss_after_escape")
        items = run_ocr_traced(shot, "open_chat_search_dismiss_after_escape", source="open_chat")
        surface = target_switch_surface_state(
            shot,
            items,
            geometry=active_geometry,
            screenshot_path=shot_path,
            target=target_hint,
        )
        result["surface"] = surface
        result["ocr_count"] = len(items)
        result["screenshot_path"] = shot_path
        if not surface.get("ok"):
            return {
                **result,
                "ok": False,
                "reason": str(surface.get("reason") or "search_dismiss_surface_not_ok"),
            }
        last_search_state = sidebar_search_state_detected(shot, items, geometry=active_geometry)
        result["search_state"] = last_search_state
        if not last_search_state.get("detected"):
            return result
        humanized_action_sleep(520, 1300)
    return {
        **result,
        "ok": False,
        "reason": str(last_search_state.get("reason") or "search_state_still_active_after_dismiss"),
        "search_state": last_search_state,
    }


def clear_sidebar_search_box_without_select_all(
    hwnd: int,
    search_x: int,
    search_y: int,
    *,
    target_hint: str = "",
    geometry: dict[str, Any] | None = None,
    artifact_dir: str | None = None,
    recover_foreground: bool = False,
    progress_event: Callable[..., None] | None = None,
) -> dict[str, Any]:
    """Prepare sidebar search with slow, verified actions.

    Search is high risk: never paste a target until WeChat visibly reports the
    sidebar search box as active, and clear stale text with a bounded select-all
    action only after that activation is confirmed.
    """
    guard = recover_send_window_guard(hwnd, max_attempts=2) if recover_foreground else basic_send_window_guard(hwnd)
    if not guard.get("ok"):
        return {"ok": False, "reason": "window_guard_failed_before_search_clear", "window_guard": guard}
    # ESC + search-box click can stall rendering on some WeChat builds. Keep
    # it opt-in for diagnostics instead of making it part of the normal search
    # preparation path.
    escape_enabled = env_flag("WECHAT_WIN32_OCR_TARGET_SEARCH_CLEAR_ESCAPE", default=False)
    if escape_enabled:
        humanized_action_sleep(520, 1200)
        key_press(win32con.VK_ESCAPE)
        humanized_action_sleep(650, 1400)
    click_result: dict[str, Any] = {"ok": True, "bounds": None}
    active_geometry = geometry if isinstance(geometry, dict) else get_window_geometry(hwnd)
    split_x = session_split_x(int(active_geometry.get("width") or 0))
    bounds = [
        max(42, int(search_x) - 56),
        max(42, int(search_y) - 22),
        min(max(120, split_x - 34), int(search_x) + 96),
        min(132, int(search_y) + 26),
    ]
    if bounds[2] > bounds[0] and bounds[3] > bounds[1]:
        click_result = human_window_image_click_in_bounds(
            hwnd,
            int(search_x),
            int(search_y),
            bounds=bounds,
            action_name="sidebar_search_box_click",
        )
    else:
        human_window_image_click(hwnd, search_x, search_y)
    humanized_action_sleep(720, 1600)
    if not artifact_dir and progress_event is None:
        # Keep the legacy low-disturbance path for ordinary visible-target
        # search fallback calls. The strict select-all/OCR verification below
        # is reserved for artifact-backed remark-code targeting.
        default_backspaces = random.randint(1, 3)
        backspaces = bounded_int(
            os.getenv("WECHAT_WIN32_OCR_TARGET_SEARCH_CLEAR_BACKSPACES"),
            default=default_backspaces,
            minimum=0,
            maximum=8,
        )
        deletes = bounded_int(
            os.getenv("WECHAT_WIN32_OCR_TARGET_SEARCH_CLEAR_DELETES"),
            default=0,
            minimum=0,
            maximum=2,
        )
        key_count = 1
        for idx in range(backspaces):
            guard = recover_send_window_guard(hwnd, max_attempts=1) if recover_foreground else basic_send_window_guard(hwnd)
            if not guard.get("ok"):
                return {
                    "ok": False,
                    "reason": "window_guard_failed_during_search_clear",
                    "window_guard": guard,
                    "backspaces": idx,
                    "deletes": 0,
                    "click": click_result,
                }
            key_press(win32con.VK_BACK)
            key_count += 1
            humanized_action_sleep(140, 460)
        for idx in range(deletes):
            guard = recover_send_window_guard(hwnd, max_attempts=1) if recover_foreground else basic_send_window_guard(hwnd)
            if not guard.get("ok"):
                return {
                    "ok": False,
                    "reason": "window_guard_failed_during_search_clear",
                    "window_guard": guard,
                    "backspaces": backspaces,
                    "deletes": idx,
                    "click": click_result,
                }
            key_press(win32con.VK_DELETE)
            key_count += 1
            humanized_action_sleep(160, 480)
        humanized_action_sleep(520, 1300)
        return {
            "ok": True,
            "method": "slow_sidebar_search_prepare",
            "backspaces": backspaces,
            "deletes": deletes,
            "key_count": key_count,
            "click": click_result,
            "window_guard": guard,
        }
    probe_shot, probe_path = capture_wechat(hwnd, artifact_dir=artifact_dir, label="open_chat_search_box_after_click")
    probe_items = run_ocr_traced(probe_shot, "open_chat_search_box_after_click", source="open_chat")
    if progress_event is not None:
        progress_event(
            "search_box_clicked",
            "completed",
            screenshot_path=probe_path,
            ocr_count=len(probe_items),
            click=click_result,
        )
    surface = target_switch_surface_state(
        probe_shot,
        probe_items,
        geometry=active_geometry,
        screenshot_path=probe_path,
        target=target_hint,
    )
    if not surface.get("ok"):
        return {
            "ok": False,
            "reason": str(surface.get("reason") or "search_box_surface_not_ok"),
            "surface": surface,
            "click": click_result,
        }
    search_state = sidebar_search_state_detected(probe_shot, probe_items, geometry=active_geometry)
    if not search_state.get("detected"):
        return {
            "ok": False,
            "reason": "search_box_focus_not_confirmed",
            "surface": surface,
            "search_state": search_state,
            "click": click_result,
        }

    guard = recover_send_window_guard(hwnd, max_attempts=2) if recover_foreground else basic_send_window_guard(hwnd)
    if not guard.get("ok"):
        return {
            "ok": False,
            "reason": "window_guard_failed_before_search_select_all",
            "window_guard": guard,
            "surface": surface,
            "search_state": search_state,
            "click": click_result,
        }
    hotkey(win32con.VK_CONTROL, ord("A"))
    humanized_action_sleep(120, 360)
    key_press(win32con.VK_BACK)
    humanized_action_sleep(520, 1300)
    clear_shot, clear_path = capture_wechat(hwnd, artifact_dir=artifact_dir, label="open_chat_search_box_after_clear")
    clear_items = run_ocr_traced(clear_shot, "open_chat_search_box_after_clear", source="open_chat")
    clear_surface = target_switch_surface_state(
        clear_shot,
        clear_items,
        geometry=active_geometry,
        screenshot_path=clear_path,
        target=target_hint,
    )
    clear_state = sidebar_search_state_detected(clear_shot, clear_items, geometry=active_geometry)
    clear_query_text = sidebar_search_query_text(clear_items, clear_shot.size, geometry=active_geometry)
    refocus_result: dict[str, Any] = {}
    if clear_surface.get("ok") and not clear_state.get("detected") and not clear_query_text:
        if bounds[2] > bounds[0] and bounds[3] > bounds[1]:
            refocus_click = human_window_image_click_in_bounds(
                hwnd,
                int(search_x),
                int(search_y),
                bounds=bounds,
                action_name="sidebar_search_box_refocus_after_clear",
            )
        else:
            human_window_image_click(hwnd, search_x, search_y)
            refocus_click = {"ok": True, "x": search_x, "y": search_y}
        humanized_action_sleep(520, 1300)
        refocus_shot, refocus_path = capture_wechat(hwnd, artifact_dir=artifact_dir, label="open_chat_search_box_after_clear_refocus")
        refocus_items = run_ocr_traced(refocus_shot, "open_chat_search_box_after_clear_refocus", source="open_chat")
        if progress_event is not None:
            progress_event(
                "search_box_refocused_after_clear",
                "completed",
                screenshot_path=refocus_path,
                ocr_count=len(refocus_items),
                click=refocus_click,
            )
        refocus_surface = target_switch_surface_state(
            refocus_shot,
            refocus_items,
            geometry=active_geometry,
            screenshot_path=refocus_path,
            target=target_hint,
        )
        refocus_state = sidebar_search_state_detected(refocus_shot, refocus_items, geometry=active_geometry)
        refocus_query_text = sidebar_search_query_text(refocus_items, refocus_shot.size, geometry=active_geometry)
        refocus_result = {
            "click": refocus_click,
            "surface": refocus_surface,
            "search_state": refocus_state,
            "query_text": refocus_query_text,
            "screenshot_path": refocus_path,
        }
        clear_surface = refocus_surface
        clear_state = refocus_state
        clear_query_text = refocus_query_text
    if not clear_surface.get("ok") or not clear_state.get("detected"):
        return {
            "ok": False,
            "reason": "search_box_focus_lost_after_clear",
            "surface": clear_surface,
            "search_state": clear_state,
            "click": click_result,
            "query_text": clear_query_text,
            "refocus": refocus_result,
        }
    if clear_query_text:
        return {
            "ok": False,
            "reason": "search_box_not_empty_after_clear",
            "query_text": clear_query_text,
            "surface": clear_surface,
            "search_state": clear_state,
            "click": click_result,
            "refocus": refocus_result,
        }
    return {
        "ok": True,
        "method": "verified_sidebar_search_select_all_clear",
        "key_count": 2,
        "query_text": clear_query_text,
        "surface": clear_surface,
        "search_state": clear_state,
        "click": click_result,
        "window_guard": guard,
        "refocused_after_clear": bool(refocus_result),
        "refocus": refocus_result,
    }


def type_sidebar_search_query(
    hwnd: int,
    target: str,
    *,
    geometry: dict[str, Any] | None = None,
    artifact_dir: str | None = None,
    recover_foreground: bool = False,
    verify_after_paste: bool = False,
) -> dict[str, Any]:
    method = str(os.getenv("WECHAT_WIN32_OCR_TARGET_SEARCH_INPUT_METHOD") or "clipboard").strip().lower()
    if method == "clipboard":
        guard = recover_send_window_guard(hwnd, max_attempts=2) if recover_foreground else basic_send_window_guard(hwnd)
        if not guard.get("ok"):
            return {"ok": False, "method": "clipboard", "reason": "window_guard_failed_before_search_paste", "window_guard": guard}
        humanized_action_sleep(300, 900)
        clipboard_copy(target)
        humanized_action_sleep(220, 720)
        hotkey(win32con.VK_CONTROL, ord("V"))
        humanized_action_sleep(850, 1700)
        if not artifact_dir and not verify_after_paste:
            return {"ok": True, "method": "clipboard", "window_guard": guard}
        active_geometry = geometry if isinstance(geometry, dict) else get_window_geometry(hwnd)
        verify_shot, verify_path = capture_wechat(hwnd, artifact_dir=artifact_dir, label="open_chat_search_box_after_paste")
        verify_items = run_ocr_traced(verify_shot, "open_chat_search_box_after_paste", source="open_chat")
        surface = target_switch_surface_state(
            verify_shot,
            verify_items,
            geometry=active_geometry,
            screenshot_path=verify_path,
            target=target,
        )
        search_state = sidebar_search_state_detected(verify_shot, verify_items, geometry=active_geometry)
        query_text = sidebar_search_query_text(verify_items, verify_shot.size, geometry=active_geometry)
        if not surface.get("ok") or not search_state.get("detected"):
            return {
                "ok": False,
                "method": "clipboard",
                "reason": "search_box_focus_not_confirmed_after_paste",
                "surface": surface,
                "search_state": search_state,
                "query_text": query_text,
            }
        if not sidebar_search_query_matches(query_text, target):
            return {
                "ok": False,
                "method": "clipboard",
                "reason": "search_query_text_mismatch_after_paste",
                "expected_query": target,
                "query_text": query_text,
                "surface": surface,
                "search_state": search_state,
            }
        return {"ok": True, "method": "clipboard", "query_text": query_text, "window_guard": guard}
    settings = {
        "enabled": True,
        "chunk_min_chars": 2,
        "chunk_max_chars": 4,
        "char_delay_min_ms": 70,
        "char_delay_max_ms": 165,
        "micro_pause_every_chars": 0,
        "micro_pause_min_ms": 0,
        "micro_pause_max_ms": 0,
        "typo_probability": 0.0,
        "typo_max": 0,
    }
    return type_text_with_sendinput_unicode(
        target,
        settings,
        window_guard_func=lambda: basic_send_window_guard(hwnd),
    )


def nudge_sidebar_search_query_for_results(
    hwnd: int,
    target: str,
    *,
    geometry: dict[str, Any] | None = None,
    artifact_dir: str | None = None,
    recover_foreground: bool = True,
) -> dict[str, Any]:
    clean_target = str(target or "").strip()
    if not clean_target:
        return {"ok": False, "reason": "target_required"}
    guard = recover_send_window_guard(hwnd, max_attempts=2) if recover_foreground else basic_send_window_guard(hwnd)
    if not guard.get("ok"):
        return {"ok": False, "reason": "window_guard_failed_before_search_nudge", "window_guard": guard}
    humanized_action_sleep(160, 420)
    key_press(win32con.VK_BACK)
    humanized_action_sleep(120, 360)
    last_char = clean_target[-1]
    typed = type_text_with_sendinput_unicode(
        last_char,
        {
            "enabled": True,
            "chunk_min_chars": 1,
            "chunk_max_chars": 1,
            "char_delay_min_ms": 70,
            "char_delay_max_ms": 165,
            "micro_pause_every_chars": 0,
            "micro_pause_min_ms": 0,
            "micro_pause_max_ms": 0,
            "typo_probability": 0.0,
            "typo_max": 0,
        },
        window_guard_func=lambda: recover_send_window_guard(hwnd, max_attempts=1),
    )
    if not typed.get("ok"):
        return {"ok": False, "reason": str(typed.get("reason") or "search_nudge_type_failed"), "typed": typed, "window_guard": guard}
    humanized_action_sleep(520, 1100)
    active_geometry = geometry if isinstance(geometry, dict) else get_window_geometry(hwnd)
    shot, shot_path = capture_wechat(hwnd, artifact_dir=artifact_dir, label="open_chat_search_box_after_nudge")
    items = run_ocr_traced(shot, "open_chat_search_box_after_nudge", source="open_chat")
    surface = target_switch_surface_state(
        shot,
        items,
        geometry=active_geometry,
        screenshot_path=shot_path,
        target=clean_target,
    )
    search_state = sidebar_search_state_detected(shot, items, geometry=active_geometry)
    query_text = sidebar_search_query_text(items, shot.size, geometry=active_geometry)
    if not surface.get("ok") or not search_state.get("detected"):
        return {
            "ok": False,
            "reason": "search_box_focus_not_confirmed_after_nudge",
            "typed": typed,
            "surface": surface,
            "search_state": search_state,
            "query_text": query_text,
            "screenshot_path": shot_path,
            "ocr_count": len(items),
        }
    if not sidebar_search_query_matches(query_text, clean_target):
        return {
            "ok": False,
            "reason": "search_query_text_mismatch_after_nudge",
            "expected_query": clean_target,
            "query_text": query_text,
            "typed": typed,
            "surface": surface,
            "search_state": search_state,
            "screenshot_path": shot_path,
            "ocr_count": len(items),
        }
    return {
        "ok": True,
        "method": "backspace_then_sendinput_last_char",
        "query_text": query_text,
        "typed": typed,
        "surface": surface,
        "search_state": search_state,
        "window_guard": guard,
        "screenshot_path": shot_path,
        "ocr_count": len(items),
    }


def remark_code_matches_text(text: str, remark_code: str) -> bool:
    expected = re.sub(r"\s+", "", str(remark_code or "")).strip().lower()
    actual = re.sub(r"\s+", "", normalize_session_name(str(text or ""))).strip().lower()
    return bool(expected and actual and expected in actual)


def search_result_sessions_matching_remark_code(
    sessions: list[dict[str, Any]],
    remark_code: str,
) -> list[dict[str, Any]]:
    return [
        item
        for item in sessions
        if isinstance(item, dict) and remark_code_matches_text(str(item.get("name") or ""), remark_code)
    ]


def _targeting_review_value(payload: dict[str, Any], keys: tuple[str, ...]) -> Any:
    value: Any = payload
    for key in keys:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def _targeting_review_row(
    *,
    title: str,
    purpose: str,
    expected: str,
    source: dict[str, Any] | None = None,
    detection: dict[str, Any] | None = None,
) -> dict[str, Any]:
    source = source if isinstance(source, dict) else {}
    detection = detection if isinstance(detection, dict) else {}
    return {
        "title": title,
        "purpose": purpose,
        "expected": expected,
        "raw": source.get("screenshot_path") or detection.get("screenshot_path") or "",
        "annotated": source.get("annotated_path") or detection.get("annotated_path") or "",
        "targets": source.get("targets") or detection.get("targets") or [],
        "detection": detection,
    }


def write_messages_targeting_review(output_dir: Path, payload: dict[str, Any]) -> str:
    rows: list[dict[str, Any]] = []
    steps = payload.get("step_events") if isinstance(payload.get("step_events"), list) else []
    by_step = {str(item.get("step") or ""): item for item in steps if isinstance(item, dict)}

    rows.append(
        _targeting_review_row(
            title="00 字段与目标模式",
            purpose="检查 C2 定向读取是否使用正式字段和 search_by_remark_code 模式。",
            expected="remark_code 必须非空；非第一屏读取必须先搜索短码，再确认会话，不允许读取当前窗口。",
            detection={
                "ok": payload.get("ok"),
                "reason": payload.get("reason"),
                "error_code": payload.get("error_code"),
                "partial": payload.get("partial"),
                "sidecar_run_id": payload.get("sidecar_run_id"),
                "target_mode": payload.get("target_mode"),
                "target": payload.get("target"),
                "remark_code": payload.get("remark_code"),
            },
        )
    )
    rows.append(
        _targeting_review_row(
            title="01 微信窗口预检",
            purpose="检查微信窗口是否可用，避免在登录页、白屏、辅助窗口或错误窗口里继续操作。",
            expected="window_guard.ok=true；若 foreground 不在目标微信窗口，必须先 recover/activate 到目标微信窗口，恢复失败不得继续键盘操作。",
            detection=by_step.get("wechat_window_precheck") or {},
        )
    )
    rows.append(
        _targeting_review_row(
            title="02 基线截图",
            purpose="搜索前截取当前微信窗口，确认当前界面和 OCR 数量。",
            expected="应能看到微信主窗口，OCR 不应为空或白屏。",
            detection=by_step.get("baseline_screenshot") or {},
        )
    )
    clear_step = by_step.get("clear_search_box") or {}
    clear_result = clear_step.get("result") if isinstance(clear_step.get("result"), dict) else {}
    rows.append(
        _targeting_review_row(
            title="03 清空搜索框后复核",
            purpose="检查搜索框是否先被点击并激活，然后 Ctrl+A + Backspace 清空。",
            expected="result.ok=true，search_state.detected=true，query_text 为空；否则禁止粘贴短码。",
            source=clear_result.get("surface") if isinstance(clear_result.get("surface"), dict) else {},
            detection=clear_step,
        )
    )
    paste_step = by_step.get("paste_remark_code") or {}
    paste_result = paste_step.get("result") if isinstance(paste_step.get("result"), dict) else {}
    rows.append(
        _targeting_review_row(
            title="04 粘贴短码后复核",
            purpose="检查粘贴后搜索框里的查询词是否与 remark_code 完全一致。",
            expected="query_text 必须等于 remark_code；例如 CJWCJWIN01WIN01 必须失败，不允许点击候选。",
            source=paste_result.get("surface") if isinstance(paste_result.get("surface"), dict) else {},
            detection=paste_step,
        )
    )
    candidate_step = by_step.get("unique_candidate_check") or {}
    rows.append(
        _targeting_review_row(
            title="05 搜索结果唯一候选",
            purpose="检查搜索结果中联系人区是否唯一命中短码，排除群聊、更多、网络结果。",
            expected="contact_match_count=1 且 match_count=1；多义或无匹配均禁止点击。",
            detection=candidate_step,
        )
    )
    click_step = by_step.get("click_unique_candidate") or {}
    rows.append(
        _targeting_review_row(
            title="06 点击唯一联系人并确认",
            purpose="检查是否点击了唯一联系人候选，并在点击后用标题/短码确认进入目标会话。",
            expected="activation.ok=true；确认失败时看 attempts 中每个点击点和 validation。",
            detection=click_step,
        )
    )
    confirm_step = by_step.get("confirm_active_title_remark_code") or {}
    rows.append(
        _targeting_review_row(
            title="07 标题/备注短码二次确认",
            purpose="读取消息前最后确认当前会话标题或备注包含短码。",
            expected="validation.ok=true；失败时不得读取当前窗口消息。",
            detection=confirm_step,
        )
    )
    rows.append(
        _targeting_review_row(
            title="99 最终判定",
            purpose="汇总本次 C2 定向读取目标确认结果。",
            expected="ok=true 后才允许读取 messages；否则只输出证据和错误码。",
            detection={
                "ok": payload.get("ok"),
                "reason": payload.get("reason"),
                "selected_candidate": payload.get("selected_candidate"),
                "validation": payload.get("validation"),
                "timing": payload.get("timing"),
            },
        )
    )
    summary = {
        "ok": payload.get("ok"),
        "reason": payload.get("reason"),
        "target": payload.get("target"),
        "remark_code": payload.get("remark_code"),
        "target_mode": payload.get("target_mode"),
        "sidecar_run_id": payload.get("sidecar_run_id"),
        "partial": payload.get("partial"),
        "timing": payload.get("timing") or {},
    }
    return write_step_event_report(
        output_dir=output_dir,
        json_name="wechat_messages_targeting_review.json",
        html_name="wechat_messages_targeting_review.html",
        title="C2 messages 定向读取复核报告",
        description="本报告验证 search_by_remark_code 是否清空搜索框、输入短码、唯一命中联系人、点击进入会话并二次确认短码。",
        summary=summary,
        events=step_events_from_review_rows(rows),
    )


def search_result_contact_candidates_matching_remark_code(
    ocr_items: list[dict[str, Any]],
    image_size: tuple[int, int],
    remark_code: str,
) -> list[dict[str, Any]]:
    width, height = image_size
    split_x = session_split_x(width)
    left_panel_right = min(max(split_x + 170, 470), width - 40)

    def in_search_panel(item: dict[str, Any]) -> bool:
        return float(item.get("left") or 0) < left_panel_right and float(item.get("center_y") or 0) >= 80

    headings: list[tuple[str, float]] = []
    for item in ocr_items or []:
        text = normalize_ocr_text(item.get("text"))
        if not text or not in_search_panel(item):
            continue
        compact = re.sub(r"\s+", "", text)
        if compact in {"联系人", "群聊", "更多"} or "搜索网络结果" in compact or "网络查找" in compact or compact.startswith("查看全部"):
            headings.append((compact, float(item.get("center_y") or 0)))

    contact_heading_y = min((y for text, y in headings if text == "联系人"), default=86.0)
    section_bottom = min(
        (
            y
            for text, y in headings
            if y > contact_heading_y and (text in {"群聊", "更多"} or "搜索网络结果" in text or "网络查找" in text)
        ),
        default=float(height),
    )

    matches: list[dict[str, Any]] = []
    consumed_rows: list[float] = []
    for item in sorted(ocr_items or [], key=lambda row: float(row.get("center_y") or 0)):
        text = str(item.get("text") or "").strip()
        center_y = float(item.get("center_y") or 0)
        if center_y <= contact_heading_y or center_y >= section_bottom:
            continue
        if not in_search_panel(item):
            continue
        if not remark_code_matches_text(text, remark_code):
            continue
        if any(abs(center_y - used_y) <= 18 for used_y in consumed_rows):
            continue
        row_items = [
            other
            for other in ocr_items or []
            if in_search_panel(other)
            and abs(float(other.get("center_y") or 0) - center_y) <= 24
            and contact_heading_y < float(other.get("center_y") or 0) < section_bottom
        ]
        row_items = sorted(row_items, key=lambda row: float(row.get("left") or 0))
        row_texts = [str(other.get("text") or "").strip() for other in row_items if str(other.get("text") or "").strip()]
        name = normalize_session_name(" ".join(row_texts)) or normalize_session_name(text)
        if not remark_code_matches_text(name, remark_code):
            name = normalize_session_name(text)
        left = min(float(other.get("left") or item.get("left") or 0) for other in row_items)
        right = max(float(other.get("right") or item.get("right") or 0) for other in row_items)
        top = min(float(other.get("top") or item.get("top") or 0) for other in row_items)
        bottom = max(float(other.get("bottom") or item.get("bottom") or 0) for other in row_items)
        text_center_x = int((float(item.get("left") or left) + float(item.get("right") or right)) / 2)
        bounds = [
            max(88, int(left) - 74),
            max(88, int(top) - 18),
            min(left_panel_right, max(int(right) + 150, int(left) + 210)),
            min(height - 12, max(int(bottom) + 22, int(top) + 62)),
        ]
        click_points = [
            [bounded_int(text_center_x, default=190, minimum=bounds[0] + 12, maximum=bounds[2] - 12), int(center_y)],
            [bounded_int(int(right) + 24, default=text_center_x, minimum=bounds[0] + 12, maximum=bounds[2] - 12), int(center_y)],
            [bounded_int(int(left) + 56, default=text_center_x, minimum=bounds[0] + 12, maximum=bounds[2] - 12), int(center_y)],
        ]
        matches.append(
            {
                "name": name,
                "session_key": rpa_session_key(name, conversation_type="contact", row_fingerprint=session_row_fingerprint(item, duplicate_index=0)),
                "conversation_type": "contact",
                "row_fingerprint": session_row_fingerprint(item, duplicate_index=0),
                "duplicate_name_index": 0,
                "ambiguous_display_name": False,
                "confidence": item.get("confidence"),
                "center_y": center_y,
                "left": left,
                "right": right,
                "top": top,
                "bottom": bottom,
                "source_adapter": "win32_ocr",
                "source": "search_contact_result",
                "search_result_bounds": bounds,
                "search_result_click_points": click_points,
                "section": "contacts",
            }
        )
        consumed_rows.append(center_y)
    return matches


def activate_search_result_candidate(
    hwnd: int,
    candidate: dict[str, Any],
    *,
    remark_code: str,
    artifact_dir: str | None = None,
) -> dict[str, Any]:
    timing: dict[str, Any] = {}
    attempts: list[dict[str, Any]] = []
    activate_started = _sidecar_timing_start(timing, "activate_search_result_candidate")
    points = candidate.get("search_result_click_points") if isinstance(candidate.get("search_result_click_points"), list) else []
    if not points:
        center_y = int(float(candidate.get("center_y") or 0))
        left = int(float(candidate.get("left") or 0))
        right = int(float(candidate.get("right") or 0))
        points = [[int((left + right) / 2), center_y], [right + 24, center_y], [left + 56, center_y]]

    def finish(ok: bool, reason: str, **payload: Any) -> dict[str, Any]:
        _sidecar_timing_finish(timing, "activate_search_result_candidate", activate_started)
        timing["ok"] = bool(ok)
        timing["reason"] = reason
        return {"ok": bool(ok), "reason": reason, "attempts": attempts, "timing": dict(timing), **payload}

    for index, point in enumerate(points[:3]):
        try:
            x, y = int(point[0]), int(point[1])
        except Exception:
            continue
        pre_click_wait_started = _sidecar_timing_start(timing, f"search_result_click_{index + 1}_wait")
        humanized_action_sleep(260, 760)
        _sidecar_timing_finish(timing, f"search_result_click_{index + 1}_wait", pre_click_wait_started)
        click_started = _sidecar_timing_start(timing, f"search_result_click_{index + 1}")
        # Search-result candidates are derived from OCR screenshot coordinates,
        # so click in the same window-image coordinate space as add_friend.
        human_window_image_click(hwnd, x, y)
        _sidecar_timing_finish(timing, f"search_result_click_{index + 1}", click_started)
        humanized_action_sleep(520, 1050)
        validation = validate_active_send_target(hwnd, remark_code, exact=False, artifact_dir=artifact_dir)
        attempts.append({"point": [x, y], "click_method": "human_window_image_click", "validation": validation})
        if active_send_guard_is_strong(validation):
            return finish(True, "search_result_candidate_confirmed", validation=validation, confirmed_point=[x, y])
        if target_switch_validation_is_hard_stop(validation):
            return finish(False, "search_result_candidate_hard_stop", validation=validation)
    last_validation = attempts[-1]["validation"] if attempts else {}
    return finish(False, "search_result_candidate_not_confirmed", validation=last_validation)


def open_chat_by_remark_code_search(
    hwnd: int,
    *,
    target: str,
    remark_code: str,
    artifact_dir: str | None = None,
    sidecar_run_id: str = "",
) -> dict[str, Any]:
    timing: dict[str, Any] = {}
    step_events: list[dict[str, Any]] = []
    clean_remark = str(remark_code or "").strip()
    clean_target = str(target or "").strip()
    clean_sidecar_run_id = str(sidecar_run_id or "").strip()
    ocr_trace_token = _ocr_trace_start()
    open_started = _sidecar_timing_start(timing, "open_chat_by_remark_code_search")
    partial_review_error = ""

    def make_report_payload(ok: bool, reason: str, partial: bool, **payload: Any) -> dict[str, Any]:
        return {
            "ok": bool(ok),
            "reason": reason,
            "partial": bool(partial),
            "sidecar_run_id": clean_sidecar_run_id,
            "target_mode": "search_by_remark_code",
            "target": clean_target,
            "remark_code": clean_remark,
            "step_events": step_events,
            "timing": dict(timing),
            **payload,
        }

    def flush_partial_review(reason: str) -> None:
        nonlocal partial_review_error
        if not artifact_dir:
            return
        try:
            review_path = write_messages_targeting_review(
                Path(artifact_dir),
                make_report_payload(False, reason, True),
            )
            timing["partial_review_path"] = review_path
        except Exception as exc:
            partial_review_error = repr(exc)
            timing["partial_review_error"] = partial_review_error

    def event(step: str, status: str, **metadata: Any) -> None:
        step_events.append({"step": step, "status": status, "sidecar_run_id": clean_sidecar_run_id, **metadata})
        flush_partial_review(f"partial_after_{step}")

    def finish(ok: bool, reason: str, **payload: Any) -> dict[str, Any]:
        global _LAST_OPEN_CHAT_TIMING
        _sidecar_timing_finish(timing, "open_chat_by_remark_code_search", open_started)
        _sidecar_timing_merge_ocr_trace(timing, "open_chat_by_remark_code_search", _ocr_trace_finish(ocr_trace_token))
        timing["opened"] = bool(ok)
        timing["reason"] = reason
        _LAST_OPEN_CHAT_TIMING = dict(timing)
        if partial_review_error:
            payload.setdefault("partial_review_error", partial_review_error)
        result = make_report_payload(bool(ok), reason, False, **payload)
        if artifact_dir:
            try:
                review_path = write_messages_targeting_review(Path(artifact_dir), result)
                result["review_path"] = review_path
                result["evidence_path"] = review_path
            except Exception as exc:
                result["review_error"] = repr(exc)
        return result

    if not clean_remark:
        event("field_validation", "failed", error_code="C2_TARGET_REMARK_CODE_MISSING")
        return finish(False, "remark_code_required", error_code="C2_TARGET_REMARK_CODE_MISSING")

    guard = recover_send_window_guard(hwnd, max_attempts=2)
    precheck_event: dict[str, Any] = {"guard": guard}
    if not guard.get("ok"):
        try:
            precheck_shot, precheck_path = capture_wechat(hwnd, artifact_dir=artifact_dir, label="messages_window_precheck_failed")
            precheck_items = run_ocr_traced(precheck_shot, "messages_window_precheck_failed", source="messages_search")
            precheck_geometry = get_window_geometry(hwnd)
            precheck_surface = target_switch_surface_state(
                precheck_shot,
                precheck_items,
                geometry=precheck_geometry,
                screenshot_path=precheck_path,
                target=clean_remark,
            )
            annotated_path = Path(artifact_dir or ".") / "messages_window_precheck_failed_annotated.png"
            annotated = draw_add_friend_screen_annotation(
                precheck_shot,
                ocr_items=precheck_items,
                targets=[],
                output_path=annotated_path,
                window_rect=None,
            )
            precheck_event.update(
                {
                    "screenshot_path": precheck_path,
                    "annotated_path": annotated,
                    "ocr_count": len(precheck_items),
                    "surface": precheck_surface,
                }
            )
        except Exception as exc:
            precheck_event["evidence_error"] = repr(exc)
    event("wechat_window_precheck", "completed" if guard.get("ok") else "failed", **precheck_event)
    if not guard.get("ok"):
        return finish(False, "window_guard_failed_before_search", window_guard=guard)

    baseline_started = _sidecar_timing_start(timing, "search_by_remark_code_baseline")
    baseline_shot, baseline_items = ensure_main_session_list(hwnd, artifact_dir=artifact_dir)
    _sidecar_timing_finish(timing, "search_by_remark_code_baseline", baseline_started)
    geometry = get_window_geometry(hwnd)
    search_x, search_y = sidebar_search_input_focus_point_for_geometry(geometry)
    session_click_x = session_click_x_for_geometry(geometry)
    baseline_event: dict[str, Any] = {"ocr_count": len(baseline_items)}
    if artifact_dir:
        try:
            baseline_path = save_screenshot_artifact(baseline_shot, artifact_dir=artifact_dir, label="messages_search_baseline")
            baseline_annotated_path = Path(artifact_dir) / "messages_search_baseline_annotated.png"
            baseline_event["screenshot_path"] = baseline_path
            baseline_event["annotated_path"] = draw_add_friend_screen_annotation(
                baseline_shot,
                ocr_items=baseline_items,
                targets=[],
                output_path=baseline_annotated_path,
                window_rect=None,
            )
        except Exception as exc:
            baseline_event["evidence_error"] = repr(exc)
    event("baseline_screenshot", "completed", **baseline_event)

    clear_started = _sidecar_timing_start(timing, "search_by_remark_code_clear_search")
    clear_result = clear_sidebar_search_box_without_select_all(
        hwnd,
        search_x,
        search_y,
        target_hint=clean_remark,
        geometry=geometry,
        artifact_dir=artifact_dir,
        recover_foreground=True,
        progress_event=event,
    )
    _sidecar_timing_finish(timing, "search_by_remark_code_clear_search", clear_started)
    event("clear_search_box", "completed" if clear_result.get("ok") else "failed", result=clear_result)
    if not clear_result.get("ok"):
        return finish(False, str(clear_result.get("reason") or "search_clear_failed"), clear_result=clear_result)

    input_started = _sidecar_timing_start(timing, "search_by_remark_code_input")
    input_result = type_sidebar_search_query(
        hwnd,
        clean_remark,
        geometry=geometry,
        artifact_dir=artifact_dir,
        recover_foreground=True,
        verify_after_paste=True,
    )
    _sidecar_timing_finish(timing, "search_by_remark_code_input", input_started)
    event("paste_remark_code", "completed" if input_result.get("ok") else "failed", result=input_result)
    if not input_result.get("ok"):
        return finish(False, str(input_result.get("reason") or "search_input_failed"), input_result=input_result)

    wait_started = _sidecar_timing_start(timing, "search_by_remark_code_wait_results")
    humanized_action_sleep(
        bounded_int(os.getenv("WECHAT_WIN32_OCR_MESSAGES_SEARCH_WAIT_MIN_MS"), default=1200, minimum=300, maximum=8000),
        bounded_int(os.getenv("WECHAT_WIN32_OCR_MESSAGES_SEARCH_WAIT_MAX_MS"), default=2400, minimum=500, maximum=10000),
    )
    _sidecar_timing_finish(timing, "search_by_remark_code_wait_results", wait_started)
    event("wait_search_results_stable", "completed")

    capture_started = _sidecar_timing_start(timing, "search_by_remark_code_capture_results")
    search_shot, search_path = capture_wechat_window_visible_screen(hwnd, artifact_dir=artifact_dir, label="messages_search_by_remark_code_results")
    search_items = run_ocr_traced(search_shot, "messages_search_by_remark_code_results", source="messages_search")
    _sidecar_timing_finish(timing, "search_by_remark_code_capture_results", capture_started)
    event("ocr_search_candidates", "completed", screenshot_path=search_path, ocr_count=len(search_items), capture_mode="wechat_window_visible_screen")
    if not search_items:
        return finish(False, "search_no_ocr_items", screenshot_path=search_path)

    surface = target_switch_surface_state(
        search_shot,
        search_items,
        geometry=geometry,
        screenshot_path=search_path,
        target=clean_remark,
    )
    if not surface.get("ok"):
        event("search_surface_check", "failed", surface=surface)
        return finish(False, str(surface.get("reason") or "search_surface_not_ok"), screenshot_path=search_path, surface=surface)
    event("search_surface_check", "completed", surface=surface)

    contact_matches = search_result_contact_candidates_matching_remark_code(search_items, search_shot.size, clean_remark)
    sessions = parse_sessions_from_ocr(search_items, search_shot.size, screenshot=search_shot)
    session_matches = search_result_sessions_matching_remark_code(sessions, clean_remark)
    matches = contact_matches or session_matches
    nudge_result: dict[str, Any] = {}
    if not matches:
        nudge_started = _sidecar_timing_start(timing, "search_by_remark_code_nudge_results")
        nudge_result = nudge_sidebar_search_query_for_results(
            hwnd,
            clean_remark,
            geometry=geometry,
            artifact_dir=artifact_dir,
            recover_foreground=True,
        )
        _sidecar_timing_finish(timing, "search_by_remark_code_nudge_results", nudge_started)
        event("search_query_nudge_for_results", "completed" if nudge_result.get("ok") else "failed", result=nudge_result)
        if nudge_result.get("ok"):
            wait_after_nudge_started = _sidecar_timing_start(timing, "search_by_remark_code_wait_results_after_nudge")
            humanized_action_sleep(
                bounded_int(os.getenv("WECHAT_WIN32_OCR_MESSAGES_SEARCH_NUDGE_WAIT_MIN_MS"), default=900, minimum=250, maximum=8000),
                bounded_int(os.getenv("WECHAT_WIN32_OCR_MESSAGES_SEARCH_NUDGE_WAIT_MAX_MS"), default=1800, minimum=400, maximum=10000),
            )
            _sidecar_timing_finish(timing, "search_by_remark_code_wait_results_after_nudge", wait_after_nudge_started)
            recapture_started = _sidecar_timing_start(timing, "search_by_remark_code_recapture_results_after_nudge")
            search_shot, search_path = capture_wechat_window_visible_screen(
                hwnd,
                artifact_dir=artifact_dir,
                label="messages_search_by_remark_code_results_after_nudge",
            )
            search_items = run_ocr_traced(search_shot, "messages_search_by_remark_code_results_after_nudge", source="messages_search")
            _sidecar_timing_finish(timing, "search_by_remark_code_recapture_results_after_nudge", recapture_started)
            event(
                "ocr_search_candidates_after_nudge",
                "completed",
                screenshot_path=search_path,
                ocr_count=len(search_items),
                capture_mode="wechat_window_visible_screen",
            )
            surface = target_switch_surface_state(
                search_shot,
                search_items,
                geometry=geometry,
                screenshot_path=search_path,
                target=clean_remark,
            )
            if not surface.get("ok"):
                event("search_surface_check_after_nudge", "failed", surface=surface)
                return finish(False, str(surface.get("reason") or "search_surface_not_ok_after_nudge"), screenshot_path=search_path, surface=surface, nudge=nudge_result)
            event("search_surface_check_after_nudge", "completed", surface=surface)
            contact_matches = search_result_contact_candidates_matching_remark_code(search_items, search_shot.size, clean_remark)
            sessions = parse_sessions_from_ocr(search_items, search_shot.size, screenshot=search_shot)
            session_matches = search_result_sessions_matching_remark_code(sessions, clean_remark)
            matches = contact_matches or session_matches
    match_targets = [
        {
            "label": item.get("name"),
            "bounds": item.get("search_result_bounds")
            or [item.get("left"), item.get("top"), item.get("right"), item.get("bottom")],
            "source": item.get("source"),
            "session_key": item.get("session_key"),
        }
        for item in matches[:5]
        if isinstance(item, dict)
    ]
    search_annotated_path = ""
    if artifact_dir:
        try:
            search_annotated_path = draw_add_friend_screen_annotation(
                search_shot,
                ocr_items=search_items,
                targets=match_targets,
                output_path=Path(artifact_dir) / "messages_search_by_remark_code_results_annotated.png",
                window_rect=None,
            )
        except Exception:
            search_annotated_path = ""
    event(
        "unique_candidate_check",
        "completed" if len(matches) == 1 else "failed",
        screenshot_path=search_path,
        annotated_path=search_annotated_path,
        targets=match_targets,
        contact_match_count=len(contact_matches),
        session_count=len(sessions),
        session_match_count=len(session_matches),
        match_count=len(matches),
        matches=[{"name": item.get("name"), "session_key": item.get("session_key")} for item in matches[:5]],
        nudge=nudge_result,
    )
    if not matches:
        return finish(False, "remark_code_search_no_match", screenshot_path=search_path, sessions=sessions, nudge=nudge_result)
    if len(matches) > 1:
        return finish(False, "remark_code_search_ambiguous", screenshot_path=search_path, sessions=sessions, matches=matches)

    selected = matches[0]
    activation_started = _sidecar_timing_start(timing, "search_by_remark_code_activate")
    if str(selected.get("source") or "") == "search_contact_result":
        activation_result = activate_search_result_candidate(
            hwnd,
            selected,
            remark_code=clean_remark,
            artifact_dir=artifact_dir,
        )
        opened = bool(activation_result.get("ok"))
    else:
        opened = activate_session_candidate(
            hwnd,
            selected,
            target=clean_remark,
            exact=False,
            geometry=geometry,
            default_click_x=session_click_x,
            artifact_dir=artifact_dir,
        )
        activation_result = {"ok": bool(opened), "timing": dict(_LAST_SESSION_ACTIVATION_TIMING)}
    _sidecar_timing_finish(timing, "search_by_remark_code_activate", activation_started)
    if isinstance(activation_result.get("timing"), dict):
        _sidecar_timing_merge_prefixed(timing, "search_by_remark_code_activate", activation_result["timing"])
    event(
        "click_unique_candidate",
        "completed" if opened else "failed",
        selected={"name": selected.get("name"), "session_key": selected.get("session_key"), "source": selected.get("source")},
        activation=activation_result,
    )
    if not opened:
        return finish(False, "remark_code_candidate_not_confirmed", screenshot_path=search_path, selected_candidate=selected, activation=activation_result)

    validation = validate_active_send_target(hwnd, clean_remark, exact=False, artifact_dir=artifact_dir)
    event("confirm_active_title_remark_code", "completed" if validation.get("ok") else "failed", validation=validation)
    if not validation.get("ok"):
        return finish(False, "active_title_remark_code_not_confirmed", screenshot_path=search_path, selected_candidate=selected, validation=validation)

    _LAST_RPA_ACTION_STATE["active_session_key"] = str(selected.get("session_key") or "")
    _LAST_RPA_ACTION_STATE["active_target"] = clean_target or clean_remark
    return finish(
        True,
        "remark_code_search_candidate_confirmed",
        screenshot_path=search_path,
        selected_candidate=selected,
        validation=validation,
    )


def open_chat(
    hwnd: int,
    target: str,
    *,
    exact: bool,
    artifact_dir: str | None = None,
    session_key: str = "",
) -> bool:
    timing: dict[str, Any] = {}
    ocr_trace_token = _ocr_trace_start()
    open_chat_total_started = _sidecar_timing_start(timing, "open_chat")

    def finish(opened: bool, reason: str = "") -> bool:
        global _LAST_OPEN_CHAT_TIMING
        _sidecar_timing_finish(timing, "open_chat", open_chat_total_started)
        _sidecar_timing_merge_ocr_trace(timing, "open_chat", _ocr_trace_finish(ocr_trace_token))
        timing["opened"] = bool(opened)
        if reason:
            timing["reason"] = reason
        _LAST_OPEN_CHAT_TIMING = dict(timing)
        return opened

    main_list_started = _sidecar_timing_start(timing, "open_chat_main_list")
    geometry_for_seed = get_window_geometry(hwnd)
    seed = consume_target_ready_prevalidation_ocr_seed(
        hwnd=hwnd,
        target=target,
        exact=exact,
        geometry=geometry_for_seed,
    )
    if isinstance(seed, dict):
        screenshot = seed["screenshot"]
        ocr_items = list(seed.get("ocr_items") or [])
        if detect_session_subview_back_target(ocr_items, screenshot.size):
            timing["open_chat_main_list_prevalidation_ocr_seed_reused"] = False
            timing["open_chat_main_list_prevalidation_ocr_seed_discarded"] = "session_subview"
            screenshot, ocr_items = ensure_main_session_list(hwnd, artifact_dir=artifact_dir)
        else:
            timing["open_chat_main_list_prevalidation_ocr_seed_reused"] = True
            timing["open_chat_main_list_prevalidation_ocr_seed_age_seconds"] = seed.get("age_seconds")
            timing["open_chat_main_list_prevalidation_ocr_seed_count"] = len(ocr_items)
    else:
        screenshot, ocr_items = ensure_main_session_list(hwnd, artifact_dir=artifact_dir)
        timing["open_chat_main_list_prevalidation_ocr_seed_reused"] = False
    _sidecar_timing_finish(timing, "open_chat_main_list", main_list_started)
    geometry_started = _sidecar_timing_start(timing, "open_chat_geometry")
    geometry = geometry_for_seed if isinstance(geometry_for_seed, dict) else get_window_geometry(hwnd)
    session_click_x = session_click_x_for_geometry(geometry)
    search_x, search_y = sidebar_search_input_focus_point_for_geometry(geometry)
    _sidecar_timing_finish(timing, "open_chat_geometry", geometry_started)
    surface_started = _sidecar_timing_start(timing, "open_chat_surface")
    surface = target_switch_surface_state(screenshot, ocr_items, geometry=geometry, target=target)
    _sidecar_timing_finish(timing, "open_chat_surface", surface_started)
    if not surface.get("ok"):
        return finish(False, str(surface.get("reason") or "surface_not_ok"))
    if not ocr_items:
        # OCR unavailable is not permission to probe the UI. Searching/clicking
        # blindly after an unreadable screenshot is a high-risk RPA pattern.
        return finish(False, "no_ocr_items")
    clean_session_key = str(session_key or "").strip()
    active_match_started = _sidecar_timing_start(timing, "open_chat_active_match")
    active_matches = active_chat_matches(ocr_items, screenshot.size, target=target, exact=exact)
    _sidecar_timing_finish(timing, "open_chat_active_match", active_match_started)
    timing["open_chat_initial_active_match"] = bool(active_matches)
    if not clean_session_key and active_matches:
        return finish(True, "active_target_match")
    if (
        clean_session_key
        and str(_LAST_RPA_ACTION_STATE.get("active_session_key") or "") == clean_session_key
        and active_matches
    ):
        return finish(True, "active_session_key_match")
    parse_started = _sidecar_timing_start(timing, "open_chat_parse_sessions")
    sessions = parse_sessions_from_ocr(ocr_items, screenshot.size, screenshot=screenshot)
    _sidecar_timing_finish(timing, "open_chat_parse_sessions", parse_started)
    timing["open_chat_session_count"] = len(sessions)
    if clean_session_key and active_matches:
        if visible_session_name_is_unambiguous(sessions, target, exact=exact):
            _LAST_RPA_ACTION_STATE["active_session_key"] = clean_session_key
            _LAST_RPA_ACTION_STATE["active_target"] = target
            return finish(True, "active_visible_unambiguous")
        return finish(False, "active_visible_ambiguous")
    if clean_session_key:
        find_started = _sidecar_timing_start(timing, "open_chat_find_session_key")
        keyed = find_session_candidate_by_key(sessions, clean_session_key)
        _sidecar_timing_finish(timing, "open_chat_find_session_key", find_started)
        if keyed is None:
            return finish(False, "session_key_candidate_not_found")
        activation_started = _sidecar_timing_start(timing, "open_chat_activate_session")
        opened = activate_session_candidate(
            hwnd,
            keyed,
            target=target,
            exact=exact,
            geometry=geometry,
            default_click_x=session_click_x,
            artifact_dir=artifact_dir,
        )
        _sidecar_timing_finish(timing, "open_chat_activate_session", activation_started)
        _sidecar_timing_merge_prefixed(timing, "open_chat", _LAST_SESSION_ACTIVATION_TIMING)
        if opened:
            _LAST_RPA_ACTION_STATE["active_session_key"] = clean_session_key
            _LAST_RPA_ACTION_STATE["active_target"] = target
        return finish(opened, "session_key_candidate_activated" if opened else "session_key_candidate_not_confirmed")
    for item in sessions:
        if not session_name_matches(str(item.get("name") or ""), target, exact=exact):
            continue
        activation_started = _sidecar_timing_start(timing, "open_chat_activate_session")
        opened = activate_session_candidate(
            hwnd,
            item,
            target=target,
            exact=exact,
            geometry=geometry,
            default_click_x=session_click_x,
            artifact_dir=artifact_dir,
        )
        _sidecar_timing_finish(timing, "open_chat_activate_session", activation_started)
        _sidecar_timing_merge_prefixed(timing, "open_chat", _LAST_SESSION_ACTIVATION_TIMING)
        return finish(opened, "name_candidate_activated" if opened else "name_candidate_not_confirmed")

    if not target_search_fallback_enabled():
        return finish(False, "visible_candidate_not_found")

    # Search is the highest-risk cross-chat path. Do it at most once per open,
    # then click a visible OCR result instead of blindly pressing Enter/Down.
    search_clear_started = _sidecar_timing_start(timing, "open_chat_search_clear")
    clear_result = clear_sidebar_search_box_without_select_all(
        hwnd,
        search_x,
        search_y,
        target_hint=target,
        geometry=geometry,
        artifact_dir=artifact_dir,
    )
    _sidecar_timing_finish(timing, "open_chat_search_clear", search_clear_started)
    timing["open_chat_search_clear_result"] = clear_result
    if not clear_result.get("ok"):
        return finish(False, str(clear_result.get("reason") or "search_clear_failed"))
    search_input_started = _sidecar_timing_start(timing, "open_chat_search_input")
    input_result = type_sidebar_search_query(hwnd, target)
    _sidecar_timing_finish(timing, "open_chat_search_input", search_input_started)
    timing["open_chat_search_input_result"] = input_result
    if not input_result.get("ok"):
        dismiss_started = _sidecar_timing_start(timing, "open_chat_search_input_failed_dismiss")
        dismiss_result = dismiss_sidebar_search_state(
            hwnd,
            target_hint=target,
            geometry=geometry,
            artifact_dir=artifact_dir,
        )
        _sidecar_timing_finish(timing, "open_chat_search_input_failed_dismiss", dismiss_started)
        timing["open_chat_search_input_failed_dismiss_result"] = dismiss_result
        return finish(False, str(input_result.get("reason") or "search_input_failed"))
    search_wait_started = _sidecar_timing_start(timing, "open_chat_search_wait")
    time.sleep(random.uniform(1.2, 2.4))
    _sidecar_timing_finish(timing, "open_chat_search_wait", search_wait_started)
    search_capture_started = _sidecar_timing_start(timing, "open_chat_search_capture_ocr")
    search_shot, search_path = capture_wechat(hwnd, artifact_dir=artifact_dir, label="open_chat_search_results")
    search_items = run_ocr_traced(search_shot, "open_chat_search_results", source="open_chat")
    _sidecar_timing_finish(timing, "open_chat_search_capture_ocr", search_capture_started)
    search_surface_started = _sidecar_timing_start(timing, "open_chat_search_surface")
    surface = target_switch_surface_state(
        search_shot,
        search_items,
        geometry=geometry,
        screenshot_path=search_path,
        target=target,
    )
    _sidecar_timing_finish(timing, "open_chat_search_surface", search_surface_started)
    if not surface.get("ok"):
        return finish(False, str(surface.get("reason") or "search_surface_not_ok"))
    if not search_items:
        return finish(False, "search_no_ocr_items")
    search_active_started = _sidecar_timing_start(timing, "open_chat_search_active_match")
    search_active_matches = active_chat_matches(search_items, search_shot.size, target=target, exact=exact)
    _sidecar_timing_finish(timing, "open_chat_search_active_match", search_active_started)
    if search_active_matches:
        dismiss_started = _sidecar_timing_start(timing, "open_chat_search_active_match_dismiss")
        dismiss_result = dismiss_sidebar_search_state(
            hwnd,
            target_hint=target,
            geometry=geometry,
            artifact_dir=artifact_dir,
        )
        _sidecar_timing_finish(timing, "open_chat_search_active_match_dismiss", dismiss_started)
        timing["open_chat_search_active_match_dismiss_result"] = dismiss_result
        if not dismiss_result.get("ok"):
            return finish(False, str(dismiss_result.get("reason") or "search_dismiss_failed_after_active_match"))
        return finish(True, "search_active_target_match")
    search_parse_started = _sidecar_timing_start(timing, "open_chat_search_parse_sessions")
    search_sessions = parse_sessions_from_ocr(search_items, search_shot.size, screenshot=search_shot)
    _sidecar_timing_finish(timing, "open_chat_search_parse_sessions", search_parse_started)
    timing["open_chat_search_session_count"] = len(search_sessions)
    for item in search_sessions:
        if not session_name_matches(str(item.get("name") or ""), target, exact=exact):
            continue
        activation_started = _sidecar_timing_start(timing, "open_chat_search_activate_session")
        opened = activate_session_candidate(
            hwnd,
            item,
            target=target,
            exact=exact,
            geometry=geometry,
            default_click_x=session_click_x,
            artifact_dir=artifact_dir,
        )
        _sidecar_timing_finish(timing, "open_chat_search_activate_session", activation_started)
        _sidecar_timing_merge_prefixed(timing, "open_chat_search", _LAST_SESSION_ACTIVATION_TIMING)
        if not opened:
            dismiss_started = _sidecar_timing_start(timing, "open_chat_search_unconfirmed_dismiss")
            dismiss_result = dismiss_sidebar_search_state(
                hwnd,
                target_hint=target,
                geometry=geometry,
                artifact_dir=artifact_dir,
            )
            _sidecar_timing_finish(timing, "open_chat_search_unconfirmed_dismiss", dismiss_started)
            timing["open_chat_search_unconfirmed_dismiss_result"] = dismiss_result
        return finish(opened, "search_candidate_activated" if opened else "search_candidate_not_confirmed")

    if target_search_enter_fallback_enabled():
        search_enter_started = _sidecar_timing_start(timing, "open_chat_search_enter")
        key_press(win32con.VK_RETURN)
        time.sleep(random.uniform(0.45, 0.7))
        validation = validate_active_send_target(hwnd, target, exact=exact, artifact_dir=artifact_dir)
        _sidecar_timing_finish(timing, "open_chat_search_enter", search_enter_started)
        if active_send_guard_is_strong(validation):
            return finish(True, "search_enter_confirmed")
        if target_switch_validation_is_hard_stop(validation):
            return finish(False, "search_enter_hard_stop")

    if not target_search_retry_after_search_enabled():
        dismiss_started = _sidecar_timing_start(timing, "open_chat_search_dismiss")
        dismiss_result = dismiss_sidebar_search_state(
            hwnd,
            target_hint=target,
            geometry=geometry,
            artifact_dir=artifact_dir,
        )
        _sidecar_timing_finish(timing, "open_chat_search_dismiss", dismiss_started)
        timing["open_chat_search_dismiss_result"] = dismiss_result
        return finish(False, "target_not_found_after_single_search_attempt")

    # Re-scan and try a direct sidebar click once more after search.
    retry_capture_started = _sidecar_timing_start(timing, "open_chat_retry_capture_ocr")
    retry_shot, _retry_path = capture_wechat(hwnd, artifact_dir=artifact_dir, label="open_chat_retry")
    retry_items = run_ocr_traced(retry_shot, "open_chat_retry", source="open_chat")
    _sidecar_timing_finish(timing, "open_chat_retry_capture_ocr", retry_capture_started)
    retry_surface_started = _sidecar_timing_start(timing, "open_chat_retry_surface")
    surface = target_switch_surface_state(retry_shot, retry_items, geometry=geometry, target=target)
    _sidecar_timing_finish(timing, "open_chat_retry_surface", retry_surface_started)
    if not surface.get("ok"):
        return finish(False, str(surface.get("reason") or "retry_surface_not_ok"))
    if not retry_items:
        return finish(False, "retry_no_ocr_items")
    retry_parse_started = _sidecar_timing_start(timing, "open_chat_retry_parse_sessions")
    retry_sessions = parse_sessions_from_ocr(retry_items, retry_shot.size, screenshot=retry_shot)
    _sidecar_timing_finish(timing, "open_chat_retry_parse_sessions", retry_parse_started)
    timing["open_chat_retry_session_count"] = len(retry_sessions)
    for item in retry_sessions:
        if not session_name_matches(str(item.get("name") or ""), target, exact=exact):
            continue
        activation_started = _sidecar_timing_start(timing, "open_chat_retry_activate_session")
        opened = activate_session_candidate(
            hwnd,
            item,
            target=target,
            exact=exact,
            geometry=geometry,
            default_click_x=session_click_x,
            artifact_dir=artifact_dir,
        )
        _sidecar_timing_finish(timing, "open_chat_retry_activate_session", activation_started)
        _sidecar_timing_merge_prefixed(timing, "open_chat_retry", _LAST_SESSION_ACTIVATION_TIMING)
        if not opened:
            dismiss_started = _sidecar_timing_start(timing, "open_chat_retry_unconfirmed_dismiss")
            dismiss_result = dismiss_sidebar_search_state(
                hwnd,
                target_hint=target,
                geometry=geometry,
                artifact_dir=artifact_dir,
            )
            _sidecar_timing_finish(timing, "open_chat_retry_unconfirmed_dismiss", dismiss_started)
            timing["open_chat_retry_unconfirmed_dismiss_result"] = dismiss_result
        return finish(opened, "retry_candidate_activated" if opened else "retry_candidate_not_confirmed")
    dismiss_started = _sidecar_timing_start(timing, "open_chat_retry_search_dismiss")
    dismiss_result = dismiss_sidebar_search_state(
        hwnd,
        target_hint=target,
        geometry=geometry,
        artifact_dir=artifact_dir,
    )
    _sidecar_timing_finish(timing, "open_chat_retry_search_dismiss", dismiss_started)
    timing["open_chat_retry_search_dismiss_result"] = dismiss_result
    return finish(False, "target_not_found_after_retry")


def ensure_target_ready_for_send(
    hwnd: int,
    target: str,
    *,
    exact: bool,
    artifact_dir: str | None = None,
    max_attempts: int | None = None,
    session_key: str = "",
) -> dict[str, Any]:
    timing: dict[str, Any] = {}
    target_ready_internal_started = _sidecar_timing_start(timing, "target_ready_internal")

    def finish(payload: dict[str, Any]) -> dict[str, Any]:
        _sidecar_timing_finish(timing, "target_ready_internal", target_ready_internal_started)
        payload["timing"] = dict(timing)
        return payload

    attempts = target_ready_attempt_count(max_attempts)
    last_validation: dict[str, Any] = {}
    clean_session_key = str(session_key or "").strip()
    for attempt in range(1, attempts + 1):
        timing["target_ready_attempts_observed"] = attempt
        # Fast path: when we are already on the correct chat, avoid the extra
        # open-chat traversal and send immediately after a strong title guard.
        # Weak/sidebar/body matches are not enough to authorize typing because
        # multi-session/group chats may show the target name inside the body.
        pre_validation_started = _sidecar_timing_start(timing, "target_ready_pre_validation")
        pre_validation = validate_active_send_target(hwnd, target, exact=exact, artifact_dir=artifact_dir)
        _sidecar_timing_finish(timing, "target_ready_pre_validation", pre_validation_started)
        _sidecar_timing_merge_validation(timing, "target_ready_pre_validation", pre_validation)
        if pre_validation.get("ok") and active_send_guard_is_strong(pre_validation):
            opened_by_session_confirm = False
            if clean_session_key:
                cached_session_match = str(_LAST_RPA_ACTION_STATE.get("active_session_key") or "") == clean_session_key
                timing["target_ready_session_cache_match"] = bool(cached_session_match)
                if not cached_session_match:
                    session_open_started = _sidecar_timing_start(timing, "target_ready_session_open_chat")
                    opened = open_chat(hwnd, target, exact=exact, artifact_dir=artifact_dir, session_key=clean_session_key)
                    _sidecar_timing_finish(timing, "target_ready_session_open_chat", session_open_started)
                    _sidecar_timing_merge_prefixed(timing, "target_ready_session", _LAST_OPEN_CHAT_TIMING)
                    if not opened:
                        return finish({
                            "ok": False,
                            "attempts": attempt,
                            "validation": pre_validation,
                            "opened": False,
                            "reason": "session_key_not_confirmed_by_active_cache",
                        })
                    opened_by_session_confirm = bool(opened)
                    session_validation_started = _sidecar_timing_start(timing, "target_ready_session_post_validation")
                    cached_validation = consume_recent_target_switch_validation(
                        hwnd=hwnd,
                        target=target,
                        exact=exact,
                        session_key=clean_session_key,
                    )
                    if isinstance(cached_validation, dict):
                        validation = cached_validation
                        timing["target_ready_session_confirm_pause_skipped"] = True
                        timing["target_ready_session_post_validation_reused"] = True
                    else:
                        session_pause_started = _sidecar_timing_start(timing, "target_ready_session_confirm_pause")
                        humanized_action_sleep(180, 320)
                        _sidecar_timing_finish(timing, "target_ready_session_confirm_pause", session_pause_started)
                        validation = validate_active_send_target(hwnd, target, exact=exact, artifact_dir=artifact_dir)
                        timing["target_ready_session_confirm_pause_skipped"] = False
                        timing["target_ready_session_post_validation_reused"] = False
                    _sidecar_timing_finish(timing, "target_ready_session_post_validation", session_validation_started)
                    _sidecar_timing_merge_validation(timing, "target_ready_session_post_validation", validation)
                    if not validation.get("ok") or not active_send_guard_is_strong(validation):
                        return finish({"ok": False, "attempts": attempt, "validation": validation, "opened": True})
                    pre_validation = validation
                _LAST_RPA_ACTION_STATE["active_session_key"] = clean_session_key
                _LAST_RPA_ACTION_STATE["active_target"] = target
            return finish({"ok": True, "attempts": attempt, "validation": pre_validation, "opened": opened_by_session_confirm})
        last_validation = pre_validation
        if target_switch_validation_is_hard_stop(pre_validation):
            return finish({"ok": False, "attempts": attempt, "validation": pre_validation, "hard_stop": True})

        open_chat_started = _sidecar_timing_start(timing, "target_ready_open_chat")
        opened = open_chat(hwnd, target, exact=exact, artifact_dir=artifact_dir, session_key=clean_session_key)
        _sidecar_timing_finish(timing, "target_ready_open_chat", open_chat_started)
        _sidecar_timing_merge_prefixed(timing, "target_ready", _LAST_OPEN_CHAT_TIMING)
        post_open_validation_started = _sidecar_timing_start(timing, "target_ready_post_open_validation")
        cached_validation = (
            consume_recent_target_switch_validation(
                hwnd=hwnd,
                target=target,
                exact=exact,
                session_key=clean_session_key,
            )
            if opened
            else None
        )
        if isinstance(cached_validation, dict):
            validation = cached_validation
            timing["target_ready_post_open_pause_skipped"] = True
            timing["target_ready_post_open_validation_reused"] = True
        else:
            post_open_pause_started = _sidecar_timing_start(timing, "target_ready_post_open_pause")
            humanized_action_sleep(280 + attempt * 90, 440 + attempt * 150)
            _sidecar_timing_finish(timing, "target_ready_post_open_pause", post_open_pause_started)
            validation = validate_active_send_target(hwnd, target, exact=exact, artifact_dir=artifact_dir)
            timing["target_ready_post_open_pause_skipped"] = False
            timing["target_ready_post_open_validation_reused"] = False
        _sidecar_timing_finish(timing, "target_ready_post_open_validation", post_open_validation_started)
        _sidecar_timing_merge_validation(timing, "target_ready_post_open_validation", validation)
        if validation.get("ok") and active_send_guard_is_strong(validation):
            return finish({"ok": True, "attempts": attempt, "validation": validation, "opened": bool(opened)})
        last_validation = validation
        if target_switch_validation_is_hard_stop(validation):
            return finish({"ok": False, "attempts": attempt, "validation": validation, "hard_stop": True})
        # Do not loop back into another open_chat/candidate click after a
        # failed target switch.  In recent WeChat builds, clicking the already
        # selected left-session row a second time can collapse/hide the chat
        # bubble pane.  Treat the first unconfirmed switch as a safe failure and
        # let the scheduler retry in a later low-frequency round.
        return finish({
            "ok": False,
            "attempts": attempt,
            "validation": last_validation,
            "opened": bool(opened),
            "reason": "target_not_confirmed_after_single_switch_attempt",
            "double_click_guard": True,
        })
    return finish({"ok": False, "attempts": attempts, "validation": last_validation})


def active_send_guard_is_strong(validation: dict[str, Any] | None) -> bool:
    if not isinstance(validation, dict) or validation.get("ok") is not True:
        return False
    confidence = str(validation.get("confirmation_confidence") or "")
    return confidence in {"active_title_strict"}


def validate_active_send_target(
    hwnd: int,
    target: str,
    *,
    exact: bool,
    artifact_dir: str | None = None,
) -> dict[str, Any]:
    timing: dict[str, Any] = {}
    ocr_trace_token = _ocr_trace_start()
    validation_started = _sidecar_timing_start(timing, "validate_active_send_target")

    def finish(payload: dict[str, Any]) -> dict[str, Any]:
        _sidecar_timing_finish(timing, "validate_active_send_target", validation_started)
        _sidecar_timing_merge_ocr_trace(timing, "validate_active_send_target", _ocr_trace_finish(ocr_trace_token))
        payload["timing"] = dict(timing)
        return payload

    geometry_started = _sidecar_timing_start(timing, "validate_active_send_target_geometry")
    geometry = get_window_geometry(hwnd)
    geometry_check = validate_send_geometry(geometry)
    _sidecar_timing_finish(timing, "validate_active_send_target_geometry", geometry_started)
    timing["validate_active_send_target_geometry_ok"] = bool(geometry_check.get("ok"))
    if not geometry_check.get("ok"):
        return finish({**geometry_check, "online": True, "geometry": geometry})
    capture_started = _sidecar_timing_start(timing, "validate_active_send_target_capture")
    screenshot, path = capture_wechat(hwnd, artifact_dir=artifact_dir, label="send_guard")
    _sidecar_timing_finish(timing, "validate_active_send_target_capture", capture_started)
    timing["validate_active_send_target_screenshot_width"] = int(getattr(screenshot, "size", (0, 0))[0] or 0)
    timing["validate_active_send_target_screenshot_height"] = int(getattr(screenshot, "size", (0, 0))[1] or 0)
    ocr_started = _sidecar_timing_start(timing, "validate_active_send_target_ocr")
    ocr_items, ocr_source, roi_blank_render = run_ocr_for_active_send_target(
        screenshot,
        target=target,
        exact=exact,
        geometry=geometry,
        timing=timing,
    )
    _sidecar_timing_finish(timing, "validate_active_send_target_ocr", ocr_started)
    timing["validate_active_send_target_ocr_count"] = len(ocr_items)
    timing["validate_active_send_target_ocr_source"] = ocr_source
    if not ocr_items:
        blank_started = _sidecar_timing_start(timing, "validate_active_send_target_blank_render")
        blank_render = roi_blank_render or detect_blank_render(screenshot, ocr_items, geometry=geometry)
        _sidecar_timing_finish(timing, "validate_active_send_target_blank_render", blank_started)
        timing["validate_active_send_target_blank_render_detected"] = bool(blank_render.get("detected"))
        if blank_render.get("detected"):
            return finish({
                "ok": False,
                "online": False,
                "reason": "blank_render",
                "state": "blank_render_detected",
                "geometry": geometry,
                "screenshot_path": path,
                "render_probe": blank_render,
                "error": "WeChat render is blank; block blind send and recover the window before automation.",
            })
        if allow_blind_target_confirmation(target):
            return finish({
                "ok": True,
                "online": True,
                "reason": "target_confirm_skipped_no_ocr",
                "blind_send": True,
                "requested_target": target,
                "confirmed_target": "",
                "confirmation_confidence": "none",
                "geometry": geometry,
                "screenshot_path": path,
            })
        return finish({
            "ok": False,
            "online": True,
            "reason": "ocr_capture_unavailable",
            "requested_target": target,
            "confirmed_target": "",
            "confirmation_confidence": "none",
            "geometry": geometry,
            "screenshot_path": path,
            "error": "No OCR text was captured from WeChat; target confirmation is unavailable.",
        })
    quick_login_started = _sidecar_timing_start(timing, "validate_active_send_target_quick_login")
    quick_login_detected = quick_login_like(ocr_items, geometry=geometry)
    _sidecar_timing_finish(timing, "validate_active_send_target_quick_login", quick_login_started)
    timing["validate_active_send_target_quick_login_detected"] = bool(quick_login_detected)
    if quick_login_detected:
        return finish({
            "ok": False,
            "online": False,
            "reason": "login_or_qr",
            "state": "login_window_detected",
            "geometry": geometry,
            "screenshot_path": path,
            "error": "WeChat quick-login view detected; enter WeChat before sending.",
        })
    auxiliary_started = _sidecar_timing_start(timing, "validate_active_send_target_auxiliary_shell")
    auxiliary_shell = auxiliary_wechat_shell_like(ocr_items, geometry=geometry)
    _sidecar_timing_finish(timing, "validate_active_send_target_auxiliary_shell", auxiliary_started)
    timing["validate_active_send_target_auxiliary_shell_detected"] = bool(auxiliary_shell.get("detected"))
    if auxiliary_shell.get("detected"):
        return finish({
            "ok": False,
            "online": False,
            "reason": "auxiliary_shell_window",
            "state": "auxiliary_shell_window_detected",
            "geometry": geometry,
            "screenshot_path": path,
            "shell_probe": auxiliary_shell,
            "error": "Selected WeChat window looks like an auxiliary shell, not the requested chat.",
        })
    blocking_started = _sidecar_timing_start(timing, "validate_active_send_target_blocking_screen")
    blocking_reason = blocking_screen_reason(ocr_items)
    _sidecar_timing_finish(timing, "validate_active_send_target_blocking_screen", blocking_started)
    timing["validate_active_send_target_blocking_detected"] = bool(blocking_reason)
    if blocking_reason:
        return finish({
            "ok": False,
            "online": False if blocking_reason in {"login_or_qr"} else True,
            "reason": blocking_reason,
            "geometry": geometry,
            "screenshot_path": path,
            "error": f"WeChat send guard found blocking screen: {blocking_reason}",
        })
    service_container_started = _sidecar_timing_start(timing, "validate_active_send_target_service_container")
    service_container = active_service_container_wrong_target(
        ocr_items,
        getattr(screenshot, "size", (0, 0)),
        target=target,
    )
    _sidecar_timing_finish(timing, "validate_active_send_target_service_container", service_container_started)
    timing["validate_active_send_target_service_container_detected"] = bool(service_container.get("detected"))
    if service_container.get("detected"):
        return finish({
            "ok": False,
            "online": True,
            "reason": "service_container_wrong_target",
            "state": "wrong_target_service_container_detected",
            "requested_target": target,
            "confirmed_target": str((service_container.get("matches") or [{}])[0].get("container") or ""),
            "confirmation_confidence": "failed_service_container",
            "geometry": geometry,
            "screenshot_path": path,
            "service_container_probe": service_container,
            "error": "The active WeChat page is a service-account container/page, not the requested chat.",
        })
    if ocr_source in {"full", "full_fallback"}:
        remember_target_ready_prevalidation_ocr_seed(
            hwnd=hwnd,
            target=target,
            exact=exact,
            screenshot=screenshot,
            ocr_items=ocr_items,
            geometry=geometry,
            screenshot_path=path,
        )
    active_match_started = _sidecar_timing_start(timing, "validate_active_send_target_active_match")
    active_match = active_chat_matches(ocr_items, screenshot.size, target=target, exact=exact)
    _sidecar_timing_finish(timing, "validate_active_send_target_active_match", active_match_started)
    timing["validate_active_send_target_active_match"] = bool(active_match)
    if not active_match:
        blind_guard_started = _sidecar_timing_start(timing, "validate_active_send_target_blind_guard")
        blind_guard = blind_target_confirmation_guard(
            target=target,
            exact=exact,
            ocr_items=ocr_items,
            image_size=screenshot.size,
            geometry=geometry,
            screenshot_path=path,
        )
        _sidecar_timing_finish(timing, "validate_active_send_target_blind_guard", blind_guard_started)
        timing["validate_active_send_target_blind_guard_ok"] = bool(blind_guard.get("ok"))
        if blind_guard.get("ok"):
            return finish(blind_guard)
        return finish({
            "ok": False,
            "online": True,
            "reason": "target_title_not_confirmed",
            "requested_target": target,
            "confirmed_target": "",
            "confirmation_confidence": "failed",
            "geometry": geometry,
            "screenshot_path": path,
            "error": "The active chat title did not match the requested target.",
        })
    remember_input_region_precheck_ocr_seed(
        hwnd=hwnd,
        target=target,
        exact=exact,
        screenshot=screenshot,
        ocr_items=ocr_items,
        geometry=geometry,
        screenshot_path=path,
    )
    return finish({
        "ok": True,
        "online": True,
        "reason": "target_confirmed",
        "requested_target": target,
        "confirmed_target": target,
        "confirmation_confidence": "active_title_strict",
        "geometry": geometry,
        "screenshot_path": path,
    })


def validate_post_send_target(
    hwnd: int,
    target: str,
    *,
    exact: bool,
    artifact_dir: str | None = None,
) -> dict[str, Any]:
    """Lightweight post-send guard.

    Pre-send already enforces strict target confirmation. Post-send primarily
    needs to detect hard failures (blank render / lost window). We therefore
    use a fast path first and only fall back to strict OCR confirmation when
    the fast probe is inconclusive.
    """
    if env_flag(
        "WECHAT_WIN32_OCR_POST_SEND_STRICT_CONFIRM",
        default=DEFAULT_POST_SEND_STRICT_CONFIRM,
    ):
        return validate_active_send_target(hwnd, target, exact=exact, artifact_dir=artifact_dir)

    geometry = get_window_geometry(hwnd)
    geometry_check = validate_send_geometry(geometry)
    if not geometry_check.get("ok"):
        return {**geometry_check, "online": True, "geometry": geometry}

    focus_guard = basic_send_window_guard(hwnd)
    if not focus_guard.get("ok"):
        return validate_active_send_target(hwnd, target, exact=exact, artifact_dir=artifact_dir)

    try:
        screenshot, path = capture_wechat(hwnd, artifact_dir=artifact_dir, label="send_post_guard_fast")
    except Exception:
        return validate_active_send_target(hwnd, target, exact=exact, artifact_dir=artifact_dir)

    blank_render = detect_blank_render(screenshot, [], geometry=geometry)
    if blank_render.get("detected"):
        return {
            "ok": False,
            "online": False,
            "reason": "blank_render",
            "state": "blank_render_detected",
            "geometry": geometry,
            "screenshot_path": path,
            "render_probe": blank_render,
            "error": "WeChat render is blank after send.",
        }
    return {
        "ok": True,
        "online": True,
        "reason": "send_window_readable_after_send",
        "requested_target": target,
        "confirmed_target": "",
        "confirmation_confidence": "post_send_window_probe_only",
        "geometry": geometry,
        "screenshot_path": path,
        "post_send_fast_guard": True,
    }


def get_window_geometry(hwnd: int) -> dict[str, int]:
    return win32_ocr_window_metrics.get_window_geometry(hwnd, win32gui_module=win32gui)


def get_window_client_geometry(hwnd: int) -> dict[str, int]:
    return win32_ocr_window_metrics.get_window_client_geometry(hwnd, win32gui_module=win32gui)


def add_friend_device_profile(hwnd: int, *, geometry: dict[str, Any] | None = None, screenshot_size: tuple[int, int] | None = None, route: str = '') -> dict[str, Any]:
    win32_ocr_add_friend_windows.bind_sidecar_ops(sys.modules[__name__])
    return win32_ocr_add_friend_windows.add_friend_device_profile(hwnd, geometry=geometry, screenshot_size=screenshot_size, route=route)


def validate_capture_geometry(geometry: dict[str, Any]) -> dict[str, Any]:
    return win32_ocr_geometry.validate_capture_geometry(geometry)


def validate_send_geometry(geometry: dict[str, Any]) -> dict[str, Any]:
    return win32_ocr_geometry.validate_send_geometry(geometry)


def calculate_send_points(geometry: dict[str, Any]) -> dict[str, Any]:
    return win32_ocr_geometry.calculate_send_points(geometry)


def _spread_points_in_rect(
    left: int,
    top: int,
    right: int,
    bottom: int,
    *,
    min_points: int = 10,
) -> list[tuple[int, int]]:
    return win32_ocr_geometry._spread_points_in_rect(left, top, right, bottom, min_points=min_points)


def input_click_candidate_points(geometry: dict[str, Any], *, min_points: int = 10) -> list[tuple[int, int]]:
    return win32_ocr_geometry.input_click_candidate_points(geometry, min_points=min_points)


def send_click_candidate_points(geometry: dict[str, Any], *, min_points: int = 10) -> list[tuple[int, int]]:
    return win32_ocr_geometry.send_click_candidate_points(geometry, min_points=min_points)


def jitter_input_click_point(x: int, y: int, geometry: dict[str, Any]) -> tuple[int, int]:
    width = int(geometry.get("width") or 0)
    height = int(geometry.get("height") or 0)
    if width <= 0 or height <= 0:
        return int(x), int(y)
    candidates = input_click_candidate_points(geometry, min_points=10)
    if candidates:
        x, y = random.choice(candidates)
    jitter_x = bounded_int(
        os.getenv("WECHAT_WIN32_OCR_INPUT_POINT_JITTER_X"),
        default=24,
        minimum=0,
        maximum=60,
    )
    jitter_y = bounded_int(
        os.getenv("WECHAT_WIN32_OCR_INPUT_POINT_JITTER_Y"),
        default=14,
        minimum=0,
        maximum=36,
    )
    split_x = session_split_x(width)
    safe_min_x = max(split_x + 64, int(width * 0.55) + 1)
    safe_max_x = max(safe_min_x, width - 88)
    safe_min_y = max(int(height * 0.84), height - 126)
    safe_max_y = max(safe_min_y, height - 76)
    jittered_x = bounded_int(
        int(x) + random.randint(-jitter_x, jitter_x),
        default=int(x),
        minimum=safe_min_x,
        maximum=safe_max_x,
    )
    jittered_y = bounded_int(
        int(y) + random.randint(-jitter_y, jitter_y),
        default=int(y),
        minimum=safe_min_y,
        maximum=safe_max_y,
    )
    return jittered_x, jittered_y


def rpa_click_surface_jitter_enabled() -> bool:
    return env_flag("WECHAT_WIN32_OCR_CLICK_SURFACE_JITTER_ENABLED", default=True)


def jitter_client_click_surface_point(hwnd: int, x: int, y: int) -> tuple[int, int, dict[str, Any]]:
    """Apply a final low-risk spread so fixed caller coordinates do not leak through."""
    original_x = int(x)
    original_y = int(y)
    if not rpa_click_surface_jitter_enabled():
        return original_x, original_y, {"enabled": False, "original": [original_x, original_y], "final": [original_x, original_y]}
    role = "generic"
    jitter_x = 3
    jitter_y = 2
    min_x = 0
    min_y = 0
    max_x = max(0, original_x + jitter_x)
    max_y = max(0, original_y + jitter_y)
    try:
        geometry = get_window_geometry(hwnd)
        width = int(geometry.get("width") or 0)
        height = int(geometry.get("height") or 0)
        if width > 0 and height > 0:
            split_x = session_split_x(width)
            max_x = max(0, width - 1)
            max_y = max(0, height - 1)
            if original_x > split_x + 40 and original_y > int(height * 0.70):
                role = "input_area"
                jitter_x = bounded_int(
                    os.getenv("WECHAT_WIN32_OCR_CLICK_SURFACE_INPUT_JITTER_X"),
                    default=12,
                    minimum=0,
                    maximum=36,
                )
                jitter_y = bounded_int(
                    os.getenv("WECHAT_WIN32_OCR_CLICK_SURFACE_INPUT_JITTER_Y"),
                    default=7,
                    minimum=0,
                    maximum=20,
                )
                min_x = max(split_x + 35, int(width * 0.48))
                max_x = min(width - 78, max(min_x, original_x + max(jitter_x, 1)))
                min_y = max(int(height * 0.73), height - 228)
                max_y = min(height - 82, max(min_y, original_y + max(jitter_y, 1)))
            elif original_x < split_x and original_y > 86:
                role = "session_or_sidebar"
                jitter_x = bounded_int(
                    os.getenv("WECHAT_WIN32_OCR_CLICK_SURFACE_SESSION_JITTER_X"),
                    default=9,
                    minimum=0,
                    maximum=24,
                )
                jitter_y = bounded_int(
                    os.getenv("WECHAT_WIN32_OCR_CLICK_SURFACE_SESSION_JITTER_Y"),
                    default=6,
                    minimum=0,
                    maximum=16,
                )
                min_x = 65
                max_x = max(min_x, min(split_x - 38, original_x + max(jitter_x, 1)))
                min_y = 84
                max_y = max(min_y, min(height - 20, original_y + max(jitter_y, 1)))
            elif original_x < split_x and original_y <= 86:
                role = "search_or_header"
                jitter_x = bounded_int(
                    os.getenv("WECHAT_WIN32_OCR_CLICK_SURFACE_HEADER_JITTER_X"),
                    default=3,
                    minimum=0,
                    maximum=8,
                )
                jitter_y = bounded_int(
                    os.getenv("WECHAT_WIN32_OCR_CLICK_SURFACE_HEADER_JITTER_Y"),
                    default=2,
                    minimum=0,
                    maximum=5,
                )
                min_x = 70
                max_x = max(min_x, min(split_x - 28, original_x + max(jitter_x, 1)))
                min_y = 38
                max_y = max(min_y, min(88, original_y + max(jitter_y, 1)))
    except Exception:
        pass
    final_x = bounded_int(
        original_x + random.randint(-jitter_x, jitter_x),
        default=original_x,
        minimum=max(0, min_x),
        maximum=max(max_x, min_x),
    )
    final_y = bounded_int(
        original_y + random.randint(-jitter_y, jitter_y),
        default=original_y,
        minimum=max(0, min_y),
        maximum=max(max_y, min_y),
    )
    return final_x, final_y, {
        "enabled": True,
        "role": role,
        "original": [original_x, original_y],
        "final": [final_x, final_y],
        "jitter": [jitter_x, jitter_y],
    }


def jitter_screen_click_surface_point(x: int, y: int) -> tuple[int, int, dict[str, Any]]:
    original_x = int(x)
    original_y = int(y)
    if not rpa_click_surface_jitter_enabled():
        return original_x, original_y, {"enabled": False, "original": [original_x, original_y], "final": [original_x, original_y]}
    jitter_x = bounded_int(os.getenv("WECHAT_WIN32_OCR_SCREEN_CLICK_JITTER_X"), default=3, minimum=0, maximum=8)
    jitter_y = bounded_int(os.getenv("WECHAT_WIN32_OCR_SCREEN_CLICK_JITTER_Y"), default=2, minimum=0, maximum=6)
    final_x = max(0, original_x + random.randint(-jitter_x, jitter_x))
    final_y = max(0, original_y + random.randint(-jitter_y, jitter_y))
    return final_x, final_y, {
        "enabled": True,
        "role": "screen",
        "original": [original_x, original_y],
        "final": [final_x, final_y],
        "jitter": [jitter_x, jitter_y],
    }


def jitter_window_image_click_surface_point(hwnd: int, x: int, y: int) -> tuple[int, int, dict[str, Any]]:
    original_x = int(x)
    original_y = int(y)
    if not rpa_click_surface_jitter_enabled():
        return original_x, original_y, {"enabled": False, "original": [original_x, original_y], "final": [original_x, original_y]}
    role = "window_image"
    jitter_x = bounded_int(os.getenv("WECHAT_WIN32_OCR_WINDOW_IMAGE_CLICK_JITTER_X"), default=5, minimum=0, maximum=16)
    jitter_y = bounded_int(os.getenv("WECHAT_WIN32_OCR_WINDOW_IMAGE_CLICK_JITTER_Y"), default=4, minimum=0, maximum=12)
    min_x = 0
    min_y = 0
    max_x = max(0, original_x + jitter_x)
    max_y = max(0, original_y + jitter_y)
    try:
        geometry = get_window_geometry(hwnd)
        width = int(geometry.get("width") or 0)
        height = int(geometry.get("height") or 0)
        if width > 0 and height > 0:
            split_x = session_split_x(width)
            max_x = max(0, width - 1)
            max_y = max(0, height - 1)
            if original_x < split_x and original_y <= 92:
                role = "search_or_header_window"
                jitter_x = bounded_int(os.getenv("WECHAT_WIN32_OCR_WINDOW_IMAGE_HEADER_JITTER_X"), default=7, minimum=0, maximum=18)
                jitter_y = bounded_int(os.getenv("WECHAT_WIN32_OCR_WINDOW_IMAGE_HEADER_JITTER_Y"), default=5, minimum=0, maximum=14)
                min_x = 55
                max_x = max(min_x, min(split_x - 22, original_x + max(jitter_x, 1)))
                min_y = 34
                max_y = max(min_y, min(98, original_y + max(jitter_y, 1)))
                search_x, _search_y = search_box_point_for_geometry(geometry)
                windows_plus_x, windows_plus_y = add_friend_windows_plus_button_point_for_geometry(geometry)
                is_windows_plus_entry = (
                    abs(original_x - windows_plus_x) <= 20
                    and abs(original_y - windows_plus_y) <= 18
                    and original_x >= search_x + 130
                )
                if original_x >= split_x - 34 or is_windows_plus_entry:
                    role = "plus_entry_button"
                    jitter_x = bounded_int(os.getenv("WECHAT_WIN32_OCR_PLUS_ENTRY_JITTER_X"), default=3, minimum=0, maximum=8)
                    jitter_y = bounded_int(os.getenv("WECHAT_WIN32_OCR_PLUS_ENTRY_JITTER_Y"), default=3, minimum=0, maximum=8)
                    if is_windows_plus_entry:
                        min_x = max(55, original_x - 10)
                        max_x = min(split_x - 22, original_x + 10)
                    else:
                        min_x = max(55, split_x - 34)
                        max_x = max(min_x, min(split_x - 8, original_x + max(jitter_x, 1)))
                    min_y = max(34, original_y - 8)
                    max_y = max(min_y, min(108, original_y + max(jitter_y, 1)))
            elif original_x < split_x:
                role = "session_or_sidebar_window"
                jitter_x = bounded_int(os.getenv("WECHAT_WIN32_OCR_WINDOW_IMAGE_SESSION_JITTER_X"), default=8, minimum=0, maximum=20)
                jitter_y = bounded_int(os.getenv("WECHAT_WIN32_OCR_WINDOW_IMAGE_SESSION_JITTER_Y"), default=5, minimum=0, maximum=14)
                min_x = 65
                max_x = max(min_x, min(split_x - 30, original_x + max(jitter_x, 1)))
                min_y = 82
                max_y = max(min_y, min(height - 22, original_y + max(jitter_y, 1)))
    except Exception:
        pass
    final_x = bounded_int(
        original_x + random.randint(-jitter_x, jitter_x),
        default=original_x,
        minimum=max(0, min_x),
        maximum=max(max_x, min_x),
    )
    final_y = bounded_int(
        original_y + random.randint(-jitter_y, jitter_y),
        default=original_y,
        minimum=max(0, min_y),
        maximum=max(max_y, min_y),
    )
    return final_x, final_y, {
        "enabled": True,
        "role": role,
        "original": [original_x, original_y],
        "final": [final_x, final_y],
        "jitter": [jitter_x, jitter_y],
    }


def jitter_send_click_point(x: int, y: int, geometry: dict[str, Any]) -> tuple[int, int]:
    width = int(geometry.get("width") or 0)
    height = int(geometry.get("height") or 0)
    if width <= 0 or height <= 0:
        return int(x), int(y)
    candidates = send_click_candidate_points(geometry, min_points=10)
    if candidates:
        x, y = random.choice(candidates)
    jitter_x = bounded_int(
        os.getenv("WECHAT_WIN32_OCR_SEND_POINT_JITTER_X"),
        default=6,
        minimum=0,
        maximum=16,
    )
    jitter_y = bounded_int(
        os.getenv("WECHAT_WIN32_OCR_SEND_POINT_JITTER_Y"),
        default=5,
        minimum=0,
        maximum=14,
    )
    split_x = session_split_x(width)
    safe_min_x = max(split_x + 80, width - 132)
    safe_max_x = max(safe_min_x, width - 20)
    safe_min_y = max(int(height * 0.80), height - 92)
    safe_max_y = max(safe_min_y, height - 16)
    jittered_x = bounded_int(
        int(x) + random.randint(-jitter_x, jitter_x),
        default=int(x),
        minimum=safe_min_x,
        maximum=safe_max_x,
    )
    jittered_y = bounded_int(
        int(y) + random.randint(-jitter_y, jitter_y),
        default=int(y),
        minimum=safe_min_y,
        maximum=safe_max_y,
    )
    return jittered_x, jittered_y


def blocking_screen_reason(ocr_items: list[dict[str, Any]]) -> str:
    texts = [normalize_ocr_text(item.get("text")) for item in ocr_items if normalize_ocr_text(item.get("text"))]
    joined = "\n".join(texts)
    login_card_tokens = (
        "进入微信",
        "切换账号",
        "仅传输文件",
    )
    qr_login_tokens = (
        "扫码登录",
        "二维码登录",
        "扫描二维码登录",
        "请使用微信扫描二维码",
        "手机确认登录",
    )
    if sum(1 for token in login_card_tokens if token in joined) >= 2:
        return "login_or_qr"
    if any(token in joined for token in qr_login_tokens):
        return "login_or_qr"
    for token in HARD_BLOCKING_SCREEN_TOKENS:
        if token in joined:
            return f"blocking_text:{token}"
    chat_surface_tokens = (
        "搜索",
        "文件传输助手",
        "发送",
        "聊天",
        "通讯录",
        "订阅号",
        "朋友圈",
        "小程序",
        "视频号",
    )
    has_chat_surface = any(token in text for text in texts for token in chat_surface_tokens)
    compact_text_count = len([text for text in texts if text])
    token_items = [
        item
        for item in ocr_items
        if any(token in normalize_ocr_text(item.get("text")) for token in SOFT_BLOCKING_SCREEN_TOKENS)
    ]
    # Soft safety words can appear in normal chat bubbles. Only treat them as
    # global blockers when the capture looks like a sparse/login/dialog page,
    # not when the normal WeChat chat surface is visible behind the text.
    soft_page_like = (
        bool(token_items)
        and not has_chat_surface
        and (
            compact_text_count <= 8
            or any(180 <= float(item.get("center_y") or 0) <= 720 for item in token_items)
        )
    )
    if soft_page_like:
        for token in SOFT_BLOCKING_SCREEN_TOKENS:
            if token in joined:
                return f"blocking_text:{token}"
    return ""


def reserve_send_rate(*, target: str, text: str) -> dict[str, Any]:
    if env_flag("WECHAT_WIN32_OCR_SEND_RATE_GUARD", default=True) is False:
        return {"ok": True, "guard_disabled": True}
    now_ts = time.time()
    min_interval = env_int("WECHAT_WIN32_OCR_SEND_MIN_INTERVAL_SECONDS", DEFAULT_SEND_MIN_INTERVAL_SECONDS)
    burst_window = env_int("WECHAT_WIN32_OCR_SEND_BURST_WINDOW_SECONDS", DEFAULT_SEND_BURST_WINDOW_SECONDS)
    burst_limit = env_int("WECHAT_WIN32_OCR_SEND_BURST_LIMIT", DEFAULT_SEND_BURST_LIMIT)
    state = read_send_guard_state()
    decision = send_rate_decision(
        state,
        target=target,
        now_ts=now_ts,
        min_interval_seconds=min_interval,
        burst_window_seconds=burst_window,
        burst_limit=burst_limit,
    )
    if not decision.get("ok"):
        return decision
    events = [
        item
        for item in state.get("events", [])
        if isinstance(item, dict) and now_ts - float(item.get("at") or 0) <= max(burst_window, min_interval, 1)
    ]
    events.append(
        {
            "target": target,
            "at": now_ts,
            "text_hash": hashlib.sha1(text.encode("utf-8")).hexdigest()[:12],
        }
    )
    write_send_guard_state({"events": events, "updated_at": now_ts})
    return decision


def send_rate_decision(
    state: dict[str, Any],
    *,
    target: str,
    now_ts: float,
    min_interval_seconds: int,
    burst_window_seconds: int,
    burst_limit: int,
) -> dict[str, Any]:
    return win32_ocr_send_action_risk.send_rate_decision(
        state,
        target=target,
        now_ts=now_ts,
        min_interval_seconds=min_interval_seconds,
        burst_window_seconds=burst_window_seconds,
        burst_limit=burst_limit,
    )


def read_send_guard_state() -> dict[str, Any]:
    try:
        payload = json.loads(SEND_GUARD_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {"events": []}
    return payload if isinstance(payload, dict) else {"events": []}


def write_send_guard_state(payload: dict[str, Any]) -> None:
    SEND_GUARD_PATH.parent.mkdir(parents=True, exist_ok=True)
    temp = SEND_GUARD_PATH.with_suffix(".json.tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, SEND_GUARD_PATH)


def env_int(name: str, default: int) -> int:
    return win32_ocr_env.env_int(name, default)


def env_float(name: str, default: float) -> float:
    return win32_ocr_env.env_float(name, default)


def env_flag(name: str, *, default: bool) -> bool:
    return win32_ocr_env.env_flag(name, default=default)


def rpa_action_pacing_enabled() -> bool:
    return win32_ocr_env.rpa_action_pacing_enabled()


def ui_action_kind(action: str) -> str:
    return win32_ocr_send_action_risk.ui_action_kind(action)


def ui_action_point(metadata: dict[str, Any] | None) -> tuple[int, int] | None:
    return win32_ocr_send_action_risk.ui_action_point(metadata)


def ui_action_min_gap_ms(kind: str) -> int:
    return win32_ocr_send_action_risk.ui_action_min_gap_ms(
        kind,
        keyboard_min_gap_ms=env_int("WECHAT_WIN32_OCR_UI_ACTION_KEYBOARD_MIN_GAP_MS", DEFAULT_UI_ACTION_KEYBOARD_MIN_GAP_MS),
        scroll_min_gap_ms=env_int("WECHAT_WIN32_OCR_UI_ACTION_SCROLL_MIN_GAP_MS", DEFAULT_UI_ACTION_SCROLL_MIN_GAP_MS),
        focus_min_gap_ms=env_int("WECHAT_WIN32_OCR_UI_ACTION_FOCUS_MIN_GAP_MS", DEFAULT_UI_ACTION_FOCUS_MIN_GAP_MS),
        mouse_min_gap_ms=env_int("WECHAT_WIN32_OCR_UI_ACTION_MOUSE_MIN_GAP_MS", DEFAULT_UI_ACTION_MOUSE_MIN_GAP_MS),
        default_min_gap_ms=env_int("WECHAT_WIN32_OCR_UI_ACTION_MIN_GAP_MS", 70),
    )


def count_recent_near_point_actions(
    events: list[dict[str, Any]],
    *,
    point: tuple[int, int],
    now_ts: float,
    radius: int,
    window_seconds: float,
) -> int:
    return win32_ocr_send_action_risk.count_recent_near_point_actions(
        events,
        point=point,
        now_ts=now_ts,
        radius=radius,
        window_seconds=window_seconds,
    )


def coordinate_rpa_action(
    action: str,
    *,
    metadata: dict[str, Any] | None = None,
    recent_events: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    kind = ui_action_kind(action)
    if not rpa_action_pacing_enabled():
        return {"enabled": False, "kind": kind, "delay_ms": 0}
    now = time.time()
    plan = win32_ocr_send_action_risk.plan_rpa_action_pacing(
        action,
        metadata=metadata,
        recent_events=recent_events if isinstance(recent_events, list) else [],
        last_state=_LAST_RPA_ACTION_STATE,
        now_ts=now,
        min_gap_ms=ui_action_min_gap_ms(kind),
        kind_switch_gap_ms=env_int("WECHAT_WIN32_OCR_UI_ACTION_KIND_SWITCH_GAP_MS", DEFAULT_UI_ACTION_KIND_SWITCH_GAP_MS),
        near_point_radius_px=env_int("WECHAT_WIN32_OCR_UI_ACTION_NEAR_POINT_RADIUS_PX", DEFAULT_UI_ACTION_NEAR_POINT_RADIUS_PX),
        near_point_gap_ms=env_int("WECHAT_WIN32_OCR_UI_ACTION_NEAR_POINT_GAP_MS", DEFAULT_UI_ACTION_NEAR_POINT_GAP_MS),
        near_point_soft_limit=env_int("WECHAT_WIN32_OCR_UI_ACTION_NEAR_POINT_SOFT_LIMIT", DEFAULT_UI_ACTION_NEAR_POINT_SOFT_LIMIT),
        extra_delay_ms=lambda reason: (
            random.randint(18, 70)
            if reason == "min_gap"
            else (random.randint(90, 260) if reason == "near_point_repeat" else random.randint(240, 680))
        ),
    )
    delay_ms = int(plan.get("delay_ms") or 0)
    if delay_ms > 0:
        time.sleep(delay_ms / 1000.0)
    point = plan.get("point")
    _LAST_RPA_ACTION_STATE.update(
        {
            "ts": time.time(),
            "kind": kind,
            "action": str(action or "unknown"),
            "point": list(point) if isinstance(point, list) else None,
        }
    )
    return {
        "enabled": True,
        "kind": kind,
        "delay_ms": delay_ms,
        "reasons": plan.get("reasons") or [],
    }


def active_ui_action_budget_decision(
    *,
    action: str,
    metadata: dict[str, Any] | None = None,
    now_ts: float | None = None,
    reserve: bool = True,
) -> dict[str, Any]:
    if not env_flag("WECHAT_WIN32_OCR_UI_ACTION_BUDGET_ENABLED", default=True):
        return {"ok": True, "enabled": False, "action": action}
    now = float(now_ts if now_ts is not None else time.time())
    window_seconds = env_int(
        "WECHAT_WIN32_OCR_UI_ACTION_BUDGET_WINDOW_SECONDS",
        DEFAULT_UI_ACTION_BUDGET_WINDOW_SECONDS,
    )
    limit = env_int("WECHAT_WIN32_OCR_UI_ACTION_BUDGET_LIMIT", DEFAULT_UI_ACTION_BUDGET_LIMIT)
    window_seconds = max(1, int(window_seconds))
    limit = max(1, int(limit))
    events: list[dict[str, Any]] = []
    if UI_ACTION_GUARD_PATH.exists():
        try:
            payload = json.loads(UI_ACTION_GUARD_PATH.read_text(encoding="utf-8"))
            raw_events = payload.get("events", []) if isinstance(payload, dict) else []
            events = [item for item in raw_events if isinstance(item, dict)]
        except (OSError, json.JSONDecodeError):
            events = []
    cutoff = now - float(window_seconds)
    kept = []
    for item in events:
        try:
            ts = float(item.get("ts") or 0.0)
        except (TypeError, ValueError):
            ts = 0.0
        if ts >= cutoff:
            kept.append(item)
    pacing = coordinate_rpa_action(action, metadata=metadata, recent_events=kept) if reserve else {"enabled": rpa_action_pacing_enabled(), "kind": ui_action_kind(action), "delay_ms": 0}
    now = float(now_ts if now_ts is not None else time.time())
    cutoff = now - float(window_seconds)
    kept = [
        item
        for item in kept
        if float(item.get("ts") or 0.0) >= cutoff
    ]
    allowed = len(kept) < limit
    decision = {
        "ok": allowed,
        "enabled": True,
        "action": action,
        "count": len(kept),
        "limit": limit,
        "window_seconds": window_seconds,
        "pacing": pacing,
    }
    if reserve and allowed:
        kept.append({"ts": now, "action": str(action or "unknown"), "metadata": metadata or {}, "kind": ui_action_kind(action)})
    if reserve or len(kept) != len(events):
        try:
            UI_ACTION_GUARD_PATH.parent.mkdir(parents=True, exist_ok=True)
            UI_ACTION_GUARD_PATH.write_text(
                json.dumps({"events": kept[-max(limit * 2, limit):]}, ensure_ascii=True, indent=2),
                encoding="utf-8",
            )
        except OSError:
            pass
    return decision


def record_ui_action(action: str, *, decision: dict[str, Any] | None = None, metadata: dict[str, Any] | None = None) -> None:
    if not env_flag("WECHAT_WIN32_OCR_UI_ACTION_AUDIT_ENABLED", default=True):
        return
    payload = {
        "ts": time.time(),
        "action": str(action or "unknown"),
        "ok": bool((decision or {}).get("ok", True)),
        "decision": decision or {},
        "metadata": metadata or {},
    }
    try:
        UI_ACTION_AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
        with UI_ACTION_AUDIT_PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=True, sort_keys=True) + "\n")
    except OSError:
        return


def require_active_ui_action_budget(action: str, *, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    decision = active_ui_action_budget_decision(action=action, metadata=metadata, reserve=True)
    record_ui_action(action, decision=decision, metadata=metadata)
    if not decision.get("ok") and env_flag("WECHAT_WIN32_OCR_UI_ACTION_BUDGET_ENFORCE", default=True):
        raise RuntimeError(f"ui_action_budget_exceeded:{action}:{decision.get('count')}/{decision.get('limit')}")
    return decision


def active_chat_matches(ocr_items: list[dict[str, Any]], image_size: tuple[int, int], *, target: str, exact: bool) -> bool:
    if not target:
        return False
    normalized_target = normalize_session_name(target)
    if not normalized_target:
        return False
    width, height = image_size
    split_x = session_split_x(width)
    title_left = active_chat_title_left_x(width)
    title_right = active_chat_title_right_x(width)
    title_top = active_chat_title_top_y(height)
    title_bottom = active_chat_title_bottom_y(height)
    x_tolerance = 24
    y_tolerance = 8
    for item in ocr_items:
        text = normalize_ocr_text(item.get("text"))
        if not text:
            continue
        if item["right"] < split_x + 8:
            continue
        if item["center_x"] < title_left - x_tolerance or item["center_x"] > title_right + x_tolerance:
            continue
        if item["center_y"] < title_top - y_tolerance or item["center_y"] > title_bottom + y_tolerance:
            continue
        if item["top"] < title_top - 16 or item["bottom"] > title_bottom + 18:
            continue
        candidates = {
            text,
            strip_chat_unread_suffix(text),
            re.sub(r"^[：:.\s]+", "", text).strip(),
            normalize_chat_title_for_match(text),
        }
        for candidate in candidates:
            if session_name_matches(candidate, normalized_target, exact=exact):
                return True
    return False


def target_switch_passive_confirm_attempts() -> int:
    return bounded_int(
        os.getenv("WECHAT_WIN32_OCR_TARGET_SWITCH_PASSIVE_CONFIRM_ATTEMPTS"),
        default=2,
        minimum=1,
        maximum=4,
    )


def scroll_chat_history(hwnd: int, load_times: int, *, wheel_units: int = 8, delay_seconds: float = 0.18) -> None:
    rect = win32gui.GetWindowRect(hwnd)
    x = max(380, int((rect[2] - rect[0]) * 0.6)) + random.randint(-12, 12)
    y = max(180, int((rect[3] - rect[1]) * 0.45)) + random.randint(-10, 10)
    require_active_ui_action_budget(
        "scroll_chat_history",
        metadata={
            "load_times": int(load_times or 0),
            "cursor": [int(x), int(y)],
            "wheel_units": int(wheel_units or 0),
        },
    )
    activate_window(hwnd)
    ensure_left_button_released()
    screen_x, screen_y = win32gui.ClientToScreen(hwnd, (x, y))
    win32api.SetCursorPos((screen_x, screen_y))
    humanized_action_sleep(45, 110)
    wheel_message = getattr(win32con, "WM_MOUSEWHEEL", 0x020A)
    lparam = ((int(screen_y) & 0xFFFF) << 16) | (int(screen_x) & 0xFFFF)
    for _ in range(max(0, load_times)):
        units = max(1, int(wheel_units or 1) + random.choice([-1, 0, 1]))
        delta = int(units * 120)
        try:
            win32gui.PostMessage(hwnd, wheel_message, (delta & 0xFFFF) << 16, lparam)
        except Exception:
            win32api.mouse_event(win32con.MOUSEEVENTF_WHEEL, 0, 0, delta, 0)
        base_ms = max(0, int(float(delay_seconds) * 1000))
        humanized_action_sleep(max(35, int(base_ms * 0.65)), max(70, int(base_ms * 1.45)))
    ensure_left_button_released()


def scroll_chat_to_latest(hwnd: int, *, attempts: int = 16) -> None:
    requested_attempts = max(0, int(attempts or 0))
    spread = 2 if requested_attempts >= 10 else 1
    actual_attempts = max(1, requested_attempts + random.randint(-spread, spread))
    rect = win32gui.GetWindowRect(hwnd)
    x = max(380, int((rect[2] - rect[0]) * 0.6)) + random.randint(-12, 12)
    y = max(180, int((rect[3] - rect[1]) * 0.55)) + random.randint(-10, 10)
    require_active_ui_action_budget(
        "scroll_chat_to_latest",
        metadata={"attempts": requested_attempts, "actual_attempts": actual_attempts, "cursor": [int(x), int(y)]},
    )
    activate_window(hwnd)
    ensure_left_button_released()
    screen_x, screen_y = win32gui.ClientToScreen(hwnd, (x, y))
    win32api.SetCursorPos((screen_x, screen_y))
    humanized_action_sleep(45, 110)
    wheel_message = getattr(win32con, "WM_MOUSEWHEEL", 0x020A)
    lparam = ((int(screen_y) & 0xFFFF) << 16) | (int(screen_x) & 0xFFFF)
    for _ in range(actual_attempts):
        delta = int(-random.randint(5, 7) * 120)
        try:
            win32gui.PostMessage(hwnd, wheel_message, (delta & 0xFFFF) << 16, lparam)
        except Exception:
            win32api.mouse_event(win32con.MOUSEEVENTF_WHEEL, 0, 0, delta, 0)
        humanized_action_sleep(85, 180)
    ensure_left_button_released()


def capture_wechat(hwnd: int, *, artifact_dir: str | None = None, label: str = "wechat") -> tuple[Any, str]:
    image = capture_window_image(hwnd)
    if image is None:
        candidates = capture_window_by_rect(hwnd)
        if not candidates:
            raise RuntimeError("capture_wechat_failed: no screenshot candidate is available")
        image = win32_ocr_capture.select_best_capture_candidate(candidates, score=image_information_score)
    saved = save_screenshot_artifact(image, artifact_dir=artifact_dir, label=label)
    return image, saved


def capture_wechat_visible_rect(hwnd: int, *, artifact_dir: str | None = None, label: str = "wechat_visible") -> tuple[Any, str]:
    candidates = capture_window_by_rect(hwnd)
    if candidates:
        image = win32_ocr_capture.select_best_capture_candidate(candidates, score=image_information_score)
    else:
        image = capture_window_image(hwnd)
    if image is None:
        raise RuntimeError("capture_wechat_visible_rect_failed: no screenshot candidate is available")
    saved = save_screenshot_artifact(image, artifact_dir=artifact_dir, label=label)
    return image, saved


def capture_visible_screen(*, artifact_dir: str | None = None, label: str = "screen_visible") -> tuple[Any, str]:
    try:
        image = ImageGrab.grab()
    except Exception as exc:
        raise RuntimeError(f"capture_visible_screen_failed: {exc!r}") from exc
    saved = save_screenshot_artifact(image, artifact_dir=artifact_dir, label=label)
    return image, saved


def capture_wechat_window_visible_screen(hwnd: int, *, artifact_dir: str | None = None, label: str = "wechat_window_visible") -> tuple[Any, str]:
    rect = win32gui.GetWindowRect(hwnd)
    image = try_image_grab((int(rect[0]), int(rect[1]), int(rect[2]), int(rect[3])))
    if image is None:
        raise RuntimeError("capture_wechat_window_visible_screen_failed")
    saved = save_screenshot_artifact(image, artifact_dir=artifact_dir, label=label)
    return image, saved


def capture_window_image(hwnd: int) -> Any | None:
    return win32_ocr_capture.capture_window_image(
        hwnd,
        win32gui_module=win32gui,
        win32ui_module=win32ui,
        user32=getattr(getattr(ctypes, "windll", None), "user32", None),
        image_factory=Image,
    )


def capture_window_by_rect(hwnd: int) -> list[Any]:
    return win32_ocr_capture.capture_window_by_rect(
        hwnd,
        rect_provider=lambda current_hwnd: win32gui.GetWindowRect(current_hwnd),
        dpi_scale_provider=window_dpi_scale,
        grabber=try_image_grab,
    )


def try_image_grab(rect: tuple[int, int, int, int]) -> Any | None:
    return win32_ocr_capture.try_image_grab(rect, image_grabber=ImageGrab.grab)


def window_dpi_scale(hwnd: int) -> float:
    return win32_ocr_window_metrics.window_dpi_scale(hwnd, windll=getattr(ctypes, "windll", None))


def image_information_score(image: Any) -> float:
    return win32_ocr_render.image_information_score(image)


def run_ocr(image: Any) -> list[dict[str, Any]]:
    global _OCR_ENGINE
    items, _OCR_ENGINE = win32_ocr_engine.run_ocr_with_cache(
        image,
        engine_factory=RapidOCR,
        engine=_OCR_ENGINE,
        import_error=_OCR_IMPORT_ERROR,
        min_confidence=OCR_MIN_CONFIDENCE,
    )
    return items


def likely_foreign_overlay_capture(ocr_items: list[dict[str, Any]]) -> bool:
    return win32_ocr_render.likely_foreign_overlay_capture(ocr_items)


def allow_blind_target_confirmation(target: str) -> bool:
    if env_flag("WECHAT_WIN32_OCR_ALLOW_BLIND_FILE_TRANSFER_SEND", default=False) is False:
        return False
    return is_file_transfer_session_alias(target)


def blind_target_confirmation_guard(
    *,
    target: str,
    exact: bool,
    ocr_items: list[dict[str, Any]],
    image_size: tuple[int, int],
    geometry: dict[str, Any],
    screenshot_path: str,
) -> dict[str, Any]:
    if not allow_blind_target_confirmation(target):
        return {"ok": False}
    sidebar_match_count = 0
    sidebar_sessions = parse_sessions_from_ocr(ocr_items, image_size)
    for session in sidebar_sessions:
        if session_name_matches(str(session.get("name") or ""), target, exact=exact):
            sidebar_match_count += 1
    if sidebar_match_count <= 0:
        return {"ok": False}
    return {
        "ok": True,
        "online": True,
        "reason": "target_confirm_skipped_title_ocr_drift",
        "blind_send": True,
        "requested_target": target,
        "confirmed_target": "",
        "confirmation_confidence": "weak_sidebar_only",
        "geometry": geometry,
        "screenshot_path": screenshot_path,
        "sidebar_match_count": sidebar_match_count,
    }


def parse_sessions_from_ocr(
    ocr_items: list[dict[str, Any]],
    image_size: tuple[int, int],
    *,
    screenshot: Any | None = None,
) -> list[dict[str, Any]]:
    width, height = image_size
    split_x = session_split_x(width)
    min_header_y = chat_header_cutoff_y(height)
    left_min = max(42, int(width * 0.09))
    left_max = split_x - max(36, int(width * 0.07))
    right_limit = split_x + max(12, int(width * 0.03))
    candidates: list[dict[str, Any]] = []
    for item in ocr_items:
        text = str(item.get("text") or "").strip()
        if not is_session_name_candidate(text):
            continue
        if item["center_y"] < min_header_y or item["center_y"] > height - 20:
            continue
        if item["left"] < left_min or item["left"] > left_max:
            continue
        if item["right"] > right_limit:
            continue
        candidates.append(item)

    sessions: list[dict[str, Any]] = []
    last_y = -999.0
    min_session_row_gap = max(34, int(height * 0.048))
    name_counts: dict[str, int] = {}
    for item in sorted(candidates, key=lambda row: float(row["center_y"])):
        center_y = float(item["center_y"])
        if center_y - last_y < min_session_row_gap:
            continue
        name = normalize_session_name(str(item.get("text") or ""))
        # OCR occasionally glues sidebar timestamps into the session title
        # (e.g. "新数据测试昨天" or "新数据测试昨天19:23"),
        # which breaks session-target matching.
        name = strip_session_time_suffix(name)
        if is_file_transfer_session_alias(name):
            name = "文件传输助手"
        if not name:
            continue
        duplicate_index = int(name_counts.get(name, 0))
        name_counts[name] = duplicate_index + 1
        conversation_type = infer_conversation_type(name)
        row_fingerprint = session_row_fingerprint(item, duplicate_index=duplicate_index)
        sessions.append(
            {
                "name": name,
                "session_key": rpa_session_key(name, conversation_type=conversation_type, row_fingerprint=row_fingerprint),
                "conversation_type": conversation_type,
                "row_fingerprint": row_fingerprint,
                "duplicate_name_index": duplicate_index,
                "ambiguous_display_name": duplicate_index > 0,
                "confidence": item.get("confidence"),
                "center_y": center_y,
                "left": float(item.get("left") or 0),
                "right": float(item.get("right") or 0),
                "top": float(item.get("top") or 0),
                "bottom": float(item.get("bottom") or 0),
                "source_adapter": "win32_ocr",
            }
        )
        last_y = center_y
    enrich_sessions_with_sidebar_signals(
        sessions,
        ocr_items,
        image_size,
        screenshot=screenshot,
        min_header_y=min_header_y,
        split_x=split_x,
    )
    return sessions


def rpa_session_key(name: str, *, conversation_type: str = "unknown", row_fingerprint: dict[str, Any] | None = None) -> str:
    fingerprint = row_fingerprint if isinstance(row_fingerprint, dict) else {}
    duplicate = str(fingerprint.get("duplicate_discriminator") or "").strip()
    seed = json.dumps([str(conversation_type or "unknown"), str(name or ""), duplicate], ensure_ascii=False, sort_keys=True)
    return "wx:rpa:v1:" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:20]


def session_row_fingerprint(item: dict[str, Any], *, duplicate_index: int = 0) -> dict[str, Any]:
    center_y = float(item.get("center_y") or 0)
    return {
        "title_text": normalize_session_name(str(item.get("text") or "")),
        "title_bbox": [
            int(float(item.get("left") or 0)),
            int(float(item.get("top") or 0)),
            int(float(item.get("right") or 0)),
            int(float(item.get("bottom") or 0)),
        ],
        "row_y_bucket": int(center_y // 8),
        "duplicate_name_index": int(duplicate_index or 0),
        "duplicate_discriminator": str(duplicate_index) if int(duplicate_index or 0) > 0 else "",
    }


def enrich_sessions_with_sidebar_signals(
    sessions: list[dict[str, Any]],
    ocr_items: list[dict[str, Any]],
    image_size: tuple[int, int],
    *,
    screenshot: Any | None,
    min_header_y: int,
    split_x: int,
) -> None:
    if not sessions:
        return
    _width, height = image_size
    centers = [float(item.get("center_y") or 0) for item in sessions]
    for index, session in enumerate(sessions):
        center_y = float(session.get("center_y") or 0)
        previous_y = centers[index - 1] if index > 0 else max(float(min_header_y), center_y - 42)
        next_y = centers[index + 1] if index + 1 < len(centers) else min(float(height - 18), center_y + 52)
        row_top = max(float(min_header_y), (previous_y + center_y) / 2.0 if index > 0 else center_y - 38)
        row_bottom = min(float(height - 18), (center_y + next_y) / 2.0 if index + 1 < len(centers) else center_y + 44)
        preview, time_text = session_preview_and_time(
            ocr_items,
            session,
            row_top=row_top,
            row_bottom=row_bottom,
            split_x=split_x,
        )
        unread = detect_visual_session_unread_badge(
            screenshot,
            session,
            row_top=row_top,
            row_bottom=row_bottom,
            split_x=split_x,
        )
        if preview:
            session["preview"] = preview
        if time_text:
            session["time"] = time_text
        if unread.get("detected"):
            session["unread_badge"] = "visual_red_dot"
            session["unread_badge_meta"] = unread
            fingerprint = session.get("row_fingerprint") if isinstance(session.get("row_fingerprint"), dict) else {}
            fingerprint["last_unread_badge_bbox"] = unread.get("bbox") or unread.get("bounds") or []
            session["row_fingerprint"] = fingerprint


def session_preview_and_time(
    ocr_items: list[dict[str, Any]],
    session: dict[str, Any],
    *,
    row_top: float,
    row_bottom: float,
    split_x: int,
) -> tuple[str, str]:
    name = str(session.get("name") or "")
    session_left = float(session.get("left") or 0)
    session_center_y = float(session.get("center_y") or 0)
    preview_parts: list[str] = []
    time_text = ""
    for item in sorted(ocr_items, key=lambda row: (float(row.get("center_y") or 0), float(row.get("left") or 0))):
        text = normalize_ocr_text(item.get("text"))
        if not text or text == name:
            continue
        center_y = float(item.get("center_y") or 0)
        if center_y < row_top or center_y > row_bottom:
            continue
        left = float(item.get("left") or 0)
        right = float(item.get("right") or 0)
        if right > split_x + 10:
            continue
        if is_session_time_text(text):
            if not time_text and left >= session_left + 60:
                time_text = text
            continue
        if center_y <= session_center_y + 6:
            continue
        if left < session_left - 12:
            continue
        if text in {name, "搜索", "新对话"}:
            continue
        preview_parts.append(text)
    preview = " ".join(preview_parts).strip()
    if len(preview) > 160:
        preview = preview[:160]
    return preview, time_text


def detect_visual_session_unread_badge(
    screenshot: Any | None,
    session: dict[str, Any],
    *,
    row_top: float,
    row_bottom: float,
    split_x: int,
) -> dict[str, Any]:
    if screenshot is None:
        return {"detected": False, "reason": "no_screenshot"}
    try:
        image = screenshot.convert("RGB")
    except Exception:
        return {"detected": False, "reason": "image_unavailable"}
    width, height = image.size
    session_left = float(session.get("left") or 0)
    center_y = float(session.get("center_y") or 0)
    # The unread dot sits near the avatar's upper-right corner, immediately
    # left of the OCR name text. Keep this crop narrow to avoid red avatars.
    left = max(0, int(session_left - 34))
    right = min(width, int(min(session_left + 8, split_x - 26)))
    top = max(0, int(max(row_top, center_y - 32)))
    bottom = min(height, int(min(row_bottom, center_y + 8)))
    if right <= left or bottom <= top:
        return {"detected": False, "reason": "empty_crop"}
    crop = image.crop((left, top, right, bottom))
    red_pixels: list[tuple[int, int]] = []
    for y in range(crop.height):
        for x in range(crop.width):
            r, g, b = crop.getpixel((x, y))
            if r >= 190 and g <= 125 and b <= 135 and (r - max(g, b)) >= 55:
                red_pixels.append((x, y))
    if len(red_pixels) < 10:
        return {"detected": False, "red_pixel_count": len(red_pixels), "crop": [left, top, right, bottom]}
    xs = [point[0] for point in red_pixels]
    ys = [point[1] for point in red_pixels]
    box_width = max(xs) - min(xs) + 1
    box_height = max(ys) - min(ys) + 1
    compact = 4 <= box_width <= 32 and 4 <= box_height <= 32
    return {
        "detected": bool(compact),
        "red_pixel_count": len(red_pixels),
        "red_box": [left + min(xs), top + min(ys), left + max(xs) + 1, top + max(ys) + 1],
        "crop": [left, top, right, bottom],
        "reason": "visual_red_dot" if compact else "red_pixels_not_compact",
    }


def parse_messages_from_ocr(ocr_items: list[dict[str, Any]], image_size: tuple[int, int], *, target: str) -> list[dict[str, Any]]:
    width, height = image_size
    split_x = session_split_x(width)
    header_cutoff = chat_header_cutoff_y(height)
    geometry = {"left": 0, "top": 0, "right": width, "bottom": height, "width": width, "height": height}
    bottom_exclude_px = bounded_int(
        os.getenv("WECHAT_WIN32_OCR_MESSAGE_BOTTOM_EXCLUDE_PX"),
        default=max(DEFAULT_MESSAGE_BOTTOM_EXCLUDE_PX, int(height * 0.10)),
        minimum=60,
        maximum=max(180, int(height * 0.22)),
    )
    merge_vertical_gap = max(28, int(height * 0.03))
    rows: list[dict[str, Any]] = []
    for item in ocr_items:
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        if item["center_y"] < header_cutoff:
            continue
        if item["center_y"] > height - bottom_exclude_px:
            continue
        if item["left"] < split_x - 5:
            continue
        if is_message_noise(text):
            continue
        side_details = classify_message_side_details(item, width=width)
        side = str(side_details.get("side") or "unknown")
        rect = {
            "left": int(float(item.get("left") or 0)),
            "top": int(float(item.get("top") or 0)),
            "right": int(float(item.get("right") or 0)),
            "bottom": int(float(item.get("bottom") or 0)),
        }
        # The composer draft box lives above the send button, not only in the
        # final bottom strip.  Exclude left/unknown-side OCR there so failed or
        # partial drafts cannot be fed back to the LLM as customer messages.
        if side != "self" and rect_in_input_area(rect, geometry):
            continue
        rows.append(
            {
                **item,
                "side": side,
                "sender_role_algorithm": side_details.get("algorithm"),
                "sender_role_confidence": side_details.get("confidence"),
                "sender_role_evidence": side_details.get("evidence") or [],
            }
        )

    grouped: list[list[dict[str, Any]]] = []
    for item in sorted(rows, key=lambda row: (float(row["center_y"]), float(row["left"]))):
        side = str(item.get("side") or classify_message_side(item, width=width))
        if not grouped:
            grouped.append([{**item, "side": side}])
            continue
        previous = grouped[-1][-1]
        previous_side = str(previous.get("side") or "unknown")
        vertical_gap = float(item["top"]) - float(previous["bottom"])
        if side != "self" and previous_side == "self" and message_line_continues_previous_self_bubble(item, previous, vertical_gap):
            evidence = list(item.get("sender_role_evidence") or [])
            evidence.append("self_continuation_from_previous_line")
            grouped[-1].append({**item, "side": "self", "sender_role_evidence": evidence})
            continue
        if previous_side == side and vertical_gap <= merge_vertical_gap:
            grouped[-1].append({**item, "side": side})
        else:
            grouped.append([{**item, "side": side}])

    messages: list[dict[str, Any]] = []
    for group in grouped:
        if message_group_is_voice_duration_only(group):
            continue
        raw_content = "\n".join(str(item.get("text") or "").strip() for item in group if str(item.get("text") or "").strip())
        content = normalize_message_content(raw_content)
        if not content:
            continue
        if message_group_is_file_card_noise(group, content):
            continue
        side = str(group[0].get("side") or "unknown")
        y = float(group[0].get("center_y") or 0)
        rect = {
            "left": int(min(float(item.get("left") or 0) for item in group)),
            "top": int(min(float(item.get("top") or 0) for item in group)),
            "right": int(max(float(item.get("right") or 0) for item in group)),
            "bottom": int(max(float(item.get("bottom") or 0) for item in group)),
        }
        quality_flags: list[str] = []
        content, voice_duration_prefix_removed = strip_voice_duration_prefix_from_message_content(content, group)
        if not content:
            continue
        if voice_duration_prefix_removed:
            quality_flags.append("voice_duration_prefix_removed")
        if len(group) > 1:
            gaps = [
                max(0.0, float(group[index].get("top") or 0) - float(group[index - 1].get("bottom") or 0))
                for index in range(1, len(group))
            ]
            avg_height = sum(max(1.0, float(item.get("bottom") or 0) - float(item.get("top") or 0)) for item in group) / len(group)
            if any(gap > max(18.0, avg_height * 1.8) for gap in gaps):
                quality_flags.append("multi_bubble_possible_merge")
        ocr_confidence = min(float(item.get("confidence") or 0) for item in group)
        digest = hashlib.sha1(f"{target}|{side}|{round(y)}|{content}".encode("utf-8")).hexdigest()[:16]
        sender, sender_role = sender_fields_for_message_side(side, target=target)
        record = {
            "id": f"win32_ocr:{digest}",
            "type": "text",
            "sender": sender,
            "sender_role": sender_role,
            "sender_role_algorithm": str(group[0].get("sender_role_algorithm") or "wechat_win32_bubble_role_v2"),
            "sender_role_confidence": float(group[0].get("sender_role_confidence") or 0.0),
            "sender_role_evidence": list(group[0].get("sender_role_evidence") or []),
            "content": content,
            "content_raw_ocr": raw_content,
            "time": "",
            "source_adapter": "win32_ocr",
            "ocr_confidence": ocr_confidence,
            "bubble_rect": rect,
            "ocr_items": group,
            "quality_flags": quality_flags,
        }
        envelope = build_message_envelope(
            record,
            source_adapter="win32_ocr",
            conversation={"target_name": target, "conversation_type": infer_conversation_type(target)},
            ocr_items=group,
            bubble_rect=rect,
        )
        message = apply_message_envelope_to_record(record, envelope)
        if str(message.get("content") or "").strip():
            messages.append(message)
    return messages


def classify_message_side(item: dict[str, Any], *, width: int) -> str:
    return str(classify_message_side_details(item, width=width).get("side") or "unknown")


def message_line_continues_previous_self_bubble(item: dict[str, Any], previous: dict[str, Any], vertical_gap: float) -> bool:
    if vertical_gap < 0:
        return False
    previous_height = max(1.0, float(previous.get("bottom") or 0) - float(previous.get("top") or 0))
    current_height = max(1.0, float(item.get("bottom") or 0) - float(item.get("top") or 0))
    gap_limit = max(8.0, min(14.0, max(previous_height, current_height) * 0.65))
    if vertical_gap > gap_limit:
        return False
    previous_left = float(previous.get("left") or 0)
    current_left = float(item.get("left") or 0)
    return abs(current_left - previous_left) <= 32.0


def classify_message_side_details(item: dict[str, Any], *, width: int) -> dict[str, Any]:
    split_x = session_split_x(width)
    left = float(item.get("left") or 0)
    right = float(item.get("right") or 0)
    center_x = float(item.get("center_x") or 0)
    if center_x <= 0 and left and right:
        center_x = (left + right) / 2.0
    chat_width = max(1.0, float(width - split_x))
    rel_left = (left - float(split_x)) / chat_width
    rel_right = (right - float(split_x)) / chat_width
    rel_center = (center_x - float(split_x)) / chat_width
    legacy_left_hint_min = max(float(split_x + 75), float(width) * 0.43)
    left_in_self_lane = left >= legacy_left_hint_min
    reaches_right_self_lane = right >= max(float(split_x + 260), float(width) * 0.72)
    center_in_self_lane = center_x >= max(float(split_x + 180), float(width) * 0.58)
    compact_right_aligned = right >= float(width) * 0.84 and center_x >= float(width) * 0.62
    evidence: list[str] = [
        "wechat_win32_bubble_role_v2",
        f"rel_left={rel_left:.3f}",
        f"rel_center={rel_center:.3f}",
        f"rel_right={rel_right:.3f}",
    ]
    if left_in_self_lane:
        evidence.append("legacy_text_left_hint")
    if reaches_right_self_lane:
        evidence.append("right_self_lane_reached")
    if center_in_self_lane:
        evidence.append("center_in_self_lane")
    if compact_right_aligned:
        evidence.append("compact_right_aligned")
    if left_in_self_lane and ((reaches_right_self_lane and center_in_self_lane) or compact_right_aligned):
        return {
            "side": "self",
            "confidence": 0.92 if reaches_right_self_lane and center_in_self_lane else 0.86,
            "algorithm": "wechat_win32_bubble_role_v2",
            "evidence": evidence,
        }
    left_customer_lane = (
        rel_left <= 0.46
        and rel_center <= 0.68
        and left <= max(float(split_x + 360), float(width) * 0.72)
    )
    if left_customer_lane:
        evidence.append("left_customer_lane")
        if left_in_self_lane:
            evidence.append("legacy_left_hint_downgraded_without_right_structure")
        return {
            "side": "customer",
            "confidence": 0.84,
            "algorithm": "wechat_win32_bubble_role_v2",
            "evidence": evidence,
        }
    if left_in_self_lane:
        evidence.append("legacy_left_hint_downgraded_without_right_structure")
    elif reaches_right_self_lane and center_in_self_lane:
        evidence.append("right_structure_downgraded_without_left_alignment")
    return {
        "side": "unknown",
        "confidence": 0.76 if not left_in_self_lane else 0.64,
        "algorithm": "wechat_win32_bubble_role_v2",
        "evidence": evidence,
    }


def probe_wechat_windows() -> dict[str, Any]:
    windows: list[dict[str, Any]] = []
    visible_windows: list[dict[str, Any]] = []
    main_windows: list[dict[str, Any]] = []
    visible_main_windows: list[dict[str, Any]] = []
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    enum_windows_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    process_query_limited_information = 0x1000

    def process_path(pid: int) -> str:
        handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
        if not handle:
            return ""
        try:
            size = wintypes.DWORD(32768)
            buffer = ctypes.create_unicode_buffer(size.value)
            if kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                return buffer.value
            return ""
        finally:
            kernel32.CloseHandle(handle)

    def callback(hwnd: int, _lparam: int) -> bool:
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        path = process_path(int(pid.value))
        if not path.lower().endswith("\\weixin.exe"):
            return True
        title_length = user32.GetWindowTextLengthW(hwnd)
        title_buffer = ctypes.create_unicode_buffer(title_length + 1)
        user32.GetWindowTextW(hwnd, title_buffer, title_length + 1)
        class_buffer = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, class_buffer, 256)
        item = {
            "hwnd": int(hwnd),
            "pid": int(pid.value),
            "title": title_buffer.value,
            "class_name": class_buffer.value,
            "visible": bool(user32.IsWindowVisible(hwnd)),
            "path": path,
        }
        windows.append(item)
        if item["visible"]:
            visible_windows.append(item)
        if is_wechat_main_window(item):
            main_windows.append(item)
            if item["visible"]:
                visible_main_windows.append(item)
        return True

    user32.EnumWindows(enum_windows_proc(callback), 0)
    return {
        "windows": windows,
        "visible_windows": visible_windows,
        "main_windows": main_windows,
        "visible_main_windows": visible_main_windows,
        "visible_count": len(visible_windows),
        "main_count": len(main_windows),
        "visible_main_count": len(visible_main_windows),
    }


def select_primary_visible_main_window(probe: dict[str, Any]) -> dict[str, Any] | None:
    visible = probe.get("visible_main_windows") or []
    if not visible:
        return None
    candidates: list[dict[str, Any]] = []
    enable_content_probe = len(visible) > 1 and env_flag(
        "WECHAT_WIN32_OCR_MULTI_WINDOW_CONTENT_PROBE",
        default=True,
    )
    for item in visible:
        hwnd = int(item.get("hwnd") or 0)
        if not hwnd:
            continue
        try:
            geometry = get_window_geometry(hwnd)
        except Exception:
            geometry = {"left": 0, "top": 0, "width": 0, "height": 0}
        capture_ready = bool(validate_capture_geometry(geometry).get("ok"))
        content_score = window_content_health_score(hwnd, geometry) if enable_content_probe and capture_ready else 0
        candidates.append(
            {
                "item": item,
                "geometry": geometry,
                "content_health_score": content_score,
                "score": win32_ocr_window_actions.visible_window_candidate_score(
                    geometry,
                    capture_ready=capture_ready,
                    content_health_score=content_score,
                    min_send_width=MIN_SEND_CLIENT_WIDTH,
                    min_send_height=MIN_SEND_CLIENT_HEIGHT,
                    title_score=wechat_window_title_score(item),
                ),
            }
        )
    selected = win32_ocr_window_actions.select_best_visible_window_candidate(candidates)
    if selected is not None:
        return selected
    return dict(visible[0])


def window_content_health_score(hwnd: int, geometry: dict[str, Any]) -> int:
    try:
        screenshot, _path = capture_wechat(hwnd, artifact_dir=None, label="window_select_probe")
        ocr_items = run_ocr(screenshot)
    except Exception:
        return 0
    blank_render = detect_blank_render(screenshot, ocr_items, geometry=geometry)
    if blank_render.get("detected"):
        return win32_ocr_render.window_content_health_score_from_signals(
            ocr_items,
            blank_render_detected=True,
            quick_login_detected=False,
            auxiliary_shell_detected=False,
            blocking_reason="",
            text_normalizer=normalize_ocr_text,
        )
    quick_login_detected = quick_login_like(ocr_items, geometry=geometry)
    if quick_login_detected:
        return win32_ocr_render.window_content_health_score_from_signals(
            ocr_items,
            blank_render_detected=False,
            quick_login_detected=True,
            auxiliary_shell_detected=False,
            blocking_reason="",
            text_normalizer=normalize_ocr_text,
        )
    auxiliary_shell = auxiliary_wechat_shell_like(ocr_items, geometry=geometry)
    if auxiliary_shell.get("detected"):
        return win32_ocr_render.window_content_health_score_from_signals(
            ocr_items,
            blank_render_detected=False,
            quick_login_detected=False,
            auxiliary_shell_detected=True,
            blocking_reason="",
            text_normalizer=normalize_ocr_text,
        )
    blocking_reason = blocking_screen_reason(ocr_items)
    return win32_ocr_render.window_content_health_score_from_signals(
        ocr_items,
        blank_render_detected=False,
        quick_login_detected=False,
        auxiliary_shell_detected=False,
        blocking_reason=blocking_reason,
        text_normalizer=normalize_ocr_text,
    )


def ensure_visible_wechat_window(*, interactive: bool = True) -> dict[str, Any]:
    probe = probe_wechat_windows()
    usable_visible = probe_has_usable_visible_main_window(probe) if probe["visible_main_windows"] else False
    tray_hidden = wechat_main_window_is_tray_hidden(probe) if not probe["visible_main_windows"] else False
    plan = win32_ocr_window_actions.plan_ensure_visible_wechat_window(
        probe,
        interactive=interactive,
        usable_visible=usable_visible,
        tray_hidden=tray_hidden,
    )
    deps = win32_ocr_window_visibility.EnsureVisibleDependencies(
        probe_wechat_windows=probe_wechat_windows,
        focus_wechat_window=focus_wechat_window,
        restore_wechat_window=restore_wechat_window,
        humanized_action_sleep=humanized_action_sleep,
    )
    return win32_ocr_window_visibility.ensure_visible_wechat_window_with_dependencies(
        probe,
        plan=plan,
        deps=deps,
    )


def wechat_main_window_is_tray_hidden(probe: dict[str, Any]) -> bool:
    """Detect WeChat running with only hidden/tray main windows.

    In this state, automatic ShowWindow/foreground recovery can surface a
    half-rendered shell and trigger blank-screen RPA failures. Prefer an
    explicit manual open by the operator before automation starts.
    """
    return win32_ocr_window_state.tray_hidden_from_probe(probe)


def probe_has_usable_visible_main_window(probe: dict[str, Any]) -> bool:
    """Treat offscreen/minimized "visible" windows as not ready for RPA."""
    visible = probe.get("visible_main_windows") or []
    if not visible:
        return False
    checked = False
    for item in visible:
        hwnd = int(item.get("hwnd") or 0)
        if not hwnd:
            continue
        try:
            geometry = get_window_geometry(hwnd)
        except Exception:
            # Unit tests and exotic shell windows may not expose geometry. In
            # that case, keep the old non-invasive focus behavior.
            return True
        checked = True
        if validate_capture_geometry(geometry).get("ok"):
            return True
    return not checked


def restore_wechat_window(probe: dict[str, Any]) -> dict[str, Any] | None:
    candidates = [item for item in (probe.get("windows") or []) if is_wechat_main_window(item)]
    candidates.sort(key=wechat_window_title_score, reverse=True)
    for item in candidates:
        hwnd = int(item.get("hwnd") or 0)
        if hwnd:
            activate_window(hwnd)
            return dict(item)
    return None


def focus_wechat_window(probe: dict[str, Any]) -> dict[str, Any] | None:
    item = select_primary_visible_main_window(probe)
    if not item:
        return None
    hwnd = int(item.get("hwnd") or 0)
    if hwnd:
        activate_window(hwnd)
        try:
            focus_match = foreground_window_matches_target(hwnd)
            if win32_ocr_window_state.foreground_guard_ready(focus_match):
                return dict(item)
        except Exception:
            pass
    return None


def activate_window(hwnd: int) -> None:
    if not hwnd:
        return
    activation_settings = win32_ocr_window_state.activate_window_settings(
        aggressive_focus=env_flag("WECHAT_WIN32_OCR_AGGRESSIVE_FOCUS", default=False),
        attach_thread_input=env_flag("WECHAT_WIN32_OCR_ATTACH_THREAD_INPUT", default=False),
        debounce_seconds=env_float("WECHAT_WIN32_OCR_ACTIVATE_DEBOUNCE_SECONDS", 2.5),
    )
    deps = win32_ocr_window_activation.ActivateWindowDependencies(
        user32=ctypes.windll.user32,
        win32gui=win32gui,
        win32process=win32process,
        win32api=win32api,
        win32con=win32con,
        foreground_window_matches_target=foreground_window_matches_target,
        require_active_ui_action_budget=require_active_ui_action_budget,
        humanized_action_sleep=humanized_action_sleep,
        coordinate_rpa_action=coordinate_rpa_action,
        focus_click_fallback_enabled=focus_click_fallback_enabled,
        click=click,
        monotonic=time.monotonic,
    )
    win32_ocr_window_activation.activate_window_with_dependencies(
        int(hwnd),
        settings=activation_settings,
        last_activate_monotonic_by_hwnd=_LAST_ACTIVATE_MONOTONIC_BY_HWND,
        deps=deps,
    )


def configure_dpi_awareness() -> None:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def ensure_left_button_released() -> None:
    if win32api is None:
        return
    try:
        key_state = int(win32api.GetKeyState(getattr(win32con, "VK_LBUTTON", 0x01)))
    except Exception:
        return
    if key_state >= 0:
        return
    try:
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
        humanized_action_sleep(8, 24)
    except Exception:
        pass


def client_click(hwnd: int, x: int, y: int) -> None:
    """Click a WeChat client coordinate without relying on global DPI math."""
    click_x, click_y, jitter_meta = jitter_client_click_surface_point(hwnd, int(x), int(y))
    require_active_ui_action_budget(
        "client_click",
        metadata={"hwnd": int(hwnd or 0), "x": click_x, "y": click_y, "jitter": jitter_meta},
    )
    activate_window(hwnd)
    ensure_left_button_released()
    lparam = ((int(click_y) & 0xFFFF) << 16) | (int(click_x) & 0xFFFF)
    win32gui.SendMessage(hwnd, win32con.WM_MOUSEMOVE, 0, lparam)
    humanized_action_sleep(20, 55)
    win32gui.SendMessage(hwnd, win32con.WM_LBUTTONDOWN, win32con.MK_LBUTTON, lparam)
    humanized_action_sleep(45, 100)
    win32gui.SendMessage(hwnd, win32con.WM_LBUTTONUP, 0, lparam)
    humanized_action_sleep(80, 170)


def human_client_click(hwnd: int, x: int, y: int) -> None:
    """Move the real cursor with small jitter before clicking a client point."""
    click_x, click_y, jitter_meta = jitter_client_click_surface_point(hwnd, int(x), int(y))
    require_active_ui_action_budget(
        "human_client_click",
        metadata={"hwnd": int(hwnd or 0), "x": click_x, "y": click_y, "jitter": jitter_meta},
    )
    activate_window(hwnd)
    ensure_left_button_released()
    left_down_sent = False
    try:
        screen_x, screen_y = client_to_screen(hwnd, int(click_x), int(click_y))
        start_x, start_y = win32api.GetCursorPos()
        steps = random.randint(5, 9)
        for step in range(1, steps + 1):
            ratio = step / steps
            ease = ratio * ratio * (3 - 2 * ratio)
            jitter_x = random.randint(-2, 2) if step < steps else 0
            jitter_y = random.randint(-2, 2) if step < steps else 0
            next_x = int(start_x + (screen_x - start_x) * ease) + jitter_x
            next_y = int(start_y + (screen_y - start_y) * ease) + jitter_y
            win32api.SetCursorPos((next_x, next_y))
            time.sleep(random.uniform(0.015, 0.045))
        time.sleep(random.uniform(0.04, 0.12))
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        left_down_sent = True
        time.sleep(random.uniform(0.05, 0.12))
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
        left_down_sent = False
        time.sleep(random.uniform(0.12, 0.28))
    except Exception:
        # Some desktop policies deny SetCursorPos; fall back to PostMessage clicks.
        client_click(hwnd, click_x, click_y)
    finally:
        if left_down_sent:
            ensure_left_button_released()


def human_window_image_hover(hwnd: int, x: int, y: int) -> dict[str, Any]:
    """Move the real cursor toward a screenshot-space point without clicking."""
    target_x, target_y, jitter_meta = jitter_window_image_click_surface_point(hwnd, int(x), int(y))
    require_active_ui_action_budget(
        "human_window_image_hover",
        metadata={"hwnd": int(hwnd or 0), "x": target_x, "y": target_y, "jitter": jitter_meta},
    )
    activate_window(hwnd)
    ensure_left_button_released()
    try:
        left, top, _right, _bottom = win32gui.GetWindowRect(hwnd)
        screen_x = int(left) + int(target_x)
        screen_y = int(top) + int(target_y)
        start_x, start_y = win32api.GetCursorPos()
        steps = random.randint(8, 14)
        for step in range(1, steps + 1):
            ratio = step / steps
            ease = ratio * ratio * (3 - 2 * ratio)
            drift_x = random.randint(-3, 3) if step < steps else 0
            drift_y = random.randint(-3, 3) if step < steps else 0
            next_x = int(start_x + (screen_x - start_x) * ease) + drift_x
            next_y = int(start_y + (screen_y - start_y) * ease) + drift_y
            win32api.SetCursorPos((next_x, next_y))
            time.sleep(random.uniform(0.018, 0.055))
        time.sleep(random.uniform(0.18, 0.55))
        return {"ok": True, "x": target_x, "y": target_y, "screen_x": screen_x, "screen_y": screen_y, "steps": steps, "jitter": jitter_meta}
    except Exception as exc:
        return {"ok": False, "x": target_x, "y": target_y, "error": repr(exc), "jitter": jitter_meta}


def human_window_image_click(hwnd: int, x: int, y: int) -> None:
    """Click a point measured in the same coordinate space as screenshots."""
    click_x, click_y, jitter_meta = jitter_window_image_click_surface_point(hwnd, int(x), int(y))
    require_active_ui_action_budget(
        "human_window_image_click",
        metadata={"hwnd": int(hwnd or 0), "x": click_x, "y": click_y, "jitter": jitter_meta},
    )
    activate_window(hwnd)
    ensure_left_button_released()
    left_down_sent = False
    try:
        left, top, _right, _bottom = win32gui.GetWindowRect(hwnd)
        screen_x = int(left) + int(click_x)
        screen_y = int(top) + int(click_y)
        start_x, start_y = win32api.GetCursorPos()
        steps = random.randint(5, 9)
        for step in range(1, steps + 1):
            ratio = step / steps
            ease = ratio * ratio * (3 - 2 * ratio)
            jitter_x = random.randint(-2, 2) if step < steps else 0
            jitter_y = random.randint(-2, 2) if step < steps else 0
            next_x = int(start_x + (screen_x - start_x) * ease) + jitter_x
            next_y = int(start_y + (screen_y - start_y) * ease) + jitter_y
            win32api.SetCursorPos((next_x, next_y))
            time.sleep(random.uniform(0.015, 0.045))
        time.sleep(random.uniform(0.04, 0.12))
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        left_down_sent = True
        time.sleep(random.uniform(0.05, 0.12))
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
        left_down_sent = False
        time.sleep(random.uniform(0.12, 0.28))
    except Exception:
        screen_x, screen_y = client_to_screen(hwnd, int(click_x), int(click_y))
        click(screen_x, screen_y)
    finally:
        if left_down_sent:
            ensure_left_button_released()


def human_window_image_click_in_bounds(
    hwnd: int,
    x: int,
    y: int,
    *,
    bounds: list[int],
    action_name: str = "human_window_image_click_in_bounds",
) -> dict[str, Any]:
    """Click a screenshot-space point, clamped to a known safe window rectangle."""
    raw_x, raw_y, jitter_meta = jitter_window_image_click_surface_point(hwnd, int(x), int(y))
    click_x, click_y = clamp_point_to_bounds(raw_x, raw_y, bounds)
    require_active_ui_action_budget(
        action_name,
        metadata={"hwnd": int(hwnd or 0), "x": click_x, "y": click_y, "bounds": bounds, "jitter": jitter_meta},
    )
    activate_window(hwnd)
    ensure_left_button_released()
    left_down_sent = False
    try:
        left, top, _right, _bottom = win32gui.GetWindowRect(hwnd)
        screen_x = int(left) + int(click_x)
        screen_y = int(top) + int(click_y)
        start_x, start_y = win32api.GetCursorPos()
        steps = random.randint(6, 11)
        for step in range(1, steps + 1):
            ratio = step / steps
            ease = ratio * ratio * (3 - 2 * ratio)
            jitter_x = random.randint(-2, 2) if step < steps else 0
            jitter_y = random.randint(-2, 2) if step < steps else 0
            next_x = int(start_x + (screen_x - start_x) * ease) + jitter_x
            next_y = int(start_y + (screen_y - start_y) * ease) + jitter_y
            win32api.SetCursorPos((next_x, next_y))
            time.sleep(random.uniform(0.016, 0.052))
        time.sleep(random.uniform(0.08, 0.22))
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        left_down_sent = True
        time.sleep(random.uniform(0.055, 0.145))
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
        left_down_sent = False
        time.sleep(random.uniform(0.16, 0.34))
        return {
            "ok": True,
            "x": click_x,
            "y": click_y,
            "screen_x": screen_x,
            "screen_y": screen_y,
            "raw_x": raw_x,
            "raw_y": raw_y,
            "bounds": bounds,
            "steps": steps,
            "jitter": jitter_meta,
        }
    except Exception as exc:
        return {"ok": False, "x": click_x, "y": click_y, "bounds": bounds, "error": repr(exc), "jitter": jitter_meta}
    finally:
        if left_down_sent:
            ensure_left_button_released()


def human_window_image_hover_in_bounds(
    hwnd: int,
    x: int,
    y: int,
    *,
    bounds: list[int],
    action_name: str = "human_window_image_hover_in_bounds",
) -> dict[str, Any]:
    """Hover a screenshot-space point, clamped to a known safe window rectangle."""
    raw_x, raw_y, jitter_meta = jitter_window_image_click_surface_point(hwnd, int(x), int(y))
    hover_x, hover_y = clamp_point_to_bounds(raw_x, raw_y, bounds)
    require_active_ui_action_budget(
        action_name,
        metadata={"hwnd": int(hwnd or 0), "x": hover_x, "y": hover_y, "bounds": bounds, "jitter": jitter_meta},
    )
    activate_window(hwnd)
    ensure_left_button_released()
    try:
        left, top, _right, _bottom = win32gui.GetWindowRect(hwnd)
        screen_x = int(left) + int(hover_x)
        screen_y = int(top) + int(hover_y)
        start_x, start_y = win32api.GetCursorPos()
        steps = random.randint(7, 13)
        for step in range(1, steps + 1):
            ratio = step / steps
            ease = ratio * ratio * (3 - 2 * ratio)
            jitter_x = random.randint(-2, 2) if step < steps else 0
            jitter_y = random.randint(-2, 2) if step < steps else 0
            next_x = int(start_x + (screen_x - start_x) * ease) + jitter_x
            next_y = int(start_y + (screen_y - start_y) * ease) + jitter_y
            win32api.SetCursorPos((next_x, next_y))
            time.sleep(random.uniform(0.016, 0.052))
        time.sleep(random.uniform(0.18, 0.36))
        return {
            "ok": True,
            "x": hover_x,
            "y": hover_y,
            "screen_x": screen_x,
            "screen_y": screen_y,
            "raw_x": raw_x,
            "raw_y": raw_y,
            "bounds": bounds,
            "steps": steps,
            "jitter": jitter_meta,
        }
    except Exception as exc:
        return {"ok": False, "x": hover_x, "y": hover_y, "bounds": bounds, "error": repr(exc), "jitter": jitter_meta}


def human_window_image_right_click_in_bounds(
    hwnd: int,
    x: int,
    y: int,
    *,
    bounds: list[int],
    action_name: str = "human_window_image_right_click_in_bounds",
) -> dict[str, Any]:
    """Right-click a screenshot-space point, clamped to a known safe window rectangle."""
    raw_x, raw_y, jitter_meta = jitter_window_image_click_surface_point(hwnd, int(x), int(y))
    click_x, click_y = clamp_point_to_bounds(raw_x, raw_y, bounds)
    require_active_ui_action_budget(
        action_name,
        metadata={"hwnd": int(hwnd or 0), "x": click_x, "y": click_y, "bounds": bounds, "jitter": jitter_meta},
    )
    activate_window(hwnd)
    ensure_left_button_released()
    right_down_sent = False
    try:
        left, top, _right, _bottom = win32gui.GetWindowRect(hwnd)
        screen_x = int(left) + int(click_x)
        screen_y = int(top) + int(click_y)
        start_x, start_y = win32api.GetCursorPos()
        steps = random.randint(6, 11)
        for step in range(1, steps + 1):
            ratio = step / steps
            ease = ratio * ratio * (3 - 2 * ratio)
            jitter_x = random.randint(-2, 2) if step < steps else 0
            jitter_y = random.randint(-2, 2) if step < steps else 0
            next_x = int(start_x + (screen_x - start_x) * ease) + jitter_x
            next_y = int(start_y + (screen_y - start_y) * ease) + jitter_y
            win32api.SetCursorPos((next_x, next_y))
            time.sleep(random.uniform(0.016, 0.052))
        time.sleep(random.uniform(0.08, 0.22))
        win32api.mouse_event(win32con.MOUSEEVENTF_RIGHTDOWN, 0, 0, 0, 0)
        right_down_sent = True
        time.sleep(random.uniform(0.055, 0.145))
        win32api.mouse_event(win32con.MOUSEEVENTF_RIGHTUP, 0, 0, 0, 0)
        right_down_sent = False
        time.sleep(random.uniform(0.16, 0.34))
        return {
            "ok": True,
            "x": click_x,
            "y": click_y,
            "screen_x": screen_x,
            "screen_y": screen_y,
            "raw_x": raw_x,
            "raw_y": raw_y,
            "bounds": bounds,
            "steps": steps,
            "jitter": jitter_meta,
        }
    except Exception as exc:
        return {"ok": False, "x": click_x, "y": click_y, "bounds": bounds, "error": repr(exc), "jitter": jitter_meta}
    finally:
        if right_down_sent:
            try:
                win32api.mouse_event(win32con.MOUSEEVENTF_RIGHTUP, 0, 0, 0, 0)
            except Exception:
                pass


def human_screen_hover(x: int, y: int, *, action_name: str = "human_screen_hover") -> dict[str, Any]:
    """Move the real cursor toward a screen-space point without clicking."""
    target_x, target_y, jitter_meta = jitter_screen_click_surface_point(int(x), int(y))
    require_active_ui_action_budget(action_name, metadata={"x": target_x, "y": target_y, "jitter": jitter_meta})
    ensure_left_button_released()
    try:
        start_x, start_y = win32api.GetCursorPos()
        steps = random.randint(10, 18)
        for step in range(1, steps + 1):
            ratio = step / steps
            ease = ratio * ratio * (3 - 2 * ratio)
            drift_x = random.randint(-3, 3) if step < steps else 0
            drift_y = random.randint(-3, 3) if step < steps else 0
            next_x = int(start_x + (target_x - start_x) * ease) + drift_x
            next_y = int(start_y + (target_y - start_y) * ease) + drift_y
            win32api.SetCursorPos((next_x, next_y))
            time.sleep(random.uniform(0.018, 0.06))
        time.sleep(random.uniform(0.22, 0.68))
        return {"ok": True, "screen_x": target_x, "screen_y": target_y, "steps": steps, "jitter": jitter_meta}
    except Exception as exc:
        return {"ok": False, "screen_x": target_x, "screen_y": target_y, "error": repr(exc), "jitter": jitter_meta}


def human_screen_click(x: int, y: int, *, action_name: str = "human_screen_click") -> dict[str, Any]:
    """Click a screen-space point after a short human-like cursor movement."""
    target_x, target_y, jitter_meta = jitter_screen_click_surface_point(int(x), int(y))
    require_active_ui_action_budget(action_name, metadata={"x": target_x, "y": target_y, "jitter": jitter_meta})
    ensure_left_button_released()
    left_down_sent = False
    try:
        start_x, start_y = win32api.GetCursorPos()
        steps = random.randint(4, 8)
        for step in range(1, steps + 1):
            ratio = step / steps
            ease = ratio * ratio * (3 - 2 * ratio)
            drift_x = random.randint(-2, 2) if step < steps else 0
            drift_y = random.randint(-2, 2) if step < steps else 0
            next_x = int(start_x + (target_x - start_x) * ease) + drift_x
            next_y = int(start_y + (target_y - start_y) * ease) + drift_y
            win32api.SetCursorPos((next_x, next_y))
            time.sleep(random.uniform(0.016, 0.05))
        time.sleep(random.uniform(0.08, 0.22))
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        left_down_sent = True
        time.sleep(random.uniform(0.055, 0.14))
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
        left_down_sent = False
        time.sleep(random.uniform(0.16, 0.34))
        return {"ok": True, "screen_x": target_x, "screen_y": target_y, "steps": steps, "jitter": jitter_meta}
    except Exception as exc:
        return {"ok": False, "screen_x": target_x, "screen_y": target_y, "error": repr(exc), "jitter": jitter_meta}
    finally:
        if left_down_sent:
            ensure_left_button_released()


def human_screen_click_in_bounds(
    x: int,
    y: int,
    *,
    bounds: list[int],
    action_name: str = "human_screen_click_in_bounds",
) -> dict[str, Any]:
    """Click a screen-space point, clamped to a known safe target rectangle."""
    raw_x, raw_y, jitter_meta = jitter_screen_click_surface_point(int(x), int(y))
    target_x, target_y = clamp_point_to_bounds(raw_x, raw_y, bounds)
    require_active_ui_action_budget(
        action_name,
        metadata={"x": target_x, "y": target_y, "bounds": bounds, "jitter": jitter_meta},
    )
    ensure_left_button_released()
    left_down_sent = False
    try:
        start_x, start_y = win32api.GetCursorPos()
        steps = random.randint(6, 11)
        for step in range(1, steps + 1):
            ratio = step / steps
            ease = ratio * ratio * (3 - 2 * ratio)
            drift_x = random.randint(-2, 2) if step < steps else 0
            drift_y = random.randint(-2, 2) if step < steps else 0
            next_x = int(start_x + (target_x - start_x) * ease) + drift_x
            next_y = int(start_y + (target_y - start_y) * ease) + drift_y
            win32api.SetCursorPos((next_x, next_y))
            time.sleep(random.uniform(0.016, 0.052))
        time.sleep(random.uniform(0.10, 0.24))
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        left_down_sent = True
        time.sleep(random.uniform(0.06, 0.15))
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
        left_down_sent = False
        time.sleep(random.uniform(0.18, 0.36))
        return {
            "ok": True,
            "screen_x": target_x,
            "screen_y": target_y,
            "raw_screen_x": raw_x,
            "raw_screen_y": raw_y,
            "bounds": bounds,
            "steps": steps,
            "jitter": jitter_meta,
        }
    except Exception as exc:
        return {"ok": False, "screen_x": target_x, "screen_y": target_y, "bounds": bounds, "error": repr(exc), "jitter": jitter_meta}
    finally:
        if left_down_sent:
            ensure_left_button_released()


def client_to_screen(hwnd: int, x: int, y: int) -> tuple[int, int]:
    point = wintypes.POINT(int(x), int(y))
    ctypes.windll.user32.ClientToScreen(hwnd, ctypes.byref(point))
    return int(point.x), int(point.y)


def click(x: int, y: int) -> None:
    click_x, click_y, jitter_meta = jitter_screen_click_surface_point(int(x), int(y))
    require_active_ui_action_budget("screen_click", metadata={"x": click_x, "y": click_y, "jitter": jitter_meta})
    ensure_left_button_released()
    win32api.SetCursorPos((int(click_x), int(click_y)))
    humanized_action_sleep(20, 55)
    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    humanized_action_sleep(35, 85)
    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
    ensure_left_button_released()


def hotkey(modifier: int, key: int) -> None:
    coordinate_rpa_action("hotkey", metadata={"modifier": int(modifier), "key": int(key)})
    win32api.keybd_event(modifier, 0, 0, 0)
    humanized_action_sleep(16, 42)
    win32api.keybd_event(key, 0, 0, 0)
    humanized_action_sleep(18, 48)
    win32api.keybd_event(key, 0, win32con.KEYEVENTF_KEYUP, 0)
    win32api.keybd_event(modifier, 0, win32con.KEYEVENTF_KEYUP, 0)
    humanized_action_sleep(8, 28)


def key_press(key: int) -> None:
    coordinate_rpa_action("key_press", metadata={"key": int(key)})
    win32api.keybd_event(key, 0, 0, 0)
    humanized_action_sleep(24, 70)
    win32api.keybd_event(key, 0, win32con.KEYEVENTF_KEYUP, 0)
    humanized_action_sleep(8, 26)


def is_wechat_main_window(item: dict[str, Any]) -> bool:
    return win32_ocr_windowing.is_wechat_main_window(item)


def wechat_window_title_score(item: dict[str, Any]) -> int:
    return win32_ocr_windowing.wechat_window_title_score(item)


def normalize_wechat_title(title: str) -> str:
    return win32_ocr_windowing.normalize_wechat_title(title)


def normalize_ocr_text(text: Any) -> str:
    return win32_ocr_text.normalize_ocr_text(text)


def normalize_session_name(text: str) -> str:
    return win32_ocr_text.normalize_session_name(text)


def strip_chat_unread_suffix(text: str) -> str:
    return win32_ocr_text.strip_chat_unread_suffix(text)


def normalize_chat_title_for_match(text: str) -> str:
    return win32_ocr_text.normalize_chat_title_for_match(text)


def canonical_session_name(text: str) -> str:
    return win32_ocr_text.canonical_session_name(text)


def is_file_transfer_session_alias(text: str, *, collapsed: str | None = None) -> bool:
    return win32_ocr_text.is_file_transfer_session_alias(text, collapsed=collapsed)


def normalize_message_content(text: str) -> str:
    return win32_ocr_text.normalize_message_content(text)



def quick_login_like(ocr_items: list[dict[str, Any]], *, geometry: dict[str, Any]) -> bool:
    return win32_ocr_text.quick_login_like(ocr_items, geometry=geometry)


def ensure_quick_login_if_available(
    hwnd: int,
    *,
    artifact_dir: str | None = None,
    auto_enter: bool = DEFAULT_QUICK_LOGIN_AUTO_ENTER,
) -> dict[str, Any]:
    screenshot, path = capture_wechat(hwnd, artifact_dir=artifact_dir, label="quick_login_probe")
    ocr_items = run_ocr(screenshot)
    geometry = get_window_geometry(hwnd)
    if not quick_login_like(ocr_items, geometry=geometry):
        return {
            "attempted": False,
            "detected": False,
            "geometry": geometry,
            "screenshot_path": path,
        }
    if not auto_enter:
        return {
            "attempted": False,
            "detected": True,
            "auto_enter_enabled": False,
            "geometry": geometry,
            "screenshot_path": path,
            "reason": "quick_login_detected_no_auto_enter",
        }
    enter_item = next((item for item in ocr_items if "进入微信" in str(item.get("text") or "")), None)
    if enter_item:
        click_x = int(float(enter_item.get("center_x") or (geometry["width"] * 0.5)))
        click_y = int(float(enter_item.get("center_y") or (geometry["height"] * 0.74)))
    else:
        click_x = int(geometry["width"] * 0.5)
        click_y = int(geometry["height"] * 0.74)
    human_client_click(hwnd, click_x, click_y)
    humanized_action_sleep(500, 850)
    return {
        "attempted": True,
        "detected": True,
        "auto_enter_enabled": True,
        "geometry": geometry,
        "click_point": [click_x, click_y],
        "screenshot_path": path,
        "reason": "quick_login_enter_clicked",
    }
def session_split_x(width: int) -> int:
    return win32_ocr_geometry.session_split_x(width)


def chat_header_cutoff_y(height: int) -> int:
    return win32_ocr_geometry.chat_header_cutoff_y(height)


def active_chat_title_cutoff_y(height: int) -> int:
    return win32_ocr_geometry.active_chat_title_cutoff_y(height)


def active_chat_title_top_cutoff_y(height: int) -> int:
    return win32_ocr_geometry.active_chat_title_top_cutoff_y(height)


def active_chat_title_left_x(width: int) -> int:
    return win32_ocr_geometry.active_chat_title_left_x(width)


def active_chat_title_right_x(width: int) -> int:
    return win32_ocr_geometry.active_chat_title_right_x(width)


def active_chat_title_top_y(height: int) -> int:
    return win32_ocr_geometry.active_chat_title_top_y(height)


def active_chat_title_bottom_y(height: int) -> int:
    return win32_ocr_geometry.active_chat_title_bottom_y(height)


def search_box_point_for_geometry(geometry: dict[str, Any]) -> tuple[int, int]:
    return win32_ocr_geometry.search_box_point_for_geometry(geometry)


def sidebar_search_input_focus_point_for_geometry(geometry: dict[str, Any]) -> tuple[int, int]:
    """Return a point inside the sidebar search text-input area.

    The historical search-box point is also used as a geometry reference for
    the nearby plus-entry locator. Keep that contract stable, and use this
    separate point when the intent is to focus the search input itself.
    """
    anchor_x, anchor_y = search_box_point_for_geometry(geometry)
    width = int(geometry.get("width") or 0)
    split_x = session_split_x(width)
    minimum = max(96, int(anchor_x) + 42)
    maximum = max(minimum, min(split_x - 96, int(anchor_x) + 110))
    focus_x = bounded_int(
        int(split_x * 0.52),
        default=int(anchor_x) + 68,
        minimum=minimum,
        maximum=maximum,
    )
    return focus_x, int(anchor_y)


def session_click_x_for_geometry(geometry: dict[str, Any]) -> int:
    return win32_ocr_geometry.session_click_x_for_geometry(geometry)


def normalize_wechat_window(hwnd: int) -> dict[str, Any]:
    enabled = env_flag("WECHAT_WIN32_OCR_WINDOW_NORMALIZE", default=True)
    before = get_window_geometry(hwnd)
    dpi_scale = window_dpi_scale(hwnd)
    if not enabled:
        return {"ok": True, "enabled": False, "applied": False, "before": before}

    enforce_recommended = env_flag("WECHAT_WIN32_OCR_ENFORCE_RECOMMENDED_WINDOW", default=True)
    fixed_origin = env_flag("WECHAT_WIN32_OCR_WINDOW_FIXED_ORIGIN", default=True)
    try:
        user32 = ctypes.windll.user32
        screen_width = int(user32.GetSystemMetrics(0) or 0)
        screen_height = int(user32.GetSystemMetrics(1) or 0)
        screen_metrics_available = True
    except Exception:
        screen_width = 0
        screen_height = 0
        screen_metrics_available = False

    plan = win32_ocr_window_actions.plan_normalize_wechat_window(
        before,
        enabled=True,
        dpi_scale=dpi_scale,
        requested_width=os.getenv("WECHAT_WIN32_OCR_WINDOW_WIDTH"),
        requested_height=os.getenv("WECHAT_WIN32_OCR_WINDOW_HEIGHT"),
        requested_left=os.getenv("WECHAT_WIN32_OCR_WINDOW_LEFT"),
        requested_top=os.getenv("WECHAT_WIN32_OCR_WINDOW_TOP"),
        enforce_recommended=enforce_recommended,
        fixed_origin=fixed_origin,
        screen_width=screen_width,
        screen_height=screen_height,
        screen_metrics_available=screen_metrics_available,
        default_width=DEFAULT_SAFE_WINDOW_WIDTH,
        default_height=DEFAULT_SAFE_WINDOW_HEIGHT,
        min_width=MIN_SAFE_WINDOW_WIDTH,
        min_height=MIN_SAFE_WINDOW_HEIGHT,
        max_width=MAX_SAFE_WINDOW_WIDTH,
        max_height=MAX_SAFE_WINDOW_HEIGHT,
    )
    left = int(plan.get("left") or 0)
    top = int(plan.get("top") or 0)
    safe_width = int(plan.get("width") or 0)
    safe_height = int(plan.get("height") or 0)
    effective_target = dict(plan.get("target") or {})
    requested_target = dict(plan.get("requested_target") or {})
    recommended_floor_applied = bool(plan.get("recommended_floor_applied"))
    resolution_scale = float(plan.get("resolution_scale") or 1.0)
    if not bool(plan.get("move")):
        return {
            "ok": True,
            "enabled": True,
            "applied": False,
            "before": before,
            "after": before,
            "target": effective_target,
            "requested_target": requested_target,
            "dpi_scale": dpi_scale,
            "resolution_scale": resolution_scale,
            "enforce_recommended": enforce_recommended,
            "recommended_floor_applied": recommended_floor_applied,
            "fixed_origin": fixed_origin,
            "screen": {"width": screen_width, "height": screen_height},
            "reason": "already_near_target",
        }

    try:
        win32gui.MoveWindow(hwnd, left, top, safe_width, safe_height, True)
        humanized_action_sleep(90, 180)
        after = get_window_geometry(hwnd)
        applied = (
            abs(int(after.get("width") or 0) - int(before.get("width") or 0)) > 4
            or abs(int(after.get("height") or 0) - int(before.get("height") or 0)) > 4
            or abs(int(after.get("left") or 0) - int(before.get("left") or 0)) > 4
            or abs(int(after.get("top") or 0) - int(before.get("top") or 0)) > 4
        )
        return {
            "ok": True,
            "enabled": True,
            "applied": applied,
            "before": before,
            "after": after,
            "target": effective_target,
            "requested_target": requested_target,
            "dpi_scale": dpi_scale,
            "resolution_scale": resolution_scale,
            "enforce_recommended": enforce_recommended,
            "recommended_floor_applied": recommended_floor_applied,
            "fixed_origin": fixed_origin,
            "screen": {"width": screen_width, "height": screen_height},
            "reason": "normalized" if applied else "move_attempt_no_change",
        }
    except Exception as exc:
        return {
            "ok": False,
            "enabled": True,
            "applied": False,
            "before": before,
            "target": effective_target,
            "requested_target": requested_target,
            "dpi_scale": dpi_scale,
            "resolution_scale": resolution_scale,
            "enforce_recommended": enforce_recommended,
            "recommended_floor_applied": recommended_floor_applied,
            "fixed_origin": fixed_origin,
            "error": repr(exc),
            "reason": "normalize_failed",
        }


def is_session_name_candidate(text: str) -> bool:
    return win32_ocr_text.is_session_name_candidate(text)


def is_session_time_text(text: str) -> bool:
    return win32_ocr_text.is_session_time_text(text)


def is_message_noise(text: str) -> bool:
    return win32_ocr_text.is_message_noise(text)


def infer_conversation_type(name: str) -> str:
    return win32_ocr_text.infer_conversation_type(name)


def bounded_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    return win32_ocr_geometry.bounded_int(value, default=default, minimum=minimum, maximum=maximum)


def bounded_float(value: Any, *, default: float, minimum: float, maximum: float) -> float:
    return win32_ocr_geometry.bounded_float(value, default=default, minimum=minimum, maximum=maximum)


def args_for_daemon_request(request: dict[str, Any]) -> list[str]:
    action = str(request.get("action") or "").strip().lower()
    if action not in set(SIDECAR_ACTION_CHOICES):
        action = "status"
    argv: list[str] = [action]
    sidecar_run_id = str(request.get("sidecar_run_id") or request.get("run_id") or "").strip()
    if sidecar_run_id:
        argv.extend(["--sidecar-run-id", sidecar_run_id])
    if bool(request.get("exact")):
        argv.append("--exact")
    target = str(request.get("target") or "").strip()
    if target:
        argv.extend(["--target", target])
    session_key = str(request.get("session_key") or "").strip()
    if session_key:
        argv.extend(["--session-key", session_key])
    text = str(request.get("text") or "")
    if action == "send" and text:
        argv.extend(["--text", text])
    for key, flag in (
        ("phone", "--phone"),
        ("wechat", "--wechat"),
    ):
        value = str(request.get(key) or "")
        if add_friend_route_accepts_query(action) and value:
            argv.extend([flag, value])
    for key, flag in (
        ("verify_message", "--verify-message"),
        ("remark_name", "--remark-name"),
        ("remark_code", "--remark-code"),
    ):
        value = str(request.get(key) or "")
        if add_friend_route_accepts_formal_fields(action) and value:
            argv.extend([flag, value])
    if bool(request.get("skip_send_rate_guard")):
        argv.append("--skip-send-rate-guard")
    if action in ADD_FRIEND_ROUTES and bool(request.get("calibration_only")):
        argv.append("--calibration-only")
    if action == "messages":
        target_mode = str(request.get("target_mode") or "").strip()
        if target_mode:
            argv.extend(["--target-mode", target_mode])
        remark_code = str(request.get("remark_code") or "").strip()
        if remark_code:
            argv.extend(["--remark-code", remark_code])
        numeric_flags = (
            ("history_load_times", "--history-load-times"),
            ("max_scroll_steps", "--max-scroll-steps"),
            ("max_duration_seconds", "--max-duration-seconds"),
            ("max_snapshots", "--max-snapshots"),
            ("min_delay_ms", "--min-delay-ms"),
            ("max_delay_ms", "--max-delay-ms"),
        )
        for key, flag in numeric_flags:
            if key in request:
                try:
                    value = int(request.get(key) or 0)
                except (TypeError, ValueError):
                    value = 0
                argv.extend([flag, str(max(0, value))])
        history_mode = str(request.get("history_mode") or "").strip()
        if history_mode:
            argv.extend(["--history-mode", history_mode])
        for key, flag in (
            ("anchor_ids", "--anchor-id"),
            ("anchor_content_keys", "--anchor-content-key"),
            ("reply_content_keys", "--reply-content-key"),
        ):
            values = request.get(key)
            if isinstance(values, list):
                for item in values:
                    clean = str(item or "").strip()
                    if clean:
                        argv.extend([flag, clean])
        if request.get("restore_to_latest") is True:
            argv.append("--restore-to-latest")
        elif request.get("restore_to_latest") is False:
            argv.append("--no-restore-to-latest")
    artifact_dir = str(request.get("artifact_dir") or "").strip()
    if artifact_dir:
        argv.extend(["--artifact-dir", artifact_dir])
    return argv


def run_daemon_loop() -> int:
    while True:
        line = sys.stdin.readline()
        if not line:
            break
        clean = str(line).strip()
        if not clean:
            continue
        try:
            request = json.loads(clean)
        except json.JSONDecodeError:
            print(json.dumps({"ok": False, "state": "daemon_invalid_json", "error": "invalid_json"}, ensure_ascii=True), flush=True)
            continue
        if not isinstance(request, dict):
            print(json.dumps({"ok": False, "state": "daemon_invalid_request", "error": "request_must_be_object"}, ensure_ascii=True), flush=True)
            continue
        if str(request.get("action") or "").strip().lower() in {"exit", "quit", "stop"}:
            print(json.dumps({"ok": True, "state": "daemon_exit"}, ensure_ascii=True), flush=True)
            return 0
        argv = args_for_daemon_request(request)
        env_overrides = request.get("_env_overrides") if isinstance(request.get("_env_overrides"), dict) else {}
        original_env: dict[str, str | None] = {}
        if env_overrides:
            for key, value in env_overrides.items():
                clean_key = str(key or "").strip()
                if not clean_key:
                    continue
                original_env[clean_key] = os.getenv(clean_key)
                os.environ[clean_key] = str(value)
        try:
            payload = run_sidecar_cli(argv)
        except Exception as exc:  # noqa: BLE001
            payload = exception_payload_for_sidecar(exc, state="daemon_dispatch_failed")
            payload["request"] = request
        finally:
            if env_overrides:
                for key, old_value in original_env.items():
                    if old_value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = old_value
        print(json.dumps(payload, ensure_ascii=True), flush=True)
    return 0


def run_sidecar_cli(argv: list[str] | None = None) -> dict[str, Any]:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=SIDECAR_ACTION_CHOICES, nargs="?")
    parser.add_argument("--sidecar-run-id", default="", help="Correlation id for one Worker-to-sidecar run.")
    parser.add_argument("--target", help="Chat name for messages/send.")
    parser.add_argument("--session-key", default="", help="Internal session key for row-level RPA targeting.")
    parser.add_argument("--target-mode", default="", help="Targeting mode for messages, e.g. search_by_remark_code.")
    parser.add_argument("--text", help="Message text for send.")
    parser.add_argument("--phone", default="", help="Phone number for add-friend.")
    parser.add_argument("--wechat", default="", help="WeChat ID for add-friend fallback.")
    parser.add_argument("--verify-message", default="", help="Required add-friend verification message for the entry-click route.")
    parser.add_argument("--remark-name", default="", help="Required WeChat remark name for the entry-click route.")
    parser.add_argument("--remark-code", default="", help="Required system remark code that must be included in remark-name.")
    parser.add_argument("--calibration-only", action="store_true", help="For add-friend routes, capture/OCR/locate/report without clicking.")
    parser.add_argument("--exact", action="store_true", help="Use exact chat name matching.")
    parser.add_argument(
        "--skip-send-rate-guard",
        action="store_true",
        help="Skip rate guard reservation for controlled loopback simulation only.",
    )
    parser.add_argument("--history-load-times", type=int, default=0, help="Scroll upward this many times before reading messages.")
    parser.add_argument("--history-mode", default="", help="History loading strategy, e.g. anchor_until_found.")
    parser.add_argument("--anchor-id", action="append", default=[], help="Message id anchor to stop bounded history search.")
    parser.add_argument("--anchor-content-key", action="append", default=[], help="Normalized customer message content key anchor.")
    parser.add_argument("--reply-content-key", action="append", default=[], help="Normalized self reply content key anchor.")
    parser.add_argument("--max-scroll-steps", type=int, default=6, help="Maximum bounded upward scroll steps for anchor history search.")
    parser.add_argument("--max-duration-seconds", type=int, default=12, help="Maximum bounded anchor history search duration.")
    parser.add_argument("--max-snapshots", type=int, default=8, help="Maximum screenshots during anchor history search.")
    parser.add_argument("--min-delay-ms", type=int, default=180, help="Minimum pause between bounded anchor search scrolls.")
    parser.add_argument("--max-delay-ms", type=int, default=650, help="Maximum pause between bounded anchor search scrolls.")
    parser.add_argument("--restore-to-latest", dest="restore_to_latest", action="store_true", default=None)
    parser.add_argument("--no-restore-to-latest", dest="restore_to_latest", action="store_false")
    parser.add_argument(
        "--artifact-dir",
        default="",
        help="Optional directory for OCR screenshots and diagnostics.",
    )
    parser.add_argument("--daemon", action="store_true", help="Run as stdin/stdout JSON daemon.")
    args = parser.parse_args(argv)
    if args.daemon:
        return {"ok": False, "state": "daemon_reentry_not_supported"}
    configure_dpi_awareness()
    return run_action(args)


if __name__ == "__main__":
    if "--daemon" in sys.argv:
        raise SystemExit(run_daemon_loop())
    payload = run_sidecar_cli()
    print(json.dumps(payload, ensure_ascii=True))
    raise SystemExit(0 if bool(payload.get("ok")) else 1)
