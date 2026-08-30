from __future__ import annotations

import copy
from typing import Any, Callable, Iterable
from pathlib import Path

from .api import ApiError
from .c2_contract import c2_contract_v3, contract_values


class FlowOutcomeAccumulator:
    """Keep irreversible item outcomes monotonic until one common finalize."""

    def __init__(
        self,
        *,
        checkpoint: Callable[[list[dict[str, Any]]], None] | None = None,
        origin_read_run_id: str | None = None,
    ) -> None:
        self._item_outcomes: list[dict[str, Any]] = []
        self._checkpoint = checkpoint
        self._action_journal_paths: set[Path] = set()
        self._origin_read_run_id = str(origin_read_run_id or "").strip()
        # A text context menu can disprove one provisional image candidate.
        # This receipt is intentionally scoped to this in-memory read flow:
        # it is neither a durable message identity nor an Outbox fact.
        self._confirmed_text_candidate_receipts: list[dict[str, Any]] = []

    @property
    def origin_read_run_id(self) -> str:
        return self._origin_read_run_id

    def _with_origin(
        self,
        outcomes: Iterable[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        for raw in outcomes:
            item = dict(raw)
            existing = str(item.get("origin_read_run_id") or "").strip()
            if existing and self._origin_read_run_id and existing != self._origin_read_run_id:
                raise ValueError("C2_FLOW_OUTCOME_ORIGIN_READ_RUN_ID_CONFLICT")
            if self._origin_read_run_id:
                item["origin_read_run_id"] = self._origin_read_run_id
            normalized.append(item)
        return normalized

    def record(self, *outcomes: dict[str, Any]) -> None:
        self._item_outcomes = merge_item_outcomes(
            self._item_outcomes,
            self._with_origin(outcomes),
        )
        self._persist_checkpoint()

    def extend(self, outcomes: Iterable[dict[str, Any]] | None) -> None:
        self._item_outcomes = merge_item_outcomes(
            self._item_outcomes,
            self._with_origin(outcomes or []),
        )
        self._persist_checkpoint()

    def snapshot(self) -> list[dict[str, Any]]:
        return [dict(item) for item in self._item_outcomes]

    def replace_source_key(self, old_source_key: str, new_source_key: str) -> None:
        """Commit one action-local outcome to its durable message identity."""

        old_key = str(old_source_key or "").strip()
        new_key = str(new_source_key or "").strip()
        if not old_key or not new_key:
            raise ValueError("C2_FLOW_OUTCOME_SOURCE_KEY_MISSING")
        if old_key == new_key:
            return
        remapped: list[dict[str, Any]] = []
        for raw in self._item_outcomes:
            item = dict(raw)
            if str(item.get("source_message_key") or "").strip() == old_key:
                item["source_message_key"] = new_key
            remapped.append(item)
        self._item_outcomes = merge_item_outcomes([], remapped)
        self._persist_checkpoint()

    def register_action_journal(self, path: str | Path) -> None:
        self._action_journal_paths.add(Path(path))

    def action_journal_paths(self) -> list[Path]:
        return sorted(self._action_journal_paths)

    def record_confirmed_text_candidate_receipt(
        self,
        receipt: dict[str, Any],
    ) -> None:
        """Remember a menu-confirmed text candidate for this read run only."""

        item = copy.deepcopy(receipt)
        receipt_origin = str(item.get("origin_read_run_id") or "").strip()
        if (
            not self._origin_read_run_id
            or receipt_origin != self._origin_read_run_id
        ):
            raise ValueError("C2_CONFIRMED_TEXT_RECEIPT_ORIGIN_CONFLICT")
        receipt_id = str(item.get("receipt_id") or "").strip()
        fallback_digest = str(
            item.get("fallback_business_projection_digest") or ""
        ).strip()
        fallback_projection = item.get("fallback_business_projection")
        if (
            int(item.get("schema_version") or 0) != 1
            or not receipt_id
            or not fallback_digest
            or not isinstance(fallback_projection, list)
            or not fallback_projection
        ):
            raise ValueError("C2_CONFIRMED_TEXT_RECEIPT_INVALID")
        for existing in self._confirmed_text_candidate_receipts:
            if str(existing.get("receipt_id") or "").strip() != receipt_id:
                continue
            if existing != item:
                raise ValueError("C2_CONFIRMED_TEXT_RECEIPT_COLLISION")
            return
        self._confirmed_text_candidate_receipts.append(item)

    def confirmed_text_candidate_receipts(self) -> list[dict[str, Any]]:
        """Return read-run-local type receipts without exposing mutable state."""

        return copy.deepcopy(self._confirmed_text_candidate_receipts)

    def _persist_checkpoint(self) -> None:
        if self._checkpoint is not None:
            self._checkpoint(self.snapshot())


def _action_result_contract() -> dict[str, Any]:
    value = c2_contract_v3().get("action_result_contract")
    if not isinstance(value, dict):
        raise RuntimeError("Invalid C2 action_result_contract")
    return value


def classify_action_result(
    action_kind: str,
    payload: dict[str, Any] | None,
    *,
    source_message_key: str | None = None,
) -> dict[str, Any]:
    """Map one OmniAuto action envelope to the only Worker result shape."""

    action = str(action_kind or "").strip().lower()
    raw = payload if isinstance(payload, dict) else {}
    contract = _action_result_contract()
    allowed_phases = contract_values("action_phases")
    phase = str(raw.get("action_phase") or "").strip().lower()
    phase_missing = phase not in allowed_phases
    if phase_missing:
        # Missing per-item evidence must never authorize a repeated physical
        # action. Conservatively preserve the no-repeat barrier for every
        # irreversible action kind.
        phase = str(
            contract.get("missing_phase_fallback")
            or "trigger_attempted"
        )

    phase_map = contract.get(action)
    if not isinstance(phase_map, dict) or phase not in phase_map:
        raise ValueError(f"Unsupported action result mapping: {action}:{phase}")
    result = str(phase_map[phase])
    observed_phase = phase
    evidence = dict(raw.get("evidence") or raw)
    nested_send = (
        raw.get("send_result")
        if isinstance(raw.get("send_result"), dict)
        else {}
    )
    business_state = str(
        raw.get("business_state")
        or (nested_send.get("result") if action == "send" else "")
        or raw.get("state")
        or evidence.get("business_state")
        or ""
    ).strip().lower()
    explicit_business_confirmation = raw.get("business_result_confirmed")
    if not isinstance(explicit_business_confirmation, bool):
        explicit_business_confirmation = evidence.get(
            "business_result_confirmed"
        )
    if action == "send" and nested_send:
        business_confirmed = bool(
            nested_send.get("confirmed") is True
            and nested_send.get("result") == "sent"
            and (
                raw.get("physical_send_triggered") is True
                or nested_send.get("physical_send_triggered") is True
            )
        )
    else:
        business_confirmed = explicit_business_confirmation is True

    completion_contract = contract.get("business_completion")
    action_completion = (
        completion_contract.get(action)
        if isinstance(completion_contract, dict)
        else None
    )
    if phase == "confirmed" and isinstance(action_completion, dict):
        completed_states = {
            str(value).strip().lower()
            for value in (action_completion.get("completed_states") or [])
        }
        if not business_confirmed or business_state not in completed_states:
            result = str(
                action_completion.get("unconfirmed_result") or "failed"
            )
            error_code = str(
                action_completion.get("unconfirmed_error_code")
                or f"{action.upper()}_BUSINESS_RESULT_UNCONFIRMED"
            )
            if action == "send":
                phase = "trigger_attempted"
            return {
                "action_phase": phase,
                "observed_action_phase": observed_phase,
                "business_state": business_state or None,
                "business_result_confirmed": False,
                "evidence_sufficient": False,
                "result": result,
                "error_code": error_code,
                "evidence": evidence,
                "source_message_key": (
                    str(source_message_key or "").strip() or None
                ),
                "contract_valid": False,
            }

    error_code = str(raw.get("error_code") or raw.get("reason") or "").strip()
    if phase_missing:
        error_code = f"{action.upper()}_ACTION_PHASE_MISSING"
    elif phase == "trigger_attempted" and result in {"unknown", "failed"}:
        unknown_codes = contract.get("unknown_error_codes")
        if isinstance(unknown_codes, dict):
            error_code = error_code or str(unknown_codes.get(action) or "")

    return {
        "action_phase": phase,
        "observed_action_phase": observed_phase,
        "business_state": business_state or None,
        "business_result_confirmed": business_confirmed,
        "evidence_sufficient": (
            business_confirmed if phase == "confirmed" else not phase_missing
        ),
        "result": result,
        "error_code": error_code or None,
        "evidence": evidence,
        "source_message_key": str(source_message_key or "").strip() or None,
        "contract_valid": not phase_missing,
    }


def merge_item_outcomes(
    existing: Iterable[dict[str, Any]] | None,
    additional: Iterable[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """Monotonically merge terminal item outcomes by source_message_key."""

    merged: dict[str, dict[str, Any]] = {}
    for item in [*(existing or []), *(additional or [])]:
        if not isinstance(item, dict):
            continue
        source_key = str(item.get("source_message_key") or "").strip()
        result = str(item.get("result") or "").strip().lower()
        if not source_key or result not in {"completed", "failed"}:
            continue
        previous = merged.get(source_key)
        if previous is not None and previous.get("result") != result:
            raise ValueError(f"ITEM_OUTCOME_TERMINAL_CONFLICT:{source_key}")
        if previous is None:
            merged[source_key] = dict(item)
            continue
        evidence = dict(previous.get("evidence") or {})
        evidence.update(
            item.get("evidence") if isinstance(item.get("evidence"), dict) else {}
        )
        merged[source_key] = {
            **previous,
            **item,
            "source_message_key": source_key,
            "result": result,
            "evidence": evidence,
        }
    return [merged[key] for key in sorted(merged)]


def classify_outbox_recovery(value: BaseException | str | None) -> str:
    """Normalize transport failures or a backend-owned recovery_action."""

    contract = c2_contract_v3().get("outbox_recovery_contract")
    if not isinstance(contract, dict):
        raise RuntimeError("Invalid C2 outbox_recovery_contract")
    allowed = {
        str(value)
        for value in (contract.get("actions") or [])
    }
    if isinstance(value, BaseException) and not isinstance(value, ApiError):
        return str(contract.get("transport_error_action") or "retry")
    action = str(
        value.recovery_action if isinstance(value, ApiError) else value or ""
    ).strip()
    if action in allowed:
        return action
    if isinstance(value, ApiError):
        code = str(value.code or "").strip()
        for field, mapped_action in (
            ("identity_quarantined_codes", "identity_quarantined"),
            ("refresh_and_rebuild_codes", "refresh_and_rebuild"),
            ("split_and_retry_codes", "split_and_retry"),
            ("target_terminated_codes", "target_terminated"),
            ("conversation_terminated_codes", "conversation_terminated"),
            ("capability_paused_codes", "capability_paused"),
        ):
            if code in {
                str(item)
                for item in (contract.get(field) or [])
            }:
                return mapped_action
    return str(
        contract.get("unknown_api_error_action")
        or "capability_paused"
    )


def transition_outbox_state(
    *,
    current_state: str,
    event: str,
    attempt_count: int,
    refresh_attempt_count: int,
) -> str:
    """Apply the only contract-owned Outbox state transition table."""

    contract = c2_contract_v3().get("outbox_recovery_contract")
    machine = contract.get("state_machine") if isinstance(contract, dict) else None
    if not isinstance(machine, dict):
        raise RuntimeError("Invalid C2 outbox state_machine")
    states = {str(value) for value in (machine.get("states") or [])}
    state = str(current_state or "").strip()
    transition = str(event or "").strip()
    if state not in states:
        return "capability_paused"
    if state == "confirmed":
        return state
    transitions = machine.get("transitions")
    next_state = (
        str(transitions.get(transition) or "")
        if isinstance(transitions, dict)
        else ""
    )
    return (
        next_state
        if next_state in states
        else "capability_paused"
    )
