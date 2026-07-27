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
from datetime import datetime, timezone
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
        SW_MINIMIZE = 6

    win32con = _Win32ConFallback()  # type: ignore[assignment]
from PIL import Image, ImageDraw, ImageEnhance, ImageFont, ImageGrab, ImageStat

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
from apps.wechat_ai_customer_service.adapters.wechat_win32_ocr import interaction_evidence as win32_ocr_interaction_evidence
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


DEFAULT_MESSAGE_BOTTOM_EXCLUDE_PX = 95
OCR_MIN_CONFIDENCE = 0.45
SIDECAR_BASE_ACTIONS = ("status", "capabilities", "sessions", "open-chat", "messages", "send", "recover-render", "voice-transcribe")
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
VOICE_TRANSCRIBE_COLLAPSE_TEXT_TOKENS = ("收起文字", "收起")
CHAT_INFO_PANEL_TEXT_TOKENS = ("查找聊天内容", "消息免打扰", "置顶聊天", "清空聊天记录")
TEXT_MESSAGE_CONTEXT_MENU_TOKENS = ("复制", "放大阅读", "翻译", "搜一搜", "转发")
AVATAR_CONTEXT_MENU_TOKENS = ("拍一拍",)
DEFAULT_RENDER_RECOVERY_MIN_INTERVAL_SECONDS = 180
DEFAULT_QUICK_LOGIN_AUTO_ENTER = False
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

# This Chejin-only adapter schema is generated from c2_contract_v3.json. OmniAuto
# generic modules stay contract-agnostic, while this adapter cannot drift into a
# second handwritten set of message rules.
_C2_GENERATED_SCHEMA_PATH = Path(__file__).with_name("chejin_c2_observation_schema.generated.json")
_C2_GENERATED_SCHEMA = json.loads(_C2_GENERATED_SCHEMA_PATH.read_text(encoding="utf-8"))
C2_OBSERVATION_SCHEMA_VERSION = int(_C2_GENERATED_SCHEMA["observation_schema_version"])
C2_OBSERVATION_CONTRACT_REVISION = str(_C2_GENERATED_SCHEMA["contract_revision"])
C2_OBSERVATION_CONTRACT_SHA256 = str(_C2_GENERATED_SCHEMA["contract_sha256"])
C2_ACTION_PHASES = tuple(
    str(value) for value in _C2_GENERATED_SCHEMA["action_phases"]
)
MESSAGE_OBSERVATION_SENDER_ROLES = frozenset(
    str(value) for value in _C2_GENERATED_SCHEMA["sender_roles"]
)
C2_ROW_RULES = {
    str(row_kind): dict(rule)
    for row_kind, rule in dict(_C2_GENERATED_SCHEMA["row_rules"]).items()
}
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
DEFAULT_SEND_TRIGGER_MODE = "click_only"
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
    parser.add_argument(
        "--conversation-type",
        default="",
        help="Caller metadata only; C2 private admission still requires title OCR evidence.",
    )
    parser.add_argument("--target-mode", default="", help="Targeting mode for messages, e.g. search_by_remark_code.")
    parser.add_argument("--visible-session-candidate", default="", help="JSON row candidate from the same Worker visible-session scan.")
    parser.add_argument("--text", help="Message text for send.")
    parser.add_argument("--phone", default="", help="Phone number for add-friend.")
    parser.add_argument("--wechat", default="", help="WeChat ID for add-friend fallback.")
    parser.add_argument("--verify-message", default="", help="Required add-friend verification message for the entry-click route.")
    parser.add_argument("--remark-name", default="", help="Required WeChat remark name for the entry-click route.")
    parser.add_argument("--remark-code", default="", help="Required system remark code that must be included in remark-name.")
    parser.add_argument("--calibration-only", action="store_true", help="For add-friend routes, capture/OCR/locate/report without clicking.")
    parser.add_argument("--exact", action="store_true", help="Use exact chat name matching.")
    parser.add_argument(
        "--current-only",
        action="store_true",
        help="For send, validate the current chat only and never search or switch sessions.",
    )
    parser.add_argument(
        "--expected-context-guard",
        default="",
        help="JSON context guard from the final C2 read; required before C2-C3 send.",
    )
    parser.add_argument(
        "--action-journal",
        default="",
        help="Worker-owned JSON journal for an irreversible send, voice, image, or add-friend action.",
    )
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
    parser.add_argument("--excluded-voice-anchor-keys", default="[]", help="JSON list of persistent voice anchors that must not be clicked.")
    parser.add_argument(
        "--capture-initial-messages",
        action="store_true",
        help="Reuse the post-open title-confirmation frame as the first message read.",
    )
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


def locate_chat_target_for_c2(
    hwnd: int,
    *,
    target: str,
    session_key: str,
    remark_code: str,
    target_mode: str,
    visible_session_candidate: dict[str, Any] | None = None,
    exact: bool,
    artifact_dir: str | None,
    sidecar_run_id: str,
    failure_state: str,
    failure_error_code: str,
) -> dict[str, Any]:
    clean_target = str(target or "").strip()
    clean_session_key = str(session_key or "").strip()
    clean_remark_code = str(remark_code or "").strip()
    normalized_mode = str(target_mode or "").strip().lower() or "visible"
    visible_candidate = visible_session_candidate if isinstance(visible_session_candidate, dict) else {}
    targeting: dict[str, Any] = {}
    if visible_candidate.get("_parse_error"):
        targeting["visible_session_candidate_parse_error"] = visible_candidate.get("_parse_error")
        visible_candidate = {}
    opened = False

    def finish(
        *,
        ok: bool,
        validation: dict[str, Any] | None = None,
        state: str | None = None,
        error_code: str | None = None,
        error: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "ok": bool(ok),
            "online": bool((validation or {}).get("online", True)),
            "state": "chat_target_confirmed" if ok else (state or failure_state),
            "target": clean_target,
            "remark_code": clean_remark_code,
            "target_mode": normalized_mode,
            "opened": bool(opened),
            "guard": validation or {},
            "targeting": targeting,
            "step_events": targeting.get("step_events") if isinstance(targeting, dict) else None,
            "review_path": targeting.get("review_path") if isinstance(targeting, dict) else None,
            "evidence_path": targeting.get("evidence_path") if isinstance(targeting, dict) else None,
            "open_chat_timing": dict(_LAST_OPEN_CHAT_TIMING),
        }
        if not ok:
            payload["error_code"] = error_code or failure_error_code
            payload["error"] = error or "The target chat was not confirmed."
        if artifact_dir and not payload.get("review_path"):
            try:
                review_payload = {
                    "ok": bool(ok),
                    "reason": payload.get("state"),
                    "partial": False,
                    "sidecar_run_id": sidecar_run_id,
                    "target_mode": normalized_mode,
                    "target": clean_target,
                    "remark_code": clean_remark_code,
                    "targeting": targeting,
                    "guard": validation or {},
                    "open_chat_timing": dict(_LAST_OPEN_CHAT_TIMING),
                }
                review_path = write_messages_targeting_review(Path(artifact_dir), review_payload)
                payload["review_path"] = review_path
                payload["evidence_path"] = review_path
            except Exception as exc:
                payload["review_error"] = repr(exc)
        return payload

    if not clean_remark_code:
        return finish(
            ok=False,
            state=failure_state,
            error_code="C2_TARGET_REMARK_CODE_MISSING",
            error="C2 requires a valid remark_code before any chat can be opened.",
        )
    if clean_remark_code.upper() not in extract_c2_remark_codes(clean_remark_code):
        return finish(
            ok=False,
            state=failure_state,
            error_code="C2_TARGET_REMARK_CODE_INVALID",
            error="C2 remark_code does not match the supported short-code contract.",
        )

    if normalized_mode == "search_by_remark_code":
        targeting = open_chat_by_remark_code_search(
            hwnd,
            target=clean_target or clean_remark_code,
            remark_code=clean_remark_code,
            artifact_dir=artifact_dir,
            sidecar_run_id=sidecar_run_id,
        )
        opened = bool(targeting.get("ok"))
        validation = targeting.get("validation") if isinstance(targeting.get("validation"), dict) else None
        if validation is None and opened:
            validation = validate_active_send_target(
                hwnd,
                clean_remark_code or clean_target,
                exact=False,
                artifact_dir=artifact_dir,
            )
        if not opened or not c2_target_activation_confirmed(validation):
            admission_code, admission_error = c2_target_admission_error(validation, str(targeting.get("error_code") or failure_error_code))
            return finish(
                ok=False,
                validation=validation,
                state=failure_state,
                error_code=admission_code,
                error=admission_error,
            )
        return finish(ok=True, validation=validation)

    if normalized_mode == "current":
        validation_target = clean_remark_code or clean_target
        validation = validate_active_send_target(
            hwnd,
            validation_target,
            exact=False if clean_remark_code else bool(exact),
            artifact_dir=artifact_dir,
        )
        if not c2_target_activation_confirmed(validation):
            admission_code, admission_error = c2_target_admission_error(validation, failure_error_code)
            return finish(
                ok=False,
                validation=validation,
                state=failure_state,
                error_code=admission_code,
                error=admission_error,
            )
        return finish(ok=True, validation=validation)

    validation_target = clean_remark_code or clean_target
    validation_exact = False if clean_remark_code else bool(exact)
    # open_chat starts from a fresh screenshot and checks both the active chat
    # and the visible session list. A separate full OCR here duplicates the
    # same evidence without making a later click safer.
    targeting["visible_precheck"] = {
        "skipped": True,
        "reason": "merged_into_open_chat_fresh_rescan",
    }
    if normalized_mode == "visible" and visible_candidate:
        # Worker observations may be several screenshots old and window
        # normalization can move every row. Keep them as semantic hints only;
        # open_chat captures a fresh frame and reacquires the session_key before
        # any physical click.
        targeting["visible_session_candidate_seed"] = {
            "name": visible_candidate.get("name") or clean_target or clean_remark_code,
            "session_key": visible_candidate.get("session_key") or clean_session_key,
            "row_fingerprint": visible_candidate.get("row_fingerprint"),
            "source": visible_candidate.get("source") or "worker_visible_scan",
        }
        targeting["visible_session_candidate_activation"] = {
            "ok": False,
            "skipped": True,
            "reason": "fresh_semantic_reacquire_required",
        }
        targeting["visible_session_candidate_fallback"] = "open_chat_fresh_rescan"
    open_chat_started_at = time.monotonic()
    opened = open_chat(
        hwnd,
        clean_target or clean_remark_code,
        exact=bool(exact),
        artifact_dir=artifact_dir,
        session_key=clean_session_key,
        semantic_target=clean_remark_code,
    )
    validation = None
    initial_title_evidence = _LAST_OPEN_CHAT_TIMING.get("open_chat_initial_active_evidence")
    if (
        not opened
        and isinstance(initial_title_evidence, dict)
        and initial_title_evidence.get("short_code_confirmed") is True
        and str(initial_title_evidence.get("conversation_type") or "unknown") in {"group", "unknown"}
    ):
        conversation_type = str(initial_title_evidence.get("conversation_type") or "unknown")
        validation = {
            "ok": False,
            "online": True,
            "reason": f"active_{conversation_type}_remark_code_blocked",
            "active_title_match": True,
            "confirmation_confidence": "active_title_strict",
            "conversation_type": conversation_type,
            "conversation_type_evidence": dict(initial_title_evidence),
        }
    reused_session_key = str(_LAST_RPA_ACTION_STATE.get("active_session_key") or clean_session_key).strip()
    if opened:
        validation = consume_recent_target_switch_validation(
            hwnd=hwnd,
            target=validation_target,
            exact=validation_exact,
            session_key=reused_session_key,
            minimum_cached_at=open_chat_started_at,
            require_session_key_match=bool(clean_session_key),
        )
    targeting["visible_postcheck"] = {
        "reused": isinstance(validation, dict),
        "reason": (
            "terminal_active_title_admission_reused"
            if isinstance(validation, dict) and not opened
            else "strict_open_chat_switch_validation_reused"
            if isinstance(validation, dict)
            else "strict_open_chat_switch_validation_unavailable"
        ),
        "session_key": reused_session_key,
    }
    if validation is None:
        if opened:
            humanized_action_sleep(260, 420)
        validation = validate_active_send_target(
            hwnd,
            validation_target,
            exact=validation_exact,
            artifact_dir=artifact_dir,
        )
        targeting["visible_postcheck"]["fallback_full_ocr"] = True
    else:
        targeting["visible_postcheck"]["fallback_full_ocr"] = False
    if not opened or not c2_target_activation_confirmed(validation):
        open_reason = str(_LAST_OPEN_CHAT_TIMING.get("reason") or "")
        ambiguous_visible_target = open_reason in {
            "active_visible_ambiguous",
            "semantic_candidate_ambiguous",
            "session_key_drift_semantic_candidate_ambiguous",
        }
        admission_code, admission_error = c2_target_admission_error(validation, failure_error_code)
        return finish(
            ok=False,
            validation=validation,
            state=failure_state,
            error_code="C2_VISIBLE_TARGET_AMBIGUOUS" if ambiguous_visible_target else admission_code,
            error=(
                "Visible session target was ambiguous; stop before search fallback."
                if ambiguous_visible_target
                else admission_error
            ),
        )
    return finish(ok=True, validation=validation)


def parse_visible_session_candidate_arg(raw: Any) -> dict[str, Any] | None:
    clean = str(raw or "").strip()
    if not clean:
        return None
    try:
        parsed = json.loads(clean)
    except json.JSONDecodeError as exc:
        return {"_parse_error": f"invalid_json: {exc}", "_raw": clean[:1000]}
    if not isinstance(parsed, dict):
        return {"_parse_error": f"candidate_must_be_object: {type(parsed).__name__}", "_raw": clean[:1000]}
    return parsed


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
        quick_login_auto_enter = env_flag(
            "WECHAT_WIN32_OCR_QUICK_LOGIN_AUTO_ENTER",
            default=DEFAULT_QUICK_LOGIN_AUTO_ENTER,
        )
        first_business_frame_has_full_health_gate = action == "open-chat" or (
            action in {"messages", "voice-transcribe"} and bool(getattr(args, "target", ""))
        )
        if first_business_frame_has_full_health_gate and not quick_login_auto_enter:
            # These actions all begin with a fresh full-frame OCR gate before
            # any click. Reuse that business frame for login/blank/shell checks
            # instead of taking an unchanged quick-login frame first.
            quick_login = {
                "attempted": False,
                "detected": False,
                "reason": "merged_into_first_business_frame",
            }
        else:
            quick_login = ensure_quick_login_if_available(
                hwnd,
                artifact_dir=args.artifact_dir,
                auto_enter=quick_login_auto_enter,
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
    if action == "open-chat":
        clean_sidecar_run_id = str(getattr(args, "sidecar_run_id", "") or "").strip()
        if not args.target:
            return {
                "ok": False,
                "online": True,
                "adapter": "win32_ocr",
                "state": "target_not_confirmed",
                "sidecar_run_id": clean_sidecar_run_id,
                "error_code": "C2_TARGET_LOCATOR_MISSING",
                "window_probe": probe,
                "target_mode": str(args.target_mode or "").strip().lower() or "visible",
                "remark_code": str(args.remark_code or "").strip(),
                "error": "open-chat requires target.",
            }
        locate = locate_chat_target_for_c2(
            hwnd,
            target=args.target,
            session_key=str(args.session_key or ""),
            remark_code=str(args.remark_code or ""),
            target_mode=str(args.target_mode or "").strip().lower(),
            visible_session_candidate=parse_visible_session_candidate_arg(getattr(args, "visible_session_candidate", "")),
            exact=bool(args.exact),
            artifact_dir=args.artifact_dir,
            sidecar_run_id=clean_sidecar_run_id,
            failure_state="target_not_confirmed",
            failure_error_code="TARGET_NOT_CONFIRMED",
        )
        result = {
            **locate,
            "adapter": "win32_ocr",
            "state": "chat_target_confirmed" if locate.get("ok") else str(locate.get("state") or "target_not_confirmed"),
            "sidecar_run_id": clean_sidecar_run_id,
            "window_probe": probe,
        }
        if locate.get("ok") and bool(getattr(args, "capture_initial_messages", False)):
            guard = locate.get("guard") if isinstance(locate.get("guard"), dict) else {}
            validation_target = str(args.remark_code or "").strip() or str(args.target or "").strip()
            seed = consume_target_ready_prevalidation_ocr_seed(
                hwnd=hwnd,
                target=validation_target,
                exact=False if str(args.remark_code or "").strip() else bool(args.exact),
                geometry=guard.get("geometry") if isinstance(guard.get("geometry"), dict) else get_window_geometry(hwnd),
            )
            if isinstance(seed, dict):
                screenshot = seed.get("screenshot")
                ocr_items = list(seed.get("ocr_items") or [])
                if screenshot is not None and ocr_items:
                    parsed_messages = parse_messages_from_ocr(
                        ocr_items,
                        screenshot.size,
                        target=str(args.target or ""),
                        screenshot=screenshot,
                    )
                    snapshot = {
                        "label": "open_chat_confirmation",
                        "screenshot_path": str(seed.get("screenshot_path") or ""),
                        "screenshot": screenshot,
                        "ocr_items": ocr_items,
                        "messages": parsed_messages,
                        "visible_untranscribed_voice": visible_untranscribed_voice_hint(
                            screenshot,
                            ocr_items,
                            screenshot.size,
                            parsed_messages=parsed_messages,
                        ),
                    }
                    initial_messages = messages_payload(
                        hwnd,
                        probe,
                        target=str(args.target or ""),
                        history_load_times=0,
                        max_scroll_steps=0,
                        max_duration_seconds=1,
                        max_snapshots=1,
                        artifact_dir=args.artifact_dir,
                        confirm_target=validation_target,
                        confirm_exact=False if str(args.remark_code or "").strip() else bool(args.exact),
                        seed_snapshot=snapshot,
                    )
                    if initial_messages.get("ok"):
                        initial_messages["sidecar_run_id"] = clean_sidecar_run_id
                        initial_messages["target_mode"] = str(args.target_mode or "").strip().lower() or "visible"
                        initial_messages["remark_code"] = str(args.remark_code or "").strip()
                        initial_messages["authoritative_frame_source"] = "initial_read"
                        result["initial_messages_snapshot"] = initial_messages
                        result["initial_messages_frame_reused"] = True
                        result["initial_messages_frame_age_seconds"] = seed.get("age_seconds")
        return result
    if action == "messages":
        clean_sidecar_run_id = str(getattr(args, "sidecar_run_id", "") or "").strip()
        c2_targeted_action = bool(args.target and (clean_sidecar_run_id or str(args.remark_code or "").strip()))
        target_mode = str(args.target_mode or "").strip().lower() or "visible"
        single_frame_confirmation = bool(args.target and target_mode == "current")
        targeting: dict[str, Any] = {}
        locate: dict[str, Any] = {}
        if args.target and not single_frame_confirmation:
            locate = locate_chat_target_for_c2(
                hwnd,
                target=args.target,
                session_key=str(args.session_key or ""),
                remark_code=str(args.remark_code or ""),
                target_mode=str(args.target_mode or "").strip().lower(),
                exact=bool(args.exact),
                artifact_dir=args.artifact_dir,
                sidecar_run_id=clean_sidecar_run_id,
                failure_state="target_not_confirmed_for_messages",
                failure_error_code="TARGET_NOT_CONFIRMED_FOR_MESSAGES",
            )
            targeting = locate.get("targeting") if isinstance(locate.get("targeting"), dict) else {}
            if not locate.get("ok"):
                return {
                    "ok": False,
                    "online": bool(locate.get("online", True)),
                    "adapter": "win32_ocr",
                    "state": "target_not_confirmed_for_messages",
                    "sidecar_run_id": clean_sidecar_run_id,
                    "error_code": str(locate.get("error_code") or "TARGET_NOT_CONFIRMED_FOR_MESSAGES"),
                    "window_probe": probe,
                    "target": args.target,
                    "remark_code": str(args.remark_code or "").strip(),
                    "target_mode": str(args.target_mode or "").strip().lower() or "visible",
                    "opened": bool(locate.get("opened")),
                    "guard": locate.get("guard") if isinstance(locate.get("guard"), dict) else {},
                    "targeting": targeting,
                    "step_events": targeting.get("step_events") if isinstance(targeting, dict) else None,
                    "review_path": locate.get("review_path"),
                    "evidence_path": locate.get("evidence_path"),
                    "open_chat_timing": locate.get("open_chat_timing") or dict(_LAST_OPEN_CHAT_TIMING),
                    "error": "The target chat was not confirmed before reading messages.",
                }
            if not c2_targeted_action and scroll_to_latest_before_read_enabled():
                scroll_chat_to_latest(hwnd)
        elif single_frame_confirmation:
            targeting = {
                "current_frame_confirmation": {
                    "merged": True,
                    "reason": "messages_frame_confirms_target_before_parse",
                }
            }
        load_times = 0 if c2_targeted_action else bounded_int(args.history_load_times, default=0, minimum=0, maximum=16)
        confirmation_target = str(args.remark_code or "").strip() or str(args.target or "").strip()
        payload = messages_payload(
            hwnd,
            probe,
            target=args.target or "",
            history_load_times=load_times,
            history_mode=str(args.history_mode or ""),
            anchor_ids=[str(item) for item in args.anchor_id or []],
            anchor_content_keys=[str(item) for item in args.anchor_content_key or []],
            reply_content_keys=[str(item) for item in args.reply_content_key or []],
            max_scroll_steps=0 if c2_targeted_action else bounded_int(args.max_scroll_steps, default=6, minimum=0, maximum=16),
            max_duration_seconds=bounded_int(args.max_duration_seconds, default=12, minimum=1, maximum=60),
            max_snapshots=1 if c2_targeted_action else bounded_int(args.max_snapshots, default=8, minimum=1, maximum=24),
            min_delay_ms=bounded_int(args.min_delay_ms, default=180, minimum=0, maximum=5000),
            max_delay_ms=bounded_int(args.max_delay_ms, default=650, minimum=0, maximum=10000),
            restore_to_latest=True if args.restore_to_latest is None else bool(args.restore_to_latest),
            artifact_dir=args.artifact_dir,
            confirm_target=confirmation_target if single_frame_confirmation else "",
            confirm_exact=False if str(args.remark_code or "").strip() else bool(args.exact),
        )
        if args.target:
            if single_frame_confirmation:
                guard = payload.get("target_confirmation") if isinstance(payload.get("target_confirmation"), dict) else {}
                locate = {
                    "ok": c2_target_activation_confirmed(guard),
                    "online": bool(guard.get("online", True)),
                    "state": "chat_target_confirmed" if c2_target_activation_confirmed(guard) else "target_not_confirmed_for_messages",
                    "error_code": None if c2_target_activation_confirmed(guard) else c2_target_admission_error(guard, "TARGET_NOT_CONFIRMED_FOR_MESSAGES")[0],
                    "target": str(args.target or ""),
                    "remark_code": str(args.remark_code or "").strip(),
                    "target_mode": target_mode,
                    "opened": False,
                    "guard": guard,
                    "targeting": targeting,
                }
            payload["sidecar_run_id"] = clean_sidecar_run_id
            payload["target_mode"] = target_mode
            payload["remark_code"] = str(args.remark_code or "").strip()
            payload["targeting"] = targeting if isinstance(targeting, dict) else {}
            payload["target_confirmation"] = locate
            if isinstance(targeting, dict) and targeting.get("step_events"):
                payload["step_events"] = targeting.get("step_events")
            if isinstance(targeting, dict) and targeting.get("review_path"):
                payload["review_path"] = targeting.get("review_path")
                payload["evidence_path"] = targeting.get("evidence_path") or targeting.get("review_path")
        return payload
    if action == "voice-transcribe":
        clean_sidecar_run_id = str(getattr(args, "sidecar_run_id", "") or "").strip()
        c2_targeted_action = bool(args.target and (clean_sidecar_run_id or str(args.remark_code or "").strip()))
        clean_remark_code = str(args.remark_code or "").strip()
        target_mode = str(args.target_mode or "").strip().lower() or "visible"
        single_frame_confirmation = bool(args.target and target_mode == "current")
        targeting: dict[str, Any] = {}
        locate: dict[str, Any] = {}
        if args.target and not single_frame_confirmation:
            locate = locate_chat_target_for_c2(
                hwnd,
                target=args.target,
                session_key=str(args.session_key or ""),
                remark_code=clean_remark_code,
                target_mode=target_mode,
                exact=bool(args.exact),
                artifact_dir=args.artifact_dir,
                sidecar_run_id=clean_sidecar_run_id,
                failure_state="target_not_confirmed_for_voice_transcribe",
                failure_error_code="TARGET_NOT_CONFIRMED_FOR_VOICE_TRANSCRIBE",
            )
            targeting = locate.get("targeting") if isinstance(locate.get("targeting"), dict) else {}
            if not locate.get("ok"):
                return {
                    "ok": False,
                    "online": bool(locate.get("online", True)),
                    "adapter": "win32_ocr",
                    "state": "target_not_confirmed_for_voice_transcribe",
                    "sidecar_run_id": clean_sidecar_run_id,
                    "error_code": str(locate.get("error_code") or "TARGET_NOT_CONFIRMED_FOR_VOICE_TRANSCRIBE"),
                    "window_probe": probe,
                    "target": args.target,
                    "remark_code": clean_remark_code,
                    "target_mode": target_mode or "visible",
                    "opened": bool(locate.get("opened")),
                    "guard": locate.get("guard") if isinstance(locate.get("guard"), dict) else {},
                    "targeting": targeting,
                    "step_events": targeting.get("step_events") if isinstance(targeting, dict) else None,
                    "review_path": locate.get("review_path"),
                    "evidence_path": locate.get("evidence_path"),
                    "open_chat_timing": locate.get("open_chat_timing") or dict(_LAST_OPEN_CHAT_TIMING),
                    "error": "The target chat was not confirmed before clicking voice transcription.",
                }
            if not c2_targeted_action and scroll_to_latest_before_read_enabled():
                scroll_chat_to_latest(hwnd)
        elif single_frame_confirmation:
            targeting = {
                "current_frame_confirmation": {
                    "merged": True,
                    "reason": "voice_before_frame_confirms_target_before_click",
                }
            }
        confirmation_target = clean_remark_code or str(args.target or "").strip()
        try:
            parsed_excluded_voice_anchor_keys = json.loads(str(getattr(args, "excluded_voice_anchor_keys", "[]") or "[]"))
        except json.JSONDecodeError:
            parsed_excluded_voice_anchor_keys = []
        if not isinstance(parsed_excluded_voice_anchor_keys, list):
            parsed_excluded_voice_anchor_keys = []
        payload = voice_transcribe_payload(
            hwnd,
            probe,
            target=args.target or "",
            artifact_dir=args.artifact_dir,
            max_duration_seconds=bounded_int(args.max_duration_seconds, default=240, minimum=15, maximum=900),
            confirm_target=confirmation_target if single_frame_confirmation else "",
            confirm_exact=False if clean_remark_code else bool(args.exact),
            excluded_voice_anchor_keys={
                str(value).strip() for value in parsed_excluded_voice_anchor_keys if str(value).strip()
            },
            action_journal_path=str(
                getattr(args, "action_journal", "") or ""
            ).strip(),
        )
        payload["sidecar_run_id"] = clean_sidecar_run_id
        payload["target_mode"] = target_mode or "visible"
        payload["remark_code"] = clean_remark_code
        payload["targeting"] = targeting if isinstance(targeting, dict) else {}
        if args.target:
            if single_frame_confirmation:
                guard = payload.get("target_confirmation") if isinstance(payload.get("target_confirmation"), dict) else {}
                locate = {
                    "ok": c2_target_activation_confirmed(guard),
                    "online": bool(guard.get("online", True)),
                    "state": "chat_target_confirmed" if c2_target_activation_confirmed(guard) else "target_not_confirmed_for_voice_transcribe",
                    "error_code": None if c2_target_activation_confirmed(guard) else c2_target_admission_error(guard, "TARGET_NOT_CONFIRMED_FOR_VOICE_TRANSCRIBE")[0],
                    "target": str(args.target or ""),
                    "remark_code": clean_remark_code,
                    "target_mode": target_mode,
                    "opened": False,
                    "guard": guard,
                    "targeting": targeting,
                }
            payload["target_confirmation"] = locate
        if isinstance(targeting, dict) and targeting.get("step_events"):
            payload["step_events"] = targeting.get("step_events")
        if isinstance(targeting, dict) and targeting.get("review_path"):
            payload["review_path"] = targeting.get("review_path")
            payload["evidence_path"] = targeting.get("evidence_path") or targeting.get("review_path")
        return payload
    if action == "send":
        if not args.target:
            raise ValueError("--target is required for send")
        if args.text is None:
            raise ValueError("--text is required for send")
        target_ready_timing: dict[str, Any] = {}
        target_ready_started = _sidecar_timing_start(target_ready_timing, "target_ready")
        current_only = bool(getattr(args, "current_only", False))
        if not current_only:
            return {
                "ok": False,
                "online": True,
                "adapter": "win32_ocr",
                "state": "send_current_only_required",
                "error_code": "SEND_CURRENT_CHAT_ONLY_REQUIRED",
                "target": args.target,
                "error": "C2-C3 send may validate the current chat only; session search/switch is forbidden.",
            }
        current_validation = validate_active_send_target(
            hwnd,
            args.target,
            exact=bool(args.exact),
            artifact_dir=args.artifact_dir,
        )
        target_ready = {
            "ok": bool(current_validation.get("ok") and active_send_guard_is_strong(current_validation)),
            "attempts": 1,
            "validation": current_validation,
            "opened": False,
            "timing": {
                "target_ready_current_only": True,
                "target_ready_session_switch_forbidden": True,
            },
        }
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
            expected_context_guard=parse_expected_send_context_guard(
                getattr(args, "expected_context_guard", "")
            ),
            validated_guard=target_ready.get("validation")
            if isinstance(target_ready.get("validation"), dict)
            else None,
            action_journal_path=str(
                getattr(args, "action_journal", "") or ""
            ),
        )
        if isinstance(send_result_payload, dict):
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
            action_journal_path=str(
                getattr(args, "action_journal", "") or ""
            ).strip(),
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
    items, enhanced_count = session_list_ocr_items(screenshot, run_ocr(screenshot))
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
            "ocr_items_enhanced_count": enhanced_count,
            "ocr_items": compact_ocr_items_for_report(items),
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
            "ocr_items_enhanced_count": enhanced_count,
            "ocr_items": compact_ocr_items_for_report(items),
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
                "raw_title": item.get("raw_title") or item["name"],
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
                "c2_conversation_type": item.get("c2_conversation_type") or "unknown",
                "c2_conversation_admission": item.get("c2_conversation_admission") or {},
                "c2_remark_code_candidates": item.get("c2_remark_code_candidates") or [],
                "source_adapter": "win32_ocr",
            "ocr_confidence": item.get("confidence"),
            }
            for item in sessions
        ],
        "ocr_items_count": len(items),
        "ocr_items_enhanced_count": enhanced_count,
        "ocr_items": compact_ocr_items_for_report(items),
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
    confirm_target: str = "",
    confirm_exact: bool = False,
    seed_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    mode = str(history_mode or "").strip().lower()
    if isinstance(seed_snapshot, dict):
        snapshots = [dict(seed_snapshot)]
        history_load = {
            "ok": True,
            "mode": "reused_open_chat_confirmation_frame",
            "requested_load_times": 0,
            "mechanism": "open_chat_confirmation_frame_reuse",
            "snapshot_count": 1,
        }
    elif mode == "anchor_until_found":
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
    screenshot = latest.get("screenshot")
    geometry = get_window_geometry(hwnd)
    page_fingerprint = ocr_page_fingerprint(ocr_items, geometry=geometry)
    target_confirmation: dict[str, Any] = {}
    if confirm_target:
        target_confirmation = validate_active_send_target(
            hwnd,
            confirm_target,
            exact=confirm_exact,
            artifact_dir=artifact_dir,
            screenshot=screenshot,
            ocr_items=ocr_items,
            screenshot_path=str(latest.get("screenshot_path") or ""),
        )
        if not c2_target_activation_confirmed(target_confirmation):
            admission_code, admission_error = c2_target_admission_error(
                target_confirmation,
                "TARGET_NOT_CONFIRMED_FOR_MESSAGES",
            )
            return {
                "ok": False,
                "online": bool(target_confirmation.get("online", True)),
                "adapter": "win32_ocr",
                "state": "target_not_confirmed_for_messages",
                "error_code": admission_code,
                "window_probe": probe,
                "screenshot_path": str(latest.get("screenshot_path") or ""),
                "chat_info": {"chat_name": target, "source_adapter": "win32_ocr"},
                "ocr_items_count": len(ocr_items),
                "target_confirmation": target_confirmation,
                "error": admission_error,
            }
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
    messages = merge_structural_image_messages(
        screenshot,
        ocr_items,
        merge_message_history_snapshots(snapshots),
        target=target,
    )
    visible_voice_hint = latest.get("visible_untranscribed_voice") if isinstance(latest.get("visible_untranscribed_voice"), dict) else {"detected": False}
    observations = build_message_observations_v3(messages, visible_voice_hint)
    observation_validation_errors = [
        {
            "observation_id": str(observation.get("observation_id") or ""),
            "row_kind": str(observation.get("row_kind") or ""),
            "error_codes": list(observation.get("contract_errors") or []),
        }
        for observation in observations
        if isinstance(observation, dict) and observation.get("contract_errors")
    ]
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
        "observations": observations,
        "send_context_guard": build_send_context_guard(observations),
        "observation_validation_errors": observation_validation_errors,
        "observation_schema_version": C2_OBSERVATION_SCHEMA_VERSION,
        "visible_untranscribed_voice": visible_voice_hint,
        "ocr_items_count": len(ocr_items),
        "target_confirmation": target_confirmation,
    }


def voice_transcribe_payload(
    hwnd: int,
    probe: dict[str, Any],
    *,
    target: str,
    artifact_dir: str | None = None,
    max_duration_seconds: int = 240,
    confirm_target: str = "",
    confirm_exact: bool = False,
    excluded_voice_anchor_keys: set[str] | None = None,
    action_journal_path: str = "",
) -> dict[str, Any]:
    flow_started_at = time.monotonic()
    last_progress_at = flow_started_at
    no_progress_timeout_seconds = max(15, int(max_duration_seconds))
    hard_safety_timeout_seconds = max(900, no_progress_timeout_seconds * 4)
    performance_started_at = time.perf_counter()
    performance_timing: dict[str, Any] = {
        "schema_version": 1,
        "flow": "voice_transcribe_current_chat",
        "watchdog_mode": "no_progress",
        "no_progress_timeout_seconds": no_progress_timeout_seconds,
        "hard_safety_timeout_seconds": hard_safety_timeout_seconds,
        "started_at": _sidecar_timing_now_iso(),
        "operations": [],
    }

    def timed_call(kind: str, purpose: str, callback: Callable[[], Any]) -> Any:
        started = time.perf_counter()
        operation: dict[str, Any] = {
            "kind": kind,
            "purpose": purpose,
            "started_at": _sidecar_timing_now_iso(),
        }
        try:
            result = callback()
        except Exception as exc:
            operation["error"] = type(exc).__name__
            raise
        finally:
            operation["finished_at"] = _sidecar_timing_now_iso()
            operation["duration_seconds"] = round(max(0.0, time.perf_counter() - started), 4)
            performance_timing["operations"].append(operation)
        if kind == "ocr" and isinstance(result, list):
            operation["item_count"] = len(result)
        return result

    def finalize_performance_timing() -> dict[str, Any]:
        operations = performance_timing.get("operations") if isinstance(performance_timing.get("operations"), list) else []
        performance_timing["finished_at"] = _sidecar_timing_now_iso()
        performance_timing["total_duration_seconds"] = round(
            max(0.0, time.perf_counter() - performance_started_at),
            4,
        )
        performance_timing["operation_count"] = len(operations)
        performance_timing["ocr_call_count"] = sum(1 for item in operations if item.get("kind") == "ocr")
        performance_timing["ocr_total_duration_seconds"] = round(
            sum(float(item.get("duration_seconds") or 0.0) for item in operations if item.get("kind") == "ocr"),
            4,
        )
        performance_timing["capture_call_count"] = sum(1 for item in operations if item.get("kind") == "capture")
        performance_timing["capture_total_duration_seconds"] = round(
            sum(float(item.get("duration_seconds") or 0.0) for item in operations if item.get("kind") == "capture"),
            4,
        )
        performance_timing["wait_call_count"] = sum(1 for item in operations if item.get("kind") == "wait")
        performance_timing["wait_total_duration_seconds"] = round(
            sum(float(item.get("duration_seconds") or 0.0) for item in operations if item.get("kind") == "wait"),
            4,
        )
        return performance_timing

    watchdog_stopped_reason = ""
    before_screenshot, before_path = timed_call(
        "capture",
        "voice_transcribe_before",
        lambda: capture_wechat(hwnd, artifact_dir=artifact_dir, label="voice_transcribe_before"),
    )
    before_items = timed_call("ocr", "voice_transcribe_before", lambda: run_ocr(before_screenshot))
    geometry = get_window_geometry(hwnd)
    image_size = getattr(before_screenshot, "size", (int(geometry.get("width") or 0), int(geometry.get("height") or 0)))
    target_confirmation: dict[str, Any] = {}
    if confirm_target:
        target_confirmation = validate_active_send_target(
            hwnd,
            confirm_target,
            exact=confirm_exact,
            artifact_dir=artifact_dir,
            screenshot=before_screenshot,
            ocr_items=before_items,
            screenshot_path=before_path,
        )
        if not c2_target_activation_confirmed(target_confirmation):
            admission_code, admission_error = c2_target_admission_error(
                target_confirmation,
                "TARGET_NOT_CONFIRMED_FOR_VOICE_TRANSCRIBE",
            )
            return {
                "ok": False,
                "online": bool(target_confirmation.get("online", True)),
                "adapter": "win32_ocr",
                "state": "target_not_confirmed_for_voice_transcribe",
                "error_code": admission_code,
                "window_probe": probe,
                "target": target,
                "before_screenshot_path": before_path,
                "target_confirmation": target_confirmation,
                "ocr_items_count": len(before_items),
                "timing": finalize_performance_timing(),
                "error": admission_error,
            }
    before_messages = timed_call(
        "parse",
        "voice_transcribe_before",
        lambda: parse_messages_from_ocr(before_items, image_size, target=target, screenshot=before_screenshot),
    )
    visible_button_attempt: dict[str, Any] | None = None
    context_menu_attempt: dict[str, Any] | None = None
    voice_attempts: list[dict[str, Any]] = []

    def transcribed_messages_from_after(
        after_messages: list[dict[str, Any]],
        baseline_messages: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        new_items = sidecar_new_message_occurrences(after_messages, baseline_messages)
        transcribed_items = [
            message
            for message in new_items
            if not voice_duration_text_like(str(message.get("content_clean") or message.get("content") or ""))
        ]
        return new_items, transcribed_items

    def build_voice_transcribe_result(
        *,
        state: str,
        error_code: str | None,
        click_result: dict[str, Any],
        final_click_target: dict[str, Any] | None,
        planned_click_point: list[int] | None,
        click_jitter: dict[str, Any] | None,
        after_screenshot_path: str,
        after_messages: list[dict[str, Any]],
        final_screenshot: Image.Image | None,
        final_ocr_items: list[dict[str, Any]],
        after_ocr_items_count: int,
        new_messages: list[dict[str, Any]],
        transcribed_messages: list[dict[str, Any]],
        quality_flags: list[str],
        attempt_count: int,
    ) -> dict[str, Any]:
        timing_payload = finalize_performance_timing()
        final_target_confirmation: dict[str, Any] = {}
        if confirm_target and final_screenshot is not None:
            final_target_confirmation = validate_active_send_target(
                hwnd,
                confirm_target,
                exact=confirm_exact,
                artifact_dir=artifact_dir,
                screenshot=final_screenshot,
                ocr_items=final_ocr_items,
                screenshot_path=after_screenshot_path,
            )
        final_target_confirmed = bool(
            not confirm_target or c2_target_activation_confirmed(final_target_confirmation)
        )
        authoritative_messages = merge_structural_image_messages(
            final_screenshot,
            final_ocr_items,
            after_messages,
            target=target,
        )
        observations = build_message_observations_v3(authoritative_messages)
        observation_validation_errors = [
            {
                "observation_id": str(observation.get("observation_id") or ""),
                "row_kind": str(observation.get("row_kind") or ""),
                "error_codes": list(observation.get("contract_errors") or []),
            }
            for observation in observations
            if isinstance(observation, dict) and observation.get("contract_errors")
        ]
        visible_menu_texts = voice_transcribe_menu_texts_from_items(final_ocr_items)
        visible_panel_texts = chat_info_panel_texts_from_items(final_ocr_items)
        final_frame_reusable = bool(
            final_screenshot is not None
            and final_target_confirmed
            and not visible_menu_texts
            and not visible_panel_texts
            and not observation_validation_errors
        )
        payload = {
            "ok": bool(transcribed_messages) or (bool(click_result.get("ok")) if click_result else False),
            "observation_schema_version": C2_OBSERVATION_SCHEMA_VERSION,
            "online": True,
            "adapter": "win32_ocr",
            "state": state,
            "error_code": error_code,
            "window_probe": probe,
            "target": target,
            "before_screenshot_path": before_path,
            "after_screenshot_path": after_screenshot_path,
            "voice_context_anchor": context_anchor or {},
            "click_target": final_click_target or {},
            "visible_button_attempt": visible_button_attempt or {},
            "hover_attempt": {},
            "context_menu_attempt": context_menu_attempt or {},
            "click": click_result,
            "attempts": voice_attempts,
            "item_action_outcomes": [
                {
                    "physical_anchor_keys": list(
                        attempt.get("processed_anchor_keys")
                        or attempt.get("failed_anchor_keys")
                        or attempt.get("skipped_anchor_keys")
                        or []
                    ),
                    "action_phase": str(
                        attempt.get("action_phase") or "not_attempted"
                    ),
                    "business_state": (
                        "completed"
                        if attempt.get("effective_success") is True
                        else "failed"
                    ),
                    "business_result_confirmed": bool(
                        attempt.get("effective_success") is True
                    ),
                    "evidence": {
                        "attempt_index": attempt.get("attempt_index"),
                        "method": attempt.get("method"),
                        "result": attempt.get("result"),
                        "business_state": (
                            "completed"
                            if attempt.get("effective_success") is True
                            else "failed"
                        ),
                        "business_result_confirmed": bool(
                            attempt.get("effective_success") is True
                        ),
                    },
                }
                for attempt in voice_attempts
                if isinstance(attempt, dict)
            ],
            "action_phase": (
                "trigger_attempted"
                if any(
                    isinstance(attempt, dict)
                    and attempt.get("action_phase") == "trigger_attempted"
                    for attempt in voice_attempts
                )
                else "confirmed"
                if any(
                    isinstance(attempt, dict)
                    and attempt.get("action_phase") == "confirmed"
                    for attempt in voice_attempts
                )
                else "not_attempted"
            ),
            "planned_click_point": planned_click_point or [],
            "click_jitter": click_jitter or {},
            "wait_ms": wait_ms,
            "candidate_order": "bottom_to_top",
            "skipped_already_transcribed_count": len(skipped_transcribed_anchor_keys),
            "skipped_already_transcribed_anchor_keys": sorted(skipped_transcribed_anchor_keys),
            "skipped_wrong_context_menu_count": len(skipped_wrong_context_menu_anchor_keys),
            "skipped_wrong_context_menu_anchor_keys": sorted(skipped_wrong_context_menu_anchor_keys),
            "skipped_unknown_context_menu_count": len(skipped_unknown_context_menu_anchor_keys),
            "skipped_unknown_context_menu_anchor_keys": sorted(skipped_unknown_context_menu_anchor_keys),
            "failed_voice_anchor_count": len(failed_voice_anchor_keys),
            "failed_voice_anchor_keys": sorted(failed_voice_anchor_keys),
            "processed_voice_anchor_count": len(processed_voice_anchor_keys),
            "processed_voice_anchor_keys": sorted(processed_voice_anchor_keys),
            "before_messages": before_messages,
            "messages": authoritative_messages,
            "observations": observations,
            "send_context_guard": build_send_context_guard(observations),
            "observation_validation_errors": observation_validation_errors,
            "final_frame_reusable": final_frame_reusable,
            "final_frame_validation": {
                "target_confirmed": final_target_confirmed,
                "menu_closed": not bool(visible_menu_texts),
                "chat_info_panel_closed": not bool(visible_panel_texts),
                "observation_contract_valid": not bool(observation_validation_errors),
            },
            "new_messages": new_messages,
            "transcribed_messages": transcribed_messages,
            "attempt_count": attempt_count,
            "quality_flags": quality_flags,
            "ocr_items_count": after_ocr_items_count,
            "initial_target_confirmation": target_confirmation,
            "target_confirmation": final_target_confirmation,
            "timing": timing_payload,
        }
        if artifact_dir:
            try:
                review_path = Path(artifact_dir) / "voice_transcribe_review.json"
                payload["review_path"] = str(review_path)
                review_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
            except Exception as exc:
                payload["review_write_error"] = repr(exc)
        return payload

    wait_ms = bounded_int(
        os.getenv("WECHAT_WIN32_OCR_VOICE_TRANSCRIBE_WAIT_MS"),
        default=2600,
        minimum=500,
        maximum=15000,
    )

    def has_untranscribed_voice(
        screenshot_for_messages: Image.Image,
        ocr_items_for_messages: list[dict[str, Any]],
        messages: list[dict[str, Any]],
        image_size_for_messages: tuple[int, int],
    ) -> bool:
        return any(
            observation.get("voice_state") == "untranscribed"
            and isinstance(observation.get("action_target"), dict)
            for observation in build_unified_voice_observations_v3(
                screenshot_for_messages,
                ocr_items_for_messages,
                image_size_for_messages,
                parsed_messages=messages,
            )
        )

    def bind_transcribed_messages_to_anchor(
        messages: list[dict[str, Any]],
        anchor: dict[str, Any] | None,
        image_size_for_anchor: tuple[int, int],
        *,
        after_messages_for_layout: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        if not isinstance(anchor, dict) or not anchor:
            return messages
        anchor_keys = sorted(voice_context_anchor_exclusion_keys(anchor, image_size_for_anchor))
        anchor_item = anchor.get("item") if isinstance(anchor.get("item"), dict) else {}
        anchor_meta = {
            "source": anchor.get("source"),
            "anchor_key": str(anchor.get("anchor_key") or ""),
            "anchor_stable_key": str(anchor.get("anchor_stable_key") or ""),
            "anchor_structural_key": str(anchor.get("anchor_structural_key") or ""),
            "exclusion_keys": anchor_keys,
            "item": anchor_item,
            "click_bounds": anchor.get("click_bounds") if isinstance(anchor.get("click_bounds"), list) else [],
            "avatar_alignment": anchor.get("avatar_alignment") if isinstance(anchor.get("avatar_alignment"), dict) else {},
        }
        duration_text = str(anchor_item.get("voice_duration_text") or anchor_item.get("text") or "")
        anchor_role = voice_anchor_sender_role(anchor, image_size_for_anchor)
        bound: list[dict[str, Any]] = []
        for message in messages:
            if not isinstance(message, dict):
                continue
            if not message_is_plausible_voice_transcript_for_anchor(
                message,
                anchor,
                image_size_for_anchor,
                after_messages=after_messages_for_layout,
            ):
                continue
            item = {
                **message,
                "type": "voice",
                "voice_anchor": anchor_meta,
                "voice_anchor_key": anchor_meta["anchor_key"],
                "voice_anchor_stable_key": anchor_meta["anchor_stable_key"],
                "voice_anchor_structural_key": anchor_meta["anchor_structural_key"],
                "parent_voice_anchor_key": anchor_meta["anchor_stable_key"] or anchor_meta["anchor_key"],
                "observation_schema_version": 3,
                "row_kind": "voice_transcript",
                "voice_state": "transcribed",
            }
            if message_is_combined_voice_transcript_record(message):
                item["voice_anchor_binding_evidence"] = combined_voice_transcript_anchor_match_evidence(
                    message,
                    anchor,
                    image_size_for_anchor,
                    after_messages=after_messages_for_layout,
                )
            if anchor_role == "customer":
                item["sender"] = "customer"
                item["sender_role"] = "customer"
            elif anchor_role == "self":
                item["sender"] = "self"
                item["sender_role"] = "self"
            if anchor_role in {"self", "customer"}:
                envelope = dict(item.get("message_envelope") or {})
                if envelope:
                    envelope["sender"] = anchor_role
                    envelope["sender_role"] = anchor_role
                    item["message_envelope"] = envelope
            if duration_text and voice_duration_text_like(duration_text):
                item.setdefault("voice_duration_text", duration_text)
                seconds = voice_duration_seconds_from_text(duration_text)
                if seconds is not None:
                    item.setdefault("voice_duration", seconds)
            bound.append(item)
        combined = [item for item in bound if message_is_combined_voice_transcript_record(item)]
        return combined or bound

    initial_voice_observation = find_unified_untranscribed_voice_observation(
        before_screenshot,
        before_items,
        image_size,
        parsed_messages=before_messages,
    )
    context_anchor = initial_voice_observation.get("action_target") if isinstance(initial_voice_observation, dict) else None
    click_target = initial_voice_observation.get("visible_button_target") if isinstance(initial_voice_observation, dict) else None
    if not context_anchor:
        timing_payload = finalize_performance_timing()
        return {
            "ok": False,
            "online": True,
            "adapter": "win32_ocr",
            "state": "voice_transcribe_no_visible_voice",
            "error_code": "VOICE_TRANSCRIBE_NO_VISIBLE_VOICE",
            "window_probe": probe,
            "target": target,
            "screenshot_path": before_path,
            "before_screenshot_path": before_path,
            "ocr_items_count": len(before_items),
            "messages": before_messages,
            "voice_context_anchor": {},
            "timing": timing_payload,
            "error": "No visible WeChat voice-to-text affordance or voice bubble context-menu anchor was found.",
        }

    initial_pending_voice_count = sum(
        1
        for message in before_messages
        if isinstance(message, dict) and message_is_untranscribed_voice_record(message)
    )
    default_max_attempts = max(8, min(16, initial_pending_voice_count * 3 + 2))
    max_attempts = bounded_int(
        os.getenv("WECHAT_WIN32_OCR_VOICE_TRANSCRIBE_MAX_ATTEMPTS"),
        default=default_max_attempts,
        minimum=1,
        maximum=16,
    )
    current_screenshot = before_screenshot
    current_items = before_items
    current_size = image_size
    current_messages = before_messages
    all_new_messages: list[dict[str, Any]] = []
    all_transcribed_messages: list[dict[str, Any]] = []
    seen_new_keys: set[str] = set()
    seen_transcribed_keys: set[str] = set()
    last_click_result: dict[str, Any] = {}
    last_click_target: dict[str, Any] | None = None
    last_click_point: list[int] | None = None
    last_click_jitter: dict[str, Any] | None = None
    last_after_path = before_path
    last_ocr_count = len(before_items)
    skipped_transcribed_anchor_keys: set[str] = set()
    processed_voice_anchor_keys: set[str] = set(excluded_voice_anchor_keys or set())
    skipped_wrong_context_menu_anchor_keys: set[str] = set()
    skipped_unknown_context_menu_anchor_keys: set[str] = set()
    failed_voice_anchor_keys: set[str] = set()

    for attempt_index in range(1, max_attempts + 1):
        now_monotonic = time.monotonic()
        if now_monotonic - flow_started_at >= hard_safety_timeout_seconds:
            watchdog_stopped_reason = "hard_safety_timeout_reached"
            break
        if now_monotonic - last_progress_at >= no_progress_timeout_seconds:
            watchdog_stopped_reason = "no_progress_timeout_reached"
            break
        attempt_baseline_messages = current_messages
        excluded_anchor_keys = (
            skipped_transcribed_anchor_keys
            | processed_voice_anchor_keys
            | skipped_wrong_context_menu_anchor_keys
            | skipped_unknown_context_menu_anchor_keys
            | failed_voice_anchor_keys
        )
        voice_observation = find_unified_untranscribed_voice_observation(
            current_screenshot,
            current_items,
            current_size,
            excluded_anchor_keys=excluded_anchor_keys,
            parsed_messages=current_messages,
        )
        context_anchor = voice_observation.get("action_target") if isinstance(voice_observation, dict) else None
        click_target = voice_observation.get("visible_button_target") if isinstance(voice_observation, dict) else None
        if not context_anchor:
            break
        use_visible_button = bool(click_target)
        attempt_record: dict[str, Any] = {
            "attempt_index": attempt_index,
            "candidate_order": "bottom_to_top",
            "action_phase": "not_attempted",
            "context_anchor": context_anchor or {},
            "visible_button_target": click_target or {},
            "unified_voice_observation": {
                key: value
                for key, value in (voice_observation or {}).items()
                if key not in {"action_target", "visible_button_target", "source_message"}
            },
            "selected_method": "visible_button" if use_visible_button else "context_menu",
        }
        click_result: dict[str, Any] = {}
        final_target: dict[str, Any] | None = None
        planned_point: list[int] | None = None
        click_jitter: dict[str, Any] | None = None
        clicked_context_anchor: dict[str, Any] | None = None
        explicitly_completed_without_text = False
        if use_visible_button and click_target:
            visible_item = click_target.get("item") if isinstance(click_target.get("item"), dict) else {}
            click_bounds = [
                int(float(visible_item.get("left") or 0)),
                int(float(visible_item.get("top") or 0)),
                int(float(visible_item.get("right") or 0)),
                int(float(visible_item.get("bottom") or 0)),
            ]
            if len(click_bounds) < 4 or click_bounds[2] <= click_bounds[0] or click_bounds[3] <= click_bounds[1]:
                click_bounds = [int(value) for value in click_target.get("click_bounds") or []]
            click_x = int((click_bounds[0] + click_bounds[2]) / 2)
            click_y = int((click_bounds[1] + click_bounds[3]) / 2)
            jitter_meta = {
                "enabled": False,
                "source": "ocr_visible_button_center",
                "bounds": click_bounds,
                "reason": "click_exact_ocr_button_center",
            }
            journal_anchor_keys = sorted(
                voice_context_anchor_exclusion_keys(
                    context_anchor,
                    current_size,
                )
            )
            if action_journal_path:
                write_action_phase_journal(
                    action_journal_path,
                    "trigger_attempted",
                    physical_anchor_keys=journal_anchor_keys,
                )
            attempt_record["action_phase"] = "trigger_attempted"
            click_result = timed_call(
                "click",
                f"click_visible_transcribe_{attempt_index}",
                lambda: human_window_image_click_in_bounds(
                    hwnd,
                    click_x,
                    click_y,
                    bounds=click_bounds,
                    action_name="voice_transcribe_visible_button_click",
                ),
            )
            final_target = click_target
            planned_point = [click_x, click_y]
            click_jitter = jitter_meta
            if context_anchor and abs(voice_target_center_y(click_target) - voice_target_center_y(context_anchor)) <= 96:
                clicked_context_anchor = context_anchor
            visible_button_attempt = {
                "click_target": click_target,
                "click": click_result,
                "planned_click_point": planned_point,
                "click_jitter": click_jitter,
            }
            attempt_record.update({"method": "visible_button", "visible_button_attempt": visible_button_attempt})
        if (not use_visible_button or not click_result.get("ok")) and context_anchor:
            context_menu_attempt = timed_call(
                "menu",
                f"open_context_menu_{attempt_index}",
                lambda: open_voice_transcribe_context_menu(
                    hwnd,
                    context_anchor,
                    image_size=current_size,
                    artifact_dir=artifact_dir,
                ),
            )
            menu_target = context_menu_attempt.get("click_target") if isinstance(context_menu_attempt, dict) else None
            collapse_target = context_menu_attempt.get("already_transcribed_target") if isinstance(context_menu_attempt, dict) else None
            menu_state = str((context_menu_attempt or {}).get("menu_state") or "")
            if menu_state == "already_transcribed":
                skipped_keys = sorted(voice_context_anchor_exclusion_keys(context_anchor, current_size))
                skipped_key = skipped_keys[0] if skipped_keys else ""
                skipped_transcribed_anchor_keys.update(skipped_keys)
                dismissal = timed_call(
                    "menu",
                    f"dismiss_already_transcribed_menu_{attempt_index}",
                    lambda: dismiss_voice_transcribe_context_menu(
                        hwnd,
                        artifact_dir=artifact_dir,
                        label=f"voice_transcribe_context_menu_dismissed_{attempt_index}",
                        menu_bounds=(collapse_target or {}).get("click_bounds"),
                    ),
                )
                click_result = {"ok": bool(dismissal.get("ok")), "reason": "already_transcribed_voice_skipped", "dismissal": dismissal}
                last_click_result = click_result
                last_click_target = context_anchor
                last_click_point = None
                last_click_jitter = None
                attempt_record.update({
                    "method": "context_menu",
                    "action_phase": "confirmed",
                    "context_menu_attempt": context_menu_attempt or {},
                    "menu_dismissal": dismissal,
                    "result": "already_transcribed_skipped",
                    "skipped_anchor_key": skipped_key,
                    "skipped_anchor_keys": skipped_keys,
                })
                voice_attempts.append(attempt_record)
                if not dismissal.get("ok"):
                    break
                continue
            if menu_state in {"text_message_context_menu", "avatar_context_menu"}:
                skipped_keys = sorted(voice_context_anchor_exclusion_keys(context_anchor, current_size))
                skipped_key = skipped_keys[0] if skipped_keys else ""
                skipped_wrong_context_menu_anchor_keys.update(skipped_keys)
                dismissal = timed_call(
                    "menu",
                    f"dismiss_wrong_context_menu_{attempt_index}",
                    lambda: dismiss_voice_transcribe_context_menu(
                        hwnd,
                        artifact_dir=artifact_dir,
                        label=f"voice_transcribe_context_menu_dismissed_{attempt_index}",
                    ),
                )
                click_result = {"ok": bool(dismissal.get("ok")), "reason": f"{menu_state}_skipped", "dismissal": dismissal}
                last_click_result = click_result
                last_click_target = context_anchor
                last_click_point = None
                last_click_jitter = None
                attempt_record.update({
                    "method": "context_menu",
                    "context_menu_attempt": context_menu_attempt or {},
                    "menu_dismissal": dismissal,
                    "result": f"{menu_state}_skipped",
                    "skipped_anchor_key": skipped_key,
                    "skipped_anchor_keys": skipped_keys,
                })
                voice_attempts.append(attempt_record)
                if not dismissal.get("ok"):
                    break
                continue
            if not isinstance(menu_target, dict) or not menu_target:
                skipped_keys = sorted(voice_context_anchor_exclusion_keys(context_anchor, current_size))
                skipped_key = skipped_keys[0] if skipped_keys else ""
                skipped_unknown_context_menu_anchor_keys.update(skipped_keys)
                dismissal = timed_call(
                    "menu",
                    f"dismiss_unknown_context_menu_{attempt_index}",
                    lambda: dismiss_voice_transcribe_context_menu(
                        hwnd,
                        artifact_dir=artifact_dir,
                        label=f"voice_transcribe_context_menu_dismissed_{attempt_index}",
                    ),
                )
                attempt_record.update({
                    "method": "context_menu",
                    "context_menu_attempt": context_menu_attempt or {},
                    "menu_dismissal": dismissal,
                    "result": "context_menu_target_not_found",
                    "skipped_anchor_key": skipped_key,
                    "skipped_anchor_keys": skipped_keys,
                })
                voice_attempts.append(attempt_record)
                if not dismissal.get("ok"):
                    break
                continue
            journal_anchor_keys = sorted(
                voice_context_anchor_exclusion_keys(
                    context_anchor,
                    current_size,
                )
            )
            if action_journal_path:
                write_action_phase_journal(
                    action_journal_path,
                    "trigger_attempted",
                    physical_anchor_keys=journal_anchor_keys,
                )
            attempt_record["action_phase"] = "trigger_attempted"
            click_result = timed_call(
                "click",
                f"click_context_menu_transcribe_{attempt_index}",
                lambda: click_voice_transcribe_context_menu_target(
                    hwnd,
                    menu_target,
                    geometry=geometry,
                    artifact_dir=artifact_dir,
                    attempt_index=attempt_index,
                ),
            )
            final_target = menu_target
            clicked_context_anchor = context_anchor
            planned_point = list(click_result.get("planned_click_point") or [])
            click_jitter = click_result.get("click_jitter") if isinstance(click_result.get("click_jitter"), dict) else {}
            attempt_record.update({
                "method": "context_menu",
                "context_menu_attempt": context_menu_attempt or {},
            })
        timed_call(
            "wait",
            f"wait_after_action_{attempt_index}",
            lambda: humanized_action_sleep(max(200, wait_ms - 500), wait_ms + 900),
        )
        after_screenshot, after_path = timed_call(
            "capture",
            f"voice_transcribe_after_{attempt_index}",
            lambda: capture_wechat(hwnd, artifact_dir=artifact_dir, label=f"voice_transcribe_after_{attempt_index}"),
        )
        after_items = timed_call("ocr", f"voice_transcribe_after_{attempt_index}", lambda: run_ocr(after_screenshot))
        after_size = getattr(after_screenshot, "size", current_size)
        after_messages = timed_call(
            "parse",
            f"voice_transcribe_after_{attempt_index}",
            lambda: parse_messages_from_ocr(after_items, after_size, target=target, screenshot=after_screenshot),
        )
        new_messages, transcribed_messages = transcribed_messages_from_after(after_messages, attempt_baseline_messages)
        if click_result.get("ok") and clicked_context_anchor:
            transcribed_messages = bind_transcribed_messages_to_anchor(
                transcribed_messages,
                clicked_context_anchor,
                current_size,
                after_messages_for_layout=after_messages,
            )
        reanchor_attempts: list[dict[str, Any]] = []
        if click_result.get("ok") and clicked_context_anchor and not transcribed_messages:
            passive_confirm_attempts = bounded_int(
                os.getenv("WECHAT_WIN32_OCR_VOICE_PASSIVE_CONFIRM_ATTEMPTS"),
                default=3,
                minimum=2,
                maximum=6,
            )
            for reanchor_index in range(1, passive_confirm_attempts + 1):
                scrolled_up = False
                timed_call(
                    "wait",
                    f"passive_confirm_wait_{attempt_index}_{reanchor_index}",
                    lambda: humanized_action_sleep(650, 1200),
                )
                recovered_screenshot, recovered_path = timed_call(
                    "capture",
                    f"voice_transcribe_reanchor_{attempt_index}_{reanchor_index}",
                    lambda: capture_wechat(
                        hwnd,
                        artifact_dir=artifact_dir,
                        label=f"voice_transcribe_reanchor_{attempt_index}_{reanchor_index}",
                    ),
                )
                recovered_items = timed_call(
                    "ocr",
                    f"voice_transcribe_reanchor_{attempt_index}_{reanchor_index}",
                    lambda: run_ocr(recovered_screenshot),
                )
                recovered_size = getattr(recovered_screenshot, "size", after_size)
                recovered_messages = timed_call(
                    "parse",
                    f"voice_transcribe_reanchor_{attempt_index}_{reanchor_index}",
                    lambda: parse_messages_from_ocr(
                        recovered_items,
                        recovered_size,
                        target=target,
                        screenshot=recovered_screenshot,
                    ),
                )
                recovered_new, recovered_candidates = transcribed_messages_from_after(
                    recovered_messages,
                    attempt_baseline_messages,
                )
                recovered_transcribed = bind_transcribed_messages_to_anchor(
                    recovered_candidates,
                    clicked_context_anchor,
                    current_size,
                    after_messages_for_layout=recovered_messages,
                )
                reanchor_attempts.append(
                    {
                        "attempt_index": reanchor_index,
                        "scrolled_up": scrolled_up,
                        "screenshot_path": recovered_path,
                        "message_count": len(recovered_messages),
                        "candidate_count": len(recovered_candidates),
                        "transcribed_count": len(recovered_transcribed),
                    }
                )
                after_screenshot = recovered_screenshot
                after_path = recovered_path
                after_items = recovered_items
                after_size = recovered_size
                after_messages = recovered_messages
                new_messages = recovered_new
                transcribed_messages = recovered_transcribed
                if recovered_transcribed:
                    break
        # A successful physical action is never followed by a second action on
        # the same bubble. Slow transcription is handled by passive captures
        # above; another context-menu click would risk duplicate interaction.
        if use_visible_button and clicked_context_anchor and not transcribed_messages and not click_result.get("ok"):
            fallback_menu = open_voice_transcribe_context_menu(
                hwnd,
                clicked_context_anchor,
                image_size=after_size,
                artifact_dir=artifact_dir,
            )
            attempt_record["visible_button_fallback_context_menu"] = fallback_menu or {}
            fallback_state = str((fallback_menu or {}).get("menu_state") or "")
            fallback_target = fallback_menu.get("click_target") if isinstance(fallback_menu, dict) else None
            if fallback_state == "already_transcribed":
                skipped_keys = sorted(voice_context_anchor_exclusion_keys(clicked_context_anchor, after_size))
                skipped_transcribed_anchor_keys.update(skipped_keys)
                dismissal = dismiss_voice_transcribe_context_menu(
                    hwnd,
                    artifact_dir=artifact_dir,
                    label=f"voice_transcribe_visible_fallback_dismissed_{attempt_index}",
                    menu_bounds=((fallback_menu or {}).get("already_transcribed_target") or {}).get("click_bounds"),
                )
                click_result = {
                    "ok": bool(dismissal.get("ok")),
                    "reason": "visible_button_fallback_already_transcribed",
                    "dismissal": dismissal,
                }
                attempt_record["visible_button_fallback_dismissal"] = dismissal
                if dismissal.get("ok"):
                    completed_screenshot, completed_path = capture_wechat(
                        hwnd,
                        artifact_dir=artifact_dir,
                        label=f"voice_transcribe_visible_fallback_completed_{attempt_index}",
                    )
                    completed_items = run_ocr(completed_screenshot)
                    completed_size = getattr(completed_screenshot, "size", after_size)
                    completed_messages = parse_messages_from_ocr(
                        completed_items,
                        completed_size,
                        target=target,
                        screenshot=completed_screenshot,
                    )
                    completed_new, completed_candidates = transcribed_messages_from_after(
                        completed_messages,
                        attempt_baseline_messages,
                    )
                    completed_transcribed = bind_transcribed_messages_to_anchor(
                        completed_candidates,
                        clicked_context_anchor,
                        current_size,
                        after_messages_for_layout=completed_messages,
                    )
                    after_screenshot = completed_screenshot
                    after_path = completed_path
                    after_items = completed_items
                    after_size = completed_size
                    after_messages = completed_messages
                    new_messages = completed_new
                    transcribed_messages = completed_transcribed
                explicitly_completed_without_text = not bool(transcribed_messages)
            elif isinstance(fallback_target, dict) and fallback_target:
                fallback_click = click_voice_transcribe_context_menu_target(
                    hwnd,
                    fallback_target,
                    geometry=geometry,
                    artifact_dir=artifact_dir,
                    attempt_index=attempt_index,
                )
                click_result = fallback_click
                final_target = fallback_target
                planned_point = list(fallback_click.get("planned_click_point") or [])
                click_jitter = fallback_click.get("click_jitter") if isinstance(fallback_click.get("click_jitter"), dict) else {}
                attempt_record["visible_button_fallback_click"] = fallback_click
                if fallback_click.get("ok"):
                    humanized_action_sleep(max(200, wait_ms - 500), wait_ms + 900)
                    fallback_screenshot, fallback_path = capture_wechat(
                        hwnd,
                        artifact_dir=artifact_dir,
                        label=f"voice_transcribe_after_fallback_{attempt_index}",
                    )
                    fallback_items = run_ocr(fallback_screenshot)
                    fallback_size = getattr(fallback_screenshot, "size", after_size)
                    fallback_messages = parse_messages_from_ocr(
                        fallback_items,
                        fallback_size,
                        target=target,
                        screenshot=fallback_screenshot,
                    )
                    fallback_new, fallback_candidates = transcribed_messages_from_after(
                        fallback_messages,
                        attempt_baseline_messages,
                    )
                    fallback_transcribed = bind_transcribed_messages_to_anchor(
                        fallback_candidates,
                        clicked_context_anchor,
                        current_size,
                        after_messages_for_layout=fallback_messages,
                    )
                    if not fallback_transcribed:
                        for fallback_retry_index in range(1, 2):
                            humanized_action_sleep(650, 1200)
                            retry_screenshot, retry_path = capture_wechat(
                                hwnd,
                                artifact_dir=artifact_dir,
                                label=f"voice_transcribe_fallback_reanchor_{attempt_index}_{fallback_retry_index}",
                            )
                            retry_items = run_ocr(retry_screenshot)
                            retry_size = getattr(retry_screenshot, "size", fallback_size)
                            retry_messages = parse_messages_from_ocr(
                                retry_items,
                                retry_size,
                                target=target,
                                screenshot=retry_screenshot,
                            )
                            retry_new, retry_candidates = transcribed_messages_from_after(
                                retry_messages,
                                attempt_baseline_messages,
                            )
                            retry_transcribed = bind_transcribed_messages_to_anchor(
                                retry_candidates,
                                clicked_context_anchor,
                                current_size,
                                after_messages_for_layout=retry_messages,
                            )
                            reanchor_attempts.append(
                                {
                                    "attempt_index": fallback_retry_index,
                                    "phase": "visible_button_fallback",
                                    "scrolled_up": False,
                                    "screenshot_path": retry_path,
                                    "message_count": len(retry_messages),
                                    "candidate_count": len(retry_candidates),
                                    "transcribed_count": len(retry_transcribed),
                                }
                            )
                            fallback_screenshot = retry_screenshot
                            fallback_path = retry_path
                            fallback_items = retry_items
                            fallback_size = retry_size
                            fallback_messages = retry_messages
                            fallback_new = retry_new
                            fallback_transcribed = retry_transcribed
                            if fallback_transcribed:
                                break
                    after_screenshot = fallback_screenshot
                    after_path = fallback_path
                    after_items = fallback_items
                    after_size = fallback_size
                    after_messages = fallback_messages
                    new_messages = fallback_new
                    transcribed_messages = fallback_transcribed
            else:
                dismissal = dismiss_voice_transcribe_context_menu(
                    hwnd,
                    artifact_dir=artifact_dir,
                    label=f"voice_transcribe_visible_fallback_unknown_{attempt_index}",
                )
                click_result = {
                    "ok": False,
                    "reason": f"visible_button_fallback_{fallback_state or 'unknown'}",
                    "dismissal": dismissal,
                }
                attempt_record["visible_button_fallback_dismissal"] = dismissal
        unique_new: list[dict[str, Any]] = []
        for message in new_messages:
            key = sidecar_message_content_key(message)
            if key in seen_new_keys:
                continue
            seen_new_keys.add(key)
            unique_new.append(message)
            all_new_messages.append(message)
        unique_transcribed: list[dict[str, Any]] = []
        for message in transcribed_messages:
            key = sidecar_message_content_key(message)
            if key in seen_transcribed_keys:
                continue
            seen_transcribed_keys.add(key)
            unique_transcribed.append(message)
            all_transcribed_messages.append(message)
        processed_anchor_key = ""
        if unique_transcribed and clicked_context_anchor:
            processed_keys = sorted(voice_context_anchor_exclusion_keys(clicked_context_anchor, current_size))
            processed_anchor_key = processed_keys[0] if processed_keys else ""
            processed_voice_anchor_keys.update(processed_keys)
            attempt_record["action_phase"] = "confirmed"
            if action_journal_path:
                write_action_phase_journal(
                    action_journal_path,
                    "confirmed",
                    physical_anchor_keys=processed_keys,
                    business_state="completed",
                    business_result_confirmed=True,
                    terminal_payload={
                        "state": "completed",
                        "transcribed_messages": unique_transcribed,
                    },
                )
        elif explicitly_completed_without_text and clicked_context_anchor:
            processed_keys = sorted(
                voice_context_anchor_exclusion_keys(
                    clicked_context_anchor,
                    current_size,
                )
            )
            processed_voice_anchor_keys.update(processed_keys)
            attempt_record["action_phase"] = "confirmed"
            if action_journal_path:
                write_action_phase_journal(
                    action_journal_path,
                    "confirmed",
                    physical_anchor_keys=processed_keys,
                    business_state="completed",
                    business_result_confirmed=True,
                    terminal_payload={"state": "completed"},
                )
        else:
            processed_keys = []
        failed_anchor_keys: list[str] = []
        if clicked_context_anchor and not unique_transcribed and not explicitly_completed_without_text:
            failed_anchor_keys = sorted(voice_context_anchor_exclusion_keys(clicked_context_anchor, current_size))
            failed_voice_anchor_keys.update(failed_anchor_keys)
            if action_journal_path:
                write_action_phase_journal(
                    action_journal_path,
                    "trigger_attempted",
                    physical_anchor_keys=failed_anchor_keys,
                    business_state="failed",
                    business_result_confirmed=False,
                    error_code="VOICE_TRANSCRIBE_RESULT_UNCONFIRMED",
                    terminal_payload={
                        "state": "failed",
                        "reason": "voice_transcribe_result_unconfirmed",
                    },
                )
        if unique_transcribed or explicitly_completed_without_text or failed_anchor_keys:
            last_progress_at = time.monotonic()
        attempt_record.update({
            "click_target": final_target or {},
            "click": click_result,
            "planned_click_point": planned_point or [],
            "click_jitter": click_jitter or {},
            "processed_anchor_key": processed_anchor_key,
            "processed_anchor_keys": processed_keys,
            "failed_anchor_keys": failed_anchor_keys,
            "after_screenshot_path": after_path,
            "reanchor_attempts": reanchor_attempts,
            "new_messages_count": len(unique_new),
            "transcribed_messages_count": len(unique_transcribed),
            "effective_success": bool(unique_transcribed or explicitly_completed_without_text),
            "explicitly_completed_without_text": explicitly_completed_without_text,
            "remaining_untranscribed_voice": has_untranscribed_voice(
                after_screenshot,
                after_items,
                after_messages,
                after_size,
            ),
        })
        voice_attempts.append(attempt_record)
        last_click_result = click_result
        last_click_target = final_target
        last_click_point = planned_point
        last_click_jitter = click_jitter
        last_after_path = after_path
        last_ocr_count = len(after_items)
        current_screenshot = after_screenshot
        current_items = after_items
        current_size = after_size
        current_messages = after_messages
        if not click_result.get("ok") and not unique_transcribed and not clicked_context_anchor:
            break
        if not has_untranscribed_voice(after_screenshot, after_items, after_messages, after_size) and not has_remaining_voice_transcribe_candidate(
            after_screenshot,
            after_items,
            after_size,
            excluded_anchor_keys=(
                skipped_transcribed_anchor_keys
                | processed_voice_anchor_keys
                | skipped_wrong_context_menu_anchor_keys
                | skipped_unknown_context_menu_anchor_keys
                | failed_voice_anchor_keys
            ),
            parsed_messages=after_messages,
        ):
            break

    state = "voice_transcribe_click_failed"
    error_code = "VOICE_TRANSCRIBE_CLICK_FAILED"
    quality_flags: list[str] = []
    remaining_untranscribed_voice = has_untranscribed_voice(
        current_screenshot,
        current_items,
        current_messages,
        current_size,
    )
    if all_transcribed_messages:
        state = (
            "voice_transcribe_partial"
            if remaining_untranscribed_voice or failed_voice_anchor_keys
            else "voice_transcribe_completed"
        )
        error_code = ""
    elif last_click_result.get("ok"):
        state = "voice_transcribe_no_new_text"
        error_code = ""
        quality_flags.append("no_new_transcribed_text")
    else:
        quality_flags.append("voice_transcribe_click_failed")
    if remaining_untranscribed_voice:
        quality_flags.append("untranscribed_voice_remaining")
    if skipped_transcribed_anchor_keys:
        quality_flags.append("already_transcribed_voice_skipped")
    if processed_voice_anchor_keys:
        quality_flags.append("processed_voice_anchor_skipped")
    if skipped_wrong_context_menu_anchor_keys:
        quality_flags.append("wrong_context_menu_anchor_skipped")
    if skipped_unknown_context_menu_anchor_keys:
        quality_flags.append("unknown_context_menu_anchor_skipped")
    if failed_voice_anchor_keys:
        quality_flags.append("voice_transcribe_anchor_failed")
    if watchdog_stopped_reason:
        quality_flags.append(f"voice_transcribe_{watchdog_stopped_reason}")
    return build_voice_transcribe_result(
        state=state,
        error_code=error_code or None,
        click_result=last_click_result,
        final_click_target=last_click_target,
        planned_click_point=last_click_point,
        click_jitter=last_click_jitter,
        after_screenshot_path=last_after_path,
        after_messages=current_messages,
        final_screenshot=current_screenshot,
        final_ocr_items=current_items,
        after_ocr_items_count=last_ocr_count,
        new_messages=all_new_messages,
        transcribed_messages=all_transcribed_messages,
        quality_flags=quality_flags,
        attempt_count=len(voice_attempts),
    )


def voice_transcribe_compact_text(text: str) -> str:
    return re.sub(r"\s+", "", normalize_ocr_text(text))


def voice_transcribe_button_text_like(text: str) -> bool:
    compact = voice_transcribe_compact_text(text)
    return bool(compact) and any(voice_transcribe_compact_text(token) in compact for token in VOICE_TRANSCRIBE_TEXT_TOKENS)


def voice_transcribe_collapse_text_like(text: str) -> bool:
    compact = voice_transcribe_compact_text(text)
    return bool(compact) and any(voice_transcribe_compact_text(token) in compact for token in VOICE_TRANSCRIBE_COLLAPSE_TEXT_TOKENS)


def text_message_context_menu_text_like(text: str) -> bool:
    compact = voice_transcribe_compact_text(text)
    return bool(compact) and any(voice_transcribe_compact_text(token) in compact for token in TEXT_MESSAGE_CONTEXT_MENU_TOKENS)


def avatar_context_menu_text_like(text: str) -> bool:
    compact = voice_transcribe_compact_text(text)
    return bool(compact) and any(voice_transcribe_compact_text(token) in compact for token in AVATAR_CONTEXT_MENU_TOKENS)


def voice_duration_text_like(text: str) -> bool:
    compact = voice_transcribe_compact_text(text).replace("“", '"').replace("”", '"').replace("″", '"')
    if not compact:
        return False
    if re.fullmatch(r"\d{1,3}\"", compact):
        return True
    if re.fullmatch(r"\d{1,3}[\"']?[\(\[（]?[A-Za-z]{1,2}", compact):
        return True
    if re.fullmatch(r"[^0-9A-Za-z\u4e00-\u9fff]{1,3}\d{1,3}[\"']?", compact):
        return True
    if re.fullmatch(r"\d{1,3}[\"']?[\(\[（]{1,2}", compact):
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
    width, _height = image_size
    is_self_side_voice = float(duration_item.get("center_x") or 0) > width * 0.62
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
        if is_self_side_voice:
            # Expanded self transcripts are right-aligned with the green voice
            # bubble and may extend hundreds of pixels to the left.  Their left
            # edge is therefore not a stable ownership signal.
            starts_near_voice = abs(right - duration_right) <= 96 or (
                right >= duration_left - 80 and right <= duration_right + 96
            )
            extends_like_transcript = left <= duration_left - 40 or len(voice_transcribe_compact_text(text)) >= 4
        else:
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


def voice_context_anchor_key(target: dict[str, Any] | None) -> str:
    if not isinstance(target, dict):
        return ""
    item = target.get("item") if isinstance(target.get("item"), dict) else {}
    seed = {
        "source": str(target.get("source") or ""),
        "left": round(float(item.get("left") or 0), 1),
        "top": round(float(item.get("top") or 0), 1),
        "right": round(float(item.get("right") or 0), 1),
        "bottom": round(float(item.get("bottom") or 0), 1),
        "center_y": round(float(item.get("center_y") or 0), 1),
        "text": str(item.get("text") or ""),
    }
    return hashlib.sha1(json.dumps(seed, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:20]


def voice_context_anchor_stable_parts(target: dict[str, Any] | None, image_size: tuple[int, int] | None = None) -> dict[str, Any] | None:
    if not isinstance(target, dict):
        return None
    item = target.get("item") if isinstance(target.get("item"), dict) else {}
    rect = voice_context_anchor_rect_bounds(target)
    if not rect:
        return None
    left, top, right, bottom = rect
    center_x = float(item.get("center_x") or 0) or (left + right) / 2.0
    center_y = float(item.get("center_y") or 0) or (top + bottom) / 2.0
    width = float((image_size or (0, 0))[0] or 0)
    side = voice_anchor_sender_role(target, image_size)
    if not side:
        side = "self" if width > 0 and center_x > width * 0.62 else "unknown"
    duration_text = str(item.get("voice_duration_text") or item.get("text") or "")
    duration_match = re.search(r"\d{1,3}", voice_transcribe_compact_text(duration_text))
    return {
        "side": side,
        "y_bucket": round(center_y / 18.0),
        "x_bucket": round(center_x / 72.0),
        "duration": duration_match.group(0) if duration_match else "",
    }


def voice_context_anchor_stable_key_from_parts(parts: dict[str, Any], *, y_bucket: int | None = None) -> str:
    seed = {
        "side": str(parts.get("side") or ""),
        "y_bucket": int(parts.get("y_bucket") if y_bucket is None else y_bucket),
        "x_bucket": int(parts.get("x_bucket") or 0),
        "duration": str(parts.get("duration") or ""),
    }
    return "voice-stable:" + hashlib.sha1(json.dumps(seed, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:20]


def voice_context_anchor_stable_key(target: dict[str, Any] | None, image_size: tuple[int, int] | None = None) -> str:
    parts = voice_context_anchor_stable_parts(target, image_size)
    if not parts:
        return ""
    return voice_context_anchor_stable_key_from_parts(parts)


def voice_context_anchor_stable_keys(
    target: dict[str, Any] | None,
    image_size: tuple[int, int] | None = None,
    *,
    y_bucket_radius: int = 1,
) -> set[str]:
    parts = voice_context_anchor_stable_parts(target, image_size)
    if not parts:
        return set()
    center_bucket = int(parts.get("y_bucket") or 0)
    return {
        voice_context_anchor_stable_key_from_parts(parts, y_bucket=bucket)
        for bucket in range(center_bucket - y_bucket_radius, center_bucket + y_bucket_radius + 1)
    }


def voice_context_anchor_exclusion_keys(target: dict[str, Any] | None, image_size: tuple[int, int] | None = None) -> set[str]:
    keys = {voice_context_anchor_key(target), voice_context_anchor_stable_key(target, image_size)}
    keys.update(voice_context_anchor_stable_keys(target, image_size))
    if isinstance(target, dict):
        for key_name in ("anchor_key", "anchor_stable_key", "anchor_structural_key"):
            value = str(target.get(key_name) or "").strip()
            if value:
                keys.add(value)
    return {key for key in keys if key}


def mark_voice_context_anchor_keys(target: dict[str, Any], image_size: tuple[int, int]) -> dict[str, Any]:
    target["anchor_key"] = voice_context_anchor_key(target)
    target["anchor_stable_key"] = voice_context_anchor_stable_key(target, image_size)
    return target


def voice_context_anchor_is_excluded(
    target: dict[str, Any],
    image_size: tuple[int, int],
    excluded_anchor_keys: set[str] | None,
) -> bool:
    excluded = excluded_anchor_keys or set()
    return bool(excluded and voice_context_anchor_exclusion_keys(target, image_size) & excluded)


def voice_target_center_y(target: dict[str, Any] | None) -> float:
    if not isinstance(target, dict):
        return 0.0
    item = target.get("item") if isinstance(target.get("item"), dict) else {}
    if item.get("center_y") is not None:
        return float(item.get("center_y") or 0)
    bounds = target.get("click_bounds")
    if isinstance(bounds, list) and len(bounds) >= 4:
        return (float(bounds[1]) + float(bounds[3])) / 2.0
    return 0.0


def voice_duration_context_click_bounds(item: dict[str, Any], image_size: tuple[int, int]) -> list[int]:
    width, height = image_size
    split_x = session_split_x(width)
    item_left = int(float(item.get("left") or 0))
    item_right = int(float(item.get("right") or 0))
    item_center_x = float(item.get("center_x") or 0)
    is_self_side_voice = item_center_x > width * 0.62
    left = max(split_x + 16, item_left - (42 if is_self_side_voice else 18))
    top = max(chat_header_cutoff_y(height), int(float(item.get("top") or 0)) - 16)
    # Right-side/self voice bubbles sit immediately beside the avatar. Keep the
    # context-menu click inside the green bubble so jitter cannot land on avatar.
    right_padding = 18 if is_self_side_voice else 78
    right_limit = width - 104 if is_self_side_voice else width - 18
    right = min(right_limit, item_right + right_padding)
    bottom = min(height - DEFAULT_MESSAGE_BOTTOM_EXCLUDE_PX, int(float(item.get("bottom") or 0)) + 16)
    if right <= left:
        right = min(right_limit, left + 64)
    if bottom <= top:
        bottom = min(height - DEFAULT_MESSAGE_BOTTOM_EXCLUDE_PX, top + 28)
    return [left, top, right, bottom]


def voice_duration_context_click_target(duration_target: dict[str, Any], image_size: tuple[int, int]) -> dict[str, Any] | None:
    source = str(duration_target.get("source") or "")
    if source in {
        "visual_self_voice_bubble_context_menu_anchor",
        "visual_customer_voice_bubble_context_menu_anchor",
        "parser_voice_message_context_menu_anchor",
    }:
        bounds = [int(value) for value in duration_target.get("click_bounds") or []]
        if len(bounds) >= 4:
            return voice_transcribe_click_target_from_bounds(
                source=source,
                label=str(duration_target.get("label") or "WeChat voice bubble context-menu anchor"),
                bounds=bounds[:4],
                item=duration_target.get("item") if isinstance(duration_target.get("item"), dict) else None,
            )
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


def message_rect_bounds(message: dict[str, Any]) -> list[float] | None:
    rect = message.get("bubble_rect") if isinstance(message, dict) else None
    if isinstance(rect, dict):
        values = [rect.get("left"), rect.get("top"), rect.get("right"), rect.get("bottom")]
    elif isinstance(rect, (list, tuple)) and len(rect) >= 4:
        values = list(rect[:4])
    else:
        return None
    try:
        left, top, right, bottom = [float(value) for value in values]
    except (TypeError, ValueError):
        return None
    if right <= left or bottom <= top:
        return None
    return [left, top, right, bottom]


def voice_context_anchor_rect_bounds(anchor: dict[str, Any] | None) -> list[float] | None:
    if not isinstance(anchor, dict):
        return None
    item = anchor.get("item") if isinstance(anchor.get("item"), dict) else {}
    rect = item.get("parser_bubble_rect")
    if isinstance(rect, (list, tuple)) and len(rect) >= 4:
        try:
            left, top, right, bottom = [float(value) for value in rect[:4]]
        except (TypeError, ValueError):
            left = top = right = bottom = 0.0
    else:
        try:
            left = float(item.get("left") or 0)
            top = float(item.get("top") or 0)
            right = float(item.get("right") or 0)
            bottom = float(item.get("bottom") or 0)
        except (TypeError, ValueError):
            left = top = right = bottom = 0.0
    if right <= left or bottom <= top:
        bounds = anchor.get("click_bounds")
        if isinstance(bounds, list) and len(bounds) >= 4:
            try:
                left, top, right, bottom = [float(value) for value in bounds[:4]]
            except (TypeError, ValueError):
                return None
    if right <= left or bottom <= top:
        return None
    return [left, top, right, bottom]


def voice_anchor_sender_role(anchor: dict[str, Any] | None, image_size: tuple[int, int] | None = None) -> str:
    if not isinstance(anchor, dict):
        return ""
    item = anchor.get("item") if isinstance(anchor.get("item"), dict) else {}
    role = str(item.get("sender_role") or "").strip().lower()
    if role in {"self", "sales", "sales_candidate"}:
        return "self"
    if role in {"customer", "contact"}:
        return "customer"
    rect = voice_context_anchor_rect_bounds(anchor)
    width = float((image_size or (0, 0))[0] or 0)
    if rect and width > 0:
        center_x = (rect[0] + rect[2]) / 2.0
        return "self" if center_x > width * 0.62 else "customer"
    return ""


def voice_anchor_duration_number(anchor: dict[str, Any] | None) -> str:
    if not isinstance(anchor, dict):
        return ""
    item = anchor.get("item") if isinstance(anchor.get("item"), dict) else {}
    text = str(item.get("voice_duration_text") or item.get("text") or "")
    match = re.search(r"\d{1,3}", voice_transcribe_compact_text(text))
    return match.group(0) if match else ""


def message_voice_duration_number(message: dict[str, Any]) -> str:
    value = message.get("voice_duration")
    if isinstance(value, (int, float)) and value > 0:
        return str(int(value))
    for key in ("voice_duration_text", "content", "content_raw_ocr"):
        text = str(message.get(key) or "")
        match = re.search(r"\d{1,3}", voice_transcribe_compact_text(text))
        if match:
            return match.group(0)
    ocr_items = message.get("ocr_items")
    if isinstance(ocr_items, list):
        for item in ocr_items:
            if not isinstance(item, dict):
                continue
            text = str(item.get("text") or "")
            match = re.search(r"\d{1,3}", voice_transcribe_compact_text(text))
            if match:
                return match.group(0)
    return ""


def voice_transcript_layout_matches_rect(
    message: dict[str, Any],
    voice_rect: list[float],
    *,
    role: str,
    allow_pre_click_shift: bool = False,
) -> bool:
    message_rect = message_rect_bounds(message)
    if not message_rect:
        return False
    voice_left, voice_top, voice_right, voice_bottom = voice_rect
    msg_left, msg_top, msg_right, msg_bottom = message_rect
    voice_width = max(1.0, voice_right - voice_left)
    voice_height = max(1.0, voice_bottom - voice_top)
    gap = msg_top - voice_bottom
    min_gap = -max(10.0, voice_height * 0.35)
    max_gap = max(96.0, voice_height * 2.8)
    if allow_pre_click_shift:
        min_gap = -max(72.0, voice_height * 2.2)
        max_gap = max(128.0, voice_height * 3.4)
    if gap < min_gap or gap > max_gap:
        return False
    if msg_bottom <= voice_top:
        return False
    align_tolerance = max(28.0, min(64.0, voice_width * 0.45))
    if role == "self":
        right_aligned = abs(msg_right - voice_right) <= max(42.0, align_tolerance)
        same_column = msg_left <= voice_right and msg_right >= voice_left - max(220.0, voice_width * 1.8)
        return bool(right_aligned and same_column)
    left_aligned = abs(msg_left - voice_left) <= max(42.0, align_tolerance)
    same_column = msg_right >= voice_left + min(48.0, voice_width * 0.5) and msg_left <= voice_right + max(90.0, voice_width * 0.8)
    return bool(left_aligned and same_column)


def voice_message_matches_clicked_anchor(
    message: dict[str, Any],
    anchor: dict[str, Any] | None,
    image_size: tuple[int, int],
) -> bool:
    if not voice_message_role_matches_clicked_anchor(message, anchor, image_size):
        return False
    anchor_duration = voice_anchor_duration_number(anchor)
    message_duration = message_voice_duration_number(message)
    return not (anchor_duration and message_duration and anchor_duration != message_duration)


def voice_message_role_matches_clicked_anchor(
    message: dict[str, Any],
    anchor: dict[str, Any] | None,
    image_size: tuple[int, int],
) -> bool:
    if not isinstance(message, dict) or not isinstance(anchor, dict):
        return False
    if not message_is_voice_record(message):
        return False
    anchor_role = voice_anchor_sender_role(anchor, image_size)
    message_role = str(message.get("sender_role") or message.get("sender") or "").strip().lower()
    if anchor_role == "self" and message_role not in {"self", "sales", "sales_candidate"}:
        return False
    if anchor_role == "customer" and message_role not in {"customer", "contact"}:
        return False
    return True


def message_has_same_row_avatar_structure(message: dict[str, Any]) -> bool:
    alignment = message.get("avatar_alignment")
    if not isinstance(alignment, dict):
        return False
    if str(alignment.get("role") or "").strip().lower() in {"self", "customer"}:
        return True
    for side in ("customer", "self"):
        details = alignment.get(side)
        if isinstance(details, dict) and bool(details.get("present")):
            return True
    return False


def message_is_combined_voice_transcript_record(message: dict[str, Any]) -> bool:
    if not message_is_voice_record(message) or message_is_untranscribed_voice_record(message):
        return False
    flags = message.get("quality_flags")
    if not isinstance(flags, list) or "voice_duration_prefix_removed" not in flags:
        return False
    raw = str(message.get("content_raw_ocr") or "").strip()
    content = str(message.get("content_clean") or message.get("content") or "").strip()
    return bool(raw and content and raw != content)


def combined_voice_transcript_matches_clicked_anchor(
    message: dict[str, Any],
    anchor: dict[str, Any] | None,
    image_size: tuple[int, int],
    *,
    after_messages: list[dict[str, Any]] | None = None,
) -> bool:
    return bool(
        combined_voice_transcript_anchor_match_evidence(
            message,
            anchor,
            image_size,
            after_messages=after_messages,
        ).get("accepted")
    )


def combined_voice_transcript_anchor_match_evidence(
    message: dict[str, Any],
    anchor: dict[str, Any] | None,
    image_size: tuple[int, int],
    *,
    after_messages: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    anchor_duration = voice_anchor_duration_number(anchor)
    message_duration = message_voice_duration_number(message)
    evidence: dict[str, Any] = {
        "accepted": False,
        "strategy": "rejected",
        "anchor_duration": anchor_duration,
        "message_duration": message_duration,
        "duration_conflict": bool(
            anchor_duration and message_duration and anchor_duration != message_duration
        ),
        "structural_candidate_count": 0,
    }
    if not voice_message_role_matches_clicked_anchor(message, anchor, image_size):
        evidence["reason"] = "role_or_voice_structure_mismatch"
        return evidence
    message_rect = message_rect_bounds(message)
    anchor_rect = voice_context_anchor_rect_bounds(anchor)
    if not message_rect or not anchor_rect:
        evidence["reason"] = "missing_layout_bounds"
        return evidence
    role = voice_anchor_sender_role(anchor, image_size)
    anchor_left, anchor_top, anchor_right, anchor_bottom = anchor_rect
    message_left, message_top, message_right, message_bottom = message_rect
    row_height = max(1.0, anchor_bottom - anchor_top)
    lane_limit = max(48.0, row_height * 2.0)
    if role == "self":
        if abs(message_right - anchor_right) > lane_limit:
            evidence["reason"] = "self_lane_mismatch"
            return evidence
    elif abs(message_left - anchor_left) > lane_limit:
        evidence["reason"] = "customer_lane_mismatch"
        return evidence

    # Expanded text can make a voice record grow, but duration alone never
    # authorizes a match after a viewport shift. A local row/lane relation is
    # still required so a distant same-duration voice cannot be rebound.
    comparable: list[dict[str, Any]] = []
    for candidate in after_messages or [message]:
        if not isinstance(candidate, dict) or not message_is_combined_voice_transcript_record(candidate):
            continue
        if not voice_message_role_matches_clicked_anchor(candidate, anchor, image_size):
            continue
        candidate_rect = message_rect_bounds(candidate)
        if not candidate_rect:
            continue
        candidate_left, candidate_top, candidate_right, candidate_bottom = candidate_rect
        lane_delta = abs(candidate_right - anchor_right) if role == "self" else abs(candidate_left - anchor_left)
        if lane_delta > lane_limit:
            continue
        vertical_overlap = max(0.0, min(anchor_bottom, candidate_bottom) - max(anchor_top, candidate_top))
        vertical_gap = 0.0
        if vertical_overlap <= 0:
            vertical_gap = min(abs(anchor_top - candidate_bottom), abs(candidate_top - anchor_bottom))
        local_relation = vertical_overlap > 0 or vertical_gap <= max(16.0, row_height * 0.75)
        candidate_duration = message_voice_duration_number(candidate)
        duration_exact = bool(anchor_duration and candidate_duration and anchor_duration == candidate_duration)
        comparable.append(
            {
                "message": candidate,
                "duration": candidate_duration,
                "duration_exact": duration_exact,
                "local_relation": local_relation,
                "vertical_overlap": vertical_overlap,
                "vertical_gap": vertical_gap,
                "lane_delta": lane_delta,
            }
        )
    evidence["structural_candidate_count"] = len(comparable)
    evidence["vertical_overlap"] = max(
        0.0,
        min(anchor_bottom, message_bottom) - max(anchor_top, message_top),
    )
    if evidence["vertical_overlap"] <= 0:
        evidence["vertical_gap"] = min(
            abs(anchor_top - message_bottom),
            abs(message_top - anchor_bottom),
        )
    else:
        evidence["vertical_gap"] = 0.0
    if not comparable:
        evidence["reason"] = "no_structural_candidate"
        return evidence

    exact_duration_candidates = [candidate for candidate in comparable if candidate["duration_exact"]]
    local_candidates = [candidate for candidate in comparable if candidate["local_relation"]]
    local_exact_candidates = [candidate for candidate in exact_duration_candidates if candidate["local_relation"]]
    if local_exact_candidates:
        selected_pool = local_exact_candidates
        strategy = "unique_duration_and_region"
    else:
        selected_pool = local_candidates
        strategy = (
            "unique_structure_with_duration_conflict"
            if evidence["duration_conflict"]
            else "unique_structural_region"
        )

    accepted = len(selected_pool) == 1 and (
        selected_pool[0]["message"] is message
        or selected_pool[0]["message"] == message
    )
    if accepted:
        reason = "unique_structural_match"
    elif len(selected_pool) > 1:
        reason = "ambiguous_duration_conflict" if evidence["duration_conflict"] else "ambiguous_structural_match"
    elif exact_duration_candidates:
        reason = "ambiguous_duration_match"
    else:
        reason = "no_local_structural_match"
    evidence.update(
        {
            "accepted": accepted,
            "strategy": strategy if selected_pool else "rejected",
            "reason": reason,
            "exact_duration_candidate_count": len(exact_duration_candidates),
            "local_candidate_count": len(local_candidates),
            "selected_candidate_count": len(selected_pool),
        }
    )
    return evidence


def message_is_plausible_voice_transcript_for_anchor(
    message: dict[str, Any],
    anchor: dict[str, Any] | None,
    image_size: tuple[int, int],
    *,
    after_messages: list[dict[str, Any]] | None = None,
) -> bool:
    if not isinstance(message, dict):
        return False
    content = str(message.get("content_clean") or message.get("content") or "").strip()
    if not content:
        return False
    if voice_duration_text_like(content):
        return False
    if voice_transcribe_button_text_like(content) or voice_transcribe_collapse_text_like(content):
        return False
    if text_message_context_menu_text_like(content) or avatar_context_menu_text_like(content):
        return False
    # The parser intentionally merges a duration row and its expanded transcript
    # into one voice record. That record owns the original voice-row avatar, so it
    # must be matched before applying the no-avatar rule for split text records.
    if message_is_combined_voice_transcript_record(message):
        return combined_voice_transcript_matches_clicked_anchor(
            message,
            anchor,
            image_size,
            after_messages=after_messages,
        )
    # A normal message owns an avatar on its row. WeChat's expanded voice text
    # sits below the voice bubble and does not own a second row-level avatar.
    if message_has_same_row_avatar_structure(message):
        return False
    message_rect = message_rect_bounds(message)
    anchor_rect = voice_context_anchor_rect_bounds(anchor)
    if not message_rect or not anchor_rect:
        return False
    role = voice_anchor_sender_role(anchor, image_size)
    matching_voice_rects: list[list[float]] = []
    for candidate in after_messages or []:
        if not isinstance(candidate, dict) or candidate is message:
            continue
        if not voice_message_matches_clicked_anchor(candidate, anchor, image_size):
            continue
        candidate_rect = message_rect_bounds(candidate)
        if not candidate_rect:
            continue
        if voice_transcript_layout_matches_rect(message, candidate_rect, role=role):
            matching_voice_rects.append(candidate_rect)
    # Do not bind against the pre-click coordinates. Expanding text can move the
    # viewport; without exactly one post-click voice/text pair, ownership is unknown.
    return len(matching_voice_rects) == 1


def rects_overlap_or_near(first: list[float], second: list[float], *, pad: float = 8.0) -> bool:
    return not (
        first[2] < second[0] - pad
        or first[0] > second[2] + pad
        or first[3] < second[1] - pad
        or first[1] > second[3] + pad
    )


def message_is_voice_record(message: dict[str, Any]) -> bool:
    message_type = str(message.get("type") or message.get("message_type") or message.get("content_type") or "").lower()
    if message_type in {"voice", "audio"}:
        return True
    if message.get("voice_duration") is not None or message.get("voice_duration_text"):
        return True
    flags = message.get("quality_flags")
    return isinstance(flags, list) and "untranscribed_voice_placeholder" in flags


def message_is_untranscribed_voice_record(message: dict[str, Any]) -> bool:
    flags = message.get("quality_flags")
    if isinstance(flags, list) and "untranscribed_voice_placeholder" in flags:
        return True
    content = str(message.get("content_clean") or message.get("content") or "")
    compact = voice_transcribe_compact_text(content)
    return bool("语音" in compact and voice_duration_text_like(compact))


def message_is_text_record(message: dict[str, Any]) -> bool:
    message_type = str(message.get("type") or message.get("message_type") or message.get("content_type") or "").lower()
    return message_type == "text"


def message_voice_has_transcribed_text_below(
    message: dict[str, Any],
    parsed_messages: list[dict[str, Any]] | None,
    image_size: tuple[int, int],
) -> bool:
    if not parsed_messages or not message_is_voice_record(message):
        return False
    voice_rect = message_rect_bounds(message)
    if not voice_rect:
        return False
    role = str(message.get("sender_role") or message.get("sender") or "").strip().lower()
    if role in {"sales", "sales_candidate"}:
        role = "self"
    if role not in {"self", "customer"}:
        width = float(image_size[0] if image_size else 0)
        center_x = (voice_rect[0] + voice_rect[2]) / 2.0
        role = "self" if width > 0 and center_x > width * 0.62 else "customer"
    for candidate in parsed_messages:
        if not isinstance(candidate, dict) or candidate is message:
            continue
        if message_is_voice_record(candidate):
            continue
        content = str(candidate.get("content_clean") or candidate.get("content") or "").strip()
        if not content or message_is_untranscribed_voice_record(candidate) or is_message_noise(content):
            continue
        if not message_is_text_record(candidate):
            candidate_type = str(candidate.get("type") or candidate.get("message_type") or "").lower()
            if candidate_type not in {"", "unknown"}:
                continue
        if voice_transcript_layout_matches_rect(candidate, voice_rect, role=role):
            return True
    return False


def component_bounds(component: dict[str, Any]) -> list[float] | None:
    try:
        left = float(component.get("left") or 0)
        top = float(component.get("top") or 0)
        right = float(component.get("right") or 0)
        bottom = float(component.get("bottom") or 0)
    except (TypeError, ValueError):
        return None
    if right <= left or bottom <= top:
        return None
    return [left, top, right, bottom]


def parsed_message_overlapping_bounds(
    bounds: list[float],
    parsed_messages: list[dict[str, Any]] | None,
    *,
    pad: float = 8.0,
) -> dict[str, Any] | None:
    if not parsed_messages:
        return None
    best_message: dict[str, Any] | None = None
    best_area = -1.0
    for message in parsed_messages:
        if not isinstance(message, dict):
            continue
        rect = message_rect_bounds(message)
        if not rect or not rects_overlap_or_near(bounds, rect, pad=pad):
            continue
        overlap_left = max(bounds[0], rect[0])
        overlap_top = max(bounds[1], rect[1])
        overlap_right = min(bounds[2], rect[2])
        overlap_bottom = min(bounds[3], rect[3])
        area = max(0.0, overlap_right - overlap_left) * max(0.0, overlap_bottom - overlap_top)
        if area > best_area:
            best_area = area
            best_message = message
    return best_message


def visual_component_rejected_by_parsed_text(
    component: dict[str, Any],
    parsed_messages: list[dict[str, Any]] | None,
) -> bool:
    bounds = component_bounds(component)
    if not bounds:
        return False
    message = parsed_message_overlapping_bounds(bounds, parsed_messages, pad=10.0)
    return bool(message and message_is_text_record(message) and not message_is_voice_record(message))


def visual_component_overlaps_transcribed_parser_voice(
    component: dict[str, Any],
    parsed_messages: list[dict[str, Any]] | None,
    image_size: tuple[int, int],
) -> bool:
    bounds = component_bounds(component)
    if not bounds or not parsed_messages:
        return False
    for message in parsed_messages:
        if not isinstance(message, dict) or not message_is_voice_record(message):
            continue
        rect = message_rect_bounds(message)
        if not rect or not rects_overlap_or_near(bounds, rect, pad=18.0):
            continue
        if not message_is_untranscribed_voice_record(message):
            return True
        if message_voice_has_transcribed_text_below(message, parsed_messages, image_size):
            return True
    return False


def message_voice_context_anchor_targets(
    parsed_messages: list[dict[str, Any]] | None,
    image_size: tuple[int, int],
    *,
    excluded_anchor_keys: set[str] | None = None,
) -> list[dict[str, Any]]:
    if not parsed_messages:
        return []
    width, height = image_size
    split_x = session_split_x(width)
    top_limit = chat_header_cutoff_y(height)
    bottom_limit = height - max(DEFAULT_MESSAGE_BOTTOM_EXCLUDE_PX, int(height * 0.10))
    excluded = excluded_anchor_keys or set()
    targets: list[dict[str, Any]] = []
    for message in parsed_messages:
        if not isinstance(message, dict) or not message_is_untranscribed_voice_record(message):
            continue
        if message_voice_has_transcribed_text_below(message, parsed_messages, image_size):
            continue
        rect = message_rect_bounds(message)
        if not rect:
            continue
        left, top, right, bottom = rect
        center_x = (left + right) / 2.0
        center_y = (top + bottom) / 2.0
        if center_y < top_limit or center_y > bottom_limit:
            continue
        if right < split_x + 20:
            continue
        ocr_items = message.get("ocr_items")
        duration_item = None
        if isinstance(ocr_items, list):
            duration_item = next((item for item in ocr_items if isinstance(item, dict) and voice_duration_item_like(item)), None)
        if isinstance(duration_item, dict):
            item = dict(duration_item)
        else:
            item = {
                "text": str(message.get("voice_duration_text") or "[语音]"),
                "left": left,
                "top": top,
                "right": right,
                "bottom": bottom,
                "center_x": center_x,
                "center_y": center_y,
                "confidence": float(message.get("ocr_confidence") or 0.0),
            }
        is_self_side = str(message.get("sender_role") or "").lower() in {"self", "sales"} or center_x > width * 0.62
        if is_self_side:
            safe_left = max(split_x + 16, int(left) + 8)
            safe_right = min(width - 104, int(left) + min(112, max(44, int((right - left) * 0.72))))
        else:
            safe_left = max(split_x + 16, int(left) + 8)
            safe_right = min(width - 18, int(right) - 8)
        safe_top = max(top_limit, int(top) + 5)
        safe_bottom = min(bottom_limit, int(bottom) - 5)
        if safe_right <= safe_left:
            safe_right = min(width - (104 if is_self_side else 18), safe_left + 44)
        if safe_bottom <= safe_top:
            safe_bottom = min(bottom_limit, safe_top + 20)
        target = voice_transcribe_click_target_from_bounds(
            source="parser_voice_message_context_menu_anchor",
            label="Parser-confirmed WeChat voice message context-menu anchor",
            bounds=[safe_left, safe_top, safe_right, safe_bottom],
            item={
                **item,
                "message_id": str(message.get("id") or ""),
                "message_type": str(message.get("type") or ""),
                "sender_role": str(message.get("sender_role") or ""),
                "avatar_alignment": message.get("avatar_alignment") if isinstance(message.get("avatar_alignment"), dict) else {},
                "parser_bubble_rect": [left, top, right, bottom],
            },
        )
        mark_voice_context_anchor_keys(target, image_size)
        if voice_context_anchor_is_excluded(target, image_size, excluded):
            continue
        targets.append(target)
    return targets


def voice_duration_context_anchor_targets(
    ocr_items: list[dict[str, Any]],
    image_size: tuple[int, int],
    *,
    excluded_anchor_keys: set[str] | None = None,
) -> list[dict[str, Any]]:
    anchors: list[dict[str, Any]] = []
    excluded = excluded_anchor_keys or set()
    for item in ocr_items:
        if not voice_duration_item_like(item):
            continue
        if not voice_transcribe_item_is_in_chat_surface(item, image_size):
            continue
        anchor = voice_duration_context_click_target({"item": item}, image_size)
        if anchor:
            mark_voice_context_anchor_keys(anchor, image_size)
            if voice_context_anchor_is_excluded(anchor, image_size, excluded):
                continue
            anchors.append(anchor)
    return anchors


def find_voice_duration_context_anchor_target(
    ocr_items: list[dict[str, Any]],
    image_size: tuple[int, int],
    *,
    excluded_anchor_keys: set[str] | None = None,
) -> dict[str, Any] | None:
    anchors = [
        anchor
        for anchor in voice_duration_context_anchor_targets(
            ocr_items,
            image_size,
            excluded_anchor_keys=excluded_anchor_keys,
        )
        if not voice_duration_has_transcribed_text_below(
            anchor.get("item") if isinstance(anchor.get("item"), dict) else {},
            ocr_items,
            image_size,
        )
    ]
    if not anchors:
        return None
    return max(anchors, key=lambda target: float(((target.get("item") or {}).get("center_y") or 0)))


def green_voice_bubble_pixel(red: int, green: int, blue: int) -> bool:
    return bool(
        green >= 135
        and 70 <= red <= 190
        and 70 <= blue <= 190
        and green - red >= 35
        and green - blue >= 20
    )


def customer_voice_bubble_pixel(red: int, green: int, blue: int) -> bool:
    avg = (red + green + blue) / 3.0
    spread = max(red, green, blue) - min(red, green, blue)
    return bool(210.0 <= avg <= 245.0 and spread <= 18)


def find_visual_customer_voice_context_anchor_targets(
    image: Image.Image,
    image_size: tuple[int, int],
    ocr_items: list[dict[str, Any]] | None = None,
    excluded_anchor_keys: set[str] | None = None,
    parsed_messages: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    if image is None:
        return []
    try:
        rgb = image.convert("RGB")
    except Exception:
        return []
    width, height = image_size
    split_x = session_split_x(width)
    top_limit = chat_header_cutoff_y(height)
    bottom_limit = height - max(DEFAULT_MESSAGE_BOTTOM_EXCLUDE_PX, int(height * 0.10))
    left_limit = max(split_x + 48, int(width * 0.40))
    right_limit = min(width - 18, max(split_x + 360, int(width * 0.74)))
    row_runs: list[tuple[int, int, int, int]] = []
    for y in range(max(0, top_limit), min(height, bottom_limit)):
        xs: list[int] = []
        for x in range(left_limit, max(left_limit, right_limit)):
            red, green, blue = rgb.getpixel((x, y))
            if customer_voice_bubble_pixel(red, green, blue):
                xs.append(x)
        if xs:
            row_runs.append((y, min(xs), max(xs), len(xs)))
    if not row_runs:
        return []

    components: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for y, row_left, row_right, count in row_runs:
        if current is None or y > int(current["bottom"]) + 2:
            if current is not None:
                components.append(current)
            current = {"top": y, "bottom": y, "left": row_left, "right": row_right, "gray_count": count}
            continue
        current["bottom"] = y
        current["left"] = min(int(current["left"]), row_left)
        current["right"] = max(int(current["right"]), row_right)
        current["gray_count"] = int(current["gray_count"]) + count
    if current is not None:
        components.append(current)

    candidates: list[dict[str, Any]] = []
    excluded = excluded_anchor_keys or set()
    for component in components:
        left = int(component["left"])
        right = int(component["right"])
        top = int(component["top"])
        bottom = int(component["bottom"])
        bubble_width = right - left + 1
        bubble_height = bottom - top + 1
        center_x = (left + right) / 2.0
        center_y = (top + bottom) / 2.0
        gray_count = int(component.get("gray_count") or 0)
        if bubble_width < 92 or bubble_width > 260:
            continue
        if bubble_height < 28 or bubble_height > 72:
            continue
        if left < split_x + 42 or left > split_x + 170:
            continue
        if center_x > split_x + 320 or center_x > width * 0.70:
            continue
        if gray_count < 850:
            continue
        if visual_component_rejected_by_parsed_text(component, parsed_messages):
            continue
        if visual_component_overlaps_transcribed_parser_voice(component, parsed_messages, image_size):
            continue
        if visual_customer_voice_component_overlaps_text(component, ocr_items or [], image_size):
            continue
        if visual_customer_voice_component_has_transcribed_text_below(component, ocr_items or [], image_size):
            continue
        safe_left = max(split_x + 16, left + 8)
        safe_right = min(width - 18, right - 8)
        safe_top = max(top_limit, top + 5)
        safe_bottom = min(bottom_limit, bottom - 5)
        if safe_right <= safe_left:
            safe_right = min(width - 18, safe_left + 44)
        if safe_bottom <= safe_top:
            safe_bottom = min(bottom_limit, safe_top + 20)
        bounds = [safe_left, safe_top, safe_right, safe_bottom]
        target = voice_transcribe_click_target_from_bounds(
            source="visual_customer_voice_bubble_context_menu_anchor",
            label="Visually detected left-side WeChat customer voice bubble context-menu anchor",
            bounds=bounds,
            item={
                "text": "",
                "left": float(left),
                "top": float(top),
                "right": float(right),
                "bottom": float(bottom),
                "center_x": center_x,
                "center_y": center_y,
                "confidence": 0.0,
                "visual_gray_count": gray_count,
                "visual_bubble_size": [bubble_width, bubble_height],
            },
        )
        mark_voice_context_anchor_keys(target, image_size)
        if voice_context_anchor_is_excluded(target, image_size, excluded):
            continue
        candidates.append(target)
    return sorted(candidates, key=lambda target: float(((target.get("item") or {}).get("center_y") or 0)))


def find_visual_customer_voice_context_anchor_target(
    image: Image.Image,
    image_size: tuple[int, int],
    ocr_items: list[dict[str, Any]] | None = None,
    excluded_anchor_keys: set[str] | None = None,
    parsed_messages: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    targets = find_visual_customer_voice_context_anchor_targets(
        image,
        image_size,
        ocr_items,
        excluded_anchor_keys,
        parsed_messages,
    )
    return targets[-1] if targets else None


def visual_customer_voice_component_has_transcribed_text_below(
    component: dict[str, Any],
    ocr_items: list[dict[str, Any]],
    image_size: tuple[int, int],
) -> bool:
    return visual_voice_component_has_transcribed_layout_below(component, ocr_items, image_size, role="customer")


def visual_voice_component_has_transcribed_layout_below(
    component: dict[str, Any],
    ocr_items: list[dict[str, Any]],
    image_size: tuple[int, int],
    *,
    role: str,
) -> bool:
    voice_rect = component_bounds(component)
    if not voice_rect:
        return False
    for item in ocr_items:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        if voice_duration_item_like(item) or voice_transcribe_button_text_like(text):
            continue
        if voice_transcribe_collapse_text_like(text) or text_message_context_menu_text_like(text):
            continue
        if is_message_noise(text):
            continue
        if not voice_transcribe_item_is_in_chat_surface(item, image_size):
            continue
        message = {
            "type": "text",
            "content": text,
            "bubble_rect": [
                float(item.get("left") or 0),
                float(item.get("top") or 0),
                float(item.get("right") or 0),
                float(item.get("bottom") or 0),
            ],
        }
        if voice_transcript_layout_matches_rect(message, voice_rect, role=role):
            return True
    return False


def visual_customer_voice_component_overlaps_text(
    component: dict[str, Any],
    ocr_items: list[dict[str, Any]],
    image_size: tuple[int, int],
) -> bool:
    left = float(component.get("left") or 0)
    top = float(component.get("top") or 0)
    right = float(component.get("right") or 0)
    bottom = float(component.get("bottom") or 0)
    if right <= left or bottom <= top:
        return False
    expanded_left = left - 8
    expanded_top = top - 8
    expanded_right = right + 8
    expanded_bottom = bottom + 8
    for item in ocr_items:
        text = str(item.get("text") or "").strip()
        if not text or voice_duration_item_like(item) or voice_transcribe_button_text_like(text) or is_message_noise(text):
            continue
        if not voice_transcribe_item_is_in_chat_surface(item, image_size):
            continue
        center_x = float(item.get("center_x") or 0)
        center_y = float(item.get("center_y") or 0)
        if expanded_left <= center_x <= expanded_right and expanded_top <= center_y <= expanded_bottom:
            return True
    return False


def find_visual_self_voice_context_anchor_targets(
    image: Image.Image,
    image_size: tuple[int, int],
    ocr_items: list[dict[str, Any]] | None = None,
    excluded_anchor_keys: set[str] | None = None,
    parsed_messages: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    if image is None:
        return []
    try:
        rgb = image.convert("RGB")
    except Exception:
        return []
    width, height = image_size
    split_x = session_split_x(width)
    top_limit = chat_header_cutoff_y(height)
    bottom_limit = height - max(DEFAULT_MESSAGE_BOTTOM_EXCLUDE_PX, int(height * 0.10))
    left_limit = max(split_x + 120, int(width * 0.58))
    right_limit = max(left_limit + 1, width - 18)
    row_runs: list[tuple[int, int, int, int]] = []
    for y in range(max(0, top_limit), min(height, bottom_limit)):
        xs: list[int] = []
        for x in range(left_limit, min(width, right_limit)):
            red, green, blue = rgb.getpixel((x, y))
            if green_voice_bubble_pixel(red, green, blue):
                xs.append(x)
        if not xs:
            continue
        row_runs.append((y, min(xs), max(xs), len(xs)))
    if not row_runs:
        return []

    components: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for y, row_left, row_right, count in row_runs:
        if current is None or y > int(current["bottom"]) + 2:
            if current is not None:
                components.append(current)
            current = {"top": y, "bottom": y, "left": row_left, "right": row_right, "green_count": count}
            continue
        current["bottom"] = y
        current["left"] = min(int(current["left"]), row_left)
        current["right"] = max(int(current["right"]), row_right)
        current["green_count"] = int(current["green_count"]) + count
    if current is not None:
        components.append(current)

    candidates: list[dict[str, Any]] = []
    excluded = excluded_anchor_keys or set()
    for component in components:
        left = int(component["left"])
        right = int(component["right"])
        top = int(component["top"])
        bottom = int(component["bottom"])
        bubble_width = right - left + 1
        bubble_height = bottom - top + 1
        center_x = (left + right) / 2.0
        center_y = (top + bottom) / 2.0
        green_count = int(component.get("green_count") or 0)
        if bubble_width < 42 or bubble_width > 220:
            continue
        if bubble_height < 22 or bubble_height > 76:
            continue
        if center_x < width * 0.62 or right < width * 0.70:
            continue
        if green_count < 220:
            continue
        if visual_component_rejected_by_parsed_text(component, parsed_messages):
            continue
        if visual_component_overlaps_transcribed_parser_voice(component, parsed_messages, image_size):
            continue
        if visual_self_voice_component_overlaps_text(component, ocr_items or [], image_size):
            continue
        if visual_voice_component_has_transcribed_layout_below(component, ocr_items or [], image_size, role="self"):
            continue
        safe_left = max(split_x + 16, left + 8)
        safe_right = min(width - 104, left + min(110, max(44, int(bubble_width * 0.72))))
        safe_top = max(top_limit, top + 5)
        safe_bottom = min(bottom_limit, bottom - 5)
        if safe_right <= safe_left:
            safe_right = min(width - 104, safe_left + 44)
        if safe_bottom <= safe_top:
            safe_bottom = min(bottom_limit, safe_top + 20)
        bounds = [safe_left, safe_top, safe_right, safe_bottom]
        target = voice_transcribe_click_target_from_bounds(
            source="visual_self_voice_bubble_context_menu_anchor",
            label="Visually detected right-side WeChat voice bubble context-menu anchor",
            bounds=bounds,
            item={
                "text": "",
                "left": float(left),
                "top": float(top),
                "right": float(right),
                "bottom": float(bottom),
                "center_x": center_x,
                "center_y": center_y,
                "confidence": 0.0,
                "visual_green_count": green_count,
                "visual_bubble_size": [bubble_width, bubble_height],
            },
        )
        mark_voice_context_anchor_keys(target, image_size)
        if voice_context_anchor_is_excluded(target, image_size, excluded):
            continue
        candidates.append(target)
    return sorted(candidates, key=lambda target: float(((target.get("item") or {}).get("center_y") or 0)))


def find_visual_self_voice_context_anchor_target(
    image: Image.Image,
    image_size: tuple[int, int],
    ocr_items: list[dict[str, Any]] | None = None,
    excluded_anchor_keys: set[str] | None = None,
    parsed_messages: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    targets = find_visual_self_voice_context_anchor_targets(
        image,
        image_size,
        ocr_items,
        excluded_anchor_keys,
        parsed_messages,
    )
    return targets[-1] if targets else None


def visual_self_voice_component_overlaps_text(
    component: dict[str, Any],
    ocr_items: list[dict[str, Any]],
    image_size: tuple[int, int],
) -> bool:
    left = float(component.get("left") or 0)
    top = float(component.get("top") or 0)
    right = float(component.get("right") or 0)
    bottom = float(component.get("bottom") or 0)
    if right <= left or bottom <= top:
        return False
    expanded_left = left - 8
    expanded_top = top - 8
    expanded_right = right + 8
    expanded_bottom = bottom + 8
    for item in ocr_items:
        text = str(item.get("text") or "").strip()
        if not text or voice_duration_item_like(item) or voice_transcribe_button_text_like(text) or is_message_noise(text):
            continue
        if not voice_transcribe_item_is_in_chat_surface(item, image_size):
            continue
        center_x = float(item.get("center_x") or 0)
        center_y = float(item.get("center_y") or 0)
        if expanded_left <= center_x <= expanded_right and expanded_top <= center_y <= expanded_bottom:
            return True
    return False


def voice_target_matches_parsed_message(
    target: dict[str, Any],
    message: dict[str, Any],
    image_size: tuple[int, int],
) -> bool:
    target_rect = voice_context_anchor_rect_bounds(target)
    message_rect = message_rect_bounds(message)
    if not target_rect or not message_rect or not rects_overlap_or_near(target_rect, message_rect, pad=18.0):
        return False
    target_role = voice_anchor_sender_role(target, image_size)
    message_role = str(message.get("sender_role") or message.get("sender") or "").strip().lower()
    if message_role in {"sales", "sales_candidate"}:
        message_role = "self"
    if message_role == "contact":
        message_role = "customer"
    if target_role in {"self", "customer"} and message_role in {"self", "customer"} and target_role != message_role:
        return False
    target_duration = voice_anchor_duration_number(target)
    message_duration = message_voice_duration_number(message)
    return not (target_duration and message_duration and target_duration != message_duration)


def normalized_voice_sender_role(value: Any) -> str:
    role = str(value or "").strip().lower()
    if role in {"self", "sales", "sales_candidate"}:
        return "self"
    if role in {"customer", "contact"}:
        return "customer"
    return "unknown"


def voice_structural_anchor_key(
    *,
    role: str,
    duration: str,
    ordinal_from_bottom: int,
) -> str:
    seed = {
        "side": normalized_voice_sender_role(role),
        "duration": str(duration or "").strip(),
        "ordinal_from_bottom": max(1, int(ordinal_from_bottom or 1)),
    }
    return "voice-structural:" + hashlib.sha1(
        json.dumps(seed, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:20]


def attach_structural_voice_anchor_keys(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Attach one relative voice identity in every parsed message frame."""
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for message in messages:
        if not isinstance(message, dict):
            continue
        if str(message.get("type") or message.get("message_type") or "").lower() not in {"voice", "audio"}:
            continue
        role = normalized_voice_sender_role(message.get("sender_role") or message.get("sender"))
        duration = str(message.get("voice_duration") or "").strip()
        if not duration:
            duration_match = re.search(
                r"\d{1,3}",
                voice_transcribe_compact_text(message.get("voice_duration_text")),
            )
            duration = duration_match.group(0) if duration_match else ""
        if role not in {"customer", "self"} or not duration:
            continue
        groups.setdefault((role, duration), []).append(message)

    def message_center_y(message: dict[str, Any]) -> float:
        rect = message.get("bubble_rect")
        if isinstance(rect, dict):
            return (float(rect.get("top") or 0) + float(rect.get("bottom") or 0)) / 2.0
        if isinstance(rect, (list, tuple)) and len(rect) >= 4:
            return (float(rect[1]) + float(rect[3])) / 2.0
        return 0.0

    for (role, duration), group in groups.items():
        for ordinal_from_bottom, message in enumerate(
            sorted(group, key=message_center_y, reverse=True),
            start=1,
        ):
            structural_key = voice_structural_anchor_key(
                role=role,
                duration=duration,
                ordinal_from_bottom=ordinal_from_bottom,
            )
            message["voice_anchor_structural_key"] = structural_key
            message["voice_anchor_key"] = structural_key
            flags = set(message.get("quality_flags") or [])
            if "untranscribed_voice_placeholder" not in flags:
                message["parent_voice_anchor_key"] = structural_key
    return messages


def unified_voice_observation_rect(observation: dict[str, Any]) -> list[float] | None:
    rect = observation.get("bubble_rect")
    if isinstance(rect, dict):
        values = [rect.get("left"), rect.get("top"), rect.get("right"), rect.get("bottom")]
    elif isinstance(rect, (list, tuple)) and len(rect) >= 4:
        values = list(rect[:4])
    else:
        return None
    try:
        left, top, right, bottom = [float(value) for value in values]
    except (TypeError, ValueError):
        return None
    return [left, top, right, bottom] if right > left and bottom > top else None


def voice_target_matches_unified_observation(
    target: dict[str, Any],
    observation: dict[str, Any],
    image_size: tuple[int, int],
) -> bool:
    source_message = observation.get("source_message")
    if isinstance(source_message, dict) and source_message:
        return voice_target_matches_parsed_message(target, source_message, image_size)

    target_rect = voice_context_anchor_rect_bounds(target)
    observation_rect = unified_voice_observation_rect(observation)
    if not target_rect or not observation_rect or not rects_overlap_or_near(target_rect, observation_rect, pad=18.0):
        return False
    target_role = normalized_voice_sender_role(voice_anchor_sender_role(target, image_size))
    observation_role = normalized_voice_sender_role(observation.get("sender_role"))
    if target_role != "unknown" and observation_role != "unknown" and target_role != observation_role:
        return False
    target_duration = voice_anchor_duration_number(target)
    observation_duration = str(observation.get("voice_duration") or "")
    if not observation_duration:
        match = re.search(r"\d{1,3}", voice_transcribe_compact_text(observation.get("voice_duration_text")))
        observation_duration = match.group(0) if match else ""
    return not (target_duration and observation_duration and target_duration != observation_duration)


def normalize_voice_evidence_target(
    image: Image.Image,
    target: dict[str, Any],
    image_size: tuple[int, int],
) -> dict[str, Any]:
    normalized = dict(target)
    anchor_rect = voice_context_anchor_rect_bounds(normalized)
    source = str(normalized.get("source") or "")
    click_bounds = normalized.get("click_bounds")
    if source.startswith("visual_") and isinstance(click_bounds, list) and len(click_bounds) >= 4:
        anchor_rect = [float(value) for value in click_bounds[:4]]
    avatar_alignment = message_row_avatar_role_details(image, anchor_rect or [], image_size)
    avatar_role = str(avatar_alignment.get("role") or "")
    item = dict(normalized.get("item") or {})
    expected_role = "self" if "self_voice" in source else ("customer" if "customer_voice" in source else "")
    if avatar_role in {"self", "customer"} and (not expected_role or expected_role == avatar_role):
        item["sender_role"] = avatar_role
        item["avatar_alignment"] = avatar_alignment
    normalized["item"] = item
    normalized["avatar_alignment"] = avatar_alignment
    mark_voice_context_anchor_keys(normalized, image_size)
    return normalized


def build_unified_voice_observations_v3(
    image: Image.Image,
    ocr_items: list[dict[str, Any]],
    image_size: tuple[int, int],
    *,
    excluded_anchor_keys: set[str] | None = None,
    parsed_messages: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Fuse parser, OCR, pixels, avatar and button evidence into one voice truth."""
    messages = [message for message in parsed_messages or [] if isinstance(message, dict)]
    excluded = excluded_anchor_keys or set()
    parser_targets = {
        str((target.get("item") or {}).get("message_id") or ""): normalize_voice_evidence_target(image, target, image_size)
        for target in message_voice_context_anchor_targets(messages, image_size)
    }
    observations: list[dict[str, Any]] = []
    for message in messages:
        if not message_is_voice_record(message):
            continue
        message_id = str(message.get("id") or message.get("message_id") or "")
        has_transcript = message_is_combined_voice_transcript_record(message) or message_voice_has_transcribed_text_below(
            message,
            messages,
            image_size,
        )
        untranscribed = message_is_untranscribed_voice_record(message) and not has_transcript
        target = parser_targets.get(message_id) if untranscribed else None
        target_avatar_role = str(((target or {}).get("avatar_alignment") or {}).get("role") or "")
        message_role = normalized_voice_sender_role(message.get("sender_role") or message.get("sender"))
        if isinstance(target, dict) and target_avatar_role != message_role:
            target = None
        observations.append(
            {
                "schema_version": C2_OBSERVATION_SCHEMA_VERSION,
                "row_kind": "voice_bubble",
                "voice_state": "untranscribed" if untranscribed else "transcribed",
                "sender_role": normalized_voice_sender_role(message.get("sender_role") or message.get("sender")),
                "sender_role_source": "same_row_avatar"
                if (
                    message_has_same_row_avatar_structure(message)
                    or (target_avatar_role in {"customer", "self"} and target_avatar_role == message_role)
                )
                else "unknown",
                "bubble_rect": message.get("bubble_rect"),
                "voice_duration": message.get("voice_duration"),
                "voice_duration_text": message.get("voice_duration_text"),
                "source_message_id": message_id,
                "source_message_key": str(message.get("source_message_key") or ""),
                "action_target": target,
                "visible_button_target": None,
                "evidence_sources": ["parser"],
                "source_message": message,
            }
        )

    def matching_observation(target: dict[str, Any]) -> dict[str, Any] | None:
        matches = [
            observation
            for observation in observations
            if voice_target_matches_unified_observation(target, observation, image_size)
        ]
        if not matches:
            return None
        target_y = voice_target_center_y(target)
        return min(
            matches,
            key=lambda observation: abs(
                target_y
                - (
                    (float((observation.get("bubble_rect") or {}).get("top") or 0) + float((observation.get("bubble_rect") or {}).get("bottom") or 0)) / 2.0
                    if isinstance(observation.get("bubble_rect"), dict)
                    else target_y
                )
            ),
        )

    def merge_evidence(target: dict[str, Any] | None, source: str, *, inferred_state: str = "untranscribed") -> None:
        if not isinstance(target, dict):
            return
        normalized = normalize_voice_evidence_target(image, target, image_size)
        expected_role = "self" if "self_voice" in source else ("customer" if "customer_voice" in source else "")
        actual_role = voice_anchor_sender_role(normalized, image_size)
        avatar_role = str((normalized.get("avatar_alignment") or {}).get("role") or "")
        if avatar_role not in {"self", "customer"}:
            return
        actual_role = avatar_role
        if expected_role and actual_role and expected_role != actual_role:
            return
        matched = matching_observation(normalized)
        if matched is not None:
            matched["sender_role"] = normalized_voice_sender_role(actual_role)
            matched["sender_role_source"] = "same_row_avatar"
            if source not in matched["evidence_sources"]:
                matched["evidence_sources"].append(source)
            if matched.get("voice_state") == "untranscribed" and not matched.get("action_target"):
                matched["action_target"] = normalized
            return
        rect = voice_context_anchor_rect_bounds(normalized)
        if not rect:
            return
        item = normalized.get("item") if isinstance(normalized.get("item"), dict) else {}
        observations.append(
            {
                "schema_version": C2_OBSERVATION_SCHEMA_VERSION,
                "row_kind": "voice_bubble",
                "voice_state": inferred_state,
                "sender_role": normalized_voice_sender_role(actual_role),
                "sender_role_source": "same_row_avatar" if actual_role in {"self", "customer"} else "unknown",
                "bubble_rect": {"left": rect[0], "top": rect[1], "right": rect[2], "bottom": rect[3]},
                "voice_duration": None,
                "voice_duration_text": str(item.get("text") or ""),
                "source_message_id": "",
                "source_message_key": "",
                "action_target": normalized if inferred_state == "untranscribed" else None,
                "visible_button_target": None,
                "evidence_sources": [source],
                "source_message": {},
            }
        )

    for raw_target in voice_duration_context_anchor_targets(ocr_items, image_size):
        raw_item = raw_target.get("item") if isinstance(raw_target.get("item"), dict) else {}
        raw_state = "transcribed" if voice_duration_has_transcribed_text_below(raw_item, ocr_items, image_size) else "untranscribed"
        merge_evidence(raw_target, "ocr_duration", inferred_state=raw_state)

    for visual_target in find_visual_customer_voice_context_anchor_targets(
        image,
        image_size,
        ocr_items,
        parsed_messages=messages,
    ):
        merge_evidence(visual_target, "visual_customer_bubble")
    for visual_target in find_visual_self_voice_context_anchor_targets(
        image,
        image_size,
        ocr_items,
        parsed_messages=messages,
    ):
        merge_evidence(visual_target, "visual_self_bubble")

    for index, observation in enumerate(observations):
        rect = observation.get("bubble_rect")
        source_message = observation.get("source_message")
        if not isinstance(source_message, dict) or not source_message:
            source_message = {
                "id": str(observation.get("source_message_id") or f"visual-voice-{index}"),
                "type": "voice",
                "sender_role": observation.get("sender_role"),
                "bubble_rect": rect,
            }
            observation["source_message"] = source_message
        observation["observation_id"] = str(
            observation.get("observation_id")
            or observation.get("source_message_id")
            or source_message.get("id")
            or f"voice-observation-{index}"
        )
        observation["message_type"] = "voice"
        errors = validate_message_observation_v3(observation)
        if errors:
            observation["contract_errors"] = errors
        else:
            observation.pop("contract_errors", None)

    pending = [
        observation
        for observation in observations
        if observation.get("voice_state") == "untranscribed"
        and not observation.get("contract_errors")
        and isinstance(observation.get("action_target"), dict)
    ]
    for button in find_voice_transcribe_targets(ocr_items, image_size, allow_inferred=False):
        if not pending:
            break
        button_y = voice_target_center_y(button)
        nearest = min(pending, key=lambda observation: abs(button_y - voice_target_center_y(observation.get("action_target"))))
        if abs(button_y - voice_target_center_y(nearest.get("action_target"))) <= 96.0:
            nearest["visible_button_target"] = button
            if "visible_transcribe_button" not in nearest["evidence_sources"]:
                nearest["evidence_sources"].append("visible_transcribe_button")

    def observation_center_y(observation: dict[str, Any]) -> float:
        target = observation.get("action_target")
        if isinstance(target, dict):
            return voice_target_center_y(target)
        rect = observation.get("bubble_rect")
        if isinstance(rect, dict):
            return (float(rect.get("top") or 0) + float(rect.get("bottom") or 0)) / 2.0
        if isinstance(rect, (list, tuple)) and len(rect) >= 4:
            return (float(rect[1]) + float(rect[3])) / 2.0
        return 0.0

    structural_groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for observation in observations:
        role = normalized_voice_sender_role(observation.get("sender_role"))
        duration = str(observation.get("voice_duration") or "")
        if not duration:
            duration_match = re.search(r"\d{1,3}", voice_transcribe_compact_text(observation.get("voice_duration_text")))
            duration = duration_match.group(0) if duration_match else ""
        structural_groups.setdefault((role, duration), []).append(observation)
    for (role, duration), group in structural_groups.items():
        for ordinal_from_bottom, observation in enumerate(
            sorted(group, key=observation_center_y, reverse=True),
            start=1,
        ):
            structural_key = voice_structural_anchor_key(
                role=role,
                duration=duration,
                ordinal_from_bottom=ordinal_from_bottom,
            )
            observation["voice_anchor_structural_key"] = structural_key
            source_message = observation.get("source_message")
            if isinstance(source_message, dict):
                source_message["voice_anchor_structural_key"] = structural_key
                source_message["voice_anchor_key"] = structural_key
                if observation.get("voice_state") == "transcribed":
                    source_message["parent_voice_anchor_key"] = structural_key
            target = observation.get("action_target")
            if isinstance(target, dict):
                target["anchor_structural_key"] = structural_key

    for observation in observations:
        target = observation.get("action_target")
        if isinstance(target, dict):
            observation["voice_anchor_key"] = str(target.get("anchor_structural_key") or target.get("anchor_stable_key") or target.get("anchor_key") or "")
            observation["excluded"] = voice_context_anchor_is_excluded(target, image_size, excluded)
        else:
            observation["voice_anchor_key"] = None
            observation["excluded"] = False
    return observations


def find_unified_untranscribed_voice_observation(
    image: Image.Image,
    ocr_items: list[dict[str, Any]],
    image_size: tuple[int, int],
    *,
    excluded_anchor_keys: set[str] | None = None,
    parsed_messages: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    observations = [
        observation
        for observation in build_unified_voice_observations_v3(
            image,
            ocr_items,
            image_size,
            excluded_anchor_keys=excluded_anchor_keys,
            parsed_messages=parsed_messages,
        )
        if observation.get("voice_state") == "untranscribed"
        and not observation.get("contract_errors")
        and not observation.get("excluded")
        and isinstance(observation.get("action_target"), dict)
    ]
    if not observations:
        return None
    return max(observations, key=lambda observation: voice_target_center_y(observation.get("action_target")))


def find_voice_context_menu_anchor_target(
    image: Image.Image,
    ocr_items: list[dict[str, Any]],
    image_size: tuple[int, int],
    *,
    excluded_anchor_keys: set[str] | None = None,
    parsed_messages: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    observation = find_unified_untranscribed_voice_observation(
        image,
        ocr_items,
        image_size,
        excluded_anchor_keys=excluded_anchor_keys,
        parsed_messages=parsed_messages,
    )
    if not observation:
        return None
    target = dict(observation["action_target"])
    target["unified_voice_observation"] = {
        key: value
        for key, value in observation.items()
        if key not in {"action_target", "visible_button_target", "source_message"}
    }
    return target


def visible_untranscribed_voice_hint(
    image: Image.Image,
    ocr_items: list[dict[str, Any]],
    image_size: tuple[int, int],
    *,
    parsed_messages: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Return a read-only visual hint; this function never clicks the UI."""
    observation = find_unified_untranscribed_voice_observation(
        image,
        ocr_items,
        image_size,
        parsed_messages=parsed_messages,
    )
    if not isinstance(observation, dict):
        return {"detected": False}
    anchor = observation.get("action_target") if isinstance(observation.get("action_target"), dict) else {}
    source = str(anchor.get("source") or "")
    avatar_alignment = anchor.get("avatar_alignment") if isinstance(anchor.get("avatar_alignment"), dict) else {}
    item = anchor.get("item") if isinstance(anchor.get("item"), dict) else {}
    sender_role = str(observation.get("sender_role") or avatar_alignment.get("role") or item.get("sender_role") or item.get("sender") or "")
    if sender_role == "contact":
        sender_role = "customer"
    if sender_role not in {"customer", "self"}:
        return {"detected": False}
    bounds = voice_context_anchor_rect_bounds(anchor) or anchor.get("click_bounds") or []
    return {
        "detected": True,
        "detection_mode": "unified_voice_observation_v3",
        "source": source,
        "sender_role": sender_role,
        "anchor_key": str(anchor.get("anchor_key") or ""),
        "anchor_stable_key": str(anchor.get("anchor_stable_key") or ""),
        "anchor_structural_key": str(anchor.get("anchor_structural_key") or ""),
        "bubble_rect": [int(round(float(value))) for value in bounds[:4]],
        "center_y": float(item.get("center_y") or 0),
        "avatar_alignment": avatar_alignment,
        "evidence_sources": list(observation.get("evidence_sources") or []),
    }


def validate_message_observation_v3(observation: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if int(observation.get("schema_version") or 0) != C2_OBSERVATION_SCHEMA_VERSION:
        errors.append("OBSERVATION_SCHEMA_VERSION_MISMATCH")
    row_kind = str(observation.get("row_kind") or "").strip()
    rule = C2_ROW_RULES.get(row_kind)
    if not isinstance(rule, dict):
        return [*errors, "OBSERVATION_ROW_KIND_UNKNOWN"]
    item_state = str(observation.get("item_state") or "discovered").strip().lower()
    required_fields = rule.get("required_fields") or []
    if row_kind == "image_bubble" and item_state == "discovered":
        required_fields = rule.get("discovery_required_fields") or required_fields
    elif row_kind == "image_bubble" and item_state == "failed":
        required_fields = rule.get("failed_required_fields") or required_fields
    for field in required_fields:
        value = observation.get(str(field))
        if value is None or (isinstance(value, str) and not value.strip()):
            errors.append(f"OBSERVATION_REQUIRED_FIELD_MISSING:{field}")
    if str(observation.get("message_type") or "") != str(rule.get("message_type") or ""):
        errors.append("OBSERVATION_MESSAGE_TYPE_MISMATCH")
    allowed_roles = rule.get("allowed_sender_roles") or []
    allowed_role_sources = rule.get("allowed_sender_role_sources") or []
    if row_kind == "image_bubble" and str(observation.get("item_state") or "discovered") == "discovered":
        allowed_roles = rule.get("discovery_allowed_sender_roles") or allowed_roles
        allowed_role_sources = rule.get("discovery_allowed_sender_role_sources") or allowed_role_sources
    if str(observation.get("sender_role") or "") not in {
        str(value) for value in allowed_roles
    }:
        errors.append("OBSERVATION_SENDER_ROLE_INVALID")
    if str(observation.get("sender_role_source") or "") not in {
        str(value) for value in allowed_role_sources
    }:
        errors.append("OBSERVATION_ROLE_SOURCE_INVALID")
    if str(observation.get("voice_state") or "") not in {
        str(value) for value in rule.get("allowed_voice_states") or []
    }:
        errors.append("OBSERVATION_VOICE_STATE_INVALID")
    return errors


def build_message_observations_v3(
    messages: list[dict[str, Any]],
    visible_voice_hint: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Convert OCR output into the only message/voice observation contract."""
    observations: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            continue
        role = str(message.get("sender_role") or message.get("sender") or "unknown").lower()
        if role == "contact":
            role = "customer"
        if role not in MESSAGE_OBSERVATION_SENDER_ROLES:
            role = "unknown"
        msg_type = str(message.get("type") or message.get("message_type") or "unknown").lower()
        quality_flags = message.get("quality_flags") if isinstance(message.get("quality_flags"), list) else []
        untranscribed = msg_type == "voice" and "untranscribed_voice_placeholder" in quality_flags
        if "non_chat_call_event" in quality_flags:
            row_kind = "call_event"
            voice_state = "not_voice"
        elif msg_type == "voice":
            row_kind = "voice_bubble" if untranscribed else "voice_transcript"
            voice_state = "untranscribed" if untranscribed else "transcribed"
        elif msg_type == "text":
            row_kind = "text_bubble"
            voice_state = "not_voice"
        elif msg_type == "image":
            row_kind = "image_bubble"
            voice_state = "not_voice"
        elif msg_type == "system":
            row_kind = "system_message"
            voice_state = "not_voice"
        else:
            row_kind = "unknown"
            voice_state = "not_voice"
        if row_kind in {"system_message", "call_event", "system_banner"}:
            role = "system"
        elif row_kind == "unknown":
            role = "unknown"
        anchor_key = str(
            message.get("parent_voice_anchor_key")
            or message.get("voice_anchor_structural_key")
            or message.get("voice_anchor_stable_key")
            or message.get("voice_anchor_key")
            or ((message.get("voice_anchor") or {}).get("anchor_stable_key") if isinstance(message.get("voice_anchor"), dict) else "")
            or ""
        )
        avatar = message.get("avatar_alignment") if isinstance(message.get("avatar_alignment"), dict) else {}
        if row_kind == "voice_transcript":
            # A transcript row has no same-row avatar. Any retained avatar
            # metadata belongs to its parent voice bubble and is diagnostic
            # evidence only; the role itself must come from the bound parent.
            role_source = "parent_voice" if anchor_key and role in {"customer", "self"} else "unknown"
        elif str(avatar.get("role") or "") == role and role in {"customer", "self"}:
            role_source = "same_row_avatar"
        elif role == "system":
            role_source = "system"
        else:
            role_source = "unknown"
        observation_id = str(message.get("id") or "").strip() or f"ocr-observation-{index}"
        if observation_id in seen_ids:
            observation_id = f"{observation_id}:{index}"
        seen_ids.add(observation_id)
        observation = {
            "schema_version": C2_OBSERVATION_SCHEMA_VERSION,
            "observation_id": observation_id,
            "row_kind": row_kind,
            "sender_role": role,
            "sender_role_source": role_source,
            "message_type": "voice" if msg_type == "voice" else msg_type,
            "voice_state": voice_state,
            "voice_anchor_key": anchor_key or None,
            "parent_voice_anchor_key": (anchor_key or None) if row_kind == "voice_transcript" else None,
            "content_clean": "" if untranscribed else str(message.get("content") or "").strip(),
            "content_raw": str(message.get("content_raw_ocr") or message.get("content") or ""),
            "bubble_rect": message.get("bubble_rect"),
            "voice_duration": message.get("voice_duration"),
            "voice_duration_text": message.get("voice_duration_text"),
            "image_physical_anchor": message.get("image_physical_anchor"),
            "ocr_confidence": message.get("ocr_confidence"),
            "quality_flags": quality_flags,
            "source_message": message,
        }
        if row_kind == "image_bubble":
            observation["item_state"] = "discovered"
        contract_errors = validate_message_observation_v3(observation)
        if contract_errors:
            observation["contract_errors"] = contract_errors
        observations.append(observation)
    hint = visible_voice_hint if isinstance(visible_voice_hint, dict) else {}
    if hint.get("detected"):
        hint_key = str(hint.get("anchor_stable_key") or hint.get("anchor_key") or "")
        already_seen = any(
            item.get("row_kind") == "voice_bubble"
            and item.get("voice_state") == "untranscribed"
            and (not hint_key or item.get("voice_anchor_key") == hint_key)
            for item in observations
        )
        if not already_seen:
            observation = {
                    "schema_version": C2_OBSERVATION_SCHEMA_VERSION,
                    "observation_id": f"voice-hint:{hint_key or len(observations)}",
                    "row_kind": "voice_bubble",
                    "sender_role": str(hint.get("sender_role") or "unknown"),
                    "sender_role_source": "same_row_avatar",
                    "message_type": "voice",
                    "voice_state": "untranscribed",
                    "voice_anchor_key": hint_key or None,
                    "parent_voice_anchor_key": None,
                    "content_clean": "",
                    "content_raw": "",
                    "bubble_rect": hint.get("bubble_rect"),
                    "voice_duration": None,
                    "voice_duration_text": None,
                    "ocr_confidence": None,
                    "quality_flags": ["visual_voice_hint"],
                    "source_message": {},
                }
            contract_errors = validate_message_observation_v3(observation)
            if contract_errors:
                observation["contract_errors"] = contract_errors
            observations.append(observation)
    return observations


def merge_structural_image_messages(
    screenshot: Image.Image | None,
    ocr_items: list[dict[str, Any]],
    messages: list[dict[str, Any]],
    *,
    target: str,
) -> list[dict[str, Any]]:
    merged = [dict(item) for item in messages if isinstance(item, dict)]
    if screenshot is None:
        return merged
    try:
        from apps.wechat_ai_customer_service.optional_plugins.vision.capture.surface import (
            visual_image_messages_from_current_surface,
        )

        image_messages = visual_image_messages_from_current_surface(
            screenshot,
            ocr_items,
            merged,
            target=target,
            side_filter="all",
            max_images=8,
        )
    except Exception:
        image_messages = []
    def stable_message_signature(message: dict[str, Any]) -> str:
        payload = {
            "sender_role": str(message.get("sender_role") or message.get("sender") or "unknown").strip().lower(),
            "message_type": str(message.get("type") or message.get("message_type") or "unknown").strip().lower(),
            "content": normalize_message_content(
                str(message.get("content") or message.get("content_raw_ocr") or "")
            ),
            "voice_duration": message.get("voice_duration"),
        }
        return "message_semantic_" + hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()[:24]

    stable_by_message_id = {
        str(message.get("id") or message.get("message_id") or "").strip(): stable_message_signature(message)
        for message in merged
        if str(message.get("id") or message.get("message_id") or "").strip()
    }

    def bubble_visual_fingerprint(bounds: Any) -> str:
        rect = message_rect_bounds({"bubble_rect": bounds})
        if not rect:
            return ""
        left, top, right, bottom = (
            max(0, int(rect[0])),
            max(0, int(rect[1])),
            min(screenshot.size[0], int(rect[2])),
            min(screenshot.size[1], int(rect[3])),
        )
        if right <= left or bottom <= top:
            return ""
        crop = screenshot.crop((left, top, right, bottom))
        try:
            resampling = getattr(Image, "Resampling", Image).LANCZOS
            gray = crop.convert("L").resize((9, 8), resampling)
            pixels = list(gray.getdata())
            bits = [
                1 if pixels[row * 9 + column] > pixels[row * 9 + column + 1] else 0
                for row in range(8)
                for column in range(8)
            ]
            value = sum(bit << index for index, bit in enumerate(bits))
            return f"dhash64:{value:016x}"
        finally:
            crop.close()

    image_occurrences: dict[tuple[str, str, str, str], int] = {}
    for image_message in image_messages:
        bounds = image_message.get("bubble_rect") if isinstance(image_message, dict) else None
        avatar_alignment = message_row_avatar_role_details(screenshot, bounds or [], screenshot.size)
        avatar_role = str(avatar_alignment.get("role") or "").strip().lower()
        if avatar_role not in {"customer", "self"}:
            avatar_role = "unknown"
        image_message["sender"] = avatar_role
        image_message["sender_role"] = avatar_role
        image_message["avatar_alignment"] = avatar_alignment
        preceding_id = str(image_message.get("_vision_preceding_text_id") or "").strip()
        following_id = str(image_message.get("_vision_following_text_id") or "").strip()
        preceding = stable_by_message_id.get(preceding_id, "")
        following = stable_by_message_id.get(following_id, "")
        visual_fingerprint = bubble_visual_fingerprint(bounds)
        occurrence_group = (avatar_role, preceding, following, visual_fingerprint)
        occurrence_index = image_occurrences.get(occurrence_group, 0)
        image_occurrences[occurrence_group] = occurrence_index + 1
        image_message["image_physical_anchor"] = {
            "sender_role": avatar_role,
            "preceding_stable_message": preceding,
            "following_stable_message": following,
            "bubble_visual_fingerprint": visual_fingerprint,
            "occurrence_index": occurrence_index,
        }
        visual_seed = json.dumps(
            {
                "target": str(target or "").strip().upper(),
                "sender_role": avatar_role,
                "message_type": "image",
                "occurrence_index": occurrence_index,
                "preceding_stable_message": preceding,
                "following_stable_message": following,
                "bubble_visual_fingerprint": visual_fingerprint,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        canonical_visual_id = "canonical_visual_" + hashlib.sha256(visual_seed.encode("utf-8")).hexdigest()[:24]
        image_message["canonical_visual_id"] = canonical_visual_id
        image_message["id"] = canonical_visual_id
        image_message["message_id"] = canonical_visual_id
    merged.extend(image_messages)

    def message_visual_top(item: dict[str, Any]) -> float:
        rect = item.get("bubble_rect")
        try:
            return float(rect.get("top") if isinstance(rect, dict) else rect[1])
        except (TypeError, ValueError, IndexError):
            return 0.0

    merged.sort(key=lambda item: (message_visual_top(item), str(item.get("id") or "")))
    return merged


# Compatibility alias for downstream integrations that used the original name.
build_c2_observations_v3 = build_message_observations_v3


def has_remaining_voice_transcribe_candidate(
    image: Image.Image,
    ocr_items: list[dict[str, Any]],
    image_size: tuple[int, int],
    *,
    excluded_anchor_keys: set[str] | None = None,
    parsed_messages: list[dict[str, Any]] | None = None,
) -> bool:
    return bool(
        find_unified_untranscribed_voice_observation(
            image,
            ocr_items,
            image_size,
            excluded_anchor_keys=excluded_anchor_keys,
            parsed_messages=parsed_messages,
        )
    )


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
        voice_left = int(float(item.get("left") or 0))
        voice_right = int(float(item.get("right") or 0))
        width, height = image_size
        split_x = session_split_x(width)
        is_self_side_voice = float(item.get("center_x") or 0) > width * 0.62
        if is_self_side_voice:
            left = max(split_x + 24, voice_left - 154)
            right = max(split_x + 34, voice_left - 70)
        else:
            left = max(split_x + 86, voice_right + 70)
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
    menu_wait_ms = bounded_int(
        os.getenv("WECHAT_WIN32_OCR_VOICE_CONTEXT_MENU_WAIT_MS"),
        default=1200,
        minimum=400,
        maximum=4000,
    )
    humanized_action_sleep(max(350, menu_wait_ms - 250), menu_wait_ms + 450)
    # The WeChat context menu is a desktop popup. On right-side/self voice
    # bubbles it can extend outside the WeChat window rectangle, so capture the
    # visible screen and click in screen coordinates instead of window coords.
    menu_screenshot, menu_path = capture_visible_screen(artifact_dir=artifact_dir, label="voice_transcribe_context_menu")
    menu_items = run_ocr(menu_screenshot)
    menu_size = getattr(menu_screenshot, "size", image_size)
    anchor_screen_y = int((right_click or {}).get("screen_y") or 0)
    menu_target = find_voice_transcribe_menu_item_target(menu_items, menu_size, anchor=anchor, anchor_screen_y=anchor_screen_y)
    collapse_target = find_voice_transcribe_menu_collapse_item_target(menu_items, menu_size, anchor=anchor, anchor_screen_y=anchor_screen_y)
    local_radius = max(96.0, min(180.0, float(menu_size[1] if menu_size else 0) * 0.18))
    if menu_target and float(menu_target.get("menu_distance_to_anchor") or 0.0) > local_radius:
        menu_target = None
    if collapse_target and float(collapse_target.get("menu_distance_to_anchor") or 0.0) > local_radius:
        collapse_target = None
    if menu_target and collapse_target:
        menu_distance = float(menu_target.get("menu_distance_to_anchor") or 0.0)
        collapse_distance = float(collapse_target.get("menu_distance_to_anchor") or 0.0)
        if collapse_distance <= menu_distance:
            menu_target = None
        else:
            collapse_target = None
    menu_texts = [
        str(item.get("text") or "")
        for item in menu_items
        if voice_transcribe_button_text_like(str(item.get("text") or "")) or voice_transcribe_collapse_text_like(str(item.get("text") or ""))
    ][:12]
    text_menu_texts = [
        str(item.get("text") or "")
        for item in menu_items
        if text_message_context_menu_text_like(str(item.get("text") or ""))
    ][:12]
    avatar_menu_texts = [
        str(item.get("text") or "")
        for item in menu_items
        if avatar_context_menu_text_like(str(item.get("text") or ""))
    ][:12]
    wrong_text_menu = bool(text_menu_texts) and not menu_target and not collapse_target
    wrong_avatar_menu = bool(avatar_menu_texts) and not menu_target and not collapse_target
    menu_state = (
        "transcribe_available"
        if menu_target
        else (
            "already_transcribed"
            if collapse_target
            else ("avatar_context_menu" if wrong_avatar_menu else ("text_message_context_menu" if wrong_text_menu else "unknown"))
        )
    )
    return {
        "ok": bool(right_click.get("ok") and (menu_target or collapse_target)),
        "right_click": right_click,
        "anchor": anchor,
        "anchor_point": [anchor_x, anchor_y],
        "anchor_jitter": anchor_jitter,
        "menu_wait_ms": menu_wait_ms,
        "menu_screenshot_path": menu_path,
        "menu_capture_mode": "visible_screen",
        "menu_local_radius": local_radius,
        "menu_ocr_items_count": len(menu_items),
        "menu_state": menu_state,
        "menu_texts": menu_texts,
        "wrong_context_menu_texts": text_menu_texts,
        "wrong_avatar_menu_texts": avatar_menu_texts,
        "click_target": menu_target,
        "already_transcribed_target": collapse_target,
        "reason": "menu_target_found"
        if menu_target
        else (
            "already_transcribed_menu_found"
            if collapse_target
            else (
                "wrong_context_menu_avatar"
                if wrong_avatar_menu
                else ("wrong_context_menu_text_message" if wrong_text_menu else "menu_target_not_found")
            )
        ),
    }


def find_voice_transcribe_menu_item_target(
    ocr_items: list[dict[str, Any]],
    image_size: tuple[int, int],
    *,
    anchor: dict[str, Any] | None = None,
    anchor_screen_y: int | None = None,
) -> dict[str, Any] | None:
    width, height = image_size
    anchor_item = (anchor or {}).get("item") if isinstance(anchor, dict) else None
    anchor_y = float(anchor_screen_y or 0) or (float(anchor_item.get("center_y") or 0) if isinstance(anchor_item, dict) else 0.0)
    targets: list[dict[str, Any]] = []
    for item in ocr_items:
        text = str(item.get("text") or "")
        if not voice_transcribe_button_text_like(text):
            continue
        left = max(0, int(float(item.get("left") or 0)) + 3)
        top = max(0, int(float(item.get("top") or 0)) + 2)
        right = min(width, int(float(item.get("right") or 0)) - 3)
        bottom = min(height, int(float(item.get("bottom") or 0)) - 2)
        if right <= left or bottom <= top:
            continue
        target = voice_transcribe_click_target_from_bounds(
            source="ocr_context_menu_transcribe_item",
            label="OCR matched WeChat context-menu voice-to-text item",
            bounds=[left, top, right, bottom],
            item=item,
        )
        target["coordinate_space"] = "screen"
        target["menu_distance_to_anchor"] = abs(float(item.get("center_y") or 0) - anchor_y) if anchor_y else 0.0
        targets.append(target)
    if not targets:
        return None
    if anchor_y:
        return min(targets, key=lambda target: float(target.get("menu_distance_to_anchor") or 0.0))
    return max(targets, key=lambda target: float((target.get("item") or {}).get("center_y") or 0))


def find_voice_transcribe_menu_collapse_item_target(
    ocr_items: list[dict[str, Any]],
    image_size: tuple[int, int],
    *,
    anchor: dict[str, Any] | None = None,
    anchor_screen_y: int | None = None,
) -> dict[str, Any] | None:
    width, height = image_size
    anchor_item = (anchor or {}).get("item") if isinstance(anchor, dict) else None
    anchor_y = float(anchor_screen_y or 0) or (float(anchor_item.get("center_y") or 0) if isinstance(anchor_item, dict) else 0.0)
    targets: list[dict[str, Any]] = []
    for item in ocr_items:
        text = str(item.get("text") or "")
        if not voice_transcribe_collapse_text_like(text):
            continue
        left = max(0, int(float(item.get("left") or 0)) + 3)
        top = max(0, int(float(item.get("top") or 0)) + 2)
        right = min(width, int(float(item.get("right") or 0)) - 3)
        bottom = min(height, int(float(item.get("bottom") or 0)) - 2)
        if right <= left or bottom <= top:
            continue
        target = voice_transcribe_click_target_from_bounds(
            source="ocr_context_menu_collapse_text_item",
            label="OCR matched WeChat context-menu collapse-transcript item",
            bounds=[left, top, right, bottom],
            item=item,
        )
        target["coordinate_space"] = "screen"
        target["menu_distance_to_anchor"] = abs(float(item.get("center_y") or 0) - anchor_y) if anchor_y else 0.0
        targets.append(target)
    if not targets:
        return None
    if anchor_y:
        return min(targets, key=lambda target: float(target.get("menu_distance_to_anchor") or 0.0))
    return max(targets, key=lambda target: float((target.get("item") or {}).get("center_y") or 0))


def dismiss_voice_transcribe_context_menu(
    hwnd: int,
    *,
    artifact_dir: str | None = None,
    label: str = "voice_transcribe_context_menu_dismissed",
    menu_bounds: list[int] | None = None,
) -> dict[str, Any]:
    try:
        activate_window(hwnd)
        geometry = get_window_geometry(hwnd)
        before_shot, before_path = capture_wechat(hwnd, artifact_dir=artifact_dir, label=f"{label}_before")
        before_items = run_ocr(before_shot)
        safe_target = safe_window_header_blank_click_target(before_items, before_shot.size, geometry=geometry)
        if not safe_target:
            return {
                "ok": False,
                "method": "fresh_header_blank_click",
                "reason": "safe_header_blank_target_not_found",
                "screenshot_path": before_path,
            }
        click_x, click_y = safe_target["point"]
        click_result = human_window_image_click_in_bounds(
            hwnd,
            click_x,
            click_y,
            bounds=safe_target["bounds"],
            action_name="voice_transcribe_context_menu_title_bar_dismiss_click",
        )
        humanized_action_sleep(120, 260)
        window_probe = probe_wechat_windows()
        window_visible = probe_has_usable_visible_main_window(window_probe) if window_probe.get("visible_main_windows") else False
        result: dict[str, Any] = {
            "ok": bool(click_result.get("ok")) and bool(window_visible),
            "method": "fresh_header_blank_click",
            "verified": False,
            "click": click_result,
            "safe_target": safe_target,
            "window_visible_after_dismiss": bool(window_visible),
            "window_probe_after_dismiss": window_probe,
        }
        if not window_visible:
            result["reason"] = "window_not_visible_after_dismiss"
            return result
        try:
            screenshot, screenshot_path = capture_visible_screen(artifact_dir=artifact_dir, label=label)
            items = run_ocr(screenshot)
            visible_menu_texts = voice_transcribe_menu_texts_from_items(items, menu_bounds=menu_bounds)
            visible_panel_texts = chat_info_panel_texts_from_items(items)
            result.update({
                "verified": True,
                "screenshot_path": screenshot_path,
                "ocr_items_count": len(items),
                "visible_menu_texts": visible_menu_texts,
                "visible_panel_texts": visible_panel_texts,
                "menu_bounds": menu_bounds or [],
                "ok": bool(click_result.get("ok")) and not bool(visible_menu_texts) and not bool(visible_panel_texts),
                "reason": "menu_closed" if not visible_menu_texts and not visible_panel_texts else "menu_or_panel_still_visible",
            })
        except Exception as verify_exc:
            result.update({"verify_error": repr(verify_exc), "reason": "dismiss_sent_without_verification"})
        return result
    except Exception as exc:
        return {"ok": False, "method": "safe_chat_surface_click", "error": repr(exc)}


def safe_window_header_blank_click_target(
    ocr_items: list[dict[str, Any]],
    image_size: tuple[int, int],
    *,
    geometry: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Find a fresh blank title-bar segment away from chat content and controls."""
    width, height = image_size
    if width < 700 or height < 260:
        return None
    active_geometry = geometry if isinstance(geometry, dict) else {"width": width, "height": height}
    split_x = session_split_x(int(active_geometry.get("width") or width))
    zone_left = max(split_x + 28, int(width * 0.42))
    zone_right = min(width - 230, int(width * 0.76))
    zone_top = 8
    zone_bottom = min(42, max(26, chat_header_cutoff_y(height) - 46))
    if zone_right - zone_left < 56 or zone_bottom - zone_top < 14:
        return None
    blocked: list[tuple[int, int]] = []
    for item in ocr_items or []:
        top = int(float(item.get("top") or 0))
        bottom = int(float(item.get("bottom") or 0))
        if bottom < zone_top - 8 or top > zone_bottom + 8:
            continue
        left = max(zone_left, int(float(item.get("left") or 0)) - 18)
        right = min(zone_right, int(float(item.get("right") or 0)) + 18)
        if right > left:
            blocked.append((left, right))
    segments = [(zone_left, zone_right)]
    for blocked_left, blocked_right in sorted(blocked):
        next_segments: list[tuple[int, int]] = []
        for left, right in segments:
            if blocked_right <= left or blocked_left >= right:
                next_segments.append((left, right))
                continue
            if blocked_left - left >= 48:
                next_segments.append((left, blocked_left))
            if right - blocked_right >= 48:
                next_segments.append((blocked_right, right))
        segments = next_segments
    if not segments:
        return None
    left, right = max(segments, key=lambda segment: segment[1] - segment[0])
    if right - left < 48:
        return None
    bounds = [left + 6, zone_top, right - 6, zone_bottom]
    return {
        "point": [(bounds[0] + bounds[2]) // 2, (bounds[1] + bounds[3]) // 2],
        "bounds": bounds,
        "source": "fresh_ocr_header_blank_segment",
        "blocked_intervals": [list(interval) for interval in blocked],
    }


def chat_info_panel_text_like(text: str) -> bool:
    compact = voice_transcribe_compact_text(text)
    return bool(compact) and any(voice_transcribe_compact_text(token) in compact for token in CHAT_INFO_PANEL_TEXT_TOKENS)


def chat_info_panel_texts_from_items(ocr_items: list[dict[str, Any]]) -> list[str]:
    return [
        str(item.get("text") or "")
        for item in ocr_items or []
        if chat_info_panel_text_like(str(item.get("text") or ""))
    ][:12]


def ocr_item_center_in_bounds(item: dict[str, Any], bounds: list[int] | None, *, padding: int = 0) -> bool:
    if not bounds or len(bounds) < 4:
        return True
    center_x = float(item.get("center_x") or 0)
    center_y = float(item.get("center_y") or 0)
    return (
        float(bounds[0]) - padding <= center_x <= float(bounds[2]) + padding
        and float(bounds[1]) - padding <= center_y <= float(bounds[3]) + padding
    )


def voice_transcribe_menu_texts_from_items(
    ocr_items: list[dict[str, Any]],
    *,
    menu_bounds: list[int] | None = None,
) -> list[str]:
    return [
        str(item.get("text") or "")
        for item in ocr_items or []
        if ocr_item_center_in_bounds(item, menu_bounds, padding=8)
        and (
            voice_transcribe_button_text_like(str(item.get("text") or ""))
            or voice_transcribe_collapse_text_like(str(item.get("text") or ""))
        )
    ][:12]


def verify_voice_transcribe_context_menu_closed(
    *,
    artifact_dir: str | None = None,
    label: str = "voice_transcribe_context_menu_after_click",
    menu_bounds: list[int] | None = None,
) -> dict[str, Any]:
    try:
        screenshot, screenshot_path = capture_visible_screen(artifact_dir=artifact_dir, label=label)
        items = run_ocr(screenshot)
        visible_menu_texts = voice_transcribe_menu_texts_from_items(items, menu_bounds=menu_bounds)
        visible_panel_texts = chat_info_panel_texts_from_items(items)
        return {
            "ok": not bool(visible_menu_texts) and not bool(visible_panel_texts),
            "screenshot_path": screenshot_path,
            "ocr_items_count": len(items),
            "visible_menu_texts": visible_menu_texts,
            "visible_panel_texts": visible_panel_texts,
            "reason": "menu_closed" if not visible_menu_texts and not visible_panel_texts else "menu_or_panel_still_visible",
        }
    except Exception as exc:
        return {"ok": False, "reason": "menu_close_verification_failed", "error": repr(exc)}


def click_voice_transcribe_context_menu_target(
    hwnd: int,
    menu_target: dict[str, Any],
    *,
    geometry: dict[str, Any],
    artifact_dir: str | None = None,
    attempt_index: int = 1,
) -> dict[str, Any]:
    menu_bounds = [int(value) for value in menu_target.get("click_bounds") or []]
    if len(menu_bounds) < 4:
        return {"ok": False, "reason": "context_menu_click_bounds_missing", "click_target": menu_target}

    click_attempts: list[dict[str, Any]] = []

    def click_once(click_x: int, click_y: int, *, retry_index: int, jitter_meta: dict[str, Any]) -> dict[str, Any]:
        if str(menu_target.get("coordinate_space") or "") == "screen":
            click = human_screen_click_in_bounds(
                click_x,
                click_y,
                bounds=menu_bounds,
                action_name="voice_transcribe_context_menu_click",
            )
        else:
            click = human_window_image_click_in_bounds(
                hwnd,
                click_x,
                click_y,
                bounds=menu_bounds,
                action_name="voice_transcribe_context_menu_click",
            )
        humanized_action_sleep(260, 620)
        verification = verify_voice_transcribe_context_menu_closed(
            artifact_dir=artifact_dir,
            label=f"voice_transcribe_context_menu_after_click_{attempt_index}_{retry_index}",
            menu_bounds=menu_bounds,
        )
        return {
            "retry_index": retry_index,
            "click": click,
            "planned_click_point": [click_x, click_y],
            "click_jitter": jitter_meta,
            "menu_close_verification": verification,
        }

    menu_item = menu_target.get("item") if isinstance(menu_target.get("item"), dict) else {}
    menu_x = int(float(menu_item.get("center_x") or (menu_bounds[0] + menu_bounds[2]) / 2))
    menu_y = int(float(menu_item.get("center_y") or (menu_bounds[1] + menu_bounds[3]) / 2))
    menu_jitter = {
        "enabled": False,
        "source": "ocr_context_menu_text_center",
        "bounds": menu_bounds,
        "reason": "click_exact_ocr_menu_text_center",
    }
    first_attempt = click_once(menu_x, menu_y, retry_index=1, jitter_meta=menu_jitter)
    click_attempts.append(first_attempt)
    if first_attempt.get("click", {}).get("ok") and first_attempt.get("menu_close_verification", {}).get("ok"):
        return {
            **first_attempt["click"],
            "ok": True,
            "reason": "context_menu_click_closed_menu",
            "planned_click_point": first_attempt["planned_click_point"],
            "click_jitter": first_attempt["click_jitter"],
            "menu_close_verification": first_attempt["menu_close_verification"],
            "click_attempts": click_attempts,
        }

    center_x = int((menu_bounds[0] + menu_bounds[2]) / 2)
    center_y = int((menu_bounds[1] + menu_bounds[3]) / 2)
    center_jitter = {"enabled": False, "source": str(menu_target.get("source") or ""), "bounds": menu_bounds, "reason": "retry_center_point"}
    second_attempt = click_once(center_x, center_y, retry_index=2, jitter_meta=center_jitter)
    click_attempts.append(second_attempt)
    if second_attempt.get("click", {}).get("ok") and second_attempt.get("menu_close_verification", {}).get("ok"):
        return {
            **second_attempt["click"],
            "ok": True,
            "reason": "context_menu_retry_closed_menu",
            "planned_click_point": second_attempt["planned_click_point"],
            "click_jitter": second_attempt["click_jitter"],
            "menu_close_verification": second_attempt["menu_close_verification"],
            "click_attempts": click_attempts,
        }

    dismissal = dismiss_voice_transcribe_context_menu(
        hwnd,
        artifact_dir=artifact_dir,
        label=f"voice_transcribe_context_menu_dismissed_after_failed_click_{attempt_index}",
        menu_bounds=menu_bounds,
    )
    last_attempt = click_attempts[-1]
    return {
        **(last_attempt.get("click") if isinstance(last_attempt.get("click"), dict) else {}),
        "ok": False,
        "reason": "context_menu_click_did_not_close_menu",
        "planned_click_point": last_attempt.get("planned_click_point") or [],
        "click_jitter": last_attempt.get("click_jitter") or {},
        "menu_close_verification": last_attempt.get("menu_close_verification") or {},
        "click_attempts": click_attempts,
        "menu_dismissal": dismissal,
    }


def find_voice_transcribe_targets(
    ocr_items: list[dict[str, Any]],
    image_size: tuple[int, int],
    *,
    allow_inferred: bool = True,
) -> list[dict[str, Any]]:
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
        return sorted(direct_targets, key=lambda target: float((target.get("item") or {}).get("center_y") or 0))

    if not allow_inferred:
        return []

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
        voice_left = int(float(item.get("left") or 0))
        voice_right = int(float(item.get("right") or 0))
        split_x = session_split_x(width)
        is_self_side_voice = float(item.get("center_x") or 0) > width * 0.62
        if is_self_side_voice:
            left = max(split_x + 24, voice_left - 154)
            right = max(split_x + 34, voice_left - 70)
        else:
            left = max(split_x + 86, voice_right + 70)
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
    return sorted(inferred_targets, key=lambda target: float((target.get("item") or {}).get("center_y") or 0))


def find_voice_transcribe_target(
    ocr_items: list[dict[str, Any]],
    image_size: tuple[int, int],
    *,
    allow_inferred: bool = True,
) -> dict[str, Any] | None:
    targets = find_voice_transcribe_targets(ocr_items, image_size, allow_inferred=allow_inferred)
    return targets[-1] if targets else None


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
        compact_lines = [line.strip() for line in str(content or "").splitlines() if line.strip()]
        if len(compact_lines) >= 2 and voice_duration_text_like(compact_lines[0]):
            return "\n".join(compact_lines[1:]).strip(), True
        return content, False
    lines = [line.strip() for line in str(content or "").splitlines() if line.strip()]
    if len(lines) < 2:
        return content, False
    return "\n".join(lines[1:]).strip(), True


def message_group_voice_duration_text(group: list[dict[str, Any]]) -> str:
    for item in group or []:
        text = normalize_ocr_text(item.get("text"))
        if voice_duration_item_like(item):
            return text
    return ""


def message_group_is_untranscribed_voice_placeholder(group: list[dict[str, Any]]) -> bool:
    if not group:
        return False
    has_duration = any(voice_duration_item_like(item) for item in group)
    if not has_duration:
        return False
    non_duration = [
        normalize_ocr_text(item.get("text"))
        for item in group
        if not voice_duration_item_like(item) and normalize_ocr_text(item.get("text"))
    ]
    if not non_duration:
        return True
    return all(voice_transcribe_button_text_like(text) for text in non_duration)


def voice_duration_seconds_from_text(text: str) -> int | None:
    match = re.search(r"(\d{1,3})", voice_transcribe_compact_text(text))
    if not match:
        return None
    value = int(match.group(1))
    if value <= 0 or value > 300:
        return None
    return value


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


def avatar_lane_visual_score(
    screenshot: Any | None,
    *,
    bounds: list[float],
    role: str,
    image_size: tuple[int, int],
) -> dict[str, Any]:
    if screenshot is None or len(bounds) < 4:
        return {"present": False, "score": 0.0, "reason": "screenshot_unavailable"}
    try:
        image = screenshot.convert("RGB")
    except Exception:
        return {"present": False, "score": 0.0, "reason": "screenshot_unavailable"}
    width, height = image_size
    if width <= 0 or height <= 0:
        return {"present": False, "score": 0.0, "reason": "image_size_invalid"}
    split_x = session_split_x(width)
    bubble_left, bubble_top, bubble_right, bubble_bottom = [float(value) for value in bounds[:4]]
    if role == "customer":
        lane_left = max(split_x + 4, int(round(bubble_left - 140.0)))
        lane_right = min(int(round(bubble_left - 4.0)), split_x + 150)
    else:
        lane_left = max(int(round(bubble_right + 4.0)), width - 150, split_x + 1)
        lane_right = width - 6
    row_top, row_bottom = bubble_top, bubble_bottom
    row_center = min((row_top + row_bottom) / 2.0, row_top + 24.0)
    crop_top = max(chat_header_cutoff_y(height), int(round(row_center - 24.0)))
    crop_bottom = min(height - DEFAULT_MESSAGE_BOTTOM_EXCLUDE_PX, crop_top + 48)
    lane_left = max(0, int(lane_left))
    lane_right = min(width, int(lane_right))
    if lane_right - lane_left < 20 or crop_bottom - crop_top < 20:
        return {"present": False, "score": 0.0, "reason": "avatar_lane_empty"}
    crop = image.crop((lane_left, crop_top, lane_right, crop_bottom))
    stat = ImageStat.Stat(crop)
    color_stddev = sum(float(value) for value in stat.stddev[:3]) / 3.0
    pixels = crop.load()
    border_pixels: list[tuple[int, int, int]] = []
    for y in range(crop.height):
        for x in range(crop.width):
            if x < 4 or x >= crop.width - 4 or y < 3 or y >= crop.height - 3:
                border_pixels.append(pixels[x, y])
    background = tuple(
        sorted(int(pixel[channel]) for pixel in border_pixels)[len(border_pixels) // 2]
        for channel in range(3)
    ) if border_pixels else (247, 247, 247)
    foreground_points: list[tuple[int, int]] = []
    edge_hits = 0
    edge_checks = 0
    for y in range(crop.height):
        for x in range(crop.width):
            current = pixels[x, y]
            if sum(abs(int(current[index]) - int(background[index])) for index in range(3)) / 3.0 >= 18.0:
                foreground_points.append((x, y))
            if x + 1 < crop.width:
                adjacent = pixels[x + 1, y]
                edge_hits += int(sum(abs(int(current[index]) - int(adjacent[index])) for index in range(3)) / 3.0 >= 18.0)
                edge_checks += 1
            if y + 1 < crop.height:
                adjacent = pixels[x, y + 1]
                edge_hits += int(sum(abs(int(current[index]) - int(adjacent[index])) for index in range(3)) / 3.0 >= 18.0)
                edge_checks += 1
    edge_ratio = edge_hits / max(1, edge_checks)
    if foreground_points:
        foreground_left = min(point[0] for point in foreground_points)
        foreground_top = min(point[1] for point in foreground_points)
        foreground_right = max(point[0] for point in foreground_points)
        foreground_bottom = max(point[1] for point in foreground_points)
        foreground_width = foreground_right - foreground_left + 1
        foreground_height = foreground_bottom - foreground_top + 1
    else:
        foreground_left = foreground_top = foreground_right = foreground_bottom = 0
        foreground_width = 0
        foreground_height = 0
    foreground_ratio = len(foreground_points) / max(1, crop.width * crop.height)
    avatar_sized_component = bool(
        30 <= foreground_width <= crop.width
        and 30 <= foreground_height <= 48
        and foreground_ratio >= 0.16
    )
    component_bounds = [
        lane_left + foreground_left,
        crop_top + foreground_top,
        lane_left + foreground_right,
        crop_top + foreground_bottom,
    ]
    component_center_y = (component_bounds[1] + component_bounds[3]) / 2.0
    bubble_center_y = min((bubble_top + bubble_bottom) / 2.0, bubble_top + 24.0)
    horizontal_gap = (
        bubble_left - component_bounds[2]
        if role == "customer"
        else component_bounds[0] - bubble_right
    )
    max_gap = 150.0 if role == "customer" else 320.0
    relative_alignment = bool(-20.0 <= horizontal_gap <= max_gap and abs(component_center_y - bubble_center_y) <= 30.0)
    score = color_stddev + edge_ratio * 180.0
    present = bool(
        avatar_sized_component
        and relative_alignment
        and color_stddev >= 14.0
        and edge_ratio >= 0.018
        and score >= 22.0
    )
    return {
        "present": present,
        "score": round(score, 4),
        "color_stddev": round(color_stddev, 4),
        "edge_ratio": round(edge_ratio, 6),
        "foreground_ratio": round(foreground_ratio, 6),
        "foreground_bounds_size": [foreground_width, foreground_height],
        "foreground_bounds": component_bounds,
        "horizontal_gap": round(horizontal_gap, 2),
        "relative_alignment": relative_alignment,
        "avatar_sized_component": avatar_sized_component,
        "bounds": [lane_left, crop_top, lane_right, crop_bottom],
        "position_source": "bubble_relative_avatar_adjacency",
        "reason": "avatar_relative_structure" if present else "avatar_relative_structure_not_found",
    }


def message_row_avatar_role_details(
    screenshot: Any | None,
    bounds: list[float],
    image_size: tuple[int, int],
) -> dict[str, Any]:
    customer = avatar_lane_visual_score(screenshot, bounds=bounds, role="customer", image_size=image_size)
    self_side = avatar_lane_visual_score(screenshot, bounds=bounds, role="self", image_size=image_size)
    customer_present = bool(customer.get("present"))
    self_present = bool(self_side.get("present"))
    role = "customer" if customer_present and not self_present else ("self" if self_present and not customer_present else "")
    return {
        "role": role,
        "source": "wechat_avatar_row_structure_v2" if role else "",
        "customer": customer,
        "self": self_side,
        "ambiguous": bool(customer_present and self_present),
    }


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
        parsed_messages = parse_messages_from_ocr(ocr_items, screenshot.size, target=target, screenshot=screenshot)
        snapshots.append(
            {
                "label": label,
                "screenshot_path": path,
                "screenshot": screenshot,
                "ocr_items": ocr_items,
                "messages": parsed_messages,
                "visible_untranscribed_voice": visible_untranscribed_voice_hint(
                    screenshot,
                    ocr_items,
                    screenshot.size,
                    parsed_messages=parsed_messages,
                ),
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
        parsed_messages = parse_messages_from_ocr(ocr_items, screenshot.size, target=target, screenshot=screenshot)
        snapshots.append(
            {
                "label": label,
                "screenshot_path": path,
                "ocr_items": ocr_items,
                "messages": parsed_messages,
                "visible_untranscribed_voice": visible_untranscribed_voice_hint(
                    screenshot,
                    ocr_items,
                    screenshot.size,
                    parsed_messages=parsed_messages,
                ),
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
    # Repeated long messages are just as legitimate as repeated short replies.
    # The per-snapshot occurrence number keeps both bubbles while still aligning
    # the same occurrence across history snapshots.
    return True


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
    parts = [
        str(message.get("sender") or "").strip(),
        str(message.get("type") or "").strip(),
        content,
    ]
    anchor_key = str(
        message.get("voice_anchor_stable_key")
        or message.get("voice_anchor_key")
        or ""
    ).strip()
    if anchor_key:
        parts.append(anchor_key)
    return "\x1f".join(parts)


def sidecar_new_message_occurrences(
    after_messages: list[dict[str, Any]],
    baseline_messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return only surplus occurrences visible after an action."""
    before_key_counts: dict[str, int] = {}
    for message in baseline_messages:
        key = sidecar_message_content_key(message)
        if key:
            before_key_counts[key] = before_key_counts.get(key, 0) + 1
    new_items: list[dict[str, Any]] = []
    for message in after_messages:
        key = sidecar_message_content_key(message)
        remaining = before_key_counts.get(key, 0)
        if key and remaining > 0:
            before_key_counts[key] = remaining - 1
            continue
        new_items.append(message)
    return new_items


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


def click_add_contact_entry_from_search_result(hwnd: int, output_dir: Path, *, result_shot: Image.Image, result_path: str, result_items: list[dict[str, Any]], query: str, verify_message: str = '', remark_name: str = '', remark_code: str = '', action_journal_path: str = '') -> dict[str, Any]:
    win32_ocr_add_friend_windows.bind_sidecar_ops(sys.modules[__name__])
    return win32_ocr_add_friend_windows.click_add_contact_entry_from_search_result(hwnd, output_dir, result_shot=result_shot, result_path=result_path, result_items=result_items, query=query, verify_message=verify_message, remark_name=remark_name, remark_code=remark_code, action_journal_path=action_journal_path)


def add_friend_invite_form_targets(image_size: tuple[int, int], ocr_items: list[dict[str, Any]] | None = None) -> dict[str, dict[str, Any]]:
    return win32_ocr_add_friend_windows.add_friend_invite_form_targets(image_size, ocr_items)


def paste_invite_form_text(hwnd: int, target: dict[str, Any], text: str, *, action_name: str) -> dict[str, Any]:
    win32_ocr_add_friend_windows.bind_sidecar_ops(sys.modules[__name__])
    return win32_ocr_add_friend_windows.paste_invite_form_text(hwnd, target, text, action_name=action_name)


def fill_add_friend_invite_form_and_confirm(hwnd: int, output_dir: Path, *, verify_message: str, remark_name: str, remark_code: str, action_journal_path: str = '') -> dict[str, Any]:
    win32_ocr_add_friend_windows.bind_sidecar_ops(sys.modules[__name__])
    return win32_ocr_add_friend_windows.fill_add_friend_invite_form_and_confirm(hwnd, output_dir, verify_message=verify_message, remark_name=remark_name, remark_code=remark_code, action_journal_path=action_journal_path)


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


def input_add_friend_query_and_search(hwnd: int, output_dir: Path, *, query: str, verify_message: str = '', remark_name: str = '', remark_code: str = '', action_journal_path: str = '') -> dict[str, Any]:
    win32_ocr_add_friend_windows.bind_sidecar_ops(sys.modules[__name__])
    return win32_ocr_add_friend_windows.input_add_friend_query_and_search(hwnd, output_dir, query=query, verify_message=verify_message, remark_name=remark_name, remark_code=remark_code, action_journal_path=action_journal_path)


def write_add_friend_entry_click_review(output_dir: Path, payload: dict[str, Any]) -> str:
    win32_ocr_add_friend_windows.bind_sidecar_ops(sys.modules[__name__])
    return win32_ocr_add_friend_windows.write_add_friend_entry_click_review(output_dir, payload)


def add_friend_entry_click_plan_payload(hwnd: int, probe: dict[str, Any], *, route: str = ADD_FRIEND_MAIN_ROUTE, phone: str = '', wechat: str = '', verify_message: str = '', remark_name: str = '', remark_code: str = '', artifact_dir: str | None = None, calibration_only: bool = False, action_journal_path: str = '') -> dict[str, Any]:
    win32_ocr_add_friend_windows.bind_sidecar_ops(sys.modules[__name__])
    return win32_ocr_add_friend_windows.add_friend_entry_click_plan_payload(hwnd, probe, route=route, phone=phone, wechat=wechat, verify_message=verify_message, remark_name=remark_name, remark_code=remark_code, artifact_dir=artifact_dir, calibration_only=calibration_only, action_journal_path=action_journal_path)


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


def write_action_phase_journal(
    path: str,
    phase: str,
    *,
    physical_anchor_keys: list[str] | None = None,
    business_state: str | None = None,
    business_result_confirmed: bool | None = None,
    error_code: str | None = None,
    terminal_payload: dict[str, Any] | None = None,
) -> None:
    raw_path = str(path or "").strip()
    if not raw_path:
        raise ValueError("ACTION_JOURNAL_PATH_MISSING")
    target = Path(raw_path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_suffix(f"{target.suffix}.tmp-{os.getpid()}")
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError("ACTION_JOURNAL_NOT_INITIALIZED") from exc
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise ValueError("ACTION_JOURNAL_INVALID") from exc
    if not isinstance(payload, dict):
        raise ValueError("ACTION_JOURNAL_INVALID")
    phase_rank = {
        value: index for index, value in enumerate(C2_ACTION_PHASES)
    }
    requested_phase = str(phase or "not_attempted").strip()
    if requested_phase not in phase_rank:
        raise ValueError("ACTION_JOURNAL_PHASE_INVALID")
    items = (
        payload.get("items")
        if isinstance(payload.get("items"), dict)
        else {}
    )
    if not items:
        raise ValueError("ACTION_JOURNAL_ITEMS_MISSING")
    anchor_keys = {
        str(value).strip()
        for value in (physical_anchor_keys or [])
        if str(value).strip()
    }
    selected_source_key = ""
    for source_key, item in items.items():
        if not isinstance(item, dict):
            continue
        item_anchors = {
            str(value).strip()
            for value in (item.get("physical_anchor_keys") or [])
            if str(value).strip()
        }
        if anchor_keys and item_anchors & anchor_keys:
            selected_source_key = str(source_key)
            break
    if not selected_source_key and len(items) == 1:
        selected_source_key = str(next(iter(items)))
    if items and not selected_source_key:
        raise ValueError("ACTION_JOURNAL_ITEM_NOT_FOUND")
    updated_at = datetime.now(timezone.utc).isoformat()
    if selected_source_key:
        item = dict(items.get(selected_source_key) or {})
        current_phase = str(
            item.get("action_phase") or "not_attempted"
        ).strip()
        if phase_rank[requested_phase] < phase_rank.get(current_phase, 0):
            requested_phase = current_phase
        item["action_phase"] = requested_phase
        if business_state is not None:
            item["business_state"] = (
                str(business_state or "").strip() or None
            )
        if business_result_confirmed is not None:
            item["business_result_confirmed"] = bool(
                business_result_confirmed
            )
        if error_code is not None:
            item["error_code"] = str(error_code or "").strip() or None
        if terminal_payload is not None:
            item["terminal_payload"] = terminal_payload
        item["updated_at"] = updated_at
        items[selected_source_key] = item
        payload["items"] = items
    current_top_phase = str(
        payload.get("action_phase") or "not_attempted"
    ).strip()
    if phase_rank[requested_phase] < phase_rank.get(current_top_phase, 0):
        requested_phase = current_top_phase
    payload["action_phase"] = requested_phase
    payload["updated_at"] = updated_at
    payload["updated_at_unix_ms"] = int(time.time() * 1000)
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
    )
    with temp.open("w", encoding="utf-8") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, target)


def send_payload(
    hwnd: int,
    probe: dict[str, Any],
    *,
    target: str,
    text: str,
    exact: bool,
    skip_send_rate_guard: bool = False,
    artifact_dir: str | None = None,
    expected_context_guard: dict[str, Any] | None = None,
    validated_guard: dict[str, Any] | None = None,
    action_journal_path: str = "",
) -> dict[str, Any]:
    timing: dict[str, Any] = {}
    ocr_trace_token = _ocr_trace_start()
    send_payload_started = _sidecar_timing_start(timing, "send_payload")

    def finish(payload: dict[str, Any]) -> dict[str, Any]:
        _sidecar_timing_finish(timing, "send_payload", send_payload_started)
        _sidecar_timing_merge_ocr_trace(timing, "send_payload", _ocr_trace_finish(ocr_trace_token))
        payload["timing"] = dict(timing)
        send_result = payload.get("send_result")
        nested_send_result = send_result if isinstance(send_result, dict) else {}
        physical_send_triggered = (
            payload.get("physical_send_triggered") is True
            or nested_send_result.get("physical_send_triggered") is True
        )
        send_confirmed = bool(
            nested_send_result.get("confirmed") is True
            and nested_send_result.get("result") == "sent"
        )
        action_phase = (
            "confirmed"
            if send_confirmed
            else "trigger_attempted"
            if physical_send_triggered
            else "not_attempted"
        )
        payload["action_phase"] = action_phase
        if action_phase == "confirmed" and action_journal_path:
            write_action_phase_journal(
                action_journal_path,
                "confirmed",
            )
        if isinstance(send_result, dict):
            send_result["action_phase"] = action_phase
            existing = send_result.get("timing")
            send_result_timing = dict(timing)
            if isinstance(existing, dict):
                send_result_timing.update(existing)
            send_result["timing"] = send_result_timing
        return payload

    final_send_text = sendinput_safe_text(text)
    if not final_send_text or final_send_text != str(text or ""):
        return finish({
            "ok": False,
            "online": True,
            "adapter": "win32_ocr",
            "state": "send_text_contract_invalid",
            "error_code": "SEND_TEXT_NOT_CANONICAL",
            "target": target,
            "error": "Worker must pass the exact canonical final send text without hidden normalization.",
        })

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
    points = {
        "ok": True,
        "source": "uia_observed_controls",
        "geometry": geometry,
    }
    input_region_seed = consume_input_region_precheck_ocr_seed(
        hwnd=hwnd,
        target=target,
        exact=exact,
        geometry=geometry,
    )
    timing["input_region_precheck_seed_reused"] = bool(input_region_seed)
    if isinstance(input_region_seed, dict):
        timing["input_region_precheck_seed_age_seconds"] = input_region_seed.get("age_seconds")
    baseline_started = _sidecar_timing_start(timing, "send_baseline_snapshot")
    try:
        baseline_snapshot = capture_send_fact_snapshot(
            hwnd,
            target=target,
            text=final_send_text,
            exact=exact,
            artifact_dir=artifact_dir,
            label="send_baseline",
        )
    except Exception as exc:
        _sidecar_timing_finish(timing, "send_baseline_snapshot", baseline_started)
        return finish({
            "ok": False,
            "online": True,
            "adapter": "win32_ocr",
            "state": "send_baseline_unavailable",
            "error_code": "SEND_BASELINE_UNAVAILABLE",
            "target": target,
            "guard": validation,
            "error": repr(exc),
        })
    _sidecar_timing_finish(timing, "send_baseline_snapshot", baseline_started)
    if not baseline_snapshot.get("ok"):
        return finish({
            "ok": False,
            "online": True,
            "adapter": "win32_ocr",
            "state": "send_baseline_target_unconfirmed",
            "error_code": "SEND_TARGET_NOT_CONFIRMED",
            "target": target,
            "guard": baseline_snapshot.get("validation"),
            "send_baseline": baseline_snapshot,
            "error": "The current chat was not strictly confirmed in the baseline send frame.",
        })
    baseline_context_validation = validate_send_context_guard(
        expected_context_guard,
        baseline_snapshot.get("send_context_guard"),
    )
    if not baseline_context_validation.get("ok"):
        return finish({
            "ok": False,
            "online": True,
            "adapter": "win32_ocr",
            "state": "send_context_changed_before_input",
            "error_code": str(
                baseline_context_validation.get("error_code")
                or "C3_CONTEXT_CHANGED_BEFORE_SEND"
            ),
            "target": target,
            "guard": baseline_snapshot.get("validation"),
            "context_validation": baseline_context_validation,
            "send_baseline": baseline_snapshot,
            "error": "The visible message sequence changed after the final C2 refresh.",
        })
    input_region_seed = {
        "input_region": dict(baseline_snapshot.get("input_region") or {}),
        "age_seconds": 0.0,
        "source": "send_baseline",
    }

    requested_send_mode = str(os.getenv("WECHAT_WIN32_OCR_SEND_MODE") or DEFAULT_SEND_MODE).strip().lower()
    settings = adapt_humanized_input_settings(humanized_input_settings(), text)
    settings["enabled"] = True
    settings["method"] = "sendinput_unicode"
    # The formal C2-C3 path has one rule: UIA locates the real controls, while
    # Win32 SendInput and a physical click perform the customer-visible action.
    send_mode = "uia_observed_controls"
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
    uia_send_started = _sidecar_timing_start(timing, "uia_observed_send")
    def pre_click_context_check() -> dict[str, Any]:
        try:
            snapshot = capture_send_fact_snapshot(
                hwnd,
                target=target,
                text=final_send_text,
                exact=exact,
                artifact_dir=artifact_dir,
                label="send_pre_click_context",
            )
        except Exception as exc:
            return {
                "ok": False,
                "reason": "pre_click_context_snapshot_failed",
                "error_code": "C3_SEND_PRE_CLICK_CONTEXT_UNAVAILABLE",
                "error": repr(exc),
            }
        validation_result = validate_send_context_guard(
            expected_context_guard,
            snapshot.get("send_context_guard"),
        )
        return {
            **validation_result,
            "snapshot": snapshot,
        }

    uia_result = send_with_uia_controls(
        hwnd,
        final_send_text,
        geometry=geometry,
        settings=settings,
        artifact_dir=artifact_dir,
        before_input_region_seed=input_region_seed,
        before_send_click_check=pre_click_context_check,
        action_journal_path=action_journal_path,
    )
    _sidecar_timing_finish(timing, "uia_observed_send", uia_send_started)
    if isinstance(uia_result.get("timing"), dict):
        _sidecar_timing_merge_prefixed(timing, "uia", uia_result["timing"])
    physical_send_triggered = bool(
        uia_result.get("physical_send_triggered") is True
    )
    click_result: dict[str, Any] = {"ok": False, "reason": "disabled_by_single_send_rule", "mode": send_mode}
    if not uia_result.get("ok"):
        if physical_send_triggered:
            return finish({
                "ok": False,
                "online": True,
                "adapter": "win32_ocr",
                "state": "send_result_unknown",
                "error_code": "SEND_RESULT_UNKNOWN",
                "physical_send_triggered": True,
                "window_probe": probe,
                "target": target,
                "guard": {
                    **validation,
                    "points": points,
                    "rate": rate,
                    "uia": uia_result,
                    "click": click_result,
                    "send_baseline": baseline_snapshot,
                },
                "send_result": {
                    "ok": False,
                    "confirmed": False,
                    "result": "unknown",
                    "physical_send_triggered": True,
                },
                "error": "The physical send click ran, but the remaining send flow could not be confirmed.",
            })
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
                "send_baseline": baseline_snapshot,
            },
            "error_code": str(uia_result.get("error_code") or "SEND_INPUT_NOT_READY"),
            "error": str(uia_result.get("error") or uia_result.get("reason") or "send input could not be confirmed"),
            "physical_send_triggered": False,
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
            "error_code": "SEND_RESULT_UNKNOWN",
            "physical_send_triggered": True,
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
            "send_result": {
                "ok": False,
                "confirmed": False,
                "result": "unknown",
                "physical_send_triggered": True,
            },
            "error": "WeChat render became blank after input/send; stop before any further RPA action.",
        })
    sent_confirmation_started = _sidecar_timing_start(timing, "sent_confirmation")
    sent_confirmation = confirm_reply_sent(
        hwnd,
        target=target,
        text=final_send_text,
        exact=exact,
        baseline_match_count=int(baseline_snapshot.get("matching_self_message_count") or 0),
        baseline_message_sequence=list(baseline_snapshot.get("message_sequence") or []),
        artifact_dir=artifact_dir,
    )
    _sidecar_timing_finish(timing, "sent_confirmation", sent_confirmation_started)
    if not sent_confirmation.get("ok"):
        return finish({
            "ok": False,
            "online": True,
            "adapter": "win32_ocr",
            "state": "send_result_unknown",
            "error_code": "SEND_RESULT_UNKNOWN",
            "physical_send_triggered": True,
            "window_probe": probe,
            "target": target,
            "guard": {
                **validation,
                "points": points,
                "rate": rate,
                "uia": uia_result,
                "send_baseline": baseline_snapshot,
                "post_send_guard": post_validation,
                "sent_confirmation": sent_confirmation,
            },
            "send_result": {
                "ok": False,
                "confirmed": False,
                "result": "unknown",
                "physical_send_triggered": True,
                "sent_confirmation": sent_confirmation,
            },
            "error": "The send action ran, but a matching new right-side bubble was not proven.",
        })
    active_result = uia_result
    return finish({
        "ok": True,
        "online": True,
        "adapter": "win32_ocr",
        "state": "send_win32_rpa",
        "physical_send_triggered": True,
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
            "input_control": uia_result.get("edit"),
            "send_control": uia_result.get("send_button"),
            "rate": rate,
            "uia": uia_result,
            "click": click_result,
            "post_send_guard": post_validation,
            "send_baseline": baseline_snapshot,
            "sent_confirmation": sent_confirmation,
            "confirmed": True,
            "result": "sent",
            "physical_send_triggered": True,
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


def apply_interaction_rhythm(settings: dict[str, Any]) -> dict[str, Any]:
    return win32_ocr_humanized.apply_interaction_rhythm(settings)


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


def input_surface_click_evidence(input_region: dict[str, Any] | None) -> dict[str, Any]:
    return win32_ocr_interaction_evidence.input_surface_click_evidence(input_region)


def choose_verified_input_click_point(evidence: dict[str, Any] | None) -> dict[str, Any]:
    return win32_ocr_interaction_evidence.choose_input_click_point(evidence, random_module=random)


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
    verified_input_point: tuple[int, int] | None = None,
) -> dict[str, Any]:
    timing: dict[str, Any] = {}
    ocr_trace_token = _ocr_trace_start()
    paste_started = _sidecar_timing_start(timing, "paste_text_with_confirmation")
    last_input_click_evidence: dict[str, Any] = {}
    last_input_click: dict[str, Any] = {}

    def finish(payload: dict[str, Any]) -> dict[str, Any]:
        _sidecar_timing_finish(timing, "paste_text_with_confirmation", paste_started)
        _sidecar_timing_merge_ocr_trace(timing, "paste_text_with_confirmation", _ocr_trace_finish(ocr_trace_token))
        if last_input_click_evidence:
            payload.setdefault("input_click_evidence", last_input_click_evidence)
        if last_input_click:
            payload.setdefault("input_click", last_input_click)
        payload["timing"] = dict(timing)
        return payload

    probe_tokens = message_probe_tokens(text)
    probe_token = probe_tokens[0] if probe_tokens else ""
    settings = settings or adapt_humanized_input_settings(humanized_input_settings(), text)
    # Missing input evidence is a hard stop. Alternate geometry retries can
    # click chat history after the WeChat surface shifts.
    attempts = ["verified_input_evidence"]
    allow_copyback = env_flag("WECHAT_WIN32_OCR_INPUT_COPYBACK_CONFIRM", default=False)
    fast_visual_confirm = env_flag(
        "WECHAT_WIN32_OCR_INPUT_FAST_VISUAL_CONFIRM",
        default=DEFAULT_INPUT_FAST_VISUAL_CONFIRM,
    )
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
    for attempt, mode in enumerate(attempts, start=1):
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
        if input_region_soft_blank_noise(before_input_region):
            before_input_region = normalize_soft_blank_input_state(
                before_input_region,
                reason="input_region_soft_blank_noise",
            )
        if before_input_region.get("has_visible_text"):
            return finish({
                "ok": False,
                "reason": "unknown_input_draft_present",
                "error_code": "WECHAT_INPUT_DRAFT_PRESENT",
                "error": "WeChat input contains an unknown draft; preserve it and stop automatic reply.",
                "probe_token": probe_token,
                "probe_tokens": probe_tokens,
                "attempts": attempt,
                "copyback_enabled": allow_copyback,
                "input_region": before_input_region,
                "input_mode": input_method,
                "input_result": last_input_result,
            })
        clear_result = {
            "ok": True,
            "cleared": False,
            "reason": "input_region_confirmed_blank",
            "before": before_input_region,
            "after": before_input_region,
        }
        if verified_input_point is not None:
            click_x, click_y = [int(value) for value in verified_input_point]
            last_input_click_evidence = {
                "ok": True,
                "reason": "uia_edit_control_bounds",
                "point": [click_x, click_y],
            }
            last_input_click = {
                "ok": True,
                "reason": "uia_edit_control_center",
                "point": [click_x, click_y],
            }
        else:
            last_input_click_evidence = input_surface_click_evidence(before_input_region)
            if not last_input_click_evidence.get("ok"):
                return finish({
                    "ok": False,
                    "reason": "input_click_evidence_missing_before_type",
                    "probe_token": probe_token,
                    "probe_tokens": probe_tokens,
                    "attempts": attempt,
                    "copyback_enabled": allow_copyback,
                    "input_region": before_input_region,
                    "input_clear": clear_result,
                    "input_mode": input_method,
                    "input_result": last_input_result,
                })
            last_input_click = choose_verified_input_click_point(last_input_click_evidence)
            if not last_input_click.get("ok"):
                return finish({
                    "ok": False,
                    "reason": "input_click_evidence_missing_before_type",
                    "probe_token": probe_token,
                    "probe_tokens": probe_tokens,
                    "attempts": attempt,
                    "copyback_enabled": allow_copyback,
                    "input_region": before_input_region,
                    "input_clear": clear_result,
                    "input_mode": input_method,
                    "input_result": last_input_result,
                })
            click_x, click_y = [int(value) for value in last_input_click["point"]]
        input_click_started = _sidecar_timing_start(timing, "input_click")
        human_client_click(hwnd, click_x, click_y)
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
    before_physical_trigger: Any | None = None,
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
        return {
            "ok": False,
            "reason": "keyboard_send_trigger_disabled",
            "error_code": "SEND_BUTTON_CONTROL_REQUIRED",
            "error": "Enter-based send is disabled because WeChat may use Ctrl+Enter.",
            "send_trigger_mode": mode,
            "window_guard": guard,
        }
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
        if callable(before_physical_trigger):
            before_physical_trigger()
        human_client_click(hwnd, int(send_point[0]), int(send_point[1]))
        if active_settings.get("enabled"):
            humanized_sleep_ms(
                int(active_settings.get("send_after_trigger_delay_min_ms") or DEFAULT_HUMANIZED_SEND_AFTER_TRIGGER_DELAY_MIN_MS),
                int(active_settings.get("send_after_trigger_delay_max_ms") or DEFAULT_HUMANIZED_SEND_AFTER_TRIGGER_DELAY_MAX_MS),
            )
        return {"ok": True, "method": "human_click_send", "send_trigger_mode": mode, "window_guard": click_guard}
    return {"ok": False, "reason": "unsupported_send_trigger_mode", "send_trigger_mode": mode, "window_guard": guard}


def _read_uia_value_pattern_text(value_pattern: Any | None) -> tuple[bool, str]:
    if value_pattern is None:
        return False, ""
    try:
        raw_value = getattr(
            value_pattern,
            "Value",
            getattr(value_pattern, "value", ""),
        )
        return True, str(raw_value() if callable(raw_value) else raw_value or "")
    except Exception:
        return False, ""


def clear_confirmed_program_draft(
    hwnd: int,
    *,
    value_pattern: Any | None,
    input_point: tuple[int, int],
    expected_text: str,
    paste_result: dict[str, Any],
) -> dict[str, Any]:
    """Clear only a draft proven to have been typed by this send attempt."""

    input_result = (
        paste_result.get("input_result")
        if isinstance(paste_result.get("input_result"), dict)
        else {}
    )
    try:
        typed_chars = int(input_result.get("typed_chars") or 0)
    except (TypeError, ValueError):
        typed_chars = 0
    if not input_result.get("ok") or typed_chars < len(expected_text):
        return {
            "ok": False,
            "reason": "program_input_attempt_not_proven",
            "cleared": False,
        }
    value_readable, current_value = _read_uia_value_pattern_text(value_pattern)
    if not value_readable or current_value != expected_text:
        return {
            "ok": False,
            "reason": "program_draft_not_proven",
            "cleared": False,
            "observed_length": len(current_value) if value_readable else None,
        }
    try:
        human_client_click(hwnd, int(input_point[0]), int(input_point[1]))
        hotkey(win32con.VK_CONTROL, ord("A"))
        humanized_action_sleep(40, 80)
        key_press(win32con.VK_BACK)
        humanized_action_sleep(60, 120)
    except Exception as exc:
        return {
            "ok": False,
            "reason": "program_draft_clear_failed",
            "cleared": False,
            "error": repr(exc),
        }
    after_readable, after_clear = _read_uia_value_pattern_text(value_pattern)
    cleared = bool(after_readable and not after_clear)
    return {
        "ok": cleared,
        "reason": (
            "confirmed_program_draft_cleared"
            if cleared
            else "program_draft_clear_not_confirmed"
        ),
        "cleared": cleared,
    }


def send_with_uia_controls(
    hwnd: int,
    text: str,
    *,
    geometry: dict[str, Any],
    settings: dict[str, Any] | None = None,
    artifact_dir: str | None = None,
    before_input_region_seed: dict[str, Any] | None = None,
    before_send_click_check: Any | None = None,
    action_journal_path: str = "",
) -> dict[str, Any]:
    physical_send_triggered = False
    try:
        import uiautomation as auto  # type: ignore
    except Exception as exc:
        return {
            "ok": False,
            "reason": "uiautomation_unavailable",
            "error": repr(exc),
            "physical_send_triggered": False,
        }

    try:
        root = auto.ControlFromHandle(hwnd)
        controls = collect_uia_controls(root, max_depth=8, max_count=900)
        edit = select_uia_edit_control(controls, geometry)
        send_button = select_uia_send_button(controls, geometry)
        if edit is None:
            return {
                "ok": False,
                "reason": "uia_edit_not_found",
                "control_count": len(controls),
                "physical_send_triggered": False,
            }
        if send_button is None:
            return {
                "ok": False,
                "reason": "uia_send_button_not_found",
                "control_count": len(controls),
                "physical_send_triggered": False,
            }

        value_pattern = None
        try:
            value_pattern = edit.GetPattern(auto.PatternId.ValuePattern)
            raw_existing_value = getattr(
                value_pattern,
                "Value",
                getattr(value_pattern, "value", ""),
            )
            existing_value = str(
                raw_existing_value() if callable(raw_existing_value) else raw_existing_value or ""
            )
        except Exception:
            existing_value = ""
        if existing_value.strip():
            return {
                "ok": False,
                "reason": "unknown_input_draft_present",
                "error_code": "WECHAT_INPUT_DRAFT_PRESENT",
                "error": "WeChat input contains an unknown draft; preserve it and stop automatic reply.",
                "control_count": len(controls),
                "edit": describe_uia_control(edit, geometry),
                "existing_draft_length": len(existing_value),
                "physical_send_triggered": False,
            }

        edit_screen_rect = uia_rect_to_dict(
            safe_uia_attr(edit, "BoundingRectangle")
        )
        send_screen_rect = uia_rect_to_dict(
            safe_uia_attr(send_button, "BoundingRectangle")
        )
        input_point = screen_point_to_client(
            hwnd,
            int((edit_screen_rect["left"] + edit_screen_rect["right"]) / 2),
            int((edit_screen_rect["top"] + edit_screen_rect["bottom"]) / 2),
        )
        send_point = screen_point_to_client(
            hwnd,
            int((send_screen_rect["left"] + send_screen_rect["right"]) / 2),
            int((send_screen_rect["top"] + send_screen_rect["bottom"]) / 2),
        )
        settings = settings or adapt_humanized_input_settings(humanized_input_settings(), text)
        paste_result = paste_text_with_confirmation(
            hwnd,
            text,
            points={
                "input_point": list(input_point),
                "send_point": list(send_point),
            },
            geometry=geometry,
            artifact_dir=artifact_dir,
            settings=settings,
            before_input_region_seed=before_input_region_seed,
            verified_input_point=input_point,
        )
        if not paste_result.get("ok"):
            draft_clear = clear_confirmed_program_draft(
                hwnd,
                value_pattern=value_pattern,
                input_point=input_point,
                expected_text=text,
                paste_result=paste_result,
            )
            return {
                **paste_result,
                "draft_clear": draft_clear,
                "control_count": len(controls),
                "edit": describe_uia_control(edit, geometry),
                "send_button": describe_uia_control(send_button, geometry),
                "physical_send_triggered": False,
            }
        if settings.get("enabled"):
            humanized_sleep_ms(
                int(settings.get("send_post_input_delay_min_ms") or DEFAULT_HUMANIZED_SEND_POST_INPUT_DELAY_MIN_MS),
                int(settings.get("send_post_input_delay_max_ms") or DEFAULT_HUMANIZED_SEND_POST_INPUT_DELAY_MAX_MS),
            )
        context_check = (
            before_send_click_check()
            if callable(before_send_click_check)
            else {
                "ok": False,
                "reason": "pre_click_context_check_missing",
                "error_code": "C3_SEND_CONTEXT_GUARD_REQUIRED",
            }
        )
        if not context_check.get("ok"):
            draft_clear = clear_confirmed_program_draft(
                hwnd,
                value_pattern=value_pattern,
                input_point=input_point,
                expected_text=text,
                paste_result=paste_result,
            )
            return {
                "ok": False,
                "reason": "send_context_changed_before_click",
                "error_code": str(
                    context_check.get("error_code")
                    or "C3_CONTEXT_CHANGED_BEFORE_SEND"
                ),
                "context_check": context_check,
                "draft_clear": draft_clear,
                "control_count": len(controls),
                "edit": describe_uia_control(edit, geometry),
                "send_button": describe_uia_control(send_button, geometry),
                "paste": paste_result,
                "physical_send_triggered": False,
            }
        trigger_result = safe_send_trigger(
            hwnd,
            trigger_mode="click_only",
            send_point=send_point,
            settings=settings,
            focus_guard_func=lambda hwnd=hwnd: recover_send_window_guard(hwnd, max_attempts=1),
            before_physical_trigger=(
                lambda: write_action_phase_journal(
                    action_journal_path,
                    "trigger_attempted",
                )
                if action_journal_path
                else None
            ),
        )
        if not trigger_result.get("ok"):
            return {
                **trigger_result,
                "control_count": len(controls),
                "edit": describe_uia_control(edit, geometry),
                "send_button": describe_uia_control(send_button, geometry),
                "paste": paste_result,
                "physical_send_triggered": False,
            }
        physical_send_triggered = True
        input_method = str(paste_result.get("input_mode") or paste_result.get("method") or "sendinput")
        return {
            "ok": True,
            "method": f"uia_observed.{input_method}+SendInput+human_click",
            "control_count": len(controls),
            "edit": describe_uia_control(edit, geometry),
            "send_button": describe_uia_control(send_button, geometry),
            "humanized_input": settings,
            "input_result": paste_result,
            "send_trigger": trigger_result,
            "physical_send_triggered": True,
        }
    except Exception as exc:
        return {
            "ok": False,
            "reason": "uia_send_failed",
            "error": repr(exc),
            "physical_send_triggered": physical_send_triggered,
        }


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
    timing["activation_click_point"] = [int(click_x), int(click_y)]
    timing["activation_click_method"] = "human_window_image_click"
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
    # Session rows are parsed from screenshot/OCR coordinates. Use the same
    # window-image click path as search-result activation to avoid client
    # coordinate drift on Windows DPI / scaled WeChat windows.
    human_window_image_click(hwnd, click_x, click_y)
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
        if not target_switch_validation_is_hard_stop(validation):
            selected_confirm_started = _sidecar_timing_start(timing, f"activation_confirm_{attempt + 1}_selected_session_validation")
            selected_validation = validate_active_selected_session_target(
                hwnd,
                target,
                exact=exact,
                artifact_dir=artifact_dir,
            )
            _sidecar_timing_finish(timing, f"activation_confirm_{attempt + 1}_selected_session_validation", selected_confirm_started)
            _sidecar_timing_merge_prefixed(
                timing,
                f"activation_confirm_{attempt + 1}_selected_session_validation",
                selected_validation,
            )
            if selected_validation.get("ok"):
                timing["activation_confirmed_by_attempt"] = attempt + 1
                timing["activation_confirmation_confidence"] = "selected_session_list"
                timing[f"activation_confirm_{attempt + 1}_selected_session_ok"] = True
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


def find_unique_session_candidate_by_semantics(
    sessions: list[dict[str, Any]],
    *,
    target: str,
    semantic_target: str = "",
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    clean_target = str(target or "").strip()
    clean_semantic_target = str(semantic_target or "").strip()
    hints: list[tuple[str, str]] = []
    if clean_semantic_target:
        hints.append(("remark_code", clean_semantic_target))
    elif clean_target:
        hints.append(("display_name", clean_target))

    attempts: list[dict[str, Any]] = []
    for source, hint in hints:
        matches: list[dict[str, Any]] = []
        excluded: list[dict[str, Any]] = []
        for item in sessions:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "")
            if source == "remark_code":
                matched = remark_code_matches_text(name, hint)
                admission = classify_c2_conversation_title(item.get("raw_title") or name, hint)
                if matched and not admission.get("admission_allowed"):
                    excluded.append(
                        {
                            "name": name,
                            "raw_title": str(item.get("raw_title") or name),
                            "conversation_type": admission.get("conversation_type"),
                            "reason": admission.get("reason"),
                        }
                    )
                    matched = False
            else:
                matched = session_name_matches(name, hint, exact=False)
            if matched:
                matches.append(item)
        attempts.append(
            {
                "source": source,
                "hint": hint,
                "match_count": len(matches),
                "matches": [
                    {
                        "name": str(item.get("name") or ""),
                        "session_key": str(item.get("session_key") or ""),
                        "center_y": item.get("center_y"),
                    }
                    for item in matches[:5]
                ],
                "excluded_by_c2_admission": excluded[:5],
            }
        )
        if len(matches) == 1:
            return matches[0], {"matched_by": source, "attempts": attempts}
        if len(matches) > 1:
            return None, {"matched_by": "", "ambiguous": True, "attempts": attempts}
    return None, {"matched_by": "", "ambiguous": False, "attempts": attempts}


def visible_session_name_is_unambiguous(
    sessions: list[dict[str, Any]],
    target: str,
    *,
    exact: bool,
    semantic_target: str = "",
) -> bool:
    if semantic_target:
        matches = search_result_sessions_matching_remark_code(sessions, semantic_target)
    else:
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
    ocr_items, _enhanced_count = session_list_ocr_items(
        screenshot,
        run_ocr_traced(screenshot, "open_chat_main_list", source="ensure_main_session_list"),
    )
    hops = max(0, int(max_hops))
    for _ in range(hops):
        back_target = detect_session_subview_back_target(ocr_items, screenshot.size)
        if not back_target:
            break
        client_click(hwnd, int(back_target["x"]), int(back_target["y"]))
        humanized_action_sleep(280, 480)
        screenshot, _path = capture_wechat(hwnd, artifact_dir=artifact_dir, label="open_chat_main_list")
        ocr_items, _enhanced_count = session_list_ocr_items(
            screenshot,
            run_ocr_traced(screenshot, "open_chat_main_list_after_back", source="ensure_main_session_list"),
        )
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
    minimum_cached_at: float | None = None,
    require_session_key_match: bool = False,
) -> dict[str, Any] | None:
    cached = _LAST_RPA_ACTION_STATE.get("target_ready_last_switch_validation")
    if not isinstance(cached, dict):
        return None
    ttl = target_ready_switch_validation_cache_seconds() if ttl_seconds is None else max(0.0, float(ttl_seconds))
    if ttl <= 0:
        return None
    cached_at = float(cached.get("ts") or 0.0)
    if minimum_cached_at is not None and cached_at < float(minimum_cached_at):
        return None
    age = max(0.0, time.monotonic() - cached_at)
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
    if require_session_key_match and (not clean_session_key or cached_session_key != clean_session_key):
        return None
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


def bounded_edit_distance(left: str, right: str, *, max_distance: int = 2) -> int:
    a = str(left or "")
    b = str(right or "")
    if abs(len(a) - len(b)) > max_distance:
        return max_distance + 1
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        current = [i]
        row_min = current[0]
        for j, cb in enumerate(b, start=1):
            cost = 0 if ca == cb else 1
            value = min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + cost)
            current.append(value)
            row_min = min(row_min, value)
        if row_min > max_distance:
            return max_distance + 1
        previous = current
    return previous[-1]


def sidebar_search_query_mismatch_allows_candidate_probe(query_text: str, expected: str) -> bool:
    query = re.sub(r"\s+", "", normalize_session_name(str(query_text or ""))).strip().lower()
    target = re.sub(r"\s+", "", normalize_session_name(str(expected or ""))).strip().lower()
    if not query or not target:
        return False
    if query == target:
        return True
    if len(query) > len(target) + 2:
        return False
    return bounded_edit_distance(query, target, max_distance=2) <= 2


def sidebar_search_clear_residue_allows_candidate_probe(query_text: str) -> bool:
    query = re.sub(r"\s+", "", normalize_session_name(str(query_text or ""))).strip().lower()
    if not query:
        return True
    return query in {"q", "o", "0", "搜索", "q搜索"}


def sidebar_search_input_target_from_ocr(
    ocr_items: list[dict[str, Any]],
    image_size: tuple[int, int],
    *,
    geometry: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    width, height = image_size
    if width <= 0 or height <= 0:
        return None
    active_geometry = geometry if isinstance(geometry, dict) else {"width": width, "height": height}
    split_x = session_split_x(int(active_geometry.get("width") or width))
    expected_x, expected_y = sidebar_search_input_focus_point_for_geometry(active_geometry)
    candidates = []
    for item in ocr_items or []:
        center_x = float(item.get("center_x") or 0)
        center_y = float(item.get("center_y") or 0)
        text = voice_transcribe_compact_text(item.get("text"))
        if not (48 <= center_x <= split_x - 44 and 38 <= center_y <= min(138, height * 0.18)):
            continue
        if text in {"+", "＋"}:
            continue
        candidates.append((abs(center_x - expected_x) + abs(center_y - expected_y) * 2.0, item))
    if not candidates:
        return None
    _, item = min(candidates, key=lambda entry: entry[0])
    center_y = int(float(item.get("center_y") or expected_y))
    bounds = [
        max(44, int(float(item.get("left") or expected_x)) - 34),
        max(38, int(float(item.get("top") or center_y)) - 12),
        min(split_x - 48, max(int(float(item.get("right") or expected_x)) + 58, expected_x + 46)),
        min(138, int(float(item.get("bottom") or center_y)) + 12),
    ]
    if bounds[2] - bounds[0] < 54 or bounds[3] - bounds[1] < 18:
        return None
    return {
        "point": [min(max(expected_x, bounds[0] + 8), bounds[2] - 8), (bounds[1] + bounds[3]) // 2],
        "bounds": bounds,
        "source": "fresh_ocr_sidebar_search_input",
        "ocr_text": str(item.get("text") or ""),
    }


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
    result: dict[str, Any] = {"ok": True, "method": "fresh_search_clear_and_header_blank_click", "attempts": 0}
    max_attempts = 2 if artifact_dir else 1
    last_search_state: dict[str, Any] = {"detected": False, "reason": ""}
    for attempt in range(1, max_attempts + 1):
        result["attempts"] = attempt
        before_shot, before_path = capture_wechat(hwnd, artifact_dir=artifact_dir, label="open_chat_search_dismiss_before")
        before_items = run_ocr_traced(before_shot, "open_chat_search_dismiss_before", source="open_chat")
        search_target = sidebar_search_input_target_from_ocr(before_items, before_shot.size, geometry=active_geometry)
        if not search_target:
            fallback_x, fallback_y = sidebar_search_input_focus_point_for_geometry(active_geometry)
            search_target = {
                "point": [fallback_x, fallback_y],
                "bounds": [max(44, fallback_x - 54), max(38, fallback_y - 22), min(session_split_x(before_shot.size[0]) - 48, fallback_x + 88), min(138, fallback_y + 24)],
                "source": "geometry_fallback_with_post_verification",
            }
        human_window_image_click_in_bounds(
            hwnd,
            int(search_target["point"][0]),
            int(search_target["point"][1]),
            bounds=search_target["bounds"],
            action_name="sidebar_search_dismiss_focus_fresh_target",
        )
        humanized_action_sleep(180, 420)
        hotkey(win32con.VK_CONTROL, ord("A"))
        humanized_action_sleep(100, 260)
        key_press(win32con.VK_BACK)
        humanized_action_sleep(260, 620)
        cleared_shot, _ = capture_wechat(hwnd, artifact_dir=artifact_dir, label="open_chat_search_dismiss_cleared")
        cleared_items = run_ocr_traced(cleared_shot, "open_chat_search_dismiss_cleared", source="open_chat")
        blank_target = safe_window_header_blank_click_target(cleared_items, cleared_shot.size, geometry=active_geometry)
        if not blank_target:
            return {**result, "ok": False, "reason": "safe_header_blank_target_not_found", "search_target": search_target}
        human_window_image_click_in_bounds(
            hwnd,
            int(blank_target["point"][0]),
            int(blank_target["point"][1]),
            bounds=blank_target["bounds"],
            action_name="sidebar_search_dismiss_header_blank_click",
        )
        result["search_target"] = search_target
        result["blank_target"] = blank_target
        humanized_action_sleep(620, 1400)
        after_guard = basic_send_window_guard(hwnd)
        result["window_guard"] = after_guard
        if not after_guard.get("ok"):
            return {"ok": False, "reason": "window_guard_failed_after_search_dismiss", "window_guard": after_guard}
        shot, shot_path = capture_wechat(hwnd, artifact_dir=artifact_dir, label="open_chat_search_dismiss_after_click")
        items = run_ocr_traced(shot, "open_chat_search_dismiss_after_click", source="open_chat")
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


def sidebar_search_box_evidence(
    ocr_items: list[dict[str, Any]],
    *,
    geometry: dict[str, Any],
) -> dict[str, Any]:
    """Require a currently visible sidebar search label before clicking."""

    width = int(geometry.get("width") or 0)
    height = int(geometry.get("height") or 0)
    split_x = session_split_x(width)
    if width <= 0 or height <= 0 or split_x <= 0:
        return {"ok": False, "reason": "search_box_evidence_geometry_invalid"}
    for item in ocr_items:
        text = normalize_ocr_text(item.get("text"))
        compact = re.sub(r"\s+", "", text).lower()
        visible_search_label = compact == "search" or bool(re.fullmatch(r"[qo0]?搜索", compact))
        if not visible_search_label:
            continue
        try:
            left = int(float(item.get("left") or 0))
            top = int(float(item.get("top") or 0))
            right = int(float(item.get("right") or 0))
            bottom = int(float(item.get("bottom") or 0))
        except (TypeError, ValueError):
            continue
        if left < 32 or right > split_x - 12 or top < 28 or bottom > min(142, int(height * 0.20)):
            continue
        bounds = [
            max(42, left - 58),
            max(42, top - 24),
            min(max(120, split_x - 34), right + 82),
            min(132, bottom + 24),
        ]
        if bounds[2] <= bounds[0] or bounds[3] <= bounds[1]:
            continue
        return {
            "ok": True,
            "reason": "visible_sidebar_search_label",
            "bounds": bounds,
            "point": [int((bounds[0] + bounds[2]) / 2), int((bounds[1] + bounds[3]) / 2)],
            "item": item,
        }
    return {"ok": False, "reason": "search_box_evidence_missing"}


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
    click_result: dict[str, Any] = {"ok": True, "bounds": None}
    active_geometry = geometry if isinstance(geometry, dict) else get_window_geometry(hwnd)
    try:
        evidence_shot, evidence_path = capture_wechat(
            hwnd,
            artifact_dir=artifact_dir,
            label="open_chat_search_box_before_click",
        )
        evidence_items = run_ocr_traced(evidence_shot, "open_chat_search_box_before_click", source="open_chat")
    except Exception as exc:
        return {
            "ok": False,
            "reason": "search_box_evidence_capture_failed",
            "error": repr(exc),
        }
    evidence_surface = target_switch_surface_state(
        evidence_shot,
        evidence_items,
        geometry=active_geometry,
        screenshot_path=evidence_path,
        target=target_hint,
    )
    search_box_evidence = sidebar_search_box_evidence(evidence_items, geometry=active_geometry)
    if not evidence_surface.get("ok") or not search_box_evidence.get("ok"):
        return {
            "ok": False,
            "reason": "search_box_evidence_missing_before_click",
            "surface": evidence_surface,
            "search_box_evidence": search_box_evidence,
            "screenshot_path": evidence_path,
            "ocr_count": len(evidence_items),
        }
    bounds = [int(value) for value in search_box_evidence["bounds"]]
    search_x, search_y = [int(value) for value in search_box_evidence["point"]]
    click_result = human_window_image_click_in_bounds(
        hwnd,
        search_x,
        search_y,
        bounds=bounds,
        action_name="sidebar_search_box_click",
    )
    if not click_result.get("ok"):
        return {
            "ok": False,
            "reason": "search_box_click_failed",
            "click": click_result,
            "search_box_evidence": search_box_evidence,
        }
    humanized_action_sleep(720, 1600)
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
        target="",
    )
    if not surface.get("ok"):
        return {
            "ok": False,
            "reason": str(surface.get("reason") or "search_box_surface_not_ok"),
            "surface": surface,
            "click": click_result,
            "search_box_evidence": search_box_evidence,
        }
    search_state = sidebar_search_state_detected(probe_shot, probe_items, geometry=active_geometry)
    if not search_state.get("detected"):
        return {
            "ok": False,
            "reason": "search_box_focus_not_confirmed",
            "surface": surface,
            "search_state": search_state,
            "click": click_result,
            "search_box_evidence": search_box_evidence,
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
        target="",
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
            target="",
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
            "search_box_evidence": search_box_evidence,
            "query_text": clear_query_text,
            "refocus": refocus_result,
        }
    if clear_query_text:
        if sidebar_search_clear_residue_allows_candidate_probe(clear_query_text):
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
                "warning": "search_clear_ocr_residue_ignored",
                "continued_after_clear_ocr_residue": True,
            }
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
        "search_box_evidence": search_box_evidence,
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
        if not verify_after_paste:
            return {"ok": True, "method": "clipboard", "window_guard": guard}
        active_geometry = geometry if isinstance(geometry, dict) else get_window_geometry(hwnd)
        verify_shot, verify_path = capture_wechat(hwnd, artifact_dir=artifact_dir, label="open_chat_search_box_after_paste")
        verify_items = run_ocr_traced(verify_shot, "open_chat_search_box_after_paste", source="open_chat")
        surface = target_switch_surface_state(
            verify_shot,
            verify_items,
            geometry=active_geometry,
            screenshot_path=verify_path,
            target="",
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
        target="",
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
    matches: list[dict[str, Any]] = []
    for item in sessions:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "")
        if not remark_code_matches_text(name, remark_code):
            continue
        admission = classify_c2_conversation_title(item.get("raw_title") or name, remark_code)
        if admission.get("admission_allowed"):
            matches.append({**item, "c2_conversation_admission": admission})
    return matches


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
            expected="result.ok=true，search_state.detected=true；若 query_text 仅为 Q/搜索图标残影可继续，真实旧查询串必须失败。",
            source=clear_result.get("surface") if isinstance(clear_result.get("surface"), dict) else {},
            detection=clear_step,
        )
    )
    paste_step = by_step.get("paste_remark_code") or {}
    paste_result = paste_step.get("result") if isinstance(paste_step.get("result"), dict) else {}
    rows.append(
        _targeting_review_row(
            title="04 粘贴短码后复核",
            purpose="粘贴短码；默认不再强制 OCR 复核搜索框文本，避免 J/I/图标误识别拖慢流程。",
            expected="安全判断以后续唯一候选和点击后标题/备注短码确认为准；如启用粘贴复核，真实拼接旧查询串仍必须失败。",
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
        if compact in {"联系人", "群聊", "更多", "收藏"} or "搜索网络结果" in compact or "网络查找" in compact or compact.startswith("查看全部"):
            headings.append((compact, float(item.get("center_y") or 0)))

    contact_heading_y = min((y for text, y in headings if text == "联系人"), default=86.0)
    section_bottom = min(
        (
            y
            for text, y in headings
            if y > contact_heading_y and (text in {"群聊", "更多", "收藏"} or "搜索网络结果" in text or "网络查找" in text)
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
        raw_title = normalize_ocr_text(" ".join(row_texts)) or normalize_ocr_text(text)
        name = normalize_session_name(raw_title) or normalize_session_name(text)
        if not remark_code_matches_text(name, remark_code):
            name = normalize_session_name(text)
            raw_title = normalize_ocr_text(text)
        admission = classify_c2_conversation_title(raw_title, remark_code)
        if not admission.get("admission_allowed"):
            continue
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
                "raw_title": raw_title,
                "session_key": rpa_session_key(
                    name,
                    row_fingerprint=session_row_fingerprint(item, duplicate_index=0),
                ),
                "conversation_type": str(admission.get("conversation_type") or "unknown"),
                "candidate_kind": "contact",
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
                "c2_conversation_admission": admission,
            }
        )
        consumed_rows.append(center_y)
    return matches


def fallback_first_search_contact_candidate(
    ocr_items: list[dict[str, Any]],
    image_size: tuple[int, int],
    remark_code: str,
) -> dict[str, Any] | None:
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
        if compact in {"联系人", "群聊", "更多", "收藏"} or "搜索网络结果" in compact or "网络查找" in compact or compact.startswith("查看全部"):
            headings.append((compact, float(item.get("center_y") or 0)))

    contact_heading_y = min((y for text, y in headings if text == "联系人"), default=88.0)
    section_bottom = min(
        (
            y
            for text, y in headings
            if y > contact_heading_y and (text in {"群聊", "更多", "收藏"} or "搜索网络结果" in text or "网络查找" in text)
        ),
        default=min(float(height), contact_heading_y + 150.0),
    )
    row_items = [
        item
        for item in ocr_items or []
        if in_search_panel(item)
        and contact_heading_y < float(item.get("center_y") or 0) < section_bottom
        and normalize_ocr_text(item.get("text"))
    ]
    if not row_items:
        return None
    first_y = min(float(item.get("center_y") or 0) for item in row_items)
    row_items = [
        item
        for item in row_items
        if abs(float(item.get("center_y") or 0) - first_y) <= 28
    ]
    if not row_items:
        return None
    row_items = sorted(row_items, key=lambda row: float(row.get("left") or 0))
    row_texts = [str(item.get("text") or "").strip() for item in row_items if str(item.get("text") or "").strip()]
    raw_title = normalize_ocr_text(" ".join(row_texts))
    name = normalize_session_name(raw_title)
    admission = classify_c2_conversation_title(raw_title, remark_code)
    if not name or not remark_code_matches_text(name, remark_code) or not admission.get("admission_allowed"):
        return None
    left = min(float(item.get("left") or 0) for item in row_items)
    right = max(float(item.get("right") or 0) for item in row_items)
    top = min(float(item.get("top") or 0) for item in row_items)
    bottom = max(float(item.get("bottom") or 0) for item in row_items)
    bounds = [
        max(88, int(left) - 74),
        max(88, int(top) - 20),
        min(left_panel_right, max(int(right) + 150, int(left) + 240)),
        min(height - 12, max(int(bottom) + 24, int(top) + 68)),
    ]
    center_y = int((bounds[1] + bounds[3]) / 2)
    text_center_x = int((left + right) / 2)
    click_points = [
        [bounded_int(text_center_x, default=190, minimum=bounds[0] + 12, maximum=bounds[2] - 12), center_y],
        [bounded_int(int(left) + 58, default=text_center_x, minimum=bounds[0] + 12, maximum=bounds[2] - 12), center_y],
        [bounded_int(int(right) + 24, default=text_center_x, minimum=bounds[0] + 12, maximum=bounds[2] - 12), center_y],
    ]
    return {
        "name": name,
        "raw_title": raw_title,
        "session_key": rpa_session_key(
            name,
            row_fingerprint=session_row_fingerprint(row_items[0], duplicate_index=0),
        ),
        "conversation_type": str(admission.get("conversation_type") or "unknown"),
        "candidate_kind": "contact",
        "row_fingerprint": session_row_fingerprint(row_items[0], duplicate_index=0),
        "duplicate_name_index": 0,
        "ambiguous_display_name": True,
        "confidence": None,
        "center_y": center_y,
        "left": left,
        "right": right,
        "top": top,
        "bottom": bottom,
        "source_adapter": "win32_ocr",
        "source": "search_contact_result",
        "fallback_source": "first_contact_row_after_search",
        "search_result_bounds": bounds,
        "search_result_click_points": click_points,
        "section": "contacts",
        "c2_conversation_admission": admission,
    }


def active_selected_session_matches(
    ocr_items: list[dict[str, Any]],
    image_size: tuple[int, int],
    *,
    target: str,
    exact: bool,
) -> bool:
    if not target:
        return False
    normalized_target = normalize_session_name(target)
    if not normalized_target:
        return False
    width, height = image_size
    split_x = session_split_x(width)
    top_limit = 88
    bottom_limit = min(height, 238)
    for item in ocr_items or []:
        text = normalize_ocr_text(item.get("text"))
        if not text:
            continue
        if float(item.get("center_x") or 0) >= split_x + 16:
            continue
        center_y = float(item.get("center_y") or 0)
        if center_y < top_limit or center_y > bottom_limit:
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


def validate_active_selected_session_target(
    hwnd: int,
    target: str,
    *,
    exact: bool,
    artifact_dir: str | None = None,
) -> dict[str, Any]:
    screenshot, path = capture_wechat(hwnd, artifact_dir=artifact_dir, label="selected_session_guard")
    items = run_ocr_traced(screenshot, "selected_session_guard", source="validate_selected_session")
    matched = active_selected_session_matches(items, screenshot.size, target=target, exact=exact)
    return {
        "ok": bool(matched),
        "online": True,
        "reason": "selected_session_confirmed" if matched else "selected_session_not_confirmed",
        "requested_target": target,
        "confirmed_target": target if matched else "",
        "confirmation_confidence": "selected_session_list" if matched else "failed_selected_session_list",
        "screenshot_path": path,
        "ocr_count": len(items),
    }


def c2_target_activation_confirmed(validation: dict[str, Any] | None) -> bool:
    if not isinstance(validation, dict):
        return False
    return bool(
        active_send_guard_is_strong(validation)
        and validation.get("conversation_type") == "private"
        and (validation.get("conversation_type_evidence") or {}).get("short_code_confirmed") is True
    )


def c2_target_admission_error(validation: dict[str, Any] | None, fallback: str) -> tuple[str, str]:
    evidence = validation if isinstance(validation, dict) else {}
    title_evidence = evidence.get("conversation_type_evidence") if isinstance(evidence.get("conversation_type_evidence"), dict) else evidence
    if title_evidence.get("short_code_confirmed") is not True:
        return fallback, "The active title is not the requested short-code target; continue visible or short-code lookup."
    if "conversation_type" not in title_evidence:
        return fallback, "The target chat title and private-chat admission were not both confirmed."
    conversation_type = str(title_evidence.get("conversation_type") or "unknown")
    raw_title = str(title_evidence.get("raw_title") or "")
    if conversation_type == "group":
        return "C2_GROUP_CHAT_NOT_ALLOWED", f"C2 excludes group chat title: {raw_title or '<unknown>'}."
    if conversation_type == "unknown":
        return "C2_CONVERSATION_TYPE_UNKNOWN", "C2 could not safely confirm a private chat from the existing title OCR."
    return fallback, "The target chat title and private-chat admission were not both confirmed."


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

    point = next((item for item in points if isinstance(item, (list, tuple)) and len(item) >= 2), None)
    if point is None:
        return finish(False, "search_result_candidate_click_point_missing", validation={})
    x, y = int(point[0]), int(point[1])
    pre_click_wait_started = _sidecar_timing_start(timing, "search_result_click_wait")
    humanized_action_sleep(260, 760)
    _sidecar_timing_finish(timing, "search_result_click_wait", pre_click_wait_started)
    click_started = _sidecar_timing_start(timing, "search_result_click")
    # One fresh OCR row produces exactly one physical click. Slow UI updates
    # are handled by passive verification, never by probing more row points.
    human_window_image_click(hwnd, x, y)
    _sidecar_timing_finish(timing, "search_result_click", click_started)
    last_validation: dict[str, Any] = {}
    for verification_index in range(2):
        humanized_action_sleep(520, 1050)
        validation = validate_active_send_target(hwnd, remark_code, exact=False, artifact_dir=artifact_dir)
        if not active_send_guard_is_strong(validation) and not target_switch_validation_is_hard_stop(validation):
            selected_validation = validate_active_selected_session_target(hwnd, remark_code, exact=False, artifact_dir=artifact_dir)
            if selected_validation.get("ok"):
                validation = {**validation, "selected_session_validation": selected_validation, "ok": True, "confirmation_confidence": "selected_session_list"}
        last_validation = validation
        attempts.append({
            "point": [x, y],
            "click_method": "human_window_image_click" if verification_index == 0 else "passive_recheck",
            "validation": validation,
        })
        if c2_target_activation_confirmed(validation):
            return finish(True, "search_result_candidate_confirmed", validation=validation, confirmed_point=[x, y])
        if target_switch_validation_is_hard_stop(validation):
            return finish(False, "search_result_candidate_hard_stop", validation=validation)
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
    baseline_surface = target_switch_surface_state(
        baseline_shot,
        baseline_items,
        geometry=geometry,
        target=clean_remark,
    )
    event("baseline_surface_check", "completed" if baseline_surface.get("ok") else "failed", surface=baseline_surface)
    if not baseline_surface.get("ok"):
        return finish(
            False,
            str(baseline_surface.get("reason") or "search_baseline_surface_not_ok"),
            surface=baseline_surface,
        )
    search_x, search_y = sidebar_search_input_focus_point_for_geometry(geometry)
    search_target = sidebar_search_input_target_from_ocr(baseline_items, baseline_shot.size, geometry=geometry)
    if search_target:
        search_x, search_y = [int(value) for value in search_target["point"]]
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
        verify_after_paste=env_flag("WECHAT_WIN32_OCR_MESSAGES_VERIFY_SEARCH_QUERY_AFTER_PASTE", default=False),
    )
    _sidecar_timing_finish(timing, "search_by_remark_code_input", input_started)
    input_ocr_soft_mismatch = (
        not input_result.get("ok")
        and str(input_result.get("reason") or "") == "search_query_text_mismatch_after_paste"
        and sidebar_search_query_mismatch_allows_candidate_probe(str(input_result.get("query_text") or ""), clean_remark)
    )
    event(
        "paste_remark_code",
        "completed" if input_result.get("ok") else ("warning" if input_ocr_soft_mismatch else "failed"),
        result=input_result,
        continued_after_ocr_query_mismatch=bool(input_ocr_soft_mismatch),
    )
    if not input_result.get("ok") and not input_ocr_soft_mismatch:
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
        target="",
    )
    if not surface.get("ok"):
        event("search_surface_check", "failed", surface=surface)
        return finish(False, str(surface.get("reason") or "search_surface_not_ok"), screenshot_path=search_path, surface=surface)
    event("search_surface_check", "completed", surface=surface)

    contact_matches = search_result_contact_candidates_matching_remark_code(search_items, search_shot.size, clean_remark)
    sessions = parse_sessions_from_ocr(search_items, search_shot.size, screenshot=search_shot)
    session_matches = search_result_sessions_matching_remark_code(sessions, clean_remark)
    matches = contact_matches or session_matches
    fallback_candidate: dict[str, Any] | None = None
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
                target="",
            )
            if not surface.get("ok"):
                event("search_surface_check_after_nudge", "failed", surface=surface)
                return finish(False, str(surface.get("reason") or "search_surface_not_ok_after_nudge"), screenshot_path=search_path, surface=surface, nudge=nudge_result)
            event("search_surface_check_after_nudge", "completed", surface=surface)
            contact_matches = search_result_contact_candidates_matching_remark_code(search_items, search_shot.size, clean_remark)
            sessions = parse_sessions_from_ocr(search_items, search_shot.size, screenshot=search_shot)
            session_matches = search_result_sessions_matching_remark_code(sessions, clean_remark)
            matches = contact_matches or session_matches
    if not matches:
        fallback_candidate = fallback_first_search_contact_candidate(search_items, search_shot.size, clean_remark)
        if fallback_candidate:
            matches = [fallback_candidate]
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
        fallback_candidate={"name": fallback_candidate.get("name"), "bounds": fallback_candidate.get("search_result_bounds")} if fallback_candidate else None,
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
    if not active_send_guard_is_strong(validation) and not target_switch_validation_is_hard_stop(validation):
        selected_validation = validate_active_selected_session_target(hwnd, clean_remark, exact=False, artifact_dir=artifact_dir)
        if selected_validation.get("ok"):
            validation = {**validation, "selected_session_validation": selected_validation, "ok": True, "confirmation_confidence": "selected_session_list"}
    event("confirm_active_title_remark_code", "completed" if c2_target_activation_confirmed(validation) else "failed", validation=validation)
    if not c2_target_activation_confirmed(validation):
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
    semantic_target: str = "",
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
        ocr_items, enhanced_count = session_list_ocr_items(screenshot, list(seed.get("ocr_items") or []))
        if detect_session_subview_back_target(ocr_items, screenshot.size):
            timing["open_chat_main_list_prevalidation_ocr_seed_reused"] = False
            timing["open_chat_main_list_prevalidation_ocr_seed_discarded"] = "session_subview"
            screenshot, ocr_items = ensure_main_session_list(hwnd, artifact_dir=artifact_dir)
            enhanced_count = sum(
                1
                for item in ocr_items
                if str(item.get("ocr_source") or "") == "sidebar_visible_list_enhanced"
            )
        else:
            timing["open_chat_main_list_prevalidation_ocr_seed_reused"] = True
            timing["open_chat_main_list_prevalidation_ocr_seed_age_seconds"] = seed.get("age_seconds")
            timing["open_chat_main_list_prevalidation_ocr_seed_count"] = len(ocr_items)
    else:
        screenshot, ocr_items = ensure_main_session_list(hwnd, artifact_dir=artifact_dir)
        enhanced_count = sum(
            1
            for item in ocr_items
            if str(item.get("ocr_source") or "") == "sidebar_visible_list_enhanced"
        )
        timing["open_chat_main_list_prevalidation_ocr_seed_reused"] = False
    timing["open_chat_main_list_enhanced_ocr_count"] = enhanced_count
    _sidecar_timing_finish(timing, "open_chat_main_list", main_list_started)
    geometry_started = _sidecar_timing_start(timing, "open_chat_geometry")
    geometry = geometry_for_seed if isinstance(geometry_for_seed, dict) else get_window_geometry(hwnd)
    session_click_x = session_click_x_for_geometry(geometry)
    search_x, search_y = sidebar_search_input_focus_point_for_geometry(geometry)
    search_target = sidebar_search_input_target_from_ocr(ocr_items, screenshot.size, geometry=geometry)
    if search_target:
        search_x, search_y = [int(value) for value in search_target["point"]]
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
    clean_semantic_target = str(semantic_target or "").strip()
    active_identity_target = clean_semantic_target or target
    active_match_started = _sidecar_timing_start(timing, "open_chat_active_match")
    active_evidence = active_chat_title_evidence(
        ocr_items,
        screenshot.size,
        target=active_identity_target,
        exact=False if clean_semantic_target else exact,
    )
    active_matches = bool(active_evidence.get("matched"))
    _sidecar_timing_finish(timing, "open_chat_active_match", active_match_started)
    timing["open_chat_initial_active_match"] = bool(active_matches)
    timing["open_chat_initial_active_evidence"] = active_evidence
    active_semantic_private = bool(
        clean_semantic_target and active_evidence.get("admission_allowed") is True
    )
    active_semantic_terminal = bool(
        clean_semantic_target
        and active_evidence.get("short_code_confirmed") is True
        and str(active_evidence.get("conversation_type") or "unknown") in {"group", "unknown"}
    )
    if active_semantic_terminal:
        conversation_type = str(active_evidence.get("conversation_type") or "unknown")
        return finish(False, f"active_{conversation_type}_remark_code_blocked")
    if not clean_semantic_target and not clean_session_key and active_matches:
        return finish(True, "active_target_match")
    if (
        not clean_semantic_target
        and clean_session_key
        and str(_LAST_RPA_ACTION_STATE.get("active_session_key") or "") == clean_session_key
        and active_matches
    ):
        return finish(True, "active_session_key_match")
    parse_started = _sidecar_timing_start(timing, "open_chat_parse_sessions")
    sessions = parse_sessions_from_ocr(ocr_items, screenshot.size, screenshot=screenshot)
    _sidecar_timing_finish(timing, "open_chat_parse_sessions", parse_started)
    timing["open_chat_session_count"] = len(sessions)
    if active_semantic_private:
        _candidate, semantic_match = find_unique_session_candidate_by_semantics(
            sessions,
            target=target,
            semantic_target=clean_semantic_target,
        )
        timing["open_chat_active_semantic_candidate"] = semantic_match
        if semantic_match.get("ambiguous"):
            return finish(False, "active_private_remark_code_ambiguous")
        _LAST_RPA_ACTION_STATE["active_target"] = clean_semantic_target
        return finish(True, "active_private_remark_code_match")
    if not clean_semantic_target and clean_session_key and active_matches:
        if visible_session_name_is_unambiguous(
            sessions,
            target,
            exact=exact,
            semantic_target=str(semantic_target or ""),
        ):
            _LAST_RPA_ACTION_STATE["active_session_key"] = clean_session_key
            _LAST_RPA_ACTION_STATE["active_target"] = target
            return finish(True, "active_visible_unambiguous")
        return finish(False, "active_visible_ambiguous")
    if clean_semantic_target:
        semantic_candidate, semantic_match = find_unique_session_candidate_by_semantics(
            sessions,
            target=target,
            semantic_target=clean_semantic_target,
        )
        timing["open_chat_semantic_candidate"] = semantic_match
        if semantic_match.get("ambiguous"):
            return finish(False, "semantic_candidate_ambiguous")
        if semantic_candidate is None:
            return finish(False, "semantic_private_candidate_not_found")
        activation_started = _sidecar_timing_start(timing, "open_chat_activate_semantic_session")
        opened = activate_session_candidate(
            hwnd,
            semantic_candidate,
            target=clean_semantic_target,
            exact=False,
            geometry=geometry,
            default_click_x=session_click_x,
            artifact_dir=artifact_dir,
        )
        _sidecar_timing_finish(timing, "open_chat_activate_semantic_session", activation_started)
        _sidecar_timing_merge_prefixed(timing, "open_chat_semantic", _LAST_SESSION_ACTIVATION_TIMING)
        if opened:
            _LAST_RPA_ACTION_STATE["active_session_key"] = str(semantic_candidate.get("session_key") or "")
            _LAST_RPA_ACTION_STATE["active_target"] = clean_semantic_target
        return finish(opened, "semantic_candidate_activated" if opened else "semantic_candidate_not_confirmed")
    if clean_session_key:
        find_started = _sidecar_timing_start(timing, "open_chat_find_session_key")
        keyed = find_session_candidate_by_key(sessions, clean_session_key)
        _sidecar_timing_finish(timing, "open_chat_find_session_key", find_started)
        if keyed is not None and semantic_target:
            keyed_admission = classify_c2_conversation_title(
                keyed.get("raw_title") or keyed.get("name") or "",
                semantic_target,
            )
            timing["open_chat_session_key_c2_admission"] = keyed_admission
            if not keyed_admission.get("admission_allowed"):
                keyed = None
                timing["open_chat_session_key_rejected_by_c2_admission"] = True
        if keyed is None:
            semantic_started = _sidecar_timing_start(timing, "open_chat_find_semantic_candidate")
            keyed, semantic_match = find_unique_session_candidate_by_semantics(
                sessions,
                target=target,
                semantic_target=semantic_target,
            )
            _sidecar_timing_finish(timing, "open_chat_find_semantic_candidate", semantic_started)
            timing["open_chat_session_key_drift_detected"] = True
            timing["open_chat_semantic_candidate"] = semantic_match
            if keyed is None:
                reason = (
                    "session_key_drift_semantic_candidate_ambiguous"
                    if semantic_match.get("ambiguous")
                    else "session_key_drift_semantic_candidate_not_found"
                )
                return finish(False, reason)
            timing["open_chat_session_key_reacquired"] = {
                "old_session_key": clean_session_key,
                "new_session_key": str(keyed.get("session_key") or ""),
                "name": str(keyed.get("name") or ""),
                "matched_by": semantic_match.get("matched_by"),
            }
        activation_started = _sidecar_timing_start(timing, "open_chat_activate_session")
        activation_target = str(semantic_target or target).strip()
        opened = activate_session_candidate(
            hwnd,
            keyed,
            target=activation_target,
            exact=False if semantic_target else exact,
            geometry=geometry,
            default_click_x=session_click_x,
            artifact_dir=artifact_dir,
        )
        _sidecar_timing_finish(timing, "open_chat_activate_session", activation_started)
        _sidecar_timing_merge_prefixed(timing, "open_chat", _LAST_SESSION_ACTIVATION_TIMING)
        if opened:
            _LAST_RPA_ACTION_STATE["active_session_key"] = str(keyed.get("session_key") or clean_session_key)
            _LAST_RPA_ACTION_STATE["active_target"] = target
        if timing.get("open_chat_session_key_drift_detected"):
            reason = "semantic_candidate_reacquired" if opened else "semantic_candidate_not_confirmed"
        else:
            reason = "session_key_candidate_activated" if opened else "session_key_candidate_not_confirmed"
        return finish(opened, reason)
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
    screenshot: Any | None = None,
    ocr_items: list[dict[str, Any]] | None = None,
    screenshot_path: str = "",
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
    supplied_frame = screenshot is not None and ocr_items is not None
    if supplied_frame:
        path = str(screenshot_path or "")
        timing["validate_active_send_target_frame_reused"] = True
    else:
        capture_started = _sidecar_timing_start(timing, "validate_active_send_target_capture")
        screenshot, path = capture_wechat(hwnd, artifact_dir=artifact_dir, label="send_guard")
        _sidecar_timing_finish(timing, "validate_active_send_target_capture", capture_started)
        timing["validate_active_send_target_frame_reused"] = False
    timing["validate_active_send_target_screenshot_width"] = int(getattr(screenshot, "size", (0, 0))[0] or 0)
    timing["validate_active_send_target_screenshot_height"] = int(getattr(screenshot, "size", (0, 0))[1] or 0)
    if supplied_frame:
        ocr_items = list(ocr_items or [])
        ocr_source = "supplied_full_frame"
        roi_blank_render = None
    else:
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
    if ocr_source in {"full", "full_fallback", "supplied_full_frame"}:
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
    title_evidence = active_chat_title_evidence(ocr_items, screenshot.size, target=target, exact=exact)
    active_match = bool(title_evidence.get("matched"))
    _sidecar_timing_finish(timing, "validate_active_send_target_active_match", active_match_started)
    if supplied_frame and not active_match:
        # Reuse the business screenshot, not its potentially incomplete
        # full-frame OCR result. A small right-panel ROI preserves the strict
        # title guard without paying for another window capture.
        title_roi_bounds = active_send_target_roi_bounds(getattr(screenshot, "size", (0, 0)))
        timing["validate_active_send_target_supplied_frame_title_roi_bounds"] = list(title_roi_bounds)
        title_roi_started = _sidecar_timing_start(
            timing,
            "validate_active_send_target_supplied_frame_title_roi_ocr",
        )
        title_roi_items = run_ocr_on_screen_region(
            screenshot,
            title_roi_bounds,
            purpose="active_send_target_supplied_frame_title_roi",
            source="validate_active_send_target",
        )
        _sidecar_timing_finish(
            timing,
            "validate_active_send_target_supplied_frame_title_roi_ocr",
            title_roi_started,
        )
        timing["validate_active_send_target_supplied_frame_title_roi_ocr_count"] = len(title_roi_items)
        title_evidence = active_chat_title_evidence(
            title_roi_items,
            screenshot.size,
            target=target,
            exact=exact,
        )
        active_match = bool(title_evidence.get("matched"))
        timing["validate_active_send_target_supplied_frame_title_roi_match"] = bool(active_match)
    semantic_target = exact_c2_remark_code_target(target)
    if semantic_target and title_evidence.get("short_code_confirmed") is True and title_evidence.get("admission_allowed") is not True:
        return finish({
            "ok": False,
            "online": True,
            "reason": "c2_private_admission_failed",
            "requested_target": target,
            "confirmed_target": "",
            "confirmation_confidence": "failed",
            "conversation_type": str(title_evidence.get("conversation_type") or "unknown"),
            "conversation_type_reason": str(title_evidence.get("reason") or "c2_private_admission_failed"),
            "raw_title": str(title_evidence.get("raw_title") or ""),
            "conversation_type_evidence": title_evidence,
            "geometry": geometry,
            "screenshot_path": path,
            "error": "The requested short-code target is not a confirmed private chat.",
        })
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
            "conversation_type": str(title_evidence.get("conversation_type") or "unknown"),
            "conversation_type_reason": str(title_evidence.get("reason") or "target_title_not_confirmed"),
            "raw_title": str(title_evidence.get("raw_title") or ""),
            "conversation_type_evidence": title_evidence,
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
        "conversation_type": str(title_evidence.get("conversation_type") or "unknown"),
        "conversation_type_reason": str(title_evidence.get("reason") or ""),
        "raw_title": str(title_evidence.get("raw_title") or ""),
        "conversation_type_evidence": title_evidence,
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


def normalized_send_confirmation_text(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "")).strip()


SEND_CONTEXT_ROW_KINDS = {
    "text_bubble",
    "voice_transcript",
    "image_bubble",
    "system_message",
}


def _send_context_anchor_value(value: Any) -> str:
    if value in (None, "", [], {}):
        return ""
    if isinstance(value, (dict, list)):
        return hashlib.sha256(
            json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            )
        ).hexdigest()[:24]
    return str(value).strip()


def send_context_entry_from_observation(observation: dict[str, Any]) -> dict[str, str]:
    row_kind = str(observation.get("row_kind") or "").strip().lower()
    return {
        "row_kind": row_kind,
        "sender_role": str(observation.get("sender_role") or "").strip().lower(),
        "content_normalized": normalized_send_confirmation_text(
            observation.get("content_clean")
        ),
        "voice_anchor": _send_context_anchor_value(
            observation.get("parent_voice_anchor_key")
            or observation.get("voice_anchor_key")
        ),
        "image_anchor": _send_context_anchor_value(
            observation.get("image_physical_anchor")
        ),
    }


def build_send_context_guard(
    observations: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    sequence = [
        send_context_entry_from_observation(observation)
        for observation in (observations or [])
        if isinstance(observation, dict)
        and str(observation.get("row_kind") or "").strip().lower()
        in SEND_CONTEXT_ROW_KINDS
    ]
    serialized = json.dumps(
        sequence,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return {
        "schema_version": 1,
        "sequence": sequence,
        "sequence_sha256": hashlib.sha256(serialized.encode("utf-8")).hexdigest(),
        "message_count": len(sequence),
        "bottom": dict(sequence[-1]) if sequence else None,
    }


def parse_expected_send_context_guard(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    clean = str(value or "").strip()
    if not clean:
        return {}
    try:
        payload = json.loads(clean)
    except json.JSONDecodeError:
        return {}
    return dict(payload) if isinstance(payload, dict) else {}


def validate_send_context_guard(
    expected: dict[str, Any] | None,
    current: dict[str, Any] | None,
) -> dict[str, Any]:
    expected_payload = expected if isinstance(expected, dict) else {}
    current_payload = current if isinstance(current, dict) else {}
    expected_sequence = expected_payload.get("sequence")
    current_sequence = current_payload.get("sequence")
    if int(expected_payload.get("schema_version") or 0) != 1 or not isinstance(
        expected_sequence, list
    ):
        return {
            "ok": False,
            "reason": "expected_context_guard_missing_or_invalid",
            "error_code": "C3_SEND_CONTEXT_GUARD_REQUIRED",
        }
    if int(current_payload.get("schema_version") or 0) != 1 or not isinstance(
        current_sequence, list
    ):
        return {
            "ok": False,
            "reason": "current_context_guard_invalid",
            "error_code": "C3_SEND_CONTEXT_GUARD_INVALID",
        }
    if expected_sequence != current_sequence:
        return {
            "ok": False,
            "reason": "message_sequence_changed",
            "error_code": "C3_CONTEXT_CHANGED_BEFORE_SEND",
            "expected_message_count": len(expected_sequence),
            "current_message_count": len(current_sequence),
            "expected_bottom": expected_payload.get("bottom"),
            "current_bottom": current_payload.get("bottom"),
            "expected_sequence_sha256": expected_payload.get("sequence_sha256"),
            "current_sequence_sha256": current_payload.get("sequence_sha256"),
        }
    return {
        "ok": True,
        "reason": "message_sequence_unchanged",
        "message_count": len(current_sequence),
        "sequence_sha256": current_payload.get("sequence_sha256"),
        "bottom": current_payload.get("bottom"),
    }


def send_reply_match_count(messages: list[dict[str, Any]], text: str) -> int:
    expected = normalized_send_confirmation_text(text)
    if not expected:
        return 0
    count = 0
    for message in messages:
        role = str(message.get("sender_role") or message.get("sender") or "").strip().lower()
        if role not in {"self", "sales"}:
            continue
        actual = normalized_send_confirmation_text(message.get("content"))
        if actual == expected:
            count += 1
    return count


def capture_send_fact_snapshot(
    hwnd: int,
    *,
    target: str,
    text: str,
    exact: bool,
    artifact_dir: str | None,
    label: str,
) -> dict[str, Any]:
    screenshot, path = capture_wechat(hwnd, artifact_dir=artifact_dir, label=label)
    ocr_items = run_ocr_traced(
        screenshot,
        f"{label}_full_ocr",
        source="capture_send_fact_snapshot",
    )
    geometry = get_window_geometry(hwnd)
    validation = validate_active_send_target(
        hwnd,
        target,
        exact=exact,
        artifact_dir=artifact_dir,
        screenshot=screenshot,
        ocr_items=ocr_items,
        screenshot_path=path,
    )
    messages = parse_messages_from_ocr(
        ocr_items,
        screenshot.size,
        target=target,
        screenshot=screenshot,
    )
    observations = build_message_observations_v3(messages)
    input_region = input_text_region_state(screenshot, ocr_items, geometry=geometry)
    message_sequence = [
        {
            "sequence_index": index,
            "observation_id": str(observation.get("observation_id") or ""),
            "row_kind": str(observation.get("row_kind") or ""),
            "sender_role": str(observation.get("sender_role") or ""),
            "content": str(observation.get("content_clean") or ""),
            "content_normalized": normalized_send_confirmation_text(
                observation.get("content_clean")
            ),
            "bubble_rect": observation.get("bubble_rect"),
        }
        for index, observation in enumerate(observations)
        if isinstance(observation, dict)
        and str(observation.get("row_kind") or "")
        in {"text_bubble", "voice_transcript", "image_bubble", "system_message"}
    ]
    return {
        "ok": bool(validation.get("ok") and active_send_guard_is_strong(validation)),
        "screenshot_path": path,
        "validation": validation,
        "input_region": input_region,
        "matching_self_message_count": send_reply_match_count(messages, text),
        "message_count": len(messages),
        "observations": observations,
        "send_context_guard": build_send_context_guard(observations),
        "message_sequence": message_sequence,
        "matching_self_messages": [
            {
                "content": str(message.get("content") or ""),
                "rect": message.get("rect"),
            }
            for message in messages
            if str(message.get("sender_role") or message.get("sender") or "").strip().lower() in {"self", "sales"}
            and normalized_send_confirmation_text(message.get("content"))
            == normalized_send_confirmation_text(text)
        ],
    }


def find_new_matching_self_message(
    baseline_sequence: list[dict[str, Any]],
    current_sequence: list[dict[str, Any]],
    text: str,
) -> dict[str, Any] | None:
    """Find an added bottom-most self bubble by ordered visual facts, not counts."""

    def signature(item: dict[str, Any]) -> tuple[str, str, str]:
        return (
            str(item.get("row_kind") or ""),
            str(item.get("sender_role") or ""),
            str(item.get("content_normalized") or ""),
        )

    before = [item for item in baseline_sequence if isinstance(item, dict)]
    after = [item for item in current_sequence if isinstance(item, dict)]
    before_signatures = [signature(item) for item in before]
    after_signatures = [signature(item) for item in after]
    rows = len(before_signatures) + 1
    columns = len(after_signatures) + 1
    lcs = [[0] * columns for _ in range(rows)]
    for before_index in range(len(before_signatures) - 1, -1, -1):
        for after_index in range(len(after_signatures) - 1, -1, -1):
            if before_signatures[before_index] == after_signatures[after_index]:
                lcs[before_index][after_index] = 1 + lcs[before_index + 1][after_index + 1]
            else:
                lcs[before_index][after_index] = max(
                    lcs[before_index + 1][after_index],
                    lcs[before_index][after_index + 1],
                )
    matched_after: set[int] = set()
    before_index = 0
    after_index = 0
    while before_index < len(before_signatures) and after_index < len(after_signatures):
        if before_signatures[before_index] == after_signatures[after_index]:
            matched_after.add(after_index)
            before_index += 1
            after_index += 1
        elif lcs[before_index + 1][after_index] >= lcs[before_index][after_index + 1]:
            before_index += 1
        else:
            after_index += 1

    expected = normalized_send_confirmation_text(text)
    candidates = [
        (index, item)
        for index, item in enumerate(after)
        if index not in matched_after
        and str(item.get("row_kind") or "") == "text_bubble"
        and str(item.get("sender_role") or "") in {"self", "sales"}
        and str(item.get("content_normalized") or "") == expected
    ]
    if not candidates:
        return None
    candidate_index, candidate = candidates[-1]
    later_chat_messages = [
        item
        for item in after[candidate_index + 1 :]
        if str(item.get("row_kind") or "")
        in {"text_bubble", "voice_transcript", "image_bubble"}
    ]
    if later_chat_messages:
        return None
    return dict(candidate)


def confirm_reply_sent(
    hwnd: int,
    *,
    target: str,
    text: str,
    exact: bool,
    baseline_match_count: int,
    baseline_message_sequence: list[dict[str, Any]] | None = None,
    artifact_dir: str | None = None,
    max_attempts: int = 6,
) -> dict[str, Any]:
    attempts: list[dict[str, Any]] = []
    for attempt in range(1, max(1, max_attempts) + 1):
        if attempt > 1:
            time.sleep(min(1.2, 0.35 + attempt * 0.12))
        try:
            snapshot = capture_send_fact_snapshot(
                hwnd,
                target=target,
                text=text,
                exact=exact,
                artifact_dir=artifact_dir,
                label=f"send_result_confirm_{attempt}",
            )
        except Exception as exc:
            attempts.append({"attempt": attempt, "ok": False, "error": repr(exc)})
            continue
        snapshot["attempt"] = attempt
        attempts.append(snapshot)
        input_blank = not bool((snapshot.get("input_region") or {}).get("has_visible_text"))
        confirmed_message = find_new_matching_self_message(
            list(baseline_message_sequence or []),
            list(snapshot.get("message_sequence") or []),
            text,
        )
        if snapshot.get("ok") and input_blank and confirmed_message:
            confirmed_observation = next(
                (
                    dict(item)
                    for item in (snapshot.get("observations") or [])
                    if isinstance(item, dict)
                    and str(item.get("observation_id") or "")
                    == str(confirmed_message.get("observation_id") or "")
                ),
                None,
            )
            return {
                "ok": True,
                "reason": "new_stable_self_bubble_and_empty_input",
                "attempt": attempt,
                "baseline_match_count": int(baseline_match_count),
                "matching_self_message_count": int(snapshot.get("matching_self_message_count") or 0),
                "confirmed_message": confirmed_message,
                "confirmed_observation": confirmed_observation,
                "snapshot": snapshot,
            }
    last = attempts[-1] if attempts else {}
    return {
        "ok": False,
        "reason": "send_result_not_proven",
        "error_code": "SEND_RESULT_UNKNOWN",
        "baseline_match_count": int(baseline_match_count),
        "matching_self_message_count": int(last.get("matching_self_message_count") or 0),
        "input_region": last.get("input_region"),
        "attempts": attempts,
    }


def get_window_geometry(hwnd: int) -> dict[str, int]:
    return win32_ocr_window_metrics.get_window_geometry(hwnd, win32gui_module=win32gui)


def screen_point_to_client(hwnd: int, screen_x: int, screen_y: int) -> tuple[int, int]:
    if win32gui is None or not hasattr(win32gui, "ScreenToClient"):
        raise RuntimeError("win32_screen_to_client_unavailable")
    client_x, client_y = win32gui.ScreenToClient(
        int(hwnd),
        (int(screen_x), int(screen_y)),
    )
    return int(client_x), int(client_y)


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


def active_chat_title_evidence(
    ocr_items: list[dict[str, Any]],
    image_size: tuple[int, int],
    *,
    target: str,
    exact: bool,
) -> dict[str, Any]:
    normalized_target = normalize_session_name(target)
    if not normalized_target:
        return {
            "matched": False,
            "conversation_type": "unknown",
            "reason": "target_empty",
            "raw_title": "",
            "title_candidates": [],
            "short_code_confirmed": False,
            "admission_allowed": False,
        }
    width, height = image_size
    split_x = session_split_x(width)
    title_left = active_chat_title_left_x(width)
    title_right = active_chat_title_right_x(width)
    title_top = active_chat_title_top_y(height)
    title_bottom = active_chat_title_bottom_y(height)
    x_tolerance = 24
    y_tolerance = 8
    title_items: list[dict[str, Any]] = []
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
        title_items.append({**item, "text": text})

    raw_candidates = [str(item.get("text") or "") for item in title_items]
    ordered = sorted(title_items, key=lambda item: (float(item.get("center_y") or 0), float(item.get("left") or 0)))
    combined_parts: list[str] = []
    combined_right = -1.0
    combined_y = -999.0
    for item in ordered:
        left = float(item.get("left") or 0)
        right = float(item.get("right") or 0)
        center_y = float(item.get("center_y") or 0)
        text = str(item.get("text") or "")
        if combined_parts and abs(center_y - combined_y) <= 12 and left >= combined_right - 4 and left - combined_right <= 64:
            combined_parts.append(text)
            combined_right = max(combined_right, right)
            continue
        if len(combined_parts) > 1:
            raw_candidates.append("".join(combined_parts))
        combined_parts = [text]
        combined_right = right
        combined_y = center_y
    if len(combined_parts) > 1:
        raw_candidates.append("".join(combined_parts))

    unique_candidates: list[str] = []
    for text in raw_candidates:
        if text and text not in unique_candidates:
            unique_candidates.append(text)
    requested_codes = extract_c2_remark_codes(normalized_target)
    normalized_code_target = re.sub(r"[^A-Z0-9]", "", normalized_target.upper())
    c2_short_code_target = (
        requested_codes[0]
        if len(requested_codes) == 1 and re.sub(r"[^A-Z0-9]", "", requested_codes[0]) == normalized_code_target
        else ""
    )
    matched_candidates: list[dict[str, Any]] = []
    for text in unique_candidates:
        variants = {
            text,
            strip_chat_unread_suffix(text),
            re.sub(r"^[：:.\s]+", "", text).strip(),
            normalize_chat_title_for_match(text),
        }
        admission = classify_c2_conversation_title(text, c2_short_code_target or normalized_target)
        if c2_short_code_target:
            matched = admission.get("short_code_confirmed") is True
        else:
            matched = any(session_name_matches(candidate, normalized_target, exact=exact) for candidate in variants)
        if matched:
            matched_candidates.append({"raw_title": text, "matched": matched, "admission": admission})
    if not matched_candidates:
        return {
            "matched": False,
            "conversation_type": "unknown",
            "reason": "target_title_not_confirmed",
            "raw_title": "",
            "title_candidates": unique_candidates,
            "short_code_confirmed": False,
            "admission_allowed": False,
        }

    matched_candidates.sort(key=lambda item: len(re.sub(r"\s+", "", str(item.get("raw_title") or ""))), reverse=True)
    strongest: list[dict[str, Any]] = []
    for item in matched_candidates:
        compact = re.sub(r"\s+", "", str(item.get("raw_title") or ""))
        if any(compact and compact in re.sub(r"\s+", "", str(other.get("raw_title") or "")) for other in strongest):
            continue
        strongest.append(item)
    types = {str((item.get("admission") or {}).get("conversation_type") or "unknown") for item in strongest}
    selected = strongest[0]
    result = dict(selected.get("admission") or {})
    if len(types) > 1:
        result.update(
            {
                "conversation_type": "unknown",
                "reason": "title_ocr_evidence_conflict",
                "admission_allowed": False,
            }
        )
    result.update(
        {
            "matched": bool(any(bool(item.get("matched")) for item in matched_candidates)),
            "raw_title": str(selected.get("raw_title") or ""),
            "title_candidates": unique_candidates,
            "matched_title_candidates": [str(item.get("raw_title") or "") for item in matched_candidates],
        }
    )
    return result


def active_chat_matches(ocr_items: list[dict[str, Any]], image_size: tuple[int, int], *, target: str, exact: bool) -> bool:
    evidence = active_chat_title_evidence(ocr_items, image_size, target=target, exact=exact)
    return bool(evidence.get("matched"))


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


def compact_ocr_items_for_report(ocr_items: list[dict[str, Any]], *, limit: int = 120) -> list[dict[str, Any]]:
    compacted: list[dict[str, Any]] = []
    for item in ocr_items[: max(0, int(limit))]:
        if not isinstance(item, dict):
            continue
        row: dict[str, Any] = {"text": str(item.get("text") or "")}
        for key in ("left", "top", "right", "bottom", "center_x", "center_y", "confidence"):
            if key in item:
                try:
                    row[key] = round(float(item.get(key) or 0), 3)
                except Exception:
                    row[key] = item.get(key)
        if item.get("ocr_source"):
            row["ocr_source"] = item.get("ocr_source")
        compacted.append(row)
    return compacted


def sidebar_visible_list_enhanced_ocr_items(
    screenshot: Any,
    image_size: tuple[int, int],
    *,
    ocr_runner: Callable[[Any], list[dict[str, Any]]] | None = None,
) -> list[dict[str, Any]]:
    if screenshot is None or not hasattr(screenshot, "crop"):
        return []
    width, height = image_size
    if width <= 0 or height <= 0:
        return []
    split_x = session_split_x(width)
    min_header_y = chat_header_cutoff_y(height)
    crop_left = 0
    crop_top = max(0, min_header_y - 14)
    crop_right = min(width, split_x + max(4, int(width * 0.02)))
    crop_bottom = max(crop_top + 1, height - 20)
    if crop_right <= crop_left or crop_bottom <= crop_top:
        return []
    try:
        crop = screenshot.crop((crop_left, crop_top, crop_right, crop_bottom)).convert("RGB")
        crop = ImageEnhance.Contrast(crop).enhance(1.65)
        crop = ImageEnhance.Sharpness(crop).enhance(1.4)
        scale = 2.0
        resampling = getattr(getattr(Image, "Resampling", Image), "LANCZOS", 1)
        enhanced = crop.resize((int(crop.width * scale), int(crop.height * scale)), resampling)
        runner = ocr_runner or run_ocr
        crop_items = runner(enhanced)
    except Exception:
        return []
    mapped: list[dict[str, Any]] = []
    for item in crop_items or []:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        if not text or not is_session_name_candidate(text):
            continue
        row = dict(item)
        for key in ("left", "right", "center_x"):
            if key in row:
                try:
                    row[key] = float(row[key]) / scale + crop_left
                except Exception:
                    pass
        for key in ("top", "bottom", "center_y"):
            if key in row:
                try:
                    row[key] = float(row[key]) / scale + crop_top
                except Exception:
                    pass
        row.setdefault("center_x", (float(row.get("left") or 0) + float(row.get("right") or 0)) / 2)
        row.setdefault("center_y", (float(row.get("top") or 0) + float(row.get("bottom") or 0)) / 2)
        row["ocr_source"] = "sidebar_visible_list_enhanced"
        mapped.append(row)
    return mapped


def session_list_ocr_items(
    screenshot: Any,
    base_items: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    items = [dict(item) for item in base_items if isinstance(item, dict)]
    existing_enhanced = [
        item for item in items if str(item.get("ocr_source") or "") == "sidebar_visible_list_enhanced"
    ]
    if existing_enhanced:
        return items, len(existing_enhanced)
    image_size = getattr(screenshot, "size", (0, 0))
    enhanced_items = sidebar_visible_list_enhanced_ocr_items(screenshot, image_size)
    if enhanced_items:
        items.extend(enhanced_items)
    return items, len(enhanced_items)


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

    min_session_row_gap = max(34, int(height * 0.048))
    candidate_rows: list[list[dict[str, Any]]] = []
    for item in sorted(candidates, key=lambda row: float(row["center_y"])):
        center_y = float(item["center_y"])
        if not candidate_rows or center_y - float(candidate_rows[-1][0]["center_y"]) >= min_session_row_gap:
            candidate_rows.append([item])
        else:
            candidate_rows[-1].append(item)

    sessions: list[dict[str, Any]] = []
    name_counts: dict[str, int] = {}
    for row_candidates in candidate_rows:
        item, remark_codes, c2_admission = select_session_row_title_candidate(row_candidates)
        center_y = float(item["center_y"])
        raw_title = normalize_ocr_text(item.get("text"))
        name = normalize_session_name(raw_title)
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
        row_fingerprint = session_row_fingerprint(item, duplicate_index=duplicate_index)
        sessions.append(
            {
                "name": name,
                "raw_title": raw_title,
                "session_key": rpa_session_key(name, row_fingerprint=row_fingerprint),
                "conversation_type": c2_admission.get("conversation_type"),
                "c2_conversation_type": c2_admission.get("conversation_type"),
                "c2_conversation_admission": c2_admission,
                "c2_remark_code_candidates": remark_codes if c2_admission.get("admission_allowed") else [],
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
    enrich_sessions_with_sidebar_signals(
        sessions,
        ocr_items,
        image_size,
        screenshot=screenshot,
        min_header_y=min_header_y,
        split_x=split_x,
    )
    return sessions


def select_session_row_title_candidate(
    row_candidates: list[dict[str, Any]],
) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    details: list[dict[str, Any]] = []
    distinct_codes: list[str] = []
    for item in row_candidates:
        raw_title = normalize_ocr_text(item.get("text"))
        codes = extract_c2_remark_codes(raw_title)
        for code in codes:
            if code not in distinct_codes:
                distinct_codes.append(code)
        details.append(
            {
                "item": item,
                "raw_title": raw_title,
                "codes": codes,
                "enhanced": str(item.get("ocr_source") or "") == "sidebar_visible_list_enhanced",
                "confidence": float(item.get("confidence") or 0),
            }
        )

    if len(distinct_codes) > 1:
        selected = max(
            (detail for detail in details if detail["codes"]),
            key=lambda detail: (
                not detail["enhanced"],
                detail["confidence"],
                len(detail["raw_title"]),
            ),
        )
        return selected["item"], [], {
            "conversation_type": "unknown",
            "reason": "multiple_remark_codes_in_visual_row",
            "raw_title": selected["raw_title"],
            "remark_code": "",
            "short_code_confirmed": False,
            "member_count_suffix": "",
            "admission_allowed": False,
            "detected_remark_codes": distinct_codes,
        }

    if len(distinct_codes) == 1:
        code = distinct_codes[0]
        code_details = [detail for detail in details if code in detail["codes"]]

        def code_candidate_priority(detail: dict[str, Any]) -> tuple[int, bool, float, int]:
            admission = classify_c2_conversation_title(detail["raw_title"], code)
            conversation_type = str(admission.get("conversation_type") or "unknown")
            type_priority = {"group": 3, "private": 2, "unknown": 1}.get(conversation_type, 0)
            return (
                type_priority,
                not detail["enhanced"],
                detail["confidence"],
                len(detail["raw_title"]),
            )

        selected = max(code_details, key=code_candidate_priority)
        admission = classify_c2_conversation_title(selected["raw_title"], code)
        return selected["item"], [code] if admission.get("admission_allowed") else [], admission

    selected = min(
        details,
        key=lambda detail: (
            float(detail["item"].get("center_y") or 0),
            detail["enhanced"],
            -detail["confidence"],
        ),
    )
    return selected["item"], [], classify_c2_conversation_title(selected["raw_title"], "")


def rpa_session_key(
    name: str,
    *,
    row_fingerprint: dict[str, Any] | None = None,
) -> str:
    fingerprint = row_fingerprint if isinstance(row_fingerprint, dict) else {}
    duplicate = str(fingerprint.get("duplicate_discriminator") or "").strip()
    # This is physical-row evidence for duplicate detection and compatibility.
    # C2 target identity is remark_code; no locate/read/send decision may rely
    # on this key when a valid remark_code is available.
    seed = json.dumps(["private", str(name or ""), duplicate], ensure_ascii=False, sort_keys=True)
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


def call_event_text_like(text: str) -> bool:
    compact = re.sub(r"\s+", "", normalize_ocr_text(text))
    return bool(
        re.fullmatch(
            r"(?:语音|视频)?通话时长[:：]?\d{1,3}:\d{2}(?:口|□|▢|▣|[^0-9一-鿿]{0,3})?",
            compact,
        )
    )


def parse_messages_from_ocr(
    ocr_items: list[dict[str, Any]],
    image_size: tuple[int, int],
    *,
    target: str,
    screenshot: Any | None = None,
) -> list[dict[str, Any]]:
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
        rect = {
            "left": int(float(item.get("left") or 0)),
            "top": int(float(item.get("top") or 0)),
            "right": int(float(item.get("right") or 0)),
            "bottom": int(float(item.get("bottom") or 0)),
        }
        avatar_alignment = message_row_avatar_role_details(
            screenshot,
            [rect["left"], rect["top"], rect["right"], rect["bottom"]],
            image_size,
        )
        avatar_role = str(avatar_alignment.get("role") or "")
        top_edge_guard = header_cutoff + max(4, int(height * 0.008))
        if rect["top"] <= top_edge_guard and not avatar_role and not voice_duration_item_like(item):
            continue
        if avatar_role in {"self", "customer"}:
            geometry_evidence = list(side_details.get("evidence") or [])
            side_details = {
                "side": avatar_role,
                "confidence": 0.98,
                "algorithm": "wechat_avatar_row_structure_v2",
                "evidence": [
                    *geometry_evidence,
                    f"avatar_row_role={avatar_role}",
                    "avatar_row_structure_confirmed",
                ],
            }
        side = str(side_details.get("side") or "unknown")
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
                "avatar_alignment": avatar_alignment,
            }
        )

    file_card_footers = [
        row
        for row in rows
        if str(row.get("text") or "").strip() in FILE_CARD_FOOTER_TEXTS
    ]
    if file_card_footers:
        rows = [
            row
            for row in rows
            if not any(
                str(row.get("side") or "unknown") == str(footer.get("side") or "unknown")
                and -8.0 <= float(footer.get("top") or 0) - float(row.get("bottom") or 0) <= 140.0
                and abs(float(row.get("left") or 0) - float(footer.get("left") or 0)) <= 56.0
                for footer in file_card_footers
            )
        ]

    grouped: list[list[dict[str, Any]]] = []
    for item in sorted(rows, key=lambda row: (float(row["center_y"]), float(row["left"]))):
        side = str(item.get("side") or classify_message_side(item, width=width))
        if not grouped:
            grouped.append([{**item, "side": side}])
            continue
        previous = grouped[-1][-1]
        previous_side = str(previous.get("side") or "unknown")
        vertical_gap = float(item["top"]) - float(previous["bottom"])
        if message_line_continues_voice_transcript_group(item, grouped[-1], vertical_gap):
            evidence = list(item.get("sender_role_evidence") or [])
            evidence.append("voice_transcript_inherits_parent_role")
            parent_side = str(grouped[-1][0].get("side") or previous_side)
            grouped[-1].append({**item, "side": parent_side, "sender_role_evidence": evidence})
            continue
        if side != "self" and previous_side == "self" and message_line_continues_previous_self_bubble(item, previous, vertical_gap):
            evidence = list(item.get("sender_role_evidence") or [])
            evidence.append("self_continuation_from_previous_line")
            grouped[-1].append({**item, "side": "self", "sender_role_evidence": evidence})
            continue
        previous_height = max(1.0, float(previous.get("bottom") or 0) - float(previous.get("top") or 0))
        item_height = max(1.0, float(item.get("bottom") or 0) - float(item.get("top") or 0))
        same_bubble_line_gap = min(merge_vertical_gap, max(7.0, min(previous_height, item_height) * 0.45))
        previous_avatar_role = str((previous.get("avatar_alignment") or {}).get("role") or "")
        current_avatar_role = str((item.get("avatar_alignment") or {}).get("role") or "")
        voice_transcript_continuation = bool(
            voice_duration_item_like(previous)
            and not voice_duration_item_like(item)
            and not current_avatar_role
            and vertical_gap <= max(42.0, height * 0.055)
        )
        starts_new_avatar_row = bool(
            previous_avatar_role
            and current_avatar_role
            and vertical_gap > 3.0
        )
        if voice_transcript_continuation and not starts_new_avatar_row:
            evidence = list(item.get("sender_role_evidence") or [])
            evidence.append("voice_transcript_inherits_parent_role")
            grouped[-1].append({**item, "side": previous_side, "sender_role_evidence": evidence})
        elif previous_side == side and vertical_gap <= same_bubble_line_gap and not starts_new_avatar_row:
            grouped[-1].append({**item, "side": side})
        else:
            grouped.append([{**item, "side": side}])

    messages: list[dict[str, Any]] = []
    for group in grouped:
        is_untranscribed_voice = message_group_is_untranscribed_voice_placeholder(group)
        is_voice_transcript = False
        if message_group_is_voice_duration_only(group) and not is_untranscribed_voice:
            continue
        raw_content = "\n".join(str(item.get("text") or "").strip() for item in group if str(item.get("text") or "").strip())
        content = normalize_message_content(raw_content)
        voice_duration_text = message_group_voice_duration_text(group)
        if is_untranscribed_voice:
            content = f"[语音] {voice_duration_text or ''}".strip()
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
        if is_untranscribed_voice:
            content = f"[语音] {voice_duration_text or ''}".strip()
        if not content:
            continue
        if voice_duration_prefix_removed:
            quality_flags.append("voice_duration_prefix_removed")
            is_voice_transcript = True
        if is_untranscribed_voice:
            quality_flags.append("untranscribed_voice_placeholder")
        is_call_event = call_event_text_like(content)
        if is_call_event:
            quality_flags.append("non_chat_call_event")
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
        avatar_alignment = next(
            (
                item.get("avatar_alignment")
                for item in group
                if isinstance(item.get("avatar_alignment"), dict) and item.get("avatar_alignment", {}).get("role")
            ),
            group[0].get("avatar_alignment") if isinstance(group[0].get("avatar_alignment"), dict) else {},
        )
        if screenshot is not None and str(avatar_alignment.get("role") or "") not in {"self", "customer"}:
            continue
        record = {
            "id": f"win32_ocr:{digest}",
            "type": "voice" if (is_untranscribed_voice or is_voice_transcript) else ("system" if is_call_event else "text"),
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
            "avatar_alignment": avatar_alignment,
        }
        if is_untranscribed_voice or is_voice_transcript:
            record["voice_duration_text"] = voice_duration_text
            voice_seconds = voice_duration_seconds_from_text(voice_duration_text)
            if voice_seconds is not None:
                record["voice_duration"] = voice_seconds
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
    return attach_structural_voice_anchor_keys(messages)


def classify_message_side(item: dict[str, Any], *, width: int) -> str:
    return str(classify_message_side_details(item, width=width).get("side") or "unknown")


def message_line_continues_previous_self_bubble(item: dict[str, Any], previous: dict[str, Any], vertical_gap: float) -> bool:
    if vertical_gap < -4.0:
        return False
    previous_height = max(1.0, float(previous.get("bottom") or 0) - float(previous.get("top") or 0))
    current_height = max(1.0, float(item.get("bottom") or 0) - float(item.get("top") or 0))
    gap_limit = max(8.0, min(14.0, max(previous_height, current_height) * 0.65))
    if vertical_gap > gap_limit:
        return False
    previous_left = float(previous.get("left") or 0)
    current_left = float(item.get("left") or 0)
    return abs(current_left - previous_left) <= 32.0


def message_line_continues_voice_transcript_group(
    item: dict[str, Any],
    group: list[dict[str, Any]],
    vertical_gap: float,
) -> bool:
    if len(group) < 2 or vertical_gap < -4.0 or vertical_gap > 16.0:
        return False
    if voice_duration_item_like(item):
        return False
    if not any(voice_duration_item_like(candidate) for candidate in group):
        return False
    if not any(not voice_duration_item_like(candidate) for candidate in group):
        return False
    avatar_role = str((item.get("avatar_alignment") or {}).get("role") or "")
    if avatar_role in {"self", "customer"}:
        return False
    previous_text_line = next(
        (candidate for candidate in reversed(group) if not voice_duration_item_like(candidate)),
        None,
    )
    if not isinstance(previous_text_line, dict):
        return False
    previous_left = float(previous_text_line.get("left") or 0)
    current_left = float(item.get("left") or 0)
    return abs(current_left - previous_left) <= 36.0


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


def extract_c2_remark_codes(*values: Any) -> list[str]:
    return win32_ocr_text.extract_c2_remark_codes(*values)


def exact_c2_remark_code_target(value: Any) -> str:
    text = str(value or "").strip().upper()
    codes = extract_c2_remark_codes(text)
    normalized = re.sub(r"[^A-Z0-9]", "", text)
    return codes[0] if len(codes) == 1 and codes[0] == normalized else ""


def classify_c2_conversation_title(raw_title: Any, remark_code: Any) -> dict[str, Any]:
    return win32_ocr_text.classify_c2_conversation_title(raw_title, remark_code)


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
    fixed_origin = env_flag("WECHAT_WIN32_OCR_WINDOW_FIXED_ORIGIN", default=False)
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
    if bool(request.get("current_only")):
        argv.append("--current-only")
    target = str(request.get("target") or "").strip()
    if target:
        argv.extend(["--target", target])
    session_key = str(request.get("session_key") or "").strip()
    if session_key:
        argv.extend(["--session-key", session_key])
    text = str(request.get("text") or "")
    if action == "send" and text:
        argv.extend(["--text", text])
    expected_context_guard = request.get("expected_context_guard")
    if action == "send" and isinstance(expected_context_guard, dict):
        argv.extend(
            [
                "--expected-context-guard",
                json.dumps(expected_context_guard, ensure_ascii=True, sort_keys=True),
            ]
        )
    action_journal = str(request.get("action_journal") or "").strip()
    if action_journal and (
        action in {"send", "voice-transcribe"}
        or action in ADD_FRIEND_ROUTES
    ):
        argv.extend(["--action-journal", action_journal])
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
    parser.add_argument("--visible-session-candidate", default="", help="JSON row candidate from the same Worker visible-session scan.")
    parser.add_argument("--text", help="Message text for send.")
    parser.add_argument("--phone", default="", help="Phone number for add-friend.")
    parser.add_argument("--wechat", default="", help="WeChat ID for add-friend fallback.")
    parser.add_argument("--verify-message", default="", help="Required add-friend verification message for the entry-click route.")
    parser.add_argument("--remark-name", default="", help="Required WeChat remark name for the entry-click route.")
    parser.add_argument("--remark-code", default="", help="Required system remark code that must be included in remark-name.")
    parser.add_argument("--calibration-only", action="store_true", help="For add-friend routes, capture/OCR/locate/report without clicking.")
    parser.add_argument("--exact", action="store_true", help="Use exact chat name matching.")
    parser.add_argument(
        "--current-only",
        action="store_true",
        help="For send, validate the current chat only and never search or switch sessions.",
    )
    parser.add_argument(
        "--expected-context-guard",
        default="",
        help="JSON context guard from the final C2 read; required before C2-C3 send.",
    )
    parser.add_argument(
        "--action-journal",
        default="",
        help="Worker-owned JSON journal for an irreversible send, voice, or image action.",
    )
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
    parser.add_argument("--excluded-voice-anchor-keys", default="[]", help="JSON list of persistent voice anchors that must not be clicked.")
    parser.add_argument(
        "--capture-initial-messages",
        action="store_true",
        help="Reuse the post-open title-confirmation frame as the first message read.",
    )
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
    try:
        payload = run_sidecar_cli()
    except Exception as exc:
        payload = exception_payload_for_sidecar(exc, state="win32_ocr_failed")
    print(json.dumps(payload, ensure_ascii=True))
    raise SystemExit(0 if bool(payload.get("ok")) else 1)
