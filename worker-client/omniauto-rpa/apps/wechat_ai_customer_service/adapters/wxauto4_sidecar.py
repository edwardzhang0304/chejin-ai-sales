"""JSON sidecar for wxauto4-based WeChat probes.

Run this script with a Python 3.9-3.12 interpreter that has ``wxauto4``
installed. The main OmniAuto project currently runs on Python 3.13, while
wxauto4 only publishes wheels through cp312, so keeping this as a sidecar
avoids contaminating the primary environment.
"""

from __future__ import annotations

import argparse
import contextlib
import ctypes
import io
import json
import re
import sys
import time
from ctypes import wintypes
from typing import Any


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["status", "sessions", "messages", "send"], nargs="?")
    parser.add_argument("--target", help="Chat name for messages/send.")
    parser.add_argument("--text", help="Message text for send.")
    parser.add_argument("--exact", action="store_true", help="Use exact chat name matching.")
    parser.add_argument("--resize", action="store_true", help="Allow wxauto4 to resize WeChat.")
    parser.add_argument("--history-load-times", type=int, default=0, help="Use wxauto4 LoadMoreCache before reading messages.")
    parser.add_argument("--daemon", action="store_true", help="Run in daemon mode (read commands from stdin).")
    args = parser.parse_args()

    if args.daemon:
        return run_daemon()

    payload: dict[str, Any]
    captured = io.StringIO()
    try:
        with contextlib.redirect_stdout(captured):
            payload = run_action(args)
        payload.setdefault("ok", bool(payload.get("online")))
    except Exception as exc:
        payload = {"ok": False, "error": repr(exc)}

    logs = captured.getvalue().strip()
    if logs:
        payload["library_stdout"] = logs

    print(json.dumps(payload, ensure_ascii=True, indent=2))
    return 0 if payload.get("ok") else 1


def run_daemon() -> int:
    """Run in daemon mode: read JSON commands from stdin, write JSON responses to stdout."""
    import sys

    wx = None
    window_probe = None

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            print(json.dumps({"ok": False, "error": "invalid_json"}, ensure_ascii=True))
            sys.stdout.flush()
            continue

        if request.get("action") == "exit":
            print(json.dumps({"ok": True, "state": "exiting"}, ensure_ascii=True))
            sys.stdout.flush()
            return 0

        if bool(request.get("fresh")):
            # Force a fresh probe and fresh wxauto attachment for the next action.
            wx = None
            window_probe = None

        # Probe window on first real command
        if window_probe is None:
            window_probe = ensure_visible_wechat_window()

        if not window_probe["visible_main_windows"]:
            print(json.dumps({
                "ok": False,
                "online": False,
                "state": "main_window_not_found",
                "window_probe": window_probe,
                "error": "No visible WeChat main window was found.",
            }, ensure_ascii=True))
            sys.stdout.flush()
            continue

        # Lazy init WeChat connection once and reuse
        if wx is None:
            from wxauto4 import WeChat
            try:
                resize = bool(request.get("resize"))
                wx = WeChat(debug=False, resize=resize, ads=False)
            except Exception as exc:
                print(json.dumps({
                    "ok": False,
                    "online": False,
                    "state": "connect_failed",
                    "connect_error": repr(exc),
                    "window_probe": window_probe,
                    "error": "Visible WeChat window exists, but wxauto4 could not attach to it.",
                }, ensure_ascii=True))
                sys.stdout.flush()
                continue

        # Build args from request and execute
        args = argparse.Namespace(
            action=request.get("action", "status"),
            target=request.get("target"),
            text=request.get("text"),
            exact=bool(request.get("exact", False)),
            resize=bool(request.get("resize", False)),
            history_load_times=int(request.get("history_load_times", 0) or 0),
        )

        captured = io.StringIO()
        try:
            with contextlib.redirect_stdout(captured):
                payload = run_action(args, wx=wx, window_probe=window_probe)
            payload.setdefault("ok", bool(payload.get("online")))
        except Exception as exc:
            payload = {"ok": False, "error": repr(exc)}

        logs = captured.getvalue().strip()
        if logs:
            payload["library_stdout"] = logs

        print(json.dumps(payload, ensure_ascii=True))
        sys.stdout.flush()

    return 0


def run_action(
    args: argparse.Namespace,
    wx: Any = None,
    window_probe: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from wxauto4 import WeChat

    if window_probe is None:
        window_probe = ensure_visible_wechat_window()
    if not window_probe["visible_main_windows"]:
        return {
            "login_window_exists": False,
            "online": False,
            "state": "main_window_not_found",
            "window_probe": window_probe,
            "error": "No visible WeChat main window was found; refusing to start or attach to a login/secondary window.",
        }

    if wx is None:
        try:
            wx = WeChat(debug=False, resize=args.resize, ads=False)
        except Exception as exc:
            return {
                "login_window_exists": False,
                "online": False,
                "state": "connect_failed",
                "connect_error": repr(exc),
                "window_probe": window_probe,
                "error": "Visible WeChat window exists, but wxauto4 could not attach to it.",
            }

    payload: dict[str, Any] = {
        "login_window_exists": False,
        "online": bool(wx.IsOnline()),
        "my_info": safe_call(wx.GetMyInfo),
        "window_probe": window_probe,
    }

    if args.action == "status":
        payload["state"] = "main_window"
    elif args.action == "sessions":
        payload["sessions"] = [session_to_dict(item) for item in wx.GetSession()]
    elif args.action == "messages":
        if args.target:
            wx.ChatWith(args.target, exact=args.exact)
            time.sleep(0.5)
        payload["scroll_to_latest"] = scroll_message_list_to_latest(wx)
        payload["chat_info"] = safe_call(wx.ChatInfo)
        history_load_times = bounded_history_load_times(getattr(args, "history_load_times", 0))
        if history_load_times:
            history_result = collect_messages_with_rpa_history(wx, history_load_times)
            payload["history_load"] = history_result["history_load"]
            payload["messages"] = history_result["messages"]
        else:
            payload["messages"] = [message_to_dict(item) for item in wx.GetAllMessage()]
    elif args.action == "send":
        if not args.target:
            raise ValueError("--target is required for send")
        if args.text is None:
            raise ValueError("--text is required for send")
        chat_result = safe_call(lambda: wx.ChatWith(args.target, exact=args.exact, force=True, force_wait=0.5))
        time.sleep(0.5)
        chat_info = safe_call(wx.ChatInfo)
        if not chat_matches(chat_info, args.target, exact=args.exact):
            raise RuntimeError(f"target chat not active before send: target={args.target!r} chat_info={chat_info!r}")
        payload["chat_with_result"] = chat_result
        payload["chat_info_before_send"] = chat_info
        payload["send_result"] = send_text_with_chatbox_controls(wx, args.text)
        time.sleep(0.5)
        payload["chat_info_after_send"] = safe_call(wx.ChatInfo)
    return payload


def send_text_with_chatbox_controls(wx: Any, text: str) -> dict[str, Any]:
    """Send through wxauto4's exposed ChatBox controls.

    In some WeChat 4.x environments wxauto4's high-level SendMsg wrapper can
    block indefinitely. The lower-level ChatBox UIA controls remain the same
    wxauto4/RPA boundary but give us explicit control and predictable returns.
    """
    chatbox = getattr(wx, "ChatBox", None)
    if chatbox is None:
        raise RuntimeError("wxauto4 ChatBox is unavailable")
    editbox = getattr(chatbox, "editbox", None)
    sendbtn = getattr(chatbox, "sendbtn", None)
    if editbox is None or sendbtn is None:
        raise RuntimeError("wxauto4 ChatBox editbox/send button is unavailable")
    editbox.SetFocus()
    time.sleep(0.1)
    value_pattern = editbox.GetValuePattern()
    value_pattern.SetValue("")
    time.sleep(0.05)
    value_pattern.SetValue(text)
    time.sleep(0.15)
    sendbtn.Click()
    return {"ok": True, "method": "wxauto4.ChatBox.editbox.ValuePattern.SetValue+sendbtn.Click"}


def collect_messages_with_rpa_history(wx: Any, load_times: int) -> dict[str, Any]:
    """Collect visible windows while scrolling upward with wxauto4 UIA controls.

    wxauto4 exposes a high-level ``LoadMoreCache`` API, but on the current
    WeChat 4.x window it delegates to a missing internal method. This keeps the
    implementation inside the existing wxauto4/RPA boundary while making the
    history-read behavior explicit and auditable.
    """
    windows: list[list[dict[str, Any]]] = [[message_to_dict(item) for item in wx.GetAllMessage()]]
    history_load: dict[str, Any] = {
        "ok": True,
        "requested_load_times": load_times,
        "mechanism": "wxauto4.ChatBox.msgbox.WheelUp+GetAllMessage",
        "scroll_units_per_load": 6,
        "window_samples_per_load": 4,
        "window_counts": [len(windows[0])],
        "errors": [],
    }
    chatbox = getattr(wx, "ChatBox", None)
    msgbox = getattr(chatbox, "msgbox", None) if chatbox is not None else None
    if msgbox is None:
        history_load["ok"] = False
        history_load["errors"].append("wxauto4 ChatBox msgbox is unavailable")
        return {"history_load": history_load, "messages": windows[0]}

    scroll_units = int(history_load["scroll_units_per_load"])
    samples_per_load = int(history_load["window_samples_per_load"])
    requested_samples = max(0, int(load_times or 0)) * max(1, samples_per_load)
    performed_samples = 0
    for _ in range(requested_samples):
        try:
            msgbox.MoveCursorToMyCenter()
            msgbox.WheelUp(wheelTimes=scroll_units)
            performed_samples += 1
            time.sleep(0.3)
            window = [message_to_dict(item) for item in wx.GetAllMessage()]
            windows.append(window)
            history_load["window_counts"].append(len(window))
        except Exception as exc:
            history_load["ok"] = False
            history_load["errors"].append(repr(exc))
            break

    if performed_samples:
        history_load["restore_to_latest"] = scroll_message_list_to_latest(
            wx,
            wheel_times=max(30, performed_samples * scroll_units * 3),
        )

    merged = merge_message_dict_windows(*reversed(windows))
    history_load["performed_scroll_samples"] = performed_samples
    history_load["merged_count"] = len(merged)
    return {"history_load": history_load, "messages": merged}


def scroll_message_list_to_latest(wx: Any, wheel_times: int = 60) -> dict[str, Any]:
    chatbox = getattr(wx, "ChatBox", None)
    msgbox = getattr(chatbox, "msgbox", None) if chatbox is not None else None
    if msgbox is None:
        return {"ok": False, "reason": "msgbox_unavailable"}
    try:
        msgbox.MoveCursorToMyCenter()
        msgbox.SendKeys("{End}")
        time.sleep(0.15)
        msgbox.WheelDown(wheelTimes=max(1, int(wheel_times or 1)))
        time.sleep(0.2)
        return {"ok": True, "method": "wxauto4.ChatBox.msgbox.SendKeys(End)+WheelDown", "wheel_times": wheel_times}
    except Exception as exc:
        return {"ok": False, "reason": "wheel_down_failed", "error": repr(exc), "wheel_times": wheel_times}


def merge_message_dict_windows(*windows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[Any, ...]] = set()
    merged: list[dict[str, Any]] = []
    for window in windows:
        for item in window or []:
            if not isinstance(item, dict):
                continue
            key = (
                item.get("id") or "",
                item.get("sender") or "",
                item.get("type") or "",
                item.get("time") or "",
                item.get("content") or "",
            )
            if key in seen:
                continue
            seen.add(key)
            merged.append(item)
    return merged


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


def ensure_visible_wechat_window() -> dict[str, Any]:
    probe = probe_wechat_windows()
    if probe["visible_main_windows"]:
        focused = focus_wechat_window(probe)
        if focused:
            time.sleep(0.2)
            probe = probe_wechat_windows()
            probe["focused_window"] = focused
        return probe

    restored = restore_wechat_window(probe)
    if restored:
        time.sleep(0.8)
        probe = probe_wechat_windows()
        probe["restored_window"] = restored
        focused = focus_wechat_window(probe)
        if focused:
            time.sleep(0.2)
            probe = probe_wechat_windows()
            probe["focused_window"] = focused
    return probe


def restore_wechat_window(probe: dict[str, Any]) -> dict[str, Any] | None:
    for item in probe.get("windows") or []:
        if not is_wechat_main_window(item):
            continue
        hwnd = int(item.get("hwnd") or 0)
        if not hwnd:
            continue
        activate_window(hwnd)
        return dict(item)
    return None


def focus_wechat_window(probe: dict[str, Any]) -> dict[str, Any] | None:
    visible = probe.get("visible_main_windows") or []
    if not visible:
        return None
    item = visible[0]
    hwnd = int(item.get("hwnd") or 0)
    if not hwnd:
        return None
    activate_window(hwnd)
    return dict(item)


def activate_window(hwnd: int) -> None:
    user32 = ctypes.windll.user32
    sw_restore = 9
    sw_show = 5
    user32.ShowWindow(hwnd, sw_restore)
    user32.ShowWindow(hwnd, sw_show)
    user32.SetForegroundWindow(hwnd)
    user32.BringWindowToTop(hwnd)


def is_wechat_main_window(item: dict[str, Any]) -> bool:
    title = str(item.get("title") or "").strip()
    class_name = str(item.get("class_name") or "")
    normalized = normalize_wechat_title(title)
    if not normalized:
        return False
    class_name_lower = class_name.lower()
    main_class_ok = ("qwindowicon" in class_name_lower) or ("wechatmainwndforpc" in class_name_lower)
    if not main_class_ok:
        return False
    positive_tokens = ("微信", "weixin", "wechat")
    negative_tokens = ("登录", "login", "qr", "扫码", "更新", "update")
    lowered = normalized.lower()
    if any(token in lowered for token in negative_tokens):
        return False
    return any(token in normalized for token in positive_tokens) or any(token in lowered for token in positive_tokens)


def normalize_wechat_title(title: str) -> str:
    text = str(title or "").strip()
    if not text:
        return ""
    # Common unread prefix: "(2) 微信"
    text = re.sub(r"^\(\d+\)\s*", "", text).strip()
    # Some locales prepend unread with full-width parentheses.
    text = re.sub(r"^（\d+）\s*", "", text).strip()
    return text


def bounded_history_load_times(value: Any, *, max_times: int = 5) -> int:
    try:
        parsed = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return max(0, min(max_times, parsed))


def chat_matches(chat_info: Any, target: str, exact: bool) -> bool:
    if not isinstance(chat_info, dict):
        return False
    names = [
        chat_info.get("chat_name"),
        chat_info.get("name"),
        chat_info.get("Name"),
        chat_info.get("title"),
    ]
    normalized = [str(item).strip() for item in names if item]
    if exact:
        return target in normalized
    return any(target in item for item in normalized)


def session_to_dict(session: Any) -> dict[str, Any]:
    info = safe_getattr(session, "info")
    if isinstance(info, dict):
        return info
    return {
        "name": safe_getattr(session, "name"),
        "content": safe_getattr(session, "content"),
        "time": safe_getattr(session, "time"),
    }


def message_to_dict(message: Any) -> dict[str, Any]:
    return {
        "repr": repr(message),
        "type": safe_getattr(message, "type"),
        "sender": safe_getattr(message, "sender"),
        "content": safe_getattr(message, "content"),
        "time": safe_getattr(message, "time"),
        "id": safe_getattr(message, "id"),
    }


def normalize_response(response: Any) -> Any:
    if isinstance(response, dict):
        return response
    return {
        "repr": repr(response),
        "status": safe_getattr(response, "status"),
        "message": safe_getattr(response, "message"),
        "data": safe_getattr(response, "data"),
    }


def safe_getattr(obj: Any, name: str) -> Any:
    try:
        return getattr(obj, name)
    except AttributeError:
        return None
    except Exception as exc:
        return f"<error: {exc!r}>"


def safe_call(fn: Any) -> Any:
    try:
        return fn()
    except Exception as exc:
        return {"error": repr(exc)}


if __name__ == "__main__":
    raise SystemExit(main())
