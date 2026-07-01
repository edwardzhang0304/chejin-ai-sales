"""Backward-compatible add_friend names for the generic RPA operator guard."""

from __future__ import annotations

from typing import Any

from apps.wechat_ai_customer_service.adapters.rpa_operator_guard import (
    rpa_operator_guard_checkpoint,
    rpa_operator_guard_dir,
    rpa_operator_guard_paths,
    rpa_operator_guard_settings,
    start_rpa_operator_guard,
    stop_rpa_operator_guard,
)


def add_friend_operator_guard_settings() -> dict[str, Any]:
    return rpa_operator_guard_settings()


def add_friend_operator_guard_dir(tenant_id: str | None = None):
    return rpa_operator_guard_dir(tenant_id)


def add_friend_operator_guard_paths(tenant_id: str | None = None) -> dict[str, Any]:
    return rpa_operator_guard_paths(tenant_id)


def start_add_friend_operator_guard(*, route: str = "", artifact_dir: str | None = None) -> dict[str, Any]:
    return start_rpa_operator_guard(operation=route or "add_friend", artifact_dir=artifact_dir)


def stop_add_friend_operator_guard(guard: dict[str, Any] | None, *, reason: str = "add_friend_flow_finished") -> dict[str, Any]:
    return stop_rpa_operator_guard(guard, reason=reason)


def add_friend_operator_guard_checkpoint(*, reason: str = "") -> dict[str, Any]:
    return rpa_operator_guard_checkpoint(reason=reason)
