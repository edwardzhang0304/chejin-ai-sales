from __future__ import annotations

import unittest
import inspect

import chejin_worker_client.sequence_alignment as alignment_module
import chejin_worker_client.task_runner as task_runner_module
import chejin_worker_client.wechat_c2 as wechat_c2_module

from chejin_worker_client.sequence_alignment import (
    align_committed_message_sequence,
    build_post_action_observation_sequence,
    build_pre_action_identity_sequence,
    inherited_worker_ids,
)
from chejin_worker_client.task_runner import confirmed_voice_action_mapping


def observation(
    observation_id: str,
    message_type: str,
    content: str = "",
    *,
    sender_role: str = "customer",
    native_id: str = "",
    visual_id: str = "",
    bubble_rect: list[int] | None = None,
) -> dict:
    row_kind = {
        "text": "text_bubble",
        "voice": "voice_bubble",
        "image": "image_bubble",
    }[message_type]
    result = {
        "observation_id": observation_id,
        "row_kind": row_kind,
        "message_type": message_type,
        "sender_role": sender_role,
        "content_clean": content,
        "native_source_message_id": native_id,
        "canonical_visual_id": visual_id,
        "voice_state": "untranscribed" if message_type == "voice" else "not_voice",
    }
    if bubble_rect is not None:
        result["bubble_rect"] = bubble_rect
    return result


class SequenceAlignmentTests(unittest.TestCase):
    def align(self, pre, post, mapping=None):
        return align_committed_message_sequence(
            pre,
            post,
            mapping,
            pre_sequence_source="action_frame",
            pre_frame_id="frame-before",
            post_frame_id="frame-after",
        )

    def test_identical_text_at_new_tail_gets_new_suffix(self):
        before = [
            observation("text-1", "text", "您好"),
            observation("voice-1", "voice", native_id="voice-native-1"),
            observation("image-1", "image", visual_id="image-visual-1"),
            observation("text-2", "text", "好的"),
        ]
        pre = build_pre_action_identity_sequence(
            before,
            committed_ids={
                "text-1": "worker-message-10",
                "voice-1": "worker-message-11",
                "image-1": "worker-message-12",
                "text-2": "worker-message-13",
            },
        )
        after = [*before, observation("text-3", "text", "好的")]
        post = build_post_action_observation_sequence(after)

        result = self.align(pre, post)

        self.assertEqual(result["alignment_status"], "unique")
        self.assertEqual(
            [pair["worker_stable_id"] for pair in result["matched_pairs"]],
            [
                "worker-message-10",
                "worker-message-11",
                "worker-message-12",
                "worker-message-13",
            ],
        )
        self.assertEqual(result["new_suffix_observation_ids"], ["text-3"])

    def test_visual_line_wrap_in_confirmed_reply_does_not_hide_new_voice(self):
        """OCR layout wraps cannot turn a valid new voice into an identity gate."""

        before = [
            observation("old-customer", "text", "你好在吗"),
            observation(
                "old-self",
                "text",
                "你好，欢迎加上好友，很高兴认识你！请问有什么可以帮您？",
                sender_role="self",
            ),
        ]
        pre = build_pre_action_identity_sequence(
            before,
            committed_ids={
                "old-customer": "worker-message-1",
                "old-self": "worker-message-2",
            },
        )
        after = [
            observation("current-customer", "text", "你好在吗"),
            observation(
                "current-self",
                "text",
                "你好，欢迎加上好友，很高兴认识你！请问有\n什么可以帮您？",
                sender_role="self",
            ),
            observation("new-five-second-voice", "voice"),
        ]

        result = self.align(
            pre,
            build_post_action_observation_sequence(after),
        )

        self.assertEqual(result["alignment_status"], "unique")
        self.assertEqual(
            result["new_suffix_observation_ids"],
            ["new-five-second-voice"],
        )

    def test_one_historical_text_uniquely_exposes_new_tail(self):
        pre = build_pre_action_identity_sequence(
            [observation("old-hello", "text", "你好")],
            committed_ids={"old-hello": "worker-message-1"},
        )
        post = build_post_action_observation_sequence(
            [
                observation("visible-old-hello", "text", "你好"),
                observation("new-question", "text", "在吗"),
            ]
        )

        result = self.align(pre, post)

        self.assertEqual(result["alignment_status"], "unique")
        self.assertEqual(
            inherited_worker_ids(result),
            {"visible-old-hello": "worker-message-1"},
        )
        self.assertEqual(
            result["new_suffix_observation_ids"],
            ["new-question"],
        )

    def test_one_historical_text_does_not_allow_new_prefix(self):
        pre = build_pre_action_identity_sequence(
            [observation("old-hello", "text", "你好")],
            committed_ids={"old-hello": "worker-message-1"},
        )
        post = build_post_action_observation_sequence(
            [
                observation("unexpected-prefix", "text", "先出现"),
                observation("visible-old-hello", "text", "你好"),
                observation("new-question", "text", "在吗"),
            ]
        )

        result = self.align(pre, post)

        self.assertEqual(result["alignment_status"], "ambiguous")
        self.assertEqual(result["new_suffix_observation_ids"], [])

    def test_one_historical_voice_without_strong_anchor_stays_ambiguous(self):
        pre = build_pre_action_identity_sequence(
            [observation("old-voice", "voice")],
            committed_ids={"old-voice": "worker-message-1"},
        )
        post = build_post_action_observation_sequence(
            [
                observation("visible-old-voice", "voice"),
                observation("new-text", "text", "在吗"),
            ]
        )

        result = self.align(pre, post)

        self.assertEqual(result["alignment_status"], "ambiguous")
        self.assertEqual(result["new_suffix_observation_ids"], [])

    def test_two_weak_voice_rows_cannot_rebind_after_viewport_shift(self):
        pre = build_pre_action_identity_sequence(
            [
                observation("old-voice-1", "voice"),
                observation("old-voice-2", "voice"),
            ],
            committed_ids={
                "old-voice-1": "worker-message-1",
                "old-voice-2": "worker-message-2",
            },
        )
        post = build_post_action_observation_sequence(
            [
                observation("visible-old-voice-2", "voice"),
                observation("new-voice-same-seat", "voice"),
            ]
        )

        result = self.align(pre, post)

        self.assertEqual(result["alignment_status"], "ambiguous")
        self.assertEqual(result["matched_pairs"], [])
        self.assertEqual(result["new_suffix_observation_ids"], [])

    def test_two_media_rows_require_a_real_anchor_or_text_context(self):
        pre = build_pre_action_identity_sequence(
            [
                observation("old-voice", "voice"),
                observation("old-image", "image"),
            ],
            committed_ids={
                "old-voice": "worker-message-1",
                "old-image": "worker-message-2",
            },
        )
        post = build_post_action_observation_sequence(
            [
                observation("current-voice", "voice"),
                observation("current-image", "image"),
                observation("new-text", "text", "在吗"),
            ]
        )

        result = self.align(pre, post)

        self.assertEqual(result["alignment_status"], "ambiguous")
        self.assertEqual(result["new_suffix_observation_ids"], [])

    def test_single_identical_text_is_ambiguous(self):
        pre = build_pre_action_identity_sequence(
            [observation("text-2", "text", "好的")],
            committed_ids={"text-2": "worker-message-13"},
        )
        post = build_post_action_observation_sequence(
            [observation("only-visible", "text", "好的")]
        )

        result = self.align(pre, post)

        self.assertEqual(result["alignment_status"], "ambiguous")
        self.assertGreater(result["candidate_alignment_count"], 1)
        self.assertEqual(result["new_suffix_observation_ids"], [])
        self.assertEqual(inherited_worker_ids(result), {})

    def test_frame_local_unselected_is_consumed_but_does_not_inherit(self):
        before = [
            observation("text-1", "text", "您好"),
            observation("voice-unselected", "voice", native_id="voice-native"),
            observation("image-unselected", "image", visual_id="image-visual"),
        ]
        pre = build_pre_action_identity_sequence(
            before,
            committed_ids={"text-1": "worker-message-1"},
        )
        post = build_post_action_observation_sequence(before)

        result = self.align(pre, post)

        self.assertEqual(result["alignment_status"], "unique")
        self.assertEqual(result["new_suffix_observation_ids"], [])
        self.assertEqual(
            inherited_worker_ids(result),
            {"text-1": "worker-message-1"},
        )

    def test_confirmed_voice_action_commits_reserved_id(self):
        before = [
            observation("text-1", "text", "您好"),
            observation("voice-selected", "voice"),
        ]
        pre = build_pre_action_identity_sequence(
            before,
            committed_ids={"text-1": "worker-message-20"},
            selected_observation_id="voice-selected",
            canonical_action_id="voice-action-1",
            reserved_worker_stable_id="worker-message-21",
        )
        after = [
            observation("text-1-post", "text", "您好"),
            observation("voice-post", "voice", "转写正文"),
            {
                **observation("transcript-row", "text", "转写正文"),
                "row_kind": "voice_transcript",
            },
        ]
        mapping = {
            "canonical_action_id": "voice-action-1",
            "binding_confirmed": True,
            "post_observation_id": "voice-post",
            "derived_observation_ids": ["transcript-row"],
        }
        post = build_post_action_observation_sequence(
            after,
            confirmed_action_mapping=mapping,
        )

        result = self.align(pre, post, mapping)

        self.assertEqual(result["alignment_status"], "unique")
        self.assertEqual(
            inherited_worker_ids(result)["voice-post"],
            "worker-message-21",
        )
        self.assertNotIn("transcript-row", result["new_suffix_observation_ids"])

    def test_confirmed_voice_action_survives_transcript_visual_id_change(self):
        before = [
            observation(
                "voice-selected",
                "voice",
                visual_id="visual-untranscribed-bubble",
            ),
        ]
        pre = build_pre_action_identity_sequence(
            before,
            selected_observation_id="voice-selected",
            canonical_action_id="voice-action-visual-change",
            reserved_worker_stable_id="worker-message-3",
        )
        mapping = {
            "canonical_action_id": "voice-action-visual-change",
            "binding_confirmed": True,
            "post_observation_id": "voice-transcript",
            "derived_observation_ids": [],
        }
        post = build_post_action_observation_sequence(
            [
                observation(
                    "voice-transcript",
                    "voice",
                    "中午好，你在吗？有个事儿咨询你一下。",
                    visual_id="visual-expanded-transcript",
                ),
            ],
            confirmed_action_mapping=mapping,
        )

        result = self.align(pre, post, mapping)

        self.assertEqual(result["alignment_status"], "unique")
        self.assertEqual(result["candidate_alignment_count"], 1)
        self.assertEqual(
            result["matched_pairs"],
            [
                {
                    "identity_state": "selected_action",
                    "worker_stable_id": "worker-message-3",
                    "pre_observation_id": "voice-selected",
                    "post_observation_id": "voice-transcript",
                    "pre_index": 0,
                    "post_index": 0,
                    "match_basis": "confirmed_action",
                }
            ],
        )
        self.assertEqual(result["new_suffix_observation_ids"], [])

    def test_confirmed_action_selects_one_of_two_same_duration_voices(self):
        before = [
            observation(
                "voice-upper",
                "voice",
                '[语音] 3"',
                visual_id="visual-upper-untranscribed",
            ),
            observation(
                "voice-lower-selected",
                "voice",
                '[语音] 3"',
                visual_id="visual-lower-untranscribed",
            ),
        ]
        pre = build_pre_action_identity_sequence(
            before,
            committed_ids={
                "voice-upper": "worker-message-2",
            },
            selected_observation_id="voice-lower-selected",
            canonical_action_id="voice-action-lower",
            reserved_worker_stable_id="worker-message-3",
        )
        mapping = {
            "canonical_action_id": "voice-action-lower",
            "binding_confirmed": True,
            "post_observation_id": "voice-lower-transcript",
            "derived_observation_ids": [],
        }
        post = build_post_action_observation_sequence(
            [
                observation(
                    "voice-upper-after",
                    "voice",
                    '[语音] 3"',
                    visual_id="visual-upper-untranscribed",
                ),
                observation(
                    "voice-lower-transcript",
                    "voice",
                    "中午好，你在吗？有个事儿咨询你一下。",
                    visual_id="visual-lower-expanded-transcript",
                ),
            ],
            confirmed_action_mapping=mapping,
        )

        result = self.align(pre, post, mapping)

        self.assertEqual(result["alignment_status"], "unique")
        self.assertEqual(result["candidate_alignment_count"], 1)
        self.assertEqual(
            inherited_worker_ids(result),
            {
                "voice-upper-after": "worker-message-2",
                "voice-lower-transcript": "worker-message-3",
            },
        )
        self.assertEqual(result["new_suffix_observation_ids"], [])

    def test_unconfirmed_voice_visual_id_change_remains_unresolved(self):
        pre = build_pre_action_identity_sequence(
            [
                observation(
                    "historical-voice",
                    "voice",
                    visual_id="visual-before",
                )
            ],
            committed_ids={
                "historical-voice": "worker-message-2",
            },
        )
        post = build_post_action_observation_sequence(
            [
                observation(
                    "different-voice",
                    "voice",
                    visual_id="visual-after",
                )
            ]
        )

        result = self.align(pre, post)

        self.assertEqual(result["alignment_status"], "unresolved")
        self.assertEqual(result["matched_pairs"], [])

    def test_same_duration_voice_at_old_position_does_not_inherit(self):
        pre = build_pre_action_identity_sequence(
            [
                observation("text-anchor", "text", "前文"),
                observation("old-voice", "voice", native_id="old-native"),
            ],
            committed_ids={
                "text-anchor": "worker-message-30",
                "old-voice": "worker-message-31",
            },
        )
        post = build_post_action_observation_sequence(
            [
                observation("text-anchor-post", "text", "前文"),
                observation("old-voice-moved", "voice", native_id="old-native"),
                observation("new-same-duration", "voice"),
            ]
        )

        result = self.align(pre, post)

        self.assertEqual(result["alignment_status"], "unique")
        self.assertEqual(
            inherited_worker_ids(result)["old-voice-moved"],
            "worker-message-31",
        )
        self.assertEqual(
            result["new_suffix_observation_ids"],
            ["new-same-duration"],
        )

    def test_empty_checkpoint_is_distinct_from_missing_local_state(self):
        post = build_post_action_observation_sequence(
            [observation("first", "text", "第一次会话")]
        )
        empty = align_committed_message_sequence(
            [],
            post,
            pre_sequence_source="empty_checkpoint",
            pre_frame_id="checkpoint:none:conv-1",
            post_frame_id="frame-first",
        )
        missing_context = align_committed_message_sequence(
            build_pre_action_identity_sequence(
                [observation("history", "text", "历史")],
                committed_ids={"history": "worker-message-1"},
            ),
            post,
            pre_sequence_source="checkpoint",
            pre_frame_id="checkpoint:revision-1",
            post_frame_id="frame-first",
        )

        self.assertEqual(empty["alignment_status"], "not_required")
        self.assertEqual(empty["new_suffix_observation_ids"], ["first"])
        self.assertEqual(missing_context["alignment_status"], "unresolved")
        self.assertEqual(missing_context["new_suffix_observation_ids"], [])

    def test_native_source_id_is_a_single_message_strong_anchor(self):
        pre = build_pre_action_identity_sequence(
            [observation("before", "text", "相同", native_id="native-1")],
            committed_ids={"before": "worker-message-40"},
        )
        post = build_post_action_observation_sequence(
            [
                observation("after", "text", "相同", native_id="native-1"),
                observation("new", "text", "相同"),
            ]
        )

        result = self.align(pre, post)

        self.assertEqual(result["alignment_status"], "unique")
        self.assertEqual(result["matched_pairs"][0]["match_basis"], "native_source_message_id")
        self.assertEqual(result["new_suffix_observation_ids"], ["new"])

    def test_canonical_visual_id_is_a_single_message_strong_anchor(self):
        pre = build_pre_action_identity_sequence(
            [observation("before", "image", visual_id="visual-1")],
            committed_ids={"before": "worker-message-41"},
        )
        post = build_post_action_observation_sequence(
            [observation("after", "image", visual_id="visual-1")]
        )

        result = self.align(pre, post)

        self.assertEqual(result["alignment_status"], "unique")
        self.assertEqual(result["matched_pairs"][0]["match_basis"], "canonical_visual_id")

    def test_text_identity_survives_page_shift_without_using_coordinates(self):
        pre = build_pre_action_identity_sequence(
            [
                observation("old-hello", "text", "您好"),
                observation("old-ok", "text", "好的"),
            ],
            committed_ids={
                "old-hello": "worker-message-1",
                "old-ok": "worker-message-2",
            },
        )
        post = build_post_action_observation_sequence(
            [
                observation("shifted-hello", "text", "您好"),
                observation("shifted-ok", "text", "好的"),
            ]
        )

        result = self.align(pre, post)

        self.assertEqual(result["alignment_status"], "unique")
        self.assertEqual(
            inherited_worker_ids(result),
            {
                "shifted-hello": "worker-message-1",
                "shifted-ok": "worker-message-2",
            },
        )

    def test_text_sequence_survives_rebuilt_visual_ids_after_viewport_scroll(self):
        pre = build_pre_action_identity_sequence(
            [
                observation(
                    "before-top",
                    "text",
                    "顶部会滚出",
                    sender_role="self",
                    visual_id="visual-before-top",
                ),
                observation(
                    "before-long",
                    "text",
                    "这是一条保留下来的长消息",
                    sender_role="self",
                    visual_id="visual-before-long",
                ),
                observation(
                    "before-ok-1",
                    "text",
                    "好",
                    visual_id="visual-before-ok-1",
                ),
                observation(
                    "before-middle",
                    "text",
                    "小号回我一局",
                    sender_role="self",
                    visual_id="visual-before-middle",
                ),
                observation(
                    "before-ok-2",
                    "text",
                    "好",
                    visual_id="visual-before-ok-2",
                ),
            ],
            committed_ids={
                "before-top": "worker-message-1",
                "before-long": "worker-message-2",
                "before-ok-1": "worker-message-3",
                "before-middle": "worker-message-4",
                "before-ok-2": "worker-message-5",
            },
        )
        post = build_post_action_observation_sequence(
            [
                observation(
                    "after-long",
                    "text",
                    "这是一条保留下来的长消息",
                    sender_role="self",
                    visual_id="visual-after-long",
                ),
                observation(
                    "after-ok-1",
                    "text",
                    "好",
                    visual_id="visual-after-ok-1",
                ),
                observation(
                    "after-middle",
                    "text",
                    "小号回我一局",
                    sender_role="self",
                    visual_id="visual-after-middle",
                ),
                observation(
                    "after-ok-2",
                    "text",
                    "好",
                    visual_id="visual-after-ok-2",
                ),
                observation("new-tail", "text", "新的尾部消息"),
            ]
        )

        result = self.align(pre, post)

        self.assertEqual(result["alignment_status"], "unique")
        self.assertEqual(
            inherited_worker_ids(result),
            {
                "after-long": "worker-message-2",
                "after-ok-1": "worker-message-3",
                "after-middle": "worker-message-4",
                "after-ok-2": "worker-message-5",
            },
        )
        self.assertEqual(result["new_suffix_observation_ids"], ["new-tail"])

    def test_media_visual_id_mismatch_remains_unresolved(self):
        pre = build_pre_action_identity_sequence(
            [
                observation(
                    "image-before",
                    "image",
                    visual_id="visual-image-before",
                )
            ],
            committed_ids={"image-before": "worker-message-1"},
        )
        post = build_post_action_observation_sequence(
            [
                observation(
                    "image-after",
                    "image",
                    visual_id="visual-image-after",
                )
            ]
        )

        result = self.align(pre, post)

        self.assertEqual(result["alignment_status"], "unresolved")
        self.assertEqual(result["matched_pairs"], [])

    def test_repeated_text_suffix_has_one_monotonic_alignment(self):
        pre = build_pre_action_identity_sequence(
            [
                observation("old-a-1", "text", "A"),
                observation("old-a-2", "text", "A"),
                observation("old-b", "text", "B"),
            ],
            committed_ids={
                "old-a-1": "worker-message-1",
                "old-a-2": "worker-message-2",
                "old-b": "worker-message-3",
            },
        )
        post = build_post_action_observation_sequence(
            [
                observation("visible-a", "text", "A"),
                observation("visible-b", "text", "B"),
                observation("new-c", "text", "C"),
            ]
        )

        result = self.align(pre, post)

        self.assertEqual(result["alignment_status"], "unique")
        self.assertEqual(
            inherited_worker_ids(result),
            {
                "visible-a": "worker-message-2",
                "visible-b": "worker-message-3",
            },
        )
        self.assertEqual(result["new_suffix_observation_ids"], ["new-c"])

    def test_image_ocr_drift_keeps_identity_only_with_canonical_visual_id(self):
        pre = build_pre_action_identity_sequence(
            [
                observation(
                    "image-before",
                    "image",
                    "第一次误识文字",
                    visual_id="visual-image-1",
                )
            ],
            committed_ids={"image-before": "worker-message-7"},
        )
        post = build_post_action_observation_sequence(
            [
                observation(
                    "image-after",
                    "image",
                    "第二次另一种误识文字",
                    visual_id="visual-image-1",
                )
            ]
        )

        result = self.align(pre, post)

        self.assertEqual(result["alignment_status"], "unique")
        self.assertEqual(
            inherited_worker_ids(result),
            {"image-after": "worker-message-7"},
        )

    def test_zero_legal_alignment_is_unresolved_and_has_no_new_suffix(self):
        pre = build_pre_action_identity_sequence(
            [
                observation("one", "text", "一"),
                observation("two", "text", "二"),
            ],
            committed_ids={
                "one": "worker-message-1",
                "two": "worker-message-2",
            },
        )
        post = build_post_action_observation_sequence(
            [observation("different", "image", visual_id="visual-new")]
        )

        result = self.align(pre, post)

        self.assertEqual(result["alignment_status"], "unresolved")
        self.assertEqual(result["candidate_alignment_count"], 0)
        self.assertFalse(result["old_tail_fully_consumed"])
        self.assertEqual(result["new_suffix_observation_ids"], [])

    def test_multiple_legal_positions_are_ambiguous(self):
        pre = build_pre_action_identity_sequence(
            [
                observation("one", "text", "好的"),
                observation("two", "text", "好的"),
            ],
            committed_ids={
                "one": "worker-message-1",
                "two": "worker-message-2",
            },
        )
        post = build_post_action_observation_sequence(
            [
                observation("a", "text", "好的"),
                observation("b", "text", "好的"),
                observation("c", "text", "好的"),
            ]
        )

        result = self.align(pre, post)

        self.assertEqual(result["alignment_status"], "ambiguous")
        self.assertGreater(result["candidate_alignment_count"], 1)
        self.assertEqual(result["new_suffix_observation_ids"], [])

    def test_worker_accepts_only_complete_sidecar_tracking_binding(self):
        post = [observation("voice-post", "voice", "转写正文")]
        mapping = confirmed_voice_action_mapping(
            voice_payload={
                "canonical_voice_action_id": "voice-action-1",
                "reserved_worker_stable_id": "worker-message-9",
                "transcript_binding_status": "confirmed",
                "transcript_binding_method": (
                    "continuous_target_tracking"
                ),
                "binding_candidate_count": 1,
                "tracking_frame_ids": ["pre", "mid", "post"],
                "tracking_edges": [
                    {
                        "from_frame_id": "pre",
                        "from_observation_id": "voice-pre",
                        "to_frame_id": "mid",
                        "to_observation_id": "voice-mid",
                        "sender_role": "customer",
                        "message_type": "voice",
                        "structural_evidence": {"same_target": True},
                        "displacement_evidence": {"continuous": True},
                        "edge_candidate_count": 1,
                    },
                    {
                        "from_frame_id": "mid",
                        "from_observation_id": "voice-mid",
                        "to_frame_id": "post",
                        "to_observation_id": "voice-post",
                        "sender_role": "customer",
                        "message_type": "voice",
                        "structural_evidence": {"same_target": True},
                        "displacement_evidence": {"continuous": True},
                        "edge_candidate_count": 1,
                    },
                ],
                "matched_neighbor_pairs": [],
                "native_source_message_id": None,
                "confirmed_action_mapping": {
                    "canonical_action_id": "voice-action-1",
                    "reserved_worker_stable_id": "worker-message-9",
                    "binding_confirmed": True,
                    "post_observation_id": "voice-post",
                    "derived_observation_ids": [],
                },
            },
            pre_observations=[observation("voice-pre", "voice")],
            post_observations=post,
            canonical_action_id="voice-action-1",
            reserved_worker_stable_id="worker-message-9",
            expected_pre_frame_id="pre",
            pre_observation_id="voice-pre",
            selected_anchor_keys={"old-frame-anchor"},
        )

        self.assertTrue(mapping["binding_confirmed"])
        self.assertEqual(mapping["post_observation_id"], "voice-post")
        self.assertEqual(
            mapping["binding_method"], "continuous_target_tracking"
        )

    def test_worker_rejects_disconnected_sidecar_tracking_edges(self):
        with self.assertRaisesRegex(
            ValueError, "C2_VOICE_IDENTITY_CONTRACT_INVALID"
        ):
            confirmed_voice_action_mapping(
                voice_payload={
                    "canonical_voice_action_id": "voice-action-1",
                    "reserved_worker_stable_id": "worker-message-9",
                    "transcript_binding_status": "confirmed",
                    "transcript_binding_method": (
                        "continuous_target_tracking"
                    ),
                    "binding_candidate_count": 1,
                    "tracking_frame_ids": [
                        "pre",
                        "mid-a",
                        "post",
                    ],
                    "tracking_edges": [
                        {
                            "from_frame_id": "pre",
                            "from_observation_id": "voice-pre",
                            "to_frame_id": "mid-a",
                            "to_observation_id": "voice-mid",
                            "sender_role": "customer",
                            "message_type": "voice",
                            "structural_evidence": {"same_target": True},
                            "displacement_evidence": {"continuous": True},
                            "edge_candidate_count": 1,
                        },
                        {
                            "from_frame_id": "mid-b",
                            "from_observation_id": "voice-mid",
                            "to_frame_id": "post",
                            "to_observation_id": "voice-post",
                            "sender_role": "customer",
                            "message_type": "voice",
                            "structural_evidence": {"same_target": True},
                            "displacement_evidence": {"continuous": True},
                            "edge_candidate_count": 1,
                        },
                    ],
                    "matched_neighbor_pairs": [],
                    "native_source_message_id": None,
                    "confirmed_action_mapping": {
                        "canonical_action_id": "voice-action-1",
                        "reserved_worker_stable_id": "worker-message-9",
                        "binding_confirmed": True,
                        "post_observation_id": "voice-post",
                        "derived_observation_ids": [],
                    },
                },
                pre_observations=[observation("voice-pre", "voice")],
                post_observations=[
                    observation("voice-post", "voice", "转写正文")
                ],
                canonical_action_id="voice-action-1",
                reserved_worker_stable_id="worker-message-9",
                expected_pre_frame_id="pre",
                pre_observation_id="voice-pre",
                selected_anchor_keys={"old-frame-anchor"},
            )

    def test_worker_accepts_native_and_uniquely_projected_neighbor_proofs(self):
        native_pre = [
            observation(
                "voice-pre",
                "voice",
                native_id="wx-native-voice-1",
            )
        ]
        native_post = [
            observation(
                "voice-post",
                "voice",
                "转写正文",
                native_id="wx-native-voice-1",
            )
        ]
        neighbor_pre = [
            observation("before-a", "text", "上文", bubble_rect=[10, 100, 100, 120]),
            observation("voice-pre", "voice", bubble_rect=[10, 290, 100, 310]),
            observation("before-b", "text", "下文", sender_role="self", bubble_rect=[10, 400, 100, 420]),
        ]
        neighbor_post = [
            observation("after-a", "text", "上文", bubble_rect=[10, 5, 100, 25]),
            observation("voice-post", "voice", "转写正文", bubble_rect=[10, 195, 100, 215]),
            observation("after-b", "text", "下文", sender_role="self", bubble_rect=[10, 305, 100, 325]),
        ]

        def payload(method: str) -> dict:
            return {
                "canonical_voice_action_id": "voice-action-2",
                "reserved_worker_stable_id": "worker-message-10",
                "transcript_binding_status": "confirmed",
                "transcript_binding_method": method,
                "binding_candidate_count": 1,
                "tracking_frame_ids": [],
                "tracking_edges": [],
                "matched_neighbor_pairs": [],
                "native_source_message_id": None,
                "confirmed_action_mapping": {
                    "canonical_action_id": "voice-action-2",
                    "reserved_worker_stable_id": "worker-message-10",
                    "binding_confirmed": True,
                    "post_observation_id": "voice-post",
                    "derived_observation_ids": [],
                },
            }

        native = payload("native_source_id")
        native["native_source_message_id"] = "wx-native-voice-1"
        neighbors = payload("neighbor_scroll_alignment")
        neighbors["matched_neighbor_pairs"] = [
            {
                "pre_observation_id": "before-a",
                "post_observation_id": "after-a",
                "sender_role": "customer",
                "scroll_delta_y": -96.0,
            },
            {
                "pre_observation_id": "before-b",
                "post_observation_id": "after-b",
                "sender_role": "self",
                "scroll_delta_y": -94.0,
            },
        ]
        for proof, pre, post in (
            (native, native_pre, native_post),
            (neighbors, neighbor_pre, neighbor_post),
        ):
            mapping = confirmed_voice_action_mapping(
                voice_payload=proof,
                pre_observations=pre,
                post_observations=post,
                canonical_action_id="voice-action-2",
                reserved_worker_stable_id="worker-message-10",
                expected_pre_frame_id="pre",
                pre_observation_id="voice-pre",
                selected_anchor_keys=set(),
            )
            self.assertTrue(mapping["binding_confirmed"])

    def test_worker_rejects_neighbor_proof_without_unique_target_projection(self):
        payload = {
            "canonical_voice_action_id": "voice-action-3",
            "reserved_worker_stable_id": "worker-message-11",
            "transcript_binding_status": "confirmed",
            "transcript_binding_method": "neighbor_scroll_alignment",
            "binding_candidate_count": 1,
            "tracking_frame_ids": [],
            "tracking_edges": [],
            "native_source_message_id": None,
            "matched_neighbor_pairs": [
                {
                    "pre_observation_id": "before-a",
                    "post_observation_id": "after-a",
                    "sender_role": "customer",
                    "scroll_delta_y": -95.0,
                },
                {
                    "pre_observation_id": "before-b",
                    "post_observation_id": "after-b",
                    "sender_role": "self",
                    "scroll_delta_y": -95.0,
                },
            ],
            "confirmed_action_mapping": {
                "canonical_action_id": "voice-action-3",
                "reserved_worker_stable_id": "worker-message-11",
                "binding_confirmed": True,
                "post_observation_id": "voice-post-a",
                "derived_observation_ids": [],
            },
        }
        pre = [
            observation("before-a", "text", "上文", bubble_rect=[10, 100, 100, 120]),
            observation("voice-pre", "voice", bubble_rect=[10, 290, 100, 310]),
            observation("before-b", "text", "下文", sender_role="self", bubble_rect=[10, 400, 100, 420]),
        ]
        post = [
            observation("after-a", "text", "上文", bubble_rect=[10, 5, 100, 25]),
            observation("voice-post-a", "voice", "正文A", bubble_rect=[10, 195, 100, 215]),
            observation("voice-post-b", "voice", "正文B", bubble_rect=[10, 196, 100, 216]),
            observation("after-b", "text", "下文", sender_role="self", bubble_rect=[10, 305, 100, 325]),
        ]

        with self.assertRaisesRegex(
            ValueError,
            "C2_VOICE_IDENTITY_CONTRACT_INVALID",
        ):
            confirmed_voice_action_mapping(
                voice_payload=payload,
                pre_observations=pre,
                post_observations=post,
                canonical_action_id="voice-action-3",
                reserved_worker_stable_id="worker-message-11",
                expected_pre_frame_id="pre",
                pre_observation_id="voice-pre",
                selected_anchor_keys=set(),
            )

    def test_legacy_cross_action_reattachment_cannot_reenter_identity_path(self):
        aligner_source = inspect.getsource(alignment_module)
        task_runner_source = inspect.getsource(task_runner_module)
        ingest_source = inspect.getsource(
            wechat_c2_module._build_message_ingest_payload_v3
        )

        self.assertNotIn("attach_inflight_worker_ids", task_runner_source)
        self.assertNotIn("voice_worker_ids_by_anchor", task_runner_source)
        self.assertNotIn("observation_identity_signature", task_runner_source)
        self.assertNotIn(
            "reconcile_cross_round_observation_identities",
            task_runner_source,
        )
        binding_source = inspect.getsource(
            task_runner_module.confirmed_voice_action_mapping
        )
        self.assertNotIn("voice_action_journal_anchor_keys", binding_source)
        self.assertNotIn("bubble_rect", binding_source)
        self.assertNotIn("bubble_rect", aligner_source)
        self.assertNotIn("occurrence_index", aligner_source)
        self.assertNotIn("voice_duration", aligner_source)
        self.assertNotIn("source_message_key_from_dedupe", ingest_source)
        self.assertNotIn("ocr_message_identity_context", ingest_source)


if __name__ == "__main__":
    unittest.main()
