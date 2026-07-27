from __future__ import annotations

import os
import tempfile
import unittest

os.environ.setdefault(
    "CHEJIN_WORKER_HOME",
    tempfile.mkdtemp(prefix="chejin-worker-contract-test-"),
)

from chejin_worker_client.c2_contract import (
    observation_role_is_trusted,
    temporary_capability_gate_codes,
)
from chejin_worker_client.api import ApiError
from chejin_worker_client.transaction_outcomes import (
    FlowOutcomeAccumulator,
    classify_action_result,
    classify_outbox_recovery,
    merge_item_outcomes,
    transition_outbox_state,
)
from chejin_worker_client.wechat_c2 import build_vision_capability_pause_gate


class C2ContractTests(unittest.TestCase):
    def test_outbox_recovery_uses_only_backend_action(self):
        for recovery_action in (
            "retry",
            "refresh_and_rebuild",
            "rebuild_failed_facts",
            "split_and_retry",
            "capability_paused",
            "target_terminated",
            "conversation_terminated",
        ):
            with self.subTest(recovery_action=recovery_action):
                exc = ApiError(
                    "ANY_CODE",
                    "error text must not affect recovery",
                    409,
                    {"recovery_action": recovery_action},
                )
                self.assertEqual(
                    classify_outbox_recovery(exc),
                    recovery_action,
                )
        self.assertEqual(
            classify_outbox_recovery(
                ApiError("UNKNOWN", "missing action", 503, {})
            ),
            "capability_paused",
        )
        self.assertEqual(
            classify_outbox_recovery(ConnectionError("offline")),
            "retry",
        )
        self.assertEqual(
            classify_outbox_recovery("refresh_and_rebuild"),
            "refresh_and_rebuild",
        )
        self.assertEqual(
            classify_outbox_recovery(None),
            "capability_paused",
        )
        self.assertEqual(
            classify_outbox_recovery("unrecognized_action"),
            "capability_paused",
        )

    def test_action_result_matrix_is_contract_driven(self):
        cases = (
            ("send", "not_attempted", None, False, "failed"),
            ("send", "trigger_attempted", None, False, "unknown"),
            ("send", "confirmed", "sent", True, "sent"),
            ("voice", "not_attempted", None, False, "failed"),
            ("voice", "trigger_attempted", None, False, "failed"),
            ("voice", "confirmed", "completed", True, "completed"),
            ("image", "not_attempted", None, False, "failed"),
            ("image", "trigger_attempted", None, False, "failed"),
            ("image", "confirmed", "completed", True, "completed"),
        )
        for action, phase, business_state, confirmed, expected in cases:
            with self.subTest(action=action, phase=phase):
                result = classify_action_result(
                    action,
                    {
                        "action_phase": phase,
                        "business_state": business_state,
                        "business_result_confirmed": confirmed,
                    },
                    source_message_key="source-1",
                )
                self.assertEqual(result["result"], expected)

    def test_confirmed_action_is_not_completed_without_business_evidence(self):
        expected = {
            "send": ("unknown", "SEND_RESULT_UNKNOWN"),
            "voice": (
                "failed",
                "VOICE_TRANSCRIBE_RESULT_UNCONFIRMED",
            ),
            "image": (
                "failed",
                "IMAGE_UNDERSTANDING_RESULT_UNCONFIRMED",
            ),
        }
        for action, (result, error_code) in expected.items():
            with self.subTest(action=action):
                classified = classify_action_result(
                    action,
                    {"action_phase": "confirmed"},
                    source_message_key="source-1",
                )
                self.assertEqual(classified["result"], result)
                self.assertEqual(classified["error_code"], error_code)
                self.assertFalse(classified["contract_valid"])

    def test_image_completion_is_decided_inside_the_single_classifier(self):
        completed = classify_action_result(
            "image",
            {
                "action_phase": "confirmed",
                "state": "completed",
                "customer_image_understanding": {
                    "applied": True,
                    "vision_summary": "车辆图片",
                },
            },
            source_message_key="image-1",
        )
        copied_only = classify_action_result(
            "image",
            {
                "action_phase": "confirmed",
                "state": "completed",
                "customer_image_understanding": {},
            },
            source_message_key="image-2",
        )

        self.assertEqual(completed["result"], "completed")
        self.assertEqual(copied_only["result"], "failed")

    def test_send_confirmation_requires_a_physical_trigger(self):
        classified = classify_action_result(
            "send",
            {
                "action_phase": "confirmed",
                "send_result": {
                    "result": "sent",
                    "confirmed": True,
                    "physical_send_triggered": False,
                },
            },
        )

        self.assertEqual(classified["result"], "unknown")
        self.assertEqual(classified["action_phase"], "trigger_attempted")

    def test_missing_item_action_phase_never_authorizes_a_repeat(self):
        for action in ("send", "voice", "image"):
            with self.subTest(action=action):
                classified = classify_action_result(
                    action,
                    {
                        "business_state": "completed",
                        "business_result_confirmed": True,
                    },
                )
                self.assertEqual(
                    classified["action_phase"],
                    "trigger_attempted",
                )
                self.assertFalse(classified["contract_valid"])

    def test_item_outcomes_are_monotonic(self):
        merged = merge_item_outcomes(
            [
                {
                    "source_message_key": "voice-1",
                    "result": "completed",
                }
            ],
            [
                {
                    "source_message_key": "voice-2",
                    "result": "failed",
                }
            ],
        )
        self.assertEqual(
            {
                (item["source_message_key"], item["result"])
                for item in merged
            },
            {("voice-1", "completed"), ("voice-2", "failed")},
        )
        self.assertEqual(merge_item_outcomes(merged, []), merged)

    def test_flow_accumulator_preserves_prior_terminal_results(self):
        accumulator = FlowOutcomeAccumulator()
        accumulator.record(
            {
                "source_message_key": "voice-1",
                "result": "completed",
                "terminal_payload": {"content": "已转写"},
            }
        )
        accumulator.extend([])

        self.assertEqual(
            accumulator.snapshot()[0]["terminal_payload"]["content"],
            "已转写",
        )

    def test_outbox_refresh_never_discards_facts_after_fixed_attempt_count(self):
        for attempt in (1, 2, 3, 4, 20, 100):
            self.assertEqual(
                transition_outbox_state(
                    current_state="refresh_pending",
                    event="refresh_and_rebuild",
                    attempt_count=attempt,
                    refresh_attempt_count=attempt,
                ),
                "refresh_pending",
            )
        self.assertEqual(
            transition_outbox_state(
                current_state="refresh_pending",
                event="capability_paused",
                attempt_count=101,
                refresh_attempt_count=101,
            ),
            "capability_paused",
        )

    def test_vision_pause_gate_uses_contract_trusted_slot_position(self):
        self.assertIn(
            "C2_VISION_CAPABILITY_PAUSED",
            temporary_capability_gate_codes(),
        )
        errors, details = build_vision_capability_pause_gate(
            [
                {
                    "row_kind": "image_bubble",
                    "ledger_state": "NEW_MESSAGE",
                    "screen_order": 2,
                    "order_source": "visual_top",
                }
            ]
        )

        self.assertEqual(errors, ["C2_VISION_CAPABILITY_PAUSED"])
        self.assertEqual(
            details,
            [
                {
                    "error_code": "C2_VISION_CAPABILITY_PAUSED",
                    "position_source": "slot_ledger_visual_top",
                    "min_screen_order": 2,
                    "max_screen_order": 2,
                }
            ],
        )

    def test_role_trust_is_derived_from_each_contract_row_rule(self):
        self.assertTrue(
            observation_role_is_trusted(
                {
                    "row_kind": "text_bubble",
                    "sender_role": "customer",
                    "sender_role_source": "same_row_avatar",
                }
            )
        )
        self.assertTrue(
            observation_role_is_trusted(
                {
                    "row_kind": "voice_transcript",
                    "sender_role": "self",
                    "sender_role_source": "parent_voice",
                }
            )
        )
        self.assertTrue(
            observation_role_is_trusted(
                {
                    "row_kind": "image_bubble",
                    "sender_role": "self",
                    "sender_role_source": "same_row_avatar",
                }
            )
        )

    def test_role_trust_rejects_cross_row_or_unknown_sources(self):
        self.assertFalse(
            observation_role_is_trusted(
                {
                    "row_kind": "voice_transcript",
                    "sender_role": "customer",
                    "sender_role_source": "same_row_avatar",
                }
            )
        )
        self.assertFalse(
            observation_role_is_trusted(
                {
                    "row_kind": "image_bubble",
                    "sender_role": "customer",
                    "sender_role_source": "vision",
                }
            )
        )
        self.assertFalse(
            observation_role_is_trusted(
                {
                    "row_kind": "unknown",
                    "sender_role": "customer",
                    "sender_role_source": "same_row_avatar",
                }
            )
        )


if __name__ == "__main__":
    unittest.main()
