"""Interpret an existing pre-trigger receipt; never authorize a physical send.

Worker and backend consume the same strict, dependency-free business rule.
The shared continuity comparator remains the owner of identity alignment.
"""
from __future__ import annotations

from typing import Any


def _object(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _corresponding_sequences(old_guard, snapshot, decision):
    """Recompute D1 from the same observations; never trust a claimed match."""
    from .historical_text_alignment import compare_historical_viewports
    from .message_viewport_projection import normalized_business_message_sequence
    contract = _object(old_guard.get('worker_continuity_contract'))
    historical = _object(contract.get('historical_alignment'))
    baseline = historical.get('baseline_observations')
    # The compact send sequence is for receipt occurrence counting. HC needs
    # the original observation fields, just as the Sidecar's guard does.
    current = snapshot.get('observations', snapshot.get('message_sequence'))
    checkpoint = historical.get('checkpoint')
    if not isinstance(baseline, list) or not isinstance(current, list) or not isinstance(checkpoint, dict):
        raise ValueError('interruption_correspondence_evidence_missing')
    if (normalized_business_message_sequence(baseline, message_viewport_bounds=None) != old_guard['sequence']
            or normalized_business_message_sequence(current, message_viewport_bounds=None)
            != snapshot['send_context_guard']['sequence']):
        raise ValueError('interruption_observations_changed')
    tokens = contract['old_boundary_tokens']
    if (not isinstance(tokens, dict) or any(not isinstance(v, list) or str(int(k)) != k for k, v in tokens.items())):
        raise ValueError('interruption_boundary_changed')
    compared = compare_historical_viewports(checkpoint, baseline, current,
        old_boundary_tokens={int(k): set(v) for k, v in tokens.items()})
    if compared is None:
        raise ValueError('interruption_correspondence_changed')
    old, new, rebuilt = compared
    if decision['text_correspondence'] != rebuilt['text_correspondence']:
        raise ValueError('interruption_correspondence_changed')
    if any(rebuilt.get(k) != decision.get(k) for k in
           ('relation', 'matched_pairs', 'new_suffix_indexes', 'overlap_candidates', 'old_count', 'new_count')):
        raise ValueError('interruption_continuity_changed')
    return old, new


def confirmed_customer_interruption(
    *, send_result: str, action_phase: str, error_code: str,
    evidence: dict, target: str,
) -> bool:
    return customer_interruption_proof(
        send_result=send_result, action_phase=action_phase, error_code=error_code,
        evidence=evidence, target=target,
    ) is not None


def _cleanup_completed(cleanup: dict) -> bool:
    if cleanup.get("ok") is not True:
        return False
    if "clear_attempted" in cleanup:
        # New clients record the one clear operation without claiming that
        # the field was observed empty. The next input replaces remaining text.
        return (cleanup.get("clear_attempted") is True
                and cleanup.get("method") == "select_all_backspace"
                and cleanup.get("reason") == "confirmed_program_draft_clear_requested")
    # Existing persisted receipts retain their original proof requirements.
    return (cleanup.get("cleared") is True
            and _object(cleanup.get("input_region")).get("has_visible_text") is False)


def confirmed_program_draft_cleanup(cleanup: object) -> bool:
    """Owned full draft plus completed cleanup operation, not an empty-field claim."""
    cleanup = _object(cleanup)
    focus = _object(cleanup.get("focus_check"))
    return bool(_cleanup_completed(cleanup)
                and focus.get("ok") is True
                and type(focus.get("expected_length")) is int
                and type(focus.get("observed_length")) is int
                and focus["expected_length"] == focus["observed_length"] > 0)


def customer_interruption_proof(
    *, send_result: str, action_phase: str, error_code: str,
    evidence: object, target: str,
) -> dict | None:
    """Facts shared by single and segmented replies, without choosing a flow."""
    if (send_result, action_phase, error_code) != (
        "failed", "not_attempted", "C3_CONTEXT_CHANGED_BEFORE_SEND"
    ) or not target:
        return None
    evidence = _object(evidence)
    guard = _object(_object(evidence).get("guard"))
    visual = _object(guard.get("visual"))
    cleanup = _object(visual.get("draft_clear"))
    check = _object(visual.get("context_check"))
    snapshot = _object(check.get("snapshot"))
    before_input = evidence.get("state") == "send_context_changed_before_input"
    if before_input:
        check = _object(evidence.get("context_validation"))
        snapshot = _object(evidence.get("send_baseline"))
        journal = _object(evidence.get("action_journal"))
        safe_input = (
            journal.get("ok") is True
            and journal.get("action_phase") == "not_attempted"
            and bool(snapshot.get("screenshot_path"))
            and guard.get("screenshot_path") == snapshot.get("screenshot_path")
        )
        old_guard = _object(check.get("expected_context_guard"))
    else:
        safe_input = (
            visual.get("physical_send_triggered") is False
            and visual.get("error_code") == error_code
            and confirmed_program_draft_cleanup(cleanup)
        )
        old_guard = _object(_object(guard.get("send_baseline")).get("send_context_guard"))
    validation = _object(snapshot.get("validation"))
    if not (
        guard.get("ok") is True
        and guard.get("confirmed_target") == target
        and guard.get("conversation_type") == "private"
        and safe_input
        and check.get("ok") is False and check.get("error_code") == error_code
        and snapshot.get("ok") is True and validation.get("ok") is True
        and validation.get("confirmed_target") == target
        and validation.get("conversation_type") == "private"
    ):
        return None
    new_guard = _object(snapshot.get("send_context_guard"))
    old, new = old_guard.get("sequence"), new_guard.get("sequence")
    decision = _object(check.get("worker_continuity_decision"))
    relation = decision.get("relation")
    if (
        not isinstance(relation, str)
        or relation not in {"unique_tail_append", "unique_viewport_slide_with_tail_append"}
        or check.get("continuity_relation") != relation
        or not isinstance(old, list) or not old
        or not isinstance(new, list) or not new
        or decision.get("old_count") != len(old)
        or decision.get("new_count") != len(new)
        or not old_guard.get("sequence_sha256") or not new_guard.get("sequence_sha256")
        or check.get("expected_sequence_sha256") != old_guard["sequence_sha256"]
        or check.get("current_sequence_sha256") != new_guard["sequence_sha256"]
    ):
        return None
    candidates = decision.get("overlap_candidates")
    pairs, suffix = decision.get("matched_pairs"), decision.get("new_suffix_indexes")
    if (not isinstance(candidates, list) or len(candidates) != 1
        or _object(candidates[0]).get("has_unique_strong_boundary") is not True
        or not isinstance(pairs, list) or not pairs
        or not isinstance(suffix, list) or not suffix):
        return None
    start = len(old) - len(pairs)
    if (start < 0 or len(pairs) >= len(new)
        or pairs != [{"old_index": start + i, "new_index": i} for i in range(len(pairs))]
        or suffix != list(range(len(pairs), len(new)))):
        return None
    # Confirm that the evidence actually describes the comparator's declared
    # overlap/new tail. These projections already exclude screen position.
    if decision.get('text_correspondence'):
        try:
            old, new = _corresponding_sequences(old_guard, snapshot, decision)
        except (ValueError, TypeError, KeyError, AttributeError):
            return None
    fields = ("sender_role", "message_type", "normalized_content_signature", "media_state")
    for pair in pairs:
        a, b = _object(old[pair["old_index"]]), _object(new[pair["new_index"]])
        if not a.get("normalized_content_signature") or any(a.get(k) != b.get(k) for k in fields):
            return None
    if not all(
        _object(new[i]).get("sender_role") == "customer"
        and _object(new[i]).get("message_type") in ("text", "voice", "image")
        and bool(_object(new[i]).get("normalized_content_signature"))
        for i in suffix
    ):
        return None
    return {"snapshot": snapshot, "check": check, "suffix": suffix,
            "before_input": before_input, "cleanup": cleanup}
