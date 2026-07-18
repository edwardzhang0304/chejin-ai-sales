from __future__ import annotations

import unittest

from chejin_worker_client.models import WechatReadTarget
from chejin_worker_client.wechat_c2 import build_message_ingest_payload, build_scan_result_payload, extract_remark_codes


class WechatC2Test(unittest.TestCase):
    def test_scan_payload_maps_sessions_and_remark_codes(self):
        payload = build_scan_result_payload(
            {
                "ok": True,
                "adapter": "win32_ocr",
                "state": "sessions_ocr",
                "screenshot_path": "C:/scan.png",
                "sessions": [
                    {
                        "name": "王先生 CJ8K2P",
                        "session_key": "wx:rpa:v1:a",
                        "row_fingerprint": {"row": 1, "text": "王先生"},
                        "content": "你好",
                        "unread_signal": True,
                        "ocr_confidence": 0.97,
                    }
                ],
            }
        )

        self.assertFalse(payload["scan_failed"])
        self.assertEqual(payload["sessions"][0]["rpa_session_key"], "wx:rpa:v1:a")
        self.assertEqual(payload["sessions"][0]["remark_code_candidates"], ["CJ8K2P"])
        self.assertTrue(payload["sessions"][0]["unread_hint"])

    def test_scan_payload_does_not_bind_remark_code_from_message_preview(self):
        payload = build_scan_result_payload(
            {
                "ok": True,
                "sessions": [
                    {
                        "name": "聿安的家",
                        "session_key": "wx:rpa:v1:group",
                        "content": "CJR8S5K3虾丸子大人：蛹者",
                    }
                ],
            }
        )

        self.assertEqual(payload["sessions"][0]["display_name"], "聿安的家")
        self.assertEqual(payload["sessions"][0]["last_message_preview"], "CJR8S5K3虾丸子大人：蛹者")
        self.assertEqual(payload["sessions"][0]["remark_code_candidates"], [])

    def test_ocr_message_payload_uses_structural_dedupe_key(self):
        target = WechatReadTarget(conversation_id="conv-1", rpa_session_key="wx:rpa:v1:a", display_name="王先生", remark_code="CJ8K2P")
        sidecar = {
            "ok": True,
            "sidecar_run_id": "message-20260630-abc123ef",
            "artifact_dir": "C:/artifacts/message-20260630-abc123ef",
            "review_path": "C:/artifacts/message-20260630-abc123ef/wechat_messages_targeting_review.html",
            "screenshot_path": "C:/message.png",
            "messages": [
                {"id": "win32_ocr:abc", "sender_role": "unknown", "type": "text", "content": "你好", "ocr_confidence": 0.91}
            ],
        }

        payload = build_message_ingest_payload(target, sidecar)

        self.assertEqual(payload["conversation_id"], "conv-1")
        self.assertEqual(payload["rpa_session_key"], "wx:rpa:v1:a")
        self.assertNotEqual(payload["messages"][0]["dedupe_key"], "conv-1:win32_ocr:abc")
        self.assertEqual(payload["messages"][0]["raw_payload"]["dedupe_basis"]["source"], "ocr_structural_identity")
        self.assertEqual(payload["messages"][0]["sender_role_hint"], "unknown")
        self.assertEqual(payload["messages"][0]["message_type"], "text")
        self.assertEqual(payload["messages"][0]["raw_payload"]["dedupe_basis"]["remark_code"], "CJ8K2P")
        self.assertEqual(payload["evidence"]["remark_code"], "CJ8K2P")
        self.assertEqual(payload["sidecar_run_id"], "message-20260630-abc123ef")
        self.assertEqual(payload["evidence"]["sidecar_run_id"], "message-20260630-abc123ef")
        self.assertIn("message-20260630-abc123ef", payload["evidence"]["artifact_dir"])
        self.assertIn("wechat_messages_targeting_review.html", payload["evidence"]["review_path"])

    def test_message_dedupe_key_ignores_rpa_session_key_changes(self):
        message = {
            "sender_role": "customer",
            "type": "text",
            "content": "你好",
            "occurred_at": "2026-06-23T09:01:20+00:00",
            "bubble_rect": {"left": 400, "top": 160, "right": 620, "bottom": 210},
        }
        payload_a = build_message_ingest_payload(
            WechatReadTarget(conversation_id="conv-1", rpa_session_key="wx:rpa:v1:a", display_name="CJTEST01 许聪", remark_code="CJTEST01"),
            {"ok": True, "messages": [message]},
        )
        payload_b = build_message_ingest_payload(
            WechatReadTarget(conversation_id="conv-1", rpa_session_key="wx:rpa:v1:b", display_name="CJTEST01许聪", remark_code="CJTEST01"),
            {"ok": True, "messages": [message]},
        )

        self.assertEqual(payload_a["messages"][0]["dedupe_key"], payload_b["messages"][0]["dedupe_key"])
        self.assertEqual(payload_a["messages"][0]["raw_payload"]["dedupe_confidence"], "medium")
        self.assertEqual(payload_a["messages"][0]["raw_payload"]["dedupe_basis"]["source"], "content_visual_bucket")

    def test_ocr_message_dedupe_key_ignores_vertical_page_shift(self):
        target = WechatReadTarget(
            conversation_id="conv-1",
            rpa_session_key="wx:rpa:v1:a",
            display_name="CJR8S5K3 虾丸子大人",
            remark_code="CJR8S5K3",
        )
        first_messages = [
            {
                "id": "win32_ocr:first-a",
                "source_adapter": "win32_ocr",
                "sender_role": "self",
                "type": "text",
                "content": "不一定",
                "bubble_rect": {"left": 803, "top": 546, "right": 868, "bottom": 578},
            },
            {
                "id": "win32_ocr:first-b",
                "source_adapter": "win32_ocr",
                "sender_role": "self",
                "type": "text",
                "content": "特来电就是可以充特斯拉的",
                "bubble_rect": {"left": 648, "top": 619, "right": 864, "bottom": 645},
            },
        ]
        shifted_messages = [
            {
                **first_messages[0],
                "id": "win32_ocr:shifted-a",
                "bubble_rect": {"left": 803, "top": 274, "right": 868, "bottom": 306},
            },
            {
                **first_messages[1],
                "id": "win32_ocr:shifted-b",
                "bubble_rect": {"left": 650, "top": 349, "right": 864, "bottom": 372},
            },
        ]

        first = build_message_ingest_payload(target, {"ok": True, "messages": first_messages})
        shifted = build_message_ingest_payload(target, {"ok": True, "messages": shifted_messages})

        self.assertEqual(
            [item["dedupe_key"] for item in first["messages"]],
            [item["dedupe_key"] for item in shifted["messages"]],
        )

    def test_repeated_equal_ocr_text_keeps_distinct_occurrence_keys(self):
        target = WechatReadTarget(
            conversation_id="conv-1",
            rpa_session_key="wx:rpa:v1:a",
            display_name="CJR8S5K3 虾丸子大人",
            remark_code="CJR8S5K3",
        )
        messages = [
            {"id": "win32_ocr:a", "source_adapter": "win32_ocr", "sender_role": "self", "type": "text", "content": "好的"},
            {"id": "win32_ocr:b", "source_adapter": "win32_ocr", "sender_role": "self", "type": "text", "content": "好的"},
        ]

        payload = build_message_ingest_payload(target, {"ok": True, "messages": messages})

        self.assertEqual(len(payload["messages"]), 2)
        self.assertNotEqual(payload["messages"][0]["dedupe_key"], payload["messages"][1]["dedupe_key"])

    def test_existing_ocr_message_keys_do_not_change_when_new_message_is_appended(self):
        target = WechatReadTarget(
            conversation_id="conv-1",
            rpa_session_key="wx:rpa:v1:a",
            display_name="CJR8S5K3 虾丸子大人",
            remark_code="CJR8S5K3",
        )
        existing = [
            {"id": "win32_ocr:a", "source_adapter": "win32_ocr", "sender_role": "self", "type": "text", "content": "第一条"},
            {"id": "win32_ocr:b", "source_adapter": "win32_ocr", "sender_role": "self", "type": "text", "content": "第二条"},
        ]
        first = build_message_ingest_payload(target, {"ok": True, "messages": existing})
        appended = build_message_ingest_payload(
            target,
            {
                "ok": True,
                "messages": [
                    *existing,
                    {"id": "win32_ocr:c", "source_adapter": "win32_ocr", "sender_role": "self", "type": "text", "content": "第三条"},
                ],
            },
        )

        self.assertEqual(
            [item["dedupe_key"] for item in first["messages"]],
            [item["dedupe_key"] for item in appended["messages"][:2]],
        )

    def test_plain_text_equal_to_voice_transcript_is_not_promoted_to_voice(self):
        target = WechatReadTarget(conversation_id="conv-1", rpa_session_key="wx:rpa:v1:a", display_name="CJR8S5K3 虾丸子大人", remark_code="CJR8S5K3")
        payload = build_message_ingest_payload(
            target,
            {
                "ok": True,
                "messages": [
                    {
                        "id": "win32_ocr:text-good",
                        "source_adapter": "win32_ocr",
                        "type": "text",
                        "sender_role": "self",
                        "content": "好的",
                    }
                ],
                "voice_transcription": {
                    "state": "voice_transcribe_completed",
                    "transcribed_messages": [
                        {
                            "type": "voice",
                            "sender_role": "customer",
                            "content": "好的",
                            "voice_anchor_stable_key": "voice-customer-good",
                        }
                    ],
                },
            },
        )

        self.assertEqual([(item["sender_role_hint"], item["message_type"]) for item in payload["messages"]], [("self", "text"), ("customer", "voice")])

    def test_same_source_text_and_voice_conflict_emits_one_canonical_voice(self):
        target = WechatReadTarget(conversation_id="conv-1", rpa_session_key="wx:rpa:v1:a", display_name="CJR8S5K3 虾丸子大人", remark_code="CJR8S5K3")
        source_id = "win32_ocr:56d7bf09e4c9b810"
        visual_id = "canonical_visual_6f1a3efe1d603befb804ba73"
        payload = build_message_ingest_payload(
            target,
            {
                "ok": True,
                "messages": [
                    {
                        "id": source_id,
                        "canonical_visual_id": visual_id,
                        "source_adapter": "win32_ocr",
                        "type": "text",
                        "sender_role": "self",
                        "content": "好的，不着急，我身上还带了水果。",
                    }
                ],
                "voice_transcription": {
                    "state": "voice_transcribe_completed",
                    "transcribed_messages": [
                        {
                            "id": source_id,
                            "canonical_visual_id": visual_id,
                            "type": "voice",
                            "sender_role": "customer",
                            "content": "好的，不着急，我身上还带了水果。",
                            "voice_anchor_stable_key": "voice-stable:fruit",
                        }
                    ],
                },
            },
        )

        self.assertEqual(len(payload["messages"]), 1)
        self.assertEqual(payload["contract_version"], 2)
        self.assertEqual(payload["messages"][0]["message_type"], "voice")
        self.assertEqual(payload["messages"][0]["sender_role_hint"], "customer")
        self.assertTrue(payload["messages"][0]["source_message_key"])
        self.assertEqual(payload["messages"][0]["item_state"], "completed")

    def test_audio_message_and_voice_transcription_emit_one_voice(self):
        target = WechatReadTarget(conversation_id="conv-1", rpa_session_key="wx:rpa:v1:a", display_name="CJR8S5K3 虾丸子大人", remark_code="CJR8S5K3")
        payload = build_message_ingest_payload(
            target,
            {
                "ok": True,
                "messages": [
                    {
                        "id": "win32_ocr:audio-1",
                        "canonical_visual_id": "canonical_visual_audio_1",
                        "source_adapter": "win32_ocr",
                        "type": "audio",
                        "sender_role": "customer",
                        "content": "我到了。",
                    }
                ],
                "voice_transcription": {
                    "state": "voice_transcribe_completed",
                    "transcribed_messages": [
                        {
                            "id": "win32_ocr:audio-1",
                            "canonical_visual_id": "canonical_visual_audio_1",
                            "type": "voice",
                            "sender_role": "customer",
                            "content": "我到了。",
                            "voice_anchor_stable_key": "voice-stable:audio-1",
                        }
                    ],
                },
            },
        )

        self.assertEqual(len(payload["messages"]), 1)
        self.assertEqual(payload["messages"][0]["message_type"], "voice")
        self.assertEqual(payload["messages"][0]["content"], "我到了。")

    def test_equal_voice_transcripts_keep_distinct_anchor_identities(self):
        target = WechatReadTarget(conversation_id="conv-1", rpa_session_key="wx:rpa:v1:a", display_name="CJR8S5K3 虾丸子大人", remark_code="CJR8S5K3")
        transcripts = [
            {"type": "voice", "sender_role": "customer", "content": "好的", "voice_anchor_stable_key": "voice-anchor-a"},
            {"type": "voice", "sender_role": "customer", "content": "好的", "voice_anchor_stable_key": "voice-anchor-b"},
        ]
        payload = build_message_ingest_payload(
            target,
            {
                "ok": True,
                "messages": [],
                "voice_transcription": {"state": "voice_transcribe_completed", "transcribed_messages": transcripts},
            },
        )

        self.assertEqual(len(payload["messages"]), 2)
        self.assertNotEqual(payload["messages"][0]["dedupe_key"], payload["messages"][1]["dedupe_key"])
        self.assertEqual(
            [item["raw_payload"]["dedupe_basis"]["source"] for item in payload["messages"]],
            ["voice_semantic_identity", "voice_semantic_identity"],
        )

    def test_completed_voice_dedupe_is_stable_when_anchor_position_changes(self):
        target = WechatReadTarget(conversation_id="conv-1", rpa_session_key="wx:rpa:v1:a", display_name="CJR8S5K3 虾丸子大人", remark_code="CJR8S5K3")
        base = {
            "type": "voice",
            "sender_role": "customer",
            "content": "好的，不着急，我身上还带了水果。",
            "voice_duration": 6,
        }
        first = build_message_ingest_payload(
            target,
            {"ok": True, "messages": [{**base, "voice_anchor_stable_key": "voice-stable:y10"}]},
        )
        shifted = build_message_ingest_payload(
            target,
            {"ok": True, "messages": [{**base, "voice_anchor_stable_key": "voice-stable:y30"}]},
        )

        self.assertEqual(first["messages"][0]["dedupe_key"], shifted["messages"][0]["dedupe_key"])
        self.assertEqual(first["messages"][0]["raw_payload"]["dedupe_basis"]["source"], "voice_semantic_identity")

    def test_image_message_dedupe_uses_image_hash(self):
        target = WechatReadTarget(conversation_id="conv-1", rpa_session_key="wx:rpa:v1:a", display_name="CJTEST01 许聪", remark_code="CJTEST01")
        payload = build_message_ingest_payload(
            target,
            {
                "ok": True,
                "messages": [
                    {
                        "type": "image",
                        "sender_role": "customer",
                        "image_local_path": "C:/tmp/image.png",
                        "image_hash": "image-hash-001",
                        "occurred_at": "2026-06-23T09:01:20+00:00",
                    }
                ],
            },
        )

        self.assertTrue(payload["messages"][0]["dedupe_key"].startswith("conv-1:"))
        self.assertEqual(payload["messages"][0]["raw_payload"]["dedupe_confidence"], "medium")
        self.assertEqual(payload["messages"][0]["raw_payload"]["dedupe_basis"]["source"], "image_hash")

    def test_voice_message_without_text_is_not_dropped(self):
        target = WechatReadTarget(conversation_id="conv-1", rpa_session_key="wx:rpa:v1:a", display_name="CJTEST01 许聪", remark_code="CJTEST01")
        payload = build_message_ingest_payload(
            target,
            {
                "ok": True,
                "messages": [
                    {
                        "type": "voice",
                        "sender_role": "customer",
                        "voice_duration": 3,
                        "bubble_rect": {"left": 410, "top": 180, "right": 520, "bottom": 220},
                    }
                ],
            },
        )

        self.assertEqual(len(payload["messages"]), 1)
        self.assertEqual(payload["messages"][0]["sender_role_hint"], "customer")
        self.assertEqual(payload["messages"][0]["message_type"], "voice")
        self.assertIsNone(payload["messages"][0]["content"])
        self.assertEqual(payload["messages"][0]["raw_payload"]["dedupe_confidence"], "low")

    def test_voice_transcription_marks_matching_message_as_voice(self):
        target = WechatReadTarget(conversation_id="conv-1", rpa_session_key="wx:rpa:v1:a", display_name="CJTEST01 许聪", remark_code="CJTEST01")
        payload = build_message_ingest_payload(
            target,
            {
                "ok": True,
                "sidecar_run_id": "message-run-1",
                "messages": [
                    {
                        "id": "wx-msg-voice-1",
                        "type": "voice",
                        "sender_role": "customer",
                        "content": "我一会儿再回复你",
                        "bubble_rect": {"left": 410, "top": 180, "right": 620, "bottom": 230},
                    }
                ],
                "voice_transcription": {
                    "ok": True,
                    "state": "voice_transcribe_completed",
                    "sidecar_run_id": "voice-run-1",
                    "artifact_dir": "C:/voice-run-1",
                    "attempt_count": 1,
                    "quality_flags": [],
                    "after_screenshot_path": "C:/voice-run-1/voice_transcribe_after.png",
                    "transcribed_messages": [{"content": "我一会儿再回复你", "sender_role": "customer"}],
                },
            },
        )

        self.assertEqual(payload["messages"][0]["message_type"], "voice")
        self.assertEqual(payload["messages"][0]["content"], "我一会儿再回复你")
        self.assertEqual(payload["messages"][0]["sender_role_hint"], "customer")
        self.assertEqual(payload["messages"][0]["raw_payload"]["voice_transcription"], "我一会儿再回复你")
        self.assertEqual(payload["messages"][0]["raw_payload"]["voice_transcription_meta"]["state"], "voice_transcribe_completed")
        self.assertEqual(payload["messages"][0]["raw_payload"]["voice_transcription_meta"]["attempt_count"], 1)
        self.assertEqual(payload["evidence"]["voice_transcription"]["state"], "voice_transcribe_completed")

    def test_voice_transcription_match_uses_anchor_sender_role_over_ocr_role(self):
        target = WechatReadTarget(conversation_id="conv-1", rpa_session_key="wx:rpa:v1:a", display_name="CJVOICE01 虾丸子大人", remark_code="CJVOICE01")
        payload = build_message_ingest_payload(
            target,
            {
                "ok": True,
                "sidecar_run_id": "message-run-1",
                "messages": [
                    {
                        "id": "wx-msg-voice-transcript-1",
                        "type": "voice",
                        "voice_duration_text": '10"',
                        "sender_role": "self",
                        "content": "没找到那家店，我准备回去了。我都导航到哪边了，但是发现那边并没有。",
                        "bubble_rect": {"left": 484, "top": 462, "right": 883, "bottom": 510},
                    }
                ],
                "voice_transcription": {
                    "ok": True,
                    "state": "voice_transcribe_completed",
                    "sidecar_run_id": "voice-run-1",
                    "attempt_count": 1,
                    "quality_flags": [],
                    "transcribed_messages": [
                        {
                            "content": "没找到那家店，我准备回去了。我都导航到哪边了，但是发现那边并没有。",
                            "sender_role": "customer",
                            "voice_duration_text": '10"',
                        }
                    ],
                },
            },
        )

        self.assertEqual(len(payload["messages"]), 1)
        self.assertEqual(payload["messages"][0]["message_type"], "voice")
        self.assertEqual(payload["messages"][0]["sender_role_hint"], "customer")
        self.assertEqual(payload["messages"][0]["raw_payload"]["sender_role"], "customer")

    def test_voice_transcription_is_ingested_even_when_final_messages_miss_transcript(self):
        target = WechatReadTarget(conversation_id="conv-1", rpa_session_key="wx:rpa:v1:a", display_name="CJVOICE01 虾丸子大人", remark_code="CJVOICE01")
        payload = build_message_ingest_payload(
            target,
            {
                "ok": True,
                "sidecar_run_id": "message-run-1",
                "messages": [
                    {
                        "id": "wx-msg-voice-raw",
                        "type": "voice",
                        "sender_role": "customer",
                        "voice_duration": 2,
                        "content": '[语音] 2"',
                        "quality_flags": ["untranscribed_voice_placeholder"],
                    }
                ],
                "voice_transcription": {
                    "ok": True,
                    "state": "voice_transcribe_completed",
                    "sidecar_run_id": "voice-run-1",
                    "attempt_count": 1,
                    "quality_flags": [],
                    "transcribed_messages": [{"content": "不噜噜不噜噜不噜噜不噜噜。", "sender_role": "customer"}],
                },
            },
        )

        self.assertEqual(len(payload["messages"]), 1)
        self.assertEqual(payload["messages"][0]["message_type"], "voice")
        self.assertEqual(payload["messages"][0]["content"], "不噜噜不噜噜不噜噜不噜噜。")
        self.assertEqual(payload["messages"][0]["raw_payload"]["voice_transcription"], "不噜噜不噜噜不噜噜不噜噜。")
        self.assertEqual(payload["messages"][0]["raw_payload"]["voice_transcription_meta"]["state"], "voice_transcribe_completed")

    def test_voice_transcription_payload_dict_is_not_used_as_content(self):
        target = WechatReadTarget(conversation_id="conv-1", rpa_session_key="wx:rpa:v1:a", display_name="CJVOICE01 虾丸子大人", remark_code="CJVOICE01")
        payload = build_message_ingest_payload(
            target,
            {
                "ok": True,
                "sidecar_run_id": "message-run-1",
                "messages": [],
                "voice_transcription": {
                    "ok": True,
                    "state": "voice_transcribe_completed",
                    "sidecar_run_id": "voice-run-1",
                    "attempt_count": 1,
                    "quality_flags": [],
                    "transcribed_messages": [
                        {
                            "content": {"state": "voice_transcribe_completed", "payload": "debug"},
                            "content_clean": "还很凉快。",
                            "sender_role": "customer",
                        }
                    ],
                },
            },
        )

        self.assertEqual(len(payload["messages"]), 1)
        self.assertEqual(payload["messages"][0]["message_type"], "voice")
        self.assertEqual(payload["messages"][0]["content"], "还很凉快。")
        self.assertNotIn("voice_transcribe_completed", payload["messages"][0]["content"])

    def test_voice_transcription_payload_dict_without_clean_text_is_not_ingested(self):
        target = WechatReadTarget(conversation_id="conv-1", rpa_session_key="wx:rpa:v1:a", display_name="CJVOICE01 虾丸子大人", remark_code="CJVOICE01")
        payload = build_message_ingest_payload(
            target,
            {
                "ok": True,
                "sidecar_run_id": "message-run-1",
                "messages": [],
                "voice_transcription": {
                    "ok": True,
                    "state": "voice_transcribe_completed",
                    "sidecar_run_id": "voice-run-1",
                    "attempt_count": 1,
                    "quality_flags": [],
                    "transcribed_messages": [
                        {"content": {"state": "voice_transcribe_completed", "payload": "debug"}, "sender_role": "customer"}
                    ],
                },
            },
        )

        self.assertEqual(payload["messages"], [])

    def test_voice_transcription_payload_string_without_clean_text_is_not_ingested(self):
        target = WechatReadTarget(conversation_id="conv-1", rpa_session_key="wx:rpa:v1:a", display_name="CJVOICE01 虾丸子大人", remark_code="CJVOICE01")
        payload = build_message_ingest_payload(
            target,
            {
                "ok": True,
                "sidecar_run_id": "message-run-1",
                "messages": [],
                "voice_transcription": {
                    "ok": True,
                    "state": "voice_transcribe_completed",
                    "sidecar_run_id": "voice-run-1",
                    "attempt_count": 1,
                    "quality_flags": [],
                    "transcribed_messages": [
                        {
                            "content": "{'state': 'voice_transcribe_completed', 'transcribed_messages': [], 'after_screenshot_path': 'C:/tmp/after.png'}",
                            "sender_role": "self",
                        }
                    ],
                },
            },
        )

        self.assertEqual(payload["messages"], [])

    def test_voice_ocr_duration_prefix_is_cleaned_and_marked_voice(self):
        target = WechatReadTarget(conversation_id="conv-1", rpa_session_key="wx:rpa:v1:a", display_name="CJR8S5K3 虾丸子大人", remark_code="CJR8S5K3")
        payload = build_message_ingest_payload(
            target,
            {
                "ok": True,
                "sidecar_run_id": "message-run-1",
                "messages": [
                    {
                        "id": "wx-msg-voice-ocr-1",
                        "type": "voice",
                        "sender_role": "customer",
                        "content": 'の3"\n还很凉快。',
                        "content_raw_ocr": 'の3"\n还很凉快。',
                    }
                ],
                "voice_transcription": {
                    "state": "voice_transcribe_completed",
                    "attempt_count": 1,
                    "quality_flags": [],
                    "transcribed_messages": [{"content": "还很凉快。", "sender_role": "customer"}],
                },
            },
        )

        self.assertEqual(len(payload["messages"]), 1)
        self.assertEqual(payload["messages"][0]["message_type"], "voice")
        self.assertEqual(payload["messages"][0]["content"], "还很凉快。")
        self.assertTrue(payload["messages"][0]["raw_payload"]["voice_duration_prefix_removed"])

    def test_voice_ocr_duration_prefix_without_completed_transcription_is_not_ingested(self):
        target = WechatReadTarget(conversation_id="conv-1", rpa_session_key="wx:rpa:v1:a", display_name="CJR8S5K3 虾丸子大人", remark_code="CJR8S5K3")
        payload = build_message_ingest_payload(
            target,
            {
                "ok": True,
                "sidecar_run_id": "message-run-1",
                "messages": [
                    {
                        "id": "wx-msg-voice-ocr-1",
                        "type": "text",
                        "sender_role": "customer",
                        "content": 'の3"\n还很凉快。',
                        "content_raw_ocr": 'の3"\n还很凉快。',
                    },
                    {
                        "id": "wx-msg-voice-placeholder",
                        "type": "voice",
                        "sender_role": "customer",
                        "content": '[语音] の3"',
                        "quality_flags": ["untranscribed_voice_placeholder"],
                    },
                ],
                "voice_transcription": {
                    "state": "voice_transcribe_click_failed",
                    "attempt_count": 1,
                    "quality_flags": ["voice_transcribe_click_failed"],
                    "transcribed_messages": [],
                },
            },
        )

        self.assertEqual(payload["messages"], [])

    def test_worker_does_not_rebind_expanded_text_without_sidecar_anchor(self):
        target = WechatReadTarget(conversation_id="conv-1", rpa_session_key="wx:rpa:v1:a", display_name="CJR8S5K3 虾丸子大人", remark_code="CJR8S5K3")
        payload = build_message_ingest_payload(
            target,
            {
                "ok": True,
                "sidecar_run_id": "message-run-expanded",
                "initial_messages": {
                    "messages": [
                        {
                            "id": "wx-voice-customer",
                            "type": "voice",
                            "sender_role": "customer",
                            "voice_duration": 12,
                            "content": '[语音] 12"',
                            "bubble_rect": {"left": 472, "top": 306, "right": 668, "bottom": 350},
                        },
                        {
                            "id": "wx-voice-self",
                            "type": "voice",
                            "sender_role": "self",
                            "voice_duration": 2,
                            "content": '[语音] 2"',
                            "bubble_rect": {"left": 772, "top": 496, "right": 904, "bottom": 541},
                        },
                    ]
                },
                "messages": [
                    {
                        "id": "wx-text-customer-expanded",
                        "type": "text",
                        "sender_role": "self",
                        "content": "好像是我发的，前面的几个字没有识别出来。",
                        "bubble_rect": {"left": 472, "top": 361, "right": 904, "bottom": 421},
                    },
                    {
                        "id": "wx-text-self-expanded",
                        "type": "text",
                        "sender_role": "self",
                        "content": "你中午回家吃饭不",
                        "bubble_rect": {"left": 689, "top": 552, "right": 904, "bottom": 592},
                    },
                ],
                "voice_transcription": {
                    "state": "voice_transcribe_no_new_text",
                    "attempt_count": 2,
                    "quality_flags": ["no_new_transcribed_text"],
                    "sidecar_run_id": "voice-run-expanded",
                    "transcribed_messages": [],
                },
            },
        )

        self.assertEqual([item["message_type"] for item in payload["messages"]], ["text", "text"])
        self.assertEqual(payload["messages"][0]["sender_role_hint"], "self")
        self.assertEqual(payload["messages"][1]["sender_role_hint"], "self")
        self.assertNotIn("voice_transcription", payload["messages"][0]["raw_payload"])
        self.assertNotIn("voice_transcription", payload["messages"][1]["raw_payload"])

    def test_worker_does_not_cross_bind_self_text_to_customer_voice(self):
        target = WechatReadTarget(
            conversation_id="conv-1",
            rpa_session_key="wx:rpa:v1:a",
            display_name="CJR8S5K3 虾丸子大人",
            remark_code="CJR8S5K3",
        )
        payload = build_message_ingest_payload(
            target,
            {
                "ok": True,
                "initial_messages": {
                    "messages": [
                        {
                            "id": "customer-voice-left",
                            "type": "voice",
                            "sender_role": "customer",
                            "content": '[语音] 3"',
                            "bubble_rect": {"left": 489, "top": 358, "right": 535, "bottom": 384},
                        }
                    ]
                },
                "messages": [
                    {
                        "id": "self-transcript-right",
                        "type": "text",
                        "sender_role": "self",
                        "content": "会不会这种外卖是死海鲜",
                        "bubble_rect": {"left": 668, "top": 480, "right": 866, "bottom": 506},
                        "message_envelope": {"sender_role": "self", "sender": "self"},
                    }
                ],
                "voice_transcription": {
                    "state": "voice_transcribe_completed",
                    "attempt_count": 1,
                    "quality_flags": [],
                    "transcribed_messages": [],
                },
            },
        )

        self.assertEqual(len(payload["messages"]), 1)
        self.assertEqual(payload["messages"][0]["message_type"], "text")
        self.assertEqual(payload["messages"][0]["sender_role_hint"], "self")
        self.assertEqual(payload["messages"][0]["raw_payload"]["message_envelope"]["sender_role"], "self")
        self.assertNotIn("voice_expanded_text_match", payload["messages"][0]["raw_payload"])

    def test_voice_anchor_role_override_keeps_nested_envelope_consistent(self):
        target = WechatReadTarget(
            conversation_id="conv-1",
            rpa_session_key="wx:rpa:v1:a",
            display_name="CJR8S5K3 虾丸子大人",
            remark_code="CJR8S5K3",
        )
        content = "我看到这条路上还有车经过，车是可以开的。"
        payload = build_message_ingest_payload(
            target,
            {
                "ok": True,
                "messages": [
                    {
                        "id": "expanded-text",
                        "type": "voice",
                        "sender_role": "self",
                        "sender": "self",
                        "content": content,
                        "message_envelope": {"sender_role": "self", "sender": "self"},
                    }
                ],
                "voice_transcription": {
                    "state": "voice_transcribe_partial",
                    "attempt_count": 2,
                    "quality_flags": ["untranscribed_voice_remaining"],
                    "transcribed_messages": [
                        {
                            "id": "customer-voice-5",
                            "type": "voice",
                            "sender_role": "customer",
                            "sender": "customer",
                            "content": content,
                            "voice_anchor_key": "customer-5-anchor",
                        }
                    ],
                },
            },
        )

        self.assertEqual(len(payload["messages"]), 1)
        self.assertEqual(payload["messages"][0]["sender_role_hint"], "customer")
        self.assertEqual(payload["messages"][0]["raw_payload"]["sender_role"], "customer")
        self.assertEqual(payload["messages"][0]["raw_payload"]["message_envelope"]["sender_role"], "customer")

    def test_expanded_voice_text_without_initial_placeholder_stays_text(self):
        target = WechatReadTarget(conversation_id="conv-1", rpa_session_key="wx:rpa:v1:a", display_name="CJR8S5K3 虾丸子大人", remark_code="CJR8S5K3")
        payload = build_message_ingest_payload(
            target,
            {
                "ok": True,
                "messages": [
                    {
                        "id": "wx-text-normal",
                        "type": "text",
                        "sender_role": "self",
                        "content": "你中午回家吃饭不",
                        "bubble_rect": {"left": 689, "top": 552, "right": 904, "bottom": 592},
                    }
                ],
                "voice_transcription": {
                    "state": "voice_transcribe_no_new_text",
                    "attempt_count": 1,
                    "quality_flags": ["no_new_transcribed_text"],
                    "transcribed_messages": [],
                },
            },
        )

        self.assertEqual(payload["messages"][0]["message_type"], "text")
        self.assertNotIn("voice_expanded_text_match", payload["messages"][0]["raw_payload"])

    def test_voice_transcription_failure_does_not_fabricate_message(self):
        target = WechatReadTarget(conversation_id="conv-1", rpa_session_key="wx:rpa:v1:a", display_name="CJTEST01 许聪", remark_code="CJTEST01")
        payload = build_message_ingest_payload(
            target,
            {
                "ok": True,
                "messages": [],
                "voice_transcription": {
                    "ok": False,
                    "state": "voice_transcribe_click_failed",
                    "attempt_count": 1,
                    "quality_flags": ["voice_transcribe_click_failed"],
                    "transcribed_messages": [],
                },
            },
        )

        self.assertEqual(payload["messages"], [])
        self.assertEqual(payload["evidence"]["voice_transcription"]["state"], "voice_transcribe_click_failed")

    def test_untranscribed_voice_placeholder_is_not_ingested_as_text(self):
        target = WechatReadTarget(conversation_id="conv-1", rpa_session_key="wx:rpa:v1:a", display_name="CJVOICE01 虾丸子大人", remark_code="CJVOICE01")
        payload = build_message_ingest_payload(
            target,
            {
                "ok": True,
                "messages": [
                    {
                        "type": "voice",
                        "sender_role": "customer",
                        "voice_duration": 2,
                        "content": '[语音] 2"',
                        "quality_flags": ["untranscribed_voice_placeholder"],
                    },
                    {
                        "type": "text",
                        "sender_role": "customer",
                        "content": '2"\n转文字',
                        "quality_flags": ["ocr_low_confidence"],
                    },
                ],
            },
        )

        self.assertEqual(payload["messages"], [])

    def test_raw_ocr_voice_noise_without_transcription_is_not_ingested(self):
        target = WechatReadTarget(conversation_id="conv-1", rpa_session_key="wx:rpa:v1:a", display_name="CJVOICE01 虾丸子大人", remark_code="CJVOICE01")
        payload = build_message_ingest_payload(
            target,
            {
                "ok": True,
                "messages": [
                    {
                        "type": "text",
                        "sender_role": "sales_candidate",
                        "content": '2" (c',
                        "content_raw_ocr": '2" (c',
                        "quality_flags": ["ocr_low_confidence"],
                    },
                ],
                "voice_transcription": {
                    "state": "voice_transcribe_completed",
                    "attempt_count": 1,
                    "quality_flags": [],
                    "transcribed_messages": [],
                },
            },
        )

        self.assertEqual(payload["messages"], [])

    def test_partial_voice_flow_keeps_confirmed_transcript_and_drops_other_placeholder_noise(self):
        target = WechatReadTarget(conversation_id="conv-1", rpa_session_key="wx:rpa:v1:a", display_name="CJVOICE01 虾丸子大人", remark_code="CJVOICE01")
        payload = build_message_ingest_payload(
            target,
            {
                "ok": True,
                "messages": [
                    {
                        "type": "voice",
                        "sender_role": "self",
                        "content": '[语音] 6" (c',
                        "content_raw_ocr": '6" (c',
                        "quality_flags": ["untranscribed_voice_placeholder"],
                    },
                    {
                        "type": "voice",
                        "sender_role": "customer",
                        "content": "果然掉在更衣柜里了。",
                        "content_raw_ocr": '3"\n果然掉在更衣柜里了。',
                        "quality_flags": ["voice_duration_prefix_removed"],
                    },
                ],
                "voice_transcription": {
                    "state": "voice_transcribe_partial",
                    "attempt_count": 1,
                    "quality_flags": ["untranscribed_voice_remaining"],
                    "transcribed_messages": [
                        {
                            "content": "果然掉在更衣柜里了。",
                            "sender_role": "customer",
                            "voice_anchor_key": "voice-3s",
                        }
                    ],
                },
            },
        )

        self.assertEqual(len(payload["messages"]), 1)
        self.assertEqual(payload["messages"][0]["message_type"], "voice")
        self.assertEqual(payload["messages"][0]["content"], "果然掉在更衣柜里了。")
        self.assertEqual(payload["messages"][0]["raw_payload"]["voice_transcription_meta"]["state"], "voice_transcribe_partial")

    def test_raw_payload_dedupe_is_marked_low_confidence(self):
        target = WechatReadTarget(conversation_id="conv-1", rpa_session_key="wx:rpa:v1:a", display_name="CJTEST01 许聪", remark_code="CJTEST01")
        payload = build_message_ingest_payload(
            target,
            {"ok": True, "messages": [{"type": "system", "image_local_path": "C:/tmp/missing.png"}]},
        )

        self.assertEqual(payload["messages"][0]["raw_payload"]["dedupe_confidence"], "low")
        self.assertEqual(payload["messages"][0]["raw_payload"]["dedupe_basis"]["source"], "raw_payload_hash")

    def test_extract_remark_codes_supports_manual_suffix(self):
        self.assertEqual(extract_remark_codes("CJ8K2P 王先生想看轩逸"), ["CJ8K2P"])
        self.assertEqual(extract_remark_codes("CJTEST01 许聪", "CJTEST01许聪"), ["CJTEST01"])

    def test_v3_uses_observations_and_anchor_bound_voice_without_text_reinterpretation(self):
        target = WechatReadTarget(
            conversation_id="conv-1",
            rpa_session_key="wx:rpa:v1:a",
            display_name="CJR8S5K3 虾丸子大人",
            remark_code="CJR8S5K3",
            authorization_revision="rev-1",
        )
        payload = build_message_ingest_payload(
            target,
            {
                "ok": True,
                "observation_schema_version": 3,
                "observations": [
                    {
                        "schema_version": 3,
                        "observation_id": "voice-placeholder",
                        "row_kind": "voice_bubble",
                        "sender_role": "self",
                        "sender_role_source": "same_row_avatar",
                        "message_type": "voice",
                        "voice_state": "untranscribed",
                        "content_clean": "",
                        "source_message": {"id": "placeholder", "content": '[语音] 4" (c'},
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
        self.assertEqual([(item["message_type"], item["sender_role_hint"], item["content"]) for item in payload["messages"]], [
            ("text", "customer", "普通文字"),
            ("voice", "self", "我马上回去。"),
        ])

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

        first = build_message_ingest_payload(target, sidecar_payload(top=293, prefix=True))
        shifted = build_message_ingest_payload(target, sidecar_payload(top=173, prefix=False))

        first_item = next(item for item in first["messages"] if item["content"] == "哦")
        shifted_item = next(item for item in shifted["messages"] if item["content"] == "哦")
        self.assertEqual(first_item["dedupe_key"], shifted_item["dedupe_key"])
        self.assertEqual(first_item["raw_payload"]["dedupe_basis"]["occurrence_index"], 0)
        self.assertEqual(shifted_item["raw_payload"]["dedupe_basis"]["occurrence_index"], 0)

    def test_v3_skips_non_ingestible_call_event_observation(self):
        target = WechatReadTarget(
            conversation_id="conv-1",
            rpa_session_key="wx:rpa:v1:a",
            display_name="CJR8S5K3 虾丸子大人",
            remark_code="CJR8S5K3",
            authorization_revision="rev-1",
        )
        payload = build_message_ingest_payload(
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
        payload = build_message_ingest_payload(
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

    def test_v3_rejects_payload_build_without_authorization_revision(self):
        target = WechatReadTarget(
            conversation_id="conv-1",
            rpa_session_key="wx:rpa:v1:a",
            display_name="CJR8S5K3 虾丸子大人",
            remark_code="CJR8S5K3",
        )

        with self.assertRaisesRegex(ValueError, "C2_TARGET_AUTHORIZATION_REVISION_MISSING"):
            build_message_ingest_payload(
                target,
                {
                    "ok": True,
                    "observation_schema_version": 3,
                    "observations": [],
                },
            )


if __name__ == "__main__":
    unittest.main()
