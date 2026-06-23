"""Connector wrapper for WeChat sidecars with RPA-first transport priority.

The connector is the stable boundary the workflow layer should use. It keeps
WeChat transport details outside the OmniAuto Python 3.13 process and returns
plain dictionaries that are easy to validate and persist.

Transport policy:
1. Try Win32/OCR (pure RPA) first.
2. Optionally fall back to wxauto4 when ``WECHAT_ENABLE_WXAUTO4=1``.

wxauto4 daemon mode caching remains available as a technical reserve to avoid
the ~2-5 second overhead of starting Python 3.12 and importing wxauto4 on
every operation when that reserve path is enabled.
"""

from __future__ import annotations

import difflib
import json
import os
import queue
import re
import hashlib
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psutil

from apps.wechat_ai_customer_service.adapters.add_friend_routes import ADD_FRIEND_ROUTES


ROOT = Path(__file__).resolve().parents[3]
SIDECAR_PYTHON = ROOT / "runtime/tool_envs/wxauto4-py312/Scripts/python.exe"
SIDECAR_SCRIPT = ROOT / "apps/wechat_ai_customer_service/adapters/wxauto4_sidecar.py"
COMPAT_SIDECAR_SCRIPT = ROOT / "apps/wechat_ai_customer_service/adapters/wechat_win32_ocr_sidecar.py"
COMPAT_SIDECAR_PYTHON = ROOT / ".venv/Scripts/python.exe"
WECHAT_EXE = Path(r"C:\Program Files (x86)\Tencent\Weixin\Weixin.exe")
FILE_TRANSFER_ASSISTANT = "".join(chr(c) for c in [0x6587, 0x4EF6, 0x4F20, 0x8F93, 0x52A9, 0x624B])
WECHAT_RPA_LOCK_PATH = ROOT / "runtime" / "wechat_rpa.lock"

# Global daemon process cache to avoid repeated subprocess spawn overhead.
_daemon_proc: subprocess.Popen | None = None
_daemon_lock = threading.Lock()
_compat_daemon_proc: subprocess.Popen | None = None
_compat_daemon_lock = threading.Lock()
_simulated_inbound_lock = threading.Lock()
_simulated_inbound_cache: dict[str, list[dict[str, Any]]] = {}


class WeChatConnectorError(RuntimeError):
    """Raised when the connector cannot complete a guarded operation."""


class RPALockTimeoutError(TimeoutError):
    """RPA lock acquisition timed out with observability metadata."""

    def __init__(self, message: str, *, meta: dict[str, Any]) -> None:
        super().__init__(message)
        self.meta = dict(meta)


@dataclass(frozen=True)
class WeChatConnector:
    sidecar_python: Path = SIDECAR_PYTHON
    sidecar_script: Path = SIDECAR_SCRIPT
    compat_sidecar_script: Path = COMPAT_SIDECAR_SCRIPT
    compat_sidecar_python: Path = COMPAT_SIDECAR_PYTHON
    root: Path = ROOT
    timeout_seconds: int = 120

    def wxauto4_reserve_enabled(self) -> bool:
        return env_flag("WECHAT_ENABLE_WXAUTO4", default=False)

    def status(self, *, interactive: bool = False) -> dict[str, Any]:
        lock_timeout = rpa_lock_timeout_seconds("status", default=12.0)
        try:
            with wechat_rpa_lock("status", timeout_seconds=lock_timeout) as lock_meta:
                env_overrides = interactive_rpa_probe_env() if interactive else None
                primary = self.call_compat_sidecar(["status"], allow_failure=True, env_overrides=env_overrides)
                primary = self._retry_recoverable_rpa_probe(
                    ["status"],
                    primary,
                    interactive=interactive,
                    action="status",
                )
                if primary.get("ok") and primary.get("online"):
                    primary.setdefault("adapter", "win32_ocr")
                    primary.setdefault("transport_priority", "rpa_first")
                    attach_rpa_lock_meta(primary, lock_meta)
                    return primary
                if rpa_render_fault_should_stop(primary):
                    primary.setdefault("adapter", "win32_ocr")
                    primary.setdefault("transport_priority", "rpa_first")
                    attach_rpa_lock_meta(primary, lock_meta)
                    return primary
                if rpa_payload_is_tray_hidden(primary):
                    primary.setdefault("adapter", "win32_ocr")
                    primary.setdefault("transport_priority", "rpa_first")
                    primary.setdefault("receive", {"ok": False})
                    primary.setdefault("send", {"ok": False})
                    attach_rpa_lock_meta(primary, lock_meta)
                    return primary
                reserve = self.call_reserve_sidecar(["status"], allow_failure=True, primary_payload=snapshot_payload(primary))
                if reserve.get("ok") and reserve.get("online"):
                    reserve.setdefault("adapter", "wxauto4")
                    reserve.setdefault("transport_priority", "rpa_first")
                    reserve.setdefault("reserve_reason", "rpa_primary_unavailable")
                    attach_rpa_lock_meta(reserve, lock_meta)
                    return reserve
                primary.setdefault("wxauto4_reserve_status", reserve)
                primary.setdefault("transport_priority", "rpa_first")
                attach_rpa_lock_meta(primary, lock_meta)
                return primary
        except TimeoutError as exc:
            return {
                "ok": False,
                "online": False,
                "adapter": "win32_ocr",
                "state": "status_lock_timeout",
                "error": repr(exc),
                "transport_priority": "rpa_first",
                "rpa_lock": rpa_lock_timeout_payload(exc, action="status", timeout_seconds=lock_timeout),
            }

    def capabilities(self, *, interactive: bool = False) -> dict[str, Any]:
        """Detect the active WeChat transport before starting a long-running loop."""
        lock_timeout = rpa_lock_timeout_seconds("capabilities", default=14.0)
        try:
            with wechat_rpa_lock("capabilities", timeout_seconds=lock_timeout) as lock_meta:
                env_overrides = interactive_rpa_probe_env() if interactive else None
                try:
                    primary = self.call_compat_sidecar(
                        ["capabilities"],
                        allow_failure=True,
                        env_overrides=env_overrides,
                    )
                except Exception as exc:
                    primary = {
                        "ok": False,
                        "online": False,
                        "adapter": "win32_ocr",
                        "state": "rpa_capabilities_failed",
                        "error": repr(exc),
                    }
                primary = self._retry_recoverable_rpa_probe(
                    ["capabilities"],
                    primary,
                    interactive=interactive,
                    action="capabilities",
                )
                if primary.get("ok") and primary.get("online"):
                    primary.setdefault("adapter", "win32_ocr")
                    primary.setdefault("scheme", str(primary.get("scheme") or "win32_ocr_unavailable"))
                    primary.setdefault("state", str(primary.get("state") or "rpa_primary_ready"))
                    primary.setdefault("transport_priority", "rpa_first")
                    attach_rpa_lock_meta(primary, lock_meta)
                    return primary
                if rpa_render_fault_should_stop(primary):
                    primary.setdefault("adapter", "win32_ocr")
                    primary.setdefault("receive", {"ok": False})
                    primary.setdefault("send", {"ok": False})
                    primary.setdefault("transport_priority", "rpa_first")
                    attach_rpa_lock_meta(primary, lock_meta)
                    return primary
                if rpa_payload_is_tray_hidden(primary):
                    primary.setdefault("adapter", "win32_ocr")
                    primary.setdefault("receive", {"ok": False})
                    primary.setdefault("send", {"ok": False})
                    primary.setdefault("transport_priority", "rpa_first")
                    attach_rpa_lock_meta(primary, lock_meta)
                    return primary

                try:
                    reserve = self.call_reserve_sidecar(["status"], allow_failure=True, primary_payload=snapshot_payload(primary))
                except Exception as exc:
                    reserve = {
                        "ok": False,
                        "online": False,
                        "adapter": "wxauto4",
                        "state": "wxauto4_reserve_status_failed",
                        "error": repr(exc),
                    }
                if reserve.get("online"):
                    payload = {
                        "ok": True,
                        "online": True,
                        "adapter": "wxauto4",
                        "scheme": "wxauto4",
                        "state": "wxauto4_reserve_ready",
                        "receive": {"ok": True, "method": "wxauto4.GetAllMessage"},
                        "send": {"ok": True, "preferred_mode": "wxauto4", "method": "wxauto4.ChatBox controls"},
                        "primary_status": primary,
                        "reserve_status": reserve,
                        "transport_priority": "rpa_first",
                        "message": "RPA primary is unavailable; using wxauto4 reserve adapter.",
                    }
                    attach_rpa_lock_meta(payload, lock_meta)
                    return payload

                if rpa_payload_needs_interactive_confirmation(primary):
                    primary.setdefault("ok", False)
                    primary.setdefault("online", False)
                    primary.setdefault("adapter", "win32_ocr")
                    primary.setdefault("receive", {"ok": False})
                    primary.setdefault("send", {"ok": False})
                    primary.setdefault("reserve_status", reserve)
                    primary.setdefault("wxauto4_reserve_enabled", self.wxauto4_reserve_enabled())
                    primary.setdefault("transport_priority", "rpa_first")
                    attach_rpa_lock_meta(primary, lock_meta)
                    return primary

                payload = {
                    "ok": False,
                    "online": False,
                    "adapter": "none",
                    "scheme": "wechat_not_ready",
                    "state": "no_supported_wechat_transport",
                    "receive": {"ok": False},
                    "send": {"ok": False},
                    "primary_status": primary,
                    "reserve_status": reserve,
                    "wxauto4_reserve_enabled": self.wxauto4_reserve_enabled(),
                    "transport_priority": "rpa_first",
                    "weixin_process_running": any_weixin_process(),
                    "message": "No logged-in WeChat main window is available.",
                }
                attach_rpa_lock_meta(payload, lock_meta)
                return payload
        except TimeoutError as exc:
            return {
                "ok": False,
                "online": False,
                "adapter": "win32_ocr",
                "scheme": "wechat_not_ready",
                "state": "capabilities_lock_timeout",
                "receive": {"ok": False},
                "send": {"ok": False},
                "error": repr(exc),
                "transport_priority": "rpa_first",
                "rpa_lock": rpa_lock_timeout_payload(exc, action="capabilities", timeout_seconds=lock_timeout),
            }

    def wait_online(self, seconds: int = 60) -> dict[str, Any]:
        deadline = time.time() + max(1, seconds)
        latest: dict[str, Any] = {}
        while time.time() <= deadline:
            latest = self.status()
            if latest.get("ok") and latest.get("online"):
                return latest
            time.sleep(3)
        return latest

    def list_sessions(self, *, fresh: bool = False) -> dict[str, Any]:
        args = ["sessions"]
        if fresh:
            args.append("--fresh")
        env_overrides = interactive_rpa_probe_env() if fresh else None
        lock_timeout = rpa_lock_timeout_seconds("sessions", default=14.0)
        try:
            with wechat_rpa_lock("sessions", timeout_seconds=lock_timeout) as lock_meta:
                primary = self.call_compat_sidecar(args, allow_failure=True, env_overrides=env_overrides)
                if primary.get("ok"):
                    primary.setdefault("adapter", "win32_ocr")
                    primary.setdefault("transport_priority", "rpa_first")
                    attach_rpa_lock_meta(primary, lock_meta)
                    return primary
                if rpa_render_fault_should_stop(primary):
                    primary.setdefault("adapter", "win32_ocr")
                    primary.setdefault("transport_priority", "rpa_first")
                    attach_rpa_lock_meta(primary, lock_meta)
                    return primary
                if rpa_payload_is_tray_hidden(primary):
                    primary.setdefault("adapter", "win32_ocr")
                    primary.setdefault("transport_priority", "rpa_first")
                    attach_rpa_lock_meta(primary, lock_meta)
                    return primary
                reserve = self.call_reserve_sidecar(args, allow_failure=True, primary_payload=snapshot_payload(primary))
                if reserve.get("ok"):
                    reserve.setdefault("adapter", "wxauto4")
                    reserve.setdefault("transport_priority", "rpa_first")
                    reserve.setdefault("reserve_reason", "rpa_primary_unavailable")
                    attach_rpa_lock_meta(reserve, lock_meta)
                    return reserve
                primary.setdefault("wxauto4_reserve_status", reserve)
                primary.setdefault("transport_priority", "rpa_first")
                attach_rpa_lock_meta(primary, lock_meta)
                return primary
        except TimeoutError as exc:
            return {
                "ok": False,
                "online": bool(any_weixin_process()),
                "adapter": "win32_ocr",
                "state": "sessions_lock_timeout",
                "error": repr(exc),
                "transport_priority": "rpa_first",
                "rpa_lock": rpa_lock_timeout_payload(exc, action="sessions", timeout_seconds=lock_timeout),
            }

    def get_messages(
        self,
        target: str,
        exact: bool = True,
        history_load_times: int = 0,
        *,
        history_mode: str = "",
        anchor_ids: list[str] | None = None,
        anchor_content_keys: list[str] | None = None,
        reply_content_keys: list[str] | None = None,
        max_scroll_steps: int | None = None,
        max_duration_seconds: int | None = None,
        max_snapshots: int | None = None,
        min_delay_ms: int | None = None,
        max_delay_ms: int | None = None,
        restore_to_latest: bool | None = None,
        visible_only_target: bool = False,
        session_key: str = "",
    ) -> dict[str, Any]:
        args = ["messages", "--target", target]
        clean_session_key = str(session_key or "").strip()
        if clean_session_key:
            args.extend(["--session-key", clean_session_key])
        if exact:
            args.append("--exact")
        mode = str(history_mode or "").strip()
        if mode:
            args.extend(["--history-mode", mode])
        for value in anchor_ids or []:
            clean = str(value or "").strip()
            if clean:
                args.extend(["--anchor-id", clean])
        for value in anchor_content_keys or []:
            clean = str(value or "").strip()
            if clean:
                args.extend(["--anchor-content-key", clean])
        for value in reply_content_keys or []:
            clean = str(value or "").strip()
            if clean:
                args.extend(["--reply-content-key", clean])
        for key, value in (
            ("--max-scroll-steps", max_scroll_steps),
            ("--max-duration-seconds", max_duration_seconds),
            ("--max-snapshots", max_snapshots),
            ("--min-delay-ms", min_delay_ms),
            ("--max-delay-ms", max_delay_ms),
        ):
            if value is None:
                continue
            try:
                parsed = max(0, int(value))
            except (TypeError, ValueError):
                continue
            args.extend([key, str(parsed)])
        if restore_to_latest is not None:
            args.append("--restore-to-latest" if restore_to_latest else "--no-restore-to-latest")
        if history_load_times:
            try:
                load_times = max(0, int(history_load_times))
            except (TypeError, ValueError):
                load_times = 0
            if load_times:
                args.extend(["--history-load-times", str(load_times)])
        lock_timeout = rpa_lock_timeout_seconds("messages", default=14.0)
        env_overrides = visible_only_message_env() if visible_only_target else None
        try:
            with wechat_rpa_lock("messages", timeout_seconds=lock_timeout) as lock_meta:
                primary = self.call_compat_sidecar(args, allow_failure=True, env_overrides=env_overrides)
                if primary.get("ok"):
                    primary.setdefault("adapter", "win32_ocr")
                    primary.setdefault("transport_priority", "rpa_first")
                    attach_rpa_lock_meta(primary, lock_meta)
                    return inject_simulated_inbound_messages(primary, target=target)
                if rpa_render_fault_should_stop(primary):
                    primary.setdefault("adapter", "win32_ocr")
                    primary.setdefault("transport_priority", "rpa_first")
                    attach_rpa_lock_meta(primary, lock_meta)
                    return primary
                if visible_only_target and message_target_pending_visible(primary):
                    primary.setdefault("adapter", "win32_ocr")
                    primary.setdefault("transport_priority", "rpa_first")
                    primary["target_pending_visible"] = True
                    primary.setdefault("reason", "target_not_visible_waiting_for_unread")
                    primary.setdefault("safe_user_message", "当前不在可见会话列表里，待收到新消息时会自动识别")
                    attach_rpa_lock_meta(primary, lock_meta)
                    return primary
                if mode:
                    primary.setdefault("wxauto4_reserve_status", {"ok": False, "skipped": True, "reason": "history_mode_requires_win32_ocr"})
                    primary.setdefault("transport_priority", "rpa_first")
                    attach_rpa_lock_meta(primary, lock_meta)
                    return primary
                reserve = self.call_reserve_sidecar(args, allow_failure=True, primary_payload=snapshot_payload(primary))
                if reserve.get("ok"):
                    reserve.setdefault("adapter", "wxauto4")
                    reserve.setdefault("transport_priority", "rpa_first")
                    reserve.setdefault("reserve_reason", "rpa_primary_unavailable")
                    attach_rpa_lock_meta(reserve, lock_meta)
                    return inject_simulated_inbound_messages(reserve, target=target)
                primary.setdefault("wxauto4_reserve_status", reserve)
                primary.setdefault("transport_priority", "rpa_first")
                attach_rpa_lock_meta(primary, lock_meta)
                return primary
        except TimeoutError as exc:
            return {
                "ok": False,
                "online": bool(any_weixin_process()),
                "adapter": "win32_ocr",
                "state": "messages_lock_timeout",
                "target": target,
                "exact": exact,
                "history_mode": mode,
                "error": repr(exc),
                "transport_priority": "rpa_first",
                "rpa_lock": rpa_lock_timeout_payload(exc, action="messages", timeout_seconds=lock_timeout),
            }

    def send_text(
        self,
        target: str,
        text: str,
        exact: bool = True,
        *,
        skip_send_rate_guard: bool = False,
        artifact_dir: str | None = None,
        session_key: str = "",
    ) -> dict[str, Any]:
        if not target:
            raise WeChatConnectorError("target is required")
        if not text:
            raise WeChatConnectorError("text is required")
        send_started_at = time.time()
        send_started_iso = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(send_started_at))

        def _finish_send(payload: dict[str, Any], *, adapter_stage: str) -> dict[str, Any]:
            finished_at = time.time()
            if isinstance(payload, dict):
                timing = payload.get("timing") if isinstance(payload.get("timing"), dict) else {}
                payload["timing"] = {
                    **timing,
                    "send_started_at": send_started_iso,
                    "send_finished_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(finished_at)),
                    "send_duration_seconds": round(max(0.0, finished_at - send_started_at), 4),
                    "adapter_stage": adapter_stage,
                }
            return payload

        args = ["send", "--target", target, "--text", text]
        clean_session_key = str(session_key or "").strip()
        if clean_session_key:
            args.extend(["--session-key", clean_session_key])
        if exact:
            args.append("--exact")
        if skip_send_rate_guard:
            args.append("--skip-send-rate-guard")
        compat_args_list = list(args)
        if artifact_dir:
            compat_args_list.extend(["--artifact-dir", str(artifact_dir)])
        lock_timeout = rpa_lock_timeout_seconds("send", default=18.0)
        try:
            with wechat_rpa_lock("send", timeout_seconds=lock_timeout) as lock_meta:
                primary = self.call_compat_sidecar(compat_args_list, allow_failure=True, env_overrides=send_rpa_env())
                if primary.get("ok"):
                    primary.setdefault("adapter", "win32_ocr")
                    primary.setdefault("transport_priority", "rpa_first")
                    attach_rpa_lock_meta(primary, lock_meta)
                    return _finish_send(primary, adapter_stage="win32_ocr_primary")
                if rpa_payload_has_invalid_window_handle(primary):
                    primary["risk_stop_recommended"] = True
                    primary["risk_stop_reason"] = "win32_invalid_window_handle"
                    primary["risk_stop_message"] = "微信窗口句柄失效，已停止本次发送。请人工确认微信未掉线/未白屏后再恢复。"
                    primary.setdefault(
                        "wxauto4_reserve_status",
                        {
                            "ok": False,
                            "online": False,
                            "adapter": "wxauto4",
                            "state": "wxauto4_reserve_skipped_due_to_rpa_hard_stop",
                        },
                    )
                    primary.setdefault("transport_priority", "rpa_first")
                    attach_rpa_lock_meta(primary, lock_meta)
                    return _finish_send(primary, adapter_stage="win32_invalid_window_handle")
                if rpa_render_fault_should_stop(primary):
                    primary.setdefault("adapter", "win32_ocr")
                    primary.setdefault("transport_priority", "rpa_first")
                    attach_rpa_lock_meta(primary, lock_meta)
                    return _finish_send(primary, adapter_stage="win32_render_fault_stop")
                reserve = self.call_reserve_sidecar(args, allow_failure=True, primary_payload=snapshot_payload(primary))
                if reserve.get("ok"):
                    reserve.setdefault("adapter", "wxauto4")
                    reserve.setdefault("transport_priority", "rpa_first")
                    reserve.setdefault("reserve_reason", "rpa_primary_unavailable")
                    attach_rpa_lock_meta(reserve, lock_meta)
                    return _finish_send(reserve, adapter_stage="wxauto4_reserve")
                primary.setdefault("wxauto4_reserve_status", reserve)
                primary.setdefault("transport_priority", "rpa_first")
                attach_rpa_lock_meta(primary, lock_meta)
                return _finish_send(primary, adapter_stage="win32_ocr_primary_failed")
        except TimeoutError as exc:
            return _finish_send({
                "ok": False,
                "online": bool(any_weixin_process()),
                "adapter": "win32_ocr",
                "state": "send_lock_timeout",
                "target": target,
                "exact": exact,
                "error": repr(exc),
                "transport_priority": "rpa_first",
                "rpa_lock": rpa_lock_timeout_payload(exc, action="send", timeout_seconds=lock_timeout),
            }, adapter_stage="rpa_lock_timeout")

    def send_text_and_verify(
        self,
        target: str,
        text: str,
        exact: bool = True,
        *,
        simulate_inbound_file_transfer: bool = False,
        skip_send_rate_guard: bool = False,
        artifact_dir: str | None = None,
        session_key: str = "",
    ) -> dict[str, Any]:
        verify_started_at = time.time()
        verify_started_iso = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(verify_started_at))

        def _finish_verified(payload: dict[str, Any], *, verification_attempts: int = 0) -> dict[str, Any]:
            finished_at = time.time()
            if isinstance(payload, dict):
                timing = payload.get("timing") if isinstance(payload.get("timing"), dict) else {}
                payload["timing"] = {
                    **timing,
                    "send_verify_started_at": verify_started_iso,
                    "send_verify_finished_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(finished_at)),
                    "send_verify_duration_seconds": round(max(0.0, finished_at - verify_started_at), 4),
                    "verification_attempts": max(0, int(verification_attempts or 0)),
                }
            return payload

        loopback_inbound = bool(simulate_inbound_file_transfer and is_simulated_inbound_loopback_target(target))
        send_kwargs: dict[str, Any] = {
            "exact": exact,
            "skip_send_rate_guard": bool(loopback_inbound or skip_send_rate_guard),
        }
        if artifact_dir:
            send_kwargs["artifact_dir"] = artifact_dir
        clean_session_key = str(session_key or "").strip()
        if clean_session_key:
            send_kwargs["session_key"] = clean_session_key
        send_result = self.send_text(
            target,
            text,
            **send_kwargs,
        )
        if not send_result.get("ok"):
            return _finish_verified({"ok": False, "send": send_result, "verified": False})
        messages: dict[str, Any] = {}
        verified = False
        verification_mode = "messages"
        if env_flag("WECHAT_WIN32_OCR_FAST_SEND_CONFIRMATION", default=False) and guarded_send_confirmation_fallback(
            send_result,
            {},
        ):
            if loopback_inbound:
                enqueue_simulated_inbound_message(target=target, text=text)
            return _finish_verified({
                "ok": True,
                "send": send_result,
                "messages": {"ok": True, "state": "send_guard_confirmed_fast", "messages_skipped": True},
                "verified": True,
                "verification_mode": "send_guard_confirmed_fast",
            })
        verification_attempts = 0
        for attempt in range(6):
            verification_attempts = attempt + 1
            if attempt:
                time.sleep(1)
            messages = self.get_messages(target, exact=exact, session_key=clean_session_key)
            verified = verify_send_from_messages(messages, expected_text=text)
            if verified:
                break
            if blind_send_without_ocr(send_result, messages):
                break
        if not verified and blind_send_without_ocr(send_result, messages):
            verified = True
            verification_mode = "blind_send_no_ocr"
        if not verified and guarded_send_confirmation_fallback(send_result, messages):
            verified = True
            verification_mode = "send_guard_confirmed"
        if verified and loopback_inbound:
            enqueue_simulated_inbound_message(target=target, text=text)
        return _finish_verified({
            "ok": bool(verified),
            "send": send_result,
            "messages": messages,
            "verified": bool(verified),
            "verification_mode": verification_mode,
        }, verification_attempts=verification_attempts)

    def add_friend(
        self,
        *,
        phone: str = "",
        wechat: str = "",
        verify_message: str = "",
        remark_name: str = "",
        remark_code: str = "",
        artifact_dir: str | None = None,
    ) -> dict[str, Any]:
        query = re.sub(r"\D+", "", str(phone or "")) or str(wechat or "").strip()
        if not query:
            raise WeChatConnectorError("phone or wechat is required")
        if not str(verify_message or "").strip():
            raise WeChatConnectorError("verify_message is required")
        if not str(remark_name or "").strip():
            raise WeChatConnectorError("remark_name is required")
        if not str(remark_code or "").strip():
            raise WeChatConnectorError("remark_code is required")
        if str(remark_code).strip() not in str(remark_name).strip():
            raise WeChatConnectorError("remark_name must include remark_code")
        args = ["add-friend-entry-click-plan-windows"]
        if phone:
            args.extend(["--phone", str(phone)])
        if wechat:
            args.extend(["--wechat", str(wechat)])
        args.extend(["--verify-message", str(verify_message)])
        args.extend(["--remark-name", str(remark_name)])
        args.extend(["--remark-code", str(remark_code)])
        if artifact_dir:
            args.extend(["--artifact-dir", str(artifact_dir)])
        lock_timeout = rpa_lock_timeout_seconds("add_friend", default=45.0)
        try:
            with wechat_rpa_lock("add_friend", timeout_seconds=lock_timeout) as lock_meta:
                primary = self.call_compat_sidecar(args, allow_failure=True, env_overrides=add_friend_rpa_env())
                primary.setdefault("adapter", "win32_ocr")
                primary.setdefault("transport_priority", "rpa_first")
                primary.setdefault(
                    "wxauto4_reserve_status",
                    {
                        "ok": False,
                        "online": False,
                        "adapter": "wxauto4",
                        "state": "wxauto4_reserve_skipped_for_add_friend",
                        "message": "add_friend is implemented through the Win32/OCR RPA transport.",
                    },
                )
                attach_rpa_lock_meta(primary, lock_meta)
                return primary
        except TimeoutError as exc:
            return {
                "ok": False,
                "online": bool(any_weixin_process()),
                "adapter": "win32_ocr",
                "state": "add_friend_lock_timeout",
                "task_type": "add_friend",
                "phone": phone,
                "wechat": wechat,
                "error": repr(exc),
                "transport_priority": "rpa_first",
                "rpa_lock": rpa_lock_timeout_payload(exc, action="add_friend", timeout_seconds=lock_timeout),
            }

    def require_online(self) -> dict[str, Any]:
        status = self.status(interactive=True)
        if not status.get("ok") or not status.get("online"):
            raise WeChatConnectorError(
                "WeChat is not online; open and log in to the main window first. "
                f"status={status!r}"
            )
        return status

    def ensure_wechat_started(self) -> None:
        """Compatibility helper; startup is deliberately manual."""
        status = self.status()
        if status.get("ok") and status.get("online"):
            return
        raise WeChatConnectorError(
            "Automatic WeChat startup is disabled. Open WeChat, finish login manually, "
            "and keep the main window visible before running the workflow."
        )

    def call_sidecar(self, args: list[str], allow_failure: bool = False) -> dict[str, Any]:
        if not self.sidecar_python.exists():
            raise FileNotFoundError(str(self.sidecar_python))
        if not self.sidecar_script.exists():
            raise FileNotFoundError(str(self.sidecar_script))
        return self._call_daemon(args, allow_failure)

    def call_reserve_sidecar(
        self,
        args: list[str],
        *,
        allow_failure: bool = False,
        primary_payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not self.wxauto4_reserve_enabled():
            payload: dict[str, Any] = {
                "ok": False,
                "online": False,
                "adapter": "wxauto4",
                "state": "wxauto4_reserve_disabled",
                "message": "wxauto4 reserve is disabled; enable WECHAT_ENABLE_WXAUTO4=1 to use it.",
            }
            if primary_payload is not None:
                payload["primary_status"] = snapshot_payload(primary_payload)
                payload.setdefault("reserve_reason", "rpa_primary_unavailable")
            return payload

        try:
            payload = self.call_sidecar(args, allow_failure=allow_failure)
        except Exception as exc:
            payload = {
                "ok": False,
                "online": False,
                "adapter": "wxauto4",
                "state": "wxauto4_reserve_call_failed",
                "error": repr(exc),
            }
        payload.setdefault("adapter", "wxauto4")
        if primary_payload is not None:
            payload["primary_status"] = snapshot_payload(primary_payload)
            payload.setdefault("reserve_reason", "rpa_primary_unavailable")
        if not payload.get("ok") and not allow_failure:
            payload.setdefault("error", "wxauto4_reserve_command_failed")
        return payload

    def call_compat_sidecar(
        self,
        args: list[str],
        *,
        allow_failure: bool = False,
        primary_payload: dict[str, Any] | None = None,
        env_overrides: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        if not self.compat_sidecar_script.exists():
            payload = {
                "ok": False,
                "online": False,
                "adapter": "win32_ocr",
                "state": "compat_sidecar_missing",
                "error": f"compat sidecar missing: {self.compat_sidecar_script}",
            }
            if primary_payload is not None:
                payload["primary_status"] = snapshot_payload(primary_payload)
            if not allow_failure:
                payload.setdefault("error", "compat_sidecar_missing")
            return payload

        use_daemon = os.getenv("WECHAT_WIN32_OCR_DAEMON_ENABLED", "").strip().lower() not in {"0", "false", "no", "off"}
        if use_daemon:
            payload = self._call_compat_daemon(args, allow_failure=allow_failure, env_overrides=env_overrides)
            if payload.get("ok") or payload.get("state") not in {"compat_daemon_call_failed", "compat_daemon_invalid_response"}:
                if primary_payload is not None:
                    payload["primary_status"] = snapshot_payload(primary_payload)
                    payload.setdefault("compat_reason", "primary_adapter_failed")
                if not payload.get("ok") and not allow_failure:
                    payload.setdefault("error", "compat_command_failed")
                return payload

        payload = self._call_compat_oneshot(args, allow_failure=allow_failure, env_overrides=env_overrides)
        if primary_payload is not None:
            payload["primary_status"] = snapshot_payload(primary_payload)
            payload.setdefault("compat_reason", "primary_adapter_failed")
        if not payload.get("ok") and not allow_failure:
            payload.setdefault("error", "compat_command_failed")
        return payload

    def _call_compat_oneshot(
        self,
        args: list[str],
        *,
        allow_failure: bool,
        env_overrides: dict[str, str] | None,
    ) -> dict[str, Any]:
        python = self.compat_sidecar_python if self.compat_sidecar_python.exists() else Path(sys.executable)
        cmd = [str(python), str(self.compat_sidecar_script), *compat_args(args)]
        env = os.environ.copy()
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONPATH"] = str(self.root)
        if env_overrides:
            env.update({str(key): str(value) for key, value in env_overrides.items()})
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(self.root),
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=min(max(10, self.timeout_seconds), 120),
            )
        except Exception as exc:
            return {"ok": False, "online": False, "adapter": "win32_ocr", "state": "compat_call_failed", "error": repr(exc)}

        payload = parse_json_object(proc.stdout)
        if not isinstance(payload, dict):
            payload = {
                "ok": False,
                "online": False,
                "adapter": "win32_ocr",
                "state": "compat_invalid_response",
                "error": "compat sidecar did not return JSON",
                "stdout": proc.stdout[-2000:],
                "stderr": proc.stderr[-2000:],
                "returncode": proc.returncode,
            }
        payload.setdefault("adapter", "win32_ocr")
        if proc.returncode != 0 and payload.get("ok"):
            payload["returncode_warning"] = proc.returncode
        elif proc.returncode != 0:
            payload.setdefault("returncode", proc.returncode)
            if proc.stderr:
                payload.setdefault("stderr", proc.stderr[-2000:])
        if not payload.get("ok") and not allow_failure:
            payload.setdefault("error", "compat_command_failed")
        return payload

    def _call_compat_daemon(
        self,
        args: list[str],
        *,
        allow_failure: bool,
        env_overrides: dict[str, str] | None,
    ) -> dict[str, Any]:
        request = _args_to_request(args)
        if env_overrides:
            request["_env_overrides"] = {str(key): str(value) for key, value in env_overrides.items()}
        try:
            payload = _compat_daemon_request(
                compat_sidecar_python=self.compat_sidecar_python,
                compat_sidecar_script=self.compat_sidecar_script,
                root=self.root,
                request=request,
                timeout=min(max(10, self.timeout_seconds), 120),
            )
        except Exception as exc:
            payload = {
                "ok": False,
                "online": False,
                "adapter": "win32_ocr",
                "state": "compat_daemon_call_failed",
                "error": repr(exc),
            }
        payload.setdefault("adapter", "win32_ocr")
        if not payload.get("ok") and not allow_failure:
            payload.setdefault("error", "compat_command_failed")
        return payload

    def _call_daemon(self, args: list[str], allow_failure: bool = False) -> dict[str, Any]:
        global _daemon_proc
        with _daemon_lock:
            proc = _ensure_daemon(self.sidecar_python, self.sidecar_script, self.root)

            request = _args_to_request(args)
            request_line = json.dumps(request, ensure_ascii=True) + "\n"

            try:
                proc.stdin.write(request_line.encode("utf-8"))
                proc.stdin.flush()
                payload = _read_json_response(proc, timeout=25)
            except Exception:
                # Daemon may have died; kill, restart, and retry once.
                _kill_daemon()
                proc = _ensure_daemon(self.sidecar_python, self.sidecar_script, self.root)
                proc.stdin.write(request_line.encode("utf-8"))
                proc.stdin.flush()
                payload = _read_json_response(proc, timeout=25)

            if not payload.get("ok") and not allow_failure:
                payload.setdefault("error", "daemon_command_failed")
            return payload

    def _retry_recoverable_rpa_probe(
        self,
        args: list[str],
        primary: dict[str, Any],
        *,
        interactive: bool,
        action: str,
    ) -> dict[str, Any]:
        """Retry user-initiated RPA probes when the first result is recoverable.

        Passive background probes must remain non-invasive. Start buttons and
        explicit preflight checks, however, are allowed to restore the WeChat
        window before reporting that WeChat is unavailable.
        """
        if primary.get("ok") and primary.get("online"):
            return primary
        if not interactive:
            return primary
        if rpa_payload_needs_render_recovery(primary):
            if not auto_render_recovery_enabled():
                primary["rpa_recovery"] = {
                    "ok": False,
                    "action": action,
                    "mode": "render_recovery_disabled_stop_and_report",
                    "reason": "auto_render_recovery_disabled",
                    "initial_status": snapshot_payload(primary),
                }
                return primary
            recovery = self.call_compat_sidecar(
                ["recover-render"],
                allow_failure=True,
                env_overrides=interactive_rpa_probe_env(),
            )
            if recovery.get("ok") and recovery.get("online"):
                retry = self.call_compat_sidecar(
                    args,
                    allow_failure=True,
                    env_overrides=interactive_rpa_probe_env(),
                )
                if retry.get("ok") and retry.get("online"):
                    retry.setdefault("adapter", "win32_ocr")
                    retry.setdefault("transport_priority", "rpa_first")
                    retry["rpa_recovery"] = {
                        "ok": True,
                        "action": action,
                        "mode": "interactive_blank_render_tray_redraw",
                        "initial_status": snapshot_payload(primary),
                        "recovery_status": snapshot_payload(recovery),
                    }
                    return retry
            primary["rpa_recovery"] = {
                "ok": False,
                "action": action,
                "mode": "interactive_blank_render_tray_redraw",
                "recovery_status": snapshot_payload(recovery),
            }
            return primary
        if not rpa_payload_needs_interactive_confirmation(primary):
            return primary

        delay = env_float("WECHAT_RPA_INTERACTIVE_CONFIRM_DELAY_SECONDS", 0.35)
        if delay > 0:
            time.sleep(min(delay, 2.0))
        retry = self.call_compat_sidecar(
            args,
            allow_failure=True,
            env_overrides=interactive_rpa_probe_env(),
        )
        if retry.get("ok") and retry.get("online"):
            retry.setdefault("adapter", "win32_ocr")
            retry.setdefault("transport_priority", "rpa_first")
            retry["rpa_recovery"] = {
                "ok": True,
                "action": action,
                "mode": "interactive_confirm_after_recoverable_failure",
                "initial_status": snapshot_payload(primary),
            }
            return retry
        primary["rpa_recovery"] = {
            "ok": False,
            "action": action,
            "mode": "interactive_confirm_after_recoverable_failure",
            "confirm_status": snapshot_payload(retry),
        }
        return primary


@contextmanager
def wechat_rpa_lock(action: str, *, timeout_seconds: float = 90.0, stale_seconds: float = 180.0):
    """Serialize desktop WeChat RPA/OCR calls across processes.

    wxauto4 and the Win32/OCR fallback both manipulate the same foreground
    WeChat window. Without a process-wide lock, recorder screenshots can race
    with automated sends and produce missed or partial captures.
    """
    if os.getenv("WECHAT_RPA_LOCK_DISABLED", "").strip().lower() in {"1", "true", "yes", "on"}:
        yield {
            "action": action,
            "disabled": True,
            "timeout_seconds": round(max(1.0, float(timeout_seconds)), 3),
            "waited_seconds": 0.0,
            "attempts": 1,
            "stale_breaks": 0,
        }
        return
    lock_path = WECHAT_RPA_LOCK_PATH
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.time() + max(1.0, float(timeout_seconds))
    payload = {"pid": os.getpid(), "action": action, "created_at": time.time()}
    acquire_started = time.perf_counter()
    acquired = False
    attempts = 0
    stale_breaks = 0
    while time.time() <= deadline:
        attempts += 1
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            try:
                os.write(fd, json.dumps(payload, ensure_ascii=False).encode("utf-8"))
            finally:
                os.close(fd)
            acquired = True
            break
        except FileExistsError:
            if should_break_wechat_rpa_lock(lock_path, stale_seconds=stale_seconds):
                stale_breaks += 1
                try:
                    lock_path.unlink()
                except FileNotFoundError:
                    pass
                except OSError:
                    time.sleep(0.2)
                    continue
            else:
                time.sleep(0.2)
    if not acquired:
        waited_seconds = max(0.0, time.perf_counter() - acquire_started)
        raise RPALockTimeoutError(
            f"WeChat RPA lock timeout for action={action}",
            meta={
                "action": str(action or ""),
                "timeout_seconds": round(max(1.0, float(timeout_seconds)), 3),
                "waited_seconds": round(waited_seconds, 3),
                "attempts": max(1, int(attempts)),
                "stale_breaks": max(0, int(stale_breaks)),
            },
        )
    lock_meta = {
        "action": str(action or ""),
        "timeout_seconds": round(max(1.0, float(timeout_seconds)), 3),
        "waited_seconds": round(max(0.0, time.perf_counter() - acquire_started), 3),
        "attempts": max(1, int(attempts)),
        "stale_breaks": max(0, int(stale_breaks)),
    }
    try:
        yield lock_meta
    finally:
        try:
            current = json.loads(lock_path.read_text(encoding="utf-8"))
        except Exception:
            current = {}
        if int(current.get("pid") or 0) == os.getpid():
            try:
                lock_path.unlink()
            except FileNotFoundError:
                pass


def should_break_wechat_rpa_lock(path: Path, *, stale_seconds: float) -> bool:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return True
    pid = int(payload.get("pid") or 0)
    created_at = float(payload.get("created_at") or 0)
    if pid and not psutil.pid_exists(pid):
        return True
    if created_at and time.time() - created_at > stale_seconds:
        return True
    return False


def attach_rpa_lock_meta(payload: dict[str, Any], lock_meta: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return payload
    clean = dict(lock_meta or {})
    if not clean:
        return payload
    existing = payload.get("rpa_lock")
    if isinstance(existing, dict):
        merged = dict(clean)
        merged.update({key: value for key, value in existing.items() if key not in merged})
    else:
        merged = clean
    payload["rpa_lock"] = merged
    return payload


def rpa_lock_timeout_payload(exc: TimeoutError, *, action: str, timeout_seconds: float) -> dict[str, Any]:
    payload = {
        "action": str(action or ""),
        "timeout_seconds": round(max(1.0, float(timeout_seconds)), 3),
    }
    if isinstance(exc, RPALockTimeoutError):
        payload.update({key: value for key, value in (exc.meta or {}).items() if value is not None})
    return payload


def _read_json_response(proc: subprocess.Popen, timeout: int = 25) -> dict[str, Any]:
    output: queue.Queue[tuple[str, Any]] = queue.Queue(maxsize=1)

    def read_worker() -> None:
        try:
            while True:
                if proc.poll() is not None:
                    output.put(("error", RuntimeError("daemon_process_died")))
                    return
                line = proc.stdout.readline()
                if not line:
                    output.put(("error", RuntimeError("daemon_stdout_closed")))
                    return
                clean = line.decode("utf-8", errors="replace").strip()
                if not clean:
                    continue
                try:
                    output.put(("payload", json.loads(clean)))
                    return
                except json.JSONDecodeError:
                    # Skip non-JSON lines (e.g., library warnings on startup)
                    continue
        except Exception as exc:
            output.put(("error", exc))

    thread = threading.Thread(target=read_worker, daemon=True)
    thread.start()
    try:
        kind, value = output.get(timeout=max(1, timeout))
    except queue.Empty as exc:
        raise TimeoutError("daemon_response_timeout") from exc
    if kind == "payload":
        return value
    if isinstance(value, Exception):
        raise value
    raise RuntimeError(str(value))


def _ensure_daemon(
    sidecar_python: Path,
    sidecar_script: Path,
    root: Path,
) -> subprocess.Popen:
    global _daemon_proc
    if _daemon_proc is not None:
        if _daemon_proc.poll() is None:
            return _daemon_proc
        _kill_daemon()

    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    _daemon_proc = subprocess.Popen(
        [str(sidecar_python), str(sidecar_script), "--daemon"],
        cwd=str(root),
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return _daemon_proc


def _kill_daemon() -> None:
    global _daemon_proc
    proc = _daemon_proc
    _daemon_proc = None
    if proc is not None:
        try:
            if proc.poll() is None and proc.stdin is not None:
                try:
                    proc.stdin.write(b'{"action": "exit"}\n')
                    proc.stdin.flush()
                except (BrokenPipeError, OSError, ValueError):
                    pass
                proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
                proc.wait(timeout=2)
            except Exception:
                pass
        except Exception:
            try:
                if proc.poll() is None:
                    proc.kill()
            except Exception:
                pass
        finally:
            for pipe in (proc.stdin, proc.stdout, proc.stderr):
                try:
                    if pipe is not None:
                        pipe.close()
                except (OSError, ValueError):
                    pass


def _compat_daemon_env(
    *,
    root: Path,
) -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONPATH"] = str(root)
    return env


def _ensure_compat_daemon(
    compat_sidecar_python: Path,
    compat_sidecar_script: Path,
    root: Path,
) -> subprocess.Popen:
    global _compat_daemon_proc
    if _compat_daemon_proc is not None:
        if _compat_daemon_proc.poll() is None:
            return _compat_daemon_proc
        _kill_compat_daemon()

    python = compat_sidecar_python if compat_sidecar_python.exists() else Path(sys.executable)
    env = _compat_daemon_env(root=root)
    _compat_daemon_proc = subprocess.Popen(
        [str(python), str(compat_sidecar_script), "--daemon"],
        cwd=str(root),
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return _compat_daemon_proc


def _kill_compat_daemon() -> None:
    global _compat_daemon_proc
    proc = _compat_daemon_proc
    _compat_daemon_proc = None
    if proc is not None:
        try:
            if proc.poll() is None and proc.stdin is not None:
                try:
                    proc.stdin.write(b'{"action": "exit"}\n')
                    proc.stdin.flush()
                except (BrokenPipeError, OSError, ValueError):
                    pass
                proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
                proc.wait(timeout=2)
            except Exception:
                pass
        except Exception:
            try:
                if proc.poll() is None:
                    proc.kill()
            except Exception:
                pass
        finally:
            for pipe in (proc.stdin, proc.stdout, proc.stderr):
                try:
                    if pipe is not None:
                        pipe.close()
                except (OSError, ValueError):
                    pass


def _compat_daemon_request(
    *,
    compat_sidecar_python: Path,
    compat_sidecar_script: Path,
    root: Path,
    request: dict[str, Any],
    timeout: int,
) -> dict[str, Any]:
    with _compat_daemon_lock:
        proc = _ensure_compat_daemon(compat_sidecar_python, compat_sidecar_script, root)
        request_line = json.dumps(request, ensure_ascii=True) + "\n"
        try:
            proc.stdin.write(request_line.encode("utf-8"))
            proc.stdin.flush()
            payload = _read_json_response(proc, timeout=max(1, int(timeout)))
        except Exception:
            _kill_compat_daemon()
            proc = _ensure_compat_daemon(compat_sidecar_python, compat_sidecar_script, root)
            proc.stdin.write(request_line.encode("utf-8"))
            proc.stdin.flush()
            payload = _read_json_response(proc, timeout=max(1, int(timeout)))
        if not isinstance(payload, dict):
            return {
                "ok": False,
                "online": False,
                "adapter": "win32_ocr",
                "state": "compat_daemon_invalid_response",
                "error": "compat daemon did not return JSON",
            }
        return payload


def reset_wxauto_sidecar_daemon() -> None:
    """Force the cached wxauto4 sidecar to restart on the next connector call."""
    _kill_daemon()
    _kill_compat_daemon()


def interactive_rpa_probe_env() -> dict[str, str]:
    """Environment overrides for user-initiated startup checks.

    Passive probes are intentionally safe for background health checks, but a
    start button should actively restore the WeChat window before judging
    whether the RPA transport is available.
    """
    return {
        "WECHAT_WIN32_OCR_PASSIVE_PROBE": "0",
        "WECHAT_WIN32_OCR_AGGRESSIVE_FOCUS": "1",
        "WECHAT_WIN32_OCR_ATTACH_THREAD_INPUT": "1",
        # Startup/resume preflight must actually bring WeChat to foreground.
        # Disable activate-window debounce for interactive probes so rapid
        # repeated checks do not short-circuit a required foreground raise.
        "WECHAT_WIN32_OCR_ACTIVATE_DEBOUNCE_SECONDS": "0",
    }


def visible_only_message_env() -> dict[str, str]:
    """Disable sidebar search during safe startup/bootstrap target checks."""
    return {
        "WECHAT_WIN32_OCR_TARGET_SEARCH_FALLBACK": "0",
        "WECHAT_WIN32_OCR_TARGET_SEARCH_ENTER_FALLBACK": "0",
    }


def send_rpa_env() -> dict[str, str]:
    env = interactive_rpa_probe_env()
    env["WECHAT_WIN32_OCR_STRICT_SEND_FOCUS_GUARD"] = "1"
    env["WECHAT_WIN32_OCR_ALLOW_UNKNOWN_FOREGROUND"] = "1"
    if not str(os.getenv("WECHAT_WIN32_OCR_BLANK_INPUT_FOCUS_RETRY") or "").strip():
        # Keep the product-level single-confirm rule, but allow one extra
        # candidate input point when the first attempt leaves the input region
        # visibly blank. This avoids blind sends after a missed input focus
        # without reopening or repeatedly clicking the conversation row.
        env["WECHAT_WIN32_OCR_BLANK_INPUT_FOCUS_RETRY"] = "1"
    if not str(os.getenv("WECHAT_WIN32_OCR_SEND_INPUT_CONFIRM_ATTEMPTS") or "").strip():
        env["WECHAT_WIN32_OCR_SEND_INPUT_CONFIRM_ATTEMPTS"] = "1"
    input_fast_visual_confirm = str(os.getenv("WECHAT_WIN32_OCR_INPUT_FAST_VISUAL_CONFIRM") or "").strip()
    if input_fast_visual_confirm:
        env["WECHAT_WIN32_OCR_INPUT_FAST_VISUAL_CONFIRM"] = input_fast_visual_confirm
    return env


def add_friend_rpa_env() -> dict[str, str]:
    """Environment overrides for add_friend RPA.

    add_friend needs foreground input, but it must not use invasive render
    recovery by default. If WeChat is blank or unreadable, report and stop.
    """
    env = interactive_rpa_probe_env()
    env["WECHAT_WIN32_OCR_QUICK_LOGIN_AUTO_ENTER"] = "0"
    env["WECHAT_WIN32_OCR_WINDOW_NORMALIZE"] = os.getenv("WECHAT_WIN32_OCR_WINDOW_NORMALIZE", "0") or "0"
    env["WECHAT_WIN32_OCR_RENDER_RECOVERY_AUTO"] = os.getenv("WECHAT_WIN32_OCR_RENDER_RECOVERY_AUTO", "0") or "0"
    return env


def message_target_pending_visible(payload: dict[str, Any] | None) -> bool:
    data = payload if isinstance(payload, dict) else {}
    state = str(data.get("state") or "")
    if state in {"target_not_visible_for_messages", "target_not_confirmed_for_messages"}:
        return not bool(data.get("opened"))
    reason = str(data.get("reason") or "")
    if reason in {"target_not_visible_waiting_for_unread", "target_not_visible"}:
        return True
    guard = data.get("guard") if isinstance(data.get("guard"), dict) else {}
    guard_state = str(guard.get("state") or "")
    guard_reason = str(guard.get("reason") or "")
    return guard_state in {"target_not_confirmed", "target_not_visible"} or guard_reason in {
        "target_not_confirmed",
        "target_not_visible",
    }


def _args_to_request(args: list[str]) -> dict[str, Any]:
    request: dict[str, Any] = {"resize": True}
    if not args:
        request["action"] = "status"
        return request
    if args[0] == "status":
        request["action"] = "status"
    elif args[0] == "sessions":
        request["action"] = "sessions"
        if "--fresh" in args:
            request["fresh"] = True
    elif args[0] == "messages":
        request["action"] = "messages"
        for i, arg in enumerate(args):
            if arg == "--target" and i + 1 < len(args):
                request["target"] = args[i + 1]
            elif arg == "--session-key" and i + 1 < len(args):
                request["session_key"] = args[i + 1]
            elif arg == "--exact":
                request["exact"] = True
            elif arg == "--history-load-times" and i + 1 < len(args):
                try:
                    request["history_load_times"] = max(0, int(args[i + 1]))
                except ValueError:
                    request["history_load_times"] = 0
            elif arg == "--history-mode" and i + 1 < len(args):
                request["history_mode"] = args[i + 1]
            elif arg == "--anchor-id" and i + 1 < len(args):
                request.setdefault("anchor_ids", []).append(args[i + 1])
            elif arg == "--anchor-content-key" and i + 1 < len(args):
                request.setdefault("anchor_content_keys", []).append(args[i + 1])
            elif arg == "--reply-content-key" and i + 1 < len(args):
                request.setdefault("reply_content_keys", []).append(args[i + 1])
            elif arg in {"--max-scroll-steps", "--max-duration-seconds", "--max-snapshots", "--min-delay-ms", "--max-delay-ms"} and i + 1 < len(args):
                key = arg.lstrip("-").replace("-", "_")
                try:
                    request[key] = max(0, int(args[i + 1]))
                except ValueError:
                    request[key] = 0
            elif arg == "--restore-to-latest":
                request["restore_to_latest"] = True
            elif arg == "--no-restore-to-latest":
                request["restore_to_latest"] = False
            elif arg == "--artifact-dir" and i + 1 < len(args):
                request["artifact_dir"] = args[i + 1]
    elif args[0] == "send":
        request["action"] = "send"
        for i, arg in enumerate(args):
            if arg == "--target" and i + 1 < len(args):
                request["target"] = args[i + 1]
            elif arg == "--text" and i + 1 < len(args):
                request["text"] = args[i + 1]
            elif arg == "--session-key" and i + 1 < len(args):
                request["session_key"] = args[i + 1]
            elif arg == "--exact":
                request["exact"] = True
            elif arg == "--skip-send-rate-guard":
                request["skip_send_rate_guard"] = True
            elif arg == "--artifact-dir" and i + 1 < len(args):
                request["artifact_dir"] = args[i + 1]
    elif args[0] in ADD_FRIEND_ROUTES:
        request["action"] = args[0]
        for i, arg in enumerate(args):
            if arg == "--phone" and i + 1 < len(args):
                request["phone"] = args[i + 1]
            elif arg == "--wechat" and i + 1 < len(args):
                request["wechat"] = args[i + 1]
            elif arg == "--verify-message" and i + 1 < len(args):
                request["verify_message"] = args[i + 1]
            elif arg == "--remark-name" and i + 1 < len(args):
                request["remark_name"] = args[i + 1]
            elif arg == "--remark-code" and i + 1 < len(args):
                request["remark_code"] = args[i + 1]
            elif arg == "--artifact-dir" and i + 1 < len(args):
                request["artifact_dir"] = args[i + 1]
    elif args[0] in {"status", "capabilities", "sessions", "recover-render"}:
        request["action"] = args[0]
        for i, arg in enumerate(args):
            if arg == "--artifact-dir" and i + 1 < len(args):
                request["artifact_dir"] = args[i + 1]
    return request


def compat_args(args: list[str]) -> list[str]:
    """Convert connector args to the one-shot Win32/OCR fallback CLI.

    The compatibility sidecar does not maintain a daemon cache, so ``--fresh``
    is implicit and omitted.
    """
    converted: list[str] = []
    skip_next = False
    for index, arg in enumerate(args):
        if skip_next:
            skip_next = False
            continue
        if arg == "--fresh":
            continue
        converted.append(arg)
        if arg in {
            "--target",
            "--text",
            "--session-key",
            "--phone",
            "--wechat",
            "--history-load-times",
            "--history-mode",
            "--anchor-id",
            "--anchor-content-key",
            "--reply-content-key",
            "--max-scroll-steps",
            "--max-duration-seconds",
            "--max-snapshots",
            "--min-delay-ms",
            "--max-delay-ms",
        } and index + 1 < len(args):
            converted.append(args[index + 1])
            skip_next = True
    artifact_dir = str(os.getenv("WECHAT_WIN32_OCR_ARTIFACT_DIR") or "").strip()
    if artifact_dir and "--artifact-dir" not in converted:
        converted.extend(["--artifact-dir", artifact_dir])
    return converted


def parse_json_object(text: str) -> dict[str, Any] | None:
    clean = str(text or "").strip()
    if not clean:
        return None
    try:
        payload = json.loads(clean)
        return payload if isinstance(payload, dict) else None
    except json.JSONDecodeError:
        pass

    # Some OCR libraries may print initialization lines. Parse the last JSON
    # object from stdout instead of failing the entire fallback path.
    start = clean.rfind("{")
    while start >= 0:
        candidate = clean[start:]
        try:
            payload = json.loads(candidate)
            return payload if isinstance(payload, dict) else None
        except json.JSONDecodeError:
            start = clean.rfind("{", 0, start)
    return None


def any_weixin_process() -> bool:
    for proc in psutil.process_iter(["name"]):
        try:
            if str(proc.info.get("name") or "").lower() == "weixin.exe":
                return True
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return False


def env_flag(name: str, *, default: bool = False) -> bool:
    raw = os.getenv(name, "")
    if raw is None or raw.strip() == "":
        return bool(default)
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def env_float(name: str, default: float) -> float:
    raw = os.getenv(name, "")
    if raw is None or raw.strip() == "":
        return float(default)
    try:
        return float(raw)
    except ValueError:
        return float(default)


def auto_render_recovery_enabled() -> bool:
    """Explicit opt-in for invasive WeChat render recovery.

    Blank-render recovery closes/reopens the WeChat shell and may create quick
    login windows. Keep it disabled in normal live RPA; report instead.
    """
    return env_flag("WECHAT_WIN32_OCR_RENDER_RECOVERY_AUTO", default=False)


def rpa_lock_timeout_seconds(action: str, *, default: float) -> float:
    """Resolve per-action RPA lock timeout with environment override.

    Env key format:
    - WECHAT_RPA_LOCK_TIMEOUT_<ACTION>_SECONDS
    Example:
    - WECHAT_RPA_LOCK_TIMEOUT_MESSAGES_SECONDS=18
    """
    key = f"WECHAT_RPA_LOCK_TIMEOUT_{str(action or '').strip().upper()}_SECONDS"
    raw = os.getenv(key, "")
    if raw is None or raw.strip() == "":
        return float(max(1.0, min(float(default), 120.0)))
    try:
        value = float(raw)
    except ValueError:
        value = float(default)
    return float(max(1.0, min(value, 120.0)))


def rpa_payload_needs_interactive_confirmation(payload: dict[str, Any]) -> bool:
    """Return True for RPA failures that a foreground restore can fix."""
    if not isinstance(payload, dict):
        return False
    if payload.get("ok") and payload.get("online"):
        return False
    if rpa_payload_is_tray_hidden(payload):
        return False
    if rpa_payload_needs_render_recovery(payload):
        return True

    state = str(payload.get("state") or "").strip()
    reason = str(payload.get("reason") or "").strip()
    scheme = str(payload.get("scheme") or "").strip()
    if state == "main_window_geometry_invalid" and reason in {
        "window_offscreen_or_minimized",
        "window_too_small_for_capture",
    }:
        return True
    if scheme == "win32_ocr_window_geometry_invalid" and reason in {
        "window_offscreen_or_minimized",
        "window_too_small_for_capture",
    }:
        return True

    geometry = payload.get("geometry") if isinstance(payload.get("geometry"), dict) else {}
    if geometry:
        left = int(geometry.get("left") or 0)
        top = int(geometry.get("top") or 0)
        width = int(geometry.get("width") or 0)
        height = int(geometry.get("height") or 0)
        if left <= -30000 or top <= -30000:
            return True
        if 0 < width < 420 or 0 < height < 260:
            return True

    probe = payload.get("window_probe") if isinstance(payload.get("window_probe"), dict) else {}
    if state == "main_window_not_found" and int(probe.get("main_count") or 0) > 0:
        return True
    return False


def rpa_payload_is_tray_hidden(payload: dict[str, Any]) -> bool:
    if not isinstance(payload, dict):
        return False
    state = str(payload.get("state") or "").strip()
    reason = str(payload.get("reason") or "").strip()
    scheme = str(payload.get("scheme") or "").strip()
    if state == "main_window_in_tray" or reason == "wechat_window_in_tray" or scheme == "win32_ocr_window_in_tray":
        return True
    primary = payload.get("primary_status") if isinstance(payload.get("primary_status"), dict) else {}
    if primary:
        return rpa_payload_is_tray_hidden(primary)
    return False


def rpa_payload_is_blank_render(payload: dict[str, Any]) -> bool:
    if not isinstance(payload, dict):
        return False
    state = str(payload.get("state") or "").strip()
    reason = str(payload.get("reason") or "").strip()
    scheme = str(payload.get("scheme") or "").strip()
    if state == "blank_render_detected" or reason == "blank_render" or scheme == "win32_ocr_blank_render":
        return True
    primary = payload.get("primary_status") if isinstance(payload.get("primary_status"), dict) else {}
    if primary:
        return rpa_payload_is_blank_render(primary)
    return False


def rpa_payload_needs_render_recovery(payload: dict[str, Any]) -> bool:
    if not isinstance(payload, dict):
        return False
    if rpa_payload_is_blank_render(payload):
        return True
    state = str(payload.get("state") or "").strip()
    reason = str(payload.get("reason") or "").strip()
    scheme = str(payload.get("scheme") or "").strip()
    shell_probe = payload.get("shell_probe") if isinstance(payload.get("shell_probe"), dict) else {}
    shell_reason = str(shell_probe.get("reason") or "").strip()
    sparse_shell = (
        state == "auxiliary_shell_window_detected"
        or reason == "auxiliary_shell_window"
        or scheme == "win32_ocr_auxiliary_shell"
    )
    if sparse_shell and shell_reason in {"sparse_auxiliary_shell", "title_only_shell"}:
        return True
    primary = payload.get("primary_status") if isinstance(payload.get("primary_status"), dict) else {}
    if primary:
        return rpa_payload_needs_render_recovery(primary)
    return False


def rpa_render_fault_should_stop(payload: dict[str, Any]) -> bool:
    if not rpa_payload_needs_render_recovery(payload):
        return False
    recovery = payload.get("rpa_recovery") if isinstance(payload.get("rpa_recovery"), dict) else {}
    if str(recovery.get("reason") or "") == "auto_render_recovery_disabled":
        return True
    return not auto_render_recovery_enabled()


def rpa_payload_has_invalid_window_handle(payload: dict[str, Any]) -> bool:
    if not isinstance(payload, dict):
        return False
    state = str(payload.get("state") or "").strip()
    reason = str(payload.get("reason") or "").strip()
    if reason == "window_handle_invalid":
        return True
    if payload.get("risk_stop_reason") == "win32_invalid_window_handle":
        return True
    primary = payload.get("primary_status") if isinstance(payload.get("primary_status"), dict) else {}
    if primary and rpa_payload_has_invalid_window_handle(primary):
        return True
    error_text = str(payload.get("error") or "").strip().lower()
    if state not in {"win32_ocr_failed", "daemon_dispatch_failed", "compat_daemon_call_failed"}:
        return False
    return "getwindowrect" in error_text or "无效的窗口句柄" in error_text or "invalid window handle" in error_text


def snapshot_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Create a shallow copy suitable for diagnostics without cyclic references."""
    if not isinstance(payload, dict):
        return {}
    return dict(payload)


def verify_send_from_messages(messages_payload: dict[str, Any], *, expected_text: str) -> bool:
    messages = messages_payload.get("messages", []) if isinstance(messages_payload, dict) else []
    if not isinstance(messages, list):
        return False
    expected = str(expected_text or "")
    expected_compact = compact_text(expected)
    if not expected_compact:
        return False

    normalized_messages: list[tuple[str, str]] = []
    for item in messages:
        if not isinstance(item, dict):
            continue
        sender = str(item.get("sender") or "")
        content = str(item.get("content") or "")
        compact = compact_text(content)
        if compact:
            normalized_messages.append((sender, compact))

    # Strict path: exact content from self sender.
    if any(sender == "self" and content == expected for sender, content in ((str(i.get("sender") or ""), str(i.get("content") or "")) for i in messages if isinstance(i, dict))):
        return True
    if any(sender == "self" and compact == expected_compact for sender, compact in normalized_messages):
        return True

    # Relaxed path for Win32/OCR: sender side may drift to unknown and long
    # messages may split into multiple bubbles. Keep this scoped by requiring
    # all non-empty expected lines to be found in recent message contents.
    if any(compact == expected_compact for _sender, compact in normalized_messages):
        return True
    if any(
        sender in {"self", "unknown", ""}
        and ocr_tolerant_text_match(expected_compact, compact)
        for sender, compact in normalized_messages
    ):
        return True
    expected_lines = [compact_text(line) for line in expected.splitlines() if compact_text(line)]
    if len(expected_lines) <= 1:
        return False
    for line in expected_lines:
        if not any(line in compact for _sender, compact in normalized_messages):
            return False
    return True


def compact_text(value: str) -> str:
    return re.sub(r"\s+", "", str(value or ""))


def normalize_ocr_verification_text(value: str) -> str:
    compact = compact_text(value)
    compact = re.sub(r"(?<=[\u4e00-\u9fff])[Il1|](?=[\u4e00-\u9fff])", "", compact)
    compact = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", compact)
    return compact.lower()


def ocr_tolerant_text_match(expected_compact: str, observed_compact: str) -> bool:
    expected = normalize_ocr_verification_text(expected_compact)
    observed = normalize_ocr_verification_text(observed_compact)
    if len(expected) < 24 or len(observed) < 12:
        return False
    if expected in observed or observed in expected:
        return True
    ratio = difflib.SequenceMatcher(None, expected, observed).ratio()
    threshold = 0.78 if len(expected) >= 60 else 0.84
    return ratio >= threshold


def is_file_transfer_session_alias(target: str) -> bool:
    clean = re.sub(r"\s+", "", str(target or ""))
    if not clean:
        return False
    if clean in {FILE_TRANSFER_ASSISTANT, "仅传输文件"}:
        return True
    english = re.sub(r"[^a-z]", "", clean.lower())
    return english in {"filetransferassistant", "filetransfer", "transferassistant"}


def _simulated_inbound_env_targets() -> set[str]:
    raw = str(os.getenv("WECHAT_SIMULATED_INBOUND_TARGETS") or "").strip()
    if not raw:
        return set()
    tokens = re.split(r"[,\n;|]+", raw)
    normalized: set[str] = set()
    for token in tokens:
        clean = compact_text(token).lower()
        if clean:
            normalized.add(clean)
    return normalized


def is_simulated_inbound_loopback_target(target: str) -> bool:
    if is_file_transfer_session_alias(target):
        return True
    clean = compact_text(target).lower()
    if not clean:
        return False
    return clean in _simulated_inbound_env_targets()


def simulated_inbound_session_key(target: str) -> str:
    return compact_text(target).lower()


def _simulated_inbound_queue_file() -> Path | None:
    raw = str(os.getenv("WECHAT_SIMULATED_INBOUND_QUEUE_FILE") or "").strip()
    if not raw:
        return None
    try:
        return Path(raw)
    except Exception:
        return None


def _read_simulated_inbound_file(path: Path) -> dict[str, list[dict[str, Any]]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    queues = payload.get("queues") if isinstance(payload, dict) else {}
    if not isinstance(queues, dict):
        return {}
    normalized: dict[str, list[dict[str, Any]]] = {}
    for key, value in queues.items():
        if not isinstance(key, str) or not isinstance(value, list):
            continue
        normalized[key] = [item for item in value if isinstance(item, dict)]
    return normalized


def _write_simulated_inbound_file(path: Path, queues: dict[str, list[dict[str, Any]]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"queues": queues}
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, path)


def build_simulated_inbound_message(target: str, text: str) -> dict[str, Any]:
    content = str(text or "").strip()
    digest = hashlib.sha1(f"{target}|{content}|{time.time()}".encode("utf-8")).hexdigest()[:16]
    return {
        "id": f"win32_loopback:{digest}",
        "type": "text",
        "sender": "unknown",
        "sender_role": "unknown",
        "content": content,
        "time": "",
        "source_adapter": "win32_loopback",
        "loopback_fallback": True,
        "ocr_confidence": 0.0,
    }


def enqueue_simulated_inbound_message(*, target: str, text: str) -> None:
    content = str(text or "").strip()
    if not content:
        return
    key = simulated_inbound_session_key(target)
    if not key:
        return
    queue_file = _simulated_inbound_queue_file()
    if queue_file is not None:
        with _simulated_inbound_lock:
            queues = _read_simulated_inbound_file(queue_file)
            queue = queues.get(key, [])
            queue.append(build_simulated_inbound_message(target=target, text=content))
            if len(queue) > 20:
                queue = queue[-20:]
            queues[key] = queue
            _write_simulated_inbound_file(queue_file, queues)
        return
    with _simulated_inbound_lock:
        queue = _simulated_inbound_cache.get(key, [])
        queue.append(build_simulated_inbound_message(target=target, text=content))
        if len(queue) > 20:
            queue = queue[-20:]
        _simulated_inbound_cache[key] = queue


def pop_simulated_inbound_message(target: str) -> dict[str, Any] | None:
    key = simulated_inbound_session_key(target)
    if not key:
        return None
    queue_file = _simulated_inbound_queue_file()
    if queue_file is not None:
        with _simulated_inbound_lock:
            queues = _read_simulated_inbound_file(queue_file)
            queue = list(queues.get(key, []))
            if not queue:
                return None
            message = queue.pop(0)
            if queue:
                queues[key] = queue
            else:
                queues.pop(key, None)
            _write_simulated_inbound_file(queue_file, queues)
            return message if isinstance(message, dict) else None
    with _simulated_inbound_lock:
        queue = _simulated_inbound_cache.get(key, [])
        if not queue:
            return None
        message = queue.pop(0)
        if queue:
            _simulated_inbound_cache[key] = queue
        else:
            _simulated_inbound_cache.pop(key, None)
        return message


def inject_simulated_inbound_messages(payload: dict[str, Any], *, target: str) -> dict[str, Any]:
    if not is_simulated_inbound_loopback_target(target):
        return payload
    state = str(payload.get("state") or "")
    if state in {"messages_blocked", "login_window_detected", "wechat_not_ready"}:
        return payload
    messages = payload.get("messages")
    synthetic = pop_simulated_inbound_message(target)
    if not synthetic:
        return payload
    merged = list(messages) if isinstance(messages, list) else []
    merged.append(synthetic)
    next_payload = dict(payload)
    next_payload["messages"] = merged
    next_payload["state"] = "messages_ocr_loopback"
    next_payload["loopback_fallback"] = {
        "applied": True,
        "source": "simulated_inbound_loopback",
        "count": len(merged),
    }
    return next_payload


def blind_send_without_ocr(send_result: dict[str, Any], messages: dict[str, Any]) -> bool:
    if not isinstance(send_result, dict):
        return False
    send_meta = send_result.get("send_result")
    guard = send_meta.get("post_send_guard") if isinstance(send_meta, dict) else {}
    guard_reason = str(guard.get("reason") or "")
    if guard_reason != "target_confirm_skipped_no_ocr":
        return False
    if not isinstance(messages, dict):
        return True
    msg_state = str(messages.get("state") or "")
    if msg_state in {"messages_ocr_unavailable", "messages_blocked", "login_window_detected"}:
        return True
    message_list = messages.get("messages")
    if isinstance(message_list, list) and len(message_list) == 0:
        return True
    return False


def guarded_send_confirmation_fallback(send_result: dict[str, Any], messages: dict[str, Any]) -> bool:
    if not isinstance(send_result, dict):
        return False
    if send_result.get("ok") is not True:
        return False
    send_meta = send_result.get("send_result")
    if not isinstance(send_meta, dict):
        return False
    send_meta_ok = send_meta.get("ok")
    if send_meta_ok is False:
        return False
    pre_guard = send_meta.get("pre_send_guard")
    if not isinstance(pre_guard, dict):
        return False
    if pre_guard.get("ok") is not True or str(pre_guard.get("reason") or "") != "target_confirmed":
        return False
    if str(pre_guard.get("confirmation_confidence") or "") != "active_title_strict":
        return False
    if pre_guard.get("blind_send"):
        return False
    post_guard = send_meta.get("post_send_guard")
    if not isinstance(post_guard, dict) or post_guard.get("ok") is not True:
        return False
    if str(post_guard.get("reason") or "") not in {"target_confirmed", "send_window_readable_after_send"}:
        return False
    click_meta = send_meta.get("click")
    if not isinstance(click_meta, dict):
        return False
    paste_meta = click_meta.get("paste")
    if not isinstance(paste_meta, dict) or paste_meta.get("ok") is not True:
        return False
    confirmed_by = str(paste_meta.get("confirmed_by") or "")
    if confirmed_by not in {
        "ocr_input_area",
        "clipboard_copyback",
        "input_area_visual_delta",
        "input_area_visual_delta_fast",
    }:
        return False
    if not isinstance(messages, dict):
        return True
    message_state = str(messages.get("state") or "")
    if message_state in {"messages_blocked", "login_window_detected", "wechat_not_ready"}:
        return False
    return True
