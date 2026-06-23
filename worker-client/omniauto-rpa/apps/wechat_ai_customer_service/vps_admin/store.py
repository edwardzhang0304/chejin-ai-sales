"""Small JSON-backed state store for the VPS admin control plane."""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from apps.wechat_ai_customer_service.knowledge_paths import runtime_app_root


STATE_SCHEMA_VERSION = 1
AUDIT_RETENTION_LIMIT = 20
STATE_KEYS = (
    "tenants",
    "users",
    "sessions",
    "admin_credentials",
    "auth_challenges",
    "trusted_devices",
    "smtp_config",
    "local_nodes",
    "commands",
    "command_results",
    "shared_proposals",
    "shared_library",
    "shared_patches",
    "shared_scan_state",
    "recorder_modules",
    "recorder_module_bindings",
    "backup_requests",
    "restore_requests",
    "customer_data_packages",
    "shared_snapshots",
    "releases",
    "audit_events",
)


class VpsAdminStore:
    def __init__(self, *, path: Path | None = None) -> None:
        self.path = path or default_state_path()
        self._lock = threading.RLock()

    def read(self) -> dict[str, Any]:
        with self._lock:
            return self._read_unlocked()

    def write(self, state: dict[str, Any]) -> None:
        with self._lock:
            self._write_unlocked(normalize_state(state))

    def update(self, mutator: Callable[[dict[str, Any]], Any]) -> Any:
        with self._lock:
            state = self._read_unlocked()
            result = mutator(state)
            self._write_unlocked(state)
            return result

    def _read_unlocked(self) -> dict[str, Any]:
        if not self.path.exists():
            return default_state()
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return default_state()
        if not isinstance(payload, dict):
            return default_state()
        return normalize_state(payload)

    def _write_unlocked(self, state: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(self.path.suffix + ".tmp")
        temp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        for attempt in range(20):
            try:
                os.replace(temp, self.path)
                return
            except OSError as exc:
                if not is_transient_windows_write_error(exc) or attempt == 19:
                    raise
                time.sleep(0.05 * (attempt + 1))


def default_state_path() -> Path:
    configured = os.getenv("WECHAT_VPS_ADMIN_STATE_PATH", "").strip()
    if configured:
        return Path(configured)
    return runtime_app_root() / "vps_admin" / "control_plane.json"


def is_transient_windows_write_error(exc: OSError) -> bool:
    winerror = getattr(exc, "winerror", None)
    return getattr(exc, "errno", None) in {13, 22} or winerror in {5, 32, 33}


def default_state() -> dict[str, Any]:
    state: dict[str, Any] = {"schema_version": STATE_SCHEMA_VERSION}
    for key in STATE_KEYS:
        state[key] = [] if key in {"command_results", "audit_events"} else {}
    return state


def normalize_state(payload: dict[str, Any]) -> dict[str, Any]:
    state = deepcopy(payload)
    state["schema_version"] = STATE_SCHEMA_VERSION
    for key in STATE_KEYS:
        if key in {"command_results", "audit_events"}:
            if not isinstance(state.get(key), list):
                state[key] = []
            if key == "audit_events":
                state[key] = state[key][-AUDIT_RETENTION_LIMIT:]
        elif not isinstance(state.get(key), dict):
            state[key] = {}
    return state


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def append_audit(
    state: dict[str, Any],
    *,
    actor_id: str,
    action: str,
    target_type: str,
    target_id: str,
    detail: dict[str, Any] | None = None,
) -> dict[str, Any]:
    created_at = now_iso()
    event = {
        "event_id": f"audit_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')}_{uuid.uuid4().hex[:6]}",
        "actor_id": actor_id,
        "action": action,
        "target_type": target_type,
        "target_id": target_id,
        "detail": detail or {},
        "created_at": created_at,
    }
    state.setdefault("audit_events", []).append(event)
    state["audit_events"] = state["audit_events"][-AUDIT_RETENTION_LIMIT:]
    return event
