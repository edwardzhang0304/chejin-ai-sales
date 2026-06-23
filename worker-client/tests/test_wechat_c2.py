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

    def test_message_payload_uses_stable_dedupe_key(self):
        target = WechatReadTarget(conversation_id="conv-1", rpa_session_key="wx:rpa:v1:a", display_name="王先生")
        sidecar = {
            "ok": True,
            "screenshot_path": "C:/message.png",
            "messages": [
                {"id": "win32_ocr:abc", "sender_role": "unknown", "type": "text", "content": "你好", "ocr_confidence": 0.91}
            ],
        }

        payload = build_message_ingest_payload(target, sidecar)

        self.assertEqual(payload["conversation_id"], "conv-1")
        self.assertEqual(payload["rpa_session_key"], "wx:rpa:v1:a")
        self.assertEqual(payload["messages"][0]["dedupe_key"], "conv-1:win32_ocr:abc")
        self.assertEqual(payload["messages"][0]["sender_role_hint"], "unknown")
        self.assertEqual(payload["messages"][0]["message_type"], "text")

    def test_message_dedupe_key_ignores_rpa_session_key_changes(self):
        message = {"id": "win32_ocr:abc", "sender_role": "customer", "type": "text", "content": "你好"}
        payload_a = build_message_ingest_payload(
            WechatReadTarget(conversation_id="conv-1", rpa_session_key="wx:rpa:v1:a", display_name="CJTEST01 许聪"),
            {"ok": True, "messages": [message]},
        )
        payload_b = build_message_ingest_payload(
            WechatReadTarget(conversation_id="conv-1", rpa_session_key="wx:rpa:v1:b", display_name="CJTEST01许聪"),
            {"ok": True, "messages": [message]},
        )

        self.assertEqual(payload_a["messages"][0]["dedupe_key"], payload_b["messages"][0]["dedupe_key"])

    def test_extract_remark_codes_supports_manual_suffix(self):
        self.assertEqual(extract_remark_codes("CJ8K2P 王先生想看轩逸"), ["CJ8K2P"])
        self.assertEqual(extract_remark_codes("CJTEST01 许聪", "CJTEST01许聪"), ["CJTEST01"])


if __name__ == "__main__":
    unittest.main()
