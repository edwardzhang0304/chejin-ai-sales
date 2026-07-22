from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path


os.environ.setdefault("CHEJIN_WORKER_HOME", tempfile.mkdtemp(prefix="chejin-worker-session-test-"))
os.environ.setdefault("CHEJIN_RPA_MODE", "mock")

REPO_ROOT = Path(__file__).resolve().parents[2]
OMNIAUTO_ROOT = REPO_ROOT / "worker-client" / "omniauto-rpa"
if str(OMNIAUTO_ROOT) not in sys.path:
    sys.path.insert(0, str(OMNIAUTO_ROOT))

from apps.wechat_ai_customer_service.adapters import wechat_win32_ocr_sidecar as sidecar


def ocr_item(text: str, center_y: float, *, enhanced: bool = False) -> dict:
    item = {
        "text": text,
        "left": 154.0,
        "right": 320.0,
        "top": center_y - 11.0,
        "bottom": center_y + 11.0,
        "center_x": 237.0,
        "center_y": center_y,
        "confidence": 0.98,
    }
    if enhanced:
        item["ocr_source"] = "sidebar_visible_list_enhanced"
    return item


class WechatWin32OcrSessionRowTest(unittest.TestCase):
    def test_short_code_title_wins_over_same_row_pollution_regardless_of_order_or_offset(self):
        for pollution in ("2", "您好，什么时候方便"):
            for pollution_y in (120.0, 136.0):
                for reverse in (False, True):
                    with self.subTest(pollution=pollution, pollution_y=pollution_y, reverse=reverse):
                        items = [
                            ocr_item(pollution, pollution_y, enhanced=True),
                            ocr_item("张三-CJWIN01", 128.0),
                        ]
                        if reverse:
                            items.reverse()

                        sessions = sidecar.parse_sessions_from_ocr(items, (980, 860))

                        self.assertEqual(len(sessions), 1)
                        self.assertEqual(sessions[0]["name"], "张三-CJWIN01")
                        self.assertEqual(sessions[0]["c2_remark_code_candidates"], ["CJWIN01"])
                        self.assertEqual(sessions[0]["conversation_type"], "private")

    def test_normal_and_enhanced_same_short_code_title_merge_into_one_session(self):
        sessions = sidecar.parse_sessions_from_ocr(
            [
                ocr_item("张三-CJWIN01", 128.0),
                ocr_item("张三-CJWIN01", 126.0, enhanced=True),
            ],
            (980, 860),
        )

        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0]["c2_remark_code_candidates"], ["CJWIN01"])

    def test_different_short_codes_in_same_visual_row_are_not_admitted(self):
        sessions = sidecar.parse_sessions_from_ocr(
            [
                ocr_item("张三-CJWIN01", 128.0),
                ocr_item("李四-CJWIN02", 126.0, enhanced=True),
            ],
            (980, 860),
        )

        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0]["c2_remark_code_candidates"], [])
        self.assertEqual(sessions[0]["conversation_type"], "unknown")
        self.assertEqual(
            sessions[0]["c2_conversation_admission"]["reason"],
            "multiple_remark_codes_in_visual_row",
        )

    def test_group_suffix_evidence_wins_over_private_duplicate(self):
        sessions = sidecar.parse_sessions_from_ocr(
            [
                ocr_item("销售讨论-CJWIN01", 128.0),
                ocr_item("销售讨论-CJWIN01（6）", 126.0, enhanced=True),
            ],
            (980, 860),
        )

        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0]["conversation_type"], "group")
        self.assertEqual(sessions[0]["c2_remark_code_candidates"], [])


if __name__ == "__main__":
    unittest.main()
