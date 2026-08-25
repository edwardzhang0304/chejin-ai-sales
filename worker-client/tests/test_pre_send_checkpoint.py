from __future__ import annotations

from chejin_worker_client.pre_send_checkpoint import (
    canonical_sha256,
    checkpoint_binding_error,
    compare_checkpoint_to_observations,
    reply_fact_evidence_for_observation,
    stable_fact_signature,
)


def _fact(
    stable_id: str,
    *,
    sender_role: str,
    message_type: str,
    content: str = "",
    image_fingerprint: str = "",
    native_source_message_id: str = "",
    voice_duration: str = "",
    terminal_action_receipt: bool = False,
) -> dict:
    signature = stable_fact_signature(
        sender_role=sender_role,
        message_type=message_type,
        item_state="completed",
        content=content,
        voice_duration=voice_duration,
        image_visual_fingerprint=image_fingerprint,
    )
    fact_observation = {
        "row_kind": (
            "voice_transcript"
            if message_type == "voice"
            else "image_bubble"
            if message_type == "image"
            else "text_bubble"
        ),
        "sender_role": sender_role,
        "message_type": message_type,
        "voice_state": "transcribed",
        "content_clean": content,
        "voice_duration": voice_duration,
        "image_physical_anchor": {
            "bubble_visual_fingerprint": image_fingerprint,
        },
    }
    reply_fact_evidence = reply_fact_evidence_for_observation(
        fact_observation,
        item_state="completed",
    )
    if message_type in {"voice", "image"}:
        continuity_basis = (
            "native_source_message_id"
            if native_source_message_id
            else "terminal_committed_fact_equivalence"
            if terminal_action_receipt
            else "unproven_media_continuity"
        )
        continuity_signature = (
            canonical_sha256(
                {
                    "message_type": message_type,
                    "native_source_message_id": native_source_message_id,
                }
            )
            if native_source_message_id
            else canonical_sha256(reply_fact_evidence)
            if terminal_action_receipt
            else ""
        )
    else:
        continuity_basis = "ordered_fact"
        continuity_signature = signature
    return {
        "worker_stable_id": stable_id,
        "sender_role": sender_role,
        "message_type": message_type,
        "item_state": "completed",
        "stable_fact_signature": signature,
        "continuity_basis": continuity_basis,
        "continuity_signature": continuity_signature,
        "commit_basis": (
            f"confirmed_{message_type}_action"
            if terminal_action_receipt
            else ""
        ),
        "action_receipt_digest": (
            "a" * 64 if terminal_action_receipt else ""
        ),
        "reply_fact_evidence": reply_fact_evidence,
        "physical_identity_confirmed": bool(
            message_type not in {"voice", "image"}
            or native_source_message_id
        ),
    }


def _checkpoint(*facts: dict) -> dict:
    return {
        "checkpoint_revision": 3,
        "conversation_id": "conv-1",
        "batch_id": "batch-1",
        "baseline_kind": "message_tail",
        "authoritative_frame_source": "final_read",
        "committed_tail": list(facts),
        "tail_complete": True,
    }


def _text(observation_id: str, content: str, *, role: str = "customer") -> dict:
    return {
        "observation_id": observation_id,
        "row_kind": "text_bubble",
        "sender_role": role,
        "message_type": "text",
        "content_clean": content,
    }


def _voice(
    observation_id: str,
    content: str,
    *,
    native_source_message_id: str = "",
    voice_duration: str = "",
) -> dict:
    return {
        "observation_id": observation_id,
        "row_kind": "voice_transcript",
        "sender_role": "customer",
        "message_type": "voice",
        "voice_state": "transcribed",
        "content_clean": content,
        "voice_duration": voice_duration,
        "source_adapter": "win32_ocr",
        "native_source_message_id": native_source_message_id,
    }


def _image(
    observation_id: str,
    fingerprint: str,
    *,
    native_source_message_id: str = "",
) -> dict:
    return {
        "observation_id": observation_id,
        "row_kind": "image_bubble",
        "sender_role": "customer",
        "message_type": "image",
        "image_physical_anchor": {
            "bubble_visual_fingerprint": fingerprint,
        },
        "source_adapter": "win32_ocr",
        "native_source_message_id": native_source_message_id,
    }


def test_terminal_transcribed_voice_matches_without_rechecking_long_term_identity():
    checkpoint = _checkpoint(
        _fact(
            "worker-message-1",
            sender_role="customer",
            message_type="text",
            content="您好",
        ),
        _fact(
            "worker-message-2",
            sender_role="customer",
            message_type="voice",
            content="10万块钱的二手车有什么推荐的？",
            native_source_message_id="wx-native-voice-2",
        ),
    )

    result = compare_checkpoint_to_observations(
        checkpoint,
        [
            _text("fresh-text", " 您好 "),
            _voice(
                "fresh-voice",
                "10万块钱的二手车\n有什么推荐的？",
                native_source_message_id="wx-native-voice-2",
            ),
        ],
        before_frame_id="checkpoint:one",
        after_frame_id="frame:new",
        current_tail_complete=True,
    )

    assert result["comparison_result"] == "checkpoint_equal"
    assert result["old_tail_fully_consumed"] is True
    assert [item["worker_stable_id"] for item in result["matched_pairs"]] == [
        "worker-message-1",
        "worker-message-2",
    ]


def test_terminal_image_matches_by_confirmed_stable_fingerprint():
    checkpoint = _checkpoint(
        _fact(
            "worker-message-9",
            sender_role="customer",
            message_type="image",
            image_fingerprint="sha256:image-a",
            native_source_message_id="wx-native-image-9",
        )
    )

    result = compare_checkpoint_to_observations(
        checkpoint,
        [
            _image(
                "new-frame-image",
                "sha256:image-a",
                native_source_message_id="wx-native-image-9",
            )
        ],
        before_frame_id="checkpoint:image",
        after_frame_id="frame:image",
        current_tail_complete=True,
    )

    assert result["comparison_result"] == "checkpoint_equal"


def test_unique_suffix_is_returned_without_assigning_history_identity():
    checkpoint = _checkpoint(
        _fact(
            "worker-message-1",
            sender_role="customer",
            message_type="text",
            content="旧问题",
        )
    )
    observations = [
        _text("old-current-frame", "旧问题"),
        _voice("new-voice", "想看一台SUV"),
        _image("new-image", "sha256:new-image"),
    ]

    result = compare_checkpoint_to_observations(
        checkpoint,
        observations,
        before_frame_id="checkpoint:old",
        after_frame_id="frame:suffix",
        current_tail_complete=True,
    )

    assert result["comparison_result"] == "checkpoint_unique_prefix_with_suffix"
    assert result["new_suffix_observation_ids"] == ["new-voice", "new-image"]


def test_non_ingestible_banner_does_not_create_a_fake_new_fact():
    checkpoint = _checkpoint(
        _fact(
            "worker-message-1",
            sender_role="customer",
            message_type="text",
            content="你好",
        )
    )

    result = compare_checkpoint_to_observations(
        checkpoint,
        [
            {
                "observation_id": "system-banner",
                "row_kind": "system_banner",
                "sender_role": "system",
                "message_type": "system",
            },
            _text("text-after-banner", "你好"),
        ],
        before_frame_id="checkpoint",
        after_frame_id="frame",
        current_tail_complete=True,
    )

    assert result["comparison_result"] == "checkpoint_equal"


def test_replacement_and_truncation_are_not_treated_as_new_suffix():
    checkpoint = _checkpoint(
        _fact(
            "worker-message-1",
            sender_role="customer",
            message_type="text",
            content="第一条",
        ),
        _fact(
            "worker-message-2",
            sender_role="customer",
            message_type="voice",
            content="原语音",
        ),
    )

    replaced = compare_checkpoint_to_observations(
        checkpoint,
        [_text("same-slot", "第一条"), _voice("replacement", "新语音")],
        before_frame_id="checkpoint",
        after_frame_id="replacement",
        current_tail_complete=True,
    )
    truncated = compare_checkpoint_to_observations(
        checkpoint,
        [_voice("only-tail", "原语音")],
        before_frame_id="checkpoint",
        after_frame_id="truncated",
        current_tail_complete=True,
    )

    assert replaced["comparison_result"] == "checkpoint_not_continuous"
    assert replaced["reason"] == "checkpoint_prefix_fact_mismatch"
    assert truncated["comparison_result"] == "checkpoint_not_continuous"
    assert truncated["reason"] == "checkpoint_rows_missing_or_viewport_truncated"


def test_same_transcript_new_voice_without_strong_continuity_is_not_equal():
    checkpoint = _checkpoint(
        _fact(
            "worker-message-voice-old",
            sender_role="customer",
            message_type="voice",
            content="我想看10万左右的车",
        )
    )

    result = compare_checkpoint_to_observations(
        checkpoint,
        [_voice("new-physical-voice", "我想看10万左右的车")],
        before_frame_id="checkpoint:old-voice",
        after_frame_id="frame:new-voice",
        current_tail_complete=True,
    )

    assert result["comparison_result"] == "checkpoint_not_continuous"
    assert result["reason"] == "checkpoint_media_continuity_unproven"


def test_terminal_committed_voice_exact_facts_allow_send_without_identity_inheritance():
    checkpoint = _checkpoint(
        _fact(
            "worker-message-voice-old",
            sender_role="customer",
            message_type="voice",
            content="我想看10万左右的车",
            voice_duration="4秒",
            terminal_action_receipt=True,
        )
    )

    result = compare_checkpoint_to_observations(
        checkpoint,
        [
            _voice(
                "fresh-terminal-voice",
                "我想看10万左右\n的车",
                voice_duration="4s",
            )
        ],
        before_frame_id="checkpoint:terminal-voice",
        after_frame_id="frame:terminal-voice",
        current_tail_complete=True,
    )

    assert result["comparison_result"] == "checkpoint_equal"
    assert result["physical_identity_confirmed"] is False
    assert result["terminal_fact_equivalence_count"] == 1
    assert result["matched_pairs"] == [
        {
            "pre_sequence_index": 0,
            "post_sequence_index": 0,
            "worker_stable_id": "",
            "post_observation_id": "fresh-terminal-voice",
            "match_basis": "terminal_committed_fact_equivalence",
            "physical_identity_confirmed": False,
        }
    ]


def test_terminal_committed_voice_changed_or_incomplete_tail_stays_closed():
    checkpoint = _checkpoint(
        _fact(
            "worker-message-voice-old",
            sender_role="customer",
            message_type="voice",
            content="我想看10万左右的车",
            voice_duration="4",
            terminal_action_receipt=True,
        )
    )
    changed = compare_checkpoint_to_observations(
        checkpoint,
        [_voice("changed", "我想看20万左右的车", voice_duration="4")],
        before_frame_id="checkpoint",
        after_frame_id="frame:changed",
        current_tail_complete=True,
    )
    incomplete = compare_checkpoint_to_observations(
        checkpoint,
        [_voice("same", "我想看10万左右的车", voice_duration="4")],
        before_frame_id="checkpoint",
        after_frame_id="frame:incomplete",
        current_tail_complete=False,
    )

    assert changed["comparison_result"] == "checkpoint_not_continuous"
    assert changed["reason"] == "checkpoint_prefix_fact_mismatch"
    assert incomplete["comparison_result"] == "checkpoint_not_continuous"
    assert incomplete["reason"] == "checkpoint_current_tail_incomplete"


def test_terminal_fact_equivalent_old_voice_plus_identical_new_voice_is_a_suffix():
    checkpoint = _checkpoint(
        _fact(
            "worker-message-voice-old",
            sender_role="customer",
            message_type="voice",
            content="我想看10万左右的车",
            voice_duration="4",
            terminal_action_receipt=True,
        )
    )

    result = compare_checkpoint_to_observations(
        checkpoint,
        [
            _voice("old-visible", "我想看10万左右的车", voice_duration="4"),
            _voice("new-identical", "我想看10万左右的车", voice_duration="4"),
        ],
        before_frame_id="checkpoint:old",
        after_frame_id="frame:old-plus-new",
        current_tail_complete=True,
    )

    assert result["comparison_result"] == (
        "checkpoint_unique_prefix_with_suffix"
    )
    assert result["new_suffix_observation_ids"] == ["new-identical"]
    assert result["matched_pairs"][0]["worker_stable_id"] == ""


def test_similar_new_image_without_strong_continuity_is_not_equal():
    checkpoint = _checkpoint(
        _fact(
            "worker-message-image-old",
            sender_role="customer",
            message_type="image",
            image_fingerprint="dhash64:0123456789abcdef",
        )
    )

    result = compare_checkpoint_to_observations(
        checkpoint,
        [_image("new-physical-image", "dhash64:0123456789abcdef")],
        before_frame_id="checkpoint:old-image",
        after_frame_id="frame:new-image",
        current_tail_complete=True,
    )

    assert result["comparison_result"] == "checkpoint_not_continuous"
    assert result["reason"] == "checkpoint_media_continuity_unproven"


def test_terminal_committed_image_requires_exact_not_perceptual_evidence():
    exact_a = "a" * 64
    exact_b = "b" * 64
    checkpoint = _checkpoint(
        _fact(
            "worker-message-image-old",
            sender_role="customer",
            message_type="image",
            image_fingerprint=f"imagev2:same-dhash:{exact_a}",
            terminal_action_receipt=True,
        )
    )
    same = compare_checkpoint_to_observations(
        checkpoint,
        [_image("same-exact", f"imagev2:same-dhash:{exact_a}")],
        before_frame_id="checkpoint",
        after_frame_id="frame:same",
        current_tail_complete=True,
    )
    similar_only = compare_checkpoint_to_observations(
        checkpoint,
        [_image("similar-only", f"imagev2:same-dhash:{exact_b}")],
        before_frame_id="checkpoint",
        after_frame_id="frame:similar",
        current_tail_complete=True,
    )

    assert same["comparison_result"] == "checkpoint_equal"
    assert same["physical_identity_confirmed"] is False
    assert same["matched_pairs"][0]["worker_stable_id"] == ""
    assert similar_only["comparison_result"] == "checkpoint_not_continuous"
    assert similar_only["reason"] == "checkpoint_prefix_fact_mismatch"


def test_friend_welcome_empty_baseline_allows_only_an_empty_current_frame():
    checkpoint = {
        "checkpoint_revision": 3,
        "conversation_id": "conv-1",
        "batch_id": "batch-1",
        "baseline_kind": "friend_welcome_empty",
        "authoritative_frame_source": "control_empty",
        "committed_tail": [],
        "tail_complete": True,
    }

    unchanged = compare_checkpoint_to_observations(
        checkpoint,
        [],
        before_frame_id="checkpoint:welcome",
        after_frame_id="frame:empty",
        current_tail_complete=True,
    )
    superseded = compare_checkpoint_to_observations(
        checkpoint,
        [_text("new-customer-message", "我想看SUV")],
        before_frame_id="checkpoint:welcome",
        after_frame_id="frame:message",
        current_tail_complete=True,
    )

    assert unchanged["comparison_result"] == "checkpoint_equal"
    assert superseded["comparison_result"] == (
        "checkpoint_unique_prefix_with_suffix"
    )
    assert superseded["new_suffix_observation_ids"] == [
        "new-customer-message"
    ]


def test_incomplete_current_tail_blocks_text_and_empty_welcome_baselines():
    text_checkpoint = _checkpoint(
        _fact(
            "worker-message-1",
            sender_role="customer",
            message_type="text",
            content="你好",
        )
    )
    welcome_checkpoint = {
        "checkpoint_revision": 3,
        "conversation_id": "conv-1",
        "batch_id": "batch-1",
        "baseline_kind": "friend_welcome_empty",
        "authoritative_frame_source": "control_empty",
        "committed_tail": [],
        "tail_complete": True,
    }

    text_result = compare_checkpoint_to_observations(
        text_checkpoint,
        [_text("same-text", "你好")],
        before_frame_id="checkpoint:text",
        after_frame_id="frame:incomplete-text",
        current_tail_complete=False,
    )
    welcome_result = compare_checkpoint_to_observations(
        welcome_checkpoint,
        [],
        before_frame_id="checkpoint:welcome",
        after_frame_id="frame:incomplete-empty",
        current_tail_complete=False,
    )

    assert text_result["comparison_result"] == "checkpoint_not_continuous"
    assert welcome_result["comparison_result"] == (
        "checkpoint_not_continuous"
    )
    assert text_result["reason"] == "checkpoint_current_tail_incomplete"
    assert welcome_result["reason"] == "checkpoint_current_tail_incomplete"


def test_checkpoint_binding_requires_exact_conversation_batch_reply_and_digest():
    checkpoint = _checkpoint(
        _fact(
            "worker-message-1",
            sender_role="customer",
            message_type="text",
            content="你好",
        )
    )
    binding = {
        "conversation_id": "conv-1",
        "batch_id": "batch-1",
        "reply_action_id": "reply-1",
        "checkpoint_digest": canonical_sha256(checkpoint),
    }

    assert (
        checkpoint_binding_error(
            checkpoint,
            binding,
            conversation_id="conv-1",
            batch_id="batch-1",
            reply_action_id="reply-1",
        )
        == ""
    )
    binding["reply_action_id"] = "reply-2"
    assert checkpoint_binding_error(
        checkpoint,
        binding,
        conversation_id="conv-1",
        batch_id="batch-1",
        reply_action_id="reply-1",
    ) == "binding_reply_action_id_mismatch"

    malformed = dict(checkpoint)
    malformed["checkpoint_revision"] = "not-an-integer"
    assert checkpoint_binding_error(
        malformed,
        binding,
        conversation_id="conv-1",
        batch_id="batch-1",
        reply_action_id="reply-1",
    ) == "checkpoint_revision_invalid"
