from __future__ import annotations

import os
import sys
import threading
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import TracebackType
from typing import Any

from .config import CONFIG
from .emergency_stop import reset_emergency_stop_for_tests, trigger_emergency_stop
from .incident_evidence import start_incident_worker
from .storage import append_log, load_binding, save_binding


_HOOK_LOCK = threading.RLock()
_REPORTING = threading.local()
_INSTALLED = False
_SESSION_ID = ""
_ORIGINAL_SYS_EXCEPTHOOK = sys.excepthook
_ORIGINAL_THREADING_EXCEPTHOOK = threading.excepthook


def _marker_path() -> Path:
    path = CONFIG.app_dir / "runtime" / "session.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _read_marker() -> dict[str, Any]:
    try:
        import json

        payload = json.loads(_marker_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_marker(payload: dict[str, Any]) -> None:
    import json

    path = _marker_path()
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _pause_locally() -> str:
    try:
        binding = load_binding()
        if binding is None:
            return "binding_missing"
        binding.run_status = "paused"
        save_binding(binding)
        return "paused"
    except Exception as exc:
        return f"pause_failed:{type(exc).__name__}"


def report_unhandled_exception(
    origin: str,
    exc_type: type[BaseException],
    exc: BaseException,
    tb: TracebackType | None,
) -> dict[str, Any]:
    if getattr(_REPORTING, "active", False):
        return {}
    _REPORTING.active = True
    try:
        emergency_state = trigger_emergency_stop(
            reason="WORKER_UNHANDLED_EXCEPTION",
            origin=str(origin or "unknown"),
        )
        traceback_text = "".join(traceback.format_exception(exc_type, exc, tb))
        pause_result = _pause_locally()
        try:
            marker = _read_marker()
            marker.update(
                {
                    "status": "crashed",
                    "crashed_at": datetime.now(timezone.utc).isoformat(),
                    "crash_origin": str(origin or "unknown"),
                    "exception_type": exc_type.__name__,
                }
            )
            _write_marker(marker)
        except OSError:
            pass
        return append_log(
            "ERROR",
            "worker_unhandled_exception",
            f"{origin} 出现未捕获异常，客户端已自动暂停。",
            error_code="WORKER_UNHANDLED_EXCEPTION",
            metadata={
                "origin": str(origin or "unknown"),
                "exception_type": exc_type.__name__,
                "traceback": traceback_text,
                "automatic_pause": True,
                "pause_result": pause_result,
                "runtime_session_id": _SESSION_ID,
                "emergency_stop": emergency_state,
            },
            force_incident=True,
        )
    finally:
        _REPORTING.active = False


def _sys_excepthook(
    exc_type: type[BaseException],
    exc: BaseException,
    tb: TracebackType | None,
) -> None:
    try:
        report_unhandled_exception("main_thread", exc_type, exc, tb)
    finally:
        _ORIGINAL_SYS_EXCEPTHOOK(exc_type, exc, tb)


def _threading_excepthook(args: threading.ExceptHookArgs) -> None:
    thread_name = str(getattr(args.thread, "name", "") or "unnamed")
    try:
        report_unhandled_exception(
            f"thread:{thread_name}",
            args.exc_type,
            args.exc_value,
            args.exc_traceback,
        )
    finally:
        _ORIGINAL_THREADING_EXCEPTHOOK(args)


def install_runtime_supervision() -> None:
    global _INSTALLED, _SESSION_ID
    with _HOOK_LOCK:
        if _INSTALLED:
            return
        start_incident_worker()
        previous = _read_marker()
        _SESSION_ID = f"runtime-{uuid.uuid4()}"
        if previous.get("status") in {"running", "crashed"}:
            append_log(
                "ERROR",
                "worker_previous_session_unclean_exit",
                "检测到上次客户端未正常退出，已生成恢复故障证据。",
                error_code="WORKER_PREVIOUS_SESSION_UNCLEAN_EXIT",
                metadata={
                    "previous_runtime_session_id": previous.get("session_id"),
                    "previous_started_at": previous.get("started_at"),
                    "previous_pid": previous.get("pid"),
                    "previous_status": previous.get("status"),
                    "previous_crash_origin": previous.get("crash_origin"),
                },
                force_incident=True,
            )
        _write_marker(
            {
                "schema_version": 1,
                "session_id": _SESSION_ID,
                "status": "running",
                "pid": os.getpid(),
                "started_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        sys.excepthook = _sys_excepthook
        threading.excepthook = _threading_excepthook
        _INSTALLED = True


def mark_runtime_clean_exit(exit_code: int = 0) -> None:
    marker = _read_marker()
    if str(marker.get("session_id") or "") != _SESSION_ID:
        return
    marker.update(
        {
            "status": "clean_exit",
            "exit_code": int(exit_code),
            "ended_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    _write_marker(marker)


def reset_runtime_supervision_for_tests() -> None:
    global _INSTALLED, _SESSION_ID
    with _HOOK_LOCK:
        sys.excepthook = _ORIGINAL_SYS_EXCEPTHOOK
        threading.excepthook = _ORIGINAL_THREADING_EXCEPTHOOK
        reset_emergency_stop_for_tests()
        _INSTALLED = False
        _SESSION_ID = ""
