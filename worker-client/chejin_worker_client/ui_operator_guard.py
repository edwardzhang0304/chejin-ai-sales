from __future__ import annotations

import os
import sys
import threading
from pathlib import Path
from typing import Any

from .config import CONFIG


_SERVICE_LOCK = threading.RLock()
_WORKER_GUARD: dict[str, Any] | None = None


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


def _mock_guard(reason: str) -> dict[str, Any]:
    return {
        "ok": True,
        "enabled": False,
        "started": False,
        "mode": "idle",
        "reason": reason,
    }


def start_worker_ui_operator_guard(*, client_instance_id: str = "") -> dict[str, Any]:
    """Create the single Worker-owned guard process in gray idle mode."""

    global _WORKER_GUARD
    with _SERVICE_LOCK:
        if isinstance(_WORKER_GUARD, dict):
            health = worker_ui_operator_guard_health()
            if health.get("ok") is True:
                return _WORKER_GUARD
        if CONFIG.rpa_mode == "mock" or os.name != "nt":
            _WORKER_GUARD = _mock_guard("mock_or_non_windows_has_no_real_ui")
            return _WORKER_GUARD
        guard = _guard_adapter().start_rpa_operator_guard(
            operation="worker_lifecycle",
            initial_mode="idle",
            client_instance_id=client_instance_id,
        )
        _WORKER_GUARD = guard
        if guard.get("ok") is True:
            paths = guard.get("paths") if isinstance(guard.get("paths"), dict) else {}
            os.environ["CHEJIN_OPERATOR_GUARD_ROOT"] = str(paths.get("root") or "")
            os.environ["CHEJIN_OPERATOR_GUARD_INSTANCE_ID"] = str(guard.get("guard_instance_id") or "")
        return guard


def worker_ui_operator_guard_handle() -> dict[str, Any] | None:
    with _SERVICE_LOCK:
        return _WORKER_GUARD


def operator_guard_audit_metadata(
    payload: dict[str, Any] | None,
    *,
    reason: str = "",
    **overrides: Any,
) -> dict[str, Any]:
    """Normalize guard identity/state fields for every lifecycle audit event."""

    source = payload if isinstance(payload, dict) else {}
    state = source.get("state") if isinstance(source.get("state"), dict) else {}
    if not state:
        state = (
            source.get("final_state")
            if isinstance(source.get("final_state"), dict)
            else {}
        )
    if not state:
        verify = source.get("verify") if isinstance(source.get("verify"), dict) else {}
        state = verify.get("state") if isinstance(verify.get("state"), dict) else {}
    values = {
        "guard_instance_id": state.get("guard_instance_id") or source.get("guard_instance_id"),
        "active_ui_lock_id": state.get("active_ui_lock_id") or source.get("active_ui_lock_id") or "",
        "fencing_token": int(state.get("active_fencing_token") or source.get("active_fencing_token") or 0),
        "control_epoch": int(state.get("control_epoch") or source.get("control_epoch") or 0),
        "operation_type": state.get("operation_type") or source.get("operation_type") or "",
        "owner_worker_pid": int(state.get("owner_worker_pid") or source.get("owner_worker_pid") or 0),
        "guard_pid": int(state.get("pid") or source.get("pid") or 0),
        "mode": state.get("mode") or source.get("mode") or "fault",
        "lock_enabled": bool(
            state.get("lock_enabled")
            if "lock_enabled" in state
            else source.get("lock_enabled")
        ),
        "reason": reason or source.get("reason") or state.get("reason") or "",
        "current_step": state.get("current_step") or source.get("current_step") or "",
    }
    values.update(overrides)
    return values


def worker_ui_operator_guard_health() -> dict[str, Any]:
    with _SERVICE_LOCK:
        guard = _WORKER_GUARD
        if not isinstance(guard, dict):
            if CONFIG.rpa_mode == "mock" or os.name != "nt":
                return {"ok": True, "mode": "ready", "reason": "mock_or_non_windows_has_no_real_ui"}
            return {"ok": False, "mode": "fault", "reason": "operator_guard_not_started"}
        if not guard.get("enabled"):
            return {"ok": True, "mode": str(guard.get("mode") or "idle"), "reason": str(guard.get("reason") or "not_enabled")}
        return _guard_adapter().rpa_operator_guard_health(guard)


def set_worker_ui_operator_guard_mode(mode: str, *, reason: str) -> dict[str, Any]:
    with _SERVICE_LOCK:
        guard = _WORKER_GUARD
        if not isinstance(guard, dict) and (CONFIG.rpa_mode == "mock" or os.name != "nt"):
            guard = start_worker_ui_operator_guard()
        if not isinstance(guard, dict):
            return {"ok": False, "mode": "fault", "reason": "operator_guard_not_started"}
        if not guard.get("enabled"):
            guard["mode"] = mode
            return {"ok": True, "mode": mode, "skipped": True, "reason": reason}
        return _guard_adapter().transition_rpa_operator_guard(
            guard,
            mode=mode,
            reason=reason,
        )


def activate_worker_ui_operator_guard(
    *,
    lock_id: str,
    fencing_token: int,
    operation_type: str,
    current_step: str,
) -> dict[str, Any]:
    with _SERVICE_LOCK:
        guard = _WORKER_GUARD
        if not isinstance(guard, dict) and (CONFIG.rpa_mode == "mock" or os.name != "nt"):
            guard = start_worker_ui_operator_guard()
        if not isinstance(guard, dict):
            return {"ok": False, "mode": "fault", "reason": "operator_guard_not_started"}
        if not guard.get("enabled"):
            guard["mode"] = "active"
            return {"ok": True, "mode": "active", "lock_enabled": False, "skipped": True, "reason": str(guard.get("reason") or "not_enabled")}
        health = _guard_adapter().rpa_operator_guard_health(guard)
        if health.get("ok") is not True:
            return health
        if str(health.get("mode") or "") != "ready":
            return {
                "ok": False,
                "mode": str(health.get("mode") or "fault"),
                "reason": "operator_guard_not_ready_for_activation",
                "state": health.get("state"),
            }
        return _guard_adapter().transition_rpa_operator_guard(
            guard,
            mode="active",
            ui_lock_id=lock_id,
            fencing_token=fencing_token,
            operation_type=operation_type,
            current_step=current_step,
            reason="ui_lock_activated",
        )


def deactivate_worker_ui_operator_guard(
    *,
    lock_id: str,
    fencing_token: int,
    reason: str,
) -> dict[str, Any]:
    with _SERVICE_LOCK:
        guard = _WORKER_GUARD
        if not isinstance(guard, dict):
            return {"ok": False, "mode": "fault", "reason": "operator_guard_not_started"}
        if not guard.get("enabled"):
            guard["mode"] = "ready"
            return {"ok": True, "mode": "ready", "lock_enabled": False, "skipped": True, "reason": reason}
        health = _guard_adapter().rpa_operator_guard_health(guard)
        state = health.get("state") if isinstance(health.get("state"), dict) else {}
        if health.get("ok") is not True:
            return health
        if str(state.get("mode") or "") != "active":
            # F8 already unlocked and moved to paused/stopped. Preserve that
            # local state while allowing the abandoned UI lock to be collected.
            if str(state.get("mode") or "") in {"paused", "stopped", "fault"}:
                return {"ok": True, "mode": state.get("mode"), "lock_enabled": False, "already_deactivated": True, "state": state}
            return {"ok": False, "mode": "fault", "reason": "operator_guard_active_lock_missing", "state": state}
        if (
            str(state.get("active_ui_lock_id") or "") != lock_id
            or int(state.get("active_fencing_token") or 0) != int(fencing_token)
        ):
            return {"ok": False, "mode": "fault", "reason": "operator_guard_lock_identity_mismatch", "state": state}
        return _guard_adapter().transition_rpa_operator_guard(
            guard,
            mode="ready",
            reason=reason,
        )


def shutdown_worker_ui_operator_guard(*, reason: str) -> dict[str, Any]:
    global _WORKER_GUARD
    with _SERVICE_LOCK:
        guard = _WORKER_GUARD
        if not isinstance(guard, dict):
            return {"ok": True, "skipped": True, "reason": "operator_guard_not_started"}
        if not guard.get("enabled"):
            _WORKER_GUARD = None
            return {"ok": True, "skipped": True, "reason": str(guard.get("reason") or "not_enabled")}
        result = _guard_adapter().stop_rpa_operator_guard(guard, reason=reason)
        if result.get("ok") is True:
            _WORKER_GUARD = None
            os.environ.pop("CHEJIN_OPERATOR_GUARD_ROOT", None)
            os.environ.pop("CHEJIN_OPERATOR_GUARD_INSTANCE_ID", None)
        return result


# Compatibility names retained for UiLockLease. They switch the state of the
# persistent process; they never create or destroy a guard process.
def start_ui_operator_guard(
    *,
    lock_id: str,
    fencing_token: int,
    operation_type: str,
    owner: str,
    current_step: str,
) -> dict[str, Any]:
    result = activate_worker_ui_operator_guard(
        lock_id=lock_id,
        fencing_token=fencing_token,
        operation_type=operation_type,
        current_step=current_step,
    )
    return {**result, "lock_id": lock_id, "operation_type": operation_type, "owner": owner, "current_step": current_step}


def ui_operator_guard_health(_guard: dict[str, Any] | None = None) -> dict[str, Any]:
    return worker_ui_operator_guard_health()


def stop_ui_operator_guard(
    guard: dict[str, Any] | None,
    *,
    reason: str,
) -> dict[str, Any]:
    payload = guard if isinstance(guard, dict) else {}
    return deactivate_worker_ui_operator_guard(
        lock_id=str(payload.get("lock_id") or payload.get("active_ui_lock_id") or ""),
        fencing_token=int(payload.get("fencing_token") or payload.get("active_fencing_token") or 0),
        reason=reason,
    )


def cleanup_orphaned_ui_operator_guard(*, reason: str) -> dict[str, Any]:
    """Compatibility audit hook; startup ownership validation happens in start."""

    return {"ok": True, "cleaned": False, "reason": f"validated_by_worker_guard_start:{reason}"}
