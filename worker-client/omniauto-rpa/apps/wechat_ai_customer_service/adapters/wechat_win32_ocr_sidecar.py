"""Windows Win32/OCR sidecar for the WeChat desktop recorder.

This adapter is designed as the primary transport because it relies only on
the top-level Win32 window, screenshots, OCR, clipboard paste, and guarded
click/input flows. Every physical action uses a per-frame dynamic layout
snapshot and the shared coordinate converter. No reference-resolution
coordinate model is retained or allowed to authorize a physical click.
"""

from __future__ import annotations

import argparse
import ctypes
from difflib import SequenceMatcher
import hashlib
import io
import json
import os
import random
import re
import subprocess
import sys
import time
import unicodedata
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
    semantic_invite_form_targets,
)
from apps.wechat_ai_customer_service.adapters.add_friend_locator import (
    LOCATOR_RESULT_FIELDS,
    make_locator_result,
    ocr_item_locator,
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
from apps.wechat_ai_customer_service.adapters.wechat_win32_ocr import window_layout as win32_ocr_layout

try:
    from rapidocr_onnxruntime import RapidOCR
    _OCR_IMPORT_ERROR = ""
except Exception as exc:  # pragma: no cover - OCR is only needed for live sidecar actions.
    RapidOCR = None  # type: ignore[assignment]
    _OCR_IMPORT_ERROR = repr(exc)


DEFAULT_MESSAGE_BOTTOM_EXCLUDE_PX = 95
OCR_MIN_CONFIDENCE = 0.45
SIDECAR_BASE_ACTIONS = ("status", "capabilities", "normalize-window", "sessions", "open-chat", "messages", "send", "recover-render", "voice-transcribe")
SIDECAR_ACTION_CHOICES = (*SIDECAR_BASE_ACTIONS, *ADD_FRIEND_ROUTES)
SEND_GUARD_PATH = PROJECT_ROOT / "runtime" / "wechat_win32_ocr_send_guard.json"
UI_ACTION_GUARD_PATH = PROJECT_ROOT / "runtime" / "wechat_win32_ocr_ui_action_guard.json"
UI_ACTION_AUDIT_PATH = PROJECT_ROOT / "runtime" / "wechat_win32_ocr_ui_actions.jsonl"
_LAST_ACTIVATE_MONOTONIC_BY_HWND: dict[int, float] = {}
_LAST_RPA_ACTION_STATE: dict[str, Any] = {}
_LAST_OPEN_CHAT_TIMING: dict[str, Any] = {}
_LAST_SESSION_ACTIVATION_TIMING: dict[str, Any] = {}
_LAYOUT_SNAPSHOT_STORE = win32_ocr_layout.LayoutSnapshotStore()
_LATEST_LAYOUT_SNAPSHOT_BY_HWND: dict[int, str] = {}
_LAYOUT_SNAPSHOT_ID_BY_IMAGE_ID: dict[int, str] = {}
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
DEFAULT_SEND_MODE = "visual_only"
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
C2_MESSAGE_LIMITS = dict(_C2_GENERATED_SCHEMA["message_limits"])
C2_SOURCE_MESSAGE_TRANSPORT_FIELDS = frozenset(
    str(value)
    for value in C2_MESSAGE_LIMITS[
        "source_message_transport_fields"
    ]
)
C2_VOICE_ACTION_BINDING_CONTRACT = dict(
    _C2_GENERATED_SCHEMA["voice_action_binding_contract"]
)
C2_FRAME_ACTION_BINDING_CONTAINER = str(
    C2_VOICE_ACTION_BINDING_CONTRACT["frame_binding_container"]
)
C2_FRAME_ACTION_BINDING_FIELDS = tuple(
    str(value)
    for value in C2_VOICE_ACTION_BINDING_CONTRACT[
        "frame_binding_fields"
    ]
)
C2_TARGET_LOCATION_RECOVERY_CONTRACT = dict(
    _C2_GENERATED_SCHEMA["target_location_recovery_contract"]
)
C2_VISIBLE_TARGET_STALE_AFTER_CLICK = str(
    C2_TARGET_LOCATION_RECOVERY_CONTRACT["error_code"]
)
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
DEFAULT_SEND_TRIGGER_MODE = win32_ocr_env.DEFAULT_SEND_TRIGGER_MODE
DEFAULT_STRICT_SEND_FOCUS_GUARD = True
DEFAULT_FOCUS_CLICK_FALLBACK = True
DEFAULT_ALLOW_UNKNOWN_FOREGROUND_GUARD = True
SEND_WINDOW_FOCUS_RECOVERY_DELAYS_SECONDS = (0.30, 0.70)
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
    parser.add_argument("--scan-id", default="", help="Correlation id for one sessions scan.")
    parser.add_argument(
        "--window-policy",
        choices=("normalize", "verify"),
        default="normalize",
        help="Normalize only at a UI Flow boundary; nested actions must use verify.",
    )
    parser.add_argument("--canonical-voice-action-id", default="")
    parser.add_argument("--reserved-worker-stable-id", default="")
    parser.add_argument("--voice-action-stage", choices=("prepare", "execute"), default="prepare")
    parser.add_argument("--pre-frame-id", default="")
    parser.add_argument("--selected-pre-observation-id", default="")
    parser.add_argument("--selected-action-token", default="")
    parser.add_argument("--selected-target-fingerprint", default="")
    parser.add_argument("--target", help="Chat name for messages/send.")
    parser.add_argument("--session-key", default="", help="Internal session key for row-level RPA targeting.")
    parser.add_argument(
        "--conversation-type",
        default="",
        help="Caller metadata only; C2 private admission still requires title OCR evidence.",
    )
    parser.add_argument("--target-mode", default="", help="Targeting mode for messages, e.g. search_by_remark_code.")
    parser.add_argument(
        "--expected-confirmed-self-text",
        default="",
        help=(
            "Locally confirmed AI reply text used only to recover an OCR-missed "
            "self text bubble during the next authorized read."
        ),
    )
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


def try_activate_visible_candidate_from_equivalent_frame(
    hwnd: int,
    *,
    candidate: dict[str, Any],
    remark_code: str,
    artifact_dir: str | None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "fast_path_attempted": False,
        "fast_path_used": False,
        "fallback_reason": "feature_disabled",
        "frame_digest_equal": False,
        "ocr_call_count": 0,
        "ocr_total_duration_ms": 0,
        "ui_click_performed": False,
    }
    if not env_flag("CHEJIN_C2_LOCATE_FRAME_REUSE_ENABLED", default=True):
        return result
    result["fast_path_attempted"] = True
    evidence = candidate.get("visible_frame_reuse_evidence")
    if not isinstance(evidence, dict):
        result["fallback_reason"] = "reuse_evidence_missing"
        return result
    required = (
        "hwnd",
        "geometry",
        "dpi_scale",
        "sidebar_sha256",
        "screenshot_sha256",
        "frame_id",
        "scan_id",
        "sidecar_run_id",
        "captured_monotonic",
        "candidate_bounds",
        "candidate_remark_code_candidates",
        "candidate_session_key",
        "ocr_result_sha256",
    )
    if any(evidence.get(key) in (None, "", {}) for key in required):
        result["fallback_reason"] = "reuse_evidence_incomplete"
        return result
    if any(
        re.fullmatch(r"[0-9a-f]{64}", str(evidence.get(key) or "").lower())
        is None
        for key in (
            "sidebar_sha256",
            "screenshot_sha256",
            "ocr_result_sha256",
        )
    ):
        result["fallback_reason"] = "reuse_evidence_digest_invalid"
        return result
    admission = (
        candidate.get("c2_conversation_admission")
        if isinstance(candidate.get("c2_conversation_admission"), dict)
        else {}
    )
    candidates = [
        str(value or "").strip().upper()
        for value in (candidate.get("c2_remark_code_candidates") or [])
        if str(value or "").strip()
    ]
    clean_remark = str(remark_code or "").strip().upper()
    if not (
        admission.get("admission_allowed") is True
        and str(admission.get("conversation_type") or "") == "private"
        and candidates == [clean_remark]
        and str(admission.get("remark_code") or "").strip().upper()
        == clean_remark
    ):
        result["fallback_reason"] = "candidate_identity_not_strict"
        return result
    evidence_candidates = [
        str(value or "").strip().upper()
        for value in (
            evidence.get("candidate_remark_code_candidates") or []
        )
        if str(value or "").strip()
    ]
    candidate_bounds = [
        float(candidate.get(key) or 0.0)
        for key in ("left", "top", "right", "bottom")
    ]
    evidence_bounds = [
        float(value or 0.0)
        for value in (evidence.get("candidate_bounds") or [])[:4]
    ]
    if not (
        evidence_candidates == [clean_remark]
        and len(evidence_bounds) == 4
        and evidence_bounds == candidate_bounds
        and str(evidence.get("candidate_session_key") or "")
        == str(candidate.get("session_key") or "")
    ):
        result["fallback_reason"] = "candidate_evidence_binding_mismatch"
        return result
    geometry = get_window_geometry(hwnd)
    if int(evidence.get("hwnd") or 0) != int(hwnd or 0):
        result["fallback_reason"] = "hwnd_changed"
        return result
    expected_geometry = {
        key: int((evidence.get("geometry") or {}).get(key) or 0)
        for key in ("left", "top", "right", "bottom", "width", "height")
    }
    current_geometry = {
        key: int(geometry.get(key) or 0)
        for key in ("left", "top", "right", "bottom", "width", "height")
    }
    if current_geometry != expected_geometry:
        result["fallback_reason"] = "geometry_changed"
        return result
    current_dpi = float(window_dpi_scale(hwnd))
    if abs(float(evidence.get("dpi_scale") or 0.0) - current_dpi) > 1e-9:
        result["fallback_reason"] = "dpi_changed"
        return result
    screenshot, screenshot_path = capture_wechat(
        hwnd,
        artifact_dir=artifact_dir,
        label="visible_frame_reuse_check",
    )
    current = immutable_frame_pixel_evidence(
        screenshot,
        hwnd=hwnd,
        geometry=geometry,
        screenshot_path=screenshot_path,
        layout_snapshot=layout_snapshot_for_image(screenshot),
    )
    sidebar_digest_equal = bool(
        str(current.get("sidebar_sha256") or "")
        == str(evidence.get("sidebar_sha256") or "")
    )
    full_frame_digest_equal = bool(
        str(current.get("screenshot_sha256") or "")
        == str(evidence.get("screenshot_sha256") or "")
    )
    # The reusable evidence belongs to the session-list observation. The
    # active chat viewport can legitimately render while the sidebar remains
    # byte-identical, so it is diagnostic evidence rather than an eligibility
    # condition. A changed sidebar always falls back before any click.
    result["frame_digest_equal"] = sidebar_digest_equal
    result["full_frame_digest_equal"] = full_frame_digest_equal
    result["current_frame_id"] = current.get("frame_id")
    result["source_frame_id"] = evidence.get("frame_id")
    if not sidebar_digest_equal:
        result["fallback_reason"] = "frame_digest_changed"
        return result
    try:
        session = dict(candidate)
        session.pop("visible_frame_reuse_evidence", None)
        opened = activate_session_candidate(
            hwnd,
            session,
            target=clean_remark,
            exact=False,
            geometry=geometry,
            default_click_x=0,
            artifact_dir=artifact_dir,
        )
    except Exception as exc:
        result["fallback_reason"] = "candidate_activation_exception"
        result["error"] = repr(exc)
        return result
    result["opened"] = bool(opened)
    result["ui_click_performed"] = bool(
        _LAST_SESSION_ACTIVATION_TIMING.get("activation_ui_click_performed")
        or _LAST_SESSION_ACTIVATION_TIMING.get("ui_click_performed")
    )
    result["fast_path_used"] = bool(
        opened or result["ui_click_performed"]
    )
    result["fallback_reason"] = (
        ""
        if result["fast_path_used"]
        else "candidate_rejected_before_click"
    )
    if not result["fast_path_used"]:
        return result
    validation = consume_recent_target_switch_validation(
        hwnd=hwnd,
        target=clean_remark,
        exact=False,
        session_key=str(candidate.get("session_key") or ""),
        require_session_key_match=False,
    )
    if validation is None:
        validation = validate_active_send_target(
            hwnd,
            clean_remark,
            exact=False,
            artifact_dir=artifact_dir,
        )
    result["validation"] = validation
    validation_timing = (
        validation.get("timing")
        if isinstance(validation, dict)
        and isinstance(validation.get("timing"), dict)
        else {}
    )
    result["ocr_call_count"] = int(
        validation_timing.get("validate_active_send_target_ocr_call_count")
        or validation_timing.get("ocr_call_count")
        or 0
    )
    result["ocr_total_duration_ms"] = int(
        validation_timing.get("validate_active_send_target_ocr_total_duration_ms")
        or validation_timing.get("ocr_total_duration_ms")
        or 0
    )
    return result


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
    fast_path = (
        try_activate_visible_candidate_from_equivalent_frame(
            hwnd,
            candidate=visible_candidate,
            remark_code=clean_remark_code,
            artifact_dir=artifact_dir,
        )
        if normalized_mode == "visible" and visible_candidate
        else {
            "fast_path_attempted": False,
            "fast_path_used": False,
            "fallback_reason": "visible_candidate_missing",
            "frame_digest_equal": False,
            "ocr_call_count": 0,
            "ocr_total_duration_ms": 0,
        }
    )
    targeting["visible_frame_reuse"] = fast_path
    if fast_path.get("fast_path_used"):
        opened = bool(fast_path.get("opened"))
        validation = (
            fast_path.get("validation")
            if isinstance(fast_path.get("validation"), dict)
            else None
        )
        global _LAST_OPEN_CHAT_TIMING
        _LAST_OPEN_CHAT_TIMING = {
            "opened": opened,
            "reason": (
                "visible_frame_reuse_candidate_activated"
                if opened
                else "visible_frame_reuse_candidate_not_confirmed"
            ),
            "fast_path_attempted": True,
            "fast_path_used": True,
            "fallback_reason": "",
            "frame_digest_equal": True,
            "ocr_call_count": fast_path.get("ocr_call_count", 0),
            "ocr_total_duration_ms": fast_path.get(
                "ocr_total_duration_ms", 0
            ),
            "visible_frame_reuse_ui_click_performed": bool(
                fast_path.get("ui_click_performed")
            ),
        }
    else:
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
        visible_click_performed = bool(
            normalized_mode == "visible"
            and any(
                key.endswith("_ui_click_performed") and value is True
                for key, value in _LAST_OPEN_CHAT_TIMING.items()
            )
        )
        title_evidence = (
            validation.get("conversation_type_evidence")
            if isinstance(validation, dict)
            and isinstance(
                validation.get("conversation_type_evidence"), dict
            )
            else validation
            if isinstance(validation, dict)
            else {}
        )
        active_title = normalize_ocr_text(
            title_evidence.get("raw_title")
            if isinstance(title_evidence, dict)
            else ""
        )
        target_remark_code_confirmed = (
            title_evidence.get("short_code_confirmed")
            if isinstance(title_evidence, dict)
            else None
        )
        stale_after_click = bool(
            visible_click_performed
            and not ambiguous_visible_target
            and admission_code == failure_error_code
            and active_title
            and target_remark_code_confirmed is False
        )
        if stale_after_click:
            targeting["stale_after_click"] = {
                "ui_click_performed": True,
                "active_title_nonempty": True,
                "target_remark_code_confirmed": False,
                "message_read_attempted": False,
                "media_action_attempted": False,
                "input_or_send_attempted": False,
                "safe_relocation_allowed": True,
            }
        return finish(
            ok=False,
            validation=validation,
            state=failure_state,
            error_code=(
                "C2_VISIBLE_TARGET_AMBIGUOUS"
                if ambiguous_visible_target
                else C2_VISIBLE_TARGET_STALE_AFTER_CLICK
                if stale_after_click
                else admission_code
            ),
            error=(
                "Visible session target was ambiguous; stop before search fallback."
                if ambiguous_visible_target
                else "The visible row moved before the click; the opened title is not the authorized target."
                if stale_after_click
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
    if action not in set(SIDECAR_ACTION_CHOICES):
        return {
            "ok": False,
            "online": False,
            "adapter": "win32_ocr",
            "state": "unsupported_action",
        }
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
    if not passive_probe and len(probe.get("visible_main_windows") or []) != 1:
        return {
            "ok": False,
            "online": False,
            "adapter": "win32_ocr",
            "state": "window_normalization_failed",
            "error_code": win32_ocr_layout.ERROR_WINDOW_NORMALIZATION_FAILED,
            "reason": "visible_main_window_not_unique",
            "visible_main_count": len(probe.get("visible_main_windows") or []),
            "window_probe": probe,
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
    active_window_required = action not in {"status", "capabilities"}
    if not passive_probe and active_window_required:
        blocking_windows: list[dict[str, Any]] = []
        for candidate in probe.get("visible_windows") or []:
            candidate_hwnd = int(candidate.get("hwnd") or 0)
            if not candidate_hwnd or candidate_hwnd == hwnd:
                continue
            try:
                candidate_geometry = get_window_geometry(candidate_hwnd)
            except Exception:
                candidate_geometry = {}
            if int(candidate_geometry.get("width") or 0) < 160 or int(candidate_geometry.get("height") or 0) < 80:
                continue
            blocking_windows.append(
                {
                    "hwnd": candidate_hwnd,
                    "title": str(candidate.get("title") or ""),
                    "class_name": str(candidate.get("class_name") or ""),
                    "geometry": candidate_geometry,
                }
            )
        if blocking_windows:
            return {
                "ok": False,
                "online": True,
                "adapter": "win32_ocr",
                "state": "window_normalization_failed",
                "error_code": win32_ocr_layout.ERROR_WINDOW_NORMALIZATION_FAILED,
                "reason": "blocking_wechat_popup_visible",
                "blocking_windows": blocking_windows,
                "window_probe": probe,
            }
        foreground_blank_dismissal = dismiss_blank_foreground_window_before_activation(hwnd, artifact_dir=args.artifact_dir)
        if foreground_blank_dismissal.get("attempted"):
            probe["foreground_blank_dismissal"] = foreground_blank_dismissal
        activate_window(hwnd)
        normalized_window = normalize_wechat_window(
            hwnd,
            allow_move=str(getattr(args, "window_policy", "normalize") or "normalize") == "normalize",
        )
        probe["window_normalization"] = normalized_window
        if not normalized_window.get("ok") or not normalized_window.get("enabled", True):
            return {
                "ok": False,
                "online": False,
                "adapter": "win32_ocr",
                "state": "window_normalization_failed",
                "error_code": str(
                    normalized_window.get("error_code")
                    or win32_ocr_layout.ERROR_WINDOW_NORMALIZATION_FAILED
                ),
                "reason": str(normalized_window.get("reason") or "window_normalization_failed"),
                "window_probe": probe,
            }
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
    if action == "normalize-window":
        normalization = probe.get("window_normalization") if isinstance(probe.get("window_normalization"), dict) else {}
        ready = bool(normalization.get("ok"))
        return {
            "ok": ready,
            "online": ready,
            "adapter": "win32_ocr",
            "state": "window_normalized" if ready else "window_normalization_failed",
            "error_code": str(
                normalization.get("error_code")
                or ("" if ready else win32_ocr_layout.ERROR_WINDOW_NORMALIZATION_FAILED)
            ),
            "reason": str(normalization.get("reason") or ("" if ready else "window_normalization_failed")),
            "window_normalization": normalization,
            "readiness": {
                "ok": True,
                "skipped": True,
                "reason": "deferred_to_business_action_pre_click_gate",
            } if ready else {},
            "window_probe": probe,
        }
    if action == "capabilities":
        return capabilities_payload(hwnd, probe, artifact_dir=args.artifact_dir)
    if action == "recover-render":
        return recover_blank_render_payload(hwnd, probe, artifact_dir=args.artifact_dir)
    if action == "sessions":
        return sessions_payload(
            hwnd,
            probe,
            artifact_dir=args.artifact_dir,
            scan_id=str(getattr(args, "scan_id", "") or "").strip(),
            sidecar_run_id=str(
                getattr(args, "sidecar_run_id", "") or ""
            ).strip(),
        )
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
            "window_context": build_c2_window_context(hwnd, probe),
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
                    parsed_messages = parse_current_chat_frame_messages(
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
                        expected_confirmed_self_text=str(
                            args.expected_confirmed_self_text or ""
                        ),
                        seed_snapshot=snapshot,
                    )
                    if initial_messages.get("ok"):
                        initial_messages["sidecar_run_id"] = clean_sidecar_run_id
                        initial_messages["target_mode"] = str(args.target_mode or "").strip().lower() or "visible"
                        initial_messages["remark_code"] = str(args.remark_code or "").strip()
                        initial_messages["authoritative_frame_source"] = "initial_read"
                        initial_messages["window_context"] = build_c2_window_context(hwnd, probe)
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
            expected_confirmed_self_text=str(
                args.expected_confirmed_self_text or ""
            ),
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
        payload["window_context"] = build_c2_window_context(hwnd, probe)
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
        voice_action_stage = str(
            getattr(args, "voice_action_stage", "prepare") or "prepare"
        ).strip()
        common_voice_args = {
            "target": args.target or "",
            "artifact_dir": args.artifact_dir,
            "expected_confirmed_self_text": str(
                args.expected_confirmed_self_text or ""
            ),
            "confirm_target": (
                confirmation_target if single_frame_confirmation else ""
            ),
            "confirm_exact": (
                False if clean_remark_code else bool(args.exact)
            ),
        }
        if voice_action_stage == "prepare":
            payload = prepare_voice_action_payload(
                hwnd,
                probe,
                **common_voice_args,
                excluded_voice_anchor_keys={
                    str(value).strip()
                    for value in parsed_excluded_voice_anchor_keys
                    if str(value).strip()
                },
            )
        else:
            payload = execute_voice_action_payload(
                hwnd,
                probe,
                **common_voice_args,
                action_journal_path=str(
                    getattr(args, "action_journal", "") or ""
                ).strip(),
                canonical_voice_action_id=str(
                    getattr(args, "canonical_voice_action_id", "") or ""
                ).strip(),
                reserved_worker_stable_id=str(
                    getattr(args, "reserved_worker_stable_id", "") or ""
                ).strip(),
                pre_frame_id=str(getattr(args, "pre_frame_id", "") or "").strip(),
                selected_pre_observation_id=str(
                    getattr(args, "selected_pre_observation_id", "") or ""
                ).strip(),
                selected_action_token=str(
                    getattr(args, "selected_action_token", "") or ""
                ).strip(),
                selected_target_fingerprint=str(
                    getattr(args, "selected_target_fingerprint", "") or ""
                ).strip(),
            )
        payload.setdefault(
            "observation_schema_version", C2_OBSERVATION_SCHEMA_VERSION
        )
        payload.setdefault(
            "contract_revision", C2_OBSERVATION_CONTRACT_REVISION
        )
        payload.setdefault(
            "contract_sha256", C2_OBSERVATION_CONTRACT_SHA256
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
        payload["window_context"] = build_c2_window_context(hwnd, probe)
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
        target_ready = {
            "ok": True,
            "attempts": 1,
            "validation": None,
            "opened": False,
            "timing": {
                "target_ready_current_only": True,
                "target_ready_session_switch_forbidden": True,
                "target_ready_deferred_to_send_baseline": True,
            },
        }
        _sidecar_timing_finish(target_ready_timing, "target_ready", target_ready_started)
        if isinstance(target_ready.get("timing"), dict):
            for key, value in target_ready["timing"].items():
                target_ready_timing.setdefault(str(key), value)
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
            validated_guard=None,
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
    layout_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if target_is_service_container(target):
        return {"detected": False}
    width, height = image_size
    if width <= 0 or height <= 0:
        return {"detected": False}
    try:
        sidebar_header = win32_ocr_layout.required_region(layout_snapshot, "sidebar_header_bounds")
        chat_header = win32_ocr_layout.required_region(layout_snapshot, "chat_header_bounds")
    except win32_ocr_layout.LayoutSnapshotError:
        return {"detected": False, "layout_unresolved": True}
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
        in_service_back_header = (
            has_back_arrow
            and sidebar_header[0] <= float(item.get("center_x") or 0) <= sidebar_header[2]
            and sidebar_header[1] <= center_y <= sidebar_header[3]
        )
        in_active_title = (
            chat_header[0] <= float(item.get("center_x") or 0) <= chat_header[2]
            and chat_header[1] <= center_y <= chat_header[3]
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
    input_region = (
        input_text_region_state(screenshot, ocr_items, geometry=geometry)
        if geometry_check.get("ok")
        else {}
    )
    input_evidence = input_surface_click_evidence(input_region)
    visual_input = {
        "ok": bool(
            online
            and not blocking_reason
            and geometry_check.get("ok")
            and not input_region.get("has_visible_text")
            and input_evidence.get("ok")
        ),
        "method": "win32.observed_input+rpa_text_entry+keyboard_enter",
        "geometry": geometry_check,
        "input_region": input_region,
        "input_evidence": input_evidence,
        "send_button_rule": "observe_active_green_then_press_enter",
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
    elif visual_input.get("ok"):
        scheme = "win32_ocr_visual_input"
    elif receive.get("ok"):
        scheme = "win32_ocr_receive_only"
    else:
        scheme = "win32_ocr_unavailable"
    send_ok = bool(visual_input.get("ok"))
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
            "preferred_mode": DEFAULT_SEND_MODE if send_ok else "",
            "trigger_mode": DEFAULT_SEND_TRIGGER_MODE,
            "formal_locator": "visual_ocr",
            "uia": uia,
            "visual_input": visual_input,
        },
        "compat_reason": "rpa_primary",
    }
def immutable_frame_pixel_evidence(
    screenshot: Image.Image,
    *,
    hwnd: int,
    geometry: dict[str, Any],
    screenshot_path: str = "",
    captured_monotonic: float | None = None,
    layout_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    image = screenshot.convert("RGB")
    width, height = image.size
    # Pixel evidence belongs to this physical capture. Never borrow the
    # latest HWND snapshot: another capture can already have replaced it and
    # its semantic bounds may describe a different frame.
    snapshot = layout_snapshot or layout_snapshot_for_image(screenshot) or {}
    sidebar_bounds = win32_ocr_layout.normalize_rect(snapshot.get("sidebar_bounds"))
    if sidebar_bounds[2] > sidebar_bounds[0] and sidebar_bounds[3] > sidebar_bounds[1]:
        sidebar = image.crop(tuple(sidebar_bounds))
    else:
        sidebar = image
    full_digest = hashlib.sha256(bytes(image.tobytes())).hexdigest()
    sidebar_digest = hashlib.sha256(bytes(sidebar.tobytes())).hexdigest()
    captured_at_monotonic = (
        float(captured_monotonic)
        if captured_monotonic is not None
        else time.monotonic()
    )
    return {
        # A frame id identifies one physical capture, not merely equal pixels.
        # Equal screenshots at S0/S1/S2 must still remain separate timepoints.
        "frame_id": (
            f"frame:{time.monotonic_ns()}:{os.urandom(8).hex()}:{full_digest[:16]}"
        ),
        "screenshot_sha256": full_digest,
        "hwnd": int(hwnd or 0),
        "geometry": {
            key: int(geometry.get(key) or 0)
            for key in ("left", "top", "right", "bottom", "width", "height")
        },
        "dpi_scale": float(window_dpi_scale(hwnd)),
        "sidebar_bounds": sidebar_bounds if sidebar_bounds[2] > sidebar_bounds[0] else [0, 0, width, height],
        "sidebar_sha256": sidebar_digest,
        "captured_monotonic": captured_at_monotonic,
        "screenshot_path": str(screenshot_path or ""),
    }


def sessions_payload(
    hwnd: int,
    probe: dict[str, Any],
    *,
    artifact_dir: str | None = None,
    scan_id: str = "",
    sidecar_run_id: str = "",
) -> dict[str, Any]:
    screenshot, path = capture_wechat(hwnd, artifact_dir=artifact_dir, label="sessions")
    frame_captured_monotonic = time.monotonic()
    ocr_started = time.perf_counter()
    items, enhanced_count = session_list_ocr_items(screenshot, run_ocr(screenshot))
    ocr_total_duration_ms = round(
        (time.perf_counter() - ocr_started) * 1000
    )
    # session_list_ocr_items always performs the enhanced sidebar pass unless
    # the caller already supplied enhanced rows.  sessions_payload supplies a
    # fresh base pass, so the actual invocation count is two.
    ocr_call_count = 2
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
    session_snapshot_id = str(
        (layout_snapshot_metadata(hwnd).get("snapshot") or {}).get("layout_snapshot_id") or ""
    )
    for session in sessions:
        if isinstance(session, dict):
            session["layout_snapshot_id"] = session_snapshot_id
    visible_frame_reuse_evidence: dict[str, Any] = {}
    if env_flag("CHEJIN_C2_LOCATE_FRAME_REUSE_ENABLED", default=True):
        compact_ocr_items = compact_ocr_items_for_report(items)
        ocr_result_sha256 = hashlib.sha256(
            json.dumps(
                compact_ocr_items,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        visible_frame_reuse_evidence = {
            "schema_version": 1,
            **immutable_frame_pixel_evidence(
                screenshot,
                hwnd=hwnd,
                geometry=geometry,
                screenshot_path=path,
                captured_monotonic=frame_captured_monotonic,
            ),
            "scan_id": str(scan_id or ""),
            "sidecar_run_id": str(sidecar_run_id or ""),
            "ocr_result_sha256": ocr_result_sha256,
            "ocr_item_count": len(compact_ocr_items),
            "ocr_call_count": ocr_call_count,
            "ocr_total_duration_ms": ocr_total_duration_ms,
        }
        for session in sessions:
            if not isinstance(session, dict):
                continue
            session["visible_frame_reuse_evidence"] = {
                **visible_frame_reuse_evidence,
                "candidate_session_key": str(
                    session.get("session_key") or ""
                ),
                "candidate_remark_code_candidates": list(
                    session.get("c2_remark_code_candidates") or []
                ),
                "candidate_conversation_type": str(
                    session.get("c2_conversation_type") or ""
                ),
                "candidate_admission": dict(
                    session.get("c2_conversation_admission") or {}
                ),
                "candidate_bounds": [
                    float(session.get(key) or 0.0)
                    for key in ("left", "top", "right", "bottom")
                ],
            }
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
                "visible_frame_reuse_evidence": item.get(
                    "visible_frame_reuse_evidence"
                )
                or {},
                "source_adapter": "win32_ocr",
            "ocr_confidence": item.get("confidence"),
            }
            for item in sessions
        ],
        "ocr_items_count": len(items),
        "ocr_items_enhanced_count": enhanced_count,
        "ocr_items": compact_ocr_items_for_report(items),
        "scan_id": str(scan_id or ""),
        "visible_frame_reuse_evidence": visible_frame_reuse_evidence,
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
    expected_confirmed_self_text: str = "",
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
    image_observation_errors: list[dict[str, Any]] = []
    messages = merge_structural_image_messages(
        screenshot,
        ocr_items,
        merge_message_history_snapshots(snapshots),
        target=target,
        observation_validation_errors=image_observation_errors,
    )
    confirmed_self_text_recovery: dict[str, Any] = {
        "attempted": False,
        "recovered": False,
        "reason": "not_requested",
    }
    if str(expected_confirmed_self_text or "").strip() and screenshot is not None:
        messages, confirmed_self_text_recovery = (
            recover_expected_self_text_from_structural_candidates(
                screenshot,
                messages,
                target=target,
                expected_text=expected_confirmed_self_text,
                require_correspondence=True,
            )
        )
    visible_voice_hint = (
        visible_untranscribed_voice_hint(
            screenshot,
            ocr_items,
            screenshot.size,
            parsed_messages=messages,
        )
        if screenshot is not None
        else {"detected": False}
    )
    observations = build_message_observations_v3(messages, visible_voice_hint)
    message_region_fingerprint = send_context_message_region_fingerprint(screenshot)
    observation_validation_errors = [
        {
            "observation_id": str(observation.get("observation_id") or ""),
            "row_kind": str(observation.get("row_kind") or ""),
            "error_codes": list(observation.get("contract_errors") or []),
        }
        for observation in observations
        if isinstance(observation, dict) and observation.get("contract_errors")
    ] + image_observation_errors
    payload = {
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
        "send_context_guard": build_send_context_guard(
            observations,
            message_region_sha256=str(
                message_region_fingerprint.get("sha256") or ""
            ),
            message_region_bounds=list(
                message_region_fingerprint.get("bounds") or []
            ),
        ),
        "observation_validation_errors": observation_validation_errors,
        "confirmed_self_text_recovery": confirmed_self_text_recovery,
        "observation_schema_version": C2_OBSERVATION_SCHEMA_VERSION,
        "visible_untranscribed_voice": visible_voice_hint,
        "ocr_items_count": len(ocr_items),
        "target_confirmation": target_confirmation,
    }
    if (
        screenshot is not None
        and env_flag("CHEJIN_C3_PRE_SEND_ROI_REUSE_ENABLED", default=True)
    ):
        frame_observation = immutable_frame_pixel_evidence(
            screenshot,
            hwnd=hwnd,
            geometry=geometry,
            screenshot_path=str(latest.get("screenshot_path") or ""),
        )
        frame_observation.update(
            {
                "schema_version": 1,
                "ocr_regions": ["full_frame"],
                "ocr_engine": "rapidocr",
                "ocr_parameters": {"mode": "default"},
                "ocr_cache_key": (
                    f"{frame_observation['frame_id']}:full_frame:rapidocr:default"
                ),
            }
        )
        payload["frame_observation"] = frame_observation
        payload["pre_send_frame_reuse"] = {
            "fast_path_attempted": True,
            "fast_path_used": True,
            "fallback_reason": "",
            "frame_digest_equal": True,
            "ocr_call_count": int(latest.get("ocr_call_count") or 1),
            "ocr_total_duration_ms": (
                int(latest.get("ocr_total_duration_ms"))
                if latest.get("ocr_total_duration_ms") is not None
                else None
            ),
            "shared_consumers": [
                "target_confirmation",
                "message_viewport",
                "message_sequence",
                "input_region",
            ],
        }
    if artifact_dir:
        try:
            review_path = write_messages_frame_review(Path(artifact_dir), payload)
            payload["review_path"] = review_path
            payload["evidence_path"] = review_path
        except Exception as exc:
            payload["review_error"] = repr(exc)
    return payload


def _voice_action_frame_id(image: Image.Image, screenshot_path: str) -> str:
    digest = hashlib.sha256()
    digest.update(str(screenshot_path or "").encode("utf-8"))
    digest.update(bytes(image.tobytes()))
    return f"voice-frame:{digest.hexdigest()}"


def _voice_observation_fingerprint(
    image: Image.Image,
    observation: dict[str, Any],
) -> str:
    """Return action-local target evidence without using screen position as identity."""

    source_message = (
        observation.get("source_message")
        if isinstance(observation.get("source_message"), dict)
        else {}
    )
    target = (
        observation.get("action_target")
        if isinstance(observation.get("action_target"), dict)
        else {}
    )
    rect = unified_voice_observation_rect(observation)
    crop_digest = ""
    if rect:
        left, top, right, bottom = [int(round(value)) for value in rect]
        left = max(0, left)
        top = max(0, top)
        right = min(image.size[0], right)
        bottom = min(image.size[1], bottom)
        if right > left and bottom > top:
            crop = image.crop((left, top, right, bottom)).convert("L")
            crop.thumbnail((96, 48))
            crop_digest = hashlib.sha256(bytes(crop.tobytes())).hexdigest()
    material = {
        "sender_role": normalized_voice_sender_role(
            observation.get("sender_role")
        ),
        "voice_duration": str(observation.get("voice_duration") or ""),
        "voice_duration_text": voice_transcribe_compact_text(
            observation.get("voice_duration_text")
        ),
        "source_message_id": str(
            observation.get("source_message_id")
            or source_message.get("id")
            or ""
        ),
        "anchor_stable_key": str(target.get("anchor_stable_key") or ""),
        "avatar_role": str(
            (target.get("avatar_alignment") or {}).get("role") or ""
        ),
        "evidence_sources": sorted(
            str(value) for value in (observation.get("evidence_sources") or [])
        ),
        "crop_digest": crop_digest,
        # A target crop can be pixel-identical after a newly arrived voice
        # takes the old bubble's seat.  Bind the prepare token to the complete
        # observed frame as action-local evidence so any concurrent page
        # mutation forces a zero-click re-prepare.
        "frame_visual_digest": hashlib.sha256(
            bytes(image.tobytes())
        ).hexdigest(),
    }
    return hashlib.sha256(
        json.dumps(material, sort_keys=True, ensure_ascii=True).encode("utf-8")
    ).hexdigest()


def _public_voice_observation(observation: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in observation.items()
        if key not in {"action_target", "visible_button_target"}
    }


def prepare_voice_action_payload(
    hwnd: int,
    probe: dict[str, Any],
    *,
    target: str,
    artifact_dir: str | None = None,
    confirm_target: str = "",
    confirm_exact: bool = False,
    expected_confirmed_self_text: str = "",
    excluded_voice_anchor_keys: set[str] | None = None,
) -> dict[str, Any]:
    """Capture and select exactly one physical voice; never touch WeChat UI."""

    screenshot, screenshot_path = capture_wechat(
        hwnd,
        artifact_dir=artifact_dir,
        label="voice_action_prepare",
    )
    ocr_items = run_ocr(screenshot)
    image_size = getattr(screenshot, "size", (0, 0))
    target_confirmation: dict[str, Any] = {}
    if confirm_target:
        target_confirmation = validate_active_send_target(
            hwnd,
            confirm_target,
            exact=confirm_exact,
            artifact_dir=artifact_dir,
            screenshot=screenshot,
            ocr_items=ocr_items,
            screenshot_path=screenshot_path,
        )
        if not c2_target_activation_confirmed(target_confirmation):
            error_code, error = c2_target_admission_error(
                target_confirmation,
                "TARGET_NOT_CONFIRMED_FOR_VOICE_TRANSCRIBE",
            )
            return {
                "ok": False,
                "state": "target_not_confirmed_for_voice_prepare",
                "error_code": error_code,
                "error": error,
                "target_confirmation": target_confirmation,
                "ui_action_performed": False,
            }
    messages = parse_current_chat_frame_messages(
        ocr_items,
        image_size,
        target=target,
        screenshot=screenshot,
    )
    if str(expected_confirmed_self_text or "").strip():
        messages, _confirmed_self_text_recovery = (
            recover_expected_self_text_from_structural_candidates(
                screenshot,
                messages,
                target=target,
                expected_text=expected_confirmed_self_text,
                require_correspondence=True,
            )
        )
    candidates = [
        observation
        for observation in build_unified_voice_observations_v3(
            screenshot,
            ocr_items,
            image_size,
            excluded_anchor_keys=excluded_voice_anchor_keys,
            parsed_messages=messages,
        )
        if observation.get("voice_state") == "untranscribed"
        and not observation.get("contract_errors")
        and not observation.get("excluded")
        and isinstance(observation.get("action_target"), dict)
    ]
    frame_id = _voice_action_frame_id(screenshot, screenshot_path)
    observations = build_message_observations_v3(messages)
    if not candidates:
        return {
            "ok": True,
            "state": "voice_action_prepare_empty",
            "voice_action_stage": "prepare",
            "pre_frame_id": frame_id,
            "messages": messages,
            "observations": observations,
            "target_confirmation": target_confirmation,
            "ui_action_performed": False,
        }
    selected = max(
        candidates,
        key=lambda item: voice_target_center_y(item.get("action_target")),
    )
    selected_id = str(selected.get("observation_id") or "").strip()
    fingerprint = _voice_observation_fingerprint(screenshot, selected)
    same_fingerprint_count = sum(
        _voice_observation_fingerprint(screenshot, item) == fingerprint
        for item in candidates
    )
    if not selected_id or same_fingerprint_count != 1:
        return {
            "ok": False,
            "state": "voice_action_prepare_ambiguous",
            "error_code": "C2_VOICE_PREPARE_TARGET_AMBIGUOUS",
            "pre_frame_id": frame_id,
            "candidate_group_count": len(candidates),
            "fingerprint_candidate_count": same_fingerprint_count,
            "ui_action_performed": False,
        }
    token_material = os.urandom(32) + frame_id.encode("utf-8") + fingerprint.encode("ascii")
    action_token = hashlib.sha256(token_material).hexdigest()
    return {
        "ok": True,
        "state": "voice_action_prepared",
        "voice_action_stage": "prepare",
        "pre_frame_id": frame_id,
        "selected_pre_observation_id": selected_id,
        "selected_action_token": action_token,
        "selected_target_fingerprint": fingerprint,
        "selected_voice_observation": _public_voice_observation(selected),
        "selected_physical_anchor_keys": sorted(
            voice_context_anchor_exclusion_keys(
                selected["action_target"], image_size
            )
        ),
        "candidate_group_count": len(candidates),
        "messages": messages,
        "observations": observations,
        "target_confirmation": target_confirmation,
        "screenshot_path": screenshot_path,
        "ui_action_performed": False,
    }


def _bind_voice_transcripts_for_action(
    messages: list[dict[str, Any]],
    anchor: dict[str, Any],
    image_size: tuple[int, int],
    *,
    canonical_voice_action_id: str,
    reserved_worker_stable_id: str,
    selected_action_token: str,
    pre_observation_id: str,
) -> list[dict[str, Any]]:
    anchor_role = voice_anchor_sender_role(anchor, image_size)
    anchor_keys = sorted(voice_context_anchor_exclusion_keys(anchor, image_size))
    bound: list[dict[str, Any]] = []
    for message in messages:
        if not isinstance(message, dict) or not message_is_plausible_voice_transcript_for_anchor(
            message,
            anchor,
            image_size,
            after_messages=messages,
        ):
            continue
        item = dict(message)
        item.update(
            {
                "type": "voice",
                # This is an in-memory action receipt, not formal message
                # identity.  build_message_observations_v3 projects it beside
                # source_message and the execute flow consumes it before the
                # observations leave the Sidecar.
                "_frame_action_binding": {
                    "canonical_voice_action_id": canonical_voice_action_id,
                    "reserved_worker_stable_id": reserved_worker_stable_id,
                    "selected_action_token": selected_action_token,
                    "pre_observation_id": pre_observation_id,
                    "post_observation_id": "",
                    "binding_confirmed": True,
                },
                "voice_anchor": {
                    "anchor_key": str(anchor.get("anchor_key") or ""),
                    "anchor_stable_key": str(anchor.get("anchor_stable_key") or ""),
                    "anchor_structural_key": str(anchor.get("anchor_structural_key") or ""),
                    "exclusion_keys": anchor_keys,
                },
                "voice_anchor_key": str(anchor.get("anchor_key") or ""),
                "voice_anchor_stable_key": str(anchor.get("anchor_stable_key") or ""),
                "voice_anchor_structural_key": str(anchor.get("anchor_structural_key") or ""),
                "row_kind": "voice_transcript",
                "voice_state": "transcribed",
            }
        )
        if anchor_role in {"customer", "self"}:
            item["sender"] = anchor_role
            item["sender_role"] = anchor_role
        bound.append(item)
    combined = [item for item in bound if message_is_combined_voice_transcript_record(item)]
    return combined or bound


def confirmed_voice_frame_action_observations(
    observations: list[dict[str, Any]],
    *,
    canonical_voice_action_id: str,
    reserved_worker_stable_id: str,
    selected_action_token: str,
    pre_observation_id: str,
) -> list[dict[str, Any]]:
    """Select the exact post observation using only frame-local action proof."""

    confirmed: list[dict[str, Any]] = []
    expected = {
        "canonical_voice_action_id": canonical_voice_action_id,
        "reserved_worker_stable_id": reserved_worker_stable_id,
        "selected_action_token": selected_action_token,
        "pre_observation_id": pre_observation_id,
    }
    if any(not str(value or "").strip() for value in expected.values()):
        return []
    for item in observations:
        if not isinstance(item, dict):
            continue
        binding = item.get(C2_FRAME_ACTION_BINDING_CONTAINER)
        if not isinstance(binding, dict):
            continue
        observation_id = str(item.get("observation_id") or "").strip()
        if (
            observation_id
            and all(
                str(binding.get(key) or "").strip()
                == str(value or "").strip()
                for key, value in expected.items()
            )
            and str(binding.get("post_observation_id") or "").strip()
            == observation_id
            and binding.get("binding_confirmed") is True
        ):
            confirmed.append(item)
    return confirmed


def execute_voice_action_payload(
    hwnd: int,
    probe: dict[str, Any],
    *,
    target: str,
    artifact_dir: str | None,
    confirm_target: str,
    confirm_exact: bool,
    expected_confirmed_self_text: str = "",
    action_journal_path: str,
    canonical_voice_action_id: str,
    reserved_worker_stable_id: str,
    pre_frame_id: str,
    selected_pre_observation_id: str,
    selected_action_token: str,
    selected_target_fingerprint: str,
) -> dict[str, Any]:
    """Execute only the exact, journaled prepare target and finish once."""

    journal = read_action_phase_journal(action_journal_path)
    journal_payload = journal.get("payload") if isinstance(journal.get("payload"), dict) else {}
    prepare_evidence = journal_payload.get("prepare_evidence") if isinstance(journal_payload.get("prepare_evidence"), dict) else {}
    expected = {
        "pre_frame_id": pre_frame_id,
        "selected_pre_observation_id": selected_pre_observation_id,
        "selected_action_token": selected_action_token,
        "selected_target_fingerprint": selected_target_fingerprint,
    }
    request_identity_evidence = {
        "voice_action_stage": "execute",
        "canonical_voice_action_id": canonical_voice_action_id,
        "reserved_worker_stable_id": reserved_worker_stable_id,
        **expected,
    }
    if (
        not journal.get("ok")
        or journal.get("action_phase") != "not_attempted"
        or not str(canonical_voice_action_id or "").strip()
        or not str(reserved_worker_stable_id or "").strip()
        or any(not str(value or "").strip() for value in expected.values())
        or str(journal_payload.get("canonical_action_id") or "") != canonical_voice_action_id
        or str(journal_payload.get("reserved_worker_stable_id") or "") != reserved_worker_stable_id
        or any(str(prepare_evidence.get(key) or "") != str(value) for key, value in expected.items())
    ):
        return {
            "ok": False,
            "state": "voice_action_execute_contract_rejected",
            "error_code": "C2_VOICE_EXECUTE_CONTRACT_INVALID",
            "action_phase": str(journal.get("action_phase") or "not_attempted"),
            "ui_action_performed": False,
            **request_identity_evidence,
        }
    screenshot, screenshot_path = capture_wechat(hwnd, artifact_dir=artifact_dir, label="voice_action_execute_before")
    ocr_items = run_ocr(screenshot)
    image_size = getattr(screenshot, "size", (0, 0))
    target_confirmation: dict[str, Any] = {}
    if confirm_target:
        target_confirmation = validate_active_send_target(
            hwnd,
            confirm_target,
            exact=confirm_exact,
            artifact_dir=artifact_dir,
            screenshot=screenshot,
            ocr_items=ocr_items,
            screenshot_path=screenshot_path,
        )
    messages = parse_current_chat_frame_messages(ocr_items, image_size, target=target, screenshot=screenshot)
    if str(expected_confirmed_self_text or "").strip():
        messages, _confirmed_self_text_recovery = (
            recover_expected_self_text_from_structural_candidates(
                screenshot,
                messages,
                target=target,
                expected_text=expected_confirmed_self_text,
                require_correspondence=True,
            )
        )
    candidates = [
        item for item in build_unified_voice_observations_v3(
            screenshot,
            ocr_items,
            image_size,
            parsed_messages=messages,
        )
        if item.get("voice_state") == "untranscribed"
        and not item.get("contract_errors")
        and isinstance(item.get("action_target"), dict)
    ]
    matches = [
        item for item in candidates
        if str(item.get("observation_id") or "") == selected_pre_observation_id
        and _voice_observation_fingerprint(screenshot, item) == selected_target_fingerprint
    ]
    if (
        (confirm_target and not c2_target_activation_confirmed(target_confirmation))
        or len(candidates)
        != int(prepare_evidence.get("candidate_group_count") or 0)
        or len(matches) != 1
    ):
        write_action_phase_journal(
            action_journal_path,
            "cancelled_before_trigger",
            terminal_payload={
                "state": "cancelled_before_trigger",
                "media_action_terminal": "cancelled_before_trigger",
                "reason": "prepared_voice_target_changed",
            },
        )
        return {
            "ok": True,
            "state": "voice_action_cancelled_before_trigger",
            "action_phase": "cancelled_before_trigger",
            "business_state": "not_attempted",
            "business_result_confirmed": False,
            "error_code": "C2_VOICE_PREPARED_TARGET_CHANGED",
            "ui_action_performed": False,
            "target_confirmation": target_confirmation,
            **request_identity_evidence,
        }
    selected = matches[0]
    anchor = dict(selected["action_target"])
    physical_anchor_keys = sorted(voice_context_anchor_exclusion_keys(anchor, image_size))
    execute_frame_id = _voice_action_frame_id(
        screenshot,
        screenshot_path,
    )
    tracking_edges: list[dict[str, Any]] = [
        {
            "from_frame_id": pre_frame_id,
            "from_observation_id": selected_pre_observation_id,
            "to_frame_id": execute_frame_id,
            "to_observation_id": str(
                selected.get("observation_id") or ""
            ),
            "sender_role": normalized_voice_sender_role(
                selected.get("sender_role")
            ),
            "message_type": "voice",
            "structural_evidence": {
                "selected_observation_id_unchanged": True
            },
            "displacement_evidence": {
                "target_fingerprint_unchanged": True
            },
            "edge_candidate_count": 1,
        }
    ]
    tracking_frame_ids = [pre_frame_id, execute_frame_id]
    visible_target = selected.get("visible_button_target") if isinstance(selected.get("visible_button_target"), dict) else None
    click_result: dict[str, Any]
    if visible_target:
        bounds = [int(value) for value in (visible_target.get("click_bounds") or [])]
        if len(bounds) != 4:
            item = visible_target.get("item") if isinstance(visible_target.get("item"), dict) else {}
            bounds = [int(float(item.get(key) or 0)) for key in ("left", "top", "right", "bottom")]
        if len(bounds) != 4 or bounds[2] <= bounds[0] or bounds[3] <= bounds[1]:
            write_action_phase_journal(
                action_journal_path,
                "cancelled_before_trigger",
                physical_anchor_keys=physical_anchor_keys,
                terminal_payload={
                    "state": "cancelled_before_trigger",
                    "media_action_terminal": "cancelled_before_trigger",
                    "reason": "visible_button_bounds_invalid",
                },
            )
            return {
                "ok": True,
                "state": "voice_action_cancelled_before_trigger",
                "action_phase": "cancelled_before_trigger",
                "business_state": "not_attempted",
                "business_result_confirmed": False,
                "error_code": "C2_VOICE_PREPARED_TARGET_CHANGED",
                "ui_action_performed": False,
                "target_confirmation": target_confirmation,
                **request_identity_evidence,
            }
        write_action_phase_journal(
            action_journal_path,
            "trigger_attempted",
            physical_anchor_keys=physical_anchor_keys,
        )
        click_result = human_window_image_click_in_bounds(
            hwnd,
            (bounds[0] + bounds[2]) // 2,
            (bounds[1] + bounds[3]) // 2,
            bounds=bounds,
            action_name="voice_transcribe_visible_button_click",
            expected_snapshot_id=str(visible_target.get("layout_snapshot_id") or ""),
        )
    else:
        # Opening the context menu is already a WeChat UI action, so the
        # no-repeat barrier must be durable before this call.
        write_action_phase_journal(
            action_journal_path,
            "trigger_attempted",
            physical_anchor_keys=physical_anchor_keys,
        )
        menu = open_voice_transcribe_context_menu(
            hwnd,
            anchor,
            image_size=image_size,
            artifact_dir=artifact_dir,
        )
        menu_target = menu.get("click_target") if isinstance(menu.get("click_target"), dict) else None
        if not menu_target:
            dismiss_voice_transcribe_context_menu(hwnd, artifact_dir=artifact_dir)
            click_result = {"ok": False, "reason": str(menu.get("menu_state") or "menu_target_missing")}
        else:
            click_result = click_voice_transcribe_context_menu_target(
                hwnd,
                menu_target,
                geometry=get_window_geometry(hwnd),
                artifact_dir=artifact_dir,
                attempt_index=1,
            )
    if not click_result.get("ok"):
        humanized_action_sleep(300, 700)
        failed_screenshot, failed_path = capture_wechat(
            hwnd,
            artifact_dir=artifact_dir,
            label="voice_action_execute_failed_final",
        )
        failed_items = run_ocr(failed_screenshot)
        failed_size = getattr(failed_screenshot, "size", image_size)
        failed_messages = parse_current_chat_frame_messages(
            failed_items,
            failed_size,
            target=target,
            screenshot=failed_screenshot,
        )
        if str(expected_confirmed_self_text or "").strip():
            failed_messages, _confirmed_self_text_recovery = (
                recover_expected_self_text_from_structural_candidates(
                    failed_screenshot,
                    failed_messages,
                    target=target,
                    expected_text=expected_confirmed_self_text,
                    require_correspondence=True,
                )
            )
        failed_observations = build_message_observations_v3(
            failed_messages
        )
        failed_frame_id = _voice_action_frame_id(
            failed_screenshot,
            failed_path,
        )
        failed_candidates = [
            item
            for item in build_unified_voice_observations_v3(
                failed_screenshot,
                failed_items,
                failed_size,
                parsed_messages=failed_messages,
            )
            if item.get("voice_state") == "untranscribed"
            and not item.get("contract_errors")
            and isinstance(item.get("action_target"), dict)
        ]
        tracked_candidates = [
            item
            for item in failed_candidates
            if str(item.get("observation_id") or "")
            == selected_pre_observation_id
            and _voice_observation_fingerprint(
                failed_screenshot,
                item,
            )
            == selected_target_fingerprint
        ]
        post_observation_id = ""
        if len(tracked_candidates) == 1:
            tracked_aliases = voice_context_anchor_exclusion_keys(
                dict(tracked_candidates[0]["action_target"]),
                failed_size,
            )
            matching_observations = [
                item
                for item in failed_observations
                if isinstance(item, dict)
                and item.get("row_kind") == "voice_bubble"
                and tracked_aliases
                & set(voice_context_anchor_exclusion_keys(
                    dict(item.get("action_target") or {}),
                    failed_size,
                ))
            ]
            if len(matching_observations) == 1:
                post_observation_id = str(
                    matching_observations[0].get("observation_id") or ""
                ).strip()
        if post_observation_id:
            tracking_edges.append(
                {
                    "from_frame_id": tracking_edges[-1]["to_frame_id"],
                    "from_observation_id": tracking_edges[-1][
                        "to_observation_id"
                    ],
                    "to_frame_id": failed_frame_id,
                    "to_observation_id": post_observation_id,
                    "sender_role": normalized_voice_sender_role(
                        selected.get("sender_role")
                    ),
                    "message_type": "voice",
                    "structural_evidence": {
                        "failed_action_target_tracked": True
                    },
                    "displacement_evidence": {
                        "same_action_token_chain": True
                    },
                    "edge_candidate_count": 1,
                }
            )
            tracking_frame_ids.append(failed_frame_id)
            write_action_phase_journal(
                action_journal_path,
                "failed",
                physical_anchor_keys=physical_anchor_keys,
                business_state="failed",
                business_result_confirmed=False,
                error_code="VOICE_TRANSCRIBE_TRIGGER_FAILED",
                terminal_payload={
                    "state": "failed",
                    "media_action_terminal": "committed_failed",
                    "click": click_result,
                },
            )
            return {
                "ok": False,
                "state": "voice_transcribe_click_failed",
                "error_code": "VOICE_TRANSCRIBE_TRIGGER_FAILED",
                "action_phase": "failed",
                "business_state": "failed",
                "business_result_confirmed": False,
                "canonical_voice_action_id": canonical_voice_action_id,
                "reserved_worker_stable_id": reserved_worker_stable_id,
                **request_identity_evidence,
                "post_frame_id": failed_frame_id,
                "transcript_binding_status": "failed",
                "transcript_binding_method": "continuous_target_tracking",
                "binding_candidate_count": 1,
                "tracking_frame_ids": tracking_frame_ids,
                "tracking_edges": tracking_edges,
                "confirmed_action_mapping": {
                    "canonical_action_id": canonical_voice_action_id,
                    "reserved_worker_stable_id": reserved_worker_stable_id,
                    "selected_action_token": selected_action_token,
                    "pre_observation_id": selected_pre_observation_id,
                    "binding_confirmed": True,
                    "post_observation_id": post_observation_id,
                    "derived_observation_ids": [],
                },
                "messages": failed_messages,
                "observations": failed_observations,
                "ui_action_performed": True,
                "click": click_result,
            }
        write_action_phase_journal(
            action_journal_path,
            "quarantined",
            physical_anchor_keys=physical_anchor_keys,
            business_state="failed",
            business_result_confirmed=False,
            error_code="C2_VOICE_RESULT_AMBIGUOUS",
            terminal_payload={
                "state": "quarantined",
                "media_action_terminal": "identity_unresolved",
            },
        )
        return {
            "ok": True,
            "state": "voice_transcribe_ambiguous",
            "error_code": "C2_VOICE_RESULT_AMBIGUOUS",
            "action_phase": "quarantined",
            "business_state": "failed",
            "business_result_confirmed": False,
            "canonical_voice_action_id": canonical_voice_action_id,
            "reserved_worker_stable_id": reserved_worker_stable_id,
            **request_identity_evidence,
            "post_frame_id": failed_frame_id,
            "transcript_binding_status": "ambiguous",
            "transcript_binding_method": "none",
            "binding_candidate_count": 0,
            "tracking_frame_ids": tracking_frame_ids,
            "tracking_edges": tracking_edges,
            "confirmed_action_mapping": {
                "canonical_action_id": canonical_voice_action_id,
                "reserved_worker_stable_id": reserved_worker_stable_id,
                "selected_action_token": selected_action_token,
                "pre_observation_id": selected_pre_observation_id,
                "binding_confirmed": False,
                "post_observation_id": "",
                "derived_observation_ids": [],
            },
            "messages": failed_messages,
            "observations": failed_observations,
            "ui_action_performed": True,
            "click": click_result,
        }
    bound: list[dict[str, Any]] = []
    final_screenshot = screenshot
    final_path = screenshot_path
    final_items = ocr_items
    final_messages = messages
    evidence_recovery_started_at = time.monotonic()
    evidence_recovery_deadline = evidence_recovery_started_at + 120.0
    evidence_read_count = 0
    for evidence_read in range(2):
        if time.monotonic() >= evidence_recovery_deadline:
            break
        humanized_action_sleep(500, 1100)
        if time.monotonic() >= evidence_recovery_deadline:
            break
        final_screenshot, final_path = capture_wechat(
            hwnd,
            artifact_dir=artifact_dir,
            label=f"voice_action_execute_after_{evidence_read + 1}",
        )
        final_items = run_ocr(final_screenshot)
        final_size = getattr(final_screenshot, "size", image_size)
        final_messages = parse_current_chat_frame_messages(
            final_items,
            final_size,
            target=target,
            screenshot=final_screenshot,
        )
        evidence_read_count = evidence_read + 1
        if str(expected_confirmed_self_text or "").strip():
            final_messages, _confirmed_self_text_recovery = (
                recover_expected_self_text_from_structural_candidates(
                    final_screenshot,
                    final_messages,
                    target=target,
                    expected_text=expected_confirmed_self_text,
                    require_correspondence=True,
                )
            )
        bound = _bind_voice_transcripts_for_action(
            final_messages,
            anchor,
            image_size,
            canonical_voice_action_id=canonical_voice_action_id,
            reserved_worker_stable_id=reserved_worker_stable_id,
            selected_action_token=selected_action_token,
            pre_observation_id=selected_pre_observation_id,
        )
        if len(bound) == 1:
            break
    authoritative_messages = list(final_messages)
    if len(bound) == 1:
        bound_id = str(bound[0].get("id") or bound[0].get("message_id") or "")
        authoritative_messages = [
            bound[0]
            if str(item.get("id") or item.get("message_id") or "") == bound_id
            else item
            for item in final_messages
        ]
    observations = build_message_observations_v3(authoritative_messages)
    final_frame_id = _voice_action_frame_id(
        final_screenshot,
        final_path,
    )
    action_observations = confirmed_voice_frame_action_observations(
        observations,
        canonical_voice_action_id=canonical_voice_action_id,
        reserved_worker_stable_id=reserved_worker_stable_id,
        selected_action_token=selected_action_token,
        pre_observation_id=selected_pre_observation_id,
    )
    if len(action_observations) != 1:
        write_action_phase_journal(
            action_journal_path,
            "quarantined",
            physical_anchor_keys=physical_anchor_keys,
            business_state="failed",
            business_result_confirmed=False,
            error_code="C2_VOICE_RESULT_AMBIGUOUS",
            terminal_payload={
                "state": "quarantined",
                "media_action_terminal": "identity_unresolved",
                "evidence_read_count": evidence_read_count,
                "evidence_elapsed_seconds": min(
                    120.0,
                    max(0.0, time.monotonic() - evidence_recovery_started_at),
                ),
            },
        )
        return {
            "ok": True,
            "state": "voice_transcribe_ambiguous",
            "error_code": "C2_VOICE_RESULT_AMBIGUOUS",
            "action_phase": "quarantined",
            "business_state": "failed",
            "business_result_confirmed": False,
            "canonical_voice_action_id": canonical_voice_action_id,
            "reserved_worker_stable_id": reserved_worker_stable_id,
            **request_identity_evidence,
            "post_frame_id": final_frame_id,
            "transcript_binding_status": "ambiguous",
            "transcript_binding_method": "none",
            "binding_candidate_count": 0,
            "tracking_frame_ids": tracking_frame_ids,
            "tracking_edges": tracking_edges,
            "confirmed_action_mapping": {
                "canonical_action_id": canonical_voice_action_id,
                "reserved_worker_stable_id": reserved_worker_stable_id,
                "selected_action_token": selected_action_token,
                "pre_observation_id": selected_pre_observation_id,
                "binding_confirmed": False,
                "post_observation_id": "",
                "derived_observation_ids": [],
            },
            "messages": final_messages,
            "observations": build_message_observations_v3(final_messages),
            "ui_action_performed": True,
        }
    post_observation_id = str(action_observations[0].get("observation_id") or "")
    tracking_edges.append(
        {
            "from_frame_id": tracking_edges[-1]["to_frame_id"],
            "from_observation_id": tracking_edges[-1]["to_observation_id"],
            "to_frame_id": final_frame_id,
            "to_observation_id": post_observation_id,
            "sender_role": normalized_voice_sender_role(selected.get("sender_role")),
            "message_type": "voice",
            "structural_evidence": {"unique_transcript_binding": True},
            "displacement_evidence": {"same_action_token_chain": True},
            "edge_candidate_count": 1,
        }
    )
    tracking_frame_ids.append(final_frame_id)
    write_action_phase_journal(
        action_journal_path,
        "confirmed",
        physical_anchor_keys=physical_anchor_keys,
        business_state="completed",
        business_result_confirmed=True,
        terminal_payload={
            "state": "completed",
            "media_action_terminal": "committed_completed",
            "transcribed_messages": bound,
        },
    )
    return {
        "ok": True,
        "state": "voice_transcribe_completed",
        "voice_action_stage": "execute",
        "action_phase": "confirmed",
        "business_state": "completed",
        "business_result_confirmed": True,
        "canonical_voice_action_id": canonical_voice_action_id,
        "reserved_worker_stable_id": reserved_worker_stable_id,
        **request_identity_evidence,
        "post_frame_id": final_frame_id,
        "transcript_binding_status": "confirmed",
        "transcript_binding_method": "continuous_target_tracking",
        "binding_candidate_count": 1,
        "tracking_frame_ids": tracking_frame_ids,
        "tracking_edges": tracking_edges,
        "confirmed_action_mapping": {
            "canonical_action_id": canonical_voice_action_id,
            "reserved_worker_stable_id": reserved_worker_stable_id,
            "selected_action_token": selected_action_token,
            "pre_observation_id": selected_pre_observation_id,
            "binding_confirmed": True,
            "post_observation_id": post_observation_id,
            "derived_observation_ids": [],
        },
        "processed_voice_anchor_keys": physical_anchor_keys,
        "failed_voice_anchor_keys": [],
        "item_action_outcomes": [
            {
                "physical_anchor_keys": physical_anchor_keys,
                "action_phase": "confirmed",
                "business_state": "completed",
                "business_result_confirmed": True,
            }
        ],
        "messages": authoritative_messages,
        "observations": [
            {
                key: value
                for key, value in observation.items()
                if key != C2_FRAME_ACTION_BINDING_CONTAINER
            }
            for observation in observations
        ],
        "target_confirmation": target_confirmation,
        "final_frame_reusable": True,
        "ui_action_performed": True,
    }


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


def voice_transcribe_item_is_in_chat_surface(
    item: dict[str, Any],
    image_size: tuple[int, int],
    *,
    layout_snapshot: dict[str, Any] | None = None,
) -> bool:
    try:
        viewport = win32_ocr_layout.required_region(layout_snapshot, "message_viewport_bounds")
    except win32_ocr_layout.LayoutSnapshotError:
        return False
    center_y = float(item.get("center_y") or 0)
    center_x = float(item.get("center_x") or 0)
    if not win32_ocr_layout.point_in_bounds([center_x, center_y], viewport):
        return False
    rect = {
        "left": int(float(item.get("left") or 0)),
        "top": int(float(item.get("top") or 0)),
        "right": int(float(item.get("right") or 0)),
        "bottom": int(float(item.get("bottom") or 0)),
    }
    return True


def voice_duration_has_transcribed_text_below(
    duration_item: dict[str, Any],
    ocr_items: list[dict[str, Any]],
    image_size: tuple[int, int],
    *,
    layout_snapshot: dict[str, Any] | None = None,
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
        if not voice_transcribe_item_is_in_chat_surface(item, image_size, layout_snapshot=layout_snapshot):
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
    candidates = voice_transcribe_click_candidate_points(
        {"click_bounds": [left, top, right, bottom]},
        min_points=min_points,
    )
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


def voice_duration_context_click_bounds(
    item: dict[str, Any],
    image_size: tuple[int, int],
    *,
    layout_snapshot: dict[str, Any] | None = None,
) -> list[int]:
    width, height = image_size
    viewport = win32_ocr_layout.required_region(layout_snapshot, "message_viewport_bounds")
    item_left = int(float(item.get("left") or 0))
    item_right = int(float(item.get("right") or 0))
    item_center_x = float(item.get("center_x") or 0)
    is_self_side_voice = item_center_x > width * 0.62
    left = max(viewport[0], item_left - (42 if is_self_side_voice else 18))
    top = max(viewport[1], int(float(item.get("top") or 0)) - 16)
    # Right-side/self voice bubbles sit immediately beside the avatar. Keep the
    # context-menu click inside the green bubble so jitter cannot land on avatar.
    right_padding = 18 if is_self_side_voice else 78
    right_limit = viewport[2]
    right = min(right_limit, item_right + right_padding)
    bottom = min(viewport[3], int(float(item.get("bottom") or 0)) + 16)
    if right <= left:
        right = min(right_limit, left + 64)
    if bottom <= top:
        bottom = min(viewport[3], top + 28)
    return [left, top, right, bottom]


def voice_duration_context_click_target(
    duration_target: dict[str, Any],
    image_size: tuple[int, int],
    *,
    layout_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
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
    try:
        bounds = voice_duration_context_click_bounds(item, image_size, layout_snapshot=layout_snapshot)
    except win32_ocr_layout.LayoutSnapshotError:
        return None
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


def visual_component_rejected_by_non_voice_slot(
    component: dict[str, Any],
    parsed_messages: list[dict[str, Any]] | None,
) -> bool:
    bounds = component_bounds(component)
    if not bounds:
        return False
    message = parsed_message_overlapping_bounds(bounds, parsed_messages, pad=10.0)
    return bool(message and not message_is_voice_record(message))


def evidence_overlaps_image_slot(
    evidence: dict[str, Any],
    parsed_messages: list[dict[str, Any]] | None,
) -> bool:
    bounds = component_bounds(evidence)
    if not bounds or not parsed_messages:
        return False
    for message in parsed_messages:
        if not isinstance(message, dict):
            continue
        message_type = str(message.get("type") or message.get("message_type") or "").strip().lower()
        if message_type != "image":
            continue
        image_bounds = message_rect_bounds(message)
        if image_bounds and rects_overlap_or_near(bounds, image_bounds, pad=0.0):
            return True
    return False


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
    layout_snapshot: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    if not parsed_messages:
        return []
    width, _height = image_size
    try:
        viewport = win32_ocr_layout.required_region(layout_snapshot, "message_viewport_bounds")
    except win32_ocr_layout.LayoutSnapshotError:
        return []
    top_limit, bottom_limit = viewport[1], viewport[3]
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
        if right < viewport[0]:
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
            safe_left = max(viewport[0], int(left) + 8)
            safe_right = min(viewport[2], int(left) + min(112, max(44, int((right - left) * 0.72))))
        else:
            safe_left = max(viewport[0], int(left) + 8)
            safe_right = min(viewport[2], int(right) - 8)
        safe_top = max(top_limit, int(top) + 5)
        safe_bottom = min(bottom_limit, int(bottom) - 5)
        if safe_right <= safe_left:
            safe_right = min(viewport[2], safe_left + 44)
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
    layout_snapshot: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    anchors: list[dict[str, Any]] = []
    excluded = excluded_anchor_keys or set()
    for item in ocr_items:
        if not voice_duration_item_like(item):
            continue
        if not voice_transcribe_item_is_in_chat_surface(item, image_size, layout_snapshot=layout_snapshot):
            continue
        anchor = voice_duration_context_click_target({"item": item}, image_size, layout_snapshot=layout_snapshot)
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
    layout_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    anchors = [
        anchor
        for anchor in voice_duration_context_anchor_targets(
            ocr_items,
            image_size,
            excluded_anchor_keys=excluded_anchor_keys,
            layout_snapshot=layout_snapshot,
        )
        if not voice_duration_has_transcribed_text_below(
            anchor.get("item") if isinstance(anchor.get("item"), dict) else {},
            ocr_items,
            image_size,
            layout_snapshot=layout_snapshot,
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
    snapshot = layout_snapshot_for_image(image)
    try:
        viewport = win32_ocr_layout.required_region(snapshot, "message_viewport_bounds")
    except win32_ocr_layout.LayoutSnapshotError:
        return []
    top_limit, bottom_limit = viewport[1], viewport[3]
    left_limit = viewport[0]
    right_limit = viewport[2]
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
        chat_width = max(1, viewport[2] - viewport[0])
        if left < viewport[0] or left > viewport[0] + int(chat_width * 0.42):
            continue
        if center_x > viewport[0] + int(chat_width * 0.62):
            continue
        if gray_count < 850:
            continue
        if visual_component_rejected_by_non_voice_slot(component, parsed_messages):
            continue
        if visual_component_overlaps_transcribed_parser_voice(component, parsed_messages, image_size):
            continue
        if visual_customer_voice_component_overlaps_text(
            component, ocr_items or [], image_size, layout_snapshot=snapshot
        ):
            continue
        if visual_voice_component_has_transcribed_layout_below(
            component, ocr_items or [], image_size, role="customer", layout_snapshot=snapshot
        ):
            continue
        safe_left = max(viewport[0], left + 8)
        safe_right = min(viewport[2], right - 8)
        safe_top = max(top_limit, top + 5)
        safe_bottom = min(bottom_limit, bottom - 5)
        if safe_right <= safe_left:
            safe_right = min(viewport[2], safe_left + 44)
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
    layout_snapshot: dict[str, Any] | None = None,
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
        if not voice_transcribe_item_is_in_chat_surface(item, image_size, layout_snapshot=layout_snapshot):
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
    *,
    layout_snapshot: dict[str, Any] | None = None,
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
        if not voice_transcribe_item_is_in_chat_surface(item, image_size, layout_snapshot=layout_snapshot):
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
    snapshot = layout_snapshot_for_image(image)
    try:
        viewport = win32_ocr_layout.required_region(snapshot, "message_viewport_bounds")
    except win32_ocr_layout.LayoutSnapshotError:
        return []
    top_limit, bottom_limit = viewport[1], viewport[3]
    chat_width = max(1, viewport[2] - viewport[0])
    left_limit = viewport[0] + int(chat_width * 0.48)
    right_limit = viewport[2]
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
        if center_x < viewport[0] + int(chat_width * 0.52) or right < viewport[0] + int(chat_width * 0.60):
            continue
        if green_count < 220:
            continue
        if visual_component_rejected_by_non_voice_slot(component, parsed_messages):
            continue
        if visual_component_overlaps_transcribed_parser_voice(component, parsed_messages, image_size):
            continue
        if visual_self_voice_component_overlaps_text(
            component, ocr_items or [], image_size, layout_snapshot=snapshot
        ):
            continue
        if visual_voice_component_has_transcribed_layout_below(
            component, ocr_items or [], image_size, role="self", layout_snapshot=snapshot
        ):
            continue
        safe_left = max(viewport[0], left + 8)
        safe_right = min(viewport[2], left + min(110, max(44, int(bubble_width * 0.72))))
        safe_top = max(top_limit, top + 5)
        safe_bottom = min(bottom_limit, bottom - 5)
        if safe_right <= safe_left:
            safe_right = min(viewport[2], safe_left + 44)
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
    *,
    layout_snapshot: dict[str, Any] | None = None,
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
        if not voice_transcribe_item_is_in_chat_surface(item, image_size, layout_snapshot=layout_snapshot):
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
    layout_snapshot = layout_snapshot_for_image(image)
    if not isinstance(layout_snapshot, dict) or not layout_snapshot.get("valid"):
        return []
    layout_snapshot_id = str(layout_snapshot.get("layout_snapshot_id") or "")
    voice_ocr_items = [
        item
        for item in ocr_items
        if isinstance(item, dict) and not evidence_overlaps_image_slot(item, messages)
    ]
    excluded = excluded_anchor_keys or set()
    parser_targets = {
        str((target.get("item") or {}).get("message_id") or ""): normalize_voice_evidence_target(image, target, image_size)
        for target in message_voice_context_anchor_targets(
            messages, image_size, layout_snapshot=layout_snapshot
        )
    }
    for target in parser_targets.values():
        target["layout_snapshot_id"] = layout_snapshot_id
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
        normalized["layout_snapshot_id"] = layout_snapshot_id
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

    for raw_target in voice_duration_context_anchor_targets(
        voice_ocr_items, image_size, layout_snapshot=layout_snapshot
    ):
        raw_item = raw_target.get("item") if isinstance(raw_target.get("item"), dict) else {}
        raw_state = "transcribed" if voice_duration_has_transcribed_text_below(
            raw_item, voice_ocr_items, image_size, layout_snapshot=layout_snapshot
        ) else "untranscribed"
        merge_evidence(raw_target, "ocr_duration", inferred_state=raw_state)

    for visual_target in find_visual_customer_voice_context_anchor_targets(
        image,
        image_size,
        voice_ocr_items,
        parsed_messages=messages,
    ):
        merge_evidence(visual_target, "visual_customer_bubble")
    for visual_target in find_visual_self_voice_context_anchor_targets(
        image,
        image_size,
        voice_ocr_items,
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
    for button in find_voice_transcribe_targets(
        voice_ocr_items,
        image_size,
        allow_inferred=False,
        layout_snapshot=layout_snapshot,
    ):
        if not pending:
            break
        button_y = voice_target_center_y(button)
        nearest = min(pending, key=lambda observation: abs(button_y - voice_target_center_y(observation.get("action_target"))))
        if abs(button_y - voice_target_center_y(nearest.get("action_target"))) <= 96.0:
            nearest["visible_button_target"] = button
            nearest["visible_button_target"]["layout_snapshot_id"] = layout_snapshot_id
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
        source_message = {
            str(key): value
            for key, value in message.items()
            if str(key) in C2_SOURCE_MESSAGE_TRANSPORT_FIELDS
        }
        # Worker owns durable source identity.  OmniAuto must not leak its
        # legacy frame-local envelope key into the formal C2 contract.
        source_message.pop("source_message_key", None)
        source_message.update(
            {
                "message_type": (
                    "voice" if msg_type == "voice" else msg_type
                ),
                "sender_role": role,
                "sender_role_source": role_source,
                "content": (
                    ""
                    if untranscribed
                    else str(message.get("content") or "").strip()
                ),
                "content_clean": (
                    ""
                    if untranscribed
                    else str(message.get("content") or "").strip()
                ),
            }
        )
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
            "source_message": source_message,
        }
        frame_action_binding = (
            message.get("_frame_action_binding")
            if isinstance(message.get("_frame_action_binding"), dict)
            else None
        )
        if frame_action_binding is not None:
            projected_binding = {
                field: frame_action_binding.get(field)
                for field in C2_FRAME_ACTION_BINDING_FIELDS
            }
            projected_binding["post_observation_id"] = observation_id
            observation[C2_FRAME_ACTION_BINDING_CONTAINER] = (
                projected_binding
            )
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


def _structural_image_identity(message: dict[str, Any]) -> str:
    if str(
        message.get("type") or message.get("message_type") or ""
    ).strip().lower() != "image":
        return ""
    return str(
        message.get("frame_visual_id")
        or message.get("message_id")
        or message.get("id")
        or ""
    ).strip()


def merge_structural_image_messages(
    screenshot: Image.Image | None,
    ocr_items: list[dict[str, Any]],
    messages: list[dict[str, Any]],
    *,
    target: str,
    layout_snapshot: dict[str, Any] | None = None,
    observation_validation_errors: list[dict[str, Any]] | None = None,
    voice_action_attempts: list[dict[str, Any]] | None = None,
    image_candidate_diagnostics: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    merged = [dict(item) for item in messages if isinstance(item, dict)]
    if screenshot is None:
        return merged

    def image_observation_failed(
        stage: str,
        exc: Exception,
    ) -> list[dict[str, Any]]:
        error = {
            "observation_id": "structural-image-observer",
            "row_kind": "image_bubble",
            "error_codes": ["C2_IMAGE_OBSERVATION_FAILED"],
            "stage": str(stage),
            "error_type": type(exc).__name__,
        }
        if observation_validation_errors is None:
            raise RuntimeError(
                f"C2_IMAGE_OBSERVATION_FAILED:{stage}:{type(exc).__name__}"
            ) from exc
        observation_validation_errors.append(error)
        return merged

    try:
        from apps.wechat_ai_customer_service.optional_plugins.vision.capture.surface import (
            messages_outside_image_bubbles,
            observe_structural_image_messages,
        )

        layout_snapshot = layout_snapshot or layout_snapshot_for_image(screenshot)
        viewport = (
            list((layout_snapshot or {}).get("message_viewport_bounds") or [])
            if isinstance(layout_snapshot, dict)
            else []
        )
        if not bool((layout_snapshot or {}).get("valid")) or len(viewport) != 4:
            raise RuntimeError("WECHAT_UI_LAYOUT_UNRESOLVED")

        def resolve_role_from_same_layout(
            image: Any,
            bounds: list[float],
            image_size: tuple[int, int],
        ) -> dict[str, Any]:
            return message_row_avatar_role_details(
                image,
                bounds,
                image_size,
                layout_snapshot=layout_snapshot,
            )

        image_messages = observe_structural_image_messages(
            screenshot,
            ocr_items,
            merged,
            target=target,
            role_resolver=resolve_role_from_same_layout,
            max_images=int(
                (
                    _C2_GENERATED_SCHEMA.get("image_contract") or {}
                ).get("source_limits", {}).get(
                    "max_visible_image_candidates",
                    64,
                )
            ),
            voice_action_attempts=voice_action_attempts,
            diagnostics=image_candidate_diagnostics,
            message_viewport_bounds=viewport,
        )
    except Exception as exc:
        return image_observation_failed(
            str(
                getattr(
                    exc,
                    "stage",
                    "structural_image_observation",
                )
            ),
            exc,
        )
    # A reused open-chat frame may already contain structural image messages.
    # Re-observing that frame must replace current evidence, not create another
    # occurrence. Genuine repeated bubbles have different physical occurrence
    # anchors and therefore different canonical ids.
    observed_image_ids = {
        _structural_image_identity(item)
        for item in image_messages
        if isinstance(item, dict)
    }
    observed_image_ids.discard("")
    merged = [
        item
        for item in messages_outside_image_bubbles(merged, image_messages)
        if _structural_image_identity(item) not in observed_image_ids
    ]
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
    snapshot = layout_snapshot_for_image(image)
    try:
        viewport = win32_ocr_layout.required_region(snapshot, "message_viewport_bounds")
    except win32_ocr_layout.LayoutSnapshotError:
        return None
    targets: list[dict[str, Any]] = []
    for item in ocr_items:
        if not voice_duration_item_like(item):
            continue
        if not voice_transcribe_item_is_in_chat_surface(item, image_size, layout_snapshot=snapshot):
            continue
        if voice_duration_has_transcribed_text_below(item, ocr_items, image_size, layout_snapshot=snapshot):
            continue
        center_y = int(float(item.get("center_y") or 0))
        voice_left = int(float(item.get("left") or 0))
        voice_right = int(float(item.get("right") or 0))
        width, height = image_size
        is_self_side_voice = float(item.get("center_x") or 0) > width * 0.62
        if is_self_side_voice:
            left = max(viewport[0], voice_left - 154)
            right = max(viewport[0], voice_left - 70)
        else:
            left = max(viewport[0], voice_right + 70)
            right = min(viewport[2], voice_right + 154)
        top = max(viewport[1], center_y - 18)
        bottom = min(viewport[3], center_y + 18)
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


def wait_for_wechat_context_menu_stable() -> int:
    """Wait until a WeChat desktop context menu is stable enough to OCR."""

    raw_wait_ms = os.getenv("WECHAT_WIN32_OCR_CONTEXT_MENU_WAIT_MS")
    if raw_wait_ms in (None, ""):
        raw_wait_ms = os.getenv("WECHAT_WIN32_OCR_VOICE_CONTEXT_MENU_WAIT_MS")
    menu_wait_ms = bounded_int(
        raw_wait_ms,
        default=1800,
        minimum=400,
        maximum=5000,
    )
    humanized_action_sleep(max(350, menu_wait_ms - 250), menu_wait_ms + 450)
    return menu_wait_ms


def resolve_wechat_context_menu_bounds(
    hwnd: int,
    *,
    anchor_screen: tuple[int, int] | list[int],
) -> dict[str, Any]:
    """Resolve the real popup window nearest the right-click anchor.

    OCR coordinates are deliberately not used to invent a menu boundary.
    Without a distinct visible WeChat-owned popup window, image-menu
    classification must fail closed.
    """

    if win32gui is None or win32process is None or int(hwnd or 0) <= 0:
        return {"ok": False, "reason": "context_menu_window_probe_unavailable"}
    try:
        anchor_x = int(anchor_screen[0])
        anchor_y = int(anchor_screen[1])
        main_rect = tuple(int(value) for value in win32gui.GetWindowRect(hwnd))
        main_pid = int(win32process.GetWindowThreadProcessId(hwnd)[1] or 0)
    except (AttributeError, TypeError, ValueError, IndexError):
        return {"ok": False, "reason": "context_menu_window_probe_invalid"}
    if main_pid <= 0 or len(main_rect) != 4:
        return {"ok": False, "reason": "context_menu_window_probe_invalid"}

    candidates: list[dict[str, Any]] = []

    def point_distance(rect: tuple[int, int, int, int]) -> float:
        left, top, right, bottom = rect
        dx = max(left - anchor_x, 0, anchor_x - right)
        dy = max(top - anchor_y, 0, anchor_y - bottom)
        return float((dx * dx + dy * dy) ** 0.5)

    def collect(candidate_hwnd: int, _extra: Any) -> bool:
        try:
            candidate_hwnd = int(candidate_hwnd or 0)
            if candidate_hwnd <= 0 or candidate_hwnd == int(hwnd):
                return True
            if not bool(win32gui.IsWindowVisible(candidate_hwnd)):
                return True
            if int(
                win32process.GetWindowThreadProcessId(candidate_hwnd)[1] or 0
            ) != main_pid:
                return True
            rect = tuple(
                int(value) for value in win32gui.GetWindowRect(candidate_hwnd)
            )
            if len(rect) != 4:
                return True
            left, top, right, bottom = rect
            width = right - left
            height = bottom - top
            main_width = max(1, main_rect[2] - main_rect[0])
            main_height = max(1, main_rect[3] - main_rect[1])
            distance = point_distance(rect)
            if (
                width < 72
                or height < 36
                or width > min(640, main_width)
                or height > min(960, main_height)
                or width * height >= main_width * main_height * 0.5
                or distance > 48.0
            ):
                return True
            candidates.append(
                {
                    "hwnd": candidate_hwnd,
                    "bounds": [left, top, right, bottom],
                    "distance": distance,
                    "contains_anchor": (
                        left <= anchor_x <= right and top <= anchor_y <= bottom
                    ),
                    "class_name": str(
                        win32gui.GetClassName(candidate_hwnd) or ""
                    ),
                }
            )
        except Exception:
            return True
        return True

    try:
        win32gui.EnumWindows(collect, None)
    except Exception:
        return {"ok": False, "reason": "context_menu_window_enumeration_failed"}
    if not candidates:
        return {"ok": False, "reason": "context_menu_popup_window_not_found"}
    selected = min(
        candidates,
        key=lambda item: (
            not bool(item["contains_anchor"]),
            float(item["distance"]),
            (item["bounds"][2] - item["bounds"][0])
            * (item["bounds"][3] - item["bounds"][1]),
        ),
    )
    return {
        "ok": True,
        "reason": "context_menu_popup_window_confirmed",
        "menu_panel_bounds": list(selected["bounds"]),
        "menu_hwnd": int(selected["hwnd"]),
        "menu_class_name": str(selected["class_name"]),
    }


def observe_wechat_context_menu(
    hwnd: int,
    *,
    anchor_screen: tuple[int, int] | list[int],
    artifact_dir: str | None = None,
    label: str = "wechat_context_menu",
    ocr_runner: Any | None = None,
) -> dict[str, Any]:
    """Capture one popup and OCR its anchor ROI without reactivating WeChat."""

    if int(hwnd or 0) <= 0:
        return {"ok": False, "reason": "context_menu_window_invalid"}
    try:
        anchor_x = int(anchor_screen[0])
        anchor_y = int(anchor_screen[1])
    except (TypeError, ValueError, IndexError):
        return {"ok": False, "reason": "context_menu_anchor_missing"}
    popup = resolve_wechat_context_menu_bounds(
        hwnd,
        anchor_screen=(anchor_x, anchor_y),
    )
    if popup.get("ok") is not True:
        return popup
    menu_bounds = [
        int(value) for value in popup.get("menu_panel_bounds") or []
    ]
    if len(menu_bounds) != 4:
        return {"ok": False, "reason": "context_menu_popup_bounds_invalid"}
    screenshot = None
    ocr_image = None
    roi_screenshot_path = ""
    try:
        menu_hwnd = int(popup.get("menu_hwnd") or 0)
        screenshot, screenshot_path = capture_wechat_window_visible_screen(
            menu_hwnd,
            artifact_dir=artifact_dir,
            label=label,
            popup_window=True,
        )
        menu_window_rect = get_window_geometry(menu_hwnd)
        menu_origin = [
            int(menu_window_rect.get("left") or 0),
            int(menu_window_rect.get("top") or 0),
        ]
        local_menu_bounds = [
            menu_bounds[0] - menu_origin[0],
            menu_bounds[1] - menu_origin[1],
            menu_bounds[2] - menu_origin[0],
            menu_bounds[3] - menu_origin[1],
        ]
        width, height = getattr(screenshot, "size", (0, 0))
        roi = [
            max(0, local_menu_bounds[0]),
            max(0, local_menu_bounds[1]),
            min(int(width), local_menu_bounds[2]),
            min(int(height), local_menu_bounds[3]),
        ]
        if roi[2] <= roi[0] or roi[3] <= roi[1]:
            raise RuntimeError("context_menu_ocr_roi_invalid")
        ocr_image = screenshot.crop(tuple(roi))
        roi_screenshot_path = save_screenshot_artifact(
            ocr_image,
            artifact_dir=artifact_dir,
            label=f"{label}_ocr_roi",
        )
        runner = ocr_runner if callable(ocr_runner) else run_ocr
        raw_ocr_items = runner(ocr_image)
        ocr_items: list[dict[str, Any]] = []
        for raw_item in raw_ocr_items:
            if not isinstance(raw_item, dict):
                continue
            item = dict(raw_item)
            for key in ("left", "right", "center_x"):
                if key in item:
                    item[key] = float(item.get(key) or 0) + roi[0]
            for key in ("top", "bottom", "center_y"):
                if key in item:
                    item[key] = float(item.get(key) or 0) + roi[1]
            ocr_items.append(item)
    except Exception as exc:
        close_ocr_image = getattr(ocr_image, "close", None)
        if callable(close_ocr_image):
            close_ocr_image()
        close = getattr(screenshot, "close", None)
        if callable(close):
            close()
        return {
            "ok": False,
            "reason": "context_menu_observation_failed",
            "error_type": type(exc).__name__,
        }
    close_ocr_image = getattr(ocr_image, "close", None)
    if callable(close_ocr_image):
        close_ocr_image()
    width, height = getattr(screenshot, "size", (0, 0))
    local_items: list[dict[str, Any]] = []
    for item in ocr_items:
        if not isinstance(item, dict):
            continue
        try:
            item_left = float(item.get("left") or 0)
            item_top = float(item.get("top") or 0)
            item_right = float(item.get("right") or 0)
            item_bottom = float(item.get("bottom") or 0)
        except (TypeError, ValueError):
            continue
        if (
            local_menu_bounds[0] <= item_left < item_right <= local_menu_bounds[2]
            and local_menu_bounds[1] <= item_top < item_bottom <= local_menu_bounds[3]
        ):
            local_items.append(item)
    return {
        "ok": True,
        "reason": "context_menu_observed",
        "image": screenshot,
        "image_size": (int(width), int(height)),
        "screen_origin": menu_origin,
        "menu_panel_bounds": local_menu_bounds,
        "menu_panel_screen_bounds": menu_bounds,
        "menu_window_evidence": {
            "hwnd": menu_hwnd,
            "class_name": str(popup.get("menu_class_name") or ""),
            "reason": str(popup.get("reason") or ""),
        },
        "ocr_items": ocr_items,
        "local_ocr_items": local_items,
        "ocr_item_count": len(ocr_items),
        "local_ocr_item_count": len(local_items),
        "ocr_roi": roi,
        "ocr_execution": "isolated_runner" if callable(ocr_runner) else "sidecar",
        "menu_structure_evidence": [
            {
                "text": str(item.get("text") or ""),
                "bounds": [
                    float(item.get("left") or 0),
                    float(item.get("top") or 0),
                    float(item.get("right") or 0),
                    float(item.get("bottom") or 0),
                ],
            }
            for item in local_items
            if normalize_ocr_text(item.get("text"))
            in {
                "复制", "复制图片", "编辑", "用窗口打开", "另存为", "打开方式",
                "放大阅读", "翻译", "搜一搜", "转发", "收藏", "多选", "删除", "引用",
                "语音转文字", "转文字", "收起文字",
            }
        ][:16],
        "local_ocr_evidence": [
            {
                "text": str(item.get("text") or ""),
                "confidence": item.get("confidence"),
                "bounds": [
                    float(item.get("left") or 0),
                    float(item.get("top") or 0),
                    float(item.get("right") or 0),
                    float(item.get("bottom") or 0),
                ],
            }
            for item in local_items
            if str(item.get("text") or "").strip()
        ][:64],
        "screenshot_path": screenshot_path,
        "roi_screenshot_path": roi_screenshot_path,
        "capture_mode": "wechat_window_visible_screen",
        "menu_hwnd": menu_hwnd,
        "layout_snapshot_id": str(
            (layout_snapshot_metadata(menu_hwnd).get("snapshot") or {}).get("layout_snapshot_id") or ""
        ),
        "anchor_screen": [anchor_x, anchor_y],
    }


def open_voice_transcribe_context_menu(
    hwnd: int,
    duration_target: dict[str, Any],
    *,
    image_size: tuple[int, int],
    artifact_dir: str | None = None,
) -> dict[str, Any]:
    expected_snapshot_id = str(duration_target.get("layout_snapshot_id") or "")
    current_snapshot = current_layout_snapshot(hwnd) or {}
    if not expected_snapshot_id or str(current_snapshot.get("layout_snapshot_id") or "") != expected_snapshot_id:
        return {
            "ok": False,
            "reason": "voice_target_layout_snapshot_stale",
            "error_code": win32_ocr_layout.ERROR_LAYOUT_STALE,
        }
    anchor = voice_duration_context_click_target(
        duration_target,
        image_size,
        layout_snapshot=current_snapshot,
    )
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
        expected_snapshot_id=expected_snapshot_id,
    )
    menu_wait_ms = wait_for_wechat_context_menu_stable()
    # The WeChat context menu is a desktop popup. On right-side/self voice
    # bubbles it can extend outside the WeChat window rectangle, so capture the
    # visible screen and click in screen coordinates instead of window coords.
    anchor_screen_y = int((right_click or {}).get("screen_y") or 0)
    anchor_screen_x = int((right_click or {}).get("screen_x") or 0)
    observation = observe_wechat_context_menu(
        hwnd,
        anchor_screen=(anchor_screen_x, anchor_screen_y),
        artifact_dir=artifact_dir,
        label="voice_transcribe_context_menu",
    )
    menu_items = [
        item
        for item in (observation.get("local_ocr_items") or [])
        if isinstance(item, dict)
    ]
    menu_size = tuple(observation.get("image_size") or image_size)
    menu_origin_y = int((observation.get("screen_origin") or [0, 0])[1] or 0)
    local_anchor_y = anchor_screen_y - menu_origin_y
    menu_target = find_voice_transcribe_menu_item_target(menu_items, menu_size, anchor=anchor, anchor_screen_y=local_anchor_y)
    collapse_target = find_voice_transcribe_menu_collapse_item_target(menu_items, menu_size, anchor=anchor, anchor_screen_y=local_anchor_y)
    snapshot_id = str(observation.get("layout_snapshot_id") or "")
    for target in (menu_target, collapse_target):
        if isinstance(target, dict):
            target["layout_snapshot_id"] = snapshot_id
            target["popup_hwnd"] = int(observation.get("menu_hwnd") or 0)
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
    menu_screenshot = observation.get("image")
    close_menu_screenshot = getattr(menu_screenshot, "close", None)
    if callable(close_menu_screenshot):
        close_menu_screenshot()
    return {
        "ok": bool(right_click.get("ok") and (menu_target or collapse_target)),
        "right_click": right_click,
        "anchor": anchor,
        "anchor_point": [anchor_x, anchor_y],
        "anchor_jitter": anchor_jitter,
        "menu_wait_ms": menu_wait_ms,
        "menu_screenshot_path": str(observation.get("screenshot_path") or ""),
        "menu_capture_mode": str(observation.get("capture_mode") or "visible_screen"),
        "menu_local_radius": local_radius,
        "menu_ocr_items_count": int(observation.get("ocr_item_count") or 0),
        "menu_local_ocr_items_count": len(menu_items),
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
        target["coordinate_space"] = "window_image"
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
        target["coordinate_space"] = "window_image"
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
        safe_target = safe_window_header_blank_click_target(
            before_items,
            before_shot.size,
            geometry=geometry,
            layout_snapshot=layout_snapshot_for_image(before_shot),
        )
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
            expected_snapshot_id=str(
                (layout_snapshot_metadata(hwnd).get("snapshot") or {}).get("layout_snapshot_id") or ""
            ),
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
            screenshot, screenshot_path = capture_wechat_window_visible_screen(
                hwnd,
                artifact_dir=artifact_dir,
                label=label,
            )
            items = run_ocr(screenshot)
            visible_menu_texts = voice_transcribe_menu_texts_from_items(items, menu_bounds=menu_bounds)
            visible_panel_texts = chat_info_panel_texts_from_items(items)
            result.update({
                "verified": True,
                "screenshot_path": screenshot_path,
                "ocr_items_count": len(items),
                "visible_menu_texts": visible_menu_texts,
                "visible_panel_texts": visible_panel_texts,
                "menu_panel_bounds": menu_bounds or [],
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
    layout_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Find a fresh blank title-bar segment away from chat content and controls."""
    width, height = image_size
    if width < 700 or height < 260:
        return None
    try:
        chat_header = win32_ocr_layout.required_region(layout_snapshot, "chat_header_bounds")
    except win32_ocr_layout.LayoutSnapshotError:
        return None
    # Reserve the outer portions for title text and WeChat controls; the
    # actual zone still derives from the current header, never a reference x.
    header_width = max(1, chat_header[2] - chat_header[0])
    zone_left = chat_header[0] + int(header_width * 0.30)
    zone_right = chat_header[0] + int(header_width * 0.68)
    zone_top = chat_header[1] + max(2, int((chat_header[3] - chat_header[1]) * 0.08))
    zone_bottom = chat_header[1] + max(16, int((chat_header[3] - chat_header[1]) * 0.45))
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
    hwnd: int = 0,
    artifact_dir: str | None = None,
    label: str = "voice_transcribe_context_menu_after_click",
    menu_bounds: list[int] | None = None,
) -> dict[str, Any]:
    try:
        screenshot, screenshot_path = capture_wechat_window_visible_screen(
            hwnd,
            artifact_dir=artifact_dir,
            label=label,
        )
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
        if str(menu_target.get("coordinate_space") or "") != "window_image":
            click = {
                "ok": False,
                "error_code": win32_ocr_layout.ERROR_COORDINATE_MAPPING_INVALID,
                "reason": "context_menu_target_origin_not_bound_to_popup_snapshot",
            }
        else:
            click = human_window_image_click_in_bounds(
                int(menu_target.get("popup_hwnd") or hwnd),
                click_x,
                click_y,
                bounds=menu_bounds,
                action_name="voice_transcribe_context_menu_click",
                expected_snapshot_id=str(menu_target.get("layout_snapshot_id") or ""),
            )
        humanized_action_sleep(260, 620)
        verification = verify_voice_transcribe_context_menu_closed(
            hwnd=int(menu_target.get("popup_hwnd") or hwnd),
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
    layout_snapshot: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    width, height = image_size
    try:
        viewport = win32_ocr_layout.required_region(layout_snapshot, "message_viewport_bounds")
    except win32_ocr_layout.LayoutSnapshotError:
        return []
    direct_targets: list[dict[str, Any]] = []
    for item in ocr_items:
        text = str(item.get("text") or "")
        if not voice_transcribe_button_text_like(text):
            continue
        if not voice_transcribe_item_is_in_chat_surface(item, image_size, layout_snapshot=layout_snapshot):
            continue
        left = max(viewport[0], int(float(item.get("left") or 0)) - 18)
        top = max(viewport[1], int(float(item.get("top") or 0)) - 12)
        right = min(viewport[2], int(float(item.get("right") or 0)) + 18)
        bottom = min(viewport[3], int(float(item.get("bottom") or 0)) + 12)
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
        if not voice_transcribe_item_is_in_chat_surface(item, image_size, layout_snapshot=layout_snapshot):
            continue
        if voice_duration_has_transcribed_text_below(item, ocr_items, image_size, layout_snapshot=layout_snapshot):
            continue
        center_y = int(float(item.get("center_y") or 0))
        voice_left = int(float(item.get("left") or 0))
        voice_right = int(float(item.get("right") or 0))
        is_self_side_voice = float(item.get("center_x") or 0) > width * 0.62
        if is_self_side_voice:
            left = max(viewport[0], voice_left - 154)
            right = max(viewport[0], voice_left - 70)
        else:
            left = max(viewport[0], voice_right + 70)
            right = min(viewport[2], voice_right + 154)
        top = max(viewport[1], center_y - 18)
        bottom = min(viewport[3], center_y + 18)
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
    layout_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    targets = find_voice_transcribe_targets(
        ocr_items,
        image_size,
        allow_inferred=allow_inferred,
        layout_snapshot=layout_snapshot,
    )
    return targets[-1] if targets else None


def voice_transcribe_click_candidate_points(target: dict[str, Any], *, min_points: int = 10) -> list[tuple[int, int]]:
    bounds = target.get("click_bounds") if isinstance(target, dict) else None
    if not isinstance(bounds, list) or len(bounds) < 4:
        return []
    left, top, right, bottom = [int(value) for value in bounds[:4]]
    if right <= left or bottom <= top:
        return []
    requested = max(1, min(16, int(min_points or 1)))
    relative_points = (
        (0.50, 0.50),
        (0.30, 0.30),
        (0.70, 0.30),
        (0.30, 0.70),
        (0.70, 0.70),
        (0.50, 0.25),
        (0.50, 0.75),
        (0.25, 0.50),
        (0.75, 0.50),
        (0.40, 0.40),
        (0.60, 0.40),
        (0.40, 0.60),
        (0.60, 0.60),
        (0.20, 0.20),
        (0.80, 0.20),
        (0.80, 0.80),
    )
    points: list[tuple[int, int]] = []
    for x_ratio, y_ratio in relative_points:
        point = (
            max(left, min(right, int(round(left + (right - left) * x_ratio)))),
            max(top, min(bottom, int(round(top + (bottom - top) * y_ratio)))),
        )
        if point not in points:
            points.append(point)
        if len(points) >= requested:
            break
    return points


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
    layout_snapshot: dict[str, Any] | None = None,
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
    snapshot = layout_snapshot or layout_snapshot_for_image(screenshot)
    try:
        viewport = win32_ocr_layout.required_region(snapshot, "message_viewport_bounds")
    except win32_ocr_layout.LayoutSnapshotError:
        return {"present": False, "score": 0.0, "reason": "layout_unresolved"}
    bubble_left, bubble_top, bubble_right, bubble_bottom = [float(value) for value in bounds[:4]]
    if role == "customer":
        lane_left = max(viewport[0], int(round(bubble_left - 140.0)))
        lane_right = min(int(round(bubble_left - 4.0)), viewport[0] + 150)
    else:
        lane_left = max(int(round(bubble_right + 4.0)), viewport[2] - 150)
        lane_right = viewport[2]
    lane_left = max(0, int(lane_left))
    lane_right = min(width, int(lane_right))
    if lane_right - lane_left < 20:
        return {"present": False, "score": 0.0, "reason": "avatar_lane_empty"}
    row_center = min((bubble_top + bubble_bottom) / 2.0, bubble_top + 24.0)
    crop_centers = [row_center]
    if bubble_bottom - bubble_top > 64.0:
        # A tall image/card can begin below the avatar top.  The ordinary
        # bubble-centred crop then sees only the avatar's lower strip and
        # incorrectly reports an unknown sender.  Probe the row's leading
        # edge as a second, bounded lane without widening horizontally or
        # borrowing evidence from another message row.
        crop_centers.append(bubble_top)

    candidates: list[dict[str, Any]] = []
    for crop_center in crop_centers:
        crop_top = max(
            viewport[1],
            int(round(crop_center - 24.0)),
        )
        crop_bottom = min(
            viewport[3],
            crop_top + 48,
        )
        if crop_bottom - crop_top < 20:
            continue
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
        bubble_leading_center_y = bubble_top
        bubble_regular_center_y = min(
            (bubble_top + bubble_bottom) / 2.0,
            bubble_top + 24.0,
        )
        vertical_distance = min(
            abs(component_center_y - bubble_regular_center_y),
            abs(component_center_y - bubble_leading_center_y),
        )
        horizontal_gap = (
            bubble_left - component_bounds[2]
            if role == "customer"
            else component_bounds[0] - bubble_right
        )
        max_gap = 150.0 if role == "customer" else 320.0
        relative_alignment = bool(
            -20.0 <= horizontal_gap <= max_gap
            and vertical_distance <= 30.0
        )
        score = color_stddev + edge_ratio * 180.0
        present = bool(
            avatar_sized_component
            and relative_alignment
            and color_stddev >= 14.0
            and edge_ratio >= 0.018
            and score >= 22.0
        )
        candidates.append({
            "present": present,
            "score": round(score, 4),
            "color_stddev": round(color_stddev, 4),
            "edge_ratio": round(edge_ratio, 6),
            "foreground_ratio": round(foreground_ratio, 6),
            "foreground_bounds_size": [foreground_width, foreground_height],
            "foreground_bounds": component_bounds,
            "horizontal_gap": round(horizontal_gap, 2),
            "vertical_distance": round(vertical_distance, 2),
            "relative_alignment": relative_alignment,
            "avatar_sized_component": avatar_sized_component,
            "bounds": [lane_left, crop_top, lane_right, crop_bottom],
            "position_source": "bubble_relative_avatar_adjacency",
            "reason": "avatar_relative_structure" if present else "avatar_relative_structure_not_found",
        })

    if not candidates:
        return {"present": False, "score": 0.0, "reason": "avatar_lane_empty"}
    return max(
        candidates,
        key=lambda item: (
            bool(item.get("present")),
            bool(item.get("avatar_sized_component")),
            float(item.get("score") or 0.0),
        ),
    )


def message_row_avatar_role_details(
    screenshot: Any | None,
    bounds: list[float],
    image_size: tuple[int, int],
    *,
    layout_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    customer = avatar_lane_visual_score(
        screenshot,
        bounds=bounds,
        role="customer",
        image_size=image_size,
        layout_snapshot=layout_snapshot,
    )
    self_side = avatar_lane_visual_score(
        screenshot,
        bounds=bounds,
        role="self",
        image_size=image_size,
        layout_snapshot=layout_snapshot,
    )
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
        ocr_started = time.perf_counter()
        ocr_items = run_ocr(screenshot)
        ocr_total_duration_ms = round(
            (time.perf_counter() - ocr_started) * 1000
        )
        parsed_messages = parse_current_chat_frame_messages(
            ocr_items,
            screenshot.size,
            target=target,
            screenshot=screenshot,
        )
        snapshots.append(
            {
                "label": label,
                "screenshot_path": path,
                "screenshot": screenshot,
                "ocr_items": ocr_items,
                "ocr_call_count": 1,
                "ocr_total_duration_ms": ocr_total_duration_ms,
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
        ocr_started = time.perf_counter()
        ocr_items = run_ocr(screenshot)
        ocr_total_duration_ms = round(
            (time.perf_counter() - ocr_started) * 1000
        )
        parsed_messages = parse_current_chat_frame_messages(
            ocr_items,
            screenshot.size,
            target=target,
            screenshot=screenshot,
        )
        snapshots.append(
            {
                "label": label,
                "screenshot_path": path,
                "screenshot": screenshot,
                "ocr_items": ocr_items,
                "ocr_call_count": 1,
                "ocr_total_duration_ms": ocr_total_duration_ms,
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


def add_friend_login_or_security_block(
    ocr_items: list[dict[str, Any]],
    *,
    geometry: dict[str, Any] | None = None,
    image_size: tuple[int, int] | None = None,
    layout_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return win32_ocr_add_friend_windows.add_friend_login_or_security_block(
        ocr_items,
        geometry=geometry,
        image_size=image_size,
        layout_snapshot=layout_snapshot,
    )


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


def add_friend_plus_entry_target(
    geometry: dict[str, Any],
    image_size: tuple[int, int],
    ocr_items: list[dict[str, Any]] | None = None,
    *,
    screenshot: Any | None = None,
    route_kind: str = 'windows',
    layout_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return win32_ocr_add_friend_windows.add_friend_plus_entry_target(
        geometry,
        image_size,
        ocr_items,
        screenshot=screenshot,
        route_kind=route_kind,
        layout_snapshot=layout_snapshot,
    )


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


def add_friend_popup_menu_bounds(
    image_size: tuple[int, int],
    *,
    plus_image_x: int,
    plus_image_y: int,
    layout_snapshot: dict[str, Any] | None = None,
) -> list[int]:
    return win32_ocr_add_friend_windows.add_friend_popup_menu_bounds(
        image_size,
        plus_image_x=plus_image_x,
        plus_image_y=plus_image_y,
        layout_snapshot=layout_snapshot,
    )


def run_ocr_on_screen_region(
    image: Image.Image,
    bounds: list[int],
    *,
    purpose: str = "screen_region",
    source: str = "run_ocr_on_screen_region",
) -> list[dict[str, Any]]:
    if not isinstance(bounds, (list, tuple)) or len(bounds) < 4:
        raise win32_ocr_layout.LayoutSnapshotError(
            "ocr_region_bounds_missing",
            code=win32_ocr_layout.ERROR_LAYOUT_UNRESOLVED,
            details={
                "bounds": list(bounds or [])
                if isinstance(bounds, (list, tuple))
                else []
            },
        )
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


def enhanced_ocr_items_for_structural_chat_candidate(
    screenshot: Any,
    bounds: list[float] | tuple[float, ...],
    *,
    ocr_runner: Callable[[Any], list[dict[str, Any]]] | None = None,
) -> list[dict[str, Any]]:
    """Run one enhanced OCR pass inside a structural chat candidate.

    The full-window OCR can miss pale or coloured native WeChat bubbles while
    the structural observer still sees their rectangular surface.  This pass
    is deliberately scoped to a previously observed candidate; callers must
    still provide independent type/identity evidence before changing the
    candidate's message type.
    """

    if screenshot is None or not hasattr(screenshot, "crop") or len(bounds) < 4:
        return []
    width, height = getattr(screenshot, "size", (0, 0))
    if int(width or 0) <= 0 or int(height or 0) <= 0:
        return []
    try:
        raw_left, raw_top, raw_right, raw_bottom = [
            float(value) for value in bounds[:4]
        ]
    except (TypeError, ValueError):
        return []
    padding = 4
    left = max(0, min(int(width) - 1, int(raw_left) - padding))
    top = max(0, min(int(height) - 1, int(raw_top) - padding))
    right = max(left + 1, min(int(width), int(raw_right) + padding))
    bottom = max(top + 1, min(int(height), int(raw_bottom) + padding))
    try:
        crop = screenshot.crop((left, top, right, bottom)).convert("RGB")
        crop = ImageEnhance.Contrast(crop).enhance(1.55)
        crop = ImageEnhance.Sharpness(crop).enhance(1.45)
        scale = 2.0
        resampling = getattr(
            getattr(Image, "Resampling", Image),
            "LANCZOS",
            1,
        )
        enhanced = crop.resize(
            (max(1, int(crop.width * scale)), max(1, int(crop.height * scale))),
            resampling,
        )
        if ocr_runner is None:
            crop_items = run_ocr_traced(
                enhanced,
                "structural_chat_candidate_enhanced_ocr",
                region="roi",
                source="enhanced_ocr_items_for_structural_chat_candidate",
            )
        else:
            crop_items = ocr_runner(enhanced)
    except Exception:
        return []

    mapped: list[dict[str, Any]] = []
    for item in crop_items or []:
        if not isinstance(item, dict) or not str(item.get("text") or "").strip():
            continue
        row = dict(item)
        for key in ("left", "right", "center_x"):
            if key in row:
                try:
                    row[key] = float(row[key]) / scale + left
                except (TypeError, ValueError):
                    pass
        for key in ("top", "bottom", "center_y"):
            if key in row:
                try:
                    row[key] = float(row[key]) / scale + top
                except (TypeError, ValueError):
                    pass
        box = row.get("box")
        if isinstance(box, list):
            mapped_box: list[list[float]] = []
            for point in box:
                if not isinstance(point, (list, tuple)) or len(point) < 2:
                    continue
                try:
                    mapped_box.append(
                        [float(point[0]) / scale + left, float(point[1]) / scale + top]
                    )
                except (TypeError, ValueError):
                    continue
            row["box"] = mapped_box
        row.setdefault(
            "center_x",
            (float(row.get("left") or 0) + float(row.get("right") or 0)) / 2,
        )
        row.setdefault(
            "center_y",
            (float(row.get("top") or 0) + float(row.get("bottom") or 0)) / 2,
        )
        row["ocr_source"] = "structural_chat_candidate_enhanced"
        mapped.append(row)
    return mapped


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

    del geometry
    snapshot = layout_snapshot_for_image(screenshot)
    try:
        bounds = win32_ocr_layout.required_region(snapshot, "input_bounds")
    except win32_ocr_layout.LayoutSnapshotError as exc:
        raise RuntimeError(f"{win32_ocr_layout.ERROR_LAYOUT_UNRESOLVED}:{exc.reason}") from exc
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


def active_send_target_roi_bounds(
    image_size: tuple[int, int],
    *,
    layout_snapshot: dict[str, Any] | None,
) -> list[int]:
    width, height = [int(value or 0) for value in image_size[:2]]
    if width <= 0 or height <= 0:
        return []
    try:
        header = win32_ocr_layout.required_region(layout_snapshot, "chat_header_bounds")
        input_bounds = win32_ocr_layout.required_region(layout_snapshot, "input_bounds")
    except win32_ocr_layout.LayoutSnapshotError:
        return []
    return [header[0], header[1], input_bounds[2], input_bounds[3]]


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
    roi_bounds = active_send_target_roi_bounds(
        getattr(screenshot, "size", (0, 0)),
        layout_snapshot=layout_snapshot_for_image(screenshot),
    )
    if not roi_bounds:
        raise RuntimeError(f"{win32_ocr_layout.ERROR_LAYOUT_UNRESOLVED}:active_send_target_roi")
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
    active_match = active_chat_matches(
        roi_items,
        getattr(screenshot, "size", (0, 0)),
        target=target,
        exact=exact,
        layout_snapshot=layout_snapshot_for_image(screenshot),
    )
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


def add_friend_expected_menu_target(*, name: str, label: str, plus_image_x: int, plus_image_y: int, y_offset: int, image_size: tuple[int, int]) -> dict[str, Any]:
    return win32_ocr_add_friend_windows.add_friend_expected_menu_target(name=name, label=label, plus_image_x=plus_image_x, plus_image_y=plus_image_y, y_offset=y_offset, image_size=image_size)


def add_friend_popup_menu_item_click_bounds(item: dict[str, Any], popup_bounds: list[int]) -> list[int]:
    return win32_ocr_add_friend_windows.add_friend_popup_menu_item_click_bounds(item, popup_bounds)


def add_friend_expected_menu_click_bounds(*, image_size: tuple[int, int], plus_image_x: int, plus_image_y: int, y_offset: int) -> list[int]:
    return win32_ocr_add_friend_windows.add_friend_expected_menu_click_bounds(image_size=image_size, plus_image_x=plus_image_x, plus_image_y=plus_image_y, y_offset=y_offset)


def add_friend_menu_candidate_targets(
    ocr_items: list[dict[str, Any]],
    image_size: tuple[int, int],
    *,
    plus_image_x: int | None = None,
    plus_image_y: int | None = None,
    include_expected: bool = True,
    layout_snapshot: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    return win32_ocr_add_friend_windows.add_friend_menu_candidate_targets(
        ocr_items,
        image_size,
        plus_image_x=plus_image_x,
        plus_image_y=plus_image_y,
        include_expected=include_expected,
        layout_snapshot=layout_snapshot,
    )


def plus_entry_popup_menu_detected(ocr_items: list[dict[str, Any]], targets: list[dict[str, Any]]) -> dict[str, Any]:
    return win32_ocr_add_friend_windows.plus_entry_popup_menu_detected(ocr_items, targets)


def add_friend_target_review_text(targets: list[dict[str, Any]]) -> str:
    return win32_ocr_add_friend_windows.add_friend_target_review_text(targets)


def add_friend_target_by_name(targets: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    return win32_ocr_add_friend_windows.add_friend_target_by_name(targets, name)


def add_friend_page_search_region(
    image_size: tuple[int, int],
    *,
    layout_snapshot: dict[str, Any] | None = None,
) -> list[int]:
    return win32_ocr_add_friend_windows.add_friend_page_search_region(
        image_size,
        layout_snapshot=layout_snapshot,
    )


def add_friend_search_result_region(
    image_size: tuple[int, int],
    *,
    layout_snapshot: dict[str, Any] | None = None,
) -> list[int]:
    return win32_ocr_add_friend_windows.add_friend_search_result_region(
        image_size,
        layout_snapshot=layout_snapshot,
    )


def add_friend_phone_not_found_detected(ocr_items: list[dict[str, Any]]) -> dict[str, Any]:
    return win32_ocr_add_friend_windows.add_friend_phone_not_found_detected(ocr_items)


def add_friend_search_result_add_contact_target(
    ocr_items: list[dict[str, Any]],
    image_size: tuple[int, int],
    *,
    layout_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    return win32_ocr_add_friend_windows.add_friend_search_result_add_contact_target(
        ocr_items,
        image_size,
        layout_snapshot=layout_snapshot,
    )


def click_add_contact_entry_from_search_result(hwnd: int, output_dir: Path, *, result_shot: Image.Image, result_path: str, result_items: list[dict[str, Any]], query: str, verify_message: str = '', remark_name: str = '', remark_code: str = '', action_journal_path: str = '') -> dict[str, Any]:
    win32_ocr_add_friend_windows.bind_sidecar_ops(sys.modules[__name__])
    return win32_ocr_add_friend_windows.click_add_contact_entry_from_search_result(hwnd, output_dir, result_shot=result_shot, result_path=result_path, result_items=result_items, query=query, verify_message=verify_message, remark_name=remark_name, remark_code=remark_code, action_journal_path=action_journal_path)


def add_friend_invite_form_targets(
    image_size: tuple[int, int],
    ocr_items: list[dict[str, Any]] | None = None,
    *,
    layout_snapshot: dict[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    return win32_ocr_add_friend_windows.add_friend_invite_form_targets(
        image_size,
        ocr_items,
        layout_snapshot=layout_snapshot,
    )


def capture_invite_form_field_review(
    hwnd: int,
    output_dir: Path,
    *,
    label: str,
    verify_message: str,
    remark_name: str,
    remark_code: str,
) -> dict[str, Any]:
    win32_ocr_add_friend_windows.bind_sidecar_ops(sys.modules[__name__])
    return win32_ocr_add_friend_windows.capture_invite_form_field_review(
        hwnd,
        output_dir,
        label=label,
        verify_message=verify_message,
        remark_name=remark_name,
        remark_code=remark_code,
    )


def paste_invite_form_text(hwnd: int, target: dict[str, Any], text: str, *, action_name: str) -> dict[str, Any]:
    win32_ocr_add_friend_windows.bind_sidecar_ops(sys.modules[__name__])
    return win32_ocr_add_friend_windows.paste_invite_form_text(hwnd, target, text, action_name=action_name)


def fill_add_friend_invite_form_and_confirm(hwnd: int, output_dir: Path, *, verify_message: str, remark_name: str, remark_code: str, action_journal_path: str = '') -> dict[str, Any]:
    win32_ocr_add_friend_windows.bind_sidecar_ops(sys.modules[__name__])
    return win32_ocr_add_friend_windows.fill_add_friend_invite_form_and_confirm(hwnd, output_dir, verify_message=verify_message, remark_name=remark_name, remark_code=remark_code, action_journal_path=action_journal_path)


def find_add_friend_page_search_targets(
    ocr_items: list[dict[str, Any]],
    image_size: tuple[int, int],
    screenshot: Image.Image | None = None,
    *,
    layout_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return win32_ocr_add_friend_windows.find_add_friend_page_search_targets(
        ocr_items,
        image_size,
        screenshot,
        layout_snapshot=layout_snapshot,
    )


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


def add_friend_calibration_payload(hwnd: int, probe: dict[str, Any], *, geometry: dict[str, Any], route: str, phone: str, wechat: str, verify_message: str, remark_name: str, remark_code: str, output_dir: Path) -> dict[str, Any]:
    win32_ocr_add_friend_windows.bind_sidecar_ops(sys.modules[__name__])
    return win32_ocr_add_friend_windows.add_friend_calibration_payload(hwnd, probe, geometry=geometry, route=route, phone=phone, wechat=wechat, verify_message=verify_message, remark_name=remark_name, remark_code=remark_code, output_dir=output_dir)


def add_friend_failure_payload(*, error_code: str, message: str, steps: list[str], query: str, phone: str, wechat: str, probe: dict[str, Any], evidence: dict[str, Any] | None = None, state: str = 'add_friend_failed') -> dict[str, Any]:
    return win32_ocr_add_friend_windows.add_friend_failure_payload(error_code=error_code, message=message, steps=steps, query=query, phone=phone, wechat=wechat, probe=probe, evidence=evidence, state=state)


def add_friend_surface_readiness(
    screenshot: Image.Image,
    ocr_items: list[dict[str, Any]],
    geometry: dict[str, Any],
    *,
    stage: str,
    require_main_surface: bool | None = None,
    layout_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    win32_ocr_add_friend_windows.bind_sidecar_ops(sys.modules[__name__])
    return win32_ocr_add_friend_windows.add_friend_surface_readiness(
        screenshot,
        ocr_items,
        geometry,
        stage=stage,
        require_main_surface=require_main_surface,
        layout_snapshot=layout_snapshot,
    )


def add_friend_main_entry_surface_evidence(
    ocr_items: list[dict[str, Any]],
    image_size: tuple[int, int],
    *,
    layout_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return win32_ocr_add_friend_windows.add_friend_main_entry_surface_evidence(
        ocr_items,
        image_size,
        layout_snapshot=layout_snapshot,
    )


def add_friend_human_pause(min_ms: int, max_ms: int | None = None, *, reason: str = "") -> float:
    """Randomized add_friend pacing.

    The add_friend flow runs inside WeChat's sensitive contact-add surface.
    Keep mouse, keyboard and OCR phases strictly separated by visible human
    pauses so the flow does not look like a burst of synthetic operations.
    """
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


def clear_add_friend_sidebar_search_box(hwnd: int, search_x: int, search_y: int, *, target_hint: str = '') -> dict[str, Any]:
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
    selected_journal_item_ids: list[str] = []
    for journal_item_id, item in items.items():
        if not isinstance(item, dict):
            continue
        item_anchors = {
            str(value).strip()
            for value in (item.get("physical_anchor_keys") or [])
            if str(value).strip()
        }
        if anchor_keys and item_anchors & anchor_keys:
            selected_journal_item_ids.append(str(journal_item_id))
    if not selected_journal_item_ids and len(items) == 1:
        selected_journal_item_ids.append(str(next(iter(items))))
    if items and not selected_journal_item_ids:
        raise ValueError("ACTION_JOURNAL_ITEM_NOT_FOUND")
    if any(
        phase_rank[requested_phase]
        < phase_rank.get(
            str(
                (items.get(journal_item_id) or {}).get("action_phase")
                or "not_attempted"
            ).strip(),
            0,
        )
        for journal_item_id in selected_journal_item_ids
    ):
        # All selected aliases describe one physical action. A stale writer
        # must not partially mutate the aliases that happen to lag behind.
        return
    updated_at = datetime.now(timezone.utc).isoformat()
    for journal_item_id in selected_journal_item_ids:
        item = dict(items.get(journal_item_id) or {})
        current_phase = str(
            item.get("action_phase") or "not_attempted"
        ).strip()
        item_phase = requested_phase
        item["action_phase"] = item_phase
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
        items[journal_item_id] = item
    payload["items"] = items
    payload["action_phase"] = max(
        (
            str(item.get("action_phase") or "not_attempted")
            for item in items.values()
            if isinstance(item, dict)
        ),
        key=lambda value: phase_rank.get(value, 0),
        default="not_attempted",
    )
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


def read_action_phase_journal(path: str) -> dict[str, Any]:
    raw_path = str(path or "").strip()
    if not raw_path:
        return {"ok": False, "reason": "action_journal_path_missing"}
    try:
        payload = json.loads(
            Path(raw_path).expanduser().read_text(encoding="utf-8")
        )
    except FileNotFoundError:
        return {"ok": False, "reason": "action_journal_not_initialized"}
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        return {
            "ok": False,
            "reason": "action_journal_invalid",
            "error": repr(exc),
        }
    if not isinstance(payload, dict):
        return {"ok": False, "reason": "action_journal_invalid"}
    phase_rank = {
        value: index for index, value in enumerate(C2_ACTION_PHASES)
    }
    phases = [str(payload.get("action_phase") or "not_attempted")]
    items = payload.get("items")
    if isinstance(items, dict):
        phases.extend(
            str(item.get("action_phase") or "not_attempted")
            for item in items.values()
            if isinstance(item, dict)
        )
    valid_phases = [phase for phase in phases if phase in phase_rank]
    if not valid_phases:
        return {"ok": False, "reason": "action_journal_phase_invalid"}
    action_phase = max(valid_phases, key=lambda phase: phase_rank[phase])
    return {
        "ok": True,
        "action_phase": action_phase,
        "payload": payload,
    }


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
        send_frame_reuse = payload.get("send_frame_reuse")
        if isinstance(send_frame_reuse, dict):
            send_frame_reuse["ocr_call_count"] = int(
                timing.get("send_payload_ocr_call_count") or 0
            )
            send_frame_reuse["ocr_total_duration_ms"] = round(
                float(
                    timing.get("send_payload_ocr_total_duration_seconds") or 0.0
                )
                * 1000
            )
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
        inferred_action_phase = (
            "confirmed"
            if send_confirmed
            else "trigger_attempted"
            if physical_send_triggered
            else "not_attempted"
        )
        if inferred_action_phase == "confirmed" and action_journal_path:
            write_action_phase_journal(
                action_journal_path,
                "confirmed",
            )
        journal_result = read_action_phase_journal(action_journal_path)
        phase_rank = {
            value: index for index, value in enumerate(C2_ACTION_PHASES)
        }
        journal_phase = str(
            journal_result.get("action_phase") or "not_attempted"
        )
        action_phase = inferred_action_phase
        if (
            journal_result.get("ok")
            and phase_rank.get(journal_phase, 0)
            > phase_rank.get(action_phase, 0)
        ):
            action_phase = journal_phase
        if phase_rank.get(action_phase, 0) >= phase_rank.get(
            "trigger_attempted",
            1,
        ):
            physical_send_triggered = True
            payload["physical_send_triggered"] = True
            if isinstance(send_result, dict):
                send_result["physical_send_triggered"] = True
        payload["action_phase"] = action_phase
        if action_journal_path:
            payload["action_journal"] = {
                key: value
                for key, value in journal_result.items()
                if key != "payload"
            }
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

    pre_send_guard_started = _sidecar_timing_start(timing, "pre_send_guard")
    focus_guard = recover_send_window_guard(hwnd, max_attempts=2)
    if not focus_guard.get("ok"):
        _sidecar_timing_finish(timing, "pre_send_guard", pre_send_guard_started)
        return finish({
            "ok": False,
            "online": True,
            "adapter": "win32_ocr",
            "state": "send_guard_blocked",
            "window_probe": probe,
            "target": target,
            "guard": {"window_guard": focus_guard},
            "error": str(focus_guard.get("reason") or "send focus guard blocked"),
        })
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
    validation = (
        dict(baseline_snapshot.get("validation") or {})
        if isinstance(baseline_snapshot.get("validation"), dict)
        else {}
    )
    if not validation and isinstance(validated_guard, dict):
        # Compatibility for unit-test and older adapter fixtures only. Runtime
        # snapshots always carry their own same-frame target validation.
        validation = dict(validated_guard)
        timing["send_baseline_validation_fixture_fallback"] = True
    _sidecar_timing_merge_validation(timing, "send_baseline_validation", validation)
    if not baseline_snapshot.get("ok"):
        _sidecar_timing_finish(timing, "pre_send_guard", pre_send_guard_started)
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
    geometry = (
        dict(validation.get("geometry") or {})
        if isinstance(validation.get("geometry"), dict)
        else get_window_geometry(hwnd)
    )
    geometry_check = validate_send_geometry(geometry)
    if (
        not validation.get("ok")
        or not active_send_guard_is_strong(validation)
        or not geometry_check.get("ok")
    ):
        _sidecar_timing_finish(timing, "pre_send_guard", pre_send_guard_started)
        return finish({
            "ok": False,
            "online": bool(validation.get("online", True)),
            "adapter": "win32_ocr",
            "state": "send_guard_blocked",
            "window_probe": probe,
            "target": target,
            "guard": {
                **validation,
                "window_guard": focus_guard,
                "geometry": geometry,
                "geometry_check": geometry_check,
            },
            "send_baseline": baseline_snapshot,
            "error": str(
                validation.get("error")
                or validation.get("reason")
                or geometry_check.get("error")
                or "send baseline guard blocked"
            ),
        })
    validation = {
        **validation,
        "window_guard": focus_guard,
        "single_frame_send_baseline": True,
    }
    _sidecar_timing_finish(timing, "pre_send_guard", pre_send_guard_started)
    points = {
        "ok": True,
        "source": "visual_observed_input",
        "geometry": geometry,
    }
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
    timing["input_region_precheck_seed_reused"] = True
    timing["input_region_precheck_seed_age_seconds"] = 0.0
    baseline_frame = (
        baseline_snapshot.get("frame_observation")
        if isinstance(baseline_snapshot.get("frame_observation"), dict)
        else {}
    )

    send_mode = DEFAULT_SEND_MODE
    settings = adapt_humanized_input_settings(humanized_input_settings(), text)
    settings["enabled"] = True
    settings["method"] = "sendinput_unicode"
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
    def pre_trigger_context_check(
        *,
        screenshot: Any | None = None,
        screenshot_path: str | None = None,
        ocr_items: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        try:
            if screenshot is None:
                snapshot = capture_send_fact_snapshot(
                    hwnd,
                    target=target,
                    text=final_send_text,
                    exact=exact,
                    artifact_dir=artifact_dir,
                    label="send_pre_trigger_context",
                )
            else:
                snapshot = build_send_fact_snapshot_from_frame(
                    hwnd,
                    target=target,
                    text=final_send_text,
                    exact=exact,
                    artifact_dir=artifact_dir,
                    label="send_pre_trigger_context_reused",
                    screenshot=screenshot,
                    screenshot_path=screenshot_path,
                    ocr_items=ocr_items,
                )
        except Exception as exc:
            return {
                "ok": False,
                "reason": "pre_trigger_context_snapshot_failed",
                "error_code": "C3_SEND_PRE_CLICK_CONTEXT_UNAVAILABLE",
                "error": repr(exc),
            }
        target_validation = (
            snapshot.get("validation")
            if isinstance(snapshot.get("validation"), dict)
            else {}
        )
        snapshot_frame = (
            snapshot.get("frame_observation")
            if isinstance(snapshot.get("frame_observation"), dict)
            else {}
        )
        if env_flag(
            "CHEJIN_C3_SEND_FRAME_LOCAL_REUSE_ENABLED", default=True
        ):
            baseline_frame_id = str(baseline_frame.get("frame_id") or "")
            snapshot_frame_id = str(snapshot_frame.get("frame_id") or "")
            baseline_digest = str(
                baseline_frame.get("screenshot_sha256") or ""
            )
            snapshot_digest = str(
                snapshot_frame.get("screenshot_sha256") or ""
            )
            if (
                baseline_frame_id
                and snapshot_frame_id
                and (
                    baseline_frame_id == snapshot_frame_id
                    or (
                        baseline_digest
                        and snapshot_digest
                        and baseline_digest == snapshot_digest
                    )
                )
            ):
                return {
                    "ok": False,
                    "reason": "send_s1_not_distinct_from_s0",
                    "error_code": "C3_SEND_FRAME_TIMEPOINT_INVALID",
                    "snapshot": snapshot,
                }
        if (
            snapshot.get("ok") is not True
            or target_validation.get("ok") is not True
            or not active_send_guard_is_strong(target_validation)
        ):
            return {
                "ok": False,
                "reason": "send_target_not_confirmed_before_enter",
                "error_code": "SEND_TARGET_NOT_CONFIRMED",
                "target_validation": target_validation,
                "snapshot": snapshot,
            }
        validation_result = validate_send_context_guard(
            expected_context_guard,
            snapshot.get("send_context_guard"),
        )
        return {
            **validation_result,
            "snapshot": snapshot,
            "frame_observation": snapshot_frame,
        }

    uia_result: dict[str, Any] = {
        "ok": False,
        "reason": "diagnostic_only_not_used_for_send",
        "physical_send_triggered": False,
    }
    visual_result: dict[str, Any] = {
        "ok": False,
        "reason": "visual_path_not_selected",
        "physical_send_triggered": False,
    }
    visual_send_started = _sidecar_timing_start(timing, "visual_observed_send")
    visual_result = send_with_visual_input(
        hwnd,
        final_send_text,
        geometry=geometry,
        settings=settings,
        artifact_dir=artifact_dir,
        before_input_region_seed=input_region_seed,
        before_send_trigger_check=pre_trigger_context_check,
        action_journal_path=action_journal_path,
    )
    _sidecar_timing_finish(timing, "visual_observed_send", visual_send_started)
    if isinstance(visual_result.get("timing"), dict):
        _sidecar_timing_merge_prefixed(
            timing,
            "visual",
            visual_result["timing"],
        )
    active_result = visual_result

    physical_send_triggered = bool(
        active_result.get("physical_send_triggered") is True
    )
    if not active_result.get("ok"):
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
                    "visual": visual_result,
                    "send_baseline": baseline_snapshot,
                },
                "send_result": {
                    "ok": False,
                    "confirmed": False,
                    "result": "unknown",
                    "physical_send_triggered": True,
                },
                "error": "The physical Enter send trigger ran, but the remaining send flow could not be confirmed.",
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
                "visual": visual_result,
                "send_baseline": baseline_snapshot,
            },
            "error_code": str(active_result.get("error_code") or "SEND_INPUT_NOT_READY"),
            "error": str(active_result.get("error") or active_result.get("reason") or "send input could not be confirmed"),
            "physical_send_triggered": False,
        })
    humanized_action_sleep(200, 420)
    post_send_guard_started = _sidecar_timing_start(timing, "post_send_guard")
    try:
        post_send_snapshot = capture_send_fact_snapshot(
            hwnd,
            target=target,
            text=final_send_text,
            exact=exact,
            artifact_dir=artifact_dir,
            label="send_post_guard_and_result_confirm_1",
            recover_expected_self_text=True,
        )
    except Exception as exc:
        post_send_snapshot = {
            "ok": False,
            "validation": {
                "ok": False,
                "reason": "post_send_snapshot_unavailable",
                "error": repr(exc),
            },
        }
    post_validation = (
        dict(post_send_snapshot.get("validation") or {})
        if isinstance(post_send_snapshot.get("validation"), dict)
        else {}
    )
    post_send_frame = (
        post_send_snapshot.get("frame_observation")
        if isinstance(post_send_snapshot.get("frame_observation"), dict)
        else {}
    )
    pre_trigger_snapshot = (
        ((visual_result.get("context_check") or {}).get("snapshot"))
        if isinstance(visual_result.get("context_check"), dict)
        and isinstance(
            (visual_result.get("context_check") or {}).get("snapshot"), dict
        )
        else {}
    )
    pre_trigger_frame = (
        pre_trigger_snapshot.get("frame_observation")
        if isinstance(pre_trigger_snapshot.get("frame_observation"), dict)
        else {}
    )
    if env_flag("CHEJIN_C3_SEND_FRAME_LOCAL_REUSE_ENABLED", default=True):
        frame_ids = [
            str(item.get("frame_id") or "")
            for item in (baseline_frame, pre_trigger_frame, post_send_frame)
            if isinstance(item, dict)
        ]
        nonempty_frame_ids = [value for value in frame_ids if value]
        frame_digests = [
            str(item.get("screenshot_sha256") or "")
            for item in (baseline_frame, pre_trigger_frame, post_send_frame)
            if isinstance(item, dict)
        ]
        nonempty_frame_digests = [value for value in frame_digests if value]
        if len(nonempty_frame_ids) == 3 and (
            len(set(nonempty_frame_ids)) != 3
            or (
                len(nonempty_frame_digests) == 3
                and len(set(nonempty_frame_digests)) != 3
            )
        ):
            return finish({
                "ok": False,
                "online": True,
                "adapter": "win32_ocr",
                "state": "send_result_unknown",
                "error_code": "SEND_RESULT_UNKNOWN",
                "physical_send_triggered": True,
                "target": target,
                "send_result": {
                    "ok": False,
                    "confirmed": False,
                    "result": "unknown",
                    "physical_send_triggered": True,
                },
                "send_frame_reuse": {
                    "fast_path_attempted": True,
                    "fast_path_used": False,
                    "fallback_reason": "cross_timepoint_frame_reuse_detected",
                    "frame_digest_equal": None,
                    "ocr_call_count": None,
                    "ocr_total_duration_ms": None,
                    "s0": baseline_frame,
                    "s1": pre_trigger_frame,
                    "s2": post_send_frame,
                },
                "error": "S0, S1 and S2 must be independent physical captures.",
            })
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
                "visual": visual_result,
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
        initial_snapshot=post_send_snapshot,
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
                "visual": visual_result,
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
            "method": active_result.get("method") or "win32.observed_input+rpa_text_entry+keyboard_enter",
            "mode": send_mode,
            "humanized_method": settings.get("method"),
            "validation_source": "single_frame_send_baseline",
            "pre_send_guard": validation,
            "geometry": geometry,
            "input_control": uia_result.get("edit"),
            "send_control": uia_result.get("send_button"),
            "rate": rate,
            "uia": uia_result,
            "visual": visual_result,
            "post_send_guard": post_validation,
            "send_baseline": baseline_snapshot,
            "sent_confirmation": sent_confirmation,
            "confirmed": True,
            "result": "sent",
            "physical_send_triggered": True,
        },
        "send_frame_reuse": {
            "fast_path_attempted": env_flag(
                "CHEJIN_C3_SEND_FRAME_LOCAL_REUSE_ENABLED", default=True
            ),
            "fast_path_used": bool(
                env_flag(
                    "CHEJIN_C3_SEND_FRAME_LOCAL_REUSE_ENABLED", default=True
                )
                and baseline_frame
                and pre_trigger_frame
                and post_send_frame
            ),
            "fallback_reason": (
                ""
                if baseline_frame and pre_trigger_frame and post_send_frame
                else "frame_observation_incomplete_fallback_original_flow"
            ),
            "frame_digest_equal": False,
            "ocr_call_count": timing.get("send_payload_ocr_call_count"),
            "ocr_total_duration_ms": (
                round(
                    float(
                        timing.get(
                            "send_payload_ocr_total_duration_seconds", 0.0
                        )
                        or 0.0
                    )
                    * 1000
                )
                if timing.get("send_payload_ocr_total_duration_seconds")
                is not None
                else None
            ),
            "s0": baseline_frame,
            "s1": pre_trigger_frame,
            "s2": post_send_frame,
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
    input_bounds: list[int],
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
        if not rect_overlaps_region(rect, tuple(input_bounds)):
            continue
        compact = re.sub(r"\s+", "", text)
        if normalized_token in compact or compact in normalized_token:
            return True
    return False


def input_area_contains_any_token(
    ocr_items: list[dict[str, Any]],
    *,
    input_bounds: list[int],
    tokens: list[str],
) -> bool:
    for token in tokens:
        if input_area_contains_token(ocr_items, input_bounds=input_bounds, token=token):
            return True
    return False


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
    del geometry
    snapshot = layout_snapshot_for_image(screenshot)
    try:
        bounds = tuple(win32_ocr_layout.required_region(snapshot, "input_text_bounds"))
    except win32_ocr_layout.LayoutSnapshotError as exc:
        return {
            "has_visible_text": False,
            "reason": "input_layout_unresolved",
            "error_code": win32_ocr_layout.ERROR_LAYOUT_UNRESOLVED,
            "error": exc.reason,
            "bounds": [],
        }
    ocr_evidence: list[dict[str, Any]] = []
    ignored_ocr_evidence: list[dict[str, Any]] = []
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
        overlap_width = max(
            0,
            min(rect["right"], bounds[2]) - max(rect["left"], bounds[0]),
        )
        overlap_height = max(
            0,
            min(rect["bottom"], bounds[3]) - max(rect["top"], bounds[1]),
        )
        rect_area = max(
            1,
            (rect["right"] - rect["left"])
            * (rect["bottom"] - rect["top"]),
        )
        overlap_ratio = float(overlap_width * overlap_height) / float(rect_area)
        center_x = (rect["left"] + rect["right"]) / 2.0
        center_y = (rect["top"] + rect["bottom"]) / 2.0
        snapshot = {
            "text": text,
            **rect,
            "overlap_ratio": round(overlap_ratio, 4),
        }
        if (
            bounds[0] <= center_x <= bounds[2]
            and bounds[1] <= center_y <= bounds[3]
            and overlap_ratio >= 0.8
            and rect["right"] - rect["left"] >= 3
            and rect["bottom"] - rect["top"] >= 6
        ):
            ocr_evidence.append(snapshot)
        elif overlap_width > 0 and overlap_height > 0:
            ignored_ocr_evidence.append(snapshot)
    ocr_hits = len(ocr_evidence)
    try:
        gray = screenshot.convert("L")
        crop = gray.crop(bounds)
        histogram = crop.histogram()
        total = max(1, int(sum(histogram)))
        dark_count = int(sum(histogram[:180]))
        dark_ratio = float(dark_count) / float(total)
        bright_ratio = float(sum(histogram[200:])) / float(total)
        mean = float(sum(index * count for index, count in enumerate(histogram))) / float(total)
        dark_mask = crop.point(lambda value: 255 if value < 180 else 0)
        dark_bbox = dark_mask.getbbox()
    except Exception as exc:
        return {
            "has_visible_text": bool(ocr_hits),
            "reason": "input_region_probe_failed",
            "error": repr(exc),
            "bounds": list(bounds),
            "ocr_hits": ocr_hits,
            "ocr_evidence": ocr_evidence[:12],
            "ignored_ocr_evidence": ignored_ocr_evidence[:12],
        }
    if dark_bbox:
        dark_width = max(0, int(dark_bbox[2]) - int(dark_bbox[0]))
        dark_height = max(0, int(dark_bbox[3]) - int(dark_bbox[1]))
        absolute_dark_bbox = [
            int(dark_bbox[0]) + bounds[0],
            int(dark_bbox[1]) + bounds[1],
            int(dark_bbox[2]) + bounds[0],
            int(dark_bbox[3]) + bounds[1],
        ]
    else:
        dark_width = 0
        dark_height = 0
        absolute_dark_bbox = []
    caret_like_dark_pixels = bool(
        dark_count > 0
        and dark_width <= 3
        and 8 <= dark_height <= 64
        and dark_count <= max(1, dark_width * dark_height)
    )
    # In dark-mode WeChat the whole input region can be dark even when blank.
    # Treat a uniformly dark crop without OCR or bright text strokes as blank;
    # otherwise the send guard will repeatedly refuse to type into an empty box.
    dark_theme_blank_like = bool(
        dark_ratio >= 0.90
        and mean <= 90.0
        and bright_ratio <= 0.002
    )
    text_shape_visible = bool(
        dark_count >= 12
        and dark_width >= 4
        and dark_height >= 5
        and not caret_like_dark_pixels
        and not dark_theme_blank_like
    )
    # Pixel density remains diagnostic only. A draft needs either a text-shaped
    # component or OCR whose body is contained in the editable text surface.
    # A focused two-pixel caret is therefore blank, while a short one-character
    # draft still blocks automatic typing.
    ocr_visible = bool(
        ocr_hits > 0
        and not dark_theme_blank_like
        and not caret_like_dark_pixels
        and (
            text_shape_visible
            or dark_ratio >= INPUT_TEXT_DARK_RATIO_MIN / 3.0
        )
    )
    pixel_visible = text_shape_visible
    has_visible_text = bool(pixel_visible or ocr_visible)
    return {
        "has_visible_text": has_visible_text,
        "reason": "ocr_or_text_shape" if has_visible_text else "input_region_blank",
        "bounds": list(bounds),
        "ocr_hits": ocr_hits,
        "ocr_evidence": ocr_evidence[:12],
        "ignored_ocr_evidence": ignored_ocr_evidence[:12],
        "dark_count": dark_count,
        "dark_ratio": round(dark_ratio, 6),
        "dark_bbox": absolute_dark_bbox,
        "dark_width": dark_width,
        "dark_height": dark_height,
        "bright_ratio": round(bright_ratio, 6),
        "mean": round(mean, 3),
        "threshold": INPUT_TEXT_DARK_RATIO_MIN,
        "dark_theme_blank_like": dark_theme_blank_like,
        "caret_like_dark_pixels": caret_like_dark_pixels,
        "text_shape_visible": text_shape_visible,
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


def send_button_ready_evidence(
    screenshot: Any,
    *,
    geometry: dict[str, Any],
) -> dict[str, Any]:
    """Confirm that WeChat's bottom-right send button is visibly active."""

    if screenshot is None:
        return {"ok": False, "reason": "send_ready_screenshot_missing"}
    try:
        image = screenshot.convert("RGB")
        image_width = int(getattr(image, "width", 0) or 0)
        image_height = int(getattr(image, "height", 0) or 0)
    except Exception as exc:
        return {
            "ok": False,
            "reason": "send_ready_screenshot_invalid",
            "error": repr(exc),
        }
    width = min(image_width, int(geometry.get("width") or image_width))
    height = min(image_height, int(geometry.get("height") or image_height))
    if width <= 0 or height <= 0:
        return {"ok": False, "reason": "send_ready_geometry_invalid"}

    snapshot = layout_snapshot_for_image(screenshot)
    try:
        input_bounds = win32_ocr_layout.required_region(snapshot, "input_bounds")
    except win32_ocr_layout.LayoutSnapshotError:
        return {"ok": False, "reason": "send_ready_layout_unresolved"}
    input_width = max(1, input_bounds[2] - input_bounds[0])
    input_height = max(1, input_bounds[3] - input_bounds[1])
    region_width = max(72, int(input_width * 0.18))
    left = max(input_bounds[0], input_bounds[2] - region_width)
    top = input_bounds[1] + int(input_height * 0.45)
    right = input_bounds[2]
    bottom = input_bounds[3]
    green_points: list[tuple[int, int]] = []
    try:
        for y in range(top, bottom):
            for x in range(left, right):
                red, green, blue = image.getpixel((x, y))
                if (
                    green >= 125
                    and green - red >= 35
                    and green - blue >= 20
                    and red <= 175
                ):
                    green_points.append((x, y))
    except Exception as exc:
        return {
            "ok": False,
            "reason": "send_ready_pixel_probe_failed",
            "bounds": [left, top, right, bottom],
            "error": repr(exc),
        }

    if green_points:
        green_left = min(point[0] for point in green_points)
        green_top = min(point[1] for point in green_points)
        green_right = max(point[0] for point in green_points)
        green_bottom = max(point[1] for point in green_points)
    else:
        green_left = green_top = green_right = green_bottom = 0
    green_width = max(0, green_right - green_left + 1)
    green_height = max(0, green_bottom - green_top + 1)
    green_count = len(green_points)
    ready = bool(
        green_count >= 120
        and green_width >= 24
        and green_height >= 12
    )
    return {
        "ok": ready,
        "reason": (
            "active_green_send_button_observed"
            if ready
            else "active_green_send_button_not_observed"
        ),
        "bounds": [left, top, right, bottom],
        "green_count": green_count,
        "green_bounds": [green_left, green_top, green_right, green_bottom],
        "green_width": green_width,
        "green_height": green_height,
        "used_as_click_target": False,
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
        delay_index = min(
            attempt - 1,
            len(SEND_WINDOW_FOCUS_RECOVERY_DELAYS_SECONDS) - 1,
        )
        time.sleep(SEND_WINDOW_FOCUS_RECOVERY_DELAYS_SECONDS[delay_index])
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
    post_input_screenshot: Any | None = None
    post_input_screenshot_path: str | None = None
    post_input_ocr_items: list[dict[str, Any]] | None = None
    post_input_ocr_source = ""

    def finish(payload: dict[str, Any]) -> dict[str, Any]:
        _sidecar_timing_finish(timing, "paste_text_with_confirmation", paste_started)
        _sidecar_timing_merge_ocr_trace(timing, "paste_text_with_confirmation", _ocr_trace_finish(ocr_trace_token))
        if last_input_click_evidence:
            payload.setdefault("input_click_evidence", last_input_click_evidence)
        if last_input_click:
            payload.setdefault("input_click", last_input_click)
        if payload.get("ok") and post_input_screenshot is not None:
            payload["_post_input_screenshot"] = post_input_screenshot
            payload["_post_input_screenshot_path"] = (
                post_input_screenshot_path
            )
            if post_input_ocr_source == "full" and post_input_ocr_items is not None:
                payload["_post_input_ocr_items"] = post_input_ocr_items
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
        human_client_click(
            hwnd,
            click_x,
            click_y,
            bounds=list(last_input_click.get("bounds") or last_input_click_evidence.get("click_bounds") or []),
            expected_snapshot_id=str(
                (layout_snapshot_metadata(hwnd).get("snapshot") or {}).get("layout_snapshot_id") or ""
            ),
        )
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
            wait_started = _sidecar_timing_start(
                timing,
                "wait_before_final_send_frame",
            )
            if settings.get("enabled"):
                humanized_sleep_ms(
                    int(
                        settings.get("send_post_input_delay_min_ms")
                        or DEFAULT_HUMANIZED_SEND_POST_INPUT_DELAY_MIN_MS
                    ),
                    int(
                        settings.get("send_post_input_delay_max_ms")
                        or DEFAULT_HUMANIZED_SEND_POST_INPUT_DELAY_MAX_MS
                    ),
                )
                humanized_sleep_ms(
                    int(
                        settings.get("send_trigger_delay_min_ms")
                        or DEFAULT_HUMANIZED_SEND_TRIGGER_DELAY_MIN_MS
                    ),
                    int(
                        settings.get("send_trigger_delay_max_ms")
                        or DEFAULT_HUMANIZED_SEND_TRIGGER_DELAY_MAX_MS
                    ),
                )
            _sidecar_timing_finish(
                timing,
                "wait_before_final_send_frame",
                wait_started,
            )
        except Exception as exc:
            return finish({
                "ok": False,
                "reason": "post_input_pacing_failed",
                "error_code": "SEND_INPUT_NOT_READY",
                "error": repr(exc),
                "probe_token": probe_token,
                "probe_tokens": probe_tokens,
                "attempts": attempt,
                "copyback_enabled": allow_copyback,
                "input_mode": input_method,
                "input_result": last_input_result,
            })
        try:
            after_capture_started = _sidecar_timing_start(timing, "after_capture")
            screenshot, _path = capture_wechat(hwnd, artifact_dir=artifact_dir, label=f"send_input_probe_{attempt}")
            post_input_screenshot = screenshot
            post_input_screenshot_path = _path
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
                "send_button_ready": send_button_ready_evidence(
                    screenshot,
                    geometry=geometry,
                ),
            })
        after_ocr_started = _sidecar_timing_start(timing, "after_ocr")
        ocr_items, after_ocr_source = run_ocr_for_input_confirmation(
            screenshot,
            geometry=geometry,
            timing=timing,
            prefix="after_ocr",
        )
        post_input_ocr_items = ocr_items
        post_input_ocr_source = after_ocr_source
        _sidecar_timing_finish(timing, "after_ocr", after_ocr_started)
        current_input_bounds = list(
            (layout_snapshot_for_image(screenshot) or {}).get("input_bounds") or []
        )
        if len(current_input_bounds) != 4:
            return {
                "ok": False,
                "reason": "WECHAT_UI_LAYOUT_UNRESOLVED",
                "error_code": win32_ocr_layout.ERROR_LAYOUT_UNRESOLVED,
            }
        if input_area_contains_any_token(ocr_items, input_bounds=current_input_bounds, tokens=probe_tokens):
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
                "send_button_ready": send_button_ready_evidence(
                    screenshot,
                    geometry=geometry,
                ),
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
                "send_button_ready": send_button_ready_evidence(
                    screenshot,
                    geometry=geometry,
                ),
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
            full_input_bounds = list(
                (layout_snapshot_for_image(screenshot) or {}).get("input_bounds") or []
            )
            if len(full_input_bounds) != 4:
                return {
                    "ok": False,
                    "reason": "WECHAT_UI_LAYOUT_UNRESOLVED",
                    "error_code": win32_ocr_layout.ERROR_LAYOUT_UNRESOLVED,
                }
            if input_area_contains_any_token(full_ocr_items, input_bounds=full_input_bounds, tokens=probe_tokens):
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
                    "send_button_ready": send_button_ready_evidence(
                        screenshot,
                        geometry=geometry,
                    ),
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
                    "send_button_ready": send_button_ready_evidence(
                        screenshot,
                        geometry=geometry,
                    ),
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
                    "send_button_ready": send_button_ready_evidence(
                        screenshot,
                        geometry=geometry,
                    ),
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
    settings: dict[str, Any] | None = None,
    focus_guard_func: Any | None = None,
    before_physical_trigger: Any | None = None,
) -> dict[str, Any]:
    active_settings = settings or {}
    try:
        guard = (
            focus_guard_func()
            if focus_guard_func is not None
            else recover_send_window_guard(hwnd, max_attempts=1)
        )
    except Exception as exc:
        return {
            "ok": False,
            "reason": "send_input_focus_proof_failed",
            "error_code": "SEND_INPUT_FOCUS_NOT_CONFIRMED",
            "error": repr(exc),
            "physical_send_triggered": False,
            "action_phase": "not_attempted",
            "send_trigger_mode": trigger_mode,
        }
    if not guard.get("ok"):
        return {
            "ok": False,
            "reason": "send_focus_guard_failed_before_trigger",
            "error": "WeChat lost foreground focus before send trigger; abort without retrying.",
            "window_guard": guard,
            "send_trigger_mode": trigger_mode,
        }
    mode = normalize_send_trigger_mode(trigger_mode)
    if mode == DEFAULT_SEND_TRIGGER_MODE:
        try:
            if callable(before_physical_trigger):
                before_physical_trigger()
        except Exception as exc:
            return {
                "ok": False,
                "reason": "send_action_journal_write_failed",
                "error_code": "SEND_ACTION_JOURNAL_WRITE_FAILED",
                "error": repr(exc),
                "physical_send_triggered": False,
                "action_phase": "not_attempted",
                "send_trigger_mode": mode,
                "window_guard": guard,
            }
        try:
            key_press(win32con.VK_RETURN)
        except Exception as exc:
            return {
                "ok": False,
                "reason": "send_enter_trigger_failed_after_journal",
                "error_code": "SEND_RESULT_UNKNOWN",
                "error": repr(exc),
                "physical_send_triggered": True,
                "action_phase": "trigger_attempted",
                "send_trigger_mode": mode,
                "window_guard": guard,
            }
        if active_settings.get("enabled"):
            humanized_sleep_ms(
                int(active_settings.get("send_after_trigger_delay_min_ms") or DEFAULT_HUMANIZED_SEND_AFTER_TRIGGER_DELAY_MIN_MS),
                int(active_settings.get("send_after_trigger_delay_max_ms") or DEFAULT_HUMANIZED_SEND_AFTER_TRIGGER_DELAY_MAX_MS),
            )
        return {
            "ok": True,
            "method": "keyboard_enter",
            "send_trigger_mode": mode,
            "window_guard": guard,
            "physical_send_triggered": True,
            "action_phase": "trigger_attempted",
        }
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


def confirm_exact_program_draft_focus(
    hwnd: int,
    *,
    input_point: tuple[int, int],
    expected_text: str,
) -> dict[str, Any]:
    """Prove the verified input control has focus and contains our exact draft."""

    previous_clipboard: str | None = None
    copied_text = ""
    selection_may_be_active = False
    selection_released = False

    def release_selection() -> bool:
        nonlocal selection_may_be_active, selection_released
        if not selection_may_be_active:
            return selection_released
        try:
            key_press(getattr(win32con, "VK_RIGHT", 0x27))
            humanized_action_sleep(20, 45)
            selection_released = True
            selection_may_be_active = False
        except Exception:
            return False
        return True

    try:
        previous_clipboard = clipboard_read()
    except Exception:
        previous_clipboard = None
    try:
        human_client_click(
            hwnd,
            int(input_point[0]),
            int(input_point[1]),
            bounds=list((current_layout_snapshot(hwnd) or {}).get("input_bounds") or []),
            expected_snapshot_id=str(
                (layout_snapshot_metadata(hwnd).get("snapshot") or {}).get("layout_snapshot_id") or ""
            ),
        )
        humanized_action_sleep(40, 80)
        window_guard = recover_send_window_guard(hwnd, max_attempts=1)
        if not window_guard.get("ok"):
            return {
                "ok": False,
                "reason": "input_focus_window_guard_failed",
                "window_guard": window_guard,
            }
        selection_may_be_active = True
        hotkey(win32con.VK_CONTROL, ord("A"))
        humanized_action_sleep(35, 70)
        hotkey(win32con.VK_CONTROL, ord("C"))
        humanized_action_sleep(50, 100)
        copied_text = clipboard_read()
    except Exception as exc:
        release_selection()
        return {
            "ok": False,
            "reason": "input_focus_copyback_failed",
            "error": repr(exc),
            "selection_released": selection_released,
        }
    finally:
        if previous_clipboard is not None:
            try:
                clipboard_copy(previous_clipboard)
            except Exception:
                pass
    exact_match = copied_text == expected_text
    if not exact_match:
        release_selection()
    return {
        "ok": exact_match,
        "reason": (
            "verified_input_focused_with_exact_program_draft"
            if exact_match
            else "focused_input_draft_mismatch"
        ),
        "expected_length": len(expected_text),
        "observed_length": len(copied_text),
        "selection_released": selection_released,
    }


def clear_confirmed_program_draft(
    hwnd: int,
    *,
    value_pattern: Any | None,
    input_point: tuple[int, int],
    expected_text: str,
    paste_result: dict[str, Any],
    geometry: dict[str, Any] | None = None,
    artifact_dir: str | None = None,
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
    exact_focus = (
        {
            "ok": current_value == expected_text,
            "reason": (
                "uia_value_pattern_exact_program_draft"
                if current_value == expected_text
                else "uia_value_pattern_draft_mismatch"
            ),
            "expected_length": len(expected_text),
            "observed_length": len(current_value),
        }
        if value_readable
        else confirm_exact_program_draft_focus(
            hwnd,
            input_point=input_point,
            expected_text=expected_text,
        )
    )
    if not exact_focus.get("ok") and not (
        value_readable and current_value == expected_text
    ):
        return {
            "ok": False,
            "reason": "program_draft_not_proven",
            "cleared": False,
            "observed_length": len(current_value) if value_readable else None,
            "focus_check": exact_focus,
        }
    try:
        if value_readable:
            # UIA proved the value but not the current focus.  Bind the one
            # necessary click to the exact current frame before selecting it.
            snapshot = current_layout_snapshot(hwnd) or {}
            human_client_click(
                hwnd,
                int(input_point[0]),
                int(input_point[1]),
                bounds=list(snapshot.get("input_bounds") or []),
                expected_snapshot_id=str(snapshot.get("layout_snapshot_id") or ""),
            )
            hotkey(win32con.VK_CONTROL, ord("A"))
            humanized_action_sleep(40, 80)
        else:
            # The clipboard fallback above already clicked the input surface,
            # selected all text and proved the exact program draft.  A second
            # click would both discard that proof and reuse an invalidated
            # layout snapshot, so clear the proven selection directly.
            humanized_action_sleep(20, 45)
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
    visual_clear: dict[str, Any] = {}
    if after_readable:
        cleared = not after_clear
    elif geometry:
        try:
            screenshot, _path = capture_wechat(
                hwnd,
                artifact_dir=artifact_dir,
                label="send_program_draft_cleanup",
            )
            ocr_items = run_ocr_for_input_confirmation(
                screenshot,
                geometry=geometry,
                timing={},
                prefix="draft_cleanup",
            )[0]
            after_state = input_text_region_state(
                screenshot,
                ocr_items,
                geometry=geometry,
            )
            if input_region_soft_blank_noise(after_state):
                after_state = normalize_soft_blank_input_state(
                    after_state,
                    reason="draft_cleanup_soft_blank_noise",
                )
            cleared = not bool(after_state.get("has_visible_text"))
            visual_clear = {"input_region": after_state}
        except Exception as exc:
            cleared = False
            visual_clear = {
                "reason": "draft_cleanup_visual_confirmation_failed",
                "error": repr(exc),
            }
    else:
        cleared = False
    return {
        "ok": cleared,
        "reason": (
            "confirmed_program_draft_cleared"
            if cleared
            else "program_draft_clear_not_confirmed"
        ),
        "cleared": cleared,
        "focus_check": exact_focus,
        **visual_clear,
    }


def locate_visual_send_input(
    *,
    before_input_region_seed: dict[str, Any] | None = None,
) -> dict[str, Any]:
    seed_region = (
        before_input_region_seed.get("input_region")
        if isinstance(before_input_region_seed, dict)
        else None
    )
    if not isinstance(seed_region, dict) or not seed_region:
        return {
            "ok": False,
            "reason": "visual_input_region_evidence_missing",
            "physical_send_triggered": False,
        }
    if seed_region.get("has_visible_text"):
        return {
            "ok": False,
            "reason": "unknown_input_draft_present",
            "error_code": "WECHAT_INPUT_DRAFT_PRESENT",
            "physical_send_triggered": False,
        }
    evidence = input_surface_click_evidence(seed_region)
    click = choose_verified_input_click_point(evidence)
    if not evidence.get("ok") or not click.get("ok"):
        return {
            "ok": False,
            "reason": "input_click_evidence_missing_before_type",
            "input_click_evidence": evidence,
            "physical_send_triggered": False,
        }
    return {
        "ok": True,
        "path": "visual_input",
        "input_point": tuple(int(value) for value in click["point"]),
        "value_pattern": None,
        "input_click_evidence": evidence,
        "input_click": click,
        "physical_send_triggered": False,
    }


def _draft_input_may_have_started(paste_result: dict[str, Any]) -> bool:
    input_result = (
        paste_result.get("input_result")
        if isinstance(paste_result.get("input_result"), dict)
        else {}
    )
    try:
        typed_chars = int(input_result.get("typed_chars") or 0)
    except (TypeError, ValueError):
        typed_chars = 0
    return bool(
        paste_result.get("ok")
        or input_result.get("ok")
        or input_result.get("input_progress") == "unknown"
        or typed_chars > 0
        or str(input_result.get("method") or "").startswith("clipboard")
    )


def execute_send_transaction(
    hwnd: int,
    text: str,
    *,
    locator: dict[str, Any],
    geometry: dict[str, Any],
    settings: dict[str, Any] | None = None,
    artifact_dir: str | None = None,
    before_input_region_seed: dict[str, Any] | None = None,
    before_send_trigger_check: Any | None = None,
    action_journal_path: str = "",
) -> dict[str, Any]:
    """The only formal text-input and Enter-send transaction."""

    if not locator.get("ok"):
        return dict(locator)
    raw_input_point = locator.get("input_point") or ()
    if len(raw_input_point) != 2:
        return {
            "ok": False,
            "reason": "verified_input_point_missing",
            "physical_send_triggered": False,
        }
    input_point = tuple(int(value) for value in raw_input_point)
    value_pattern = locator.get("value_pattern")
    settings = settings or adapt_humanized_input_settings(
        humanized_input_settings(),
        text,
    )
    try:
        paste_result = paste_text_with_confirmation(
            hwnd,
            text,
            points={"input_point": list(input_point), "send_point": None},
            geometry=geometry,
            artifact_dir=artifact_dir,
            settings=settings,
            before_input_region_seed=before_input_region_seed,
            verified_input_point=input_point,
        )
    except Exception as exc:
        paste_result = {
            "ok": False,
            "reason": "input_transaction_raised",
            "error": repr(exc),
            "input_result": {
                "ok": False,
                "method": "unknown_after_input_call",
                "input_progress": "unknown",
                "typed_chars": None,
            },
        }
    post_input_screenshot = paste_result.pop(
        "_post_input_screenshot",
        None,
    )
    post_input_screenshot_path = paste_result.pop(
        "_post_input_screenshot_path",
        None,
    )
    post_input_ocr_items = paste_result.pop(
        "_post_input_ocr_items",
        None,
    )
    def public_locator() -> dict[str, Any]:
        return {
            key: value
            for key, value in locator.items()
            if key != "value_pattern"
        }

    def fail_before_trigger(
        *,
        reason: str,
        error_code: str,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        draft_clear = {
            "ok": True,
            "cleared": False,
            "reason": "input_not_attempted",
        }
        if _draft_input_may_have_started(paste_result):
            draft_clear = clear_confirmed_program_draft(
                hwnd,
                value_pattern=value_pattern,
                input_point=input_point,
                expected_text=text,
                paste_result=paste_result,
                geometry=geometry,
                artifact_dir=artifact_dir,
            )
        if not draft_clear.get("ok"):
            return {
                "ok": False,
                "reason": "program_draft_cleanup_unconfirmed",
                "error_code": "SEND_DRAFT_CLEANUP_UNCONFIRMED",
                "error": (
                    "The AI draft may remain in the verified input control; "
                    "automatic sending is blocked."
                ),
                "draft_clear": draft_clear,
                "paste": paste_result,
                "locator": public_locator(),
                "physical_send_triggered": False,
                **(extra or {}),
            }
        return {
            "ok": False,
            "reason": reason,
            "error_code": error_code,
            "draft_clear": draft_clear,
            "paste": paste_result,
            "locator": public_locator(),
            "physical_send_triggered": False,
            **(extra or {}),
        }

    if not paste_result.get("ok"):
        return fail_before_trigger(
            reason=str(
                paste_result.get("reason")
                or "input_confirmation_failed"
            ),
            error_code=str(
                paste_result.get("error_code")
                or "SEND_INPUT_NOT_READY"
            ),
        )
    send_ready = (
        paste_result.get("send_button_ready")
        if isinstance(paste_result.get("send_button_ready"), dict)
        else {}
    )
    if callable(before_send_trigger_check):
        try:
            context_check_kwargs = {
                "screenshot": post_input_screenshot,
                "screenshot_path": post_input_screenshot_path,
            }
            if isinstance(post_input_ocr_items, list):
                context_check_kwargs["ocr_items"] = post_input_ocr_items
            context_check = before_send_trigger_check(**context_check_kwargs)
        except TypeError:
            try:
                context_check = before_send_trigger_check(
                    screenshot=post_input_screenshot,
                    screenshot_path=post_input_screenshot_path,
                )
            except TypeError:
                try:
                    context_check = before_send_trigger_check()
                except Exception as exc:
                    context_check = {
                        "ok": False,
                        "reason": "pre_trigger_context_check_failed",
                        "error_code": "C3_SEND_PRE_CLICK_CONTEXT_UNAVAILABLE",
                        "error": repr(exc),
                    }
            except Exception as exc:
                context_check = {
                    "ok": False,
                    "reason": "pre_trigger_context_check_failed",
                    "error_code": "C3_SEND_PRE_CLICK_CONTEXT_UNAVAILABLE",
                    "error": repr(exc),
                }
        except Exception as exc:
            context_check = {
                "ok": False,
                "reason": "pre_trigger_context_check_failed",
                "error_code": "C3_SEND_PRE_CLICK_CONTEXT_UNAVAILABLE",
                "error": repr(exc),
            }
    else:
        context_check = {
            "ok": False,
            "reason": "pre_trigger_context_check_missing",
            "error_code": "C3_SEND_CONTEXT_GUARD_REQUIRED",
        }
    if not context_check.get("ok"):
        return fail_before_trigger(
            reason="send_context_changed_before_enter",
            error_code=str(
                context_check.get("error_code")
                or "C3_CONTEXT_CHANGED_BEFORE_SEND"
            ),
            extra={"context_check": context_check},
        )
    trigger_result = safe_send_trigger(
        hwnd,
        trigger_mode=DEFAULT_SEND_TRIGGER_MODE,
        settings=settings,
        focus_guard_func=lambda: confirm_exact_program_draft_focus(
            hwnd,
            input_point=input_point,
            expected_text=text,
        ),
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
        if trigger_result.get("physical_send_triggered") is True:
            return {
                **trigger_result,
                "send_button_ready": send_ready,
                "paste": paste_result,
                "context_check": context_check,
                "physical_send_triggered": True,
            }
        return fail_before_trigger(
            reason=str(
                trigger_result.get("reason")
                or "send_trigger_blocked"
            ),
            error_code=str(
                trigger_result.get("error_code")
                or "SEND_INPUT_FOCUS_NOT_CONFIRMED"
            ),
            extra={"send_trigger": trigger_result},
        )
    input_method = str(
        paste_result.get("input_mode")
        or paste_result.get("method")
        or "sendinput"
    )
    return {
        "ok": True,
        "method": (
            f"{str(locator.get('path') or 'verified_input')}."
            f"{input_method}+keyboard_enter"
        ),
        "path": str(locator.get("path") or "verified_input"),
        "humanized_input": settings,
        "input_result": paste_result,
        "send_button_ready": send_ready,
        "context_check": context_check,
        "send_trigger": trigger_result,
        "physical_send_triggered": True,
    }


def send_with_visual_input(
    hwnd: int,
    text: str,
    *,
    geometry: dict[str, Any],
    settings: dict[str, Any] | None = None,
    artifact_dir: str | None = None,
    before_input_region_seed: dict[str, Any] | None = None,
    before_send_trigger_check: Any | None = None,
    action_journal_path: str = "",
) -> dict[str, Any]:
    return execute_send_transaction(
        hwnd,
        text,
        locator=locate_visual_send_input(
            before_input_region_seed=before_input_region_seed,
        ),
        geometry=geometry,
        settings=settings,
        artifact_dir=artifact_dir,
        before_input_region_seed=before_input_region_seed,
        before_send_trigger_check=before_send_trigger_check,
        action_journal_path=action_journal_path,
    )


def inspect_uia_send_capability(hwnd: int, geometry: dict[str, Any]) -> dict[str, Any]:
    try:
        import uiautomation as auto  # type: ignore
    except Exception as exc:
        return {"ok": False, "reason": "uiautomation_unavailable", "error": repr(exc)}

    try:
        root = auto.ControlFromHandle(hwnd)
        controls = collect_uia_controls(root, max_depth=8, max_count=900)
        snapshot = current_layout_snapshot(hwnd)
        if snapshot is None or bool(snapshot.get("invalidated")) or not bool(snapshot.get("valid")):
            return {
                "ok": False,
                "reason": "WECHAT_UI_LAYOUT_UNRESOLVED",
                "error_code": win32_ocr_layout.ERROR_LAYOUT_UNRESOLVED,
            }
        image_input_bounds = win32_ocr_layout.required_region(snapshot, "input_bounds")
        top_left = win32_ocr_layout.image_point_to_screen(snapshot, image_input_bounds[:2])
        bottom_right = win32_ocr_layout.image_point_to_screen(snapshot, image_input_bounds[2:])
        screen_input_bounds = [*top_left, *bottom_right]
        edit = select_uia_edit_control(controls, screen_input_bounds)
        send_button = select_uia_send_button(controls, screen_input_bounds)
        missing: list[str] = []
        if edit is None:
            missing.append("edit")
        return {
            "ok": not missing,
            "reason": "uia_input_ready" if not missing else "uia_controls_missing",
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


def _screen_rect_inside_bounds(rect: dict[str, int], bounds: list[int]) -> bool:
    return bool(
        len(bounds) == 4
        and int(rect.get("left") or 0) >= bounds[0]
        and int(rect.get("top") or 0) >= bounds[1]
        and int(rect.get("right") or 0) <= bounds[2]
        and int(rect.get("bottom") or 0) <= bounds[3]
    )


def select_uia_edit_control(controls: list[Any], screen_input_bounds: list[int]) -> Any | None:
    candidates: list[tuple[float, Any]] = []
    for control in controls:
        if "edit" not in str(safe_uia_attr(control, "ControlTypeName")).lower():
            continue
        rect = uia_rect_to_dict(safe_uia_attr(control, "BoundingRectangle"))
        if not _screen_rect_inside_bounds(rect, screen_input_bounds):
            continue
        area = max(1, rect["right"] - rect["left"]) * max(1, rect["bottom"] - rect["top"])
        score = area + rect["bottom"] * 2
        candidates.append((score, control))
    return max(candidates, key=lambda item: item[0])[1] if candidates else None


def select_uia_send_button(controls: list[Any], screen_input_bounds: list[int]) -> Any | None:
    candidates: list[tuple[float, Any]] = []
    for control in controls:
        control_type = str(safe_uia_attr(control, "ControlTypeName")).lower()
        name = normalize_ocr_text(safe_uia_attr(control, "Name"))
        if "button" not in control_type:
            continue
        if "发送" not in name and name.lower() not in {"send"}:
            continue
        rect = uia_rect_to_dict(safe_uia_attr(control, "BoundingRectangle"))
        if not _screen_rect_inside_bounds(rect, screen_input_bounds):
            continue
        score = rect["right"] + rect["bottom"] * 2
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
    click_x, click_y, click_meta = choose_session_row_click_point(
        session,
        geometry,
        default_x=default_click_x,
    )
    _sidecar_timing_finish(timing, "activation_choose_click", choose_started)
    timing["activation_candidate_name"] = str(session.get("name") or "")
    timing["activation_click_point"] = [int(click_x), int(click_y)]
    timing["activation_click_candidates"] = click_meta
    timing["activation_click_method"] = "human_window_image_click"
    if session_candidate_is_service_container_wrong_target(session, target):
        timing["reason"] = "service_container_candidate_wrong_target"
        timing["hard_stop"] = True
        return finish(False)
    session_bounds = list(session.get("click_bounds") or [])
    snapshot_id = str(session.get("layout_snapshot_id") or "")
    if len(session_bounds) < 4 or not snapshot_id or int(click_meta.get("candidate_count") or 0) <= 0:
        timing["reason"] = "session_dynamic_click_target_unresolved"
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
    human_window_image_click(
        hwnd,
        click_x,
        click_y,
        bounds=session_bounds,
        expected_snapshot_id=snapshot_id,
    )
    timing["ui_click_performed"] = True
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
    *,
    layout_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    width, height = image_size
    snapshot = layout_snapshot if isinstance(layout_snapshot, dict) else {}
    try:
        header_bounds = win32_ocr_layout.required_region(snapshot, "sidebar_header_bounds")
    except win32_ocr_layout.LayoutSnapshotError:
        return None
    split_x = header_bounds[2]
    header_limit = header_bounds[3]
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
        item_bounds = win32_ocr_layout.normalize_rect(
            [item.get("left"), item.get("top"), item.get("right"), item.get("bottom")]
        )
        return {
            "x": bounded_int(
                int(float(item.get("left") or 0)) + 10,
                default=header_bounds[0] + 24 if header_bounds[2] > header_bounds[0] else 108,
                minimum=header_bounds[0] if header_bounds[2] > header_bounds[0] else 70,
                maximum=header_bounds[2] if header_bounds[2] > header_bounds[0] else 170,
            ),
            "y": bounded_int(
                int(float(item.get("center_y") or 0)),
                default=header_bounds[1] + 24 if header_bounds[3] > header_bounds[1] else 124,
                minimum=header_bounds[1] if header_bounds[3] > header_bounds[1] else 86,
                maximum=header_bounds[3] if header_bounds[3] > header_bounds[1] else 220,
            ),
            "bounds": item_bounds,
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
        back_target = detect_session_subview_back_target(
            ocr_items,
            screenshot.size,
            layout_snapshot=(layout_snapshot_metadata(hwnd).get("snapshot") or {}),
        )
        if not back_target:
            break
        human_window_image_click(
            hwnd,
            int(back_target["x"]),
            int(back_target["y"]),
            bounds=list(back_target.get("bounds") or []),
            expected_snapshot_id=str(
                (layout_snapshot_metadata(hwnd).get("snapshot") or {}).get("layout_snapshot_id") or ""
            ),
        )
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
            layout_snapshot=layout_snapshot_for_image(screenshot),
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


def sidebar_search_focus_indicator_detected(
    screenshot: Any,
    geometry: dict[str, Any] | None = None,
) -> bool:
    if screenshot is None:
        return False
    try:
        image = screenshot.convert("RGB")
    except Exception:
        return False
    try:
        header = win32_ocr_layout.required_region(
            layout_snapshot_for_image(screenshot), "sidebar_header_bounds"
        )
    except win32_ocr_layout.LayoutSnapshotError:
        return False
    header_width = max(1, header[2] - header[0])
    header_height = max(1, header[3] - header[1])
    left = header[0] + int(header_width * 0.10)
    top = header[1] + int(header_height * 0.18)
    right = header[0] + int(header_width * 0.82)
    bottom = header[1] + int(header_height * 0.82)
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
    layout_snapshot: dict[str, Any] | None = None,
) -> str:
    try:
        header = win32_ocr_layout.required_region(layout_snapshot, "sidebar_header_bounds")
    except win32_ocr_layout.LayoutSnapshotError:
        return ""
    header_width = max(1, header[2] - header[0])
    header_height = max(1, header[3] - header[1])
    left = header[0] + int(header_width * 0.10)
    right = header[0] + int(header_width * 0.82)
    top = header[1] + int(header_height * 0.18)
    bottom = header[1] + int(header_height * 0.82)
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
    layout_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    width, height = image_size
    if width <= 0 or height <= 0:
        return None
    active_geometry = geometry if isinstance(geometry, dict) else {"width": width, "height": height}
    try:
        header_bounds = win32_ocr_layout.required_region(layout_snapshot, "sidebar_header_bounds")
    except win32_ocr_layout.LayoutSnapshotError:
        return None
    split_x = header_bounds[2]
    header_left = header_bounds[0]
    header_top = header_bounds[1]
    header_bottom = header_bounds[3]
    expected_x = header_left + int((split_x - header_left) * 0.40)
    expected_y = header_top + int((header_bottom - header_top) * 0.50)
    candidates = []
    for item in ocr_items or []:
        center_x = float(item.get("center_x") or 0)
        center_y = float(item.get("center_y") or 0)
        text = voice_transcribe_compact_text(item.get("text"))
        if not (header_left <= center_x <= split_x - 12 and header_top <= center_y <= header_bottom):
            continue
        if text in {"+", "＋"}:
            continue
        candidates.append((abs(center_x - expected_x) + abs(center_y - expected_y) * 2.0, item))
    if not candidates:
        return None
    _, item = min(candidates, key=lambda entry: entry[0])
    center_y = int(float(item.get("center_y") or expected_y))
    bounds = [
        max(header_left, int(float(item.get("left") or expected_x)) - 34),
        max(header_top, int(float(item.get("top") or center_y)) - 12),
        min(split_x - 12, max(int(float(item.get("right") or expected_x)) + 58, expected_x + 46)),
        min(header_bottom, int(float(item.get("bottom") or center_y)) + 12),
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
        current_snapshot = layout_snapshot_for_image(before_shot) or {}
        search_target = sidebar_search_input_target_from_ocr(
            before_items,
            before_shot.size,
            geometry=active_geometry,
            layout_snapshot=current_snapshot,
        )
        if not search_target:
            return {**result, "ok": False, "reason": "sidebar_search_target_unresolved"}
        search_click = human_window_image_click_in_bounds(
            hwnd,
            int(search_target["point"][0]),
            int(search_target["point"][1]),
            bounds=search_target["bounds"],
            action_name="sidebar_search_dismiss_focus_fresh_target",
            expected_snapshot_id=str(current_snapshot.get("layout_snapshot_id") or ""),
        )
        if not search_click.get("ok"):
            return {
                **result,
                "ok": False,
                "reason": "sidebar_search_dismiss_focus_click_failed",
                "click": search_click,
                "search_target": search_target,
            }
        humanized_action_sleep(180, 420)
        hotkey(win32con.VK_CONTROL, ord("A"))
        humanized_action_sleep(100, 260)
        key_press(win32con.VK_BACK)
        humanized_action_sleep(260, 620)
        cleared_shot, _ = capture_wechat(hwnd, artifact_dir=artifact_dir, label="open_chat_search_dismiss_cleared")
        cleared_items = run_ocr_traced(cleared_shot, "open_chat_search_dismiss_cleared", source="open_chat")
        blank_target = safe_window_header_blank_click_target(
            cleared_items,
            cleared_shot.size,
            geometry=active_geometry,
            layout_snapshot=layout_snapshot_for_image(cleared_shot),
        )
        if not blank_target:
            return {**result, "ok": False, "reason": "safe_header_blank_target_not_found", "search_target": search_target}
        human_window_image_click_in_bounds(
            hwnd,
            int(blank_target["point"][0]),
            int(blank_target["point"][1]),
            bounds=blank_target["bounds"],
            action_name="sidebar_search_dismiss_header_blank_click",
            expected_snapshot_id=str(
                (layout_snapshot_for_image(cleared_shot) or {}).get("layout_snapshot_id") or ""
            ),
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
    layout_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Require a currently visible sidebar search label before clicking."""

    width = int(geometry.get("width") or 0)
    height = int(geometry.get("height") or 0)
    try:
        header_bounds = win32_ocr_layout.required_region(layout_snapshot, "sidebar_header_bounds")
    except win32_ocr_layout.LayoutSnapshotError:
        return {"ok": False, "reason": "search_box_layout_unresolved"}
    split_x = header_bounds[2]
    header_left = header_bounds[0]
    header_top = header_bounds[1]
    header_bottom = header_bounds[3]
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
        if left < header_left or right > split_x - 12 or top < header_top or bottom > header_bottom:
            continue
        bounds = [
            max(header_left, left - 58),
            max(header_top, top - 24),
            min(split_x - 12, right + 82),
            min(header_bottom, bottom + 24),
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
    search_box_evidence = sidebar_search_box_evidence(
        evidence_items,
        geometry=active_geometry,
        layout_snapshot=(layout_snapshot_metadata(hwnd).get("snapshot") or {}),
    )
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
        expected_snapshot_id=str(
            (layout_snapshot_metadata(hwnd).get("snapshot") or {}).get("layout_snapshot_id") or ""
        ),
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
    clear_query_text = sidebar_search_query_text(
        clear_items,
        clear_shot.size,
        geometry=active_geometry,
        layout_snapshot=layout_snapshot_for_image(clear_shot),
    )
    refocus_result: dict[str, Any] = {}
    if clear_surface.get("ok") and not clear_state.get("detected") and not clear_query_text:
        clear_snapshot = layout_snapshot_for_image(clear_shot) or {}
        fresh_refocus_target = sidebar_search_input_target_from_ocr(
            clear_items,
            clear_shot.size,
            geometry=active_geometry,
            layout_snapshot=clear_snapshot,
        )
        if not fresh_refocus_target:
            return {
                "ok": False,
                "reason": "sidebar_search_refocus_target_unresolved",
                "surface": clear_surface,
                "search_state": clear_state,
                "screenshot_path": clear_path,
            }
        refocus_click = human_window_image_click_in_bounds(
            hwnd,
            int(fresh_refocus_target["point"][0]),
            int(fresh_refocus_target["point"][1]),
            bounds=list(fresh_refocus_target["bounds"]),
            action_name="sidebar_search_box_refocus_after_clear",
            expected_snapshot_id=str(clear_snapshot.get("layout_snapshot_id") or ""),
        )
        if not refocus_click.get("ok"):
            return {
                "ok": False,
                "reason": "sidebar_search_refocus_click_failed",
                "click": refocus_click,
                "search_target": fresh_refocus_target,
            }
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
        refocus_query_text = sidebar_search_query_text(
            refocus_items,
            refocus_shot.size,
            geometry=active_geometry,
            layout_snapshot=layout_snapshot_for_image(refocus_shot),
        )
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
        query_text = sidebar_search_query_text(
            verify_items,
            verify_shot.size,
            geometry=active_geometry,
            layout_snapshot=layout_snapshot_for_image(verify_shot),
        )
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
    query_text = sidebar_search_query_text(
        items,
        shot.size,
        geometry=active_geometry,
        layout_snapshot=layout_snapshot_for_image(shot),
    )
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


def write_messages_frame_review(output_dir: Path, payload: dict[str, Any]) -> str:
    observations = [
        item
        for item in (payload.get("observations") or [])
        if isinstance(item, dict)
    ]
    screenshot_path = str(payload.get("screenshot_path") or "")
    rows = [
        _targeting_review_row(
            title="00 最终当前画面",
            purpose="核对本次消息排序、角色和图片槽位所依据的实际微信画面。",
            expected="截图、会话确认和 observations 必须来自同一次当前屏读取。",
            source={"screenshot_path": screenshot_path},
            detection={
                "ok": payload.get("ok"),
                "state": payload.get("state"),
                "page_fingerprint": payload.get("page_fingerprint"),
                "ocr_items_count": payload.get("ocr_items_count"),
                "observation_count": len(observations),
            },
        ),
        _targeting_review_row(
            title="01 当前会话确认",
            purpose="确认本次最终画面仍属于目标短码 private 会话。",
            expected="target_confirmation.ok=true 且会话类型允许 C2 读取。",
            detection=(
                payload.get("target_confirmation")
                if isinstance(payload.get("target_confirmation"), dict)
                else {}
            ),
        ),
        _targeting_review_row(
            title="02 统一消息槽位",
            purpose="查看文字、语音、图片的最终顺序和角色证据。",
            expected="每个物理消息只应出现一次，角色必须来自 C2 统一规则。",
            detection={
                "observations": [
                    {
                        "observation_id": item.get("observation_id"),
                        "row_kind": item.get("row_kind"),
                        "message_type": item.get("message_type"),
                        "sender_role": item.get("sender_role"),
                        "sender_role_source": item.get("sender_role_source"),
                        "bubble_rect": item.get("bubble_rect"),
                        "item_state": item.get("item_state"),
                        "error_code": item.get("error_code"),
                        "reason_detail": item.get("reason_detail"),
                    }
                    for item in observations
                ]
            },
        ),
    ]
    return write_step_event_report(
        output_dir=output_dir,
        json_name="wechat_messages_frame_review.json",
        html_name="wechat_messages_frame_review.html",
        title="C2 当前消息画面复核报告",
        description="本报告固定最终当前屏截图、会话确认和统一消息槽位证据。",
        summary={
            "ok": payload.get("ok"),
            "state": payload.get("state"),
            "screenshot_path": screenshot_path,
            "observation_count": len(observations),
        },
        events=step_events_from_review_rows(rows),
    )


def search_result_contact_candidates_matching_remark_code(
    ocr_items: list[dict[str, Any]],
    image_size: tuple[int, int],
    remark_code: str,
    *,
    layout_snapshot: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    width, height = image_size
    try:
        panel = win32_ocr_layout.required_region(layout_snapshot, "session_list_bounds")
    except win32_ocr_layout.LayoutSnapshotError:
        return []
    left_panel_right = panel[2]

    def in_search_panel(item: dict[str, Any]) -> bool:
        return (
            panel[0] <= float(item.get("center_x") or 0) <= panel[2]
            and panel[1] <= float(item.get("center_y") or 0) <= panel[3]
        )

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
            max(panel[0], int(left) - 74),
            max(panel[1], int(top) - 18),
            min(left_panel_right, max(int(right) + 150, int(left) + 210)),
            min(panel[3], max(int(bottom) + 22, int(top) + 62)),
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
                "layout_snapshot_id": str((layout_snapshot or {}).get("layout_snapshot_id") or ""),
            }
        )
        consumed_rows.append(center_y)
    return matches


def fallback_first_search_contact_candidate(
    ocr_items: list[dict[str, Any]],
    image_size: tuple[int, int],
    remark_code: str,
    *,
    layout_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    width, height = image_size
    try:
        panel = win32_ocr_layout.required_region(layout_snapshot, "session_list_bounds")
    except win32_ocr_layout.LayoutSnapshotError:
        return None
    left_panel_right = panel[2]

    def in_search_panel(item: dict[str, Any]) -> bool:
        return (
            panel[0] <= float(item.get("center_x") or 0) <= panel[2]
            and panel[1] <= float(item.get("center_y") or 0) <= panel[3]
        )

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
        max(panel[0], int(left) - 74),
        max(panel[1], int(top) - 20),
        min(left_panel_right, max(int(right) + 150, int(left) + 240)),
        min(panel[3], max(int(bottom) + 24, int(top) + 68)),
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
        "layout_snapshot_id": str((layout_snapshot or {}).get("layout_snapshot_id") or ""),
    }


def active_selected_session_matches(
    ocr_items: list[dict[str, Any]],
    image_size: tuple[int, int],
    *,
    target: str,
    exact: bool,
    layout_snapshot: dict[str, Any] | None = None,
) -> bool:
    if not target:
        return False
    normalized_target = normalize_session_name(target)
    if not normalized_target:
        return False
    try:
        session_list = win32_ocr_layout.required_region(layout_snapshot, "session_list_bounds")
    except win32_ocr_layout.LayoutSnapshotError:
        return False
    for item in ocr_items or []:
        text = normalize_ocr_text(item.get("text"))
        if not text:
            continue
        center_x = float(item.get("center_x") or 0)
        center_y = float(item.get("center_y") or 0)
        if not win32_ocr_layout.point_in_bounds([center_x, center_y], session_list):
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
    matched = active_selected_session_matches(
        items,
        screenshot.size,
        target=target,
        exact=exact,
        layout_snapshot=layout_snapshot_for_image(screenshot),
    )
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
    candidate_bounds = list(candidate.get("search_result_bounds") or candidate.get("click_bounds") or [])
    if len(candidate_bounds) < 4:
        candidate_bounds = [
            int(float(candidate.get("left") or 0)),
            int(float(candidate.get("top") or 0)),
            int(float(candidate.get("right") or 0)),
            int(float(candidate.get("bottom") or 0)),
        ]
    human_window_image_click_in_bounds(
        hwnd,
        x,
        y,
        bounds=candidate_bounds,
        action_name="search_result_candidate_click",
        expected_snapshot_id=str(candidate.get("layout_snapshot_id") or ""),
    )
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
    search_target = sidebar_search_input_target_from_ocr(
        baseline_items,
        baseline_shot.size,
        geometry=geometry,
        layout_snapshot=(layout_snapshot_metadata(hwnd).get("snapshot") or {}),
    )
    if not search_target:
        return finish(False, "sidebar_search_target_unresolved")
    search_x, search_y = [int(value) for value in search_target["point"]]
    session_click_x = 0
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

    contact_matches = search_result_contact_candidates_matching_remark_code(
        search_items,
        search_shot.size,
        clean_remark,
        layout_snapshot=layout_snapshot_for_image(search_shot),
    )
    sessions = parse_sessions_from_ocr(search_items, search_shot.size, screenshot=search_shot)
    search_snapshot_id = str(
        (layout_snapshot_metadata(hwnd).get("snapshot") or {}).get("layout_snapshot_id") or ""
    )
    for candidate in sessions:
        if isinstance(candidate, dict):
            candidate["layout_snapshot_id"] = search_snapshot_id
    for candidate in contact_matches:
        if isinstance(candidate, dict):
            candidate["layout_snapshot_id"] = search_snapshot_id
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
            contact_matches = search_result_contact_candidates_matching_remark_code(
                search_items,
                search_shot.size,
                clean_remark,
                layout_snapshot=layout_snapshot_for_image(search_shot),
            )
            sessions = parse_sessions_from_ocr(search_items, search_shot.size, screenshot=search_shot)
            session_matches = search_result_sessions_matching_remark_code(sessions, clean_remark)
            matches = contact_matches or session_matches
    if not matches:
        fallback_candidate = fallback_first_search_contact_candidate(
            search_items,
            search_shot.size,
            clean_remark,
            layout_snapshot=layout_snapshot_for_image(search_shot),
        )
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
    session_click_x = 0
    search_target = sidebar_search_input_target_from_ocr(
        ocr_items,
        screenshot.size,
        geometry=geometry,
        layout_snapshot=(layout_snapshot_metadata(hwnd).get("snapshot") or {}),
    )
    search_x, search_y = ([int(value) for value in search_target["point"]] if search_target else [0, 0])
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
        layout_snapshot=layout_snapshot_for_image(screenshot),
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
    if search_target is None:
        return finish(False, "sidebar_search_target_unresolved")
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
    search_active_matches = active_chat_matches(
        search_items,
        search_shot.size,
        target=target,
        exact=exact,
        layout_snapshot=layout_snapshot_for_image(search_shot),
    )
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
        layout_snapshot=layout_snapshot_for_image(screenshot),
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
    title_evidence = active_chat_title_evidence(
        ocr_items,
        screenshot.size,
        target=target,
        exact=exact,
        layout_snapshot=layout_snapshot_for_image(screenshot),
    )
    active_match = bool(title_evidence.get("matched"))
    _sidecar_timing_finish(timing, "validate_active_send_target_active_match", active_match_started)
    if supplied_frame and not active_match:
        # Reuse the business screenshot, not its potentially incomplete
        # full-frame OCR result. A small right-panel ROI preserves the strict
        # title guard without paying for another window capture.
        title_roi_bounds = active_send_target_roi_bounds(
            getattr(screenshot, "size", (0, 0)),
            layout_snapshot=layout_snapshot_for_image(screenshot),
        )
        if not title_roi_bounds:
            return finish({
                "ok": False,
                "online": True,
                "state": "send_layout_unresolved",
                "reason": "WECHAT_UI_LAYOUT_UNRESOLVED",
                "error_code": win32_ocr_layout.ERROR_LAYOUT_UNRESOLVED,
            })
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
            layout_snapshot=layout_snapshot_for_image(screenshot),
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
            screenshot=screenshot,
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


_SEND_OCR_PUNCTUATION_TRANSLATION = str.maketrans(
    {
        "。": ".",
        "｡": ".",
        "、": ",",
        "､": ",",
        "“": '"',
        "”": '"',
        "„": '"',
        "‟": '"',
        "「": '"',
        "」": '"',
        "『": '"',
        "』": '"',
        "‘": "'",
        "’": "'",
        "‚": "'",
        "‛": "'",
        "—": "-",
        "–": "-",
        "―": "-",
        "−": "-",
        "‐": "-",
        "‑": "-",
        "…": "...",
        "‥": "..",
        "【": "[",
        "】": "]",
        "〔": "[",
        "〕": "]",
    }
)
SEND_OCR_MIN_EXPECTED_COVERAGE = 0.80
SEND_OCR_MIN_OBSERVED_COVERAGE = 0.80
SEND_OCR_MIN_SIMILARITY = 0.80
SEND_OCR_MIN_MATCHING_CHARACTERS = 4
SEND_OCR_MAX_REQUIRED_CONTIGUOUS_MATCH = 8


def _normalized_send_ocr_correspondence_text(value: Any) -> str:
    """Canonicalize OCR presentation differences without rewriting content."""

    normalized = unicodedata.normalize("NFKC", str(value or ""))
    normalized = normalized.translate(_SEND_OCR_PUNCTUATION_TRANSLATION)
    normalized = "".join(
        character
        for character in normalized
        if not character.isspace()
        and unicodedata.category(character) != "Cf"
        and ord(character) not in {0xFE0E, 0xFE0F}
    )
    normalized = re.sub(r"\.{2,}", "...", normalized)
    return normalized.casefold()


def _send_ocr_has_readable_text(value: Any) -> bool:
    normalized = _normalized_send_ocr_correspondence_text(value)
    return bool(re.search(r"[0-9a-z\u3400-\u9fff]", normalized))


def _send_ocr_text_correspondence(
    expected_text: Any,
    observed_text: Any,
) -> dict[str, Any]:
    """Correlate OCR text with the just-triggered AI reply.

    Message type and send ownership are deliberately separate decisions. A
    readable self-side OCR result is text even when this correspondence check
    fails. Send ownership additionally requires high ordered overlap in both
    directions so a short shared phrase inside unrelated text cannot confirm
    a send.
    """

    raw_expected = normalized_send_confirmation_text(expected_text)
    raw_observed = normalized_send_confirmation_text(observed_text)
    expected = _normalized_send_ocr_correspondence_text(expected_text)
    observed = _normalized_send_ocr_correspondence_text(observed_text)
    matcher = SequenceMatcher(None, expected, observed, autojunk=False)
    blocks = [block for block in matcher.get_matching_blocks() if block.size > 0]
    matching_characters = sum(block.size for block in blocks)
    longest_matching_block = max((block.size for block in blocks), default=0)
    expected_coverage = (
        matching_characters / len(expected) if expected else 0.0
    )
    observed_coverage = (
        matching_characters / len(observed) if observed else 0.0
    )
    similarity = matcher.ratio() if expected and observed else 0.0
    normalized_exact = bool(expected and observed and expected == observed)
    min_comparable_length = min(len(expected), len(observed))
    required_contiguous_match = min(
        SEND_OCR_MAX_REQUIRED_CONTIGUOUS_MATCH,
        max(SEND_OCR_MIN_MATCHING_CHARACTERS, int(min_comparable_length * 0.20)),
    )
    high_overlap = bool(
        expected
        and observed
        and matching_characters >= SEND_OCR_MIN_MATCHING_CHARACTERS
        and longest_matching_block >= required_contiguous_match
        and expected_coverage >= SEND_OCR_MIN_EXPECTED_COVERAGE
        and observed_coverage >= SEND_OCR_MIN_OBSERVED_COVERAGE
        and similarity >= SEND_OCR_MIN_SIMILARITY
    )
    accepted = bool(normalized_exact or high_overlap)
    if normalized_exact and raw_expected == raw_observed:
        reason = "exact_program_text"
    elif normalized_exact:
        reason = "unicode_normalized_exact_program_text"
    elif high_overlap:
        reason = "high_overlap_program_text"
    elif not expected or not observed:
        reason = "ocr_text_empty"
    else:
        reason = "ocr_text_low_overlap"
    result: dict[str, Any] = {
        "accepted": accepted,
        "exact": normalized_exact,
        "raw_exact": bool(raw_expected and raw_expected == raw_observed),
        "reason": reason,
        "expected_length": len(expected),
        "observed_length": len(observed),
        "matching_characters": matching_characters,
        "longest_matching_block": longest_matching_block,
        "required_contiguous_match": required_contiguous_match,
        "expected_coverage": round(expected_coverage, 6),
        "observed_coverage": round(observed_coverage, 6),
        "similarity": round(similarity, 6),
    }
    return result


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
        "content_normalized": _normalized_send_ocr_correspondence_text(
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
    *,
    message_region_sha256: str = "",
    message_region_bounds: list[int] | None = None,
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
    payload = {
        "schema_version": 1,
        "sequence": sequence,
        "sequence_sha256": hashlib.sha256(serialized.encode("utf-8")).hexdigest(),
        "message_count": len(sequence),
        "bottom": dict(sequence[-1]) if sequence else None,
    }
    clean_region_sha256 = str(message_region_sha256 or "").strip().lower()
    if re.fullmatch(r"[0-9a-f]{64}", clean_region_sha256):
        payload["message_region_sha256"] = clean_region_sha256
        payload["message_region_bounds"] = list(message_region_bounds or [])
    return payload


def send_context_message_region_fingerprint(screenshot: Any) -> dict[str, Any]:
    """Hash only the active chat message viewport, excluding sidebar and input.

    Sidebar unread counters are unrelated to the active conversation and must
    not invalidate a safe send.  The crop deliberately excludes the title,
    scrollbar edge, toolbar and input surface; a changed viewport still falls
    back to the existing strict observation-sequence comparison.
    """

    try:
        image = screenshot.convert("RGB")
        width = int(getattr(image, "width", 0) or 0)
        height = int(getattr(image, "height", 0) or 0)
    except Exception:
        return {}
    if width <= 0 or height <= 0:
        return {}
    try:
        viewport = win32_ocr_layout.required_region(
            layout_snapshot_for_image(screenshot), "message_viewport_bounds"
        )
    except win32_ocr_layout.LayoutSnapshotError:
        return {}
    left, top, right, bottom = viewport
    if right <= left or bottom <= top:
        return {}
    crop = image.crop((left, top, right, bottom))
    digest = hashlib.sha256()
    digest.update(f"{crop.width}x{crop.height}:rgb:".encode("ascii"))
    digest.update(crop.tobytes())
    return {
        "sha256": digest.hexdigest(),
        "bounds": [left, top, right, bottom],
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
    expected_region_sha256 = str(
        expected_payload.get("message_region_sha256") or ""
    ).strip().lower()
    current_region_sha256 = str(
        current_payload.get("message_region_sha256") or ""
    ).strip().lower()
    if (
        re.fullmatch(r"[0-9a-f]{64}", expected_region_sha256)
        and re.fullmatch(r"[0-9a-f]{64}", current_region_sha256)
        and expected_region_sha256 == current_region_sha256
    ):
        return {
            "ok": True,
            "reason": "message_region_unchanged",
            "message_count": len(current_sequence),
            "sequence_sha256": current_payload.get("sequence_sha256"),
            "message_region_sha256": current_region_sha256,
            "bottom": current_payload.get("bottom"),
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
    if not normalized_send_confirmation_text(text):
        return 0
    count = 0
    for message in messages:
        role = str(message.get("sender_role") or message.get("sender") or "").strip().lower()
        if role not in {"self", "sales"}:
            continue
        correspondence = _send_ocr_text_correspondence(
            text,
            message.get("content"),
        )
        if correspondence["accepted"]:
            count += 1
    return count


def recover_expected_self_text_from_structural_candidates(
    screenshot: Any,
    messages: list[dict[str, Any]],
    *,
    target: str,
    expected_text: str,
    ocr_runner: Callable[[Any], list[dict[str, Any]]] | None = None,
    require_correspondence: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Recover a sent text bubble that full-frame OCR classified as an image.

    This is a post-send fact recovery path, not a general image-to-text guess.
    A candidate is retyped when the structural observer independently places
    it on the self/right side, its same-row avatar confirms ``self``, and the
    enhanced ROI OCR contains readable text. Whether that text belongs to the
    just-triggered AI reply is a separate high-overlap decision.
    """

    current = [dict(item) for item in messages if isinstance(item, dict)]
    expected = normalized_send_confirmation_text(expected_text)
    diagnostics: dict[str, Any] = {
        "attempted": False,
        "recovered": False,
        "candidate_count": 0,
        "expected_text_sha256": hashlib.sha256(
            expected.encode("utf-8")
        ).hexdigest() if expected else "",
    }
    if not expected:
        diagnostics["reason"] = "expected_text_empty"
        return current, diagnostics

    candidates: list[tuple[int, dict[str, Any], list[float]]] = []
    for index, message in enumerate(current):
        if str(
            message.get("type") or message.get("message_type") or ""
        ).strip().lower() != "image":
            continue
        structural_side = str(
            message.get("visual_side")
            or message.get("sender_role")
            or message.get("sender")
            or ("self" if message.get("is_self_image") else "")
        ).strip().lower()
        avatar = (
            message.get("avatar_alignment")
            if isinstance(message.get("avatar_alignment"), dict)
            else {}
        )
        avatar_role = str(avatar.get("role") or "").strip().lower()
        bounds = message_rect_bounds(message)
        if structural_side != "self" or avatar_role != "self" or bounds is None:
            continue
        candidates.append((index, message, bounds))
    diagnostics["candidate_count"] = len(candidates)
    if not candidates:
        diagnostics["reason"] = (
            "already_observed_as_self_text"
            if send_reply_match_count(current, expected_text) > 0
            else "no_avatar_confirmed_self_structural_candidate"
        )
        return current, diagnostics

    # The send contract accepts only a newly added bottom-most self bubble.
    # OCR only that same bottom-most candidate; scanning older images would
    # add latency and could not produce an admissible send fact anyway.
    for index, candidate, bounds in sorted(
        candidates,
        key=lambda item: (item[2][1], item[2][0]),
        reverse=True,
    )[:1]:
        diagnostics["attempted"] = True
        enhanced_items = enhanced_ocr_items_for_structural_chat_candidate(
            screenshot,
            bounds,
            ocr_runner=ocr_runner,
        )
        raw_text = "\n".join(
            str(item.get("text") or "").strip()
            for item in sorted(
                enhanced_items,
                key=lambda item: (
                    float(item.get("center_y") or item.get("top") or 0),
                    float(item.get("left") or 0),
                ),
            )
            if str(item.get("text") or "").strip()
        )
        if not _send_ocr_has_readable_text(raw_text):
            diagnostics.update(
                {
                    "reason": "enhanced_ocr_has_no_readable_text",
                    "enhanced_ocr_item_count": len(enhanced_items),
                }
            )
            continue
        correspondence = _send_ocr_text_correspondence(expected_text, raw_text)
        observed_text_sha256 = hashlib.sha256(
            _normalized_send_ocr_correspondence_text(raw_text).encode("utf-8")
        ).hexdigest()
        diagnostics.update(
            {
                "reclassified_as_text": True,
                "ai_reply_correspondence_confirmed": correspondence["accepted"],
                "observed_text_sha256": observed_text_sha256,
                "expected_length": correspondence["expected_length"],
                "observed_length": correspondence["observed_length"],
                "matching_characters": correspondence["matching_characters"],
                "longest_matching_block": correspondence[
                    "longest_matching_block"
                ],
                "required_contiguous_match": correspondence[
                    "required_contiguous_match"
                ],
                "expected_coverage": correspondence["expected_coverage"],
                "observed_coverage": correspondence["observed_coverage"],
                "similarity": correspondence["similarity"],
                "text_correspondence_reason": correspondence["reason"],
            }
        )
        if require_correspondence and not correspondence["accepted"]:
            diagnostics.update(
                {
                    "reason": "current_text_does_not_match_confirmed_reply",
                    "recovered": False,
                }
            )
            return current, diagnostics
        rect = {
            "left": int(bounds[0]),
            "top": int(bounds[1]),
            "right": int(bounds[2]),
            "bottom": int(bounds[3]),
        }
        digest = hashlib.sha1(
            json.dumps(
                {
                    "target": str(target or "").strip().upper(),
                    "sender_role": "self",
                    "bounds": list(rect.values()),
                    "observed_text_sha256": observed_text_sha256,
                    "structural_observation_id": str(
                        candidate.get("observation_id")
                        or candidate.get("message_id")
                        or candidate.get("id")
                        or ""
                    ),
                },
                ensure_ascii=False,
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()[:16]
        confidences = [
            float(item.get("confidence") or 0)
            for item in enhanced_items
            if item.get("confidence") not in (None, "")
        ]
        record = {
            "id": f"win32_ocr_enhanced:{digest}",
            "type": "text",
            "message_type": "text",
            "sender": "self",
            "sender_role": "self",
            "sender_role_algorithm": "wechat_avatar_row_structure_v2",
            "sender_role_confidence": float(
                candidate.get("sender_role_confidence") or 0.98
            ),
            "sender_role_evidence": [
                "structural_candidate_visual_side=self",
                "structural_candidate_same_row_avatar=self",
                "enhanced_roi_ocr_readable_text",
            ],
            "content": raw_text.strip(),
            "content_raw_ocr": raw_text,
            "time": str(candidate.get("time") or ""),
            "source_adapter": "win32_ocr_structural_text_recovery",
            "ocr_confidence": min(confidences) if confidences else None,
            "bubble_rect": rect,
            "ocr_items": enhanced_items,
            "quality_flags": [
                "send_confirmation_enhanced_roi_ocr",
                "structural_image_candidate_reclassified_as_text",
                (
                    "ai_reply_correspondence_confirmed"
                    if correspondence["accepted"]
                    else "ai_reply_correspondence_not_confirmed"
                ),
            ],
            "send_text_correspondence": correspondence,
            "recovered_from_structural_observation_id": str(
                candidate.get("observation_id")
                or candidate.get("message_id")
                or candidate.get("id")
                or ""
            ),
            "avatar_alignment": dict(candidate.get("avatar_alignment") or {}),
        }
        envelope = build_message_envelope(
            record,
            source_adapter="win32_ocr_structural_text_recovery",
            conversation={
                "target_name": target,
                "conversation_type": infer_conversation_type(target),
            },
            ocr_items=enhanced_items,
            bubble_rect=rect,
        )
        current[index] = apply_message_envelope_to_record(record, envelope)
        diagnostics.update(
            {
                "recovered": correspondence["accepted"],
                "reason": (
                    "self_text_reclassified_and_ai_reply_correspondence_confirmed"
                    if correspondence["accepted"]
                    else "self_text_reclassified_but_ai_reply_correspondence_not_confirmed"
                ),
                "candidate_bounds": list(rect.values()),
                "enhanced_ocr_item_count": len(enhanced_items),
            }
        )
        return current, diagnostics

    diagnostics.setdefault("reason", "enhanced_ocr_has_no_readable_text")
    return current, diagnostics


def capture_send_fact_snapshot(
    hwnd: int,
    *,
    target: str,
    text: str,
    exact: bool,
    artifact_dir: str | None,
    label: str,
    recover_expected_self_text: bool = False,
) -> dict[str, Any]:
    screenshot, path = capture_wechat(hwnd, artifact_dir=artifact_dir, label=label)
    return build_send_fact_snapshot_from_frame(
        hwnd,
        target=target,
        text=text,
        exact=exact,
        artifact_dir=artifact_dir,
        label=label,
        screenshot=screenshot,
        screenshot_path=path,
        recover_expected_self_text=recover_expected_self_text,
    )


def build_send_fact_snapshot_from_frame(
    hwnd: int,
    *,
    target: str,
    text: str,
    exact: bool,
    artifact_dir: str | None,
    label: str,
    screenshot: Any,
    screenshot_path: str | None = None,
    ocr_items: list[dict[str, Any]] | None = None,
    recover_expected_self_text: bool = False,
) -> dict[str, Any]:
    supplied_ocr_items = ocr_items is not None
    if ocr_items is None:
        ocr_items = run_ocr_traced(
            screenshot,
            f"{label}_full_ocr",
            source="build_send_fact_snapshot_from_frame",
        )
    geometry = get_window_geometry(hwnd)
    validation = validate_active_send_target(
        hwnd,
        target,
        exact=exact,
        artifact_dir=artifact_dir,
        screenshot=screenshot,
        ocr_items=ocr_items,
        screenshot_path=screenshot_path,
    )
    snapshot_target_ok = bool(
        validation.get("ok") and active_send_guard_is_strong(validation)
    )
    messages = parse_current_chat_frame_messages(
        ocr_items,
        screenshot.size,
        target=target,
        screenshot=screenshot,
    )
    enhanced_text_recovery: dict[str, Any] = {
        "attempted": False,
        "recovered": False,
        "reason": "not_requested",
    }
    if recover_expected_self_text and snapshot_target_ok:
        messages, enhanced_text_recovery = (
            recover_expected_self_text_from_structural_candidates(
                screenshot,
                messages,
                target=target,
                expected_text=text,
            )
        )
    elif recover_expected_self_text:
        enhanced_text_recovery["reason"] = "send_target_not_strongly_confirmed"
    observations = build_message_observations_v3(messages)
    # This relation is frame-local send-confirmation evidence.  It must not be
    # added to the durable source-message transport allowlist, but the send
    # snapshot still needs it to prove that an old structural candidate was
    # retyped rather than treating the same bubble as a newly sent reply.
    recovered_structural_id_by_observation = {
        str(
            message.get("id")
            or message.get("observation_id")
            or ""
        ).strip(): str(
            message.get("recovered_from_structural_observation_id") or ""
        ).strip()
        for message in messages
        if isinstance(message, dict)
        and str(
            message.get("id")
            or message.get("observation_id")
            or ""
        ).strip()
        and str(
            message.get("recovered_from_structural_observation_id") or ""
        ).strip()
    }
    message_region_fingerprint = send_context_message_region_fingerprint(screenshot)
    input_region = input_text_region_state(screenshot, ocr_items, geometry=geometry)
    message_sequence = [
        {
            "sequence_index": index,
            "observation_id": str(observation.get("observation_id") or ""),
            "row_kind": str(observation.get("row_kind") or ""),
            "sender_role": str(observation.get("sender_role") or ""),
            "content": str(observation.get("content_clean") or ""),
            "content_normalized": _normalized_send_ocr_correspondence_text(
                observation.get("content_clean")
            ),
            "bubble_rect": observation.get("bubble_rect"),
            "recovered_from_structural_observation_id": str(
                (
                    observation.get("source_message")
                    if isinstance(observation.get("source_message"), dict)
                    else {}
                ).get("recovered_from_structural_observation_id")
                or recovered_structural_id_by_observation.get(
                    str(observation.get("observation_id") or "").strip()
                )
                or ""
            ),
        }
        for index, observation in enumerate(observations)
        if isinstance(observation, dict)
        and str(observation.get("row_kind") or "")
        in {"text_bubble", "voice_transcript", "image_bubble", "system_message"}
    ]
    frame_observation = immutable_frame_pixel_evidence(
        screenshot,
        hwnd=hwnd,
        geometry=geometry,
        screenshot_path=str(screenshot_path or ""),
    )
    frame_observation.update(
        {
            "schema_version": 1,
            "ocr_regions": ["full_frame"],
            "ocr_engine": "rapidocr",
            "ocr_parameters": {"mode": "default"},
            "ocr_cache_key": (
                f"{frame_observation['frame_id']}:full_frame:rapidocr:default"
            ),
        }
    )
    return {
        "ok": snapshot_target_ok,
        "screenshot_path": screenshot_path,
        "validation": validation,
        "input_region": input_region,
        "matching_self_message_count": send_reply_match_count(messages, text),
        "enhanced_text_recovery": enhanced_text_recovery,
        "message_count": len(messages),
        "observations": observations,
        "send_context_guard": build_send_context_guard(
            observations,
            message_region_sha256=str(
                message_region_fingerprint.get("sha256") or ""
            ),
            message_region_bounds=list(
                message_region_fingerprint.get("bounds") or []
            ),
        ),
        "message_sequence": message_sequence,
        "matching_self_messages": [
            {
                "content": str(message.get("content") or ""),
                "rect": message.get("rect"),
            }
            for message in messages
            if str(message.get("sender_role") or message.get("sender") or "").strip().lower() in {"self", "sales"}
            and _send_ocr_text_correspondence(
                text,
                message.get("content"),
            )["accepted"]
        ],
        "frame_observation": frame_observation,
        "frame_local_reuse": {
            "fast_path_attempted": env_flag(
                "CHEJIN_C3_SEND_FRAME_LOCAL_REUSE_ENABLED", default=True
            ),
            "fast_path_used": bool(
                env_flag(
                    "CHEJIN_C3_SEND_FRAME_LOCAL_REUSE_ENABLED", default=True
                )
                and supplied_ocr_items
            ),
            "fallback_reason": (
                ""
                if supplied_ocr_items
                else "frame_main_ocr_created_for_this_snapshot"
            ),
            "frame_digest_equal": True,
            "ocr_call_count": 0 if supplied_ocr_items else 1,
            "ocr_total_duration_ms": None,
        },
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
            _normalized_send_ocr_correspondence_text(
                item.get("content_normalized")
            ),
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

    baseline_observation_ids = {
        str(item.get("observation_id") or "")
        for item in before
        if str(item.get("observation_id") or "")
    }
    candidates = [
        (index, item)
        for index, item in enumerate(after)
        if index not in matched_after
        and str(item.get("row_kind") or "") == "text_bubble"
        and str(item.get("sender_role") or "") in {"self", "sales"}
        and _send_ocr_text_correspondence(
            text,
            item.get("content_normalized"),
        )["accepted"]
        and (
            not str(item.get("recovered_from_structural_observation_id") or "")
            or str(item.get("recovered_from_structural_observation_id") or "")
            not in baseline_observation_ids
        )
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
    result = dict(candidate)
    result["send_text_correspondence"] = _send_ocr_text_correspondence(
        text,
        candidate.get("content_normalized"),
    )
    return result


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
    initial_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    attempts: list[dict[str, Any]] = []
    for attempt in range(1, max(1, max_attempts) + 1):
        if attempt > 1:
            time.sleep(min(1.2, 0.35 + attempt * 0.12))
        try:
            if attempt == 1 and isinstance(initial_snapshot, dict):
                snapshot = dict(initial_snapshot)
            else:
                snapshot = capture_send_fact_snapshot(
                    hwnd,
                    target=target,
                    text=text,
                    exact=exact,
                    artifact_dir=artifact_dir,
                    label=f"send_result_confirm_{attempt}",
                    recover_expected_self_text=True,
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


def get_window_client_geometry(hwnd: int) -> dict[str, int]:
    return win32_ocr_window_metrics.get_window_client_geometry(hwnd, win32gui_module=win32gui)


def add_friend_device_profile(hwnd: int, *, geometry: dict[str, Any] | None = None, screenshot_size: tuple[int, int] | None = None, route: str = '') -> dict[str, Any]:
    win32_ocr_add_friend_windows.bind_sidecar_ops(sys.modules[__name__])
    return win32_ocr_add_friend_windows.add_friend_device_profile(hwnd, geometry=geometry, screenshot_size=screenshot_size, route=route)


def validate_capture_geometry(geometry: dict[str, Any]) -> dict[str, Any]:
    return win32_ocr_geometry.validate_capture_geometry(geometry)


def validate_send_geometry(geometry: dict[str, Any]) -> dict[str, Any]:
    return win32_ocr_geometry.validate_send_geometry(geometry)


def rpa_click_surface_jitter_enabled() -> bool:
    return env_flag("WECHAT_WIN32_OCR_CLICK_SURFACE_JITTER_ENABLED", default=True)


def jitter_client_click_surface_point(hwnd: int, x: int, y: int) -> tuple[int, int, dict[str, Any]]:
    """Apply tiny generic jitter; target bounds are enforced by the caller."""
    original_x = int(x)
    original_y = int(y)
    if not rpa_click_surface_jitter_enabled():
        return original_x, original_y, {"enabled": False, "original": [original_x, original_y], "final": [original_x, original_y]}
    role = "bounded_target"
    jitter_x, jitter_y = 3, 2
    final_x = original_x + random.randint(-jitter_x, jitter_x)
    final_y = original_y + random.randint(-jitter_y, jitter_y)
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
    role = "bounded_target"
    jitter_x, jitter_y = 3, 2
    final_x = original_x + random.randint(-jitter_x, jitter_x)
    final_y = original_y + random.randint(-jitter_y, jitter_y)
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
    layout_snapshot: dict[str, Any] | None = None,
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
    snapshot = layout_snapshot if isinstance(layout_snapshot, dict) else {}
    title_bounds = win32_ocr_layout.normalize_rect(snapshot.get("chat_header_bounds"))
    if not bool(snapshot.get("valid")) or title_bounds[2] <= title_bounds[0]:
        return {
            "matched": False,
            "conversation_type": "unknown",
            "reason": "layout_snapshot_missing_for_title",
            "raw_title": "",
            "title_candidates": [],
            "short_code_confirmed": False,
            "admission_allowed": False,
            "error_code": win32_ocr_layout.ERROR_LAYOUT_UNRESOLVED,
        }
    split_x = title_bounds[0]
    title_left = title_bounds[0]
    title_right = title_bounds[2]
    title_top = title_bounds[1]
    title_bottom = title_bounds[3]
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


def active_chat_matches(
    ocr_items: list[dict[str, Any]],
    image_size: tuple[int, int],
    *,
    target: str,
    exact: bool,
    layout_snapshot: dict[str, Any] | None = None,
) -> bool:
    evidence = active_chat_title_evidence(
        ocr_items,
        image_size,
        target=target,
        exact=exact,
        layout_snapshot=layout_snapshot,
    )
    return bool(evidence.get("matched"))


def target_switch_passive_confirm_attempts() -> int:
    return bounded_int(
        os.getenv("WECHAT_WIN32_OCR_TARGET_SWITCH_PASSIVE_CONFIRM_ATTEMPTS"),
        default=2,
        minimum=1,
        maximum=4,
    )


def _current_message_viewport_scroll_point(hwnd: int) -> tuple[int, int, int, int, list[int], str]:
    current = current_layout_snapshot(hwnd) or {}
    snapshot, failure = _current_click_snapshot(
        hwnd,
        expected_snapshot_id=str(current.get("layout_snapshot_id") or ""),
    )
    if failure or snapshot is None:
        raise RuntimeError(
            f"{(failure or {}).get('error_code') or win32_ocr_layout.ERROR_LAYOUT_UNRESOLVED}:"
            f"{(failure or {}).get('reason') or 'layout_snapshot_missing'}"
        )
    bounds = win32_ocr_layout.required_region(snapshot, "message_viewport_bounds")
    x = int((bounds[0] + bounds[2]) / 2)
    y = int((bounds[1] + bounds[3]) / 2)
    mapped = win32_ocr_layout.transform_target_to_screen(
        snapshot,
        point=[x, y],
        bounds=bounds,
    )
    return (
        x,
        y,
        int(mapped["screen_point"][0]),
        int(mapped["screen_point"][1]),
        bounds,
        str(snapshot.get("layout_snapshot_id") or ""),
    )


def scroll_chat_history(hwnd: int, load_times: int, *, wheel_units: int = 8, delay_seconds: float = 0.18) -> None:
    x, y, screen_x, screen_y, bounds, snapshot_id = _current_message_viewport_scroll_point(hwnd)
    require_active_ui_action_budget(
        "scroll_chat_history",
        metadata={
            "load_times": int(load_times or 0),
            "cursor": [int(x), int(y)],
            "screen_cursor": [screen_x, screen_y],
            "bounds": bounds,
            "layout_snapshot_id": snapshot_id,
            "wheel_units": int(wheel_units or 0),
        },
    )
    activate_window(hwnd)
    ensure_left_button_released()
    win32api.SetCursorPos((screen_x, screen_y))
    invalidate_layout_snapshot(hwnd, reason="scroll_started")
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
    x, y, screen_x, screen_y, bounds, snapshot_id = _current_message_viewport_scroll_point(hwnd)
    require_active_ui_action_budget(
        "scroll_chat_to_latest",
        metadata={
            "attempts": requested_attempts,
            "actual_attempts": actual_attempts,
            "cursor": [int(x), int(y)],
            "screen_cursor": [screen_x, screen_y],
            "bounds": bounds,
            "layout_snapshot_id": snapshot_id,
        },
    )
    activate_window(hwnd)
    ensure_left_button_released()
    win32api.SetCursorPos((screen_x, screen_y))
    invalidate_layout_snapshot(hwnd, reason="scroll_started")
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


def _register_layout_snapshot(
    hwnd: int,
    image: Any,
    *,
    capture_mode: str,
    screenshot_path: str,
    capture_screen_origin: list[int] | tuple[int, int] | None,
    generic_popup: bool = False,
) -> dict[str, Any]:
    image_size = getattr(image, "size", (0, 0))
    geometry = get_window_geometry(hwnd)
    client_geometry = get_window_client_geometry(hwnd)
    client_origin = None
    if isinstance(client_geometry, dict):
        if client_geometry.get("screen_left") is not None and client_geometry.get("screen_top") is not None:
            client_origin = [
                int(client_geometry.get("screen_left") or 0),
                int(client_geometry.get("screen_top") or 0),
            ]
    if generic_popup:
        width, height = [int(value or 0) for value in image_size[:2]]
        layout = {
            "ok": bool(width > 0 and height > 0),
            "regions": {"surface_bounds": [0, 0, width, height]},
            "anchors": [{"name": "popup_window_bounds", "confidence": 1.0}],
            "confidence": 1.0 if width > 0 and height > 0 else 0.0,
            "conflicts": [] if width > 0 and height > 0 else ["popup_surface_empty"],
            "vertical_candidates": [],
        }
        required_region_names = win32_ocr_layout.POPUP_LAYOUT_REGION_NAMES
        surface_kind = "popup"
    else:
        layout = win32_ocr_layout.build_structural_layout_regions(image)
        required_region_names = win32_ocr_layout.REQUIRED_LAYOUT_REGION_NAMES
        surface_kind = "wechat_main"
    screen_profile = screen_work_area(hwnd)
    window_structure = str(os.getenv("WECHAT_WIN32_OCR_WINDOW_STRUCTURE") or "").strip()
    if not window_structure and win32gui is not None:
        try:
            window_structure = str(win32gui.GetClassName(int(hwnd)) or "").strip()
        except Exception:
            window_structure = ""
    device_profile = win32_ocr_device_profile.build_device_profile(
        route="layout_snapshot",
        geometry=geometry,
        screenshot_size=(int(image_size[0] or 0), int(image_size[1] or 0)),
        client_rect=client_geometry,
        dpi_scale=window_dpi_scale(hwnd),
        screen=screen_profile,
        sidebar_bounds=list((layout.get("regions") or {}).get("sidebar_bounds") or []),
        wechat_version=str(os.getenv("WECHAT_WIN32_OCR_WECHAT_VERSION") or "").strip(),
        window_structure=window_structure,
    )
    snapshot = win32_ocr_layout.build_layout_snapshot(
        hwnd=int(hwnd),
        frame_id=win32_ocr_layout.new_frame_id(int(hwnd)),
        capture_mode=capture_mode,
        image_size=(int(image_size[0] or 0), int(image_size[1] or 0)),
        capture_screen_origin=capture_screen_origin,
        window_rect=geometry,
        client_rect=client_geometry,
        client_screen_origin=client_origin,
        dpi_scale=window_dpi_scale(hwnd),
        regions=layout.get("regions") or {},
        anchors=layout.get("anchors") or [],
        confidence=float(layout.get("confidence") or 0.0),
        conflicts=list(layout.get("conflicts") or []),
        executable=bool(layout.get("ok")),
        screenshot_path=screenshot_path,
        surface_kind=surface_kind,
        required_region_names=required_region_names,
    )
    snapshot["layout_builder"] = {
        "ok": bool(layout.get("ok")),
        "confidence": float(layout.get("confidence") or 0.0),
        "conflicts": list(layout.get("conflicts") or []),
        "vertical_candidates": list(layout.get("vertical_candidates") or []),
    }
    snapshot["compatibility"] = {
        "mode": "dynamic_layout_only",
        "device_profile": device_profile,
    }
    previous_id = _LATEST_LAYOUT_SNAPSHOT_BY_HWND.get(int(hwnd))
    if previous_id:
        _LAYOUT_SNAPSHOT_STORE.invalidate(previous_id, reason="new_frame_captured")
    _LAYOUT_SNAPSHOT_STORE.put(snapshot)
    _LATEST_LAYOUT_SNAPSHOT_BY_HWND[int(hwnd)] = str(snapshot["layout_snapshot_id"])
    _LAYOUT_SNAPSHOT_ID_BY_IMAGE_ID[id(image)] = str(snapshot["layout_snapshot_id"])
    return snapshot


def current_layout_snapshot(hwnd: int) -> dict[str, Any] | None:
    snapshot_id = _LATEST_LAYOUT_SNAPSHOT_BY_HWND.get(int(hwnd or 0))
    if not snapshot_id:
        return None
    return _LAYOUT_SNAPSHOT_STORE.get(snapshot_id)


def layout_snapshot_for_image(image: Any) -> dict[str, Any] | None:
    snapshot_id = _LAYOUT_SNAPSHOT_ID_BY_IMAGE_ID.get(id(image))
    if not snapshot_id:
        return None
    return _LAYOUT_SNAPSHOT_STORE.get(snapshot_id)


def finalize_add_friend_entry_layout_snapshot(
    image: Any,
    items: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Finalize the current frame with only add-friend entry requirements."""

    snapshot_id = _LAYOUT_SNAPSHOT_ID_BY_IMAGE_ID.get(id(image))
    existing = _LAYOUT_SNAPSHOT_STORE.get(snapshot_id or "")
    if existing is None or bool(existing.get("invalidated")):
        return None
    layout = win32_ocr_layout.build_add_friend_entry_layout_regions(
        image,
        search_anchor_items=_sidebar_search_semantic_candidates(items),
    )
    completed = win32_ocr_layout.build_layout_snapshot(
        hwnd=int(existing.get("hwnd") or 0),
        frame_id=str(existing.get("frame_id") or ""),
        capture_mode=str(existing.get("capture_mode") or ""),
        image_size=(
            int(existing.get("image_width") or 0),
            int(existing.get("image_height") or 0),
        ),
        capture_screen_origin=existing.get("capture_screen_origin"),
        window_rect=existing.get("window_rect"),
        client_rect=existing.get("client_rect"),
        client_screen_origin=existing.get("client_screen_origin"),
        dpi_scale=float(existing.get("dpi_scale") or 1.0),
        regions=layout.get("regions") or {},
        anchors=layout.get("anchors") or [],
        confidence=float(layout.get("confidence") or 0.0),
        conflicts=list(layout.get("conflicts") or []),
        executable=bool(layout.get("ok")),
        screenshot_path=str(existing.get("screenshot_path") or ""),
        surface_kind="wechat_main_add_friend_entry",
        required_region_names=win32_ocr_layout.ADD_FRIEND_ENTRY_LAYOUT_REGION_NAMES,
    )
    completed["layout_builder"] = {
        "ok": bool(layout.get("ok")),
        "confidence": float(layout.get("confidence") or 0.0),
        "conflicts": list(layout.get("conflicts") or []),
        "vertical_candidates": list(layout.get("vertical_candidates") or []),
    }
    completed["compatibility"] = dict(existing.get("compatibility") or {})
    _LAYOUT_SNAPSHOT_STORE.invalidate(
        str(existing.get("layout_snapshot_id") or ""),
        reason="add_friend_action_layout_finalized",
    )
    _LAYOUT_SNAPSHOT_STORE.put(completed)
    _LATEST_LAYOUT_SNAPSHOT_BY_HWND[int(completed.get("hwnd") or 0)] = str(
        completed.get("layout_snapshot_id") or ""
    )
    _LAYOUT_SNAPSHOT_ID_BY_IMAGE_ID[id(image)] = str(
        completed.get("layout_snapshot_id") or ""
    )
    return _LAYOUT_SNAPSHOT_STORE.get(str(completed.get("layout_snapshot_id") or ""))


def _sidebar_search_semantic_candidates(
    items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Collect search candidates with the proven 0.9.20 OCR semantics.

    In particular, OCR outputs such as ``Q搜索`` remain valid. Pixel-derived
    layout boundaries select the topmost sidebar operation row afterward.
    """

    anchors: list[dict[str, Any]] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        compact = win32_ocr_add_friend_windows.add_friend_ocr_compact(
            item.get("text")
        )
        if "搜索" not in compact:
            continue
        try:
            left = int(float(item.get("left") or 0))
            top = int(float(item.get("top") or 0))
            right = int(float(item.get("right") or left))
            bottom = int(float(item.get("bottom") or top))
        except (TypeError, ValueError):
            continue
        if right <= left or bottom <= top:
            continue
        anchors.append(dict(item))
    return anchors


def _finalize_layout_snapshot_ocr_anchors(image: Any, items: list[dict[str, Any]]) -> None:
    snapshot_id = _LAYOUT_SNAPSHOT_ID_BY_IMAGE_ID.get(id(image))
    if not snapshot_id:
        return
    existing = _LAYOUT_SNAPSHOT_STORE.get(snapshot_id)
    if existing is None or bool(existing.get("invalidated")):
        return
    if str(existing.get("surface_kind") or "") != "wechat_main":
        return
    search_anchor_items = _sidebar_search_semantic_candidates(items)
    layout = win32_ocr_layout.build_structural_layout_regions(
        image,
        ocr_items=items,
        search_anchor_items=search_anchor_items,
    )
    anchors: list[dict[str, Any]] = list(layout.get("anchors") or [])
    for item in items or []:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        if not text or not any(token in text for token in ("搜索", "发送", "确定", "备注", "添加朋友")):
            continue
        anchors.append(
            {
                "name": "ocr_anchor",
                "text": text,
                "bounds": [
                    int(float(item.get("left") or 0)),
                    int(float(item.get("top") or 0)),
                    int(float(item.get("right") or 0)),
                    int(float(item.get("bottom") or 0)),
                ],
                "confidence": float(item.get("confidence") or 0.0),
            }
        )
    completed = win32_ocr_layout.build_layout_snapshot(
        hwnd=int(existing.get("hwnd") or 0),
        frame_id=str(existing.get("frame_id") or ""),
        capture_mode=str(existing.get("capture_mode") or ""),
        image_size=(
            int(existing.get("image_width") or 0),
            int(existing.get("image_height") or 0),
        ),
        capture_screen_origin=existing.get("capture_screen_origin"),
        window_rect=existing.get("window_rect"),
        client_rect=existing.get("client_rect"),
        client_screen_origin=existing.get("client_screen_origin"),
        dpi_scale=float(existing.get("dpi_scale") or 1.0),
        regions=layout.get("regions") or {},
        anchors=anchors,
        confidence=float(layout.get("confidence") or 0.0),
        conflicts=list(layout.get("conflicts") or []),
        executable=bool(layout.get("ok")),
        screenshot_path=str(existing.get("screenshot_path") or ""),
        surface_kind="wechat_main",
        required_region_names=win32_ocr_layout.REQUIRED_LAYOUT_REGION_NAMES,
    )
    completed["layout_builder"] = {
        "ok": bool(layout.get("ok")),
        "confidence": float(layout.get("confidence") or 0.0),
        "conflicts": list(layout.get("conflicts") or []),
        "vertical_candidates": list(layout.get("vertical_candidates") or []),
        "source": "current_frame_pixels_plus_0_9_20_search_anchor",
    }
    completed["compatibility"] = dict(existing.get("compatibility") or {})
    _LAYOUT_SNAPSHOT_STORE.invalidate(
        str(existing.get("layout_snapshot_id") or ""),
        reason="current_frame_ocr_layout_finalized",
    )
    _LAYOUT_SNAPSHOT_STORE.put(completed)
    completed_id = str(completed.get("layout_snapshot_id") or "")
    _LATEST_LAYOUT_SNAPSHOT_BY_HWND[int(completed.get("hwnd") or 0)] = completed_id
    _LAYOUT_SNAPSHOT_ID_BY_IMAGE_ID[id(image)] = completed_id


def layout_snapshot_metadata(hwnd: int) -> dict[str, Any]:
    snapshot = current_layout_snapshot(hwnd)
    if snapshot is None:
        return {
            "ok": False,
            "error_code": win32_ocr_layout.ERROR_LAYOUT_UNRESOLVED,
            "reason": "layout_snapshot_missing",
        }
    return {"ok": True, "snapshot": snapshot}


def invalidate_layout_snapshot(hwnd: int, *, reason: str) -> None:
    snapshot_id = _LATEST_LAYOUT_SNAPSHOT_BY_HWND.get(int(hwnd or 0))
    if snapshot_id:
        _LAYOUT_SNAPSHOT_STORE.invalidate(snapshot_id, reason=reason)


def invalidate_all_layout_snapshots(*, reason: str) -> None:
    _LAYOUT_SNAPSHOT_STORE.invalidate_all(reason=reason)


def _current_click_snapshot(hwnd: int, *, expected_snapshot_id: str = "") -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    if not str(expected_snapshot_id or "").strip():
        return None, {
            "ok": False,
            "error_code": win32_ocr_layout.ERROR_COORDINATE_MAPPING_INVALID,
            "reason": "expected_layout_snapshot_id_missing",
        }
    snapshot = current_layout_snapshot(hwnd)
    if snapshot is None:
        return None, {
            "ok": False,
            "error_code": win32_ocr_layout.ERROR_LAYOUT_UNRESOLVED,
            "reason": "layout_snapshot_missing",
        }
    if bool(snapshot.get("invalidated")):
        return None, {
            "ok": False,
            "error_code": win32_ocr_layout.ERROR_LAYOUT_STALE,
            "reason": str(snapshot.get("invalidated_reason") or "layout_snapshot_invalidated"),
            "layout_snapshot_id": str(snapshot.get("layout_snapshot_id") or ""),
        }
    if expected_snapshot_id and str(snapshot.get("layout_snapshot_id") or "") != str(expected_snapshot_id):
        return None, {
            "ok": False,
            "error_code": win32_ocr_layout.ERROR_LAYOUT_STALE,
            "reason": "layout_snapshot_id_mismatch",
            "expected_layout_snapshot_id": str(expected_snapshot_id),
            "actual_layout_snapshot_id": str(snapshot.get("layout_snapshot_id") or ""),
        }
    if not bool(snapshot.get("executable")) or not bool(snapshot.get("clickable")):
        return None, {
            "ok": False,
            "error_code": win32_ocr_layout.ERROR_LAYOUT_UNRESOLVED,
            "reason": "layout_snapshot_not_executable",
            "layout_snapshot_id": str(snapshot.get("layout_snapshot_id") or ""),
            "conflicts": list(snapshot.get("conflicts") or []),
        }
    if not win32_ocr_layout.current_geometry_matches(
        snapshot,
        geometry_provider=get_window_geometry,
        client_geometry_provider=get_window_client_geometry,
        dpi_provider=window_dpi_scale,
    ):
        invalidate_layout_snapshot(hwnd, reason="window_geometry_changed_before_click")
        return None, {
            "ok": False,
            "error_code": win32_ocr_layout.ERROR_LAYOUT_STALE,
            "reason": "window_geometry_changed_before_click",
            "layout_snapshot_id": str(snapshot.get("layout_snapshot_id") or ""),
        }
    current_window = get_window_geometry(hwnd)
    current_client = get_window_client_geometry(hwnd)
    capture_mode = str(snapshot.get("capture_mode") or "")
    current_capture_origin = None
    if capture_mode == win32_ocr_layout.CAPTURE_MODE_WINDOW_VISIBLE_SCREEN:
        expected_size = (
            int(current_window.get("right") or 0) - int(current_window.get("left") or 0),
            int(current_window.get("bottom") or 0) - int(current_window.get("top") or 0),
        )
        image_size = (
            int(snapshot.get("image_width") or 0),
            int(snapshot.get("image_height") or 0),
        )
        if image_size != expected_size:
            current_capture_origin = None
        else:
            current_capture_origin = [
                int(current_window.get("left") or 0),
                int(current_window.get("top") or 0),
            ]
    elif capture_mode == win32_ocr_layout.CAPTURE_MODE_CLIENT_AREA:
        if current_client.get("screen_left") is not None and current_client.get("screen_top") is not None:
            current_capture_origin = [
                int(current_client.get("screen_left") or 0),
                int(current_client.get("screen_top") or 0),
            ]
    else:
        current_capture_origin = snapshot.get("capture_screen_origin")
    current_client_origin = None
    if current_client.get("screen_left") is not None and current_client.get("screen_top") is not None:
        current_client_origin = [
            int(current_client.get("screen_left") or 0),
            int(current_client.get("screen_top") or 0),
        ]
    if not win32_ocr_layout.snapshot_matches_current(
        snapshot,
        hwnd=hwnd,
        window_rect=current_window,
        client_rect=current_client,
        dpi_scale=window_dpi_scale(hwnd),
        image_size=(snapshot.get("image_width"), snapshot.get("image_height")),
        capture_mode=capture_mode,
        capture_screen_origin=current_capture_origin,
        client_screen_origin=current_client_origin,
    ):
        invalidate_layout_snapshot(hwnd, reason="capture_mapping_changed_before_click")
        return None, {
            "ok": False,
            "error_code": win32_ocr_layout.ERROR_LAYOUT_STALE,
            "reason": "capture_mapping_changed_before_click",
            "layout_snapshot_id": str(snapshot.get("layout_snapshot_id") or ""),
        }
    return snapshot, None


def _layout_region_for_point(snapshot: dict[str, Any], x: int, y: int, bounds: list[int] | None) -> list[int]:
    if isinstance(bounds, list) and len(bounds) >= 4:
        normalized = win32_ocr_layout.normalize_rect(bounds)
        if not win32_ocr_layout.point_in_bounds([x, y], normalized):
            raise win32_ocr_layout.LayoutSnapshotError(
                "target_point_outside_target_bounds",
                code=win32_ocr_layout.ERROR_COORDINATE_MAPPING_INVALID,
            )
        for region_name in tuple(snapshot.get("action_region_names") or []):
            region = win32_ocr_layout.normalize_rect(snapshot.get(region_name))
            if (
                region[0] <= normalized[0]
                and region[1] <= normalized[1]
                and region[2] >= normalized[2]
                and region[3] >= normalized[3]
            ):
                return normalized
        raise win32_ocr_layout.LayoutSnapshotError(
            "target_bounds_outside_dynamic_layout_regions",
            code=win32_ocr_layout.ERROR_COORDINATE_MAPPING_INVALID,
        )
    for region_name in tuple(snapshot.get("action_region_names") or []):
        region = snapshot.get(region_name)
        if win32_ocr_layout.point_in_bounds([x, y], region):
            return win32_ocr_layout.normalize_rect(region)
    raise win32_ocr_layout.LayoutSnapshotError(
        "target_point_outside_layout_regions",
        code=win32_ocr_layout.ERROR_COORDINATE_MAPPING_INVALID,
    )


def _map_window_image_target(
    hwnd: int,
    x: int,
    y: int,
    *,
    bounds: list[int] | None = None,
    expected_snapshot_id: str = "",
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    snapshot, failure = _current_click_snapshot(hwnd, expected_snapshot_id=expected_snapshot_id)
    if failure:
        return None, failure
    assert snapshot is not None
    try:
        target_bounds = _layout_region_for_point(snapshot, int(x), int(y), bounds)
        mapped = win32_ocr_layout.transform_target_to_screen(
            snapshot,
            point=[int(x), int(y)],
            bounds=target_bounds,
        )
        mapped["image_bounds"] = target_bounds
        return mapped, None
    except win32_ocr_layout.LayoutSnapshotError as exc:
        return None, {
            "ok": False,
            "error_code": win32_ocr_layout.ERROR_COORDINATE_MAPPING_INVALID,
            "reason": exc.reason,
            "details": exc.details,
            "layout_snapshot_id": str(snapshot.get("layout_snapshot_id") or ""),
        }


def capture_wechat(hwnd: int, *, artifact_dir: str | None = None, label: str = "wechat") -> tuple[Any, str]:
    geometry = get_window_geometry(hwnd)
    rect = (
        int(geometry.get("left") or 0),
        int(geometry.get("top") or 0),
        int(geometry.get("right") or 0),
        int(geometry.get("bottom") or 0),
    )
    image = try_image_grab(rect)
    capture_mode = win32_ocr_layout.CAPTURE_MODE_WINDOW_VISIBLE_SCREEN
    expected_size = (max(0, rect[2] - rect[0]), max(0, rect[3] - rect[1]))
    capture_origin: list[int] | None = (
        [rect[0], rect[1]]
        if image is not None and tuple(getattr(image, "size", (0, 0))[:2]) == expected_size
        else None
    )
    if image is None:
        image = capture_window_image(hwnd)
        capture_mode = win32_ocr_layout.CAPTURE_MODE_PRINT_WINDOW
    if image is None:
        candidates = capture_window_by_rect(hwnd)
        if not candidates:
            raise RuntimeError("capture_wechat_failed: no screenshot candidate is available")
        image = win32_ocr_capture.select_best_capture_candidate(candidates, score=image_information_score)
        capture_mode = win32_ocr_layout.CAPTURE_MODE_WINDOW_VISIBLE_SCREEN
        capture_origin = (
            [rect[0], rect[1]]
            if tuple(getattr(image, "size", (0, 0))[:2]) == expected_size
            else None
        )
    saved = save_screenshot_artifact(image, artifact_dir=artifact_dir, label=label)
    _register_layout_snapshot(
        hwnd,
        image,
        capture_mode=capture_mode,
        screenshot_path=saved,
        capture_screen_origin=capture_origin,
    )
    return image, saved


def capture_visible_screen(
    *,
    artifact_dir: str | None = None,
    label: str = "screen_visible",
    hwnd: int = 0,
) -> tuple[Any, str]:
    try:
        image = ImageGrab.grab()
    except Exception as exc:
        raise RuntimeError(f"capture_visible_screen_failed: {exc!r}") from exc
    saved = save_screenshot_artifact(image, artifact_dir=artifact_dir, label=label)
    # A desktop-sized capture is evidence only: its origin is not proof for a
    # WeChat HWND click.  It therefore creates no actionable snapshot and also
    # invalidates the prior one, forcing a fresh window capture before any
    # later physical action.
    if int(hwnd or 0):
        invalidate_layout_snapshot(int(hwnd), reason="desktop_evidence_frame_captured")
    else:
        invalidate_all_layout_snapshots(reason="desktop_evidence_frame_captured")
    return image, saved


def capture_wechat_window_visible_screen(
    hwnd: int,
    *,
    artifact_dir: str | None = None,
    label: str = "wechat_window_visible",
    popup_window: bool = False,
) -> tuple[Any, str]:
    rect = win32gui.GetWindowRect(hwnd)
    image = try_image_grab((int(rect[0]), int(rect[1]), int(rect[2]), int(rect[3])))
    if image is None:
        raise RuntimeError("capture_wechat_window_visible_screen_failed")
    saved = save_screenshot_artifact(image, artifact_dir=artifact_dir, label=label)
    expected_size = (max(0, int(rect[2] - rect[0])), max(0, int(rect[3] - rect[1])))
    _register_layout_snapshot(
        hwnd,
        image,
        capture_mode=win32_ocr_layout.CAPTURE_MODE_WINDOW_VISIBLE_SCREEN,
        screenshot_path=saved,
        capture_screen_origin=(
            [int(rect[0]), int(rect[1])]
            if tuple(getattr(image, "size", (0, 0))[:2]) == expected_size
            else None
        ),
        generic_popup=popup_window,
    )
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
    _finalize_layout_snapshot_ocr_anchors(image, items)
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
    layout_snapshot: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    if screenshot is None or not hasattr(screenshot, "crop"):
        return []
    width, height = image_size
    if width <= 0 or height <= 0:
        return []
    snapshot = layout_snapshot or layout_snapshot_for_image(screenshot) or {}
    session_bounds = win32_ocr_layout.normalize_rect(snapshot.get("session_list_bounds"))
    if not bool(snapshot.get("valid")) or session_bounds[2] <= session_bounds[0]:
        return []
    crop_left = session_bounds[0]
    crop_top = session_bounds[1]
    crop_right = session_bounds[2]
    crop_bottom = session_bounds[3]
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
        if not text or not is_c2_session_title_candidate(text):
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
    enhanced_items = sidebar_visible_list_enhanced_ocr_items(
        screenshot,
        image_size,
        layout_snapshot=layout_snapshot_for_image(screenshot),
    )
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
    screenshot: Any | None = None,
) -> dict[str, Any]:
    if not allow_blind_target_confirmation(target):
        return {"ok": False}
    sidebar_match_count = 0
    sidebar_sessions = parse_sessions_from_ocr(ocr_items, image_size, screenshot=screenshot)
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
    layout_snapshot: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    width, height = image_size
    snapshot = layout_snapshot or layout_snapshot_for_image(screenshot) or {}
    session_bounds = win32_ocr_layout.normalize_rect(snapshot.get("session_list_bounds"))
    if not bool(snapshot.get("valid")) or session_bounds[2] <= session_bounds[0]:
        return []
    split_x = session_bounds[2]
    snapshot_id = str(snapshot.get("layout_snapshot_id") or "")
    min_header_y = session_bounds[1]
    left_min = session_bounds[0]
    left_max = session_bounds[2]
    right_limit = session_bounds[2]
    min_session_row_gap = max(34, int(height * 0.048))
    geometric_items: list[dict[str, Any]] = []
    for item in ocr_items:
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        if item["center_y"] < min_header_y or item["center_y"] > height - 20:
            continue
        if item["left"] < left_min or item["left"] > left_max:
            continue
        if item["right"] > right_limit:
            continue
        geometric_items.append(item)

    candidates = [
        item
        for item in geometric_items
        if is_c2_session_title_candidate(str(item.get("text") or ""))
    ]
    candidate_ids = {id(item) for item in candidates}
    code_candidates = [
        item
        for item in candidates
        if extract_c2_remark_codes(item.get("text"))
    ]
    # A truncated real title can fail the text heuristic while the preview
    # below it contains a formal code.  Preserve the upper line as structural
    # evidence so the preview cannot become the row title.  Arbitrary rejected
    # preview text is not promoted into a standalone session candidate.
    for item in geometric_items:
        if id(item) in candidate_ids:
            continue
        text = str(item.get("text") or "").strip()
        if is_session_time_text(text) or re.fullmatch(r"\d{1,4}", text):
            continue
        if text in {"搜索", "新对话", "?", "？", "+", "..."}:
            continue
        center_y = float(item.get("center_y") or 0)
        if any(
            8.0 <= float(code_item.get("center_y") or 0) - center_y < min_session_row_gap
            and abs(float(code_item.get("left") or 0) - float(item.get("left") or 0)) <= 32.0
            for code_item in code_candidates
        ):
            candidates.append(item)

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
                "layout_snapshot_id": snapshot_id,
            }
        )
    enrich_sessions_with_sidebar_signals(
        sessions,
        ocr_items,
        image_size,
        screenshot=screenshot,
        min_header_y=min_header_y,
        split_x=split_x,
        session_list_bounds=session_bounds,
    )
    return sessions


def select_session_row_title_candidate(
    row_candidates: list[dict[str, Any]],
) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    ordered = sorted(
        row_candidates,
        key=lambda item: (
            float(item.get("center_y") or 0),
            float(item.get("top") or 0),
            float(item.get("left") or 0),
        ),
    )
    top_center_y = float(ordered[0].get("center_y") or 0)
    # Normal and enhanced OCR can place the same title baseline a few pixels
    # apart.  Keep that bounded alias tolerance, but never let the lower
    # preview line enter the title decision merely because it contains a code.
    title_line_candidates = [
        item
        for item in ordered
        if abs(float(item.get("center_y") or 0) - top_center_y) <= 6.0
    ]
    details: list[dict[str, Any]] = []
    distinct_codes: list[str] = []
    for item in title_line_candidates:
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
    session_list_bounds: list[int],
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
        row_bounds = [
            int(session_list_bounds[0]),
            int(row_top),
            int(session_list_bounds[2]),
            int(row_bottom),
        ]
        session["row_bounds"] = row_bounds
        session["click_bounds"] = row_bounds
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
    layout_snapshot: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    width, height = image_size
    snapshot = layout_snapshot or layout_snapshot_for_image(screenshot) or {}
    dynamic_regions = {
        name: snapshot.get(name)
        for name in win32_ocr_layout.REQUIRED_LAYOUT_REGION_NAMES
    }
    message_bounds = win32_ocr_layout.normalize_rect(snapshot.get("message_viewport_bounds"))
    input_bounds = win32_ocr_layout.normalize_rect(snapshot.get("input_bounds"))
    if not bool(snapshot.get("valid")) or message_bounds[2] <= message_bounds[0]:
        return []
    split_x = message_bounds[0]
    header_cutoff = message_bounds[1]
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
        if item["center_y"] > message_bounds[3]:
            continue
        if item["left"] < split_x - 5:
            continue
        if is_message_noise(text):
            continue
        side_details = classify_message_side_details(item, width=width, boundary_x=split_x)
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
            layout_snapshot=snapshot,
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
        in_dynamic_input = bool(
            int(input_bounds[0]) <= rect["left"] < int(input_bounds[2])
            and int(input_bounds[1]) <= (rect["top"] + rect["bottom"]) / 2 <= int(input_bounds[3])
        )
        if side != "self" and in_dynamic_input:
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


def parse_current_chat_frame_messages(
    ocr_items: list[dict[str, Any]],
    image_size: tuple[int, int],
    *,
    target: str,
    screenshot: Any | None,
) -> list[dict[str, Any]]:
    """Build one frame's message truth before any media action is allowed."""
    parsed_messages = parse_messages_from_ocr(
        ocr_items,
        image_size,
        target=target,
        screenshot=screenshot,
    )
    return merge_structural_image_messages(
        screenshot,
        ocr_items,
        parsed_messages,
        target=target,
    )


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


def classify_message_side_details(
    item: dict[str, Any],
    *,
    width: int,
    boundary_x: int | None = None,
) -> dict[str, Any]:
    if boundary_x is None:
        return {
            "side": "unknown",
            "confidence": 0.0,
            "evidence": ["dynamic_chat_boundary_missing"],
        }
    split_x = int(boundary_x)
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


def build_c2_window_context(hwnd: int, probe: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return the exact WeChat window selected by this Sidecar action."""

    selected = (
        (probe or {}).get("selected_main_window")
        if isinstance((probe or {}).get("selected_main_window"), dict)
        else {}
    )
    geometry = get_window_geometry(hwnd)
    return {
        "schema_version": 1,
        "hwnd": int(hwnd),
        "pid": int(selected.get("pid") or 0),
        "class_name": str(selected.get("class_name") or ""),
        "source": "sidecar_selected_main_window",
        "geometry": {
            key: int(geometry.get(key) or 0)
            for key in ("left", "top", "right", "bottom", "width", "height")
        },
    }


def validate_c2_window_context(context: dict[str, Any] | None) -> dict[str, Any]:
    """Validate one Sidecar-issued HWND without selecting another window."""

    value = context if isinstance(context, dict) else {}
    try:
        hwnd = int(value.get("hwnd") or 0)
    except (TypeError, ValueError):
        hwnd = 0
    if hwnd <= 0:
        return {"ok": False, "reason": "window_context_hwnd_missing"}
    try:
        if not bool(win32gui.IsWindow(hwnd)):
            return {"ok": False, "reason": "window_context_hwnd_invalid", "hwnd": hwnd}
        if not bool(win32gui.IsWindowVisible(hwnd)):
            return {
                "ok": False,
                "reason": "window_context_window_not_visible",
                "hwnd": hwnd,
            }
    except Exception as exc:
        return {
            "ok": False,
            "reason": "window_context_validation_failed",
            "hwnd": hwnd,
            "error_type": type(exc).__name__,
        }
    matching = next(
        (
            item
            for item in (probe_wechat_windows().get("windows") or [])
            if isinstance(item, dict) and int(item.get("hwnd") or 0) == hwnd
        ),
        None,
    )
    if not isinstance(matching, dict):
        return {"ok": False, "reason": "window_context_not_wechat", "hwnd": hwnd}
    expected_pid = int(value.get("pid") or 0)
    if expected_pid and int(matching.get("pid") or 0) != expected_pid:
        return {"ok": False, "reason": "window_context_pid_changed", "hwnd": hwnd}
    expected_class = str(value.get("class_name") or "").strip()
    if expected_class and str(matching.get("class_name") or "").strip() != expected_class:
        return {"ok": False, "reason": "window_context_class_changed", "hwnd": hwnd}
    return {
        "ok": True,
        "reason": "window_context_confirmed",
        "hwnd": hwnd,
        "pid": int(matching.get("pid") or 0),
        "class_name": str(matching.get("class_name") or ""),
    }


def capture_c2_window_context(
    context: dict[str, Any] | None,
    *,
    phase: str,
    label: str,
) -> dict[str, Any]:
    """Capture only the exact HWND selected by the current C2 Sidecar action."""

    validation = validate_c2_window_context(context)
    if validation.get("ok") is not True:
        return {
            "ok": False,
            "reason": "vision_window_context_invalid",
            "validation": validation,
        }
    hwnd = int(validation.get("hwnd") or 0)
    capture_mode = "wechat_window_exact_hwnd"
    screen_origin = [0, 0]
    try:
        window_rect = win32gui.GetWindowRect(hwnd)
        screen_origin = [int(window_rect[0]), int(window_rect[1])]
        try:
            image, _path = capture_wechat(
                hwnd,
                artifact_dir=None,
                label=label,
            )
        except Exception:
            time.sleep(0.12)
            image, _path = capture_wechat_window_visible_screen(
                hwnd,
                artifact_dir=None,
                label=f"{label}_retry",
            )
            capture_mode = "visible_screen_exact_hwnd_retry"
    except Exception as exc:
        return {
            "ok": False,
            "reason": (
                "capture_wechat_window_visible_screen_failed"
                if capture_mode.startswith("visible_screen")
                else "capture_wechat_failed"
            ),
            "error_type": type(exc).__name__,
            "capture_mode": capture_mode,
            "validation": validation,
        }
    return {
        "ok": True,
        "image": image,
        "hwnd": hwnd,
        "capture_mode": capture_mode,
        "screen_origin": screen_origin,
        "layout_snapshot": layout_snapshot_for_image(image),
        "validation": validation,
        "image_persisted": False,
    }


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
    def reject_unmapped_focus_click(_x: int, _y: int) -> None:
        raise RuntimeError(win32_ocr_layout.ERROR_COORDINATE_MAPPING_INVALID)

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
        # A focus fallback click has no screenshot-bound target. Refuse it so
        # every physical click remains behind the layout converter.
        focus_click_fallback_enabled=lambda: False,
        click=reject_unmapped_focus_click,
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


def client_click(
    hwnd: int,
    x: int,
    y: int,
    *,
    bounds: list[int] | None = None,
    expected_snapshot_id: str = "",
) -> None:
    """Stable API alias; coordinates are current screenshot coordinates only."""
    human_window_image_click(
        hwnd,
        int(x),
        int(y),
        bounds=list(bounds or []),
        expected_snapshot_id=expected_snapshot_id,
    )


def human_client_click(
    hwnd: int,
    x: int,
    y: int,
    *,
    bounds: list[int] | None = None,
    expected_snapshot_id: str = "",
) -> None:
    """Click a screenshot-space point through the single layout-bound path.

    The public name remains for the stable Sidecar API; it no longer accepts
    or converts an independent client-coordinate system.
    """
    human_window_image_click(
        hwnd,
        int(x),
        int(y),
        bounds=list(bounds or []),
        expected_snapshot_id=expected_snapshot_id,
    )


def human_window_image_hover(hwnd: int, x: int, y: int, *, expected_snapshot_id: str = "") -> dict[str, Any]:
    """Move the real cursor toward a screenshot-space point without clicking."""
    mapped, failure = _map_window_image_target(hwnd, int(x), int(y), expected_snapshot_id=expected_snapshot_id)
    if failure:
        return failure
    assert mapped is not None
    target_x, target_y = mapped["image_point"]
    screen_x, screen_y = mapped["screen_point"]
    require_active_ui_action_budget(
        "human_window_image_hover",
        metadata={
            "hwnd": int(hwnd or 0),
            "x": target_x,
            "y": target_y,
            "screen_x": screen_x,
            "screen_y": screen_y,
            "layout_snapshot_id": mapped.get("layout_snapshot_id"),
        },
    )
    activate_window(hwnd)
    ensure_left_button_released()
    try:
        result = human_screen_hover(screen_x, screen_y, action_name="human_window_image_hover")
        return {
            **result,
            "x": target_x,
            "y": target_y,
            "screen_x": screen_x,
            "screen_y": screen_y,
            "layout_snapshot_id": mapped.get("layout_snapshot_id"),
            "frame_id": mapped.get("frame_id"),
        }
    except Exception as exc:
        return {
            "ok": False,
            "x": target_x,
            "y": target_y,
            "screen_x": screen_x,
            "screen_y": screen_y,
            "error": repr(exc),
            "layout_snapshot_id": mapped.get("layout_snapshot_id"),
        }


def human_window_image_click(
    hwnd: int,
    x: int,
    y: int,
    *,
    bounds: list[int] | None = None,
    expected_snapshot_id: str = "",
) -> None:
    """Click a point measured in the same coordinate space as screenshots."""
    if not isinstance(bounds, list) or len(bounds) < 4:
        raise RuntimeError(f"{win32_ocr_layout.ERROR_COORDINATE_MAPPING_INVALID}:target_bounds_missing")
    mapped, failure = _map_window_image_target(
        hwnd,
        int(x),
        int(y),
        bounds=list(bounds),
        expected_snapshot_id=expected_snapshot_id,
    )
    if failure:
        raise RuntimeError(f"{failure.get('error_code')}: {failure.get('reason')}")
    assert mapped is not None
    click_x, click_y = mapped["image_point"]
    screen_x, screen_y = mapped["screen_point"]
    require_active_ui_action_budget(
        "human_window_image_click",
        metadata={
            "hwnd": int(hwnd or 0),
            "x": click_x,
            "y": click_y,
            "screen_x": screen_x,
            "screen_y": screen_y,
            "layout_snapshot_id": mapped.get("layout_snapshot_id"),
        },
    )
    invalidate_layout_snapshot(hwnd, reason="physical_click_started")
    activate_window(hwnd)
    ensure_left_button_released()
    result = human_screen_click_in_bounds(
        screen_x,
        screen_y,
        bounds=list(mapped["screen_bounds"]),
        action_name="human_window_image_click",
    )
    if not result.get("ok"):
        raise RuntimeError(str(result.get("error") or "bounded_screen_click_failed"))


def human_window_image_click_in_bounds(
    hwnd: int,
    x: int,
    y: int,
    *,
    bounds: list[int],
    action_name: str = "human_window_image_click_in_bounds",
    expected_snapshot_id: str = "",
) -> dict[str, Any]:
    """Click a screenshot-space point, clamped to a known safe window rectangle."""
    mapped, failure = _map_window_image_target(
        hwnd,
        int(x),
        int(y),
        bounds=list(bounds or []),
        expected_snapshot_id=expected_snapshot_id,
    )
    if failure:
        return failure
    assert mapped is not None
    click_x, click_y = mapped["image_point"]
    screen_x, screen_y = mapped["screen_point"]
    require_active_ui_action_budget(
        action_name,
        metadata={
            "hwnd": int(hwnd or 0),
            "x": click_x,
            "y": click_y,
            "screen_x": screen_x,
            "screen_y": screen_y,
            "bounds": bounds,
            "layout_snapshot_id": mapped.get("layout_snapshot_id"),
        },
    )
    invalidate_layout_snapshot(hwnd, reason="physical_click_started")
    activate_window(hwnd)
    ensure_left_button_released()
    try:
        result = human_screen_click_in_bounds(
            screen_x,
            screen_y,
            bounds=list(mapped["screen_bounds"]),
            action_name=action_name,
        )
        return {
            **result,
            "x": click_x,
            "y": click_y,
            "screen_x": screen_x,
            "screen_y": screen_y,
            "raw_x": int(x),
            "raw_y": int(y),
            "bounds": bounds,
            "image_bounds": mapped.get("image_bounds"),
            "layout_snapshot_id": mapped.get("layout_snapshot_id"),
            "frame_id": mapped.get("frame_id"),
        }
    except Exception as exc:
        return {
            "ok": False,
            "x": click_x,
            "y": click_y,
            "screen_x": screen_x,
            "screen_y": screen_y,
            "bounds": bounds,
            "error": repr(exc),
            "layout_snapshot_id": mapped.get("layout_snapshot_id"),
        }
    finally:
        ensure_left_button_released()


def human_window_image_right_click_in_bounds(
    hwnd: int,
    x: int,
    y: int,
    *,
    bounds: list[int],
    action_name: str = "human_window_image_right_click_in_bounds",
    expected_snapshot_id: str = "",
) -> dict[str, Any]:
    """Right-click a screenshot-space point, clamped to a known safe window rectangle."""
    mapped, failure = _map_window_image_target(
        hwnd,
        int(x),
        int(y),
        bounds=list(bounds or []),
        expected_snapshot_id=expected_snapshot_id,
    )
    if failure:
        return failure
    assert mapped is not None
    click_x, click_y = mapped["image_point"]
    screen_x, screen_y = mapped["screen_point"]
    require_active_ui_action_budget(
        action_name,
        metadata={
            "hwnd": int(hwnd or 0),
            "x": click_x,
            "y": click_y,
            "screen_x": screen_x,
            "screen_y": screen_y,
            "bounds": bounds,
            "layout_snapshot_id": mapped.get("layout_snapshot_id"),
        },
    )
    invalidate_layout_snapshot(hwnd, reason="physical_right_click_started")
    activate_window(hwnd)
    ensure_left_button_released()
    right_down_sent = False
    try:
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
            "raw_x": int(x),
            "raw_y": int(y),
            "bounds": bounds,
            "steps": steps,
            "layout_snapshot_id": mapped.get("layout_snapshot_id"),
            "frame_id": mapped.get("frame_id"),
        }
    except Exception as exc:
        return {
            "ok": False,
            "x": click_x,
            "y": click_y,
            "bounds": bounds,
            "error": repr(exc),
            "layout_snapshot_id": mapped.get("layout_snapshot_id"),
        }
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


def hotkey(modifier: int, key: int) -> None:
    invalidate_all_layout_snapshots(reason="keyboard_input_started")
    coordinate_rpa_action("hotkey", metadata={"modifier": int(modifier), "key": int(key)})
    win32api.keybd_event(modifier, 0, 0, 0)
    humanized_action_sleep(16, 42)
    win32api.keybd_event(key, 0, 0, 0)
    humanized_action_sleep(18, 48)
    win32api.keybd_event(key, 0, win32con.KEYEVENTF_KEYUP, 0)
    win32api.keybd_event(modifier, 0, win32con.KEYEVENTF_KEYUP, 0)
    humanized_action_sleep(8, 28)


def key_press(key: int) -> None:
    invalidate_all_layout_snapshots(reason="keyboard_input_started")
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
    if not enter_item:
        return {
            "attempted": False,
            "detected": True,
            "auto_enter_enabled": True,
            "geometry": geometry,
            "screenshot_path": path,
            "reason": "WECHAT_UI_LAYOUT_UNRESOLVED",
            "error_code": win32_ocr_layout.ERROR_LAYOUT_UNRESOLVED,
        }
    click_x = int(float(enter_item.get("center_x") or 0))
    click_y = int(float(enter_item.get("center_y") or 0))
    enter_bounds = [
        int(float(enter_item.get("left") or click_x)),
        int(float(enter_item.get("top") or click_y)),
        int(float(enter_item.get("right") or click_x)),
        int(float(enter_item.get("bottom") or click_y)),
    ]
    human_client_click(
        hwnd,
        click_x,
        click_y,
        bounds=enter_bounds,
        expected_snapshot_id=str(
            (layout_snapshot_metadata(hwnd).get("snapshot") or {}).get("layout_snapshot_id") or ""
        ),
    )
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
def screen_work_area(hwnd: int = 0) -> dict[str, int]:
    """Return the usable desktop work area, excluding taskbar and reserved edges."""
    try:
        user32 = ctypes.windll.user32
        if int(hwnd or 0) > 0 and hasattr(user32, "MonitorFromWindow") and hasattr(user32, "GetMonitorInfoW"):
            monitor_default_to_nearest = 2
            monitor = user32.MonitorFromWindow(int(hwnd), monitor_default_to_nearest)
            if monitor:
                class MONITORINFO(ctypes.Structure):
                    _fields_ = [
                        ("cbSize", wintypes.DWORD),
                        ("rcMonitor", wintypes.RECT),
                        ("rcWork", wintypes.RECT),
                        ("dwFlags", wintypes.DWORD),
                    ]

                info = MONITORINFO()
                info.cbSize = ctypes.sizeof(MONITORINFO)
                if user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
                    return {
                        "left": int(info.rcWork.left),
                        "top": int(info.rcWork.top),
                        "right": int(info.rcWork.right),
                        "bottom": int(info.rcWork.bottom),
                        "width": max(0, int(info.rcWork.right - info.rcWork.left)),
                        "height": max(0, int(info.rcWork.bottom - info.rcWork.top)),
                        "source": "MonitorFromWindow_GetMonitorInfoW",
                    }
        work_area = wintypes.RECT()
        spi_get_work_area = 0x0030
        if hasattr(user32, "SystemParametersInfoW") and user32.SystemParametersInfoW(
            spi_get_work_area,
            0,
            ctypes.byref(work_area),
            0,
        ):
            return {
                "left": int(work_area.left),
                "top": int(work_area.top),
                "right": int(work_area.right),
                "bottom": int(work_area.bottom),
                "width": max(0, int(work_area.right - work_area.left)),
                "height": max(0, int(work_area.bottom - work_area.top)),
                "source": "SystemParametersInfoW_SPI_GETWORKAREA",
            }
    except Exception:
        pass
    try:
        user32 = ctypes.windll.user32
        width = int(user32.GetSystemMetrics(0) or 0)
        height = int(user32.GetSystemMetrics(1) or 0)
    except Exception:
        width = 0
        height = 0
    return {
        "left": 0,
        "top": 0,
        "right": max(0, width),
        "bottom": max(0, height),
        "width": max(0, width),
        "height": max(0, height),
        "source": "GetSystemMetrics_fallback",
    }


def normalize_wechat_window(hwnd: int, *, allow_move: bool = True) -> dict[str, Any]:
    before = get_window_geometry(hwnd)
    before_client = get_window_client_geometry(hwnd)
    dpi_scale = window_dpi_scale(hwnd)

    enforce_recommended = env_flag("WECHAT_WIN32_OCR_ENFORCE_RECOMMENDED_WINDOW", default=True)
    fixed_origin = env_flag("WECHAT_WIN32_OCR_WINDOW_FIXED_ORIGIN", default=True)
    if not fixed_origin:
        return {
            "ok": False,
            "enabled": True,
            "applied": False,
            "before": before,
            "before_client": before_client,
            "error_code": win32_ocr_layout.ERROR_WINDOW_NORMALIZATION_FAILED,
            "reason": "fixed_origin_policy_disabled",
        }
    # Normalization can move or resize the window. Any frame captured before
    # this gate is therefore unusable even when the planner later decides no
    # physical MoveWindow call is necessary.
    invalidate_layout_snapshot(hwnd, reason="window_normalization_started")
    try:
        is_maximized = bool(win32gui.IsZoomed(hwnd))
    except Exception:
        is_maximized = False
    if is_maximized:
        if not allow_move:
            return {
                "ok": False,
                "enabled": True,
                "applied": False,
                "before": before,
                "dpi_scale": dpi_scale,
                "fixed_origin": fixed_origin,
                "error_code": win32_ocr_layout.ERROR_LAYOUT_STALE,
                "reason": "window_maximized_during_active_flow",
            }
        try:
            win32gui.ShowWindow(hwnd, getattr(win32con, "SW_RESTORE", 9))
            humanized_action_sleep(90, 180)
            before = get_window_geometry(hwnd)
        except Exception as exc:
            return {
                "ok": False,
                "enabled": True,
                "applied": False,
                "before": before,
                "dpi_scale": dpi_scale,
                "fixed_origin": fixed_origin,
                "error": repr(exc),
                "error_code": win32_ocr_layout.ERROR_WINDOW_NORMALIZATION_FAILED,
                "reason": "restore_from_maximized_failed",
            }
    work_area = screen_work_area(hwnd)
    screen_width = int(work_area.get("width") or 0)
    screen_height = int(work_area.get("height") or 0)
    screen_metrics_available = screen_width > 0 and screen_height > 0
    if not screen_metrics_available:
        return {
            "ok": False,
            "enabled": True,
            "applied": False,
            "before": before,
            "dpi_scale": dpi_scale,
            "fixed_origin": fixed_origin,
            "work_area": work_area,
            "error_code": win32_ocr_layout.ERROR_WINDOW_NORMALIZATION_FAILED,
            "reason": "screen_work_area_unavailable",
        }

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
        screen_left=int(work_area.get("left") or 0),
        screen_top=int(work_area.get("top") or 0),
    )
    left = int(plan.get("left") or 0)
    top = int(plan.get("top") or 0)
    safe_width = int(plan.get("width") or 0)
    safe_height = int(plan.get("height") or 0)
    effective_target = dict(plan.get("target") or {})
    requested_target = dict(plan.get("requested_target") or {})
    recommended_floor_applied = bool(plan.get("recommended_floor_applied"))
    resolution_scale = float(plan.get("resolution_scale") or 1.0)

    def verify_normalized_geometry(
        after_geometry: dict[str, Any],
        after_client_geometry: dict[str, Any],
        after_dpi_scale: float,
    ) -> tuple[bool, str]:
        geometry_matches = (
            abs(int(after_geometry.get("left") or 0) - left) <= 6
            and abs(int(after_geometry.get("top") or 0) - top) <= 6
            and abs(int(after_geometry.get("width") or 0) - safe_width) <= 6
            and abs(int(after_geometry.get("height") or 0) - safe_height) <= 6
        )
        if not geometry_matches:
            return False, "window_geometry_did_not_match_normalization_target"
        if abs(float(after_dpi_scale or 0.0) - float(dpi_scale or 0.0)) > 0.01:
            return False, "window_dpi_changed_during_normalization"
        client_width = int(after_client_geometry.get("width") or 0)
        client_height = int(after_client_geometry.get("height") or 0)
        if client_width <= 0 or client_height <= 0:
            return False, "window_client_geometry_unavailable_after_normalization"
        return True, "normalized_geometry_verified"

    if not bool(plan.get("ok")):
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
            "screen": {"width": screen_width, "height": screen_height},
            "work_area": work_area,
            "error_code": win32_ocr_layout.ERROR_WINDOW_NORMALIZATION_FAILED,
            "reason": str(plan.get("reason") or "window_normalization_plan_failed"),
        }
    if bool(plan.get("move")) and not allow_move:
        return {
            "ok": False,
            "enabled": True,
            "applied": False,
            "before": before,
            "before_client": before_client,
            "target": effective_target,
            "requested_target": requested_target,
            "dpi_scale": dpi_scale,
            "resolution_scale": resolution_scale,
            "fixed_origin": fixed_origin,
            "screen": {"width": screen_width, "height": screen_height},
            "work_area": work_area,
            "error_code": win32_ocr_layout.ERROR_LAYOUT_STALE,
            "reason": "window_geometry_changed_during_active_flow",
        }
    if not bool(plan.get("move")):
        after = get_window_geometry(hwnd)
        after_client = get_window_client_geometry(hwnd)
        after_dpi_scale = window_dpi_scale(hwnd)
        matches_plan, verification_reason = verify_normalized_geometry(
            after,
            after_client,
            after_dpi_scale,
        )
        if not matches_plan:
            return {
                "ok": False,
                "enabled": True,
                "applied": False,
                "before": before,
                "before_client": before_client,
                "after": after,
                "after_client": after_client,
                "after_dpi_scale": after_dpi_scale,
                "target": effective_target,
                "requested_target": requested_target,
                "dpi_scale": dpi_scale,
                "resolution_scale": resolution_scale,
                "fixed_origin": fixed_origin,
                "screen": {"width": screen_width, "height": screen_height},
                "work_area": work_area,
                "error_code": win32_ocr_layout.ERROR_WINDOW_NORMALIZATION_FAILED,
                "reason": verification_reason,
            }
        return {
            "ok": True,
            "enabled": True,
            "applied": False,
            "before": before,
            "before_client": before_client,
            "after": after,
            "after_client": after_client,
            "after_dpi_scale": after_dpi_scale,
            "target": effective_target,
            "requested_target": requested_target,
            "dpi_scale": dpi_scale,
            "resolution_scale": resolution_scale,
            "enforce_recommended": enforce_recommended,
            "recommended_floor_applied": recommended_floor_applied,
            "fixed_origin": fixed_origin,
            "screen": {"width": screen_width, "height": screen_height},
            "work_area": work_area,
            "reason": "already_near_target",
        }

    try:
        win32gui.MoveWindow(hwnd, left, top, safe_width, safe_height, True)
        humanized_action_sleep(90, 180)
        after = get_window_geometry(hwnd)
        after_client = get_window_client_geometry(hwnd)
        after_dpi_scale = window_dpi_scale(hwnd)
        applied = (
            abs(int(after.get("width") or 0) - int(before.get("width") or 0)) > 4
            or abs(int(after.get("height") or 0) - int(before.get("height") or 0)) > 4
            or abs(int(after.get("left") or 0) - int(before.get("left") or 0)) > 4
            or abs(int(after.get("top") or 0) - int(before.get("top") or 0)) > 4
        )
        matches_plan, verification_reason = verify_normalized_geometry(
            after,
            after_client,
            after_dpi_scale,
        )
        if not matches_plan:
            return {
                "ok": False,
                "enabled": True,
                "applied": applied,
                "before": before,
                "before_client": before_client,
                "after": after,
                "after_client": after_client,
                "after_dpi_scale": after_dpi_scale,
                "target": effective_target,
                "requested_target": requested_target,
                "dpi_scale": dpi_scale,
                "resolution_scale": resolution_scale,
                "enforce_recommended": enforce_recommended,
                "recommended_floor_applied": recommended_floor_applied,
                "fixed_origin": fixed_origin,
                "screen": {"width": screen_width, "height": screen_height},
                "work_area": work_area,
                "error_code": win32_ocr_layout.ERROR_WINDOW_NORMALIZATION_FAILED,
                "reason": verification_reason,
            }
        return {
            "ok": True,
            "enabled": True,
            "applied": applied,
            "before": before,
            "before_client": before_client,
            "after": after,
            "after_client": after_client,
            "after_dpi_scale": after_dpi_scale,
            "target": effective_target,
            "requested_target": requested_target,
            "dpi_scale": dpi_scale,
            "resolution_scale": resolution_scale,
            "enforce_recommended": enforce_recommended,
            "recommended_floor_applied": recommended_floor_applied,
            "fixed_origin": fixed_origin,
            "screen": {"width": screen_width, "height": screen_height},
            "work_area": work_area,
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
            "error_code": win32_ocr_layout.ERROR_WINDOW_NORMALIZATION_FAILED,
            "reason": "normalize_failed",
        }


def is_session_name_candidate(text: str) -> bool:
    return win32_ocr_text.is_session_name_candidate(text)


def is_c2_session_title_candidate(text: Any) -> bool:
    return win32_ocr_text.is_c2_session_title_candidate(text)


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
    scan_id = str(request.get("scan_id") or "").strip()
    if scan_id:
        argv.extend(["--scan-id", scan_id])
    for key, flag in (
        ("canonical_voice_action_id", "--canonical-voice-action-id"),
        ("reserved_worker_stable_id", "--reserved-worker-stable-id"),
        ("voice_action_stage", "--voice-action-stage"),
        ("pre_frame_id", "--pre-frame-id"),
        ("selected_pre_observation_id", "--selected-pre-observation-id"),
        ("selected_action_token", "--selected-action-token"),
        ("selected_target_fingerprint", "--selected-target-fingerprint"),
    ):
        value = str(request.get(key) or "").strip()
        if action == "voice-transcribe" and value:
            argv.extend([flag, value])
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
    if action in {"messages", "open-chat", "voice-transcribe"}:
        expected_confirmed_self_text = str(
            request.get("expected_confirmed_self_text") or ""
        )
        if expected_confirmed_self_text:
            argv.extend(
                [
                    "--expected-confirmed-self-text",
                    expected_confirmed_self_text,
                ]
            )
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
    parser.add_argument("--scan-id", default="", help="Correlation id for one sessions scan.")
    parser.add_argument(
        "--window-policy",
        choices=("normalize", "verify"),
        default="normalize",
        help="Normalize only at a UI Flow boundary; nested actions must use verify.",
    )
    parser.add_argument("--canonical-voice-action-id", default="")
    parser.add_argument("--reserved-worker-stable-id", default="")
    parser.add_argument("--voice-action-stage", choices=("prepare", "execute"), default="prepare")
    parser.add_argument("--pre-frame-id", default="")
    parser.add_argument("--selected-pre-observation-id", default="")
    parser.add_argument("--selected-action-token", default="")
    parser.add_argument("--selected-target-fingerprint", default="")
    parser.add_argument("--target", help="Chat name for messages/send.")
    parser.add_argument("--session-key", default="", help="Internal session key for row-level RPA targeting.")
    parser.add_argument("--target-mode", default="", help="Targeting mode for messages, e.g. search_by_remark_code.")
    parser.add_argument(
        "--expected-confirmed-self-text",
        default="",
        help=(
            "Locally confirmed AI reply text used only to recover an OCR-missed "
            "self text bubble during the next authorized read."
        ),
    )
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
