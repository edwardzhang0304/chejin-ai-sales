from __future__ import annotations

from typing import Any


IMAGE_PHASE_COUNTER_KEYS = (
    "discovered",
    "completed",
    "failed",
    "ignored",
    "cached",
    "authorization_revoked",
    "configuration_incomplete",
    "capability_paused",
    "deferred",
)


def new_image_phase_result() -> dict[str, Any]:
    result: dict[str, Any] = {
        key: 0 for key in IMAGE_PHASE_COUNTER_KEYS
    }
    result.update(
        {
            "new_action_source_keys": [],
            "terminal_source_keys": [],
            "unresolved_new_source_keys": [],
            "unresolved_reason_by_source": {},
            "new_action_count": 0,
            "requires_final_refresh": False,
            "must_block_brain": False,
            "brain_gate_codes": [],
        }
    )
    return result


def _clean_keys(values: Any) -> set[str]:
    return {
        str(value).strip()
        for value in (values or [])
        if str(value or "").strip()
    }


def finalize_image_phase_result(
    result: dict[str, Any],
) -> dict[str, Any]:
    action_keys = _clean_keys(result.get("new_action_source_keys"))
    terminal_keys = _clean_keys(result.get("terminal_source_keys"))
    reason_by_source = {
        str(key).strip(): str(value).strip()
        for key, value in (
            result.get("unresolved_reason_by_source") or {}
        ).items()
        if str(key).strip() and str(value).strip()
    }
    unresolved_keys = (
        _clean_keys(result.get("unresolved_new_source_keys"))
        | set(reason_by_source)
    ) - terminal_keys
    reason_by_source = {
        key: value
        for key, value in reason_by_source.items()
        if key in unresolved_keys
    }
    gate_codes = sorted(set(reason_by_source.values()))

    result["new_action_source_keys"] = sorted(action_keys)
    result["terminal_source_keys"] = sorted(terminal_keys)
    result["unresolved_new_source_keys"] = sorted(unresolved_keys)
    result["unresolved_reason_by_source"] = reason_by_source
    result["new_action_count"] = len(action_keys)
    result["requires_final_refresh"] = bool(action_keys)
    result["must_block_brain"] = bool(unresolved_keys)
    result["brain_gate_codes"] = gate_codes
    return result


def mark_image_action(
    result: dict[str, Any],
    source_message_key: str,
) -> None:
    result.setdefault("new_action_source_keys", []).append(
        str(source_message_key or "").strip()
    )
    finalize_image_phase_result(result)


def mark_image_terminal(
    result: dict[str, Any],
    source_message_key: str,
) -> None:
    result.setdefault("terminal_source_keys", []).append(
        str(source_message_key or "").strip()
    )
    finalize_image_phase_result(result)


def mark_image_unresolved(
    result: dict[str, Any],
    source_message_key: str,
    gate_code: str,
) -> None:
    key = str(source_message_key or "").strip()
    code = str(gate_code or "").strip()
    if not key or not code:
        return
    result.setdefault("unresolved_new_source_keys", []).append(key)
    result.setdefault("unresolved_reason_by_source", {})[key] = code
    finalize_image_phase_result(result)


def merge_image_phase_results(
    target: dict[str, Any],
    incoming: dict[str, Any],
) -> dict[str, Any]:
    for key in IMAGE_PHASE_COUNTER_KEYS:
        # Every refreshed frame reports prior terminal images as cached.
        # Keep screen-level counters monotonic without counting those slots
        # again; action identity is merged separately below.
        target[key] = max(
            int(target.get(key) or 0),
            int(incoming.get(key) or 0),
        )
    target.setdefault("new_action_source_keys", []).extend(
        incoming.get("new_action_source_keys") or []
    )
    target.setdefault("terminal_source_keys", []).extend(
        incoming.get("terminal_source_keys") or []
    )
    target.setdefault("unresolved_new_source_keys", []).extend(
        incoming.get("unresolved_new_source_keys") or []
    )
    target.setdefault("unresolved_reason_by_source", {}).update(
        incoming.get("unresolved_reason_by_source") or {}
    )
    return finalize_image_phase_result(target)
