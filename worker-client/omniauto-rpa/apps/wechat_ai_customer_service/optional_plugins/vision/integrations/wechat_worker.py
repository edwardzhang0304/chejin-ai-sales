"""Vision-owned WeChat desktop worker.

This module is the only bundled place that combines image-specific behavior
with the current Win32/OCR host primitives.  The shared Sidecar remains a
generic OCR/RPA host and exposes no image action, parser flag, message
injection, or vision import.

The worker never saves screenshots or image files.  A successful copy action
changes the Windows clipboard and returns only transaction metadata; the
parent vision process reads that exact clipboard generation in memory.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import sys
from typing import Any


def _failure(state: str, reason: str, **extra: Any) -> dict[str, Any]:
    return {
        "ok": False,
        "online": bool(extra.pop("online", True)),
        "adapter": "win32_ocr",
        "state": str(state or "vision_wechat_worker_failed"),
        "reason": str(reason or state or "vision_wechat_worker_failed"),
        "assets": [],
        "messages": [],
        **extra,
    }


def _load_default_host() -> Any:
    # Imported lazily so the vision package remains importable without Win32,
    # OCR, or desktop dependencies.
    from apps.wechat_ai_customer_service.adapters import wechat_win32_ocr_sidecar

    return wechat_win32_ocr_sidecar


def _prepare_target(args: argparse.Namespace, host_ops: Any) -> dict[str, Any]:
    configure_dpi = getattr(host_ops, "configure_dpi_awareness", None)
    if callable(configure_dpi):
        configure_dpi()
    import_error = str(getattr(host_ops, "_WIN32_IMPORT_ERROR", "") or "")
    if import_error:
        return _failure(
            "pywin32_unavailable",
            "pywin32_unavailable",
            online=False,
            error=import_error,
        )
    probe = host_ops.ensure_visible_wechat_window(interactive=True)
    windows = probe.get("visible_main_windows") if isinstance(probe, dict) else None
    if not windows:
        return _failure(
            "main_window_not_found",
            "wechat_main_window_not_found",
            online=False,
            window_probe=probe if isinstance(probe, dict) else {},
        )
    window = host_ops.select_primary_visible_main_window(probe)
    hwnd = int((window or {}).get("hwnd") or 0)
    if not hwnd:
        return _failure(
            "main_window_not_found",
            "wechat_main_window_not_found",
            online=False,
            window_probe=probe,
        )
    probe["selected_main_window"] = dict(window)
    dismissal = host_ops.dismiss_blank_foreground_window_before_activation(
        hwnd,
        artifact_dir=None,
    )
    if isinstance(dismissal, dict) and dismissal.get("attempted"):
        probe["foreground_blank_dismissal"] = dismissal
    host_ops.activate_window(hwnd)
    normalized = host_ops.normalize_wechat_window(hwnd)
    probe["window_normalization"] = normalized
    if isinstance(normalized, dict) and normalized.get("applied"):
        host_ops.humanized_action_sleep(210, 330)
    quick_login = host_ops.ensure_quick_login_if_available(
        hwnd,
        artifact_dir=None,
        auto_enter=host_ops.env_flag(
            "WECHAT_WIN32_OCR_QUICK_LOGIN_AUTO_ENTER",
            default=getattr(host_ops, "DEFAULT_QUICK_LOGIN_AUTO_ENTER", False),
        ),
    )
    probe["quick_login"] = quick_login
    if isinstance(quick_login, dict) and quick_login.get("attempted"):
        host_ops.humanized_action_sleep(380, 560)
    host_ops.humanized_action_sleep(140, 260)

    target = str(getattr(args, "target", "") or "").strip()
    if not target:
        return _failure("vision_wechat_target_missing", "target_missing")
    session_key = str(getattr(args, "session_key", "") or "").strip()
    conversation_type = host_ops.normalize_identity_conversation_type(
        str(getattr(args, "conversation_type", "") or "")
    )
    validation = host_ops.validate_active_send_target_for_identity(
        hwnd,
        target,
        exact=bool(getattr(args, "exact", False)),
        artifact_dir=None,
        session_key=session_key,
        conversation_type=conversation_type,
    )
    opened = False
    if not validation.get("ok"):
        opened = bool(
            host_ops.open_chat_for_identity(
                hwnd,
                target,
                exact=bool(getattr(args, "exact", False)),
                artifact_dir=None,
                session_key=session_key,
                conversation_type=conversation_type,
            )
        )
        host_ops.humanized_action_sleep(380, 620)
        validation = host_ops.validate_active_send_target_for_identity(
            hwnd,
            target,
            exact=bool(getattr(args, "exact", False)),
            artifact_dir=None,
            session_key=session_key,
            conversation_type=conversation_type,
        )
    if not validation.get("ok"):
        return _failure(
            "target_not_confirmed_for_vision",
            "vision_target_not_confirmed",
            online=bool(validation.get("online", True)),
            target=target,
            session_key=session_key,
            opened=opened,
            guard=validation,
            window_probe=probe,
        )
    if host_ops.scroll_to_latest_before_read_enabled():
        host_ops.scroll_chat_to_latest(hwnd)
    return {
        "ok": True,
        "hwnd": hwnd,
        "probe": probe,
        "target": target,
        "session_key": session_key,
        "conversation_type": conversation_type,
    }


def _observe_current_surface(
    prepared: dict[str, Any],
    args: argparse.Namespace,
    host_ops: Any,
) -> dict[str, Any]:
    from apps.wechat_ai_customer_service.optional_plugins.vision.capture.surface import (
        visual_image_messages_from_current_surface,
    )

    hwnd = int(prepared.get("hwnd") or 0)
    target = str(prepared.get("target") or "")
    try:
        screenshot, _unused_path = host_ops.capture_wechat(
            hwnd,
            artifact_dir=None,
            label="vision_current_surface_observation",
        )
        ocr_items = host_ops.run_ocr(screenshot)
        image_size = tuple(getattr(screenshot, "size", (0, 0)))
        text_messages = host_ops.parse_messages_from_ocr(
            ocr_items,
            image_size,
            target=target,
        )
        blocking_reason = host_ops.blocking_screen_reason(ocr_items)
        if blocking_reason:
            return _failure(
                "vision_current_surface_blocked",
                str(blocking_reason),
                online=False if blocking_reason == "login_or_qr" else True,
                target=target,
                session_key=str(prepared.get("session_key") or ""),
            )
        messages = visual_image_messages_from_current_surface(
            screenshot,
            ocr_items,
            text_messages,
            target=target,
            side_filter=str(getattr(args, "side_filter", "all") or "all"),
            max_images=max(1, min(int(getattr(args, "max_images", 8) or 8), 8)),
        )
    except Exception as exc:  # noqa: BLE001 - worker returns a closed failure envelope.
        return _failure(
            "vision_current_surface_observation_failed",
            "vision_current_surface_observation_failed",
            target=target,
            session_key=str(prepared.get("session_key") or ""),
            error=repr(exc),
        )
    return {
        "ok": True,
        "online": True,
        "adapter": "win32_ocr",
        "state": "vision_current_surface_observed",
        "target": target,
        "session_key": str(prepared.get("session_key") or ""),
        "assets": [],
        "messages": messages,
    }


def _copy_current_image(
    prepared: dict[str, Any],
    args: argparse.Namespace,
    host_ops: Any,
) -> dict[str, Any]:
    from apps.wechat_ai_customer_service.optional_plugins.vision.capture.wechat import (
        execute_wechat_clipboard_image_copy,
    )

    return execute_wechat_clipboard_image_copy(
        hwnd=int(prepared.get("hwnd") or 0),
        probe=prepared.get("probe") if isinstance(prepared.get("probe"), dict) else {},
        target_name=str(prepared.get("target") or ""),
        session_key=str(prepared.get("session_key") or ""),
        exact=bool(getattr(args, "exact", False)),
        source_preview=str(getattr(args, "source_preview", "") or ""),
        speaker_name=str(getattr(args, "speaker_name", "") or ""),
        pending_signal_id=str(getattr(args, "pending_signal_id", "") or ""),
        side_filter=str(getattr(args, "side_filter", "customer") or "customer"),
        sidecar_ops=host_ops,
    )


def run_operation(args: argparse.Namespace, *, host_ops: Any | None = None) -> dict[str, Any]:
    host = host_ops or _load_default_host()
    prepared = _prepare_target(args, host)
    if not prepared.get("ok"):
        return prepared
    operation = str(getattr(args, "operation", "") or "").strip().lower()
    if operation == "observe-current-surface":
        return _observe_current_surface(prepared, args, host)
    if operation == "copy-current-image":
        return _copy_current_image(prepared, args, host)
    return _failure("vision_worker_operation_invalid", "vision_worker_operation_invalid")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "operation",
        choices=("observe-current-surface", "copy-current-image"),
    )
    parser.add_argument("--target", required=True)
    parser.add_argument("--session-key", default="")
    parser.add_argument("--conversation-type", default="")
    parser.add_argument("--exact", action="store_true")
    parser.add_argument("--source-preview", default="")
    parser.add_argument("--speaker-name", default="")
    parser.add_argument("--pending-signal-id", default="")
    parser.add_argument("--side-filter", default="customer", choices=("customer", "self", "all"))
    parser.add_argument("--max-images", type=int, default=8)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    captured = io.StringIO()
    try:
        with contextlib.redirect_stdout(captured):
            payload = run_operation(args)
    except Exception as exc:  # noqa: BLE001
        payload = _failure(
            "vision_wechat_worker_failed",
            "vision_wechat_worker_failed",
            error=repr(exc),
        )
    logs = captured.getvalue().strip()
    if logs:
        payload["library_stdout"] = logs
    print(json.dumps(payload, ensure_ascii=True))
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
