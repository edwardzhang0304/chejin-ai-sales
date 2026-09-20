"""Fail-closed interpretation of the existing shared continuity receipt."""
from copy import deepcopy

import pytest

from apps.wechat_ai_customer_service.adapters.send_interruption import confirmed_customer_interruption


def receipt():
    old = [{"sender_role": "customer", "message_type": "text", "normalized_content_signature": "old", "media_state": None}]
    new = [*deepcopy(old), {**old[0], "normalized_content_signature": "new"}]
    context = {
        "ok": False, "error_code": "C3_CONTEXT_CHANGED_BEFORE_SEND",
        "continuity_relation": "unique_tail_append",
        "expected_sequence_sha256": "old-digest", "current_sequence_sha256": "new-digest",
        "snapshot": {"ok": True, "validation": {"ok": True, "confirmed_target": "CJTEST01", "conversation_type": "private"},
                     "send_context_guard": {"sequence": new, "sequence_sha256": "new-digest"}},
        "worker_continuity_decision": {
            "relation": "unique_tail_append", "old_count": 1, "new_count": 2,
            "overlap_candidates": [{"has_unique_strong_boundary": True}],
            "matched_pairs": [{"old_index": 0, "new_index": 0}], "new_suffix_indexes": [1],
        },
    }
    return {"send_result": "failed", "action_phase": "not_attempted", "error_code": "C3_CONTEXT_CHANGED_BEFORE_SEND",
            "target": "CJTEST01", "evidence": {"guard": {
                "ok": True, "confirmed_target": "CJTEST01", "conversation_type": "private",
                "send_baseline": {"send_context_guard": {"sequence": old, "sequence_sha256": "old-digest"}},
                "visual": {"physical_send_triggered": False, "error_code": "C3_CONTEXT_CHANGED_BEFORE_SEND",
                           "context_check": context,
                           "draft_clear": {"ok": True, "cleared": True, "input_region": {"has_visible_text": False},
                                           "focus_check": {"ok": True, "expected_length": 8, "observed_length": 8}}}}}}


def test_confirmed_customer_append():
    assert confirmed_customer_interruption(**receipt())


@pytest.mark.parametrize("remaining", [None, False, True])
def test_once_only_clear_does_not_require_empty_input(remaining):
    data = receipt()
    cleanup = data["evidence"]["guard"]["visual"]["draft_clear"]
    cleanup.update(cleared=False, clear_attempted=True, method="select_all_backspace",
                   reason="confirmed_program_draft_clear_requested")
    cleanup["input_region"] = {} if remaining is None else {"has_visible_text": remaining}
    assert confirmed_customer_interruption(**data)


@pytest.mark.parametrize("damage", ["clear_failed", "not_attempted", "wrong_method", "wrong_reason", "unowned", "sent"])
def test_once_only_clear_keeps_ownership_and_no_send_guards(damage):
    data = receipt()
    visual = data["evidence"]["guard"]["visual"]
    cleanup = visual["draft_clear"]
    cleanup.update(cleared=False, clear_attempted=True, method="select_all_backspace",
                   reason="confirmed_program_draft_clear_requested")
    if damage == "clear_failed": cleanup['ok'] = False
    elif damage == "not_attempted": cleanup['clear_attempted'] = False
    elif damage == "wrong_method": cleanup['method'] = "unverified_delete"
    elif damage == "wrong_reason": cleanup['reason'] = "input_clear_requested_before_replacement"
    elif damage == "unowned": cleanup['focus_check']['observed_length'] = 1
    elif damage == "sent": visual['physical_send_triggered'] = True
    assert not confirmed_customer_interruption(**data)


@pytest.mark.parametrize("field,value", [
    ("send_result", "unknown"), ("action_phase", "attempted_unknown"),
    ("error_code", "RPA_SEND_REPLY_FAILED"), ("target", "OTHER"),
    ("evidence", {}), ("evidence", None),
])
def test_other_receipts_stay_protected(field, value):
    data = receipt(); data[field] = value
    assert not confirmed_customer_interruption(**data)


@pytest.mark.parametrize("path,value", [
    (("ok",), False),
    (("visual", "context_check", "worker_continuity_decision", "relation"), {}),
    (("visual", "draft_clear", "focus_check", "expected_length"), True), (("conversation_type",), "group"),
    (("visual", "physical_send_triggered"), True),
    (("visual", "draft_clear", "cleared"), False),
    (("visual", "draft_clear", "focus_check", "ok"), False),
    (("visual", "draft_clear", "focus_check", "observed_length"), 9),
    (("visual", "draft_clear", "input_region", "has_visible_text"), True),
    (("visual", "context_check", "snapshot", "validation", "confirmed_target"), "OTHER"),
    (("visual", "context_check", "snapshot", "ok"), False),
    (("visual", "context_check", "continuity_relation"), "unmatched"),
    (("visual", "context_check", "expected_sequence_sha256"), "stale"),
    (("visual", "context_check", "worker_continuity_decision", "overlap_candidates"), []),
    (("visual", "context_check", "worker_continuity_decision", "overlap_candidates"), [{"has_unique_strong_boundary": False}]),
    (("visual", "context_check", "worker_continuity_decision", "new_suffix_indexes"), []),
    (("visual", "context_check", "worker_continuity_decision", "matched_pairs"), [{"old_index": 0, "new_index": 1}]),
])
def test_incomplete_or_ambiguous_evidence_stays_protected(path, value):
    data = receipt(); node = data["evidence"]["guard"]
    for key in path[:-1]: node = node[key]
    node[path[-1]] = value
    assert not confirmed_customer_interruption(**data)


@pytest.mark.parametrize("role,kind", [("self", "text"), ("unknown", "text"), ("customer", "unknown")])
def test_sales_or_uncertain_new_message_is_not_customer_interruption(role, kind):
    data = receipt()
    new = data["evidence"]["guard"]["visual"]["context_check"]["snapshot"]["send_context_guard"]["sequence"]
    new[-1].update(sender_role=role, message_type=kind)
    assert not confirmed_customer_interruption(**data)


def test_changed_history_is_not_a_safe_append():
    data = receipt()
    data["evidence"]["guard"]["visual"]["context_check"]["snapshot"]["send_context_guard"]["sequence"][0]["normalized_content_signature"] = "changed"
    assert not confirmed_customer_interruption(**data)


def test_mixed_customer_and_sales_suffix_stays_protected():
    data = receipt()
    check = data["evidence"]["guard"]["visual"]["context_check"]
    sequence = check["snapshot"]["send_context_guard"]["sequence"]
    sequence.append({**sequence[-1], "sender_role": "self", "normalized_content_signature": "sales"})
    check["worker_continuity_decision"].update(new_count=3, new_suffix_indexes=[1, 2])
    assert not confirmed_customer_interruption(**data)
