from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from PIL import Image

os.environ.setdefault(
    "CHEJIN_WORKER_HOME",
    tempfile.mkdtemp(prefix="chejin-voice-v3131-test-"),
)

WORKER_ROOT = Path(__file__).resolve().parents[1]
OMNIAUTO_ROOT = WORKER_ROOT / "omniauto-rpa"
if str(OMNIAUTO_ROOT) not in sys.path:
    sys.path.insert(0, str(OMNIAUTO_ROOT))

from apps.wechat_ai_customer_service.adapters import (  # noqa: E402
    wechat_win32_ocr_sidecar as sidecar,
)
from chejin_worker_client.action_journal import (  # noqa: E402
    action_journal_phase,
    initialize_action_journal,
    read_action_journal,
)
from chejin_worker_client.task_runner import (  # noqa: E402
    TaskRunner,
    confirmed_voice_action_mapping,
)


def _post_observation(observation_id: str = "voice-post") -> dict:
    return {
        "observation_id": observation_id,
        "row_kind": "voice_transcript",
        "message_type": "voice",
        "sender_role": "customer",
    }


class VoiceActionV3132Test(unittest.TestCase):
    def test_two_frame_tracking_cannot_claim_continuous_binding(self):
        payload = {
            "canonical_voice_action_id": "action-1",
            "reserved_worker_stable_id": "worker-message-1",
            "transcript_binding_status": "confirmed",
            "transcript_binding_method": "continuous_target_tracking",
            "binding_candidate_count": 1,
            "tracking_frame_ids": ["frame-a", "frame-b"],
            "tracking_edges": [
                {
                    "from_frame_id": "frame-a",
                    "from_observation_id": "voice-a",
                    "to_frame_id": "frame-b",
                    "to_observation_id": "voice-post",
                    "sender_role": "customer",
                    "message_type": "voice",
                    "structural_evidence": {"unique": True},
                    "displacement_evidence": {"continuous": True},
                    "edge_candidate_count": 1,
                }
            ],
            "matched_neighbor_pairs": [],
            "native_source_message_id": None,
            "confirmed_action_mapping": {
                "canonical_action_id": "action-1",
                "reserved_worker_stable_id": "worker-message-1",
                "binding_confirmed": True,
                "post_observation_id": "voice-post",
                "derived_observation_ids": [],
            },
        }

        with self.assertRaisesRegex(
            ValueError, "C2_VOICE_IDENTITY_CONTRACT_INVALID"
        ):
            confirmed_voice_action_mapping(
                voice_payload=payload,
                pre_observations=[
                    {**_post_observation(), "observation_id": "voice-a"}
                ],
                post_observations=[_post_observation()],
                canonical_action_id="action-1",
                reserved_worker_stable_id="worker-message-1",
                expected_pre_frame_id="frame-a",
                pre_observation_id="voice-a",
                selected_anchor_keys=set(),
            )

    def test_disconnected_tracking_edges_cannot_confirm_binding(self):
        payload = {
            "canonical_voice_action_id": "action-1",
            "reserved_worker_stable_id": "worker-message-1",
            "transcript_binding_status": "confirmed",
            "transcript_binding_method": "continuous_target_tracking",
            "binding_candidate_count": 1,
            "tracking_frame_ids": ["frame-a", "frame-b", "frame-c"],
            "tracking_edges": [
                {
                    "from_frame_id": "frame-a",
                    "from_observation_id": "voice-a",
                    "to_frame_id": "frame-b",
                    "to_observation_id": "voice-b",
                    "sender_role": "customer",
                    "message_type": "voice",
                    "structural_evidence": {"unique": True},
                    "displacement_evidence": {"continuous": True},
                    "edge_candidate_count": 1,
                },
                {
                    "from_frame_id": "frame-b",
                    "from_observation_id": "different-voice",
                    "to_frame_id": "frame-c",
                    "to_observation_id": "voice-post",
                    "sender_role": "customer",
                    "message_type": "voice",
                    "structural_evidence": {"unique": True},
                    "displacement_evidence": {"continuous": True},
                    "edge_candidate_count": 1,
                },
            ],
            "matched_neighbor_pairs": [],
            "native_source_message_id": None,
            "confirmed_action_mapping": {
                "canonical_action_id": "action-1",
                "reserved_worker_stable_id": "worker-message-1",
                "binding_confirmed": True,
                "post_observation_id": "voice-post",
                "derived_observation_ids": [],
            },
        }

        with self.assertRaisesRegex(
            ValueError, "C2_VOICE_IDENTITY_CONTRACT_INVALID"
        ):
            confirmed_voice_action_mapping(
                voice_payload=payload,
                pre_observations=[
                    {**_post_observation(), "observation_id": "voice-a"}
                ],
                post_observations=[_post_observation()],
                canonical_action_id="action-1",
                reserved_worker_stable_id="worker-message-1",
                expected_pre_frame_id="frame-a",
                pre_observation_id="voice-a",
                selected_anchor_keys=set(),
            )

    def _journal(self, directory: str) -> Path:
        path = Path(directory) / "voice-action.json"
        initialize_action_journal(
            path,
            action_kind="voice",
            transaction_id="action-1",
            conversation_id="conversation-1",
            origin_read_run_id="read-1",
            items=[
                {
                    "journal_item_id": "action-1",
                    "action_local_id": "action-1",
                    "physical_anchor_keys": ["anchor-a"],
                }
            ],
            pre_frame_id="frame-a",
            canonical_action_id="action-1",
            reserved_worker_stable_id="worker-message-1",
            prepare_evidence={
                "pre_frame_id": "frame-a",
                "selected_pre_observation_id": "voice-a",
                "selected_action_token": "token-a",
                "selected_target_fingerprint": "fingerprint-a",
                "candidate_group_count": 1,
            },
        )
        return path

    def test_sidecar_stale_phase_write_cannot_overwrite_success_fact(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal = self._journal(tmp)
            sidecar.write_action_phase_journal(
                str(journal),
                "confirmed",
                physical_anchor_keys=["anchor-a"],
                business_state="completed",
                business_result_confirmed=True,
                error_code="",
                terminal_payload={
                    "state": "completed",
                    "transcript": "已确认文本",
                },
            )
            confirmed = read_action_journal(journal)

            sidecar.write_action_phase_journal(
                str(journal),
                "not_attempted",
                physical_anchor_keys=["anchor-a"],
                business_state="failed",
                business_result_confirmed=False,
                error_code="STALE_FAILURE",
                terminal_payload={"state": "failed"},
            )
            after_stale_write = read_action_journal(journal)

            self.assertEqual(after_stale_write, confirmed)
            item = next(iter(after_stale_write["items"].values()))
            self.assertEqual(item["action_phase"], "confirmed")
            self.assertEqual(item["business_state"], "completed")
            self.assertTrue(item["business_result_confirmed"])
            self.assertIsNone(item["error_code"])
            self.assertEqual(
                item["terminal_payload"],
                {"state": "completed", "transcript": "已确认文本"},
            )

    def test_new_voice_in_same_observation_seat_cancels_before_any_click(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal = self._journal(tmp)
            click = Mock()
            candidate = {
                # Viewport-derived observation ids and anchors may be reused
                # when a newly arrived same-duration voice occupies A's old
                # seat.  Only the fresh physical fingerprint may keep the
                # prepare token attached to the original bubble.
                "observation_id": "voice-a",
                "voice_state": "untranscribed",
                "sender_role": "customer",
                "action_target": {"anchor_key": "anchor-a"},
            }
            image = Image.new("RGB", (800, 600), "white")
            with patch.object(sidecar, "capture_wechat", return_value=(image, "frame-b.png")), patch.object(
                sidecar, "run_ocr", return_value=[]
            ), patch.object(
                sidecar, "parse_current_chat_frame_messages", return_value=[]
            ), patch.object(
                sidecar, "build_unified_voice_observations_v3", return_value=[candidate]
            ), patch.object(
                sidecar, "_voice_observation_fingerprint", return_value="fingerprint-b"
            ), patch.object(
                sidecar, "human_window_image_click_in_bounds", click
            ):
                result = sidecar.execute_voice_action_payload(
                    1,
                    {},
                    target="CJK7M4Q2",
                    artifact_dir=None,
                    confirm_target="",
                    confirm_exact=False,
                    action_journal_path=str(journal),
                    canonical_voice_action_id="action-1",
                    reserved_worker_stable_id="worker-message-1",
                    pre_frame_id="frame-a",
                    selected_pre_observation_id="voice-a",
                    selected_action_token="token-a",
                    selected_target_fingerprint="fingerprint-a",
                )

            self.assertEqual(
                result["state"], "voice_action_cancelled_before_trigger"
            )
            self.assertFalse(result["ui_action_performed"])
            self.assertEqual(
                action_journal_phase(journal), "cancelled_before_trigger"
            )
            click.assert_not_called()

    def test_invalid_visible_button_bounds_cancel_before_trigger(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal = self._journal(tmp)
            click = Mock()
            candidate = {
                "observation_id": "voice-a",
                "voice_state": "untranscribed",
                "sender_role": "customer",
                "action_target": {"anchor_key": "anchor-a"},
                "visible_button_target": {
                    "click_bounds": [420, 220, 420, 250]
                },
            }
            image = Image.new("RGB", (800, 600), "white")
            with patch.object(
                sidecar,
                "capture_wechat",
                return_value=(image, "frame-a-execute.png"),
            ), patch.object(
                sidecar, "run_ocr", return_value=[]
            ), patch.object(
                sidecar,
                "parse_current_chat_frame_messages",
                return_value=[],
            ), patch.object(
                sidecar,
                "build_unified_voice_observations_v3",
                return_value=[candidate],
            ), patch.object(
                sidecar,
                "_voice_observation_fingerprint",
                return_value="fingerprint-a",
            ), patch.object(
                sidecar,
                "voice_context_anchor_exclusion_keys",
                return_value={"anchor-a"},
            ), patch.object(
                sidecar, "human_window_image_click_in_bounds", click
            ):
                result = sidecar.execute_voice_action_payload(
                    1,
                    {},
                    target="CJK7M4Q2",
                    artifact_dir=None,
                    confirm_target="",
                    confirm_exact=False,
                    action_journal_path=str(journal),
                    canonical_voice_action_id="action-1",
                    reserved_worker_stable_id="worker-message-1",
                    pre_frame_id="frame-a",
                    selected_pre_observation_id="voice-a",
                    selected_action_token="token-a",
                    selected_target_fingerprint="fingerprint-a",
                )

            self.assertEqual(
                result["state"],
                "voice_action_cancelled_before_trigger",
            )
            self.assertFalse(result["ui_action_performed"])
            self.assertEqual(
                action_journal_phase(journal),
                "cancelled_before_trigger",
            )
            click.assert_not_called()

    def test_ambiguous_result_is_quarantined_after_two_reads_and_one_click(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal = self._journal(tmp)
            image = Image.new("RGB", (800, 600), "white")
            capture = Mock(
                side_effect=[
                    (image, "execute-before.png"),
                    (image, "evidence-1.png"),
                    (image, "evidence-2.png"),
                ]
            )
            candidate = {
                "observation_id": "voice-a",
                "voice_state": "untranscribed",
                "sender_role": "customer",
                "action_target": {"anchor_key": "anchor-a"},
                "visible_button_target": {"click_bounds": [420, 220, 500, 250]},
            }
            click = Mock(return_value={"ok": True})
            with patch.object(sidecar, "capture_wechat", capture), patch.object(
                sidecar, "run_ocr", return_value=[]
            ), patch.object(
                sidecar, "parse_current_chat_frame_messages", return_value=[]
            ), patch.object(
                sidecar, "build_unified_voice_observations_v3", return_value=[candidate]
            ), patch.object(
                sidecar, "_voice_observation_fingerprint", return_value="fingerprint-a"
            ), patch.object(
                sidecar, "voice_context_anchor_exclusion_keys", return_value={"anchor-a"}
            ), patch.object(
                sidecar, "human_window_image_click_in_bounds", click
            ), patch.object(
                sidecar, "humanized_action_sleep", return_value=None
            ), patch.object(
                sidecar, "_bind_voice_transcripts_for_action", return_value=[]
            ):
                result = sidecar.execute_voice_action_payload(
                    1,
                    {},
                    target="CJK7M4Q2",
                    artifact_dir=None,
                    confirm_target="",
                    confirm_exact=False,
                    action_journal_path=str(journal),
                    canonical_voice_action_id="action-1",
                    reserved_worker_stable_id="worker-message-1",
                    pre_frame_id="frame-a",
                    selected_pre_observation_id="voice-a",
                    selected_action_token="token-a",
                    selected_target_fingerprint="fingerprint-a",
                )

            self.assertEqual(result["state"], "voice_transcribe_ambiguous")
            self.assertEqual(result["action_phase"], "quarantined")
            self.assertEqual(action_journal_phase(journal), "quarantined")
            self.assertEqual(capture.call_count, 3)
            self.assertEqual(click.call_count, 1)

    def test_click_failure_tracks_exact_voice_and_returns_finite_failed_fact(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal = self._journal(tmp)
            image = Image.new("RGB", (800, 600), "white")
            candidate = {
                "observation_id": "voice-a",
                "voice_state": "untranscribed",
                "sender_role": "customer",
                "action_target": {"anchor_key": "anchor-a"},
                "visible_button_target": {
                    "click_bounds": [420, 220, 500, 250]
                },
            }
            failed_observation = {
                "observation_id": "voice-failed-final",
                "row_kind": "voice_bubble",
                "message_type": "voice",
                "voice_state": "untranscribed",
                "sender_role": "customer",
                "action_target": {"anchor_key": "anchor-a"},
                "source_message": {
                    "id": "voice-failed-final",
                    "type": "voice",
                    "sender_role": "customer",
                },
            }
            click = Mock(return_value={"ok": False, "reason": "click_failed"})
            with patch.object(
                sidecar,
                "capture_wechat",
                side_effect=[
                    (image, "execute-before.png"),
                    (image, "execute-failed-final.png"),
                ],
            ), patch.object(
                sidecar, "run_ocr", return_value=[]
            ), patch.object(
                sidecar,
                "parse_current_chat_frame_messages",
                side_effect=[[], [{"id": "voice-failed-final"}]],
            ), patch.object(
                sidecar,
                "build_unified_voice_observations_v3",
                side_effect=[[candidate], [candidate]],
            ), patch.object(
                sidecar,
                "build_message_observations_v3",
                return_value=[failed_observation],
            ), patch.object(
                sidecar,
                "_voice_observation_fingerprint",
                return_value="fingerprint-a",
            ), patch.object(
                sidecar,
                "voice_context_anchor_exclusion_keys",
                return_value={"anchor-a"},
            ), patch.object(
                sidecar,
                "human_window_image_click_in_bounds",
                click,
            ), patch.object(
                sidecar, "humanized_action_sleep", return_value=None
            ):
                result = sidecar.execute_voice_action_payload(
                    1,
                    {},
                    target="CJK7M4Q2",
                    artifact_dir=None,
                    confirm_target="",
                    confirm_exact=False,
                    action_journal_path=str(journal),
                    canonical_voice_action_id="action-1",
                    reserved_worker_stable_id="worker-message-1",
                    pre_frame_id="frame-a",
                    selected_pre_observation_id="voice-a",
                    selected_action_token="token-a",
                    selected_target_fingerprint="fingerprint-a",
                )

            self.assertEqual(result["state"], "voice_transcribe_click_failed")
            self.assertEqual(result["action_phase"], "failed")
            self.assertEqual(result["transcript_binding_status"], "failed")
            self.assertEqual(result["binding_candidate_count"], 1)
            self.assertTrue(
                result["confirmed_action_mapping"]["binding_confirmed"]
            )
            self.assertEqual(
                result["confirmed_action_mapping"]["post_observation_id"],
                "voice-failed-final",
            )
            self.assertEqual(action_journal_phase(journal), "failed")
            self.assertEqual(click.call_count, 1)

    def test_image_ui_action_invalidates_frame_even_when_phase_not_attempted(self):
        normalized = TaskRunner._normalize_one_image_slot_result(
            {
                "state": "failed",
                "reason": "C2_IMAGE_SOURCE_INVALID",
                "action_phase": "not_attempted",
                "transaction": {
                    "status": "voice_context_menu_rejected",
                    "right_click_ok": True,
                },
            },
        )

        self.assertFalse(normalized["action_was_attempted"])
        self.assertTrue(normalized["ui_frame_invalidated"])

    def test_image_explicit_ui_action_invalidates_frame_without_adapter_flags(
        self,
    ):
        normalized = TaskRunner._normalize_one_image_slot_result(
            {
                "state": "failed",
                "reason": "C2_IMAGE_SOURCE_INVALID",
                "action_phase": "not_attempted",
                "ui_action_performed": True,
                "transaction": {"status": "menu_evidence_incomplete"},
            },
        )

        self.assertFalse(normalized["action_was_attempted"])
        self.assertTrue(normalized["ui_frame_invalidated"])

    def test_image_not_visible_cannot_hide_an_explicit_ui_action(self):
        normalized = TaskRunner._normalize_one_image_slot_result(
            {
                "state": "image_not_visible",
                "reason": "image_bubble_not_visible_after_refresh",
                "action_phase": "not_attempted",
                "ui_action_performed": True,
                "transaction": {"status": "menu_dismissed"},
            },
        )

        self.assertFalse(normalized["removed_from_final_screen"])
        self.assertTrue(normalized["ui_frame_invalidated"])
        self.assertEqual(normalized["terminal_state"], "failed")

    def test_production_has_one_voice_orchestrator_and_no_retired_paths(self):
        task_runner_source = (
            WORKER_ROOT / "chejin_worker_client" / "task_runner.py"
        ).read_text(encoding="utf-8")
        bridge_source = (
            WORKER_ROOT / "chejin_worker_client" / "rpa_bridge.py"
        ).read_text(encoding="utf-8")
        sidecar_source = sidecar.__file__ and Path(sidecar.__file__).read_text(
            encoding="utf-8"
        )
        combined = "\n".join(
            [task_runner_source, bridge_source, sidecar_source]
        )

        self.assertEqual(task_runner_source.count(".prepare_voice_action("), 1)
        self.assertEqual(task_runner_source.count(".execute_voice_action("), 1)
        dependency_source = task_runner_source[
            task_runner_source.index("    def _c2_dependencies_ready") :
            task_runner_source.index(
                "    def _c2_vision_ready_before_scan"
            )
        ]
        self.assertIn('"prepare_voice_action"', dependency_source)
        self.assertIn('"execute_voice_action"', dependency_source)
        self.assertNotIn('"voice_transcribe"', dependency_source)
        for retired in (
            "tracking_candidate_counts",
            "settled_without_refresh",
            "reconcile_v16104_identity_transition",
            "def voice_transcribe_payload(",
            ".voice_transcribe(",
        ):
            self.assertNotIn(retired, combined)


if __name__ == "__main__":
    unittest.main()
