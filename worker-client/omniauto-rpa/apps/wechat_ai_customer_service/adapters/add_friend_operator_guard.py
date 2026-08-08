"""Backward-compatible add_friend names for the generic RPA operator guard."""

from __future__ import annotations

from typing import Any

from apps.wechat_ai_customer_service.adapters.rpa_operator_guard import (
    attach_rpa_operator_guard,
    rpa_operator_guard_checkpoint,
    rpa_operator_guard_dir,
    rpa_operator_guard_paths,
    rpa_operator_guard_settings,
)


def add_friend_operator_guard_settings() -> dict[str, Any]:
    return rpa_operator_guard_settings()


def add_friend_operator_guard_dir(tenant_id: str | None = None):
    return rpa_operator_guard_dir(tenant_id)


def add_friend_operator_guard_paths(tenant_id: str | None = None) -> dict[str, Any]:
    return rpa_operator_guard_paths(tenant_id)


def start_add_friend_operator_guard(*, route: str = "", artifact_dir: str | None = None) -> dict[str, Any]:
    """Attach to the active Worker-owned guard; never spawn a Sidecar guard."""

    result = attach_rpa_operator_guard()
    return {**result, "route": route or "add_friend", "artifact_dir": str(artifact_dir or ""), "attached": True}


def stop_add_friend_operator_guard(guard: dict[str, Any] | None, *, reason: str = "add_friend_flow_finished") -> dict[str, Any]:
    return {"ok": True, "skipped": True, "reason": reason, "worker_owned_guard_kept_alive": True}


def add_friend_operator_guard_checkpoint(*, reason: str = "") -> dict[str, Any]:
    return rpa_operator_guard_checkpoint(reason=reason)
