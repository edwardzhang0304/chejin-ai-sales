from __future__ import annotations

import ast
from contextlib import redirect_stdout
import io
import inspect
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault(
    "CHEJIN_WORKER_HOME",
    tempfile.mkdtemp(prefix="chejin-worker-contract-test-"),
)

from chejin_worker_client.c2_contract import (
    c2_contract_v3,
    formal_image_failure_code,
    image_contract,
    observation_role_is_trusted,
    sidecar_contract_error,
    temporary_capability_gate_codes,
    validate_sequence_alignment_evidence,
    validate_slot_ledger_states,
)
from chejin_worker_client.config import ClientConfig
from chejin_worker_client.api import ApiError
from chejin_worker_client import message_viewport_projection as worker_projection
from chejin_worker_client.transaction_outcomes import (
    FlowOutcomeAccumulator,
    classify_action_result,
    classify_outbox_recovery,
    merge_item_outcomes,
    transition_outbox_state,
)
from chejin_worker_client.image_phase import (
    mark_image_action,
    mark_image_ui_frame_invalidated,
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
    def test_client_update_contract_keeps_business_and_program_state_separate(self):
        contract = c2_contract_v3()["client_update_contract"]
        self.assertEqual(contract["release"], "0.9.66")
        self.assertEqual(contract["trigger"], "manual_settings_check_only")
        self.assertEqual(contract["query_binding_requirement"], "none")
        self.assertIn(
            "artifact_storage_key",
            contract["backend_release_record_immutability"],
        )
        self.assertNotIn(
            "artifact_storage_key",
            contract["client_download_identity"],
        )
        self.assertIn("append_only_audit_row", contract["download_lease_rule"])
        self.assertIn("identity_fields_match", contract["download_lease_renewal_rule"])
        self.assertIn("existing_leases_fail_closed", contract["withdrawal_rule"])
        self.assertIn("inflight_flow", contract["installation_blockers"])
        self.assertIn("action_journal", contract["installation_blockers"])
        self.assertIn("ed25519_release_manifest_signature", contract["package_verification_order"])
        self.assertIn("symbolic_link", contract["forbidden_archive_entries"])
        self.assertEqual(contract["previous_retention_count"], 1)
        self.assertIn("no_later_operator_pause_or_fault", contract["state_restore_rule"])
        self.assertIn("must_remain_faulted", contract["faulted_installation_rule"])

    def test_observability_inventory_has_one_authority_and_exact_dispositions(self):
        contract = c2_contract_v3()["observability_contract"]
        self.assertEqual(contract["release"], "0.9.58")
        self.assertEqual(
            contract["authority"],
            "backend_process_stage_runs_and_api_obs_02",
        )
        self.assertIn(
            "before_sidecar_process_launch",
            contract["reply_send_standard_timing_boundary"],
        )
        self.assertIn(
            "internal_diagnostic_only",
            contract["reply_send_sidecar_timing_role"],
        )
        self.assertIn(
            "quarantined_and_never_retried",
            contract["permanent_delivery_failure_rule"],
        )
        self.assertEqual(
            contract["worker_buffer_limits"]["max_total_storage_bytes"],
            64 * 1024 * 1024,
        )
        self.assertEqual(
            contract["worker_buffer_limits"]["max_process_links"],
            5000,
        )
        self.assertEqual(
            contract["worker_buffer_limits"]["max_stage_attempt_rows"],
            5000,
        )
        self.assertIn(
            "distinct_stage_run_id",
            contract["stage_attempt_identity_rule"],
        )
        self.assertEqual(
            contract["business_neutrality_direct_comparison_scenarios"][0],
            "c0_lead_received_and_auto_assignment",
        )
        self.assertIn(
            "backend_computed",
            contract["backend_authority_snapshot_rule"],
        )
        inventory = contract["timing_source_inventory"]
        sources = [item["source"] for item in inventory]
        self.assertEqual(len(sources), len(set(sources)))
        self.assertEqual(
            {item["disposition"] for item in inventory},
            {
                "keep_source",
                "map_once",
                "delete_duplicate",
                "history_read_only",
            },
        )
        for item in inventory:
            self.assertTrue(item["writers"])
            self.assertTrue(item["readers"])
            self.assertTrue(item["standard_stage"])
            self.assertIsInstance(item["diagnostic_use"], bool)

    def test_private_multiline_text_grouping_has_one_owner_and_no_cross_layer_merge(self):
        contract = c2_contract_v3()[
            "private_multiline_text_grouping_contract"
        ]
        self.assertEqual(contract["release"], "0.9.57")
        self.assertEqual(contract["owner"], "omniauto_sidecar")
        self.assertTrue(contract["explicit_avatar_required"])
        self.assertEqual(contract["group_chat_behavior"], "forbidden")
        self.assertEqual(
            contract["continuation_function"],
            "message_line_continues_anchored_text_bubble",
        )
        self.assertIn("single_v0_9_56", contract["continuation_rule_owner"])
        self.assertIn("left_edge", contract["continuation_edge_rule"])
        self.assertIn(
            "diagnostic_only",
            contract["weak_geometry_role_behavior"],
        )
        self.assertIn(
            "without_second_line_grouping",
            contract["worker_behavior"],
        )
        self.assertIn(
            "without_second_line_grouping",
            contract["backend_behavior"],
        )
        self.assertIn(
            "worker_compensating_line_merge",
            contract["forbidden_shortcuts"],
        )
        self.assertIn(
            "backend_compensating_line_merge",
            contract["forbidden_shortcuts"],
        )

    def test_unread_generation_contract_has_bounded_reread_terminal(self):
        unread = c2_contract_v3()["unread_generation_contract"]
        terminals = c2_contract_v3()["runtime_control_contract"][
            "finish_request"
        ]["terminal_kinds"]
        self.assertIn("retry_required", terminals)
        self.assertIn("technical_failed", terminals)
        self.assertEqual(unread["inconclusive_max_authoritative_reads"], 2)
        self.assertEqual(
            unread["inconclusive_terminal_result"],
            "technical_failed",
        )
        self.assertEqual(
            unread["inconclusive_terminal_error_code"],
            "C2_UNREAD_RESULT_REPEATEDLY_INCONCLUSIVE",
        )
        self.assertIn(
            "complete_no_change",
            unread["completion_rule"],
        )
        self.assertIn(
            "must_not_add_the_worker_local_failure_or_success_cooldown",
            unread["worker_retry_cooldown_rule"],
        )
        self.assertIn(
            "must_not_preempt_that_state_machine",
            unread["bounded_identity_recovery_rule"],
        )
        for field in (
            "tail_complete",
            "send_context_guard",
            "business_projection",
            "observation_validation_errors",
            "history_gap",
        ):
            self.assertIn(field, c2_contract_v3()["optional_evidence_fields"])

    def test_sequence_alignment_rejects_uat_shallow_business_continuity_shape(self):
        """The 0.9.45 UAT payload reached HTTP with 21 nested errors."""

        with self.assertRaisesRegex(
            ValueError,
            "C2_SEQUENCE_ALIGNMENT_EVIDENCE_INVALID",
        ):
            validate_sequence_alignment_evidence(
                {
                    "pre_sequence_source": (
                        "worker_business_viewport_continuity"
                    ),
                    "pre_frame_id": "",
                    "post_frame_id": "",
                    "alignment_status": "unique",
                    "candidate_alignment_count": 1,
                    "matched_pairs": [
                        {"old_index": 0, "new_index": 0},
                        {"old_index": 1, "new_index": 1},
                        {"old_index": 2, "new_index": 2},
                    ],
                    "old_tail_fully_consumed": True,
                    "new_suffix_observation_ids": [],
                }
            )

    def test_sequence_alignment_validates_every_text_voice_image_pair(self):
        evidence = validate_sequence_alignment_evidence(
            {
                "pre_sequence_source": "action_frame",
                "pre_frame_id": "frame:mixed-before",
                "post_frame_id": "frame:mixed-after",
                "alignment_status": "unique",
                "candidate_alignment_count": 1,
                "matched_pairs": [
                    {
                        "identity_state": "committed",
                        "worker_stable_id": "worker-message-1",
                        "pre_observation_id": "text-before",
                        "post_observation_id": "text-after",
                        "pre_index": 0,
                        "post_index": 0,
                        "match_basis": "worker_business_viewport_continuity",
                    },
                    {
                        "identity_state": "selected_action",
                        "worker_stable_id": "worker-message-2",
                        "pre_observation_id": "voice-before",
                        "post_observation_id": "voice-after",
                        "pre_index": 1,
                        "post_index": 1,
                        "match_basis": "confirmed_action",
                    },
                    {
                        "identity_state": "committed",
                        "worker_stable_id": "worker-message-3",
                        "pre_observation_id": "image-before",
                        "post_observation_id": "image-after",
                        "pre_index": 2,
                        "post_index": 2,
                        "match_basis": "prior_confirmed_action",
                    },
                ],
                "old_tail_fully_consumed": True,
                "new_suffix_observation_ids": ["text-new-tail"],
            }
        )

        self.assertEqual(len(evidence["matched_pairs"]), 3)
        broken = json.loads(json.dumps(evidence))
        del broken["matched_pairs"][1]["post_observation_id"]
        with self.assertRaisesRegex(
            ValueError,
            "C2_SEQUENCE_ALIGNMENT_PAIR_INVALID",
        ):
            validate_sequence_alignment_evidence(broken)

    def test_worker_projection_surface_exposes_business_rules_only(self):
        self.assertTrue(
            callable(worker_projection.normalized_business_message_sequence)
        )
        self.assertFalse(
            hasattr(worker_projection, "normalized_message_viewport_sequence")
        )

    def _real_legacy_sidecar_identity_fixture(self):
        omniauto_root = Path(__file__).resolve().parents[1] / "omniauto-rpa"
        if str(omniauto_root) not in sys.path:
            sys.path.insert(0, str(omniauto_root))

        from apps.wechat_ai_customer_service.adapters import (  # noqa: PLC0415
            wechat_win32_ocr_sidecar as sidecar,
        )
        from apps.wechat_ai_customer_service.wechat_message_envelope import (  # noqa: PLC0415
            apply_message_envelope_to_record,
            build_message_envelope,
        )

        record = {
            "id": "win32_ocr:real-envelope-text",
            "type": "text",
            "sender_role": "customer",
            "content": "测试消息",
            "source_adapter": "win32_ocr",
            "bubble_rect": {
                "left": 100,
                "top": 100,
                "right": 240,
                "bottom": 145,
            },
        }
        envelope = build_message_envelope(
            record,
            source_adapter="win32_ocr",
            conversation={
                "target_name": "CJTEST01",
                "conversation_type": "private",
            },
            ocr_items=[],
            bubble_rect=record["bubble_rect"],
        )
        legacy_message = apply_message_envelope_to_record(record, envelope)
        observations = sidecar.build_message_observations_v3(
            [legacy_message]
        )
        raw_payload = {
            "ok": True,
            "observation_schema_version": (
                sidecar.C2_OBSERVATION_SCHEMA_VERSION
            ),
            "messages": [legacy_message],
            "observations": observations,
            "frame_action_binding": {
                "reserved_worker_stable_id": "worker-message-1",
            },
        }
        return sidecar, legacy_message, raw_payload

    def _assert_public_sidecar_identity_payload(self, payload):
        self.assertNotIn("source_message_key", payload["messages"][0])
        self.assertNotIn(
            "source_message_key",
            payload["messages"][0]["message_envelope"],
        )
        self.assertEqual(
            payload["frame_action_binding"]["reserved_worker_stable_id"],
            "worker-message-1",
        )
        self.assertEqual(sidecar_contract_error(payload), "")

    def test_media_reservation_lifecycle_is_selected_action_only(self):
        lifecycle = c2_contract_v3()[
            "message_identity_lifecycle_contract"
        ]
        self.assertEqual(
            lifecycle["media_reservation_scope"],
            "selected_current_action_only",
        )
        self.assertEqual(
            lifecycle["unselected_media_state"],
            (
                "frame_local_unselected_without_action_id_reserved_id_or_"
                "action_journal"
            ),
        )
        self.assertTrue(lifecycle["bulk_media_reservation_forbidden"])
        self.assertEqual(
            lifecycle["media_ui_action_priority"],
            "voice_then_image",
        )
        self.assertEqual(
            lifecycle["new_voice_during_image_phase"],
            (
                "finish_current_image_terminal_then_return_to_voice_before_"
                "selecting_next_image"
            ),
        )
        self.assertEqual(
            lifecycle["final_ingest_order"],
            (
                "authoritative_final_frame_screen_order_independent_of_"
                "media_ui_action_order"
            ),
        )

    def test_sidecar_cannot_return_worker_owned_message_identity(self):
        base_observation = {
            "schema_version": 3,
            "observation_id": "win32_ocr:voice-1",
            "row_kind": "voice_bubble",
            "sender_role": "customer",
            "sender_role_source": "same_row_avatar",
            "message_type": "voice",
            "voice_state": "untranscribed",
            "source_message": {
                "id": "win32_ocr:voice-1",
                "source_adapter": "win32_ocr",
                "native_source_message_id": "",
            },
        }
        base_payload = {
            "observation_schema_version": 3,
            "observations": [base_observation],
        }
        self.assertEqual(sidecar_contract_error(base_payload), "")

        for field in c2_contract_v3()[
            "frame_action_binding_contract"
        ]["sidecar_must_not_return"]:
            with self.subTest(field=field, location="observation"):
                payload = {
                    **base_payload,
                    "observations": [
                        {**base_observation, field: "forbidden"}
                    ],
                }
                self.assertEqual(
                    sidecar_contract_error(payload),
                    "C2_SIDECAR_IDENTITY_CONTRACT_INVALID",
                )
            with self.subTest(field=field, location="source_message"):
                payload = {
                    **base_payload,
                    "observations": [
                        {
                            **base_observation,
                            "source_message": {
                                **base_observation["source_message"],
                                field: "forbidden",
                            },
                        }
                    ],
                }
                self.assertEqual(
                    sidecar_contract_error(payload),
                    "C2_SIDECAR_IDENTITY_CONTRACT_INVALID",
                )
            with self.subTest(field=field, location="selected_action"):
                payload = {
                    **base_payload,
                    "selected_voice_observation": {
                        "observation_id": "win32_ocr:voice-1",
                        field: "forbidden",
                    },
                }
                self.assertEqual(
                    sidecar_contract_error(payload),
                    "C2_SIDECAR_IDENTITY_CONTRACT_INVALID",
                )
            with self.subTest(field=field, location="legacy_messages"):
                payload = {
                    **base_payload,
                    "messages": [
                        {
                            "id": "legacy-message-1",
                            "message_envelope": {field: "forbidden"},
                        }
                    ],
                }
                self.assertEqual(
                    sidecar_contract_error(payload),
                    "C2_SIDECAR_IDENTITY_CONTRACT_INVALID",
                )

        allowed_action_evidence = {
            **base_observation,
            "frame_action_binding": {
                "reserved_worker_stable_id": "worker-message-1",
                "selected_action_token": "token-1",
            },
        }
        self.assertEqual(
            sidecar_contract_error(
                {**base_payload, "observations": [allowed_action_evidence]}
            ),
            "",
        )

    def test_real_legacy_message_envelope_is_sanitized_before_worker_contract(self):
        sidecar, legacy_message, raw_payload = (
            self._real_legacy_sidecar_identity_fixture()
        )

        self.assertIn("source_message_key", legacy_message)
        self.assertIn(
            "source_message_key",
            legacy_message["message_envelope"],
        )
        self.assertEqual(
            sidecar_contract_error(raw_payload),
            "C2_SIDECAR_IDENTITY_CONTRACT_INVALID",
        )

        with patch.object(sidecar, "run_action", return_value=raw_payload):
            public_payload = sidecar.run_sidecar_cli(["messages"])

        self._assert_public_sidecar_identity_payload(public_payload)

    def test_worker_rejects_duplicate_sidecar_observation_id(self):
        observation = {
            "schema_version": 3,
            "observation_id": "voice-duplicate-id",
            "row_kind": "voice_bubble",
            "sender_role": "customer",
            "sender_role_source": "same_row_avatar",
            "message_type": "voice",
            "voice_state": "untranscribed",
            "anchor_aliases": ["voice-structural:duplicate-id"],
        }
        self.assertEqual(
            sidecar_contract_error(
                {
                    "observation_schema_version": 3,
                    "observations": [observation, dict(observation)],
                }
            ),
            "C2_SIDECAR_IDENTITY_CONTRACT_INVALID",
        )

    def test_worker_rejects_duplicate_explicit_stable_voice_anchor(self):
        common = {
            "schema_version": 3,
            "row_kind": "voice_bubble",
            "sender_role": "customer",
            "sender_role_source": "same_row_avatar",
            "message_type": "voice",
            "voice_state": "untranscribed",
            "voice_anchor_structural_key": "voice-structural:shared",
        }
        self.assertEqual(
            sidecar_contract_error(
                {
                    "observation_schema_version": 3,
                    "observations": [
                        {
                            **common,
                            "observation_id": "voice-upper",
                            "voice_anchor_stable_key": "voice-stable:duplicate",
                        },
                        {
                            **common,
                            "observation_id": "voice-lower",
                            "source_message": {
                                "voice_anchor_stable_key": (
                                    "voice-stable:duplicate"
                                )
                            },
                        },
                    ],
                }
            ),
            "C2_SIDECAR_IDENTITY_CONTRACT_INVALID",
        )

    def test_daemon_entry_sanitizes_legacy_message_identity(self):
        sidecar, _, raw_payload = self._real_legacy_sidecar_identity_fixture()
        daemon_input = io.StringIO(
            '{"action":"messages"}\n{"action":"exit"}\n'
        )
        daemon_output = io.StringIO()

        with (
            patch.object(sidecar, "run_action", return_value=raw_payload),
            patch.object(sidecar.sys, "stdin", daemon_input),
            redirect_stdout(daemon_output),
        ):
            exit_code = sidecar.run_daemon_loop()

        output_lines = daemon_output.getvalue().splitlines()
        self.assertEqual(exit_code, 0)
        self.assertEqual(len(output_lines), 2)
        self._assert_public_sidecar_identity_payload(
            json.loads(output_lines[0])
        )
        self.assertEqual(
            json.loads(output_lines[1]),
            {"ok": True, "state": "daemon_exit"},
        )

    def test_legacy_main_entry_sanitizes_legacy_message_identity(self):
        sidecar, _, raw_payload = self._real_legacy_sidecar_identity_fixture()
        cli_output = io.StringIO()

        with (
            patch.object(sidecar, "run_action", return_value=raw_payload),
            patch.object(sidecar.sys, "argv", ["sidecar", "messages"]),
            redirect_stdout(cli_output),
        ):
            exit_code = sidecar.main()

        self.assertEqual(exit_code, 0)
        self._assert_public_sidecar_identity_payload(
            json.loads(cli_output.getvalue())
        )

    def test_frozen_windows_entry_sanitizes_legacy_message_identity(self):
        sidecar, _, raw_payload = self._real_legacy_sidecar_identity_fixture()
        from chejin_worker_client import main as frozen_entry  # noqa: PLC0415

        cli_output = io.StringIO()
        with (
            patch.object(sidecar, "run_action", return_value=raw_payload),
            patch.object(
                frozen_entry.sys,
                "_MEIPASS",
                tempfile.gettempdir(),
                create=True,
            ),
            redirect_stdout(cli_output),
        ):
            exit_code = frozen_entry.run_bundled_omniauto_sidecar(
                ["messages"]
            )

        self.assertEqual(exit_code, 0)
        self._assert_public_sidecar_identity_payload(
            json.loads(cli_output.getvalue())
        )

    def test_performance_fast_path_flags_can_be_disabled_independently(self):
        env_names = [
            "CHEJIN_TASK_SAFE_WAKE_ENABLED",
            "CHEJIN_C2_LOCATE_FRAME_REUSE_ENABLED",
            "CHEJIN_C3_PRE_SEND_ROI_REUSE_ENABLED",
            "CHEJIN_C3_SEND_FRAME_LOCAL_REUSE_ENABLED",
        ]
        attrs = [
            "task_safe_wake_enabled",
            "c2_locate_frame_reuse_enabled",
            "c3_pre_send_roi_reuse_enabled",
            "c3_send_frame_local_reuse_enabled",
        ]
        disabled = {name: "0" for name in env_names}
        with patch.dict(os.environ, disabled):
            config = ClientConfig.from_env()
        self.assertEqual([getattr(config, attr) for attr in attrs], [False] * 4)

        for selected_name, selected_attr in zip(env_names, attrs, strict=True):
            values = {**disabled, selected_name: "1"}
            with self.subTest(selected=selected_name), patch.dict(
                os.environ, values
            ):
                config = ClientConfig.from_env()
                self.assertTrue(getattr(config, selected_attr))
                self.assertEqual(
                    sum(bool(getattr(config, attr)) for attr in attrs),
                    1,
                )

    def test_slot_ledger_contract_separates_fact_scope_from_delivery(self):
        schema = c2_contract_v3()["slot_ledger_state_schema"]
        self.assertEqual(c2_contract_v3()["contract_revision"], "0.9.66")
        self.assertIn(
            "anchor_aliases",
            c2_contract_v3()["message_limits"][
                "observation_transport_fields"
            ],
        )
        viewport_contract = c2_contract_v3()[
            "pre_send_message_viewport_contract"
        ]
        self.assertTrue(viewport_contract["raw_rgb_hash_forbidden"])
        self.assertTrue(
            viewport_contract["full_screen_hash_fallback_forbidden"]
        )
        self.assertEqual(
            viewport_contract["maximum_full_reidentification_count"],
            1,
        )
        startup_layout = c2_contract_v3()["startup_layout_calibration_contract"]
        self.assertIn("full_calibrated_input_bounds", startup_layout["input_click_surface_rule"])
        self.assertIn("excludes_the_bottom_toolbar", startup_layout["input_text_detection_rule"])
        performance = c2_contract_v3()["performance_fast_path_contract"]
        self.assertTrue(performance["business_semantics_unchanged"])
        self.assertEqual(
            performance["flags"],
            [
                "CHEJIN_TASK_SAFE_WAKE_ENABLED",
                "CHEJIN_C2_LOCATE_FRAME_REUSE_ENABLED",
                "CHEJIN_C3_PRE_SEND_ROI_REUSE_ENABLED",
                "CHEJIN_C3_SEND_FRAME_LOCAL_REUSE_ENABLED",
            ],
        )
        self.assertEqual(
            set(performance["required_diagnostics"]),
            {
                "fast_path_attempted",
                "fast_path_used",
                "fallback_reason",
                "frame_digest_equal",
                "ocr_call_count",
                "ocr_total_duration_ms",
            },
        )
        self.assertIn("sidebar pixel digest", performance["locate_reuse_rule"])
        self.assertIn("0.9.10 full path", performance["fallback_rule"])
        self.assertIn(
            "exact persisted screenshot",
            performance["pre_send_same_frame_full_ocr_rule"],
        )
        self.assertIn(
            "decoded pixel sha256",
            performance["pre_send_same_frame_binding_rule"],
        )
        self.assertIn(
            "full-window OCR",
            performance["sidebar_search_safety_rule"],
        )
        self.assertIn(
            "controlled only by the three declared CHEJIN flags",
            performance["formal_flag_rule"],
        )
        legacy_recovery = c2_contract_v3()[
            "legacy_media_upgrade_recovery_contract"
        ]
        self.assertTrue(
            legacy_recovery[
                "applies_only_to_records_before_first_0_9_31_cutover"
            ]
        )
        self.assertEqual(
            legacy_recovery["settlement_endpoint"],
            "POST /api/workers/{worker_id}/legacy-media-recovery/settle",
        )
        self.assertIn(
            "silent_running_permanent_pull_block",
            legacy_recovery["forbidden_behaviors"],
        )
        self.assertIn(
            "backend terminal confirmation",
            legacy_recovery["backend_confirmation_rule"],
        )
        self.assertIn(
            "HTTP 4xx",
            legacy_recovery["permanent_failure_rule"],
        )
        self.assertIn(
            "manual_review_required",
            legacy_recovery["permanent_failure_rule"],
        )
        location_recovery = c2_contract_v3()[
            "target_location_recovery_contract"
        ]
        self.assertEqual(
            location_recovery["error_code"],
            "C2_VISIBLE_TARGET_STALE_AFTER_CLICK",
        )
        self.assertEqual(
            location_recovery["required_evidence_values"],
            location_recovery["example"]["targeting"][
                "stale_after_click"
            ],
        )
        quarantine = c2_contract_v3()["outbox_recovery_contract"][
            "state_machine"
        ]["state_properties"]["identity_quarantined"]
        self.assertFalse(quarantine["automatic_retry"])
        self.assertFalse(quarantine["blocks_new_ui_actions"])
        alignment = c2_contract_v3()["sequence_alignment_contract"]
        self.assertNotIn(
            "legacy_transition",
            c2_contract_v3()["message_identity_contract"],
        )
        self.assertEqual(alignment["owner"], "worker")
        self.assertEqual(
            alignment["strong_anchor_fields"],
            ["native_source_message_id", "confirmed_action_mapping"],
        )
        self.assertEqual(alignment["frame_visual_field"], "frame_visual_id")
        self.assertIn(
            "two_uniquely_aligned_historical_boundaries",
            alignment["weak_media_identity_rule"],
        )
        self.assertIn(
            "one_sided_text_or_system_context_never_proves_media_identity",
            alignment["one_sided_media_context_rule"],
        )
        self.assertIn(
            "frame_local",
            alignment["provisional_media_identity_rule"],
        )
        self.assertIn(
            "actual_result_of_the_current_single_physical_action",
            alignment["confirmed_action_continuity_rule"],
        )
        self.assertIn(
            "actual_image_bytes_sha256",
            alignment["confirmed_image_action_rule"],
        )
        self.assertIn(
            "persisted_complete_actual_action_receipt",
            alignment["image_identity_commit_gate"],
        )
        self.assertIn(
            "no_consumer_may_query_historical_ledger_or_outbox",
            alignment["image_identity_consumer_gate"],
        )
        self.assertIn(
            "technical_failed",
            alignment["image_identity_failure_behavior"],
        )
        self.assertIn(
            "zero_handoff",
            alignment["image_identity_failure_behavior"],
        )
        image_slot = alignment["image_flow_action_slot_contract"]
        self.assertEqual(image_slot["owner"], "worker")
        self.assertEqual(
            image_slot["required_fields"],
            [
                "read_run_id",
                "action_plan_revision",
                "selected_sequence_ordinal",
                "pre_action_business_projection_digest",
            ],
        )
        self.assertFalse(image_slot["durable_message_identity"])
        self.assertIn(
            "full_authoritative_frame",
            image_slot["post_action_reread_rule"],
        )
        self.assertIn(
            "compare_business_viewport_continuity",
            image_slot["continuity_rule"],
        )
        self.assertIn(
            "unique_viewport_slide_with_tail_append",
            image_slot["continuity_rule"],
        )
        self.assertNotIn(
            "head_slide",
            image_slot["forbidden_continuity_relations"],
        )
        self.assertIn("worker_faulted", image_slot["failure_rule"])
        self.assertEqual(
            c2_contract_v3()["pre_send_fact_checkpoint_contract"][
                "checkpoint_revision"
            ],
            5,
        )
        technical_gate = c2_contract_v3()["message_identity_contract"][
            "media_action_technical_failure_gate"
        ]
        self.assertEqual(
            set(technical_gate["error_codes"]),
            {
                "C2_IMAGE_IDENTITY_CONTRACT_INVALID",
                "C2_VOICE_IDENTITY_CONTRACT_INVALID",
                "C2_VOICE_RESULT_AMBIGUOUS",
            },
        )
        self.assertFalse(technical_gate["passive_reread_allowed"])
        self.assertFalse(technical_gate["handoff_allowed"])
        self.assertFalse(technical_gate["feishu_allowed"])
        self.assertIn(
            "without_restoring_unproven_weak_media",
            alignment["recent_ai_boundary_rule"],
        )
        self.assertEqual(
            alignment["new_suffix_rule"],
            "only_after_unique_alignment_consumes_pre_tail",
        )
        self.assertIn(
            "voice_anchor_alias",
            alignment["forbidden_cross_action_identity_inputs"],
        )
        self.assertIn(
            "image_physical_anchor",
            alignment["forbidden_cross_action_identity_inputs"],
        )
        identity_contract = c2_contract_v3()["message_identity_contract"]
        self.assertEqual(
            identity_contract["ocr_cross_round_identity_field"],
            "worker_stable_id",
        )
        self.assertEqual(
            identity_contract["frame_local_action_inputs"],
            [
                "physical_anchor",
                "selected_action_token",
                "pre_frame_id",
                "selected_pre_observation_id",
                "selected_target_fingerprint",
                "candidate_group_count",
            ],
        )
        self.assertNotIn(
            "physical_anchor",
            identity_contract["omniauto_inputs"],
        )
        source_fields = c2_contract_v3()["message_limits"][
            "source_message_transport_fields"
        ]
        self.assertIn("frame_visual_id", source_fields)
        self.assertIn("native_source_message_id", source_fields)
        self.assertIn("voice_duration", source_fields)
        self.assertIn("quality_flags", source_fields)
        self.assertIn("avatar_alignment", source_fields)
        self.assertNotIn("source_message_key", source_fields)
        self.assertNotIn("canonical_visual_id", source_fields)
        self.assertNotIn("canonical_input_id", source_fields)
        for forbidden in (
            "frame_visual_id",
            "canonical_visual_id",
            "canonical_input_id",
            "voice_anchor_alias",
            "image_physical_anchor",
            "message_body_alone",
            "same_type_occurrence_index",
        ):
            self.assertIn(
                forbidden,
                identity_contract["forbidden_identity_inputs"],
            )
        voice_binding = c2_contract_v3()[
            "voice_action_binding_contract"
        ]
        self.assertEqual(voice_binding["owner"], "omniauto")
        self.assertEqual(
            voice_binding["confirmed_candidate_count"], 1
        )
        self.assertEqual(
            voice_binding["confirmed_methods"],
            ["actual_action_result"],
        )
        for field in (
            "voice_action_stage",
            "canonical_voice_action_id",
            "reserved_worker_stable_id",
            "pre_frame_id",
            "post_frame_id",
            "selected_pre_observation_id",
            "selected_action_token",
            "selected_target_fingerprint",
            "tracking_frame_ids",
            "tracking_edges",
            "action_result_receipt",
            "confirmed_action_mapping",
        ):
            self.assertIn(
                field,
                voice_binding["required_response_fields"],
            )
        self.assertIn(
            "one_physical_action_count",
            voice_binding["actual_action_result_rule"],
        )
        self.assertIn(
            "slot_ledger_states",
            c2_contract_v3()["required_evidence_fields"],
        )
        self.assertIn(
            "sequence_alignment_evidence",
            c2_contract_v3()["required_evidence_fields"],
        )
        self.assertNotIn(
            "slot_ledger_states",
            c2_contract_v3()["optional_evidence_fields"],
        )
        self.assertEqual(
            set(schema["required_fields"]),
            {
                "observation_id",
                "screen_order",
                "order_source",
                "row_kind",
                "source_message_key",
                "origin_read_run_id",
                "fact_scope",
                "delivery_state",
                "item_state",
            },
        )
        states = validate_slot_ledger_states(
            [
                {
                    "observation_id": "image-1",
                    "screen_order": 1,
                    "order_source": "visual_top",
                    "row_kind": "image_bubble",
                    "source_message_key": "source:image-1",
                    "origin_read_run_id": "read-current",
                    "fact_scope": "current_read_run",
                    "delivery_state": "outbox_waiting",
                    "item_state": "completed",
                    "ledger_state": "OUTBOX_WAITING",
                }
            ],
            read_run_id="read-current",
        )
        self.assertEqual(states[0]["fact_scope"], "current_read_run")
        not_attempted_rule = c2_contract_v3()[
            "image_transaction_recovery_contract"
        ]["not_attempted_rule"]
        for required_guard in (
            "no terminal payload",
            "no completed or failed ledger fact",
            "no corresponding outbox record",
            "regardless of action_phase",
        ):
            self.assertIn(required_guard, not_attempted_rule)
        with self.assertRaisesRegex(
            ValueError,
            "C2_SLOT_LEDGER_CURRENT_ORIGIN_MISMATCH",
        ):
            validate_slot_ledger_states(
                [{**states[0], "origin_read_run_id": "read-other"}],
                read_run_id="read-current",
            )

    def test_flow_gate_actions_match_backend_orchestration(self):
        contract = c2_contract_v3()["flow_gate_action_contract"]
        self.assertEqual(
            contract["classes"],
            [
                "non_blocking_warning",
                "item_handoff",
                "recoverable_hold",
                "hard_stop",
            ],
        )
        self.assertEqual(
            contract["self_media_failure"],
            "persist_warning_and_continue_latest_complete_customer_tail",
        )
        self.assertEqual(
            contract["high_intent_reason_code"],
            "CUSTOMER_HIGH_INTENT",
        )

    def test_outbox_recovery_uses_only_backend_action(self):
        for recovery_action in (
            "retry",
            "refresh_and_rebuild",
            "split_and_retry",
            "capability_paused",
            "identity_quarantined",
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

    def test_identity_collision_is_quarantined_without_rekeying_outbox(self):
        recovery = c2_contract_v3()["outbox_recovery_contract"]
        self.assertTrue(
            {
                "MESSAGE_IDENTITY_COLLISION",
                "MESSAGE_IDENTITY_COLLISION_NOT_REKEYABLE",
                "VOICE_TRANSCRIBE_INVALID_CONTENT",
                "IMAGE_UNDERSTANDING_EVIDENCE_MISMATCH",
            }.issubset(recovery["identity_quarantined_codes"]),
        )
        self.assertNotIn("refresh_identity_and_retry", recovery["actions"])
        self.assertNotIn("rebuild_failed_facts", recovery["actions"])
        self.assertEqual(
            classify_outbox_recovery("identity_quarantined"),
            "identity_quarantined",
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
            "C2_IMAGE_MENU_OPERATION_FAILED": (
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

    def test_any_image_ui_action_requires_chat_refresh(self):
        result = new_image_phase_result()
        mark_image_action(result, "invalid-image")
        mark_image_ui_frame_invalidated(result, "invalid-image")
        mark_image_terminal(
            result,
            "invalid-image",
            terminal_state="failed",
        )

        self.assertEqual(result["new_action_count"], 1)
        self.assertEqual(result["failed"], 1)
        self.assertTrue(result["ui_frame_invalidated"])
        self.assertTrue(result["requires_final_refresh"])
        self.assertEqual(result["refresh_source_keys"], ["invalid-image"])

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
