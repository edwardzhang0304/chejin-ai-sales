from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Any


_LOCK = threading.RLock()
_STOPPED = threading.Event()
_STATE: dict[str, Any] = {}


def trigger_emergency_stop(*, reason: str, origin: str = "") -> dict[str, Any]:
    """Stop all new and in-flight WeChat work for this process."""

    with _LOCK:
        if not _STOPPED.is_set():
            _STATE.update(
                {
                    "reason": str(reason or "WORKER_EMERGENCY_STOPPED"),
                    "origin": str(origin or "unknown"),
                    "triggered_at": datetime.now(timezone.utc).isoformat(),
                }
            )
            _STOPPED.set()
        return dict(_STATE)


def emergency_stop_requested() -> bool:
    return _STOPPED.is_set()


def emergency_stop_state() -> dict[str, Any]:
    with _LOCK:
        return dict(_STATE)


def reset_emergency_stop_for_tests() -> None:
    with _LOCK:
        _STATE.clear()
        _STOPPED.clear()
