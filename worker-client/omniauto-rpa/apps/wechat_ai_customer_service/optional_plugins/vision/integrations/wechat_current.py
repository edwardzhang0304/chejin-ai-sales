"""Current WeChat host binding for the vision-owned clipboard transaction.

All image-specific action construction, clipboard state, and customer/self
selection live here.  The shared connector supplies only generic transport and
the process-wide RPA lock primitives.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable


_WORKER_MODULE = (
    "apps.wechat_ai_customer_service.optional_plugins.vision.integrations.wechat_worker"
)


def _worker_python(connector: Any) -> str:
    return str(
        getattr(connector, "compat_sidecar_python", "")
        or getattr(connector, "sidecar_python", "")
        or sys.executable
    )


def _worker_root(connector: Any) -> Path:
    configured = getattr(connector, "root", None)
    return Path(configured) if configured else Path(__file__).resolve().parents[5]


def _run_vision_worker(connector: Any, args: list[str]) -> dict[str, Any]:
    command = [_worker_python(connector), "-m", _WORKER_MODULE, *args]
    timeout = max(5.0, float(getattr(connector, "timeout_seconds", 90.0) or 90.0))
    creationflags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
    worker_env = os.environ.copy()
    # PR #28 deliberately remains byte-immutable.  Its generic Win32 host
    # defaults fixed-origin normalization off, while OmniAuto's existing
    # physical-coordinate contract requires it on.  The normal Connector path
    # receives this host policy from ``wechat_pr28_runtime_adapter``; Vision
    # starts its own worker and therefore must carry the same additive default
    # at this external binding.  An explicit operator setting still wins.
    worker_env.setdefault("WECHAT_WIN32_OCR_WINDOW_FIXED_ORIGIN", "1")
    try:
        completed = subprocess.run(
            command,
            cwd=str(_worker_root(connector)),
            env=worker_env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            creationflags=creationflags,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "ok": False,
            "adapter": "win32_ocr",
            "state": "vision_wechat_worker_timeout",
            "reason": "vision_wechat_worker_timeout",
            "assets": [],
            "messages": [],
            "error": repr(exc),
        }
    except Exception as exc:  # noqa: BLE001 - optional worker fails closed.
        return {
            "ok": False,
            "adapter": "win32_ocr",
            "state": "vision_wechat_worker_failed",
            "reason": "vision_wechat_worker_failed",
            "assets": [],
            "messages": [],
            "error": repr(exc),
        }
    stdout = str(completed.stdout or "").strip()
    try:
        payload = json.loads(stdout) if stdout else {}
    except json.JSONDecodeError:
        payload = {}
    if not isinstance(payload, dict) or not payload:
        return {
            "ok": False,
            "adapter": "win32_ocr",
            "state": "vision_wechat_worker_invalid_response",
            "reason": "vision_wechat_worker_invalid_response",
            "assets": [],
            "messages": [],
            "returncode": int(completed.returncode),
            "stderr": str(completed.stderr or "").strip()[-2000:],
        }
    payload.setdefault("adapter", "win32_ocr")
    payload.setdefault("assets", [])
    payload.setdefault("messages", [])
    if completed.returncode and payload.get("ok"):
        payload["ok"] = False
        payload["state"] = "vision_wechat_worker_exit_mismatch"
        payload["reason"] = "vision_wechat_worker_exit_mismatch"
    return payload


def observe_current_surface(
    connector: Any,
    target: str,
    exact: bool = True,
    *,
    session_key: str = "",
    conversation_type: str = "",
    side_filter: str = "all",
    max_images: int = 8,
) -> dict[str, Any]:
    """Return text-only structural image envelopes from the current chat."""

    from apps.wechat_ai_customer_service.adapters.wechat_connector import (
        any_weixin_process,
        attach_rpa_lock_meta,
        rpa_lock_timeout_payload,
        rpa_lock_timeout_seconds,
        wechat_rpa_lock,
    )

    clean_side = str(side_filter or "all").strip().lower()
    if clean_side not in {"customer", "self", "all"}:
        return {
            "ok": False,
            "adapter": "win32_ocr",
            "state": "vision_current_surface_side_invalid",
            "reason": "vision_current_surface_side_invalid",
            "assets": [],
            "messages": [],
        }
    if not target:
        return {
            "ok": False,
            "adapter": "win32_ocr",
            "state": "vision_current_surface_target_missing",
            "reason": "target_missing",
            "assets": [],
            "messages": [],
        }
    worker_args = [
        "observe-current-surface",
        "--target",
        str(target),
        "--side-filter",
        clean_side,
        "--max-images",
        str(max(1, min(int(max_images or 8), 8))),
    ]
    if exact:
        worker_args.append("--exact")
    from apps.wechat_ai_customer_service.adapters.wechat_pr28_runtime_adapter import (
        physical_rpa_identity_kwargs,
    )

    physical_identity = physical_rpa_identity_kwargs(
        {"session_key": session_key, "conversation_type": conversation_type}
    )
    for value, flag in (
        (session_key, "--session-key"),
        (physical_identity.get("conversation_type"), "--conversation-type"),
    ):
        clean = str(value or "").strip()
        if clean:
            worker_args.extend([flag, clean])
    lock_timeout = rpa_lock_timeout_seconds("vision_current_surface_observation", default=45.0)
    try:
        with wechat_rpa_lock(
            "vision_current_surface_observation",
            timeout_seconds=lock_timeout,
        ) as lock_meta:
            result = _run_vision_worker(connector, worker_args)
            result.setdefault("transport_priority", "rpa_first")
            attach_rpa_lock_meta(result, lock_meta)
            return result
    except TimeoutError as exc:
        return {
            "ok": False,
            "online": bool(any_weixin_process()),
            "adapter": "win32_ocr",
            "state": "vision_current_surface_lock_timeout",
            "reason": "vision_current_surface_lock_timeout",
            "target": target,
            "exact": exact,
            "assets": [],
            "messages": [],
            "error": repr(exc),
            "transport_priority": "rpa_first",
            "rpa_lock": rpa_lock_timeout_payload(
                exc,
                action="vision_current_surface_observation",
                timeout_seconds=lock_timeout,
            ),
        }


def run_clipboard_image_transaction(
    connector: Any,
    target: str,
    exact: bool = True,
    *,
    session_key: str = "",
    source_preview: str = "",
    speaker_name: str = "",
    pending_signal_id: str = "",
    side_filter: str = "customer",
    consume_current_clipboard: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    from apps.wechat_ai_customer_service.adapters.wechat_connector import (
        any_weixin_process,
        attach_rpa_lock_meta,
        rpa_lock_timeout_payload,
        rpa_lock_timeout_seconds,
        wechat_rpa_lock,
    )

    clean_side_filter = str(side_filter or "customer").strip().lower()
    if clean_side_filter not in {"customer", "self"}:
        return {
            "ok": False,
            "adapter": "win32_ocr",
            "state": "image_clipboard_copy_side_invalid",
            "reason": "image_clipboard_side_filter_invalid",
            "assets": [],
            "messages": [],
        }
    if not target:
        return {
            "ok": False,
            "adapter": "win32_ocr",
            "state": "image_clipboard_copy_target_missing",
            "reason": "target_missing",
            "assets": [],
            "messages": [],
        }
    args = ["copy-current-image", "--target", target, "--side-filter", clean_side_filter]
    clean_session_key = str(session_key or "").strip()
    if clean_session_key:
        args.extend(["--session-key", clean_session_key])
    if exact:
        args.append("--exact")
    for value, flag in (
        (source_preview, "--source-preview"),
        (speaker_name, "--speaker-name"),
        (pending_signal_id, "--pending-signal-id"),
    ):
        clean = str(value or "").strip()
        if clean:
            args.extend([flag, clean])
    lock_timeout = rpa_lock_timeout_seconds("image_clipboard_transaction", default=45.0)
    try:
        with wechat_rpa_lock("image_clipboard_transaction", timeout_seconds=lock_timeout) as lock_meta:
            primary = _run_vision_worker(connector, args)
            primary.setdefault("adapter", "win32_ocr")
            primary.setdefault("transport_priority", "rpa_first")
            attach_rpa_lock_meta(primary, lock_meta)
            if not primary.get("ok"):
                return primary
            if not callable(consume_current_clipboard):
                primary["ok"] = False
                primary["state"] = "image_clipboard_consumer_missing"
                primary["reason"] = "image_clipboard_consumer_missing"
                return primary
            transaction = primary.get("transaction") if isinstance(primary.get("transaction"), dict) else {}
            try:
                consumed = consume_current_clipboard(dict(transaction))
            except Exception as exc:  # noqa: BLE001
                consumed = {"ok": False, "reason": "clipboard_current_read_failed", "error": repr(exc)}
            if not isinstance(consumed, dict) or not consumed.get("ok"):
                primary["ok"] = False
                primary["state"] = "image_clipboard_current_read_failed"
                primary["reason"] = str((consumed or {}).get("reason") or "clipboard_current_read_failed")
                primary["transaction"] = {
                    **transaction,
                    "status": "failed",
                    "clipboard_content_read": False,
                }
                return primary
            primary["_ephemeral_clipboard_image"] = consumed.get("image")
            primary["transaction"] = {
                **transaction,
                "status": "clipboard_read",
                "clipboard_content_read": True,
                "clipboard_image_valid": True,
            }
            return primary
    except TimeoutError as exc:
        return {
            "ok": False,
            "online": bool(any_weixin_process()),
            "adapter": "win32_ocr",
            "state": "image_clipboard_transaction_lock_timeout",
            "reason": "image_clipboard_transaction_lock_timeout",
            "target": target,
            "exact": exact,
            "assets": [],
            "messages": [],
            "error": repr(exc),
            "transport_priority": "rpa_first",
            "rpa_lock": rpa_lock_timeout_payload(
                exc,
                action="image_clipboard_transaction",
                timeout_seconds=lock_timeout,
            ),
        }


def run_self_clipboard_image_transaction(
    connector: Any,
    target: str,
    exact: bool = True,
    *,
    session_key: str = "",
    source_preview: str = "",
    pending_signal_id: str = "",
    consume_current_clipboard: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return run_clipboard_image_transaction(
        connector,
        target,
        exact=exact,
        session_key=session_key,
        source_preview=source_preview,
        pending_signal_id=pending_signal_id,
        side_filter="self",
        consume_current_clipboard=consume_current_clipboard,
    )
