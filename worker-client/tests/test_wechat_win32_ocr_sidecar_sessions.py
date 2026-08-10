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
    def test_short_code_identity_precedes_all_display_suffix_heuristics(self):
        cases = (
            ("CJN8Q4K2 虾丸子...", "CJN8Q4K2"),
            ("CJP6M3R7许聪…", "CJP6M3R7"),
            ("CJAB12CD 任意姓名......", "CJAB12CD"),
            ("CJXY9876 名字...12:22", "CJXY9876"),
        )

        for raw_title, expected_code in cases:
            with self.subTest(raw_title=raw_title):
                sessions = sidecar.parse_sessions_from_ocr(
                    [ocr_item(raw_title, 128.0)],
                    (980, 860),
                )

                self.assertEqual(len(sessions), 1)
                self.assertEqual(
                    sessions[0]["c2_remark_code_candidates"],
                    [expected_code],
                )
                self.assertEqual(sessions[0]["conversation_type"], "private")
                self.assertTrue(
                    sessions[0]["c2_conversation_admission"]["admission_allowed"]
                )

    def test_two_visible_truncated_short_code_titles_both_survive(self):
        sessions = sidecar.parse_sessions_from_ocr(
            [
                ocr_item("CJN8Q4K2 虾丸子...", 129.5),
                ocr_item("今天把雨都下完吧", 155.5, enhanced=True),
                ocr_item("CJP6M3R7许聪", 210.5),
                ocr_item("嗯，是的", 236.0, enhanced=True),
            ],
            (980, 860),
        )

        self.assertEqual(
            [item["c2_remark_code_candidates"] for item in sessions],
            [["CJN8Q4K2"], ["CJP6M3R7"]],
        )

    def test_short_code_with_glued_ellipsis_time_suffix_is_admitted(self):
        sessions = sidecar.parse_sessions_from_ocr(
            [ocr_item("CJR8S5K3虾丸子大...11:05", 128.0)],
            (980, 860),
        )

        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0]["name"], "CJR8S5K3虾丸子大")
        self.assertEqual(
            sessions[0]["c2_remark_code_candidates"],
            ["CJR8S5K3"],
        )
        self.assertEqual(sessions[0]["conversation_type"], "private")

    def test_fixed_short_code_does_not_consume_ascii_or_time_suffixes(self):
        cases = (
            ("CJP6M3R7Alice...", "CJP6M3R7"),
            ("CJP6M3R7-VIP...", "CJP6M3R7"),
            ("AliceCJP6M3R7...", "CJP6M3R7"),
            ("CJP6M3R712:22", "CJP6M3R7"),
        )

        for raw_title, expected in cases:
            with self.subTest(raw_title=raw_title):
                self.assertEqual(sidecar.extract_c2_remark_codes(raw_title), [expected])

    def test_numeric_unread_badge_never_replaces_short_code_title(self):
        for pollution_y in (120.0, 136.0):
            for reverse in (False, True):
                with self.subTest(pollution_y=pollution_y, reverse=reverse):
                    items = [
                        ocr_item("2", pollution_y, enhanced=True),
                        ocr_item("张三-CJWIN012", 128.0),
                    ]
                    if reverse:
                        items.reverse()

                    sessions = sidecar.parse_sessions_from_ocr(items, (980, 860))

                    self.assertEqual(len(sessions), 1)
                    self.assertEqual(sessions[0]["name"], "张三-CJWIN012")
                    self.assertEqual(sessions[0]["c2_remark_code_candidates"], ["CJWIN012"])
                    self.assertEqual(sessions[0]["conversation_type"], "private")

    def test_normal_and_enhanced_same_short_code_title_merge_into_one_session(self):
        sessions = sidecar.parse_sessions_from_ocr(
            [
                ocr_item("张三-CJWIN012", 128.0),
                ocr_item("张三-CJWIN012", 126.0, enhanced=True),
            ],
            (980, 860),
        )

        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0]["c2_remark_code_candidates"], ["CJWIN012"])

    def test_different_short_codes_in_same_visual_row_are_not_admitted(self):
        sessions = sidecar.parse_sessions_from_ocr(
            [
                ocr_item("张三-CJWIN012", 128.0),
                ocr_item("李四-CJWIN023", 126.0, enhanced=True),
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
                ocr_item("销售讨论-CJWIN012", 128.0),
                ocr_item("销售讨论-CJWIN012（6）", 126.0, enhanced=True),
            ],
            (980, 860),
        )

        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0]["conversation_type"], "group")
        self.assertEqual(sessions[0]["c2_remark_code_candidates"], [])


if __name__ == "__main__":
    unittest.main()
