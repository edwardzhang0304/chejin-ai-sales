from __future__ import annotations

from chejin_worker_client.pre_send_checkpoint import (
    canonical_sha256,
    checkpoint_binding_error,
    compare_checkpoint_to_observations,
    reply_fact_evidence_for_observation,
    stable_fact_signature,
)
from chejin_worker_client.message_viewport_projection import (
    boundary_tokens_for_observations,
    normalized_business_message_sequence,
)
from chejin_worker_client.message_identity_commit import (
    MessageCommitBasis,
    committed_identity_record,
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
    observation_id = f"checkpoint:{stable_id}"
    exact_image_sha256 = ""
    if image_fingerprint.startswith("imagev2:"):
        candidate = image_fingerprint.rsplit(":", 1)[-1].lower()
        if len(candidate) == 64:
            exact_image_sha256 = candidate
    elif image_fingerprint.startswith("sha256:"):
        candidate = image_fingerprint.split(":", 1)[1].lower()
        if len(candidate) == 64:
            exact_image_sha256 = candidate
    signature = stable_fact_signature(
        sender_role=sender_role,
        message_type=message_type,
        item_state="completed",
        content=content,
        voice_duration=voice_duration,
        image_content_sha256=image_fingerprint,
    )
    fact_observation = {
        "observation_id": observation_id,
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
        "_worker_image_action_summary": (
            {"image_sha256": exact_image_sha256}
            if terminal_action_receipt and exact_image_sha256
            else {}
        ),
    }
    reply_fact_evidence = reply_fact_evidence_for_observation(
        fact_observation,
        item_state="completed",
    )
    commit_basis = MessageCommitBasis.NEW_SUFFIX
    commit_proof: dict = {
        "alignment_status": "unique",
        "old_tail_fully_consumed": True,
        "new_suffix_observation_id": observation_id,
    }
    action_receipt_digest = ""
    if message_type == "voice" and terminal_action_receipt:
        commit_basis = MessageCommitBasis.CONFIRMED_VOICE_ACTION
        mapping = {
            "canonical_action_id": f"voice-action:{stable_id}",
            "reserved_worker_stable_id": stable_id,
            "selected_action_token": f"voice-ticket:{stable_id}",
            "pre_observation_id": f"pre:{stable_id}",
            "post_observation_id": observation_id,
            "binding_confirmed": True,
            "trigger_observation_id": observation_id,
            "physical_identity_inherited_from_prepare": False,
            "physical_action_count": 1,
            "result_candidate_count": 1,
            "stable_business_content_signature": signature,
        }
        fact_observation["_worker_voice_action_summary"] = {
            "confirmed_action_mapping": mapping,
        }
        commit_proof = dict(mapping)
        action_receipt_digest = "a" * 64
    elif message_type == "image" and terminal_action_receipt:
        commit_basis = MessageCommitBasis.CONFIRMED_IMAGE_ACTION
        mapping = {
            "canonical_action_id": f"image-action:{stable_id}",
            "reserved_worker_stable_id": stable_id,
            "pre_observation_id": f"pre:{stable_id}",
            "post_observation_id": observation_id,
            "binding_confirmed": True,
            "trigger_observation_id": observation_id,
            "physical_identity_inherited_from_prepare": False,
        }
        fact_observation["_worker_image_action_summary"] = {
            "image_sha256": exact_image_sha256,
            "confirmed_action_mapping": mapping,
        }
        commit_proof = {**mapping, "image_sha256": exact_image_sha256}
        action_receipt_digest = "a" * 64
    elif message_type in {"voice", "image"}:
        # Deliberately incomplete: checkpoint validation must reject media
        # that has no formal action result.
        commit_basis = MessageCommitBasis.NATIVE_SOURCE_MESSAGE_ID
        commit_proof = {
            "native_source_message_id": native_source_message_id,
            "sender_role": sender_role,
            "message_type": message_type,
        }
    commit_record = committed_identity_record(
        worker_stable_id=stable_id,
        commit_basis=commit_basis,
        observation_id=observation_id,
        sender_role=sender_role,
        message_type=message_type,
        proof=commit_proof,
    )
    fact_observation.update(
        {
            "native_source_message_id": native_source_message_id,
            "source_adapter": "win32_ocr",
            "_worker_stable_id": stable_id,
            "_worker_identity_scope": "committed",
            "_worker_committed_message": commit_record,
        }
    )
    return {
        "worker_stable_id": stable_id,
        "sender_role": sender_role,
        "message_type": message_type,
        "item_state": "completed",
        "stable_fact_signature": signature,
        "source_message_key": f"source:{stable_id}",
        "commit_basis": commit_basis.value,
        "action_receipt_digest": action_receipt_digest,
        "reply_fact_evidence": reply_fact_evidence,
        "message_identity_commit_record": commit_record,
        "message_identity_runtime_evidence": {},
        "_business_observation": fact_observation,
    }


def _checkpoint(*facts: dict) -> dict:
    committed_tail: list[dict] = []
    observations: list[dict] = []
    for raw in facts:
        item = dict(raw)
        observation = dict(item.pop("_business_observation"))
        observations.append(observation)
        committed_tail.append(item)
    projection = normalized_business_message_sequence(
        observations,
        message_viewport_bounds=None,
    )
    tokens = boundary_tokens_for_observations(
        observations,
        committed_only=True,
    )
    for index, item in enumerate(committed_tail):
        item["business_projection"] = projection[index]
        item["strong_boundary_tokens"] = sorted(tokens.get(index, set()))
    return {
        "checkpoint_revision": 5,
        "conversation_id": "conv-1",
        "batch_id": "batch-1",
        "baseline_kind": "message_tail",
        "authoritative_frame_source": "final_read",
        "committed_tail": committed_tail,
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
        "",
        "",
    ]


def test_image_matches_only_by_confirmed_native_identity_not_bubble_pixels():
    exact_a = "a" * 64
    exact_b = "b" * 64
    checkpoint = _checkpoint(
        _fact(
            "worker-message-9",
            sender_role="customer",
            message_type="image",
            image_fingerprint=f"imagev2:old-render:{exact_a}",
            native_source_message_id="wx-native-image-9",
            terminal_action_receipt=True,
        )
    )

    result = compare_checkpoint_to_observations(
        checkpoint,
        [
                _image(
                    "new-frame-image",
                    f"imagev2:new-render:{exact_b}",
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

    assert replaced["comparison_result"] == (
        "checkpoint_continuity_context_expansion_required"
    )
    assert truncated["comparison_result"] == (
        "checkpoint_continuity_context_expansion_required"
    )


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

    assert result["comparison_result"] == (
        "checkpoint_continuity_context_expansion_required"
    )
    assert result["continuity_relation"] == (
        "continuity_context_expansion_required"
    )


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
    assert result["terminal_fact_equivalence_count"] == 0
    assert result["matched_pairs"] == [
        {
            "pre_sequence_index": 0,
            "post_sequence_index": 0,
            "worker_stable_id": "",
            "post_observation_id": "fresh-terminal-voice",
            "match_basis": "worker_business_viewport_continuity",
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

    assert changed["comparison_result"] == (
        "checkpoint_continuity_context_expansion_required"
    )
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

    assert result["comparison_result"] == (
        "checkpoint_continuity_context_expansion_required"
    )


def test_terminal_committed_image_pixels_never_prove_cross_frame_identity():
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

    assert same["comparison_result"] == (
        "checkpoint_continuity_context_expansion_required"
    )
    assert similar_only["comparison_result"] == (
        "checkpoint_continuity_context_expansion_required"
    )


def test_checkpoint_binding_accepts_formal_image_action_without_using_pixels_as_identity():
    checkpoint = _checkpoint(
        _fact(
            "worker-message-image-old",
            sender_role="customer",
            message_type="image",
            image_fingerprint=f"imagev2:same-dhash:{'a' * 64}",
            terminal_action_receipt=True,
        )
    )
    binding = {
        "conversation_id": "conv-1",
        "batch_id": "batch-1",
        "reply_action_id": "reply-1",
        "checkpoint_digest": canonical_sha256(checkpoint),
    }

    assert checkpoint_binding_error(
        checkpoint,
        binding,
        conversation_id="conv-1",
        batch_id="batch-1",
        reply_action_id="reply-1",
    ) == ""


def test_checkpoint_binding_requires_formal_receipt_for_strong_image_identity():
    missing_receipt = _checkpoint(
        _fact(
            "worker-message-image-native",
            sender_role="customer",
            message_type="image",
            image_fingerprint=f"imagev2:native:{'a' * 64}",
            native_source_message_id="wx-native-image-1",
        )
    )
    binding = {
        "conversation_id": "conv-1",
        "batch_id": "batch-1",
        "reply_action_id": "reply-1",
        "checkpoint_digest": canonical_sha256(missing_receipt),
    }

    assert checkpoint_binding_error(
        missing_receipt,
        binding,
        conversation_id="conv-1",
        batch_id="batch-1",
        reply_action_id="reply-1",
    ) == "checkpoint_item_invalid"

    with_receipt = _checkpoint(
        _fact(
            "worker-message-image-native",
            sender_role="customer",
            message_type="image",
            image_fingerprint=f"imagev2:native:{'a' * 64}",
            native_source_message_id="wx-native-image-1",
            terminal_action_receipt=True,
        )
    )
    binding["checkpoint_digest"] = canonical_sha256(with_receipt)
    assert checkpoint_binding_error(
        with_receipt,
        binding,
        conversation_id="conv-1",
        batch_id="batch-1",
        reply_action_id="reply-1",
    ) == ""


def test_friend_welcome_empty_baseline_allows_only_an_empty_current_frame():
    checkpoint = {
        "checkpoint_revision": 5,
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
        current_empty_viewport_confirmed=True,
    )
    superseded = compare_checkpoint_to_observations(
        checkpoint,
        [_text("new-customer-message", "我想看SUV")],
        before_frame_id="checkpoint:welcome",
        after_frame_id="frame:message",
        current_tail_complete=True,
        current_empty_viewport_confirmed=False,
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
        "checkpoint_revision": 4,
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
