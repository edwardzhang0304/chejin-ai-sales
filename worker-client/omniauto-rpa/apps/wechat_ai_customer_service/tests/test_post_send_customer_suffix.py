"""Synthetic observation vectors; no physical Windows send is represented."""
from copy import deepcopy
import pytest
from apps.wechat_ai_customer_service.adapters.text_correspondence import (
    find_new_matching_self_message, confirmed_post_send_customer_suffix,
)


def row(identity, role, text, kind="text_bubble"):
    return {"observation_id": identity, "sender_role": role, "row_kind": kind,
            "content_normalized": text}


def receipt(kind="text_bubble"):
    before = [row("history", "customer", "请介绍一下这辆车的情况")]
    after = before + [row("sent", "self", "好的，我帮您核实这辆车。"),
                      row("new", "customer", "我想改看另一辆车", kind)]
    def frame(identity, sequence):
        return {"ok": True, "frame_observation": {"frame_id": identity},
                "validation": {"confirmed_target": "C3TEST01"},
                "message_sequence": sequence, "observations": sequence,
                "input_region": {"has_visible_text": False}}
    return {"target": "C3TEST01", "send_result": {
        "confirmed": True, "result": "sent", "send_baseline": frame("before", before),
        "sent_confirmation": {"ok": True, "confirmed_observation": after[1],
                              "snapshot": frame("after", after)},
    }}


@pytest.mark.parametrize("kind", ["text_bubble", "image_bubble", "voice_bubble", "voice_transcript"])
def test_customer_after_confirmed_bubble_is_a_fresh_read_signal(kind):
    proof = confirmed_post_send_customer_suffix(receipt(kind), target="C3TEST01", text="好的，我帮您核实这辆车。")
    assert proof == {"version": 1, "frame_id": "after", "observation_ids": ["new"],
                     "confirmed_observation_id": "sent"}


@pytest.mark.parametrize("change", ["wrong_target", "draft", "same_frame", "duplicate_self", "old_only", "unknown_role", "wrong_body", "forged_candidate", "duplicate_observation"])
def test_ambiguous_or_unproven_send_never_creates_read_intent(change):
    evidence = deepcopy(receipt())
    send = evidence["send_result"]
    snapshot = send["sent_confirmation"]["snapshot"]
    sequence = snapshot["message_sequence"]
    if change == "wrong_target": snapshot["validation"]["confirmed_target"] = "OTHER001"
    elif change == "draft": snapshot["input_region"]["has_visible_text"] = True
    elif change == "same_frame": snapshot["frame_observation"]["frame_id"] = "before"
    elif change == "duplicate_self": sequence.append(row("second", "self", "好的，我帮您核实这辆车。"))
    elif change == "old_only": send["send_baseline"]["message_sequence"] = sequence[:2]
    elif change == "unknown_role": sequence[-1]["sender_role"] = "unknown"
    elif change == "wrong_body": sequence[1]["content_normalized"] = "完全不同的手工回复"
    elif change == "forged_candidate": send["sent_confirmation"]["confirmed_observation"] = row("forged", "self", "假的")
    elif change == "duplicate_observation": sequence.append(deepcopy(sequence[-1]))
    assert confirmed_post_send_customer_suffix(evidence, target="C3TEST01", text="好的，我帮您核实这辆车。") is None


def test_repeated_baseline_and_clipped_old_identical_bubble_are_not_new_sends():
    old = [row("a", "customer", "好的"), row("b", "customer", "好的")]
    new = old + [row("sent", "self", "这是新的回复")]
    assert find_new_matching_self_message(old, new, "这是新的回复") is None
    old = [row("a", "customer", "你好"), row("old", "self", "这是原来的回复")]
    assert find_new_matching_self_message(old, old[-1:], "这是原来的回复") is None
