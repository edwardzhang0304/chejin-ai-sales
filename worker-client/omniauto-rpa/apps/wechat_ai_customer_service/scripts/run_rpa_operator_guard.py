"""Global operator guard for WeChat RPA with configurable pause/stop hotkeys.

This process runs alongside ``run_customer_service_listener.py`` and provides:
1. Input lock in running mode (blocks manual keyboard/mouse).
2. Hotkey single press command (pause/resume toggle).
3. Hotkey double press command (stop).
4. A topmost floating status indicator.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import math
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from ctypes import wintypes
from datetime import datetime
from pathlib import Path
from typing import Any

import psutil

try:
    import tkinter as tk
except Exception:  # pragma: no cover - optional in headless/server contexts.
    tk = None  # type: ignore[assignment]

try:
    from PIL import Image, ImageDraw, ImageFont, ImageTk
except Exception:  # pragma: no cover - Pillow is optional for headless guard mode.
    Image = None  # type: ignore[assignment]
    ImageDraw = None  # type: ignore[assignment]
    ImageFont = None  # type: ignore[assignment]
    ImageTk = None  # type: ignore[assignment]


WH_KEYBOARD_LL = 13
WH_MOUSE_LL = 14
HC_ACTION = 0
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYDOWN = 0x0104
WM_SYSKEYUP = 0x0105
VK_ESCAPE = 0x1B
VK_F8 = 0x77
WM_QUIT = 0x0012
PM_REMOVE = 0x0001
SW_SHOWNOACTIVATE = 4
WS_POPUP = 0x80000000
WS_EX_LAYERED = 0x00080000
WS_EX_NOACTIVATE = 0x08000000
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_TOPMOST = 0x00000008
WS_EX_TRANSPARENT = 0x00000020
ULW_ALPHA = 0x00000002
AC_SRC_ALPHA = 0x01
AC_SRC_OVER = 0x00
BI_RGB = 0
LLKHF_LOWER_IL_INJECTED = 0x02
LLKHF_INJECTED = 0x10
LLMHF_INJECTED = 0x01
LLMHF_LOWER_IL_INJECTED = 0x02


CONTROL_MODES = {"running", "paused", "stopped"}
COMMAND_ACTIONS = {"pause", "resume", "stop"}
CONTROL_KEY_CHOICES = {"f8": VK_F8, "esc": VK_ESCAPE}
DEFAULT_CONTROL_KEY = "f8"
DEFAULT_INDICATOR_BACKEND = "layered"
DEFAULT_LOCAL_SAFETY_STOP_PATH = "/api/customer-service/runtime/stop"
INDICATOR_THEMES = ("blue", "yellow", "red")
INDICATOR_PALETTES: dict[str, dict[str, str]] = {
    "blue": {
        "label_bg": "#031523",
        "label_outline": "#2dd4ff",
        "label_accent": "#38bdf8",
        "state": "#f4feff",
        "key": "#a7f4ff",
        "shadow": "#00121c",
        "core": "#38bdf8",
        "ring": "#bae6fd",
    },
    "yellow": {
        "label_bg": "#1d1402",
        "label_outline": "#facc15",
        "label_accent": "#f59e0b",
        "state": "#fff8dd",
        "key": "#ffe4a3",
        "shadow": "#171000",
        "core": "#facc15",
        "ring": "#fef3c7",
    },
    "red": {
        "label_bg": "#19070a",
        "label_outline": "#fb7185",
        "label_accent": "#f43f5e",
        "state": "#fff2f5",
        "key": "#ffc0ca",
        "shadow": "#160006",
        "core": "#fb7185",
        "ring": "#fecdd3",
    },
}

if ctypes.sizeof(ctypes.c_void_p) == 8:
    LRESULT = ctypes.c_longlong
    ULONG_PTR_T = ctypes.c_ulonglong
else:
    LRESULT = ctypes.c_long
    ULONG_PTR_T = ctypes.c_ulong

if hasattr(wintypes, "ULONG_PTR"):
    ULONG_PTR_T = wintypes.ULONG_PTR


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode", wintypes.DWORD),
        ("scanCode", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR_T),
    ]


class POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class SIZE(ctypes.Structure):
    _fields_ = [("cx", wintypes.LONG), ("cy", wintypes.LONG)]


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [
        ("BlendOp", ctypes.c_byte),
        ("BlendFlags", ctypes.c_byte),
        ("SourceConstantAlpha", ctypes.c_byte),
        ("AlphaFormat", ctypes.c_byte),
    ]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [
        ("bmiHeader", BITMAPINFOHEADER),
        ("bmiColors", wintypes.DWORD * 3),
    ]


class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("pt", wintypes.POINT),
        ("mouseData", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR_T),
    ]


class MSG(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("message", wintypes.UINT),
        ("wParam", wintypes.WPARAM),
        ("lParam", wintypes.LPARAM),
        ("time", wintypes.DWORD),
        ("pt", wintypes.POINT),
        ("lPrivate", wintypes.DWORD),
    ]


LowLevelKeyboardProc = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
LowLevelMouseProc = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def bounded_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def hex_to_rgb(value: str) -> tuple[int, int, int]:
    text = str(value or "").strip().lstrip("#")
    if len(text) != 6:
        return (255, 255, 255)
    try:
        return (int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16))
    except ValueError:
        return (255, 255, 255)


def enable_process_dpi_awareness() -> None:
    if os.name != "nt":
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return
    except Exception:
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


def read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


TRANSIENT_WRITE_WINERRORS = {5, 32, 33}


def transient_write_error(exc: OSError) -> bool:
    winerror = getattr(exc, "winerror", None)
    if winerror in TRANSIENT_WRITE_WINERRORS:
        return True
    errno_value = getattr(exc, "errno", None)
    return errno_value in {13, 16, 32}


def write_json(path: Path, payload: dict[str, Any]) -> None:
    output = dict(payload)
    output["updated_at"] = now_iso()
    text = json.dumps(output, ensure_ascii=True, indent=2)
    path.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(10):
        temp = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
        try:
            temp.write_text(text, encoding="utf-8")
            os.replace(temp, path)
            return
        except OSError as exc:
            try:
                temp.unlink()
            except OSError:
                pass
            if not transient_write_error(exc) or attempt >= 9:
                raise
            time.sleep(0.05 * (attempt + 1))


def write_guard_state(path: Path | None, payload: dict[str, Any]) -> None:
    if path is None:
        return
    try:
        write_json(path, payload)
    except Exception:
        pass


def normalize_control_payload(payload: dict[str, Any], *, tenant_id: str) -> dict[str, Any]:
    mode = str(payload.get("mode") or "running").strip().lower()
    if mode not in CONTROL_MODES:
        mode = "running"
    command = payload.get("command") if isinstance(payload.get("command"), dict) else {}
    try:
        command_id = max(0, int(command.get("id") or 0))
    except (TypeError, ValueError):
        command_id = 0
    action = str(command.get("action") or "none").strip().lower()
    if action not in COMMAND_ACTIONS and action != "none":
        action = "none"
    status = str(command.get("status") or "idle").strip().lower()
    if status not in {"idle", "pending", "applied", "ignored", "rejected"}:
        status = "idle"
    return {
        "version": 1,
        "tenant_id": tenant_id,
        "mode": mode,
        "command": {
            "id": command_id,
            "action": action,
            "status": status,
            "source": str(command.get("source") or ""),
            "requested_at": str(command.get("requested_at") or ""),
            "applied_at": str(command.get("applied_at") or ""),
            "message": str(command.get("message") or ""),
        },
        "updated_at": str(payload.get("updated_at") or now_iso()),
    }


def load_control_payload(path: Path, *, tenant_id: str) -> dict[str, Any]:
    return normalize_control_payload(read_json(path), tenant_id=tenant_id)


def issue_command(path: Path, *, tenant_id: str, action: str, source: str, message: str) -> dict[str, Any]:
    payload = load_control_payload(path, tenant_id=tenant_id)
    command = payload.get("command", {})
    pending = str(command.get("status") or "").strip().lower() == "pending"
    current_action = str(command.get("action") or "").strip().lower()
    if pending and current_action == action:
        return payload
    next_id = max(0, int(command.get("id") or 0)) + 1
    payload["command"] = {
        "id": next_id,
        "action": action,
        "status": "pending",
        "source": source,
        "requested_at": now_iso(),
        "applied_at": "",
        "message": message,
    }
    write_json(path, payload)
    return payload


def load_runtime_status(path: Path) -> dict[str, str]:
    payload = read_json(path)
    state = str(payload.get("state") or "").strip().lower() or "stopped"
    message = str(payload.get("message") or "").strip()
    return {"state": state, "message": message}


def write_runtime_status_hint(path: Path, *, tenant_id: str, state: str, message: str) -> None:
    """Keep the web console visually close to the desktop hotkey state."""
    try:
        payload = read_json(path)
        payload.update(
            {
                "ok": True,
                "state": state if state in {"idle", "thinking", "paused", "stopped"} else "idle",
                "message": message,
                "tenant_id": tenant_id,
            }
        )
        write_json(path, payload)
    except Exception:
        pass


def normalize_local_safety_stop_path(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return DEFAULT_LOCAL_SAFETY_STOP_PATH
    if not raw.startswith("/"):
        raw = f"/{raw}"
    if not raw.startswith("/api/") or not raw.endswith("/runtime/stop"):
        return DEFAULT_LOCAL_SAFETY_STOP_PATH
    return raw


def request_local_safety_stop(
    *,
    tenant_id: str,
    stop_path: str = DEFAULT_LOCAL_SAFETY_STOP_PATH,
    timeout_seconds: float = 1.5,
) -> bool:
    """Ask the local admin backend to run the same full stop path as the web UI."""

    safe_path = normalize_local_safety_stop_path(stop_path)
    url = f"http://127.0.0.1:8765{safe_path}?tenant_id={urllib.parse.quote(tenant_id)}"
    request = urllib.request.Request(
        url,
        data=b"{}",
        headers={"Content-Type": "application/json", "X-Tenant-ID": tenant_id},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds):
            return True
    except (OSError, urllib.error.URLError, urllib.error.HTTPError, TimeoutError):
        return False


def indicator_state_snapshot(*, mode: str, runtime_state: str, locked: bool) -> tuple[str, str, dict[str, str]]:
    """Return the compact three-color desktop indicator state."""
    mode_value = str(mode or "running").strip().lower()
    runtime_value = str(runtime_state or "idle").strip().lower()
    if mode_value == "stopped" or runtime_value == "stopped":
        return "red", "已停止 · 键鼠释放", INDICATOR_PALETTES["red"]
    if mode_value == "paused" or runtime_value == "paused":
        return "yellow", "已暂停 · 等待继续", INDICATOR_PALETTES["yellow"]
    if locked:
        return "blue", "运行中 · 键鼠锁定", INDICATOR_PALETTES["blue"]
    return "blue", "运行中 · 可操作", INDICATOR_PALETTES["blue"]


def process_alive(pid: int) -> bool:
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
    except psutil.NoSuchProcess:
        return False


def normalize_indicator_backend(value: Any) -> str:
    backend = str(value or DEFAULT_INDICATOR_BACKEND).strip().lower()
    if backend not in {"auto", "layered", "tk"}:
        backend = DEFAULT_INDICATOR_BACKEND
    return backend


class FloatingIndicator:
    def __init__(self, *, enabled: bool, tenant_id: str, control_key_label: str) -> None:
        self.requested_enabled = bool(enabled)
        self.enabled = bool(enabled and (tk is not None or (os.name == "nt" and Image is not None and ImageDraw is not None)))
        self.tenant_id = tenant_id
        self.control_key_label = str(control_key_label or DEFAULT_CONTROL_KEY).upper()
        self.backend = ""
        self.root: Any = None
        self.canvas: Any = None
        self.hwnd: int = 0
        self.items: dict[str, int] = {}
        self.ball_size = 72
        self.canvas_width = 136
        self.canvas_height = 114
        self.orb_draw_size = 70
        self.init_error = ""
        self.fallback_reason = ""
        self._last_snapshot: tuple[str, str, str, bool] | None = None
        self._orb_frames: dict[str, list[Any]] = {}
        self._active_orb_theme = "blue"
        self._orb_frame_count = 36
        self._frame_index = 0
        self._last_frame_at = 0.0
        self._layered_state_text = "运行中 · 键鼠锁定"
        self._layered_palette = INDICATOR_PALETTES["blue"]
        self._layered_font_cache: dict[tuple[int, bool], Any] = {}
        self._last_render_ok = False
        self._last_render_error = ""
        if self.requested_enabled and not self.enabled:
            self.init_error = "floating_indicator_backend_unavailable"
        if not self.enabled:
            return
        self._init_ui()

    def _init_ui(self) -> None:
        # Prefer the anti-aliased layered window for the polished desktop
        # indicator. Tk remains a compatibility fallback when layered rendering
        # is unavailable or explicitly requested.
        backend_preference = normalize_indicator_backend(os.getenv("WECHAT_RPA_INDICATOR_BACKEND"))
        if backend_preference != "tk" and os.name == "nt" and Image is not None and ImageDraw is not None:
            try:
                self._init_layered_ui()
                return
            except Exception as exc:
                self.backend = ""
                self.hwnd = 0
                self.root = None
                self.fallback_reason = f"layered_window_failed:{repr(exc)}"
                self.init_error = self.fallback_reason
        if tk is None:
            self.enabled = False
            if not self.init_error:
                self.init_error = "tkinter_unavailable"
            return
        try:
            self.backend = "tk"
            root = tk.Tk()
            root.title(f"OmniAuto RPA Indicator {self.tenant_id}")
            root.overrideredirect(True)
            root.attributes("-topmost", True)
            background_key = "#101015"
            root.configure(bg=background_key)
            try:
                # Keep the small indicator readable while the transparent
                # background prevents it from blocking the desktop.
                root.attributes("-alpha", 0.9)
            except Exception:
                pass
            try:
                # Makes the non-ball background fully transparent on Windows.
                root.attributes("-transparentcolor", background_key)
            except Exception:
                pass

            orb_size = int(self.ball_size)
            width = int(self.canvas_width)
            height = int(self.canvas_height)
            cx = width // 2
            cy = orb_size // 2 + 3
            canvas = tk.Canvas(
                root,
                width=width,
                height=height,
                bg=background_key,
                highlightthickness=0,
                bd=0,
                relief="flat",
            )
            canvas.pack(fill="both", expand=True)

            self._orb_frames = self._build_orb_frames()
            self.items["orb_shadow"] = canvas.create_oval(cx - 30, cy - 26, cx + 30, cy + 34, fill="#00121c", outline="")
            if self._orb_frames:
                self.items["orb"] = canvas.create_image(cx, cy, image=self._orb_frames["blue"][0])
            else:
                self.items["orb_fallback_outer"] = canvas.create_oval(cx - 30, cy - 30, cx + 30, cy + 30, fill="#06243a", outline="#5eeaff", width=2)
                self.items["orb_fallback_inner"] = canvas.create_oval(cx - 18, cy - 18, cx + 18, cy + 18, fill="#a7fff7", outline="#ffffff", width=1)

            label_top = orb_size + 1
            label_bottom = height - 4
            self.items["label_shadow"] = self._create_round_rect(
                canvas,
                14,
                label_top + 2,
                width - 14,
                label_bottom + 2,
                8,
                fill="#000a12",
                outline="",
            )
            self.items["label_bg"] = self._create_round_rect(
                canvas,
                12,
                label_top,
                width - 12,
                label_bottom,
                8,
                fill="#04121d",
                outline="",
            )
            self.items["label_glint"] = canvas.create_line(
                22,
                label_top + 3,
                width - 22,
                label_top + 3,
                fill="#264e62",
                width=1,
            )
            self.items["label_accent"] = self._create_round_rect(
                canvas,
                13,
                label_top + 5,
                17,
                label_bottom - 5,
                3,
                fill="#32e6ff",
                outline="",
            )
            self.items["state_label"] = canvas.create_text(
                cx,
                label_top + 11,
                text="运行中 · 键鼠锁定",
                fill="#f4feff",
                font=("Microsoft YaHei UI", 8, "bold"),
            )
            self.items["key"] = canvas.create_text(
                cx,
                label_top + 27,
                text=f"{self.control_key_label} 暂停/继续",
                fill="#9eeeff",
                font=("Microsoft YaHei UI", 7, "bold"),
            )

            screen_w = root.winfo_screenwidth()
            x = max(6, screen_w - width - 12)
            y = 10
            root.geometry(f"{width}x{height}+{x}+{y}")
            try:
                root.update_idletasks()
                self.hwnd = int(root.winfo_id() or 0)
            except Exception:
                self.hwnd = 0
            self.canvas = canvas
            self.root = root
            self.init_error = ""
        except Exception as exc:
            self.enabled = False
            self.backend = ""
            self.root = None
            self.canvas = None
            self.init_error = repr(exc)

    def _init_layered_ui(self) -> None:
        enable_process_dpi_awareness()
        user32 = ctypes.windll.user32
        gdi32 = ctypes.windll.gdi32
        user32.CreateWindowExW.restype = wintypes.HWND
        user32.CreateWindowExW.argtypes = [
            wintypes.DWORD,
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
            wintypes.DWORD,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.HWND,
            wintypes.HMENU,
            wintypes.HINSTANCE,
            wintypes.LPVOID,
        ]
        user32.UpdateLayeredWindow.restype = wintypes.BOOL
        user32.UpdateLayeredWindow.argtypes = [
            wintypes.HWND,
            wintypes.HDC,
            ctypes.POINTER(POINT),
            ctypes.POINTER(SIZE),
            wintypes.HDC,
            ctypes.POINTER(POINT),
            wintypes.DWORD,
            ctypes.POINTER(BLENDFUNCTION),
            wintypes.DWORD,
        ]
        user32.ShowWindow.restype = wintypes.BOOL
        user32.GetDC.restype = wintypes.HDC
        user32.GetDC.argtypes = [wintypes.HWND]
        user32.ReleaseDC.restype = ctypes.c_int
        user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
        user32.DestroyWindow.restype = wintypes.BOOL
        user32.DestroyWindow.argtypes = [wintypes.HWND]
        gdi32.CreateCompatibleDC.restype = wintypes.HDC
        gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
        gdi32.CreateDIBSection.restype = wintypes.HBITMAP
        gdi32.CreateDIBSection.argtypes = [
            wintypes.HDC,
            ctypes.POINTER(BITMAPINFO),
            wintypes.UINT,
            ctypes.POINTER(ctypes.c_void_p),
            wintypes.HANDLE,
            wintypes.DWORD,
        ]
        gdi32.SelectObject.restype = wintypes.HGDIOBJ
        gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
        gdi32.DeleteObject.restype = wintypes.BOOL
        gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
        gdi32.DeleteDC.restype = wintypes.BOOL
        gdi32.DeleteDC.argtypes = [wintypes.HDC]

        screen_w = int(user32.GetSystemMetrics(0))
        x = max(6, screen_w - int(self.canvas_width) - 12)
        y = 10
        style_ex = WS_EX_LAYERED | WS_EX_TOPMOST | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE | WS_EX_TRANSPARENT
        hwnd = user32.CreateWindowExW(
            style_ex,
            "STATIC",
            f"OmniAuto RPA Indicator {self.tenant_id}",
            WS_POPUP,
            x,
            y,
            int(self.canvas_width),
            int(self.canvas_height),
            None,
            None,
            None,
            None,
        )
        if not hwnd:
            err = int(ctypes.windll.kernel32.GetLastError() or 0)
            raise RuntimeError(f"CreateWindowExW layered indicator failed (win32_error={err})")
        self.backend = "layered"
        self.hwnd = int(hwnd)
        self.root = self.hwnd
        self._orb_frames = self._build_layered_orb_frames()
        if not self._orb_frames:
            raise RuntimeError("layered_orb_frames_unavailable")
        user32.ShowWindow(hwnd, SW_SHOWNOACTIVATE)
        self._update_layered_frame(force=True, strict=True)

    def _build_layered_orb_frames(self) -> dict[str, list[Any]]:
        if Image is None or ImageDraw is None:
            return {}
        render_scale = 4
        frames: dict[str, list[Any]] = {}
        for theme in INDICATOR_THEMES:
            try:
                frames[theme] = [
                    self._render_spinner_frame(
                        INDICATOR_PALETTES[theme],
                        frame_index,
                        output_size=int(self.orb_draw_size * render_scale),
                        antialias_scale=2,
                    )
                    for frame_index in range(self._orb_frame_count)
                ]
            except Exception:
                continue
        return frames if set(INDICATOR_THEMES).issubset(set(frames)) else {}

    def _create_round_rect(self, canvas: Any, x1: float, y1: float, x2: float, y2: float, radius: float, **kwargs: Any) -> int:
        radius = max(1, min(float(radius), (x2 - x1) / 2, (y2 - y1) / 2))
        points = [
            x1 + radius,
            y1,
            x2 - radius,
            y1,
            x2,
            y1,
            x2,
            y1 + radius,
            x2,
            y2 - radius,
            x2,
            y2,
            x2 - radius,
            y2,
            x1 + radius,
            y2,
            x1,
            y2,
            x1,
            y2 - radius,
            x1,
            y1 + radius,
            x1,
            y1,
        ]
        return int(canvas.create_polygon(points, smooth=True, splinesteps=8, **kwargs))

    def _pil_font(self, size: int, *, bold: bool = False) -> Any:
        key = (int(size), bool(bold))
        if key in self._layered_font_cache:
            return self._layered_font_cache[key]
        if ImageFont is None:
            return None
        candidates = [
            r"C:\Windows\Fonts\msyhbd.ttc" if bold else r"C:\Windows\Fonts\msyh.ttc",
            r"C:\Windows\Fonts\simhei.ttf",
            r"C:\Windows\Fonts\arialbd.ttf" if bold else r"C:\Windows\Fonts\arial.ttf",
        ]
        for candidate in candidates:
            try:
                font = ImageFont.truetype(candidate, int(size))
                self._layered_font_cache[key] = font
                return font
            except Exception:
                continue
        font = ImageFont.load_default()
        self._layered_font_cache[key] = font
        return font

    def _draw_centered_text(self, draw: Any, xy: tuple[float, float], text: str, font: Any, fill: tuple[int, int, int, int]) -> None:
        try:
            bbox = draw.textbbox((0, 0), text, font=font)
            width = bbox[2] - bbox[0]
            height = bbox[3] - bbox[1]
            draw.text((xy[0] - width / 2 - bbox[0], xy[1] - height / 2 - bbox[1]), text, font=font, fill=fill)
        except Exception:
            draw.text(xy, text, font=font, fill=fill, anchor="mm")

    def _render_layered_indicator_image(self) -> Any:
        scale = 4
        width = int(self.canvas_width)
        height = int(self.canvas_height)
        image = Image.new("RGBA", (width * scale, height * scale), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image, "RGBA")
        palette = self._layered_palette
        core = hex_to_rgb(palette["core"])
        ring = hex_to_rgb(palette["ring"])
        shadow = hex_to_rgb(palette["shadow"])
        label_bg = hex_to_rgb(palette["label_bg"])
        label_outline = hex_to_rgb(palette["label_outline"])
        label_accent = hex_to_rgb(palette["label_accent"])
        state_color = hex_to_rgb(palette["state"])
        key_color = hex_to_rgb(palette["key"])

        def sc(value: float) -> int:
            return int(round(value * scale))

        cx = sc(width / 2)
        orb_cy = sc(self.ball_size / 2 + 3)
        orb_size = sc(self.orb_draw_size)
        orb = self._orb_frames[self._active_orb_theme][self._frame_index]
        if getattr(orb, "size", None) != (orb_size, orb_size):
            resample = getattr(getattr(Image, "Resampling", Image), "LANCZOS", Image.LANCZOS)
            orb = orb.resize((orb_size, orb_size), resample)

        draw.ellipse(
            [cx - sc(35), orb_cy - sc(30), cx + sc(35), orb_cy + sc(38)],
            fill=(*shadow, 110),
        )
        draw.ellipse(
            [cx - sc(34), orb_cy - sc(34), cx + sc(34), orb_cy + sc(34)],
            outline=(*ring, 42),
            width=sc(1.1),
        )
        image.alpha_composite(orb, (cx - orb_size // 2, orb_cy - orb_size // 2))

        label_top = sc(self.ball_size + 1)
        label_bottom = sc(height - 4)
        label_left = sc(12)
        label_right = sc(width - 12)
        draw.rounded_rectangle(
            [label_left + sc(2), label_top + sc(2), label_right + sc(2), label_bottom + sc(2)],
            radius=sc(8),
            fill=(0, 4, 8, 135),
        )
        draw.rounded_rectangle(
            [label_left, label_top, label_right, label_bottom],
            radius=sc(8),
            fill=(*label_bg, 238),
            outline=(*label_outline, 145),
            width=sc(1),
        )
        draw.line(
            [label_left + sc(10), label_top + sc(3), label_right - sc(10), label_top + sc(3)],
            fill=(*ring, 72),
            width=max(1, sc(0.8)),
        )
        draw.rounded_rectangle(
            [label_left + sc(1), label_top + sc(5), label_left + sc(5), label_bottom - sc(5)],
            radius=sc(2.4),
            fill=(*label_accent, 245),
        )

        state_font = self._pil_font(sc(9), bold=True)
        key_font = self._pil_font(sc(8), bold=True)
        self._draw_centered_text(draw, (cx, label_top + sc(10.5)), self._layered_state_text, state_font, (*state_color, 255))
        self._draw_centered_text(draw, (cx, label_top + sc(27)), f"{self.control_key_label} 暂停/继续", key_font, (*key_color, 255))

        resample = getattr(getattr(Image, "Resampling", Image), "LANCZOS", Image.LANCZOS)
        return image.resize((width, height), resample)

    def _update_layered_frame(self, *, force: bool = False, strict: bool = False) -> bool:
        if self.backend != "layered" or not self.hwnd:
            return False
        image = self._render_layered_indicator_image().convert("RGBA")
        width, height = image.size
        user32 = ctypes.windll.user32
        gdi32 = ctypes.windll.gdi32
        screen_dc = user32.GetDC(None)
        mem_dc = gdi32.CreateCompatibleDC(screen_dc)
        if not screen_dc or not mem_dc:
            self._last_render_ok = False
            self._last_render_error = "layered_dc_unavailable"
            if strict:
                raise RuntimeError(self._last_render_error)
            return False
        bmi = BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = width
        bmi.bmiHeader.biHeight = -height
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = BI_RGB
        bits = ctypes.c_void_p()
        hbitmap = gdi32.CreateDIBSection(screen_dc, ctypes.byref(bmi), 0, ctypes.byref(bits), None, 0)
        if not hbitmap or not bits.value:
            gdi32.DeleteDC(mem_dc)
            user32.ReleaseDC(None, screen_dc)
            self._last_render_ok = False
            self._last_render_error = "layered_dib_unavailable"
            if strict:
                raise RuntimeError(self._last_render_error)
            return False
        old_bitmap = gdi32.SelectObject(mem_dc, hbitmap)
        data = image.tobytes("raw", "BGRA")
        ctypes.memmove(bits.value, data, len(data))
        screen_w = int(user32.GetSystemMetrics(0))
        dst = POINT(max(6, screen_w - width - 12), 10)
        size = SIZE(width, height)
        src = POINT(0, 0)
        blend = BLENDFUNCTION(AC_SRC_OVER, 0, 255, AC_SRC_ALPHA)
        ctypes.windll.kernel32.SetLastError(0)
        updated = user32.UpdateLayeredWindow(
            self.hwnd,
            screen_dc,
            ctypes.byref(dst),
            ctypes.byref(size),
            mem_dc,
            ctypes.byref(src),
            0,
            ctypes.byref(blend),
            ULW_ALPHA,
        )
        if not updated:
            err = int(ctypes.windll.kernel32.GetLastError() or 0)
            self._last_render_ok = False
            self._last_render_error = f"UpdateLayeredWindow_failed:{err}"
        else:
            self._last_render_ok = True
            self._last_render_error = ""
        if old_bitmap:
            gdi32.SelectObject(mem_dc, old_bitmap)
        gdi32.DeleteObject(hbitmap)
        gdi32.DeleteDC(mem_dc)
        user32.ReleaseDC(None, screen_dc)
        if not updated and strict:
            raise RuntimeError(self._last_render_error or "UpdateLayeredWindow_failed")
        return bool(updated)

    def _build_orb_frames(self) -> dict[str, list[Any]]:
        if Image is None or ImageDraw is None or ImageTk is None:
            return {}
        frames: dict[str, list[Any]] = {}
        for theme in INDICATOR_THEMES:
            try:
                frames[theme] = [
                    ImageTk.PhotoImage(self._render_spinner_frame(INDICATOR_PALETTES[theme], frame_index))
                    for frame_index in range(self._orb_frame_count)
                ]
            except Exception:
                continue
        return frames if set(INDICATOR_THEMES).issubset(set(frames)) else {}

    def _render_spinner_frame(
        self,
        palette: dict[str, str],
        frame_index: int,
        *,
        output_size: int | None = None,
        antialias_scale: int = 4,
    ) -> Any:
        """Render a colorized frame based on the MIT svg-spinners ring-resize motion."""
        scale = max(1, int(antialias_scale))
        size = int(output_size or self.orb_draw_size)
        canvas_size = size * scale
        image = Image.new("RGBA", (canvas_size, canvas_size), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image, "RGBA")
        center = canvas_size / 2
        core = hex_to_rgb(palette["core"])
        ring = hex_to_rgb(palette["ring"])
        shadow = hex_to_rgb(palette["shadow"])
        label_bg = hex_to_rgb(palette["label_bg"])

        outer_radius = canvas_size * 0.42
        ring_width = max(6, int(canvas_size * 0.075))
        frame_total = max(1, int(self._orb_frame_count))

        draw.ellipse(
            [
                center - outer_radius * 1.04,
                center - outer_radius * 0.88,
                center + outer_radius * 1.04,
                center + outer_radius * 1.16,
            ],
            fill=(*shadow, 90),
        )
        for index in range(10, 0, -1):
            ratio = index / 10
            glow_radius = outer_radius * (0.92 + ratio * 0.32)
            draw.ellipse(
                [center - glow_radius, center - glow_radius, center + glow_radius, center + glow_radius],
                fill=(*core, int(6 + 28 * (1 - ratio))),
            )

        background_radius = outer_radius * 0.84
        for index in range(18, 0, -1):
            ratio = index / 18
            radius = background_radius * ratio
            mix = 1 - ratio
            color = tuple(int(label_bg[channel] * ratio + core[channel] * mix * 0.45) for channel in range(3))
            draw.ellipse(
                [center - radius, center - radius, center + radius, center + radius],
                fill=(*color, int(34 + 142 * ratio)),
            )

        bbox_margin = canvas_size * 0.16
        arc_box = [bbox_margin, bbox_margin, canvas_size - bbox_margin, canvas_size - bbox_margin]
        draw.arc(arc_box, 0, 360, fill=(*core, 44), width=max(2, ring_width // 2))
        inner_margin = canvas_size * 0.25
        inner_arc_box = [inner_margin, inner_margin, canvas_size - inner_margin, canvas_size - inner_margin]
        draw.arc(inner_arc_box, 0, 360, fill=(*ring, 32), width=max(2, ring_width // 4))

        start_angle = (frame_index * 360 / frame_total) - 90
        sweep = 112 + 24 * math.sin(frame_index / frame_total * math.tau)
        draw.arc(arc_box, start_angle, start_angle + sweep, fill=(*ring, 245), width=ring_width)
        draw.arc(
            arc_box,
            start_angle + sweep * 0.55,
            start_angle + sweep,
            fill=(*core, 245),
            width=max(2, int(ring_width * 0.68)),
        )
        draw.arc(
            inner_arc_box,
            start_angle + 150,
            start_angle + 236,
            fill=(*core, 120),
            width=max(2, int(ring_width * 0.28)),
        )

        dot_angle = math.radians(start_angle + sweep)
        dot_radius = (canvas_size - bbox_margin * 2) / 2
        dot_x = center + math.cos(dot_angle) * dot_radius
        dot_y = center + math.sin(dot_angle) * dot_radius
        dot_size = max(5, int(ring_width * 0.46))
        draw.ellipse(
            [dot_x - dot_size, dot_y - dot_size, dot_x + dot_size, dot_y + dot_size],
            fill=(*ring, 245),
        )

        center_halo_radius = outer_radius * 0.24
        draw.ellipse(
            [
                center - center_halo_radius,
                center - center_halo_radius,
                center + center_halo_radius,
                center + center_halo_radius,
            ],
            fill=(*core, 48),
        )
        center_dot_radius = outer_radius * 0.13
        draw.ellipse(
            [
                center - center_dot_radius,
                center - center_dot_radius,
                center + center_dot_radius,
                center + center_dot_radius,
            ],
            fill=(*core, 185),
        )
        highlight_radius = outer_radius * 0.16
        draw.ellipse(
            [
                center - outer_radius * 0.46,
                center - outer_radius * 0.5,
                center - outer_radius * 0.46 + highlight_radius,
                center - outer_radius * 0.5 + highlight_radius,
            ],
            fill=(255, 255, 255, 110),
        )
        draw.arc(
            [center - outer_radius * 0.55, center - outer_radius * 0.58, center + outer_radius * 0.58, center + outer_radius * 0.55],
            210,
            262,
            fill=(255, 255, 255, 78),
            width=max(1, int(ring_width * 0.18)),
        )
        resample = getattr(getattr(Image, "Resampling", Image), "LANCZOS", Image.LANCZOS)
        return image.resize((size, size), resample)

    def _set_orb_theme(self, theme: str) -> None:
        if self.backend == "layered":
            if theme not in self._orb_frames:
                theme = "blue"
            if theme != self._active_orb_theme:
                self._active_orb_theme = theme
                self._frame_index = 0
            return
        if not self._orb_frames or self.canvas is None or "orb" not in self.items:
            return
        if theme not in self._orb_frames:
            theme = "blue"
        if theme != self._active_orb_theme:
            self._active_orb_theme = theme
            self._frame_index = 0
        self.canvas.itemconfigure(self.items["orb"], image=self._orb_frames[theme][self._frame_index])

    def _advance_animation(self) -> None:
        if self.backend == "layered":
            frames = self._orb_frames.get(self._active_orb_theme) or []
            if not frames:
                return
            now = time.monotonic()
            if len(frames) <= 1 or now - self._last_frame_at < 0.055:
                return
            self._last_frame_at = now
            self._frame_index = (self._frame_index + 1) % len(frames)
            self._update_layered_frame()
            return
        if not self._orb_frames or self.canvas is None or "orb" not in self.items:
            return
        frames = self._orb_frames.get(self._active_orb_theme) or []
        if not frames:
            return
        now = time.monotonic()
        if len(frames) <= 1 or now - self._last_frame_at < 0.055:
            return
        self._last_frame_at = now
        self._frame_index = (self._frame_index + 1) % len(frames)
        self.canvas.itemconfigure(self.items["orb"], image=frames[self._frame_index])

    def update(self, *, mode: str, runtime_state: str, runtime_message: str, locked: bool) -> None:
        if not self.enabled:
            return
        if self.backend == "layered":
            if not self.hwnd:
                return
        elif self.root is None or self.canvas is None:
            return
        snapshot = (mode, runtime_state, runtime_message, locked)
        if snapshot == self._last_snapshot:
            return
        self._last_snapshot = snapshot

        theme, state_text, palette = indicator_state_snapshot(
            mode=mode,
            runtime_state=runtime_state,
            locked=locked,
        )

        if self.backend == "layered":
            self._set_orb_theme(theme)
            self._layered_state_text = state_text
            self._layered_palette = palette
            self._update_layered_frame(force=True)
            return

        self._set_orb_theme(theme)
        if "orb_shadow" in self.items:
            self.canvas.itemconfigure(self.items["orb_shadow"], fill=palette["shadow"])
        if "orb_fallback_outer" in self.items:
            self.canvas.itemconfigure(self.items["orb_fallback_outer"], fill=palette["label_bg"], outline=palette["label_outline"])
        if "orb_fallback_inner" in self.items:
            self.canvas.itemconfigure(self.items["orb_fallback_inner"], fill=palette["key"], outline=palette["state"])
        self.canvas.itemconfigure(
            self.items["label_bg"],
            fill=palette["label_bg"],
            outline="",
        )
        if "label_glint" in self.items:
            self.canvas.itemconfigure(self.items["label_glint"], fill=palette["label_outline"])
        self.canvas.itemconfigure(self.items["label_accent"], fill=palette["label_accent"])
        self.canvas.itemconfigure(self.items["state_label"], text=state_text, fill=palette["state"])
        self.canvas.itemconfigure(self.items["key"], fill=palette["key"])

    def pump(self) -> None:
        if not self.enabled or self.root is None:
            return
        if self.backend == "layered":
            self._advance_animation()
            return
        try:
            self._advance_animation()
            self.root.update_idletasks()
            self.root.update()
        except Exception:
            self.enabled = False

    def close(self) -> None:
        if self.backend == "layered":
            if self.hwnd:
                try:
                    ctypes.windll.user32.DestroyWindow(self.hwnd)
                except Exception:
                    pass
            self.hwnd = 0
            self.root = None
            return
        if self.root is None:
            return
        try:
            self.root.destroy()
        except Exception:
            pass
        self.root = None


class InputHookGuard:
    def __init__(
        self,
        *,
        block_manual_input: bool,
        control_vk: int,
        control_key_name: str,
        control_double_window_seconds: float,
    ) -> None:
        self.block_manual_input = bool(block_manual_input)
        self.lock_enabled = True
        self.control_vk = int(control_vk)
        self.control_key_name = str(control_key_name or DEFAULT_CONTROL_KEY).strip().lower() or DEFAULT_CONTROL_KEY
        self.control_double_window_seconds = max(0.18, float(control_double_window_seconds))
        self.user32 = ctypes.windll.user32
        self.kernel32 = ctypes.windll.kernel32
        # Explicit Win32 signatures avoid pointer truncation on 64-bit Python.
        self.user32.SetWindowsHookExW.restype = ctypes.c_void_p
        self.user32.SetWindowsHookExW.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD]
        self.user32.UnhookWindowsHookEx.restype = wintypes.BOOL
        self.user32.UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]
        self.user32.CallNextHookEx.restype = LRESULT
        self.user32.CallNextHookEx.argtypes = [ctypes.c_void_p, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
        self.user32.PeekMessageW.restype = wintypes.BOOL
        self.user32.PeekMessageW.argtypes = [ctypes.POINTER(MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT, wintypes.UINT]
        self.user32.TranslateMessage.restype = wintypes.BOOL
        self.user32.TranslateMessage.argtypes = [ctypes.POINTER(MSG)]
        self.user32.DispatchMessageW.restype = LRESULT
        self.user32.DispatchMessageW.argtypes = [ctypes.POINTER(MSG)]
        self.keyboard_hook = None
        self.mouse_hook = None
        self.keyboard_proc_ref = None
        self.mouse_proc_ref = None
        self.pending_single = False
        self.pending_single_deadline = 0.0
        self.queued_action = ""

    def install(self) -> None:
        self.keyboard_proc_ref = LowLevelKeyboardProc(self._keyboard_proc)
        self.mouse_proc_ref = LowLevelMouseProc(self._mouse_proc)
        # For WH_KEYBOARD_LL/WH_MOUSE_LL hooks from ctypes callbacks, hMod=0 is the
        # most compatible path across Python runtimes.
        module = 0
        self.keyboard_hook = self.user32.SetWindowsHookExW(WH_KEYBOARD_LL, self.keyboard_proc_ref, module, 0)
        if not self.keyboard_hook:
            err = int(self.kernel32.GetLastError() or 0)
            raise RuntimeError(f"SetWindowsHookExW keyboard hook failed (win32_error={err})")
        self.mouse_hook = self.user32.SetWindowsHookExW(WH_MOUSE_LL, self.mouse_proc_ref, module, 0)
        if not self.mouse_hook:
            err = int(self.kernel32.GetLastError() or 0)
            raise RuntimeError(f"SetWindowsHookExW mouse hook failed (win32_error={err})")

    def uninstall(self) -> None:
        if self.keyboard_hook:
            try:
                self.user32.UnhookWindowsHookEx(self.keyboard_hook)
            except Exception:
                pass
            self.keyboard_hook = None
        if self.mouse_hook:
            try:
                self.user32.UnhookWindowsHookEx(self.mouse_hook)
            except Exception:
                pass
            self.mouse_hook = None

    def set_lock_enabled(self, enabled: bool) -> None:
        self.lock_enabled = bool(enabled)

    def pump_messages(self) -> bool:
        msg = MSG()
        while self.user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
            if msg.message == WM_QUIT:
                return False
            self.user32.TranslateMessage(ctypes.byref(msg))
            self.user32.DispatchMessageW(ctypes.byref(msg))
        return True

    def poll_action(self) -> str:
        if self.queued_action:
            action = self.queued_action
            self.queued_action = ""
            return action
        if self.pending_single and time.monotonic() >= self.pending_single_deadline:
            self.pending_single = False
            self.pending_single_deadline = 0.0
            return "toggle_pause"
        return ""

    def _keyboard_proc(self, n_code: int, w_param: int, l_param: int) -> int:
        if n_code == HC_ACTION:
            event = ctypes.cast(l_param, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
            flags = int(event.flags)
            injected = bool(flags & (LLKHF_INJECTED | LLKHF_LOWER_IL_INJECTED))
            message = int(w_param)
            if not injected and int(event.vkCode) == self.control_vk and message in {WM_KEYDOWN, WM_SYSKEYDOWN}:
                self._on_control_keydown()
                return 1
            if self.lock_enabled and self.block_manual_input and not injected:
                if message in {WM_KEYDOWN, WM_KEYUP, WM_SYSKEYDOWN, WM_SYSKEYUP}:
                    return 1
        return int(self.user32.CallNextHookEx(self.keyboard_hook, n_code, w_param, l_param))

    def _mouse_proc(self, n_code: int, w_param: int, l_param: int) -> int:
        if n_code == HC_ACTION and self.lock_enabled and self.block_manual_input:
            event = ctypes.cast(l_param, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
            flags = int(event.flags)
            injected = bool(flags & (LLMHF_INJECTED | LLMHF_LOWER_IL_INJECTED))
            if not injected:
                return 1
        return int(self.user32.CallNextHookEx(self.mouse_hook, n_code, w_param, l_param))

    def _on_control_keydown(self) -> None:
        now = time.monotonic()
        if self.pending_single and now <= self.pending_single_deadline:
            self.pending_single = False
            self.pending_single_deadline = 0.0
            self.queued_action = "stop"
            return
        self.pending_single = True
        self.pending_single_deadline = now + self.control_double_window_seconds


def normalize_control_key_name(value: Any) -> str:
    key = str(value or DEFAULT_CONTROL_KEY).strip().lower()
    if key not in CONTROL_KEY_CHOICES:
        key = DEFAULT_CONTROL_KEY
    return key


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--control-path", type=Path, required=True)
    parser.add_argument("--status-path", type=Path, required=True)
    parser.add_argument("--parent-pid", type=int, required=True)
    parser.add_argument("--control-key", choices=sorted(CONTROL_KEY_CHOICES.keys()), default=DEFAULT_CONTROL_KEY)
    parser.add_argument("--control-double-window-ms", type=int, default=420)
    # Backward-compatible alias kept for existing launchers/configs.
    parser.add_argument("--esc-double-window-ms", type=int, default=None)
    parser.add_argument("--pause-poll-interval-ms", type=int, default=550)
    parser.add_argument("--block-manual-input", action="store_true")
    parser.add_argument("--allow-manual-input", action="store_true")
    parser.add_argument("--floating-indicator", action="store_true")
    parser.add_argument("--no-floating-indicator", action="store_true")
    parser.add_argument("--guard-state-path", type=Path)
    parser.add_argument("--local-safety-stop-path", default=DEFAULT_LOCAL_SAFETY_STOP_PATH)
    args = parser.parse_args()

    tenant_id = str(args.tenant_id).strip() or "default"
    control_path = args.control_path.resolve()
    status_path = args.status_path.resolve()
    parent_pid = int(args.parent_pid or 0)
    control_key_name = normalize_control_key_name(args.control_key)
    control_vk = CONTROL_KEY_CHOICES[control_key_name]
    control_key_label = control_key_name.upper()
    local_safety_stop_path = normalize_local_safety_stop_path(args.local_safety_stop_path)
    control_double_window_raw = args.control_double_window_ms
    if args.esc_double_window_ms is not None:
        control_double_window_raw = args.esc_double_window_ms
    control_double_window_ms = bounded_int(
        control_double_window_raw,
        default=420,
        minimum=180,
        maximum=1200,
    )
    control_poll_interval = bounded_int(
        args.pause_poll_interval_ms,
        default=550,
        minimum=120,
        maximum=3000,
    ) / 1000.0
    block_manual_input = True
    if args.allow_manual_input:
        block_manual_input = False
    elif args.block_manual_input:
        block_manual_input = True
    floating_indicator = True
    if args.no_floating_indicator:
        floating_indicator = False
    elif args.floating_indicator:
        floating_indicator = True
    guard_state_path = args.guard_state_path.resolve() if isinstance(args.guard_state_path, Path) else None

    control_exists = control_path.exists()
    payload = load_control_payload(control_path, tenant_id=tenant_id)
    mode_before = str(payload.get("mode") or "").strip().lower()
    if mode_before == "stopped":
        payload["mode"] = "running"
    # Avoid rewriting an already-initialized control file on startup unless
    # we need to recover mode from "stopped". This narrows the race window
    # where an early external command could be overwritten.
    if (not control_exists) or mode_before == "stopped":
        write_json(control_path, payload)

    hooks_installed = False
    indicator: FloatingIndicator | None = None

    def emit_guard_state(phase: str, *, mode: str, runtime_state: str, runtime_message: str, lock_enabled: bool, reason: str = "") -> None:
        indicator_active = bool(indicator is not None and indicator.enabled and indicator.root is not None)
        indicator_backend = str((indicator.backend if indicator is not None else "") or "")
        indicator_hwnd = int((indicator.hwnd if indicator is not None else 0) or 0)
        indicator_render_ok = bool(
            indicator is not None and (indicator.backend != "layered" or indicator._last_render_ok)
        )
        indicator_render_error = str((indicator._last_render_error if indicator is not None else "") or "")
        indicator_fallback_reason = str((indicator.fallback_reason if indicator is not None else "") or "")
        indicator_error = str((indicator.init_error if indicator is not None else "") or "")
        write_guard_state(
            guard_state_path,
            {
                "phase": phase,
                "pid": os.getpid(),
                "parent_pid": parent_pid,
                "tenant_id": tenant_id,
                "mode": mode,
                "runtime_state": runtime_state,
                "runtime_message": runtime_message,
                "lock_enabled": bool(lock_enabled),
                "block_manual_input": bool(block_manual_input),
                "hooks_installed": bool(hooks_installed),
                "floating_indicator_requested": bool(floating_indicator),
                "floating_indicator_active": indicator_active,
                "floating_indicator_backend": indicator_backend,
                "floating_indicator_hwnd": indicator_hwnd,
                "floating_indicator_render_ok": indicator_render_ok,
                "floating_indicator_render_error": indicator_render_error,
                "floating_indicator_fallback_reason": indicator_fallback_reason,
                "floating_indicator_error": indicator_error,
                "control_key": control_key_name,
                "reason": reason,
            },
        )

    emit_guard_state(
        "starting",
        mode="running",
        runtime_state="idle",
        runtime_message="guard_process_started",
        lock_enabled=False,
        reason="guard_process_started",
    )
    try:
        hooks = InputHookGuard(
            block_manual_input=block_manual_input,
            control_vk=control_vk,
            control_key_name=control_key_name,
            control_double_window_seconds=float(control_double_window_ms) / 1000.0,
        )
    except Exception as exc:
        emit_guard_state(
            "failed",
            mode="stopped",
            runtime_state="stopped",
            runtime_message="hook_guard_init_failed",
            lock_enabled=False,
            reason=f"hook_guard_init_failed:{repr(exc)}",
        )
        return 2
    emit_guard_state(
        "starting",
        mode="running",
        runtime_state="idle",
        runtime_message="indicator_initializing",
        lock_enabled=False,
        reason="indicator_initializing",
    )
    try:
        indicator = FloatingIndicator(
            enabled=floating_indicator,
            tenant_id=tenant_id,
            control_key_label=control_key_label,
        )
    except Exception as exc:
        emit_guard_state(
            "failed",
            mode="stopped",
            runtime_state="stopped",
            runtime_message="floating_indicator_init_failed",
            lock_enabled=False,
            reason=f"floating_indicator_init_failed:{repr(exc)}",
        )
        return 2
    emit_guard_state(
        "starting",
        mode="running",
        runtime_state="idle",
        runtime_message="hooks_installing",
        lock_enabled=False,
        reason="hooks_installing",
    )
    try:
        hooks.install()
        hooks_installed = True
    except Exception as exc:
        emit_guard_state(
            "failed",
            mode="stopped",
            runtime_state="stopped",
            runtime_message="hook_install_failed",
            lock_enabled=False,
            reason=f"hook_install_failed:{repr(exc)}",
        )
        if indicator is not None:
            indicator.close()
        return 2

    last_control_refresh_at = 0.0
    last_runtime_refresh_at = 0.0
    last_state_emit_at = 0.0
    cached_control = payload
    cached_runtime = {"state": "idle", "message": "守护已启动"}
    loop_sleep_seconds = 0.015
    try:
        cached_runtime = load_runtime_status(status_path)
    except Exception:
        cached_runtime = {"state": "idle", "message": "守护已启动"}
    initial_mode = str(cached_control.get("mode") or "running").strip().lower()
    if initial_mode not in CONTROL_MODES:
        initial_mode = "running"
    initial_locked = initial_mode == "running"
    hooks.set_lock_enabled(initial_locked)
    indicator.update(
        mode=initial_mode,
        runtime_state=str(cached_runtime.get("state") or "stopped"),
        runtime_message=str(cached_runtime.get("message") or ""),
        locked=initial_locked,
    )
    indicator.pump()
    emit_guard_state(
        "running",
        mode=initial_mode,
        runtime_state=str(cached_runtime.get("state") or "stopped"),
        runtime_message=str(cached_runtime.get("message") or ""),
        lock_enabled=initial_locked,
        reason="hooks_installed",
    )

    try:
        while True:
            if not hooks.pump_messages():
                break

            now = time.monotonic()
            action = hooks.poll_action()
            if action == "stop":
                issue_command(
                    control_path,
                    tenant_id=tenant_id,
                    action="stop",
                    source=f"{control_key_name}_double",
                    message=f"double_{control_key_name}_stop_requested",
                )
                write_runtime_status_hint(status_path, tenant_id=tenant_id, state="stopped", message="已停止。")
                request_local_safety_stop(tenant_id=tenant_id, stop_path=local_safety_stop_path)
            elif action == "toggle_pause":
                latest = load_control_payload(control_path, tenant_id=tenant_id)
                current_mode = str(latest.get("mode") or "running").strip().lower()
                if current_mode == "paused":
                    issue_command(
                        control_path,
                        tenant_id=tenant_id,
                        action="resume",
                        source=f"{control_key_name}_single",
                        message=f"single_{control_key_name}_resume_requested",
                    )
                    write_runtime_status_hint(status_path, tenant_id=tenant_id, state="idle", message="监听运行中。")
                elif current_mode == "running":
                    issue_command(
                        control_path,
                        tenant_id=tenant_id,
                        action="pause",
                        source=f"{control_key_name}_single",
                        message=f"single_{control_key_name}_pause_requested",
                    )
                    write_runtime_status_hint(status_path, tenant_id=tenant_id, state="paused", message="已暂停，等待继续。")

            if now - last_control_refresh_at >= max(0.08, control_poll_interval):
                cached_control = load_control_payload(control_path, tenant_id=tenant_id)
                last_control_refresh_at = now
            if now - last_runtime_refresh_at >= 0.20:
                cached_runtime = load_runtime_status(status_path)
                last_runtime_refresh_at = now

            mode = str(cached_control.get("mode") or "running").strip().lower()
            if mode not in CONTROL_MODES:
                mode = "running"
            locked = mode == "running"
            hooks.set_lock_enabled(locked)
            indicator.update(
                mode=mode,
                runtime_state=str(cached_runtime.get("state") or "stopped"),
                runtime_message=str(cached_runtime.get("message") or ""),
                locked=locked,
            )
            indicator.pump()
            if now - last_state_emit_at >= 0.45:
                emit_guard_state(
                    "running",
                    mode=mode,
                    runtime_state=str(cached_runtime.get("state") or "stopped"),
                    runtime_message=str(cached_runtime.get("message") or ""),
                    lock_enabled=locked,
                )
                last_state_emit_at = now

            if not process_alive(parent_pid):
                final_payload = load_control_payload(control_path, tenant_id=tenant_id)
                final_payload["mode"] = "stopped"
                final_command = final_payload.get("command") if isinstance(final_payload.get("command"), dict) else {}
                if str(final_command.get("status") or "").strip().lower() != "pending":
                    final_command["message"] = "listener_parent_exited"
                    final_payload["command"] = final_command
                write_json(control_path, final_payload)
                emit_guard_state(
                    "stopped",
                    mode="stopped",
                    runtime_state="stopped",
                    runtime_message="listener_parent_exited",
                    lock_enabled=False,
                    reason="listener_parent_exited",
                )
                break

            if mode == "stopped":
                emit_guard_state(
                    "stopped",
                    mode="stopped",
                    runtime_state=str(cached_runtime.get("state") or "stopped"),
                    runtime_message=str(cached_runtime.get("message") or ""),
                    lock_enabled=False,
                    reason="mode_stopped",
                )
                break

            time.sleep(loop_sleep_seconds)
    finally:
        hooks_installed = False
        hooks.uninstall()
        if indicator is not None:
            indicator.close()
        emit_guard_state(
            "stopped",
            mode="stopped",
            runtime_state="stopped",
            runtime_message="guard_exit",
            lock_enabled=False,
            reason="guard_exit",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
