"""Interpret an existing pre-trigger receipt; never authorize a physical send.

Worker and backend consume the same strict, dependency-free business rule.
The shared continuity comparator remains the owner of identity alignment.
"""
from __future__ import annotations

from typing import Any


def _object(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def confirmed_customer_interruption(
    *, send_result: str, action_phase: str, error_code: str,
    evidence: dict, target: str,
) -> bool:
    if (send_result, action_phase, error_code) != (
        "failed", "not_attempted", "C3_CONTEXT_CHANGED_BEFORE_SEND"
    ) or not target:
        return False
    guard = _object(_object(evidence).get("guard"))
    visual = _object(guard.get("visual"))
    cleanup = _object(visual.get("draft_clear"))
    focus = _object(cleanup.get("focus_check"))
    check = _object(visual.get("context_check"))
    snapshot = _object(check.get("snapshot"))
    validation = _object(snapshot.get("validation"))
    if not (
        guard.get("ok") is True
        and guard.get("confirmed_target") == target
        and guard.get("conversation_type") == "private"
        and visual.get("physical_send_triggered") is False
        and visual.get("error_code") == error_code
        and cleanup.get("ok") is True and cleanup.get("cleared") is True
        and focus.get("ok") is True
        and focus.get("expected_length") == focus.get("observed_length")
        and type(focus.get("expected_length")) is int
        and type(focus.get("observed_length")) is int
        and focus["expected_length"] > 0
        and _object(cleanup.get("input_region")).get("has_visible_text") is False
        and check.get("ok") is False and check.get("error_code") == error_code
        and snapshot.get("ok") is True and validation.get("ok") is True
        and validation.get("confirmed_target") == target
        and validation.get("conversation_type") == "private"
    ):
        return False
    old_guard = _object(_object(guard.get("send_baseline")).get("send_context_guard"))
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
        return False
    candidates = decision.get("overlap_candidates")
    pairs, suffix = decision.get("matched_pairs"), decision.get("new_suffix_indexes")
    if (not isinstance(candidates, list) or len(candidates) != 1
        or _object(candidates[0]).get("has_unique_strong_boundary") is not True
        or not isinstance(pairs, list) or not pairs
        or not isinstance(suffix, list) or not suffix):
        return False
    start = len(old) - len(pairs)
    if (start < 0 or len(pairs) >= len(new)
        or pairs != [{"old_index": start + i, "new_index": i} for i in range(len(pairs))]
        or suffix != list(range(len(pairs), len(new)))):
        return False
    # Confirm that the evidence actually describes the comparator's declared
    # overlap/new tail. These projections already exclude screen position.
    fields = ("sender_role", "message_type", "normalized_content_signature", "media_state")
    for pair in pairs:
        a, b = _object(old[pair["old_index"]]), _object(new[pair["new_index"]])
        if not a.get("normalized_content_signature") or any(a.get(k) != b.get(k) for k in fields):
            return False
    return all(
        _object(new[i]).get("sender_role") == "customer"
        and _object(new[i]).get("message_type") in ("text", "voice", "image")
        and bool(_object(new[i]).get("normalized_content_signature"))
        for i in suffix
    )
