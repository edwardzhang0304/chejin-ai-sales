from __future__ import annotations

import inspect
import unittest

import chejin_worker_client.sequence_alignment as alignment_module
import chejin_worker_client.task_runner as task_runner_module
import chejin_worker_client.wechat_c2 as wechat_c2_module

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
        "frame_visual_id": visual_id,
        "voice_state": "untranscribed" if message_type == "voice" else "not_voice",
    }
    if bubble_rect is not None:
        result["bubble_rect"] = bubble_rect
    return result


class SequenceAlignmentTests(unittest.TestCase):
    def test_worker_rejects_deprecated_cross_frame_tracking_binding(self):
        post = [observation("voice-post", "voice", "转写正文")]
        with self.assertRaisesRegex(
            ValueError, "C2_VOICE_IDENTITY_CONTRACT_INVALID"
        ):
            confirmed_voice_action_mapping(
                voice_payload={
                "canonical_voice_action_id": "voice-action-1",
                "reserved_worker_stable_id": "worker-message-9",
                "selected_action_token": "token-a",
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
                    "selected_action_token": "token-a",
                    "pre_observation_id": "voice-pre",
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
                selected_action_token="token-a",
                selected_anchor_keys={"old-frame-anchor"},
            )

    def test_worker_rejects_disconnected_sidecar_tracking_edges(self):
        with self.assertRaisesRegex(
            ValueError, "C2_VOICE_IDENTITY_CONTRACT_INVALID"
        ):
            confirmed_voice_action_mapping(
                voice_payload={
                    "canonical_voice_action_id": "voice-action-1",
                    "reserved_worker_stable_id": "worker-message-9",
                    "selected_action_token": "token-a",
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
                        "selected_action_token": "token-a",
                        "pre_observation_id": "voice-pre",
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
                selected_action_token="token-a",
                selected_anchor_keys={"old-frame-anchor"},
            )

    def test_worker_rejects_native_and_neighbor_as_alternate_action_proofs(self):
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
                "selected_action_token": "token-a",
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
                    "selected_action_token": "token-a",
                    "pre_observation_id": "voice-pre",
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
            with self.assertRaisesRegex(
                ValueError, "C2_VOICE_IDENTITY_CONTRACT_INVALID"
            ):
                confirmed_voice_action_mapping(
                    voice_payload=proof,
                    pre_observations=pre,
                    post_observations=post,
                    canonical_action_id="voice-action-2",
                    reserved_worker_stable_id="worker-message-10",
                    expected_pre_frame_id="pre",
                    pre_observation_id="voice-pre",
                    selected_action_token="token-a",
                    selected_anchor_keys=set(),
                )

    def test_worker_accepts_one_actual_action_result_and_no_other_identity_proof(self):
        post = observation("voice-post", "voice", "转写正文")
        post["row_kind"] = "voice_transcript"
        post["voice_state"] = "transcribed"
        post["sender_role_source"] = "parent_voice"
        signature = task_runner_module.stable_business_content_signature(
            post
        )
        receipt = {
            "schema_version": 1,
            "canonical_action_id": "voice-action-actual",
            "reserved_worker_stable_id": "worker-message-10",
            "selected_action_token": "token-a",
            "pre_observation_id": "voice-pre",
            "trigger_observation_id": "voice-trigger-current-frame",
            "post_observation_id": "voice-post",
            "physical_identity_inherited_from_prepare": False,
            "physical_action_count": 1,
            "result_candidate_count": 1,
            "stable_business_content_signature": signature,
            "result_screen_order": 0,
            "binding_confirmed": True,
        }
        mapping = confirmed_voice_action_mapping(
            voice_payload={
                "canonical_voice_action_id": "voice-action-actual",
                "reserved_worker_stable_id": "worker-message-10",
                "pre_frame_id": "pre",
                "selected_action_token": "token-a",
                "transcript_binding_status": "confirmed",
                "transcript_binding_method": "actual_action_result",
                "binding_candidate_count": 1,
                "action_result_receipt": receipt,
                "confirmed_action_mapping": {
                    **receipt,
                    "derived_observation_ids": [],
                },
            },
            pre_observations=[observation("voice-pre", "voice")],
            post_observations=[post],
            canonical_action_id="voice-action-actual",
            reserved_worker_stable_id="worker-message-10",
            expected_pre_frame_id="pre",
            pre_observation_id="voice-pre",
            selected_action_token="token-a",
            selected_anchor_keys={"diagnostic-only"},
        )
        self.assertEqual(mapping["binding_method"], "actual_action_result")
        self.assertEqual(mapping["post_observation_id"], "voice-post")
        self.assertFalse(mapping["physical_identity_inherited_from_prepare"])

    def test_worker_rejects_neighbor_proof_without_unique_target_projection(self):
        payload = {
            "canonical_voice_action_id": "voice-action-3",
            "reserved_worker_stable_id": "worker-message-11",
            "selected_action_token": "token-a",
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
                "selected_action_token": "token-a",
                "pre_observation_id": "voice-pre",
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
                selected_action_token="token-a",
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
        self.assertNotIn("align_committed_message_sequence", aligner_source)
        self.assertNotIn("build_post_action_observation_sequence", aligner_source)
        self.assertNotIn("inherited_worker_ids", aligner_source)
        self.assertNotIn("source_message_key_from_dedupe", ingest_source)
        self.assertNotIn("ocr_message_identity_context", ingest_source)


if __name__ == "__main__":
    unittest.main()
