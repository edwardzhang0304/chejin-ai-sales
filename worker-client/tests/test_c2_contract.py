from __future__ import annotations

import ast
import os
from pathlib import Path
import tempfile
import unittest

os.environ.setdefault(
    "CHEJIN_WORKER_HOME",
    tempfile.mkdtemp(prefix="chejin-worker-contract-test-"),
)

from chejin_worker_client.c2_contract import (
    formal_image_failure_code,
    image_contract,
    observation_role_is_trusted,
    temporary_capability_gate_codes,
)
from chejin_worker_client.api import ApiError
from chejin_worker_client.c2_outbox_recovery import rebuild_identity_collision
from chejin_worker_client.transaction_outcomes import (
    FlowOutcomeAccumulator,
    classify_action_result,
    classify_outbox_recovery,
    merge_item_outcomes,
    transition_outbox_state,
)
from chejin_worker_client.image_phase import (
    mark_image_action,
    mark_image_settled_without_refresh,
    mark_image_terminal,
    merge_image_phase_results,
    new_image_phase_result,
)
from chejin_worker_client.omniauto_vision import (
    VISION_WINDOW_STABLE_FAILURE_REASONS,
)
from chejin_worker_client.wechat_c2 import (
    project_final_slot_flow_gates,
)


class C2ContractTests(unittest.TestCase):
    def test_outbox_recovery_uses_only_backend_action(self):
        for recovery_action in (
            "retry",
            "refresh_identity_and_retry",
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

    def test_identity_collision_rebuilds_only_the_identity_envelope(self):
        payload = {
            "conversation_id": "conv-1",
            "messages": [
                {
                    "source_message_key": "source-old",
                    "dedupe_key": "dedupe-old",
                    "sender_role_hint": "customer",
                    "message_type": "text",
                    "content": "原消息正文",
                    "raw_payload": {
                        "source_message_key": "source-old",
                        "dedupe_basis": {
                            "source": "worker_cross_round_sequence",
                            "worker_stable_id": "worker-message-1",
                        },
                        "observation": {"content_clean": "原消息正文"},
                    },
                }
            ],
            "evidence": {
                "ingest_partition": {
                    "expected_source_message_keys": ["source-old"]
                }
            },
        }

        rebuilt, replacement = rebuild_identity_collision(
            payload,
            source_message_key="source-old",
            dedupe_key="dedupe-old",
            next_sequence_floor=8,
        )

        item = rebuilt["messages"][0]
        self.assertEqual(item["content"], "原消息正文")
        self.assertEqual(item["sender_role_hint"], "customer")
        self.assertEqual(replacement["new_stable_id"], "worker-message-8")
        self.assertNotEqual(item["source_message_key"], "source-old")
        self.assertNotEqual(item["dedupe_key"], "dedupe-old")
        self.assertEqual(
            item["raw_payload"]["dedupe_basis"]["worker_stable_id"],
            "worker-message-8",
        )
        self.assertEqual(
            rebuilt["evidence"]["ingest_partition"][
                "expected_source_message_keys"
            ],
            [item["source_message_key"]],
        )
        self.assertEqual(
            classify_outbox_recovery("unrecognized_action"),
            "capability_paused",
        )

    def test_identity_collision_refuses_shared_stable_id(self):
        payload = {
            "conversation_id": "conv-1",
            "messages": [
                {
                    "source_message_key": "source-conflict",
                    "dedupe_key": "dedupe-conflict",
                    "message_type": "text",
                    "content": "冲突消息",
                    "raw_payload": {
                        "dedupe_basis": {
                            "source": "worker_cross_round_sequence",
                            "worker_stable_id": "worker-message-7",
                        }
                    },
                },
                {
                    "source_message_key": "source-other",
                    "dedupe_key": "dedupe-other",
                    "message_type": "text",
                    "content": "另一条消息",
                    "raw_payload": {
                        "dedupe_basis": {
                            "source": "worker_cross_round_sequence",
                            "worker_stable_id": "worker-message-7",
                        }
                    },
                },
            ],
        }

        with self.assertRaisesRegex(
            ValueError,
            "MESSAGE_IDENTITY_COLLISION_ITEM_AMBIGUOUS",
        ):
            rebuild_identity_collision(
                payload,
                source_message_key="source-conflict",
                dedupe_key="dedupe-conflict",
                next_sequence_floor=8,
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

    def test_image_completion_uses_the_producer_business_verdict(self):
        completed = classify_action_result(
            "image",
            {
                "action_phase": "confirmed",
                "state": "completed",
                "business_state": "completed",
                "business_result_confirmed": True,
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
                "business_state": "failed",
                "business_result_confirmed": False,
                "customer_image_understanding": {},
            },
            source_message_key="image-2",
        )

        self.assertEqual(completed["result"], "completed")
        self.assertEqual(copied_only["result"], "failed")

    def test_image_failure_reason_mapping_is_exact_and_contract_driven(self):
        expected = {
            "image_context_menu_copy_item_missing": (
                "C2_IMAGE_MENU_OPERATION_FAILED"
            ),
            "image_context_menu_copy_click_failed": (
                "C2_IMAGE_MENU_OPERATION_FAILED"
            ),
            "clipboard_sequence_missing_before_copy": (
                "C2_IMAGE_CLIPBOARD_TRANSACTION_FAILED"
            ),
            "clipboard_sequence_unchanged_after_copy": (
                "C2_IMAGE_CLIPBOARD_TRANSACTION_FAILED"
            ),
            "image_clipboard_transaction_lock_timeout": (
                "C2_IMAGE_CLIPBOARD_TRANSACTION_FAILED"
            ),
            "clipboard_image_fingerprint_mismatch": (
                "C2_IMAGE_CLIPBOARD_TRANSACTION_FAILED"
            ),
            "clipboard_clear_failed": (
                "C2_IMAGE_CLIPBOARD_CLEAR_FAILED"
            ),
            "customer_image_understanding_provider_failed": (
                "C2_IMAGE_UNDERSTANDING_FAILED"
            ),
        }
        for reason, code in expected.items():
            with self.subTest(reason=reason):
                self.assertEqual(
                    formal_image_failure_code(reason),
                    code,
                )

    def test_all_runtime_image_failure_reasons_are_explicitly_mapped(self):
        root = Path(__file__).resolve().parents[1]
        transaction_path = (
            root
            / "omniauto-rpa"
            / "apps"
            / "wechat_ai_customer_service"
            / "optional_plugins"
            / "vision"
            / "capture"
            / "transaction.py"
        )
        tree = ast.parse(transaction_path.read_text(encoding="utf-8"))
        runtime_reasons: set[str] = set()
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "fail"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                runtime_reasons.add(node.args[0].value)
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = (
                    node.targets
                    if isinstance(node, ast.Assign)
                    else [node.target]
                )
                if not any(
                    isinstance(target, ast.Name)
                    and target.id == "clipboard_reason"
                    for target in targets
                ):
                    continue
                if (
                    isinstance(node.value, ast.Constant)
                    and isinstance(node.value.value, str)
                ):
                    runtime_reasons.add(node.value.value)
        runtime_reasons.update(
            {
                "MESSAGE_IDENTITY_UNCONFIRMED",
                "vision_cancelled_before_start",
                "vision_configuration_incomplete",
                "vision_configuration_invalid",
                "vision_plugin_exception",
                "vision_cancelled_after_provider",
                "vision_result_invalid",
                "vision_understanding_missing",
                "C2_IMAGE_UNDERSTANDING_SCHEMA_INVALID",
            }
        )
        runtime_reasons.update(VISION_WINDOW_STABLE_FAILURE_REASONS)
        reason_map = image_contract()["failure_reason_to_error_code"]
        self.assertEqual(
            sorted(runtime_reasons - set(reason_map)),
            [],
            "图片运行时原因必须在机器合同中逐项映射",
        )
        retired_owner_reasons = {
            "clipboard_copy_ownership_unconfirmed",
            "clipboard_owner_api_unavailable",
            "clipboard_owner_window_missing",
            "clipboard_owner_not_wechat_image",
            "clipboard_owner_check_failed",
        }
        self.assertTrue(retired_owner_reasons.isdisjoint(reason_map))

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

    def test_image_temporary_gates_are_retired_from_contract(self):
        temporary_codes = temporary_capability_gate_codes()
        self.assertNotIn("C2_VISION_CAPABILITY_PAUSED", temporary_codes)
        self.assertNotIn("C2_IMAGE_PROCESSING_DEFERRED", temporary_codes)
        self.assertIn("C2_INGEST_PARTITION_INCOMPLETE", temporary_codes)

    def test_flow_gate_details_preserve_distinct_subject_evidence(self):
        projection = project_final_slot_flow_gates(
            {
                "history_gap": False,
                "identity_errors": [],
                "flow_gate_details": [],
                "slot_ledger_states": [
                    {
                        "source_message_key": "voice-customer",
                        "screen_order": 2,
                        "order_source": "visual_top",
                    },
                    {
                        "source_message_key": "voice-self",
                        "screen_order": 3,
                        "order_source": "visual_top",
                    },
                    {
                        "source_message_key": "voice-self-copy",
                        "screen_order": 3,
                        "order_source": "visual_top",
                    },
                ],
            },
            failed_voice_source_roles={
                "voice-customer": "customer",
                "voice-self": "self",
                "voice-self-copy": "self",
            },
        )
        details = projection["flow_gate_details"]

        self.assertEqual(len(details), 2)
        self.assertEqual(
            [item["subject_sender_role"] for item in details],
            ["customer", "self"],
        )

    def test_image_phase_statistics_accumulate_unique_messages(self):
        first = new_image_phase_result()
        mark_image_action(first, "image-1")
        mark_image_terminal(
            first,
            "image-1",
            terminal_state="completed",
        )
        second = new_image_phase_result()
        mark_image_action(second, "image-2")
        mark_image_terminal(
            second,
            "image-2",
            terminal_state="completed",
        )
        cached_repeat = new_image_phase_result()
        mark_image_terminal(
            cached_repeat,
            "image-1",
            terminal_state="completed",
            cached=True,
        )

        merge_image_phase_results(first, second)
        merge_image_phase_results(first, cached_repeat)

        self.assertEqual(first["completed"], 2)
        self.assertEqual(first["cached"], 1)
        self.assertEqual(first["new_action_count"], 2)
        self.assertEqual(
            first["completed_source_keys"],
            ["image-1", "image-2"],
        )

    def test_definitive_non_bitmap_failure_does_not_require_chat_refresh(self):
        result = new_image_phase_result()
        mark_image_action(result, "invalid-image")
        mark_image_settled_without_refresh(result, "invalid-image")
        mark_image_terminal(
            result,
            "invalid-image",
            terminal_state="failed",
        )

        self.assertEqual(result["new_action_count"], 1)
        self.assertEqual(result["failed"], 1)
        self.assertFalse(result["requires_final_refresh"])
        self.assertEqual(result["refresh_source_keys"], [])

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
