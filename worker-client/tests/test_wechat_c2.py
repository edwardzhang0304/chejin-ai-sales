from __future__ import annotations

import hashlib
import inspect
import json
import unittest
from pathlib import Path

import chejin_worker_client.wechat_c2 as wechat_c2_module
from chejin_worker_client.c2_contract import contract_revision, contract_sha256
from chejin_worker_client.models import WechatReadTarget
from chejin_worker_client.message_identity_commit import (
    MessageCommitBasis,
    committed_identity_record,
)
from chejin_worker_client.message_viewport_projection import (
    boundary_tokens_for_observations,
    compare_business_viewport_continuity,
    normalized_business_message_sequence,
    stable_business_content_signature,
)
from chejin_worker_client.wechat_c2 import (
    apply_image_terminal_result,
    build_flow_gate_ingest_payload,
    build_message_ingest_payload as _build_message_ingest_payload_v3,
    build_preliminary_slot_payload,
    build_scan_result_payload,
    is_formal_c2_remark_code,
    image_observation_source_key,
    message_type,
    sender_role_hint,
    validate_committed_image_identity,
    voice_observation_source_key,
)
from tests.contract_artifacts import resolve_contract_artifact


def worker_source_message_key(target, *, identity_kind, identity):
    """Construct a historical fixture key without a production bypass."""

    raw = json.dumps(
        {
            "conversation_id": target.conversation_id,
            "identity_kind": str(identity_kind).strip().lower(),
            "identity": identity,
        },
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    return "source:" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:40]

def build_v3_message_ingest_payload(
    target: WechatReadTarget,
    sidecar_payload: dict,
    *,
    preliminary: bool = False,
) -> dict:
    observations = []
    for index, raw_item in enumerate(
        sidecar_payload.get("observations") or [],
        start=1,
    ):
        if not isinstance(raw_item, dict):
            observations.append(raw_item)
            continue
        item = dict(raw_item)
        message_type = str(item.get("message_type") or "").strip().lower()
        row_kind = str(item.get("row_kind") or "").strip().lower()
        if (
            message_type == "text" and row_kind == "text_bubble"
        ) or (
            message_type == "system"
            and row_kind in {"system_row", "system_message"}
        ):
            stable_id = str(
                item.get("_worker_stable_id")
                or f"worker-message-{index}"
            )
            observation_id = str(item.get("observation_id") or "")
            item["_worker_stable_id"] = stable_id
            item["_worker_identity_scope"] = "committed"
            item["_worker_committed_message"] = committed_identity_record(
                worker_stable_id=stable_id,
                commit_basis=MessageCommitBasis.NEW_SUFFIX,
                observation_id=observation_id,
                sender_role=str(item.get("sender_role") or ""),
                message_type=message_type,
                proof={
                    "alignment_status": "not_required",
                    "old_tail_fully_consumed": True,
                    "new_suffix_observation_id": observation_id,
                },
            )
        observations.append(item)
    evidence = {
        "pre_sequence_source": "empty_checkpoint",
        "pre_frame_id": f"checkpoint:none:{target.conversation_id}",
        "post_frame_id": "frame:test-v3-message-ingest",
        "alignment_status": "not_required",
        "candidate_alignment_count": 0,
        "matched_pairs": [],
        "old_tail_fully_consumed": True,
        "new_suffix_observation_ids": [
            str(item.get("observation_id") or "")
            for item in observations
            if isinstance(item, dict)
            and str(item.get("observation_id") or "")
            and str(item.get("row_kind") or "").strip().lower()
            in {
                "text_bubble",
                "voice_bubble",
                "voice_transcript",
                "image_bubble",
                "system_message",
            }
        ],
    }
    slot_origins = {
        str(item.get("origin_read_run_id") or "").strip()
        for item in (sidecar_payload.get("slot_ledger_states") or [])
        if isinstance(item, dict)
        and str(item.get("origin_read_run_id") or "").strip()
    }
    read_run_id = (
        next(iter(slot_origins))
        if len(slot_origins) == 1
        else "read-v3-message-ingest-test"
    )
    builder = (
        build_preliminary_slot_payload
        if preliminary
        else _build_message_ingest_payload_v3
    )
    return builder(
        target,
        {
            "contract_version": 3,
            "contract_revision": contract_revision(),
            "contract_sha256": contract_sha256(),
            "observation_schema_version": 3,
            "authoritative_frame_source": "final_read",
            **sidecar_payload,
            "observations": observations,
            "sequence_alignment_evidence": sidecar_payload.get(
                "sequence_alignment_evidence", evidence
            ),
        },
        read_run_id=read_run_id,
    )


def attach_committed_record(
    observation: dict,
    *,
    basis: MessageCommitBasis,
    proof: dict,
) -> dict:
    item = dict(observation)
    normalized_proof = dict(proof)
    if basis in {
        MessageCommitBasis.CONFIRMED_VOICE_ACTION,
        MessageCommitBasis.CONFIRMED_IMAGE_ACTION,
    }:
        summary_key = (
            "_worker_voice_action_summary"
            if basis is MessageCommitBasis.CONFIRMED_VOICE_ACTION
            else "_worker_image_action_summary"
        )
        summary = item.get(summary_key)
        mapping = (
            summary.get("confirmed_action_mapping")
            if isinstance(summary, dict)
            and isinstance(summary.get("confirmed_action_mapping"), dict)
            else {}
        )
        normalized_proof.update(dict(mapping))
        if basis is MessageCommitBasis.CONFIRMED_IMAGE_ACTION:
            image_sha256 = str(
                (summary or {}).get("image_sha256") or ""
            )
            if image_sha256:
                normalized_proof["image_sha256"] = image_sha256
    item["_worker_committed_message"] = committed_identity_record(
        worker_stable_id=str(item.get("_worker_stable_id") or ""),
        commit_basis=basis,
        observation_id=str(item.get("observation_id") or ""),
        sender_role=str(item.get("sender_role") or ""),
        message_type=str(item.get("message_type") or ""),
        proof=normalized_proof,
    )
    return item


def attach_confirmed_voice_result(
    observation: dict,
    *,
    stable_id: str,
    action_id: str,
    pre_observation_id: str,
    result_screen_order: int,
) -> dict:
    item = {
        **observation,
        "_worker_stable_id": stable_id,
        "_worker_identity_scope": "committed",
    }
    signature = stable_business_content_signature(item)
    mapping = {
        "canonical_action_id": action_id,
        "reserved_worker_stable_id": stable_id,
        "selected_action_token": f"token-{action_id}",
        "pre_observation_id": pre_observation_id,
        "post_observation_id": str(item.get("observation_id") or ""),
        "trigger_observation_id": f"trigger-{action_id}",
        "physical_identity_inherited_from_prepare": False,
        "physical_action_count": 1,
        "result_candidate_count": 1,
        "stable_business_content_signature": signature,
        "result_screen_order": result_screen_order,
        "binding_confirmed": True,
    }
    item["_worker_voice_action_summary"] = {
        "confirmed_action_mapping": mapping,
    }
    return attach_committed_record(
        item,
        basis=MessageCommitBasis.CONFIRMED_VOICE_ACTION,
        proof=mapping,
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
    def test_ingest_builder_requires_and_preserves_caller_read_run_id(self):
        target = WechatReadTarget(
            conversation_id="conv-explicit-read-run",
            rpa_session_key="wx:explicit-read-run",
            display_name="CJREAD01",
            remark_code="CJREAD01",
            authorization_revision="revision-explicit-read-run",
            read_reason="waiting_user_reply",
            unread_generation=7,
        )
        sidecar_payload = {
            "contract_version": 3,
            "contract_revision": contract_revision(),
            "contract_sha256": contract_sha256(),
            "observation_schema_version": 3,
            "authoritative_frame_source": "final_read",
            "observations": [],
            "slot_ledger_states": [],
            "sequence_alignment_evidence": {
                "pre_sequence_source": "empty_checkpoint",
                "pre_frame_id": "checkpoint:none:conv-explicit-read-run",
                "post_frame_id": "frame:explicit-read-run",
                "alignment_status": "not_required",
                "candidate_alignment_count": 0,
                "matched_pairs": [],
                "old_tail_fully_consumed": True,
                "new_suffix_observation_ids": [],
            },
        }

        payload = _build_message_ingest_payload_v3(
            target,
            sidecar_payload,
            read_run_id="read-explicit-caller",
        )

        self.assertEqual(payload["read_run_id"], "read-explicit-caller")
        self.assertEqual(payload["unread_generation"], 7)
        with self.assertRaises(TypeError):
            _build_message_ingest_payload_v3(target, sidecar_payload)
        self.assertNotIn(
            "uuid",
            inspect.getsource(_build_message_ingest_payload_v3),
        )

    def test_worker_identity_module_cannot_reintroduce_title_reclassification(self):
        source = inspect.getsource(wechat_c2_module)

        self.assertNotIn("raw_title", source)
        self.assertNotIn("classify_c2_conversation_title", source)
        self.assertNotIn("extract_c2_remark_codes", source)
        self.assertNotIn("omniauto-rpa", source)

    def test_voice_frame_local_anchors_never_become_durable_identity(self):
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

        with self.assertRaisesRegex(
            ValueError,
            "C2_VOICE_IDENTITY_CONTRACT_INVALID",
        ):
            voice_observation_source_key(target, bubble)
        with self.assertRaisesRegex(
            ValueError,
            "C2_VOICE_IDENTITY_CONTRACT_INVALID",
        ):
            voice_observation_source_key(target, transcript)

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

    def test_image_physical_anchor_never_restores_cross_round_identity(self):
        target = WechatReadTarget(
            conversation_id="conv-image-anchor-retired",
            rpa_session_key="wx:image-anchor-retired",
            display_name="CJIMG001",
            remark_code="CJIMG001",
        )
        old_anchor = {
            "sender_role": "customer",
            "bubble_visual_fingerprint": "dhash64:same-seat",
            "occurrence_index": 1,
            "occurrence_count": 1,
        }
        physical_only = self._identity_image(
            "image-new-same-seat",
            260,
            occurrence_index=1,
            occurrence_count=1,
            fingerprint="dhash64:same-seat",
        )
        with self.assertRaisesRegex(
            ValueError,
            "C2_IMAGE_IDENTITY_CONTRACT_INVALID",
        ):
            image_observation_source_key(target, physical_only)

        old_anchor_key = worker_source_message_key(
            target,
            identity_kind="image_physical_anchor",
            identity=old_anchor,
        )
        frame_local = {
            **physical_only,
            "frame_visual_id": "frame-visual:new-message",
        }
        with self.assertRaisesRegex(
            ValueError,
            "C2_IMAGE_IDENTITY_CONTRACT_INVALID",
        ):
            image_observation_source_key(target, frame_local)
        sequence = {
            **frame_local,
            "_worker_stable_id": "worker-message-9",
            "_worker_identity_scope": "committed",
        }
        sequence = attach_committed_record(
            sequence,
            basis=MessageCommitBasis.HISTORICAL_CHECKPOINT_ALIGNMENT,
                proof={
                    "alignment_status": "unique",
                    "pre_observation_id": "checkpoint-image-9",
                    "worker_stable_id": "worker-message-9",
                    "post_observation_id": "image-new-same-seat",
                    "match_basis": "two_sided_historical_context",
            },
        )
        sequence_key = image_observation_source_key(target, sequence)
        self.assertNotEqual(sequence_key, old_anchor_key)
        shifted_sequence = {
            **sequence,
            "frame_visual_id": "frame-visual:shifted",
            "bubble_rect": [430, 520, 650, 600],
        }
        self.assertEqual(
            sequence_key,
            image_observation_source_key(target, shifted_sequence),
        )

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
                "scan_id": "scan-worker-sidecar-shared",
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
                        time="14:17",
                        last_message_observation_id="preview-observation-1",
                        unread_signal=True,
                        ocr_confidence=0.97,
                    )
                ],
            }
        )

        self.assertFalse(payload["scan_failed"])
        self.assertEqual(payload["scan_id"], "scan-worker-sidecar-shared")
        self.assertEqual(payload["sessions"][0]["rpa_session_key"], "wx:rpa:v1:a")
        self.assertEqual(payload["sessions"][0]["remark_code_candidates"], ["CJ8K2P34"])
        self.assertTrue(payload["sessions"][0]["unread_hint"])
        self.assertEqual(
            payload["sessions"][0]["last_message_preview_time"],
            "14:17",
        )
        self.assertEqual(
            payload["sessions"][0]["last_message_observation_id"],
            "preview-observation-1",
        )

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
        voice_signature = stable_business_content_signature(
            {
                "row_kind": "voice_transcript",
                "sender_role": "self",
                "message_type": "voice",
                "voice_state": "transcribed",
                "content_clean": "我马上回去。",
                "voice_duration": 4,
            }
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
                        "_worker_stable_id": "worker-message-30",
                        "_worker_identity_scope": "committed",
                        "_worker_committed_message": committed_identity_record(
                            worker_stable_id="worker-message-30",
                            commit_basis=MessageCommitBasis.CONFIRMED_VOICE_ACTION,
                            observation_id="voice-transcript",
                            sender_role="self",
                            message_type="voice",
                            proof={
                                "canonical_action_id": "voice-action-30",
                                "reserved_worker_stable_id": "worker-message-30",
                                "pre_observation_id": "voice-pre-30",
                                "post_observation_id": "voice-transcript",
                                "selected_action_token": "voice-token-30",
                                "trigger_observation_id": "voice-trigger-30",
                                "physical_identity_inherited_from_prepare": False,
                                "physical_action_count": 1,
                                "result_candidate_count": 1,
                                "stable_business_content_signature": voice_signature,
                                "result_screen_order": 0,
                                "binding_confirmed": True,
                            },
                        ),
                        "_worker_voice_action_summary": {
                            "confirmed_action_mapping": {
                                "canonical_action_id": "voice-action-30",
                                "reserved_worker_stable_id": "worker-message-30",
                                "pre_observation_id": "voice-pre-30",
                                "post_observation_id": "voice-transcript",
                                "selected_action_token": "voice-token-30",
                                "trigger_observation_id": "voice-trigger-30",
                                "physical_identity_inherited_from_prepare": False,
                                "physical_action_count": 1,
                                "result_candidate_count": 1,
                                "stable_business_content_signature": voice_signature,
                                "result_screen_order": 0,
                                "binding_confirmed": True,
                            }
                        },
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
                        "_worker_stable_id": "worker-message-8",
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
                    "_worker_stable_id": "worker-message-9",
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
        self.assertEqual(
            first_item["raw_payload"]["dedupe_basis"],
            {
                "source": "worker_cross_round_sequence",
                "conversation_id": "conv-1",
                "worker_stable_id": "worker-message-9",
            },
        )
        self.assertEqual(
            shifted_item["raw_payload"]["dedupe_basis"],
            first_item["raw_payload"]["dedupe_basis"],
        )

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
            item = {
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
            if msg_type == "voice":
                stable_id = (
                    "worker-message-20"
                    if observation_id == "voice-top"
                    else "worker-message-21"
                )
                action_id = f"action-{observation_id}"
                item = attach_confirmed_voice_result(
                    item,
                    stable_id=stable_id,
                    action_id=action_id,
                    pre_observation_id=f"pre-{observation_id}",
                    result_screen_order=(
                        1 if observation_id == "voice-top" else 3
                    ),
                )
            return item

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
                        "item_state": "discovered",
                        "_worker_identity_scope": (
                            "current_read_provisional"
                        ),
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
            preliminary=True,
        )

        self.assertEqual([item["message_type"] for item in payload["messages"]], ["text", "text"])
        self.assertEqual([item["message_position"]["screen_order"] for item in payload["messages"]], [1, 3])
        preliminary_image = next(
            item
            for item in payload["evidence"]["observations"]
            if item.get("observation_id") == "image"
        )
        self.assertNotIn("_worker_stable_id", preliminary_image)
        self.assertNotIn("_worker_identity_scope", preliminary_image)
        self.assertNotIn("_worker_image_action_summary", preliminary_image)
        self.assertFalse(
            str(
                (preliminary_image.get("source_message") or {}).get(
                    "source_message_key"
                )
                or ""
            ).strip()
        )
        self.assertEqual(payload["evidence"]["slot_ledger_states"], [])

    def test_v3_rejects_provisional_image_identity(self):
        target = WechatReadTarget(
            conversation_id="conv-image-internal-evidence",
            rpa_session_key="wx:rpa:v1:image-internal-evidence",
            display_name="CJIMG001",
            remark_code="CJIMG001",
            authorization_revision="rev-image-internal-evidence",
        )
        with self.assertRaisesRegex(
            ValueError,
            "C2_IMAGE_IDENTITY_CONTRACT_INVALID",
        ):
            build_v3_message_ingest_payload(
                target,
                {
                "observations": [
                    {
                        "schema_version": 3,
                        "observation_id": "image-internal-evidence",
                        "row_kind": "image_bubble",
                        "sender_role": "customer",
                        "sender_role_source": "same_row_avatar",
                        "message_type": "image",
                        "voice_state": "not_voice",
                        "item_state": "discovered",
                        "_worker_stable_id": "worker-message-1",
                        "_worker_identity_scope": "current_read_provisional",
                        "_worker_image_action_summary": {
                            "confirmed_action_mapping": {
                                "canonical_action_id": "image-action-1",
                                "post_observation_id": "image-internal-evidence",
                                "binding_confirmed": True,
                            },
                            "image_visual_fingerprint": "dhash64:0123456789abcdef",
                        },
                        "source_message": {
                            "id": "image-internal-evidence",
                            "type": "image",
                        },
                    }
                    ],
                },
            )

    def test_image_identity_receipt_requires_every_exact_field(self):
        observation = {
            "schema_version": 3,
            "observation_id": "image-receipt-fields",
            "row_kind": "image_bubble",
            "sender_role": "customer",
            "sender_role_source": "same_row_avatar",
            "message_type": "image",
            "voice_state": "not_voice",
            "item_state": "discovered",
            "_worker_stable_id": "worker-message-9",
            "_worker_identity_scope": "current_read_provisional",
            "image_physical_anchor": {
                "bubble_visual_fingerprint": (
                    "dhash64:0123456789abcdef"
                ),
            },
            "_image_candidate_verification": {
                "required": True,
                "reason": "embedded_ocr_text_requires_context_menu",
                "fallback_observations": [
                    {
                        "row_kind": "text_bubble",
                        "content_clean": "粤B·A1234",
                    }
                ],
            },
        }
        valid_receipt = {
            "canonical_action_id": "image-action-9",
            "reserved_worker_stable_id": "worker-message-9",
            "pre_observation_id": "image-receipt-fields",
            "post_observation_id": "image-receipt-fields",
            "trigger_observation_id": "image-receipt-fields",
            "physical_identity_inherited_from_prepare": False,
            "result_screen_order": 0,
            "binding_confirmed": True,
            "image_sha256": "a" * 64,
        }
        valid_result = {
            "state": "completed",
            "action_phase": "confirmed",
            "reason": "vision_ready",
            "_confirmed_image_action_receipt": valid_receipt,
            "customer_image_understanding": {
                "schema_version": 1,
                "vision_summary": "车辆图片",
            },
            "visual_bridge_input": {"summary": "车辆图片"},
        }
        committed = apply_image_terminal_result(
            observation,
            valid_result,
        )
        self.assertEqual(committed["_worker_identity_scope"], "committed")
        self.assertEqual(committed["item_state"], "completed")
        self.assertNotIn("_image_candidate_verification", committed)
        replay_input = {
            **committed,
            "_image_candidate_verification": {
                "required": True,
                "fallback_observations": [
                    {"row_kind": "text_bubble", "content_clean": "不得落盘"}
                ],
            },
        }
        replayed = wechat_c2_module.replayable_image_observation(
            replay_input
        )
        self.assertNotIn("_image_candidate_verification", replayed)

        invalid_mutations = {
            "canonical_action_id": "",
            "reserved_worker_stable_id": "worker-message-10",
            "pre_observation_id": "",
            "post_observation_id": "different-observation",
            "trigger_observation_id": "different-observation",
            "physical_identity_inherited_from_prepare": True,
            "result_screen_order": -1,
            "binding_confirmed": False,
            "image_sha256": "f" * 63,
        }
        for field, invalid_value in invalid_mutations.items():
            with self.subTest(field=field):
                invalid = {
                    **valid_result,
                    "_confirmed_image_action_receipt": {
                        **valid_receipt,
                        field: invalid_value,
                    },
                }
                rejected = apply_image_terminal_result(
                    observation,
                    invalid,
                )
                self.assertEqual(
                    rejected["_worker_identity_scope"],
                    "current_read_provisional",
                )
                self.assertEqual(rejected["item_state"], "failed")
                self.assertEqual(
                    rejected["error_code"],
                    "C2_IMAGE_IDENTITY_CONTRACT_INVALID",
                )
                self.assertNotIn("content_clean", rejected)

    def test_image_identity_receipt_accepts_distinct_pre_and_post_observations(self):
        observation = {
            "schema_version": 3,
            # Formalization consumes the row that was actually selected in
            # the action frame.  The prepare-frame id remains only in the
            # receipt below as audit evidence.
            "observation_id": "image-after-action",
            "row_kind": "image_bubble",
            "sender_role": "customer",
            "sender_role_source": "same_row_avatar",
            "message_type": "image",
            "voice_state": "not_voice",
            "item_state": "discovered",
            "_worker_stable_id": "worker-message-19",
            "_worker_identity_scope": "current_read_provisional",
            "image_physical_anchor": {
                "bubble_visual_fingerprint": (
                    "dhash64:0123456789abcdef"
                ),
            },
        }
        receipt = {
            "canonical_action_id": "image-action-19",
            "reserved_worker_stable_id": "worker-message-19",
            "pre_observation_id": "image-before-action",
            "post_observation_id": "image-after-action",
            "trigger_observation_id": "image-after-action",
            "physical_identity_inherited_from_prepare": False,
            "result_screen_order": 0,
            "binding_confirmed": True,
            "image_sha256": "b" * 64,
        }

        committed = apply_image_terminal_result(
            observation,
            {
                "state": "completed",
                "action_phase": "confirmed",
                "reason": "vision_ready",
                "_confirmed_image_action_receipt": receipt,
                "customer_image_understanding": {
                    "schema_version": 1,
                    "vision_summary": "车辆图片",
                },
                "visual_bridge_input": {"summary": "车辆图片"},
            },
        )

        self.assertEqual(committed["_worker_identity_scope"], "committed")
        validated = validate_committed_image_identity(
            committed,
            conversation_id="conversation-image-pre-post",
            require_formalization_proof=True,
        )
        self.assertEqual(
            validated["formalization_proof"]["pre_observation_id"],
            "image-before-action",
        )

    def test_committed_image_identity_is_a_strict_whitelist(self):
        observation = {
            "schema_version": 3,
            "observation_id": "image-whitelist",
            "row_kind": "image_bubble",
            "sender_role": "customer",
            "sender_role_source": "same_row_avatar",
            "message_type": "image",
            "voice_state": "not_voice",
            "item_state": "completed",
            "_worker_stable_id": "worker-message-12",
            "_worker_identity_scope": "committed",
            "image_physical_anchor": {
                "bubble_visual_fingerprint": "dhash64:0123456789abcdef",
            },
            "_worker_image_action_summary": {
                "confirmed_action_mapping": {
                    "canonical_action_id": "image-action-12",
                    "reserved_worker_stable_id": "worker-message-12",
                    "pre_observation_id": "image-whitelist",
                    "post_observation_id": "image-whitelist",
                    "trigger_observation_id": "image-whitelist",
                    "physical_identity_inherited_from_prepare": False,
                    "result_screen_order": 0,
                    "binding_confirmed": True,
                },
                "image_sha256": "c" * 64,
                "result_screen_order": 0,
            },
        }
        observation = attach_committed_record(
            observation,
            basis=MessageCommitBasis.CONFIRMED_IMAGE_ACTION,
            proof={
                **observation["_worker_image_action_summary"][
                    "confirmed_action_mapping"
                ],
                "image_sha256": (
                    observation["_worker_image_action_summary"][
                        "image_sha256"
                    ]
                ),
            },
        )
        validated = validate_committed_image_identity(
            observation,
            conversation_id="conv-image-whitelist",
            require_formalization_proof=True,
        )
        self.assertEqual(validated["worker_stable_id"], "worker-message-12")

        for scope in (None, "", "unknown", "current_read_provisional"):
            with self.subTest(scope=scope):
                rejected = dict(observation)
                if scope is None:
                    rejected.pop("_worker_identity_scope")
                else:
                    rejected["_worker_identity_scope"] = scope
                with self.assertRaisesRegex(
                    ValueError,
                    "C2_IMAGE_IDENTITY_CONTRACT_INVALID",
                ):
                    validate_committed_image_identity(
                        rejected,
                        conversation_id="conv-image-whitelist",
                    )

        proof_mutations = {
            "canonical_action_id": "",
            "reserved_worker_stable_id": "worker-message-99",
            "pre_observation_id": "other",
            "post_observation_id": "other",
            "trigger_observation_id": "other",
            "physical_identity_inherited_from_prepare": True,
            "result_screen_order": -1,
            "binding_confirmed": False,
            "image_sha256": "d" * 63,
        }
        for field, invalid_value in proof_mutations.items():
            with self.subTest(proof_field=field):
                rejected = json.loads(json.dumps(observation))
                if field == "image_sha256":
                    rejected["_worker_image_action_summary"][field] = invalid_value
                elif field == "result_screen_order":
                    rejected["_worker_image_action_summary"][field] = invalid_value
                    rejected["_worker_image_action_summary"][
                        "confirmed_action_mapping"
                    ][field] = invalid_value
                else:
                    rejected["_worker_image_action_summary"][
                        "confirmed_action_mapping"
                    ][field] = invalid_value
                with self.assertRaisesRegex(
                    ValueError,
                    "C2_IMAGE_IDENTITY_CONTRACT_INVALID",
                ):
                    validate_committed_image_identity(
                        rejected,
                        conversation_id="conv-image-whitelist",
                        require_formalization_proof=True,
                    )

        forged = dict(observation)
        forged.pop("_worker_image_action_summary")
        with self.assertRaisesRegex(
            ValueError,
            "C2_IMAGE_IDENTITY_CONTRACT_INVALID",
        ):
            validate_committed_image_identity(
                forged,
                conversation_id="conv-image-whitelist",
                require_formalization_proof=True,
            )

    def test_formal_ingest_rejects_every_noncommitted_or_unproved_image(self):
        target = WechatReadTarget(
            conversation_id="conv-formal-image-whitelist",
            rpa_session_key="wx:rpa:v1:formal-image-whitelist",
            display_name="CJIMG016",
            remark_code="CJIMG016",
            authorization_revision="revision-formal-image-whitelist",
        )
        base = {
            "schema_version": 3,
            "observation_id": "formal-image-whitelist",
            "row_kind": "image_bubble",
            "sender_role": "customer",
            "sender_role_source": "same_row_avatar",
            "message_type": "image",
            "voice_state": "not_voice",
            "item_state": "completed",
            "content_clean": "车辆图片",
            "_worker_stable_id": "worker-message-16",
            "_worker_identity_scope": "committed",
            "image_physical_anchor": {
                "bubble_visual_fingerprint": "dhash64:0123456789abcdef",
            },
            "_worker_image_action_summary": {
                "confirmed_action_mapping": {
                    "canonical_action_id": "image-action-16",
                    "reserved_worker_stable_id": "worker-message-16",
                    "pre_observation_id": "formal-image-whitelist",
                    "post_observation_id": "formal-image-whitelist",
                    "binding_confirmed": True,
                },
                "image_visual_fingerprint": "dhash64:0123456789abcdef",
            },
            "source_message": {
                "id": "formal-image-whitelist",
                "type": "image",
            },
        }
        base = attach_committed_record(
            base,
            basis=MessageCommitBasis.CONFIRMED_IMAGE_ACTION,
            proof={
                **base["_worker_image_action_summary"][
                    "confirmed_action_mapping"
                ],
                "image_visual_fingerprint": base[
                    "_worker_image_action_summary"
                ]["image_visual_fingerprint"],
            },
        )
        invalid: list[tuple[str, dict]] = []
        for scope in (None, "", "unknown", "current_read_provisional"):
            observation = json.loads(json.dumps(base))
            if scope is None:
                observation.pop("_worker_identity_scope")
            else:
                observation["_worker_identity_scope"] = scope
            invalid.append((f"scope:{scope!r}", observation))
        no_proof = json.loads(json.dumps(base))
        no_proof.pop("_worker_image_action_summary")
        invalid.append(("missing-proof", no_proof))
        for field, value in {
            "canonical_action_id": "",
            "reserved_worker_stable_id": "worker-message-99",
            "pre_observation_id": "other",
            "post_observation_id": "other",
            "binding_confirmed": False,
        }.items():
            observation = json.loads(json.dumps(base))
            observation["_worker_image_action_summary"][
                "confirmed_action_mapping"
            ][field] = value
            invalid.append((f"proof:{field}", observation))
        fingerprint_mismatch = json.loads(json.dumps(base))
        fingerprint_mismatch["_worker_image_action_summary"][
            "image_visual_fingerprint"
        ] = "dhash64:ffffffffffffffff"
        invalid.append(("proof:image_visual_fingerprint", fingerprint_mismatch))

        for label, observation in invalid:
            with self.subTest(label=label), self.assertRaisesRegex(
                ValueError,
                "C2_IMAGE_IDENTITY_CONTRACT_INVALID",
            ):
                build_v3_message_ingest_payload(
                    target,
                    {"observations": [observation]},
                )

    def test_source_key_and_formal_ingest_share_whitelist_before_cleanup(self):
        source_key_source = inspect.getsource(
            wechat_c2_module.image_observation_source_key
        )
        builder_source = inspect.getsource(
            wechat_c2_module._build_message_ingest_payload_v3
        )
        self.assertIn(
            "require_committed_message(",
            source_key_source,
        )
        proof_check = builder_source.index("require_committed_message(")
        scope_cleanup = builder_source.index(
            'observation.pop("_worker_identity_scope", None)'
        )
        self.assertLess(proof_check, scope_cleanup)

    def test_backend_confirmed_historical_image_is_not_regenerated_without_proof(self):
        target = WechatReadTarget(
            conversation_id="conv-historical-image-no-proof",
            rpa_session_key="wx:rpa:v1:historical-image-no-proof",
            display_name="CJIMG017",
            remark_code="CJIMG017",
            authorization_revision="revision-historical-image-no-proof",
            raw={},
        )
        observation = {
            "schema_version": 3,
            "observation_id": "historical-image-no-proof",
            "row_kind": "image_bubble",
            "sender_role": "customer",
            "sender_role_source": "same_row_avatar",
            "message_type": "image",
            "voice_state": "not_voice",
            "item_state": "completed",
            "content_clean": "历史车辆图片",
            "_worker_stable_id": "worker-message-17",
            "_worker_identity_scope": "committed",
            "image_physical_anchor": {
                "bubble_visual_fingerprint": "dhash64:0123456789abcdef",
            },
            "bubble_rect": [420, 180, 650, 320],
            "customer_image_understanding": {
                "schema_version": 1,
                "vision_summary": "历史车辆图片",
            },
            "visual_bridge_input": {"summary": "历史车辆图片"},
            "source_message": {
                "id": "historical-image-no-proof",
                "type": "image",
            },
        }
        observation = attach_committed_record(
            observation,
            basis=MessageCommitBasis.HISTORICAL_CHECKPOINT_ALIGNMENT,
            proof={
                "alignment_status": "unique",
                "worker_stable_id": "worker-message-17",
                "pre_observation_id": "checkpoint-image-17",
                "post_observation_id": "historical-image-no-proof",
                "match_basis": "native_source_message_id",
            },
        )
        source_key = image_observation_source_key(target, observation)
        target.raw = {
            "identity_checkpoint": {
                "recent_messages": [
                    {
                        "source_message_key": source_key,
                        "origin_read_run_id": "read-historical-image",
                    }
                ]
            }
        }

        payload = build_v3_message_ingest_payload(
            target,
            {"observations": [observation]},
        )

        self.assertEqual(payload["messages"], [])
        self.assertEqual(
            payload["evidence"]["observation_validation_errors"],
            [],
        )

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
                        "_worker_stable_id": "worker-message-19",
                        "_worker_identity_scope": "committed",
                        "_worker_committed_message": committed_identity_record(
                            worker_stable_id="worker-message-19",
                            commit_basis=MessageCommitBasis.CONFIRMED_SENT_ACK,
                            observation_id="observation-ai-reply",
                            sender_role="self",
                            message_type="text",
                            proof={"reply_action_id": "reply-action-ai"},
                        ),
                        "_worker_ai_reply_receipt": {
                            "reply_action_id": "reply-action-ai",
                            "reply_text_hash": "a" * 64,
                            "worker_stable_id": "worker-message-19",
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
        self.assertEqual(receipt["worker_stable_id"], "worker-message-19")
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
            read_run_id="read-flow-gate-history-gap",
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

    def test_worker_sequence_is_voice_business_identity_not_screen_anchor(self):
        target = WechatReadTarget(
            conversation_id="conv-voice-worker-id",
            rpa_session_key="wx:rpa:v1:voice-worker-id",
            display_name="CJVOICE1",
            remark_code="CJVOICE1",
        )
        def historical_voice(observation_id: str, anchor: str) -> dict:
            item = {
                "observation_id": observation_id,
                "row_kind": "voice_transcript",
                "message_type": "voice",
                "sender_role": "customer",
                "_worker_stable_id": "worker-message-7",
                "_worker_identity_scope": "committed",
                "voice_anchor_structural_key": anchor,
            }
            return attach_committed_record(
                item,
                basis=MessageCommitBasis.HISTORICAL_CHECKPOINT_ALIGNMENT,
                proof={
                    "alignment_status": "unique",
                    "pre_observation_id": "checkpoint-voice-7",
                    "post_observation_id": observation_id,
                    "worker_stable_id": "worker-message-7",
                    "match_basis": "two_sided_historical_context",
                },
            )

        first = historical_voice(
            "voice-first",
            "voice:customer:3:bottom:1",
        )
        moved = historical_voice(
            "voice-moved",
            "voice:customer:3:bottom:2",
        )
        self.assertEqual(
            voice_observation_source_key(target, first),
            voice_observation_source_key(target, moved),
        )

    def test_image_terminal_projection_preserves_ui_action_phase(self):
        projected = wechat_c2_module.apply_image_terminal_result(
            {
                "observation_id": "image-action-evidence",
                "row_kind": "image_bubble",
            },
            {
                "state": "failed",
                "reason": "menu_evidence_incomplete",
                "transaction": {
                    "action_phase": "trigger_attempted",
                    "status": "menu_opened_then_unconfirmed",
                },
            },
        )

        self.assertEqual(projected["action_phase"], "trigger_attempted")
        self.assertEqual(projected["item_state"], "failed")

    def test_voice_transcription_meta_preserves_ui_action_evidence(self):
        projected = wechat_c2_module.voice_transcription_meta(
            {
                "state": "voice_transcribe_completed",
                "action_phase": "confirmed",
                "ui_action_performed": True,
                "business_state": "completed",
                "business_result_confirmed": True,
                "canonical_voice_action_id": "voice-action-1",
                "voice_action_stage": "execute",
                "reserved_worker_stable_id": "voice-stable-1",
                "pre_frame_id": "voice-frame-pre-1",
                "post_frame_id": "voice-frame-post-1",
                "selected_pre_observation_id": "voice-pre-1",
                "selected_action_token": "voice-token-1",
                "selected_target_fingerprint": "voice-fingerprint-1",
                "transcript_binding_status": "confirmed",
                "transcript_binding_method": "native_source_id",
                "binding_candidate_count": 1,
                "native_source_message_id": "wx-native-voice-1",
                "confirmed_action_mapping": {
                    "canonical_action_id": "voice-action-1",
                    "reserved_worker_stable_id": "voice-stable-1",
                    "binding_confirmed": True,
                    "post_observation_id": "voice-post-1",
                    "derived_observation_ids": [],
                },
            }
        )

        self.assertEqual(projected["action_phase"], "confirmed")
        self.assertIs(projected["ui_action_performed"], True)
        self.assertIs(projected["business_result_confirmed"], True)
        self.assertEqual(
            projected["canonical_voice_action_id"], "voice-action-1"
        )
        self.assertEqual(
            projected["native_source_message_id"], "wx-native-voice-1"
        )
        self.assertEqual(
            projected["confirmed_action_mapping"]["post_observation_id"],
            "voice-post-1",
        )

    def test_ingest_evidence_preserves_monotonic_ui_frame_invalidation(self):
        target = WechatReadTarget(
            conversation_id="conv-frame-invalidated",
            rpa_session_key="wx:rpa:v1:frame-invalidated",
            display_name="CJFRAME1",
            remark_code="CJFRAME1",
            authorization_revision="revision-frame-invalidated",
        )
        payload = build_v3_message_ingest_payload(
            target,
            {
                "authoritative_frame_source": "final_read",
                "ui_frame_invalidated": True,
                "observations": [
                    {
                        "schema_version": 3,
                        "observation_id": "text-after-media-action",
                        "row_kind": "text_bubble",
                        "sender_role": "customer",
                        "sender_role_source": "same_row_avatar",
                        "message_type": "text",
                        "voice_state": "not_voice",
                        "content_clean": "媒体动作后的最新文字",
                        "source_message": {
                            "id": "text-after-media-action",
                            "type": "text",
                            "sender_role": "customer",
                            "content": "媒体动作后的最新文字",
                        },
                    }
                ],
            },
        )

        self.assertIs(
            payload["evidence"]["ui_frame_invalidated"],
            True,
        )
        self.assertEqual(
            payload["evidence"]["observations"][0][
                "source_message"
            ]["source_message_key"],
            payload["messages"][0]["source_message_key"],
        )

    def test_same_duration_voice_insertion_is_unique_tail_by_worker_comparator(self):
        old_voice = {
            "observation_id": "old-three-seconds",
            "row_kind": "voice_bubble",
            "sender_role": "customer",
            "message_type": "voice",
            "voice_state": "untranscribed",
            "native_source_message_id": "native-old-voice",
            "_worker_stable_id": "worker-message-1",
            "_worker_identity_scope": "committed",
        }
        visible_old = {
            **old_voice,
            "observation_id": "old-three-seconds-visible",
            "_worker_stable_id": "",
            "_worker_identity_scope": "",
        }
        new_voice = {
            **visible_old,
            "observation_id": "new-three-seconds",
            "native_source_message_id": "",
        }
        previous = [old_voice]
        current = [visible_old, new_voice]
        result = compare_business_viewport_continuity(
            normalized_business_message_sequence(
                previous,
                message_viewport_bounds=None,
            ),
            normalized_business_message_sequence(
                current,
                message_viewport_bounds=None,
            ),
            old_boundary_tokens=boundary_tokens_for_observations(
                previous,
                committed_only=True,
            ),
            new_boundary_tokens=boundary_tokens_for_observations(
                current,
                committed_only=False,
            ),
        )
        self.assertEqual(result["relation"], "unique_tail_append")
        self.assertEqual(result["matched_pairs"], [{"old_index": 0, "new_index": 0}])
        self.assertEqual(result["new_suffix_indexes"], [1])


if __name__ == "__main__":
    unittest.main()
