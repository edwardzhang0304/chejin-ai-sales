"""Pure state transitions for the single backend recovery-hold record.

This module deliberately knows nothing about OCR, message identity, HTTP, or
Handoff creation.  It keeps the timer/flow interlock in one implementation so
scan, flow registration, ingest, and scheduling cannot invent competing rules.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any


def suspend_recovery_hold(
    binding: Any,
    *,
    read_run_id: str | None,
    unread_generation: int,
    observed_at: datetime,
) -> dict:
    current = dict(getattr(binding, "recovery_hold", None) or {})
    if current.get("status") not in {"active", "suspended"}:
        return current
    current.update(
        {
            "status": "suspended",
            "last_seen_at": observed_at.isoformat(),
            "suspended_by_read_run_id": (
                str(read_run_id or "").strip() or None
            ),
            "suspended_unread_generation": max(
                0,
                int(unread_generation or 0),
            ),
        }
    )
    # A deferred decision is meaningful only for the flow that produced it.
    # A newer generation/flow must establish its own terminal evidence.
    current.pop("deferred_hold_after_flow", None)
    binding.recovery_hold = current
    return current


def defer_recovery_hold_until_flow_terminal(
    binding: Any,
    *,
    candidate: dict,
    read_run_id: str,
    unread_generation: int,
    observed_at: datetime,
) -> dict:
    """Keep a candidate hold transition inert until its flow is terminal."""

    existing = dict(getattr(binding, "recovery_hold", None) or {})
    suspended = existing or dict(candidate)
    suspended.update(
        {
            "status": "suspended",
            "last_seen_at": observed_at.isoformat(),
            "suspended_by_read_run_id": str(read_run_id or "").strip(),
            "suspended_unread_generation": max(
                0,
                int(unread_generation or 0),
            ),
            "deferred_hold_after_flow": dict(candidate),
        }
    )
    binding.recovery_hold = suspended
    return suspended


def settle_recovery_hold_after_flow(
    binding: Any,
    *,
    read_run_id: str,
    technical_failed: bool,
    resume_without_deferred: bool = False,
) -> dict:
    """Apply only the transition proven by the just-finished flow."""

    current = dict(getattr(binding, "recovery_hold", None) or {})
    if current.get("status") != "suspended":
        return current
    if str(current.get("suspended_by_read_run_id") or "") != str(
        read_run_id or ""
    ):
        return current
    deferred = current.get("deferred_hold_after_flow")
    if technical_failed:
        current.pop("deferred_hold_after_flow", None)
        binding.recovery_hold = current
        return current
    if not isinstance(deferred, dict):
        if resume_without_deferred:
            # A C3 flow protects the conversation while it is sending but it
            # does not produce a new C2 identity decision.  Once that flow is
            # terminal, resume the exact old hold (including first_seen_at)
            # instead of leaving it suspended forever or resetting its timer.
            current.update(
                {
                    "status": "active",
                    "suspended_by_read_run_id": None,
                    "suspended_unread_generation": None,
                }
            )
        current.pop("deferred_hold_after_flow", None)
        binding.recovery_hold = current
        return current
    settled = dict(deferred)
    settled.pop("deferred_hold_after_flow", None)
    settled["suspended_by_read_run_id"] = None
    settled["suspended_unread_generation"] = None
    binding.recovery_hold = settled
    return settled


def inflight_flow_matches_conversation(
    worker: Any,
    *,
    conversation_id: str,
) -> bool:
    state = dict(getattr(worker, "inflight_flow_state", None) or {})
    return bool(
        state.get("status") in {"active", "draining"}
        and str(state.get("flow_id") or "").strip()
        and str(state.get("conversation_id") or "").strip()
        == str(conversation_id or "").strip()
    )
