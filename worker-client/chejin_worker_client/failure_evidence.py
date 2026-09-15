"""Diagnostics at external boundaries; never grants permission or retries work."""
from __future__ import annotations

import json
from pathlib import Path
import re
import sys
import threading
import time
import traceback
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator


_FALLBACK_LOCK = threading.Lock()
_FALLBACK_LIMIT = 256 * 1024


def exception_details(exc: BaseException) -> dict[str, Any]:
    # Exception messages, request bodies and locals may contain credentials.
    return {
        "exception_type": type(exc).__name__,
        "errno": getattr(exc, "errno", None),
        "winerror": getattr(exc, "winerror", None),
        "frames": [
            {"file": Path(frame.filename).name, "function": frame.name, "line": frame.lineno}
            for frame in traceback.extract_tb(exc.__traceback__)[-12:]
        ],
    }


def record_capture_failure(origin: str, exc: BaseException, *, error_code: str = "") -> None:
    """Independent, bounded fallback when SQLite or incident capture fails."""
    from .config import CONFIG

    record = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "event": "evidence_capture_failed",
        "origin": re.sub(r"[^a-zA-Z0-9_.-]", "_", origin)[:100],
        **exception_details(exc),
    }
    if re.fullmatch(r"[A-Z][A-Z0-9_]{0,127}", error_code):
        record["original_error_code"] = error_code
    data = json.dumps(record, ensure_ascii=True) + "\n"
    try:
        with _FALLBACK_LOCK:
            path = CONFIG.app_dir / "incidents" / "evidence-recorder-failures.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists() and path.stat().st_size + len(data) > _FALLBACK_LIMIT:
                path.replace(path.with_suffix(".previous.jsonl"))
            with path.open("a", encoding="utf-8") as stream:
                stream.write(data)
    except Exception:
        # A completely unwritable disk cannot guarantee a local artifact.
        try:
            sys.stderr.write(data)
        except Exception:
            pass


def record_failure(
    event: str,
    *,
    error_code: str,
    message: str,
    metadata: dict[str, Any] | None = None,
    include_exception_text: bool = True,
) -> dict[str, Any]:
    try:
        from .storage import append_log

        return append_log(
            "WARN", event, message,
            error_code=error_code, metadata=metadata, force_incident=True,
            include_exception_text=include_exception_text,
        )
    except Exception as exc:
        record_capture_failure(event, exc, error_code=error_code)
        return {}


def mark_failure_recovered(event: str, origin: str) -> None:
    try:
        from .incident_evidence import mark_incident_recovered

        mark_incident_recovered(event, metadata={"origin": origin})
    except Exception as exc:
        record_capture_failure("mark_failure_recovered", exc)


def record_ui_failure(window: Any, payload: str) -> None:
    """Qt-thread boundary. Exclude binding secrets and limit repeated captures."""
    from .config import CONFIG

    try:
        incoming = json.loads(payload[:2048])
        if not isinstance(incoming, dict):
            return
    except (ValueError, TypeError):
        return
    kind = incoming.get("kind")
    if kind not in {"javascript_error", "unhandled_rejection", "bridge_invalid_json",
                    "bridge_invalid_state", "renderer_terminated", "page_load_failed"}:
        return
    metadata: dict[str, Any] = {"origin": kind}
    for field in ("exception_type", "file", "line", "column", "exit_code"):
        value = incoming.get(field)
        if isinstance(value, (str, int)):
            metadata[field] = re.sub(r"[^a-zA-Z0-9_.-]", "_", str(value))[:100]
    metadata["screenshot_status"] = "unavailable"
    if not getattr(window, "binding", None) or getattr(window, "active_page", "") == "bind":
        metadata["screenshot_reason"] = "sensitive_binding_screen"
    elif time.monotonic() - getattr(window, "_ui_evidence_last_capture", -60) < 60:
        metadata["screenshot_reason"] = "repeated_ui_capture_within_60_seconds"
    else:
        window._ui_evidence_last_capture = time.monotonic()
        try:
            folder = CONFIG.app_dir / "artifacts" / "ui_failures" / uuid.uuid4().hex
            folder.mkdir(parents=True)
            frame = window.grab()
            path = folder / "client-window.png"
            if frame.isNull() or not frame.save(str(path), "PNG"):
                raise OSError("UI_SCREENSHOT_SAVE_FAILED")
            metadata.update(screenshot_status="saved", screenshot_path=str(path))
        except Exception as exc:
            metadata.update(screenshot_reason="capture_failed", capture_error=exception_details(exc))
            record_capture_failure("ui_screenshot", exc)
    record_failure("ui_runtime_failed", error_code="CLIENT_UI_RUNTIME_FAILED",
                   message="客户端界面发生异常，已记录错误位置及可用截图。", metadata=metadata)


@contextmanager
def api_failure_evidence(method: str, path: str) -> Iterator[None]:
    """Preserve HTTP rejection/transport facts without headers or response bodies."""
    try:
        yield
    except Exception as exc:
        code = str(getattr(exc, "code", "") or "API_TRANSPORT_FAILED")
        if not re.fullmatch(r"[A-Z][A-Z0-9_]{0,127}", code):
            code = "API_REQUEST_FAILED"
        record_failure(
            "api_request_failed", error_code=code, message="接口请求失败，原错误已交回调用流程处理。",
            include_exception_text=False,
            metadata={
                "origin": f"{method.upper()} {path.split('?', 1)[0]}",
                "http_status": getattr(exc, "status_code", None),
                "trace_id": getattr(exc, "trace_id", None),
                "screenshot_status": "not_applicable",
                "screenshot_reason": "http_boundary_does_not_capture_desktop",
                **exception_details(exc),
            },
        )
        raise
    else:
        mark_failure_recovered("api_request_failed", f"{method.upper()} {path.split('?', 1)[0]}")
