from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

from .config import CONFIG


def _omniauto_root() -> Path:
    frozen_root = getattr(sys, "_MEIPASS", None)
    if frozen_root:
        return Path(frozen_root) / "omniauto-rpa"
    return Path(__file__).resolve().parents[1] / "omniauto-rpa"


def _guard_adapter():
    root = _omniauto_root()
    root_text = str(root)
    if root_text not in sys.path:
        sys.path.insert(0, root_text)
    from apps.wechat_ai_customer_service.adapters import rpa_operator_guard

    return rpa_operator_guard


def start_ui_operator_guard(
    *,
    lock_id: str,
    operation_type: str,
    owner: str,
    current_step: str,
) -> dict[str, Any]:
    """Start the guard before a real Windows UI lease becomes usable."""

    if CONFIG.rpa_mode == "mock":
        return {
            "ok": True,
            "enabled": False,
            "started": False,
            "reason": "mock_rpa_has_no_real_ui",
            "lock_id": lock_id,
        }
    adapter = _guard_adapter()
    result = adapter.start_rpa_operator_guard(
        operation=operation_type,
        route=f"ui_lock:{lock_id}",
    )
    result = {
        **result,
        "lock_id": lock_id,
        "operation_type": operation_type,
        "owner": owner,
        "current_step": current_step,
    }
    if os.name == "nt" and (
        result.get("ok") is not True
        or result.get("enabled") is not True
        or result.get("started") is not True
    ):
        return {
            **result,
            "ok": False,
            "reason": str(result.get("reason") or "operator_guard_not_ready"),
        }
    return result


def ui_operator_guard_health(guard: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(guard, dict):
        return {"ok": False, "reason": "operator_guard_handle_missing"}
    if not guard.get("enabled"):
        return {"ok": True, "mode": "not_enabled", "reason": str(guard.get("reason") or "not_enabled")}
    return _guard_adapter().rpa_operator_guard_health(guard)


def stop_ui_operator_guard(
    guard: dict[str, Any] | None,
    *,
    reason: str,
) -> dict[str, Any]:
    if not isinstance(guard, dict):
        return {"ok": True, "skipped": True, "reason": "operator_guard_handle_missing"}
    if not guard.get("enabled"):
        return {"ok": True, "skipped": True, "reason": str(guard.get("reason") or "not_enabled")}
    return _guard_adapter().stop_rpa_operator_guard(guard, reason=reason)


def cleanup_orphaned_ui_operator_guard(*, reason: str) -> dict[str, Any]:
    """Remove a guard left behind by a crashed Worker before normal operation."""

    if os.name != "nt" or CONFIG.rpa_mode == "mock":
        return {"ok": True, "skipped": True, "reason": "no_real_windows_ui"}
    adapter = _guard_adapter()
    paths = adapter.rpa_operator_guard_paths()
    pid_record = adapter.read_json(paths["pid_path"])
    pid = int(pid_record.get("pid") or 0)
    if pid <= 0 or not adapter.pid_alive(pid):
        return {"ok": True, "cleaned": False, "reason": "no_live_orphan"}
    synthetic_guard = {
        "ok": True,
        "enabled": True,
        "started": True,
        "pid": pid,
        "tenant_id": adapter.active_tenant_id(),
        "paths": {key: str(value) for key, value in paths.items()},
        "verify": {"state": adapter.read_json(paths["state_path"])},
    }
    result = adapter.stop_rpa_operator_guard(synthetic_guard, reason=reason)
    return {**result, "cleaned": True}
