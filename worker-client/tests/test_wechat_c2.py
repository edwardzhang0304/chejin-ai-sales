from __future__ import annotations

import inspect
import json
import unittest

import chejin_worker_client.wechat_c2 as wechat_c2_module
from chejin_worker_client.c2_contract import contract_revision, contract_sha256
from chejin_worker_client.models import WechatReadTarget
from chejin_worker_client.wechat_c2 import (
    build_flow_gate_ingest_payload,
    build_message_ingest_payload as _build_message_ingest_payload_v3,
    build_scan_result_payload,
    is_formal_c2_remark_code,
    message_dedupe_metadata,
    message_type,
    observation_alignment_signature,
    reconcile_cross_round_observation_identities,
    reconcile_v16104_identity_transition,
    sender_role_hint,
    voice_observation_anchor_key,
    voice_observation_source_key,
)
from tests.contract_artifacts import resolve_contract_artifact

def build_v3_message_ingest_payload(target: WechatReadTarget, sidecar_payload: dict) -> dict:
    return _build_message_ingest_payload_v3(
        target,
        {
            "contract_version": 3,
            "contract_revision": contract_revision(),
            "contract_sha256": contract_sha256(),
            "observation_schema_version": 3,
            "authoritative_frame_source": "final_read",
            **sidecar_payload,
        },
    )


def sidecar_session_identity(
    *,
    name: str,
    session_key: str,
    conversation_type: str,
    allowed: bool,
    code: str = "",
    reason: str = "sidecar_test_decision",
    **extra,
) -> dict:
    return {
        "name": name,
        "session_key": session_key,
        "c2_remark_code_candidates": [code] if allowed and code else [],
        "c2_conversation_admission": {
            "conversation_type": conversation_type,
            "admission_allowed": allowed,
            "remark_code": code,
            "reason": reason,
        },
        **extra,
    }


class WechatC2Test(unittest.TestCase):
    def test_worker_identity_module_cannot_reintroduce_title_reclassification(self):
        source = inspect.getsource(wechat_c2_module)

        self.assertNotIn("raw_title", source)
        self.assertNotIn("classify_c2_conversation_title", source)
        self.assertNotIn("extract_c2_remark_codes", source)
        self.assertNotIn("omniauto-rpa", source)

    def test_voice_structural_and_stable_anchors_are_one_physical_identity(self):
        target = WechatReadTarget(
            conversation_id="conv-voice-alias",
            rpa_session_key="wx:rpa:v1:voice-alias",
            display_name="CJALIAS1",
            remark_code="CJALIAS1",
        )
        bubble = {
            "voice_anchor_stable_key": "voice-stable-alias",
            "source_message": {
                "voice_anchor_structural_key": "voice-structural-canonical",
                "voice_anchor_stable_key": "voice-stable-alias",
            },
        }
        transcript = {
            "parent_voice_anchor_key": "voice-stable-alias",
            "source_message": {
                "voice_anchor_structural_key": "voice-structural-canonical",
                "voice_anchor_stable_key": "voice-stable-alias",
            },
        }

        self.assertEqual(
            voice_observation_anchor_key(bubble),
            "voice-structural-canonical",
        )
        self.assertEqual(
            voice_observation_source_key(target, bubble),
            voice_observation_source_key(target, transcript),
        )

    @staticmethod
    def _identity_text(observation_id: str, content: str, top: int) -> dict:
        return {
            "schema_version": 3,
            "observation_id": observation_id,
            "row_kind": "text_bubble",
            "sender_role": "customer",
            "sender_role_source": "same_row_avatar",
            "message_type": "text",
            "voice_state": "not_voice",
            "content_clean": content,
            "bubble_rect": [420, top, 650, top + 42],
            "source_message": {
                "id": observation_id,
                "source_adapter": "win32_ocr",
                "type": "text",
                "sender_role": "customer",
                "content": content,
            },
        }

    @staticmethod
    def _identity_image(
        observation_id: str,
        top: int,
        *,
        occurrence_index: int,
        occurrence_count: int,
        fingerprint: str = "dhash64:1234567890abcdef",
    ) -> dict:
        anchor = {
            "sender_role": "customer",
            "preceding_stable_message": "",
            "following_stable_message": "",
            "bubble_visual_fingerprint": fingerprint,
            "occurrence_index": occurrence_index,
            "occurrence_count": occurrence_count,
        }
        return {
            "schema_version": 3,
            "observation_id": observation_id,
            "row_kind": "image_bubble",
            "sender_role": "customer",
            "sender_role_source": "same_row_avatar",
            "message_type": "image",
            "voice_state": "not_voice",
            "content_clean": "",
            "bubble_rect": [420, top, 650, top + 120],
            "image_physical_anchor": anchor,
            "source_message": {
                "id": observation_id,
                "source_adapter": "win32_ocr",
                "type": "image",
                "sender_role": "customer",
                "image_physical_anchor": anchor,
            },
        }

    def test_server_checkpoint_recovers_sequence_after_local_state_loss(self):
        historical = self._identity_text("historical", "历史消息", 180)
        target = WechatReadTarget(
            conversation_id="conv-checkpoint",
            rpa_session_key="wx:checkpoint",
            display_name="张三-CJCP0001",
            remark_code="CJCP0001",
            authorization_revision="rev-checkpoint",
            raw={
                "identity_checkpoint": {
                    "version": 1,
                    "next_sequence_floor": 8,
                    "recent_messages": [
                        {
                            "stable_id": "worker-message-7",
                            "alignment_signature": (
                                observation_alignment_signature(historical)
                            ),
                        }
                    ],
                }
            },
        )

        reconciled, state, errors = reconcile_v16104_identity_transition(
            target,
            [
                historical,
                self._identity_text("new", "新消息", 260),
            ],
            None,
        )

        self.assertEqual(errors, [])
        self.assertEqual(
            reconciled[0]["_worker_stable_id"],
            "worker-message-7",
        )
        self.assertEqual(
            reconciled[1]["_worker_stable_id"],
            "worker-message-8",
        )
        self.assertEqual(state["next_sequence"], 9)

    def test_cross_round_identity_survives_page_shift(self):
        first, state, errors = reconcile_cross_round_observation_identities(
            [
                self._identity_text("frame-1-a", "您好", 180),
                self._identity_text("frame-1-b", "好的", 260),
            ]
        )
        shifted, _, shifted_errors = reconcile_cross_round_observation_identities(
            [
                self._identity_text("frame-2-a", "您好", 420),
                self._identity_text("frame-2-b", "好的", 500),
            ],
            state,
        )

        self.assertEqual(errors, [])
        self.assertEqual(shifted_errors, [])
        self.assertEqual(
            [item["_worker_stable_id"] for item in first],
            [item["_worker_stable_id"] for item in shifted],
        )

    def test_cross_round_identity_distinguishes_new_repeated_text(self):
        first, state, _ = reconcile_cross_round_observation_identities(
            [self._identity_text("old-ok", "好的", 180)]
        )
        second, _, errors = reconcile_cross_round_observation_identities(
            [
                self._identity_text("old-ok-shifted", "好的", 360),
                self._identity_text("new-ok", "好的", 440),
            ],
            state,
        )

        self.assertEqual(errors, [])
        self.assertEqual(first[0]["_worker_stable_id"], second[0]["_worker_stable_id"])
        self.assertNotEqual(second[0]["_worker_stable_id"], second[1]["_worker_stable_id"])

    def test_cross_round_identity_blocks_fully_identical_sequence(self):
        _, state, _ = reconcile_cross_round_observation_identities(
            [
                self._identity_text("old-ok-1", "好的", 180),
                self._identity_text("old-ok-2", "好的", 260),
            ]
        )

        current, blocked_state, errors = reconcile_cross_round_observation_identities(
            [
                self._identity_text("current-ok-1", "好的", 180),
                self._identity_text("current-ok-2", "好的", 260),
            ],
            state,
        )

        self.assertEqual(errors[0]["error_code"], "MESSAGE_CROSS_ROUND_IDENTITY_AMBIGUOUS")
        self.assertEqual(blocked_state, state)
        self.assertTrue(all("_worker_stable_id" not in item for item in current))

    def test_cross_round_identity_aligns_previous_suffix_for_repeated_text(self):
        first, state, errors = reconcile_cross_round_observation_identities(
            [
                self._identity_text("old-a-1", "A", 180),
                self._identity_text("old-a-2", "A", 260),
                self._identity_text("old-b", "B", 340),
            ]
        )
        current, _, current_errors = (
            reconcile_cross_round_observation_identities(
                [
                    self._identity_text("current-a", "A", 180),
                    self._identity_text("current-b", "B", 260),
                    self._identity_text("current-c", "C", 340),
                ],
                state,
            )
        )

        self.assertEqual(errors, [])
        self.assertEqual(current_errors, [])
        self.assertEqual(
            current[0]["_worker_stable_id"],
            first[1]["_worker_stable_id"],
        )
        self.assertEqual(
            current[1]["_worker_stable_id"],
            first[2]["_worker_stable_id"],
        )
        self.assertNotIn(
            current[2]["_worker_stable_id"],
            {
                first[0]["_worker_stable_id"],
                first[1]["_worker_stable_id"],
                first[2]["_worker_stable_id"],
            },
        )

    def test_cross_round_identity_aligns_repeated_image_after_occurrence_reset(self):
        first, state, errors = reconcile_cross_round_observation_identities(
            [
                self._identity_image(
                    "old-image-1",
                    180,
                    occurrence_index=0,
                    occurrence_count=2,
                ),
                self._identity_image(
                    "old-image-2",
                    340,
                    occurrence_index=1,
                    occurrence_count=2,
                ),
                self._identity_text("old-b", "B", 500),
            ]
        )
        current, _, current_errors = (
            reconcile_cross_round_observation_identities(
                [
                    self._identity_image(
                        "current-image",
                        180,
                        occurrence_index=0,
                        occurrence_count=1,
                    ),
                    self._identity_text("current-b", "B", 340),
                    self._identity_text("current-c", "C", 420),
                ],
                state,
            )
        )

        self.assertEqual(errors, [])
        self.assertEqual(current_errors, [])
        self.assertEqual(
            current[0]["_worker_stable_id"],
            first[1]["_worker_stable_id"],
        )
        self.assertEqual(
            current[1]["_worker_stable_id"],
            first[2]["_worker_stable_id"],
        )

    def test_cross_round_identity_upgrades_v2_image_state_without_guessing(self):
        first, state, errors = reconcile_cross_round_observation_identities(
            [
                self._identity_image(
                    "old-image",
                    180,
                    occurrence_index=0,
                    occurrence_count=1,
                ),
                self._identity_text("old-b", "B", 340),
            ]
        )
        v2_state = {
            **state,
            "version": 2,
            "last_frame": [
                {
                    "signature": item["signature"],
                    "stable_id": item["stable_id"],
                }
                for item in state["last_frame"]
            ],
            "recent_frames": [],
        }
        current, upgraded_state, current_errors = (
            reconcile_cross_round_observation_identities(
                [
                    self._identity_image(
                        "current-image",
                        420,
                        occurrence_index=0,
                        occurrence_count=1,
                    ),
                    self._identity_text("current-b", "B", 580),
                ],
                v2_state,
            )
        )

        self.assertEqual(errors, [])
        self.assertEqual(current_errors, [])
        self.assertEqual(upgraded_state["version"], 3)
        self.assertEqual(
            [item["_worker_stable_id"] for item in current],
            [item["_worker_stable_id"] for item in first],
        )
        self.assertTrue(
            all(
                item.get("alignment_signature")
                for item in upgraded_state["last_frame"]
            )
        )

    def test_v16104_transition_keeps_legacy_key_and_assigns_new_identity_only_to_new_suffix(self):
        old_observation = self._identity_text("legacy-old", "历史消息", 180)
        new_observation = self._identity_text("new-current", "本轮新消息", 260)
        target = WechatReadTarget(
            conversation_id="conv-upgrade",
            rpa_session_key="CJUnit01",
            display_name="张三-CJUnit01",
            remark_code="CJUnit01",
            authorization_revision="rev-upgrade",
        )
        legacy_source = {
            **old_observation,
            "content": old_observation["content_clean"],
            "type": old_observation["message_type"],
        }
        legacy_key, _, _ = message_dedupe_metadata(
            target,
            legacy_source,
            0,
            messages=[legacy_source],
        )
        target.raw = {
            "identity_transition": {
                "version": 1,
                "source_version": "v16.104",
                "legacy_messages": [{"dedupe_key": legacy_key}],
            }
        }

        transitioned, state, errors = reconcile_v16104_identity_transition(
            target,
            [old_observation, new_observation],
            None,
        )

        self.assertEqual(errors, [])
        self.assertEqual(transitioned[0]["_worker_legacy_dedupe_key"], legacy_key)
        self.assertNotIn("_worker_legacy_dedupe_key", transitioned[1])
        first_payload = build_v3_message_ingest_payload(
            target,
            {"observations": transitioned},
        )
        self.assertEqual(first_payload["messages"][0]["dedupe_key"], legacy_key)
        self.assertNotEqual(first_payload["messages"][1]["dedupe_key"], legacy_key)

        shifted, _, shifted_errors = reconcile_v16104_identity_transition(
            target,
            [
                self._identity_text("legacy-old-shifted", "历史消息", 420),
                self._identity_text("new-current-shifted", "本轮新消息", 500),
            ],
            state,
        )
        self.assertEqual(shifted_errors, [])
        self.assertEqual(shifted[0]["_worker_legacy_dedupe_key"], legacy_key)

    def test_v16104_transition_runs_when_backend_support_arrives_after_local_state(self):
        old_observation = self._identity_text("legacy-old", "历史消息", 180)
        target = WechatReadTarget(
            conversation_id="conv-delayed-upgrade",
            rpa_session_key="CJDELAY01",
            display_name="张三-CJDELAY01",
            remark_code="CJDELAY01",
            authorization_revision="rev-delayed",
        )

        first, local_state, first_errors = reconcile_v16104_identity_transition(
            target,
            [old_observation],
            None,
        )
        self.assertEqual(first_errors, [])
        self.assertTrue(first[0]["_worker_stable_id"])
        self.assertNotIn("legacy_transition_completed", local_state)

        legacy_source = {
            **old_observation,
            "content": old_observation["content_clean"],
            "type": old_observation["message_type"],
        }
        legacy_key, _, _ = message_dedupe_metadata(
            target,
            legacy_source,
            0,
            messages=[legacy_source],
        )
        target.raw = {
            "identity_transition": {
                "version": 1,
                "source_version": "v16.104",
                "legacy_messages": [{"dedupe_key": legacy_key}],
            }
        }

        migrated, migrated_state, migrated_errors = (
            reconcile_v16104_identity_transition(
                target,
                [self._identity_text("legacy-old-later", "历史消息", 420)],
                local_state,
            )
        )

        self.assertEqual(migrated_errors, [])
        self.assertEqual(migrated[0]["_worker_legacy_dedupe_key"], legacy_key)
        self.assertTrue(migrated_state["legacy_transition_completed"])

    def test_versioned_empty_transition_is_required_to_mark_migration_complete(self):
        target = WechatReadTarget(
            conversation_id="conv-empty-upgrade",
            rpa_session_key="CJEMPTY01",
            display_name="张三-CJEMPTY01",
            remark_code="CJEMPTY01",
            authorization_revision="rev-empty",
            raw={
                "identity_transition": {
                    "version": 1,
                    "source_version": "v16.104",
                    "legacy_messages": [],
                }
            },
        )

        _, state, errors = reconcile_v16104_identity_transition(
            target,
            [self._identity_text("new-only", "全新消息", 180)],
            None,
        )

        self.assertEqual(errors, [])
        self.assertTrue(state["legacy_transition_completed"])

    def test_cross_round_identity_blocks_guess_when_no_sequence_overlap_exists(self):
        _, state, _ = reconcile_cross_round_observation_identities(
            [
                self._identity_text("old-a", "第一条", 180),
                self._identity_text("old-ok", "好的", 260),
            ]
        )
        _, state, _ = reconcile_cross_round_observation_identities(
            [self._identity_text("new-context", "完全不同的上下文", 180)],
            state,
        )
        current, blocked_state, errors = reconcile_cross_round_observation_identities(
            [self._identity_text("ambiguous-ok", "好的", 180)],
            state,
        )

        self.assertNotIn("_worker_stable_id", current[0])
        self.assertEqual(errors[0]["error_code"], "MESSAGE_CROSS_ROUND_IDENTITY_AMBIGUOUS")
        self.assertEqual(blocked_state, state)
        recovered, _, recovered_errors = reconcile_cross_round_observation_identities(
            [
                self._identity_text("old-a-visible-again", "第一条", 180),
                self._identity_text("old-ok-visible-again", "好的", 260),
            ],
            blocked_state,
        )
        self.assertEqual(recovered_errors, [])
        self.assertTrue(all(item.get("_worker_stable_id") for item in recovered))

    def test_shared_mixed_roundtrip_fixture_is_translated_by_worker_in_screen_order(self):
        fixture_path = resolve_contract_artifact(
            "examples",
            "c2_v3_mixed_roundtrip.json",
        )
        fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
        target = WechatReadTarget(**fixture["target"])

        payload = build_v3_message_ingest_payload(target, fixture["omniauto_output"])

        actual = [
            {
                "screen_order": item["message_position"]["screen_order"],
                "message_type": item["message_type"],
                "sender_role_hint": item["sender_role_hint"],
            }
            for item in payload["messages"]
        ]
        self.assertEqual(actual, fixture["expected_messages"])
        self.assertEqual([item["message_type"] for item in payload["messages"]], ["text", "voice", "image"])
        encoded = json.dumps(payload, ensure_ascii=False).lower()
        for forbidden in ("image_local_path", "image_bytes", "data:image/", "original_image"):
            self.assertNotIn(forbidden, encoded)

    def test_worker_text_identity_ignores_sidecar_ids_coordinates_and_run_ids(self):
        target = WechatReadTarget(
            conversation_id="11111111-1111-1111-1111-111111111111",
            remark_code="CJIDENTITY01",
            rpa_session_key="wx:rpa:v1:identity",
            display_name="身份测试-CJIDENTITY01",
            authorization_revision="revision-1",
        )

        def payload(observation_id: str, source_id: str, top: int, run_id: str) -> dict:
            return build_v3_message_ingest_payload(
                target,
                {
                    "sidecar_run_id": run_id,
                    "authoritative_frame_source": "final_read",
                    "observations": [
                        {
                            "schema_version": 3,
                            "observation_id": observation_id,
                            "row_kind": "text_bubble",
                            "sender_role": "customer",
                            "sender_role_source": "same_row_avatar",
                            "message_type": "text",
                            "voice_state": "not_voice",
                            "content_clean": "同一条消息",
                            "bubble_rect": [420, top, 650, top + 42],
                            "source_message": {
                                "id": source_id,
                                "source_adapter": "win32_ocr",
                                "type": "text",
                                "sender_role": "customer",
                                "content": "同一条消息",
                            },
                        }
                    ],
                },
            )

        first = payload("observation-run-a", "win32_ocr:run-a", 180, "run-a")
        shifted = payload("observation-run-b", "win32_ocr:run-b", 460, "run-b")

        self.assertEqual(first["messages"][0]["dedupe_key"], shifted["messages"][0]["dedupe_key"])
        self.assertEqual(
            first["messages"][0]["source_message_key"],
            shifted["messages"][0]["source_message_key"],
        )

    def test_worker_identity_distinguishes_repeated_equal_text_by_occurrence(self):
        target = WechatReadTarget(
            conversation_id="11111111-1111-1111-1111-111111111112",
            remark_code="CJIDENTITY02",
            rpa_session_key="wx:rpa:v1:identity-two",
            display_name="身份测试-CJIDENTITY02",
            authorization_revision="revision-2",
        )
        observations = []
        for index, top in enumerate((180, 260), start=1):
            observations.append(
                {
                    "schema_version": 3,
                    "observation_id": f"equal-{index}",
                    "row_kind": "text_bubble",
                    "sender_role": "customer",
                    "sender_role_source": "same_row_avatar",
                    "message_type": "text",
                    "voice_state": "not_voice",
                    "content_clean": "好的",
                    "bubble_rect": [420, top, 650, top + 42],
                    "source_message": {
                        "id": f"win32_ocr:equal-{index}",
                        "source_adapter": "win32_ocr",
                        "type": "text",
                        "sender_role": "customer",
                        "content": "好的",
                    },
                }
            )
        result = build_v3_message_ingest_payload(
            target,
            {
                "authoritative_frame_source": "final_read",
                "observations": observations,
            },
        )
        self.assertEqual(len(result["messages"]), 2)
        self.assertNotEqual(
            result["messages"][0]["source_message_key"],
            result["messages"][1]["source_message_key"],
        )

    def test_scan_payload_maps_sessions_and_remark_codes(self):
        payload = build_scan_result_payload(
            {
                "ok": True,
                "adapter": "win32_ocr",
                "state": "sessions_ocr",
                "screenshot_path": "C:/scan.png",
                "sessions": [
                    sidecar_session_identity(
                        name="王先生 CJ8K2P34",
                        session_key="wx:rpa:v1:a",
                        conversation_type="private",
                        allowed=True,
                        code="CJ8K2P34",
                        row_fingerprint={"row": 1, "text": "王先生"},
                        content="你好",
                        unread_signal=True,
                        ocr_confidence=0.97,
                    )
                ],
            }
        )

        self.assertFalse(payload["scan_failed"])
        self.assertEqual(payload["sessions"][0]["rpa_session_key"], "wx:rpa:v1:a")
        self.assertEqual(payload["sessions"][0]["remark_code_candidates"], ["CJ8K2P34"])
        self.assertTrue(payload["sessions"][0]["unread_hint"])

    def test_scan_payload_admits_short_code_before_glued_time_suffix(self):
        payload = build_scan_result_payload(
            {
                "ok": True,
                "sessions": [
                    sidecar_session_identity(
                        name="CJR8S5K3虾丸子大",
                        raw_title="CJR8S5K3虾丸子大...11:05",
                        session_key="wx:rpa:v1:cjr8s5k3",
                        conversation_type="private",
                        allowed=True,
                        code="CJR8S5K3",
                    )
                ],
            }
        )

        self.assertEqual(
            payload["sessions"][0]["remark_code_candidates"],
            ["CJR8S5K3"],
        )
        self.assertEqual(
            payload["evidence"]["c2_conversation_admission"][
                "private_candidate_count"
            ],
            1,
        )

    def test_scan_payload_excludes_session_without_sidecar_identity(self):
        payload = build_scan_result_payload(
            {
                "ok": True,
                "sessions": [
                    {
                        "name": "张三-CJSAFE01",
                        "raw_title": "张三-CJSAFE01",
                    }
                ],
            }
        )

        self.assertEqual(payload["sessions"], [])
        admission = payload["evidence"]["c2_conversation_admission"]
        self.assertEqual(admission["missing_session_key_excluded_count"], 1)
        self.assertEqual(admission["contract_rejected_count"], 1)
        self.assertEqual(
            admission["contract_rejections"][0]["reason"],
            "session_key_missing",
        )

    def test_worker_does_not_guess_message_type_or_sender_role_from_legacy_aliases(self):
        self.assertEqual(message_type({"voice_duration": 3}), "unknown")
        self.assertEqual(message_type({"type": "audio"}), "unknown")
        self.assertEqual(message_type({"content": "普通正文"}), "unknown")
        self.assertEqual(sender_role_hint({"sender_role": "sales"}), "unknown")
        self.assertEqual(sender_role_hint({"sender_role": "contact"}), "unknown")

    def test_worker_rejects_contradictory_sidecar_identity_without_reclassifying(self):
        payload = build_scan_result_payload(
            {
                "ok": True,
                "sessions": [
                    {
                        "name": "CJP6M3R7许聪",
                        "session_key": "wx:rpa:v1:contract-conflict",
                        "c2_remark_code_candidates": ["CJP6M3R7"],
                        "c2_conversation_admission": {
                            "conversation_type": "unknown",
                            "reason": "contradictory_test_fixture",
                            "admission_allowed": True,
                            "remark_code": "CJP6M3R7",
                        },
                    }
                ],
            }
        )

        self.assertEqual(payload["sessions"][0]["remark_code_candidates"], [])
        admission = payload["evidence"]["c2_conversation_admission"]
        self.assertEqual(admission["private_candidate_count"], 0)
        self.assertEqual(admission["unknown_excluded_count"], 1)
        self.assertEqual(admission["contract_rejected_count"], 1)
        self.assertEqual(
            admission["contract_rejections"][0]["reason"],
            "allowed_identity_not_private",
        )

    def test_worker_does_not_reopen_sidecar_unknown_from_raw_title(self):
        payload = build_scan_result_payload(
            {
                "ok": True,
                "sessions": [
                    sidecar_session_identity(
                        name="CJP6M3R7许聪",
                        raw_title="CJP6M3R7许聪",
                        session_key="wx:rpa:v1:sidecar-unknown",
                        conversation_type="unknown",
                        allowed=False,
                        reason="sidecar_could_not_confirm_private_title",
                    )
                ],
            }
        )

        self.assertEqual(payload["sessions"][0]["remark_code_candidates"], [])
        admission = payload["evidence"]["c2_conversation_admission"]
        self.assertEqual(admission["unknown_excluded_count"], 1)
        self.assertEqual(admission["contract_rejected_count"], 0)

    def test_worker_copies_sidecar_code_without_reading_conflicting_raw_title(self):
        payload = build_scan_result_payload(
            {
                "ok": True,
                "sessions": [
                    sidecar_session_identity(
                        name="展示名-CJP6M3R7",
                        raw_title="标题噪声-CJFAKE23",
                        session_key="wx:rpa:v1:sidecar-authoritative",
                        conversation_type="private",
                        allowed=True,
                        code="CJP6M3R7",
                        reason="formal_code_confirmed_in_title",
                    )
                ],
            }
        )

        self.assertEqual(
            payload["sessions"][0]["remark_code_candidates"],
            ["CJP6M3R7"],
        )

    def test_worker_rejects_missing_or_invalid_sidecar_identity_contract(self):
        payload = build_scan_result_payload(
            {
                "ok": True,
                "sessions": [
                    {"name": "缺少合同", "session_key": "missing"},
                    {
                        "name": "短码非八位",
                        "session_key": "invalid-code",
                        "c2_remark_code_candidates": ["CJ123"],
                        "c2_conversation_admission": {
                            "conversation_type": "private",
                            "admission_allowed": True,
                            "remark_code": "CJ123",
                            "reason": "invalid_code_fixture",
                        },
                    },
                    {
                        "name": "短码非规范大写",
                        "session_key": "lowercase-code",
                        "c2_remark_code_candidates": ["cjp6m3r7"],
                        "c2_conversation_admission": {
                            "conversation_type": "private",
                            "admission_allowed": True,
                            "remark_code": "cjp6m3r7",
                            "reason": "lowercase_code_fixture",
                        },
                    },
                    {
                        "name": "列表与准入短码不一致",
                        "session_key": "mismatched-code",
                        "c2_remark_code_candidates": ["CJP6M3R7"],
                        "c2_conversation_admission": {
                            "conversation_type": "private",
                            "admission_allowed": True,
                            "remark_code": "CJUAT728",
                            "reason": "mismatched_code_fixture",
                        },
                    },
                ],
            }
        )

        self.assertEqual(
            [row["remark_code_candidates"] for row in payload["sessions"]],
            [[], [], [], []],
        )
        admission = payload["evidence"]["c2_conversation_admission"]
        self.assertEqual(admission["contract_rejected_count"], 4)

    def test_scan_payload_excludes_group_from_same_short_code_conflict(self):
        payload = build_scan_result_payload(
            {
                "ok": True,
                "sessions": [
                    sidecar_session_identity(
                        name="张三-CJR8S5K3",
                        raw_title="张三-CJR8S5K3",
                        session_key="private",
                        conversation_type="private",
                        allowed=True,
                        code="CJR8S5K3",
                    ),
                    sidecar_session_identity(
                        name="销售讨论-CJR8S5K3(5)",
                        raw_title="销售讨论-CJR8S5K3(5)",
                        session_key="group",
                        conversation_type="group",
                        allowed=False,
                        code="CJR8S5K3",
                    ),
                ],
            }
        )

        self.assertEqual(payload["sessions"][0]["remark_code_candidates"], ["CJR8S5K3"])
        self.assertEqual(payload["sessions"][1]["remark_code_candidates"], [])
        self.assertEqual(payload["evidence"]["c2_conversation_admission"]["group_excluded_count"], 1)

    def test_scan_payload_admits_truncated_short_code_but_rejects_fuzzy_group_suffix(self):
        payload = build_scan_result_payload(
            {
                "ok": True,
                "sessions": [
                    sidecar_session_identity(
                        name="张三-CJR8S5K3…",
                        raw_title="张三-CJR8S5K3…",
                        session_key="ellipsis",
                        conversation_type="private",
                        allowed=True,
                        code="CJR8S5K3",
                    ),
                    sidecar_session_identity(
                        name="李四-CJR8S5K3(5",
                        raw_title="李四-CJR8S5K3(5",
                        session_key="fuzzy",
                        conversation_type="unknown",
                        allowed=False,
                        code="CJR8S5K3",
                    ),
                ],
            }
        )

        self.assertEqual(
            payload["sessions"][0]["remark_code_candidates"],
            ["CJR8S5K3"],
        )
        self.assertEqual(payload["sessions"][1]["remark_code_candidates"], [])
        admission = payload["evidence"]["c2_conversation_admission"]
        self.assertEqual(admission["private_candidate_count"], 1)
        self.assertEqual(admission["unknown_excluded_count"], 1)

    def test_worker_formal_remark_code_validation_is_format_only(self):
        self.assertTrue(is_formal_c2_remark_code("CJP6M3R7"))
        self.assertFalse(is_formal_c2_remark_code("cjtest01"))
        self.assertFalse(is_formal_c2_remark_code(" CJP6M3R7"))
        self.assertFalse(is_formal_c2_remark_code("CJ123"))
        self.assertFalse(is_formal_c2_remark_code("CJABCDEFG"))

    def test_v3_uses_final_observations_as_the_only_voice_message_source(self):
        target = WechatReadTarget(
            conversation_id="conv-1",
            rpa_session_key="wx:rpa:v1:a",
            display_name="CJR8S5K3 虾丸子大人",
            remark_code="CJR8S5K3",
            authorization_revision="rev-1",
        )
        payload = build_v3_message_ingest_payload(
            target,
            {
                "ok": True,
                "observation_schema_version": 3,
                "observations": [
                    {
                        "schema_version": 3,
                        "observation_id": "voice-transcript",
                        "row_kind": "voice_transcript",
                        "sender_role": "self",
                        "sender_role_source": "parent_voice",
                        "message_type": "voice",
                        "voice_state": "transcribed",
                        "content_clean": "我马上回去。",
                        "parent_voice_anchor_key": "self:4s:row-1",
                        "source_message": {
                            "id": "voice-transcript",
                            "type": "voice",
                            "content": "我马上回去。",
                            "voice_duration": 4,
                            "voice_anchor_stable_key": "self:4s:row-1",
                        },
                    },
                    {
                        "schema_version": 3,
                        "observation_id": "text-1",
                        "row_kind": "text_bubble",
                        "sender_role": "customer",
                        "sender_role_source": "same_row_avatar",
                        "message_type": "text",
                        "voice_state": "not_voice",
                        "content_clean": "普通文字",
                        "source_message": {"id": "text-1", "type": "text", "content": "普通文字"},
                    },
                ],
                "voice_transcription": {
                    "state": "voice_transcribe_completed",
                    "attempt_count": 1,
                    "quality_flags": [],
                    "transcribed_messages": [
                        {
                            "id": "voice-1",
                            "sender_role": "self",
                            "type": "voice",
                            "content": "我马上回去。",
                            "voice_duration": 4,
                            "voice_anchor_stable_key": "self:4s:row-1",
                        }
                    ],
                },
            },
        )

        self.assertEqual(payload["contract_version"], 3)
        self.assertEqual(payload["authorization_revision"], "rev-1")
        self.assertNotIn("sidecar_run_id", payload)
        self.assertIn("sidecar_run_id", payload["evidence"])
        self.assertEqual(
            [(item["message_type"], item["sender_role_hint"], item["content"]) for item in payload["messages"]],
            [("voice", "self", "我马上回去。"), ("text", "customer", "普通文字")],
        )

    def test_v3_text_dedupe_key_is_stable_when_the_page_shifts(self):
        target = WechatReadTarget(
            conversation_id="conv-1",
            rpa_session_key="wx:rpa:v1:a",
            display_name="CJR8S5K3 虾丸子大人",
            remark_code="CJR8S5K3",
            authorization_revision="rev-1",
        )

        def sidecar_payload(*, top: int, prefix: bool) -> dict:
            observations = []
            if prefix:
                observations.append(
                    {
                        "schema_version": 3,
                        "observation_id": "prefix",
                        "row_kind": "text_bubble",
                        "sender_role": "customer",
                        "sender_role_source": "same_row_avatar",
                        "message_type": "text",
                        "voice_state": "not_voice",
                        "content_clean": "上一条消息",
                        "source_message": {"id": "win32_ocr:prefix", "source_adapter": "win32_ocr"},
                    }
                )
            observations.append(
                {
                    "schema_version": 3,
                    "observation_id": f"win32_ocr:self-{top}",
                    "row_kind": "text_bubble",
                    "sender_role": "self",
                    "sender_role_source": "same_row_avatar",
                    "message_type": "text",
                    "voice_state": "not_voice",
                    "content_clean": "哦",
                    "bubble_rect": [841, top, 868, top + 26],
                    "source_message": {
                        "id": f"win32_ocr:self-{top}",
                        "source_adapter": "win32_ocr",
                        "bubble_rect": [841, top, 868, top + 26],
                    },
                }
            )
            return {"ok": True, "observation_schema_version": 3, "observations": observations}

        first = build_v3_message_ingest_payload(target, sidecar_payload(top=293, prefix=True))
        shifted = build_v3_message_ingest_payload(target, sidecar_payload(top=173, prefix=False))

        first_item = next(item for item in first["messages"] if item["content"] == "哦")
        shifted_item = next(item for item in shifted["messages"] if item["content"] == "哦")
        self.assertEqual(first_item["dedupe_key"], shifted_item["dedupe_key"])
        self.assertNotEqual(
            first_item["message_position"].get("visual_top"),
            shifted_item["message_position"].get("visual_top"),
        )
        self.assertEqual(first_item["raw_payload"]["dedupe_basis"]["occurrence_index"], 0)
        self.assertEqual(shifted_item["raw_payload"]["dedupe_basis"]["occurrence_index"], 0)

    def test_v3_mixed_text_and_bottom_up_voice_results_are_emitted_top_to_bottom(self):
        target = WechatReadTarget(
            conversation_id="conv-1",
            rpa_session_key="wx:rpa:v1:a",
            display_name="CJR8S5K3 虾丸子大人",
            remark_code="CJR8S5K3",
            authorization_revision="rev-1",
        )

        def observation(
            observation_id: str,
            *,
            top: int,
            role: str,
            row_kind: str,
            content: str,
            duration: int | None = None,
        ) -> dict:
            msg_type = "voice" if row_kind == "voice_transcript" else "text"
            return {
                "schema_version": 3,
                "observation_id": observation_id,
                "row_kind": row_kind,
                "sender_role": role,
                "sender_role_source": "parent_voice" if msg_type == "voice" else "same_row_avatar",
                "message_type": msg_type,
                "voice_state": "transcribed" if msg_type == "voice" else "not_voice",
                "content_clean": content,
                "bubble_rect": [420, top, 760, top + 44],
                "voice_duration": duration,
                "parent_voice_anchor_key": (
                    f"voice:{role}:{duration}:{'top' if observation_id == 'voice-top' else 'bottom'}"
                    if msg_type == "voice"
                    else None
                ),
                "source_message": {
                    "id": observation_id,
                    "source_adapter": "win32_ocr",
                    "type": msg_type,
                    "sender_role": role,
                    "content": content,
                    "bubble_rect": [420, top, 760, top + 44],
                    "voice_duration": duration,
                },
            }

        payload = build_v3_message_ingest_payload(
            target,
            {
                "ok": True,
                "observation_schema_version": 3,
                "authoritative_frame_source": "final_read",
                "observations": [
                    observation("text-1", top=120, role="customer", row_kind="text_bubble", content="第一条文字"),
                    observation("voice-top", top=220, role="customer", row_kind="voice_transcript", content="上面的语音", duration=3),
                    observation("text-2", top=340, role="self", row_kind="text_bubble", content="中间文字"),
                    observation("voice-bottom", top=460, role="self", row_kind="voice_transcript", content="下面的语音", duration=4),
                    observation("text-3", top=580, role="customer", row_kind="text_bubble", content="最后文字"),
                ],
                "voice_transcription": {
                    "state": "voice_transcribe_completed",
                    "quality_flags": [],
                    # Physical operation order is deliberately bottom-to-top.
                    "transcribed_messages": [
                        {
                            "id": "physical-bottom-first",
                            "type": "voice",
                            "sender_role": "self",
                            "content": "下面的语音",
                            "voice_duration": 4,
                            "bubble_rect": [620, 420, 890, 464],
                            "voice_anchor_stable_key": "voice:self:4:bottom",
                        },
                        {
                            "id": "physical-top-second",
                            "type": "voice",
                            "sender_role": "customer",
                            "content": "上面的语音",
                            "voice_duration": 3,
                            "bubble_rect": [420, 220, 700, 264],
                            "voice_anchor_stable_key": "voice:customer:3:top",
                        },
                    ],
                },
            },
        )

        self.assertEqual(
            [item["content"] for item in payload["messages"]],
            ["第一条文字", "上面的语音", "中间文字", "下面的语音", "最后文字"],
        )
        self.assertEqual(
            [item["message_position"]["screen_order"] for item in payload["messages"]],
            [1, 2, 3, 4, 5],
        )
        self.assertTrue(all(item["message_position"]["frame_source"] == "final_read" for item in payload["messages"]))
        self.assertEqual(payload["messages"][1]["raw_payload"]["voice_anchor_stable_key"], "voice:customer:3:top")
        self.assertEqual(payload["messages"][3]["raw_payload"]["voice_anchor_stable_key"], "voice:self:4:bottom")

    def test_v3_does_not_recreate_voice_from_diagnostic_transcription_without_final_observation(self):
        target = WechatReadTarget(
            conversation_id="conv-1",
            rpa_session_key="wx:rpa:v1:a",
            display_name="CJR8S5K3 虾丸子大人",
            remark_code="CJR8S5K3",
            authorization_revision="rev-1",
        )
        payload = build_v3_message_ingest_payload(
            target,
            {
                "ok": True,
                "observation_schema_version": 3,
                "authoritative_frame_source": "final_read",
                "observations": [],
                "voice_transcription": {
                    "state": "voice_transcribe_completed",
                    "transcribed_messages": [
                        {
                            "type": "voice",
                            "sender_role": "customer",
                            "content": "已经被最终画面顶出屏幕",
                            "voice_anchor_stable_key": "voice:customer:23:offscreen",
                        }
                    ],
                },
            },
        )

        self.assertEqual(payload["messages"], [])

    def test_v3_missing_geometry_keeps_authoritative_observation_order(self):
        target = WechatReadTarget(
            conversation_id="conv-1",
            rpa_session_key="wx:rpa:v1:a",
            display_name="CJR8S5K3 虾丸子大人",
            remark_code="CJR8S5K3",
            authorization_revision="rev-1",
        )
        payload = build_v3_message_ingest_payload(
            target,
            {
                "ok": True,
                "observation_schema_version": 3,
                "observations": [
                    {
                        "schema_version": 3,
                        "observation_id": "first",
                        "row_kind": "text_bubble",
                        "sender_role": "customer",
                        "sender_role_source": "same_row_avatar",
                        "message_type": "text",
                        "voice_state": "not_voice",
                        "content_clean": "没有坐标",
                        "source_message": {"id": "first", "source_adapter": "win32_ocr"},
                    },
                    {
                        "schema_version": 3,
                        "observation_id": "second",
                        "row_kind": "text_bubble",
                        "sender_role": "self",
                        "sender_role_source": "same_row_avatar",
                        "message_type": "text",
                        "voice_state": "not_voice",
                        "content_clean": "有坐标",
                        "bubble_rect": [700, 100, 850, 140],
                        "source_message": {"id": "second", "source_adapter": "win32_ocr"},
                    },
                ],
            },
        )

        self.assertEqual([item["content"] for item in payload["messages"]], ["没有坐标", "有坐标"])
        self.assertEqual(payload["messages"][0]["message_position"]["order_source"], "observation_index_fallback")
        self.assertEqual(payload["messages"][1]["message_position"]["order_source"], "observation_index_fallback")

    def test_v3_complete_geometry_marks_entire_frame_as_visual_order(self):
        target = WechatReadTarget(
            conversation_id="conv-visual-order",
            rpa_session_key="wx:rpa:v1:visual-order",
            display_name="CJORDER01 张三",
            remark_code="CJORDER01",
            authorization_revision="rev-1",
        )
        payload = build_v3_message_ingest_payload(
            target,
            {
                "observations": [
                    self._identity_text("visual-order-a", "第一条", 100),
                    self._identity_text("visual-order-b", "第二条", 200),
                ],
            },
        )

        self.assertEqual(
            [
                item["message_position"]["order_source"]
                for item in payload["messages"]
            ],
            ["visual_top", "visual_top"],
        )

    def test_v3_image_slot_is_not_ingested_but_preserves_surrounding_order(self):
        target = WechatReadTarget(
            conversation_id="conv-1",
            rpa_session_key="wx:rpa:v1:a",
            display_name="CJR8S5K3 虾丸子大人",
            remark_code="CJR8S5K3",
            authorization_revision="rev-1",
        )
        payload = build_v3_message_ingest_payload(
            target,
            {
                "ok": True,
                "observation_schema_version": 3,
                "observations": [
                    {
                        "schema_version": 3,
                        "observation_id": "text-before",
                        "row_kind": "text_bubble",
                        "sender_role": "customer",
                        "sender_role_source": "same_row_avatar",
                        "message_type": "text",
                        "voice_state": "not_voice",
                        "content_clean": "图片前",
                        "bubble_rect": [420, 100, 600, 140],
                        "source_message": {"id": "text-before", "source_adapter": "win32_ocr"},
                    },
                    {
                        "schema_version": 3,
                        "observation_id": "image",
                        "row_kind": "image_bubble",
                        "sender_role": "customer",
                        "sender_role_source": "same_row_avatar",
                        "message_type": "image",
                        "voice_state": "not_voice",
                        "image_physical_anchor": {
                            "sender_role": "customer",
                            "preceding_stable_message": "text-before",
                            "following_stable_message": "text-after",
                            "occurrence_index": 0,
                        },
                        "content_clean": "",
                        "bubble_rect": [420, 180, 650, 320],
                        "source_message": {"id": "image", "type": "image"},
                    },
                    {
                        "schema_version": 3,
                        "observation_id": "text-after",
                        "row_kind": "text_bubble",
                        "sender_role": "self",
                        "sender_role_source": "same_row_avatar",
                        "message_type": "text",
                        "voice_state": "not_voice",
                        "content_clean": "图片后",
                        "bubble_rect": [700, 360, 860, 400],
                        "source_message": {"id": "text-after", "source_adapter": "win32_ocr"},
                    },
                ],
            },
        )

        self.assertEqual([item["message_type"] for item in payload["messages"]], ["text", "text"])
        self.assertEqual([item["message_position"]["screen_order"] for item in payload["messages"]], [1, 3])

    def test_v3_skips_non_ingestible_call_event_observation(self):
        target = WechatReadTarget(
            conversation_id="conv-1",
            rpa_session_key="wx:rpa:v1:a",
            display_name="CJR8S5K3 虾丸子大人",
            remark_code="CJR8S5K3",
            authorization_revision="rev-1",
        )
        payload = build_v3_message_ingest_payload(
            target,
            {
                "ok": True,
                "observation_schema_version": 3,
                "observations": [
                    {
                        "schema_version": 3,
                        "observation_id": "call-1",
                        "row_kind": "call_event",
                        "sender_role": "self",
                        "sender_role_source": "same_row_avatar",
                        "message_type": "system",
                        "voice_state": "not_voice",
                        "content_clean": "通话时长 06:53",
                        "source_message": {"id": "call-1", "type": "system"},
                    }
                ],
            },
        )

        self.assertEqual(payload["messages"], [])

    def test_v3_skips_chat_text_without_same_row_avatar_source(self):
        target = WechatReadTarget(
            conversation_id="conv-1",
            rpa_session_key="wx:rpa:v1:a",
            display_name="CJR8S5K3 虾丸子大人",
            remark_code="CJR8S5K3",
            authorization_revision="rev-1",
        )
        payload = build_v3_message_ingest_payload(
            target,
            {
                "ok": True,
                "observation_schema_version": 3,
                "observations": [
                    {
                        "schema_version": 3,
                        "observation_id": "banner-1",
                        "row_kind": "text_bubble",
                        "sender_role": "customer",
                        "sender_role_source": "lane_geometry",
                        "message_type": "text",
                        "voice_state": "not_voice",
                        "content_clean": "你正在其他设备进行切换",
                        "source_message": {"id": "banner-1", "type": "text"},
                    }
                ],
            },
        )

        self.assertEqual(payload["messages"], [])

    def test_v3_records_invalid_voice_role_source_without_reclassifying_it(self):
        target = WechatReadTarget(
            conversation_id="conv-1",
            rpa_session_key="wx:rpa:v1:a",
            display_name="CJR8S5K3 虾丸子大人",
            remark_code="CJR8S5K3",
            authorization_revision="rev-1",
        )
        payload = build_v3_message_ingest_payload(
            target,
            {
                "ok": True,
                "observation_schema_version": 3,
                "observations": [
                    {
                        "schema_version": 3,
                        "observation_id": "invalid-voice-role-source",
                        "row_kind": "voice_transcript",
                        "sender_role": "customer",
                        "sender_role_source": "same_row_avatar",
                        "message_type": "voice",
                        "voice_state": "transcribed",
                        "content_clean": "不能偷偷改角色来源",
                        "parent_voice_anchor_key": "voice:customer:5",
                        "source_message": {"id": "invalid-voice-role-source", "type": "voice"},
                    }
                ],
            },
        )

        self.assertEqual(payload["messages"], [])
        self.assertEqual(
            payload["evidence"]["observation_validation_errors"][0]["error_code"],
            "MESSAGE_ROW_ROLE_SOURCE_UNTRUSTED",
        )

    def test_v3_rejects_payload_build_without_authorization_revision(self):
        target = WechatReadTarget(
            conversation_id="conv-1",
            rpa_session_key="wx:rpa:v1:a",
            display_name="CJR8S5K3 虾丸子大人",
            remark_code="CJR8S5K3",
        )

        with self.assertRaisesRegex(ValueError, "C2_TARGET_AUTHORIZATION_REVISION_MISSING"):
            build_v3_message_ingest_payload(
                target,
                {
                    "ok": True,
                    "observation_schema_version": 3,
                    "observations": [],
                },
            )

    def test_v3_ignores_sidecar_machine_fingerprint_and_stamps_worker_contract(self):
        target = WechatReadTarget(
            conversation_id="conv-1",
            rpa_session_key="wx:rpa:v1:a",
            display_name="CJR8S5K3 虾丸子大人",
            remark_code="CJR8S5K3",
            authorization_revision="rev-1",
        )

        payload = build_v3_message_ingest_payload(
            target,
            {"contract_sha256": "0" * 64, "observations": []},
        )
        self.assertEqual(payload["contract_revision"], contract_revision())
        self.assertEqual(payload["contract_sha256"], contract_sha256())

    def test_v3_maps_worker_ai_receipt_to_the_exact_stable_message_only(self):
        target = WechatReadTarget(
            conversation_id="conv-ai-receipt",
            rpa_session_key="",
            display_name="CJAI01",
            remark_code="CJAI01",
            authorization_revision="revision-ai-receipt",
        )
        payload = build_v3_message_ingest_payload(
            target,
            {
                "observations": [
                    {
                        "schema_version": 3,
                        "observation_id": "observation-ai-reply",
                        "_worker_stable_id": "worker-message-ai",
                        "_worker_ai_reply_receipt": {
                            "reply_action_id": "reply-action-ai",
                            "reply_text_hash": "a" * 64,
                            "worker_stable_id": "worker-message-ai",
                            "confirmed_at": "2026-07-24T10:00:00+00:00",
                        },
                        "row_kind": "text_bubble",
                        "sender_role": "self",
                        "sender_role_source": "same_row_avatar",
                        "message_type": "text",
                        "voice_state": "not_voice",
                        "content_clean": "AI 回复",
                        "source_message": {
                            "id": "source-ai-reply",
                            "type": "text",
                            "sender_role": "self",
                            "content": "AI 回复",
                        },
                    }
                ],
            },
        )

        self.assertEqual(len(payload["messages"]), 1)
        message = payload["messages"][0]
        receipt = message["raw_payload"]["ai_reply_receipt"]
        self.assertEqual(receipt["reply_action_id"], "reply-action-ai")
        self.assertEqual(receipt["worker_stable_id"], "worker-message-ai")
        self.assertEqual(receipt["source_message_key"], message["source_message_key"])
        self.assertNotIn(
            "_worker_ai_reply_receipt",
            message["raw_payload"]["observation"],
        )

    def test_flow_gate_diagnostics_cannot_override_contract_or_continuation(self):
        target = WechatReadTarget(
            conversation_id="conv-flow-gate",
            rpa_session_key="wx:rpa:v1:flow-gate",
            display_name="CJFLOW01 客户",
            remark_code="CJFLOW01",
            read_reason="waiting_sales_reply",
            authorization_revision="revision-flow-gate",
            raw={
                "authorization_read_reason": "waiting_sales_reply",
                "batch_continuation": {
                    "batch_id": "batch-safe",
                    "token": "token-safe",
                },
            },
        )

        payload = build_flow_gate_ingest_payload(
            target,
            error_code="C2_MESSAGE_HISTORY_GAP",
            evidence={
                "contract_sha256": "0" * 64,
                "authorization_read_reason": "recall_precheck",
                "continuation_batch_id": "batch-forged",
                "continuation_token": "token-forged",
                "flow_gate_errors": ["FORGED"],
            },
        )

        evidence = payload["evidence"]
        self.assertEqual(evidence["contract_sha256"], contract_sha256())
        self.assertEqual(
            evidence["authorization_read_reason"],
            "waiting_sales_reply",
        )
        self.assertEqual(evidence["continuation_batch_id"], "batch-safe")
        self.assertEqual(evidence["continuation_token"], "token-safe")
        self.assertEqual(
            evidence["flow_gate_errors"],
            ["C2_MESSAGE_HISTORY_GAP"],
        )


if __name__ == "__main__":
    unittest.main()
