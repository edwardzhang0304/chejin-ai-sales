from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

os.environ.setdefault("CHEJIN_WORKER_HOME", tempfile.mkdtemp(prefix="chejin-worker-session-test-"))
os.environ.setdefault("CHEJIN_RPA_MODE", "mock")

REPO_ROOT = Path(__file__).resolve().parents[2]
OMNIAUTO_ROOT = REPO_ROOT / "worker-client" / "omniauto-rpa"
if str(OMNIAUTO_ROOT) not in sys.path:
    sys.path.insert(0, str(OMNIAUTO_ROOT))

from apps.wechat_ai_customer_service.adapters import wechat_win32_ocr_sidecar as sidecar
from chejin_worker_client.wechat_c2 import build_scan_result_payload


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
    @staticmethod
    def strict_visible_candidate(frame_evidence: dict) -> dict:
        bound_evidence = dict(frame_evidence)
        bound_evidence.setdefault(
            "candidate_remark_code_candidates", ["CJP6M3R7"]
        )
        bound_evidence.setdefault("candidate_session_key", "session-1")
        bound_evidence.setdefault("ocr_result_sha256", "f" * 64)
        bound_evidence.setdefault(
            "candidate_bounds", [120.0, 112.0, 330.0, 144.0]
        )
        return {
            "name": "CJP6M3R7许聪",
            "session_key": "session-1",
            "center_y": 128.0,
            "left": 120.0,
            "right": 330.0,
            "top": 112.0,
            "bottom": 144.0,
            "c2_conversation_type": "private",
            "c2_remark_code_candidates": ["CJP6M3R7"],
            "c2_conversation_admission": {
                "conversation_type": "private",
                "admission_allowed": True,
                "remark_code": "CJP6M3R7",
            },
            "visible_frame_reuse_evidence": bound_evidence,
        }

    def test_visible_candidate_reuses_only_pixel_identical_fresh_frame(self):
        frame = Image.new("RGB", (980, 860), "white")
        geometry = {
            "left": 10,
            "top": 20,
            "right": 990,
            "bottom": 880,
            "width": 980,
            "height": 860,
        }
        with patch.object(sidecar, "window_dpi_scale", return_value=1.25):
            evidence = sidecar.immutable_frame_pixel_evidence(
                frame,
                hwnd=101,
                geometry=geometry,
                screenshot_path="scan.png",
            )
        evidence.update(
            {
                "scan_id": "scan-1",
                "sidecar_run_id": "sessions-1",
            }
        )
        validation = {
            "ok": True,
            "confirmation_confidence": "active_title_strict",
            "geometry": geometry,
        }
        with (
            patch.object(sidecar, "get_window_geometry", return_value=geometry),
            patch.object(sidecar, "window_dpi_scale", return_value=1.25),
            patch.object(
                sidecar,
                "capture_wechat",
                return_value=(frame.copy(), "fresh.png"),
            ),
            patch.object(
                sidecar, "activate_session_candidate", return_value=True
            ) as activate,
            patch.object(
                sidecar,
                "consume_recent_target_switch_validation",
                return_value=validation,
            ),
        ):
            result = sidecar.try_activate_visible_candidate_from_equivalent_frame(
                101,
                candidate=self.strict_visible_candidate(evidence),
                remark_code="CJP6M3R7",
                artifact_dir=None,
            )

        self.assertTrue(result["fast_path_attempted"])
        self.assertTrue(result["fast_path_used"])
        self.assertTrue(result["frame_digest_equal"])
        activate.assert_called_once()

    def test_visible_candidate_frame_change_falls_back_before_click(self):
        old_frame = Image.new("RGB", (980, 860), "white")
        changed_frame = old_frame.copy()
        changed_frame.putpixel((100, 100), (0, 0, 0))
        geometry = {
            "left": 10,
            "top": 20,
            "right": 990,
            "bottom": 880,
            "width": 980,
            "height": 860,
        }
        with patch.object(sidecar, "window_dpi_scale", return_value=1.25):
            evidence = sidecar.immutable_frame_pixel_evidence(
                old_frame,
                hwnd=101,
                geometry=geometry,
            )
        evidence.update(
            {
                "scan_id": "scan-1",
                "sidecar_run_id": "sessions-1",
            }
        )
        with (
            patch.object(sidecar, "get_window_geometry", return_value=geometry),
            patch.object(sidecar, "window_dpi_scale", return_value=1.25),
            patch.object(
                sidecar,
                "capture_wechat",
                return_value=(changed_frame, "fresh.png"),
            ),
            patch.object(sidecar, "activate_session_candidate") as activate,
        ):
            result = sidecar.try_activate_visible_candidate_from_equivalent_frame(
                101,
                candidate=self.strict_visible_candidate(evidence),
                remark_code="CJP6M3R7",
                artifact_dir=None,
            )

        self.assertTrue(result["fast_path_attempted"])
        self.assertFalse(result["fast_path_used"])
        self.assertEqual(result["fallback_reason"], "frame_digest_changed")
        activate.assert_not_called()

    def test_visible_candidate_chat_view_change_keeps_sidebar_fast_path(self):
        old_frame = Image.new("RGB", (980, 860), "white")
        changed_frame = old_frame.copy()
        changed_frame.putpixel((800, 400), (0, 0, 0))
        geometry = {
            "left": 10,
            "top": 20,
            "right": 990,
            "bottom": 880,
            "width": 980,
            "height": 860,
        }
        with patch.object(sidecar, "window_dpi_scale", return_value=1.25):
            evidence = sidecar.immutable_frame_pixel_evidence(
                old_frame, hwnd=101, geometry=geometry
            )
        evidence.update({"scan_id": "scan-1", "sidecar_run_id": "sessions-1"})
        validation = {
            "ok": True,
            "confirmation_confidence": "active_title_strict",
            "geometry": geometry,
        }
        with (
            patch.object(sidecar, "get_window_geometry", return_value=geometry),
            patch.object(sidecar, "window_dpi_scale", return_value=1.25),
            patch.object(
                sidecar,
                "capture_wechat",
                return_value=(changed_frame, "fresh.png"),
            ),
            patch.object(
                sidecar, "activate_session_candidate", return_value=True
            ) as activate,
            patch.object(
                sidecar,
                "consume_recent_target_switch_validation",
                return_value=validation,
            ),
        ):
            result = sidecar.try_activate_visible_candidate_from_equivalent_frame(
                101,
                candidate=self.strict_visible_candidate(evidence),
                remark_code="CJP6M3R7",
                artifact_dir=None,
            )

        self.assertTrue(result["frame_digest_equal"])
        self.assertFalse(result["full_frame_digest_equal"])
        self.assertTrue(result["fast_path_used"])
        activate.assert_called_once()

    def test_equal_pixels_at_different_timepoints_get_distinct_frame_ids(self):
        frame = Image.new("RGB", (980, 860), "white")
        geometry = {"width": 980, "height": 860}
        with patch.object(sidecar, "window_dpi_scale", return_value=1.0):
            first = sidecar.immutable_frame_pixel_evidence(
                frame, hwnd=101, geometry=geometry
            )
            second = sidecar.immutable_frame_pixel_evidence(
                frame, hwnd=101, geometry=geometry
            )

        self.assertEqual(
            first["screenshot_sha256"], second["screenshot_sha256"]
        )
        self.assertNotEqual(first["frame_id"], second["frame_id"])

    def test_visible_candidate_metadata_changes_fall_back_before_capture_or_click(self):
        frame = Image.new("RGB", (980, 860), "white")
        geometry = {
            "left": 10,
            "top": 20,
            "right": 990,
            "bottom": 880,
            "width": 980,
            "height": 860,
        }
        with patch.object(sidecar, "window_dpi_scale", return_value=1.25):
            base = sidecar.immutable_frame_pixel_evidence(
                frame, hwnd=101, geometry=geometry
            )
        base.update({"scan_id": "scan-1", "sidecar_run_id": "sessions-1"})

        cases = (
            ("hwnd_changed", {**base, "hwnd": 102}),
            (
                "geometry_changed",
                {
                    **base,
                    "geometry": {**geometry, "left": 11},
                },
            ),
            ("dpi_changed", {**base, "dpi_scale": 1.5}),
        )
        for expected_reason, evidence in cases:
            with self.subTest(expected_reason=expected_reason):
                with (
                    patch.object(sidecar, "get_window_geometry", return_value=geometry),
                    patch.object(sidecar, "window_dpi_scale", return_value=1.25),
                    patch.object(sidecar, "capture_wechat") as capture,
                    patch.object(sidecar, "activate_session_candidate") as activate,
                ):
                    result = sidecar.try_activate_visible_candidate_from_equivalent_frame(
                        101,
                        candidate=self.strict_visible_candidate(evidence),
                        remark_code="CJP6M3R7",
                        artifact_dir=None,
                    )

                self.assertFalse(result["fast_path_used"])
                self.assertEqual(result["fallback_reason"], expected_reason)
                capture.assert_not_called()
                activate.assert_not_called()

    def test_visible_candidate_feature_off_preserves_original_fallback(self):
        with (
            patch.dict(
                os.environ,
                {"CHEJIN_C2_LOCATE_FRAME_REUSE_ENABLED": "0"},
            ),
            patch.object(sidecar, "capture_wechat") as capture,
            patch.object(sidecar, "activate_session_candidate") as activate,
        ):
            result = sidecar.try_activate_visible_candidate_from_equivalent_frame(
                101,
                candidate={},
                remark_code="CJP6M3R7",
                artifact_dir=None,
            )

        self.assertFalse(result["fast_path_attempted"])
        self.assertFalse(result["fast_path_used"])
        self.assertEqual(result["fallback_reason"], "feature_disabled")
        capture.assert_not_called()
        activate.assert_not_called()

    def test_sessions_payload_binds_frame_evidence_to_each_candidate_row(self):
        frame = Image.new("RGB", (980, 860), "white")
        geometry = {
            "left": 10,
            "top": 20,
            "right": 990,
            "bottom": 880,
            "width": 980,
            "height": 860,
        }
        session = self.strict_visible_candidate({})
        session.pop("visible_frame_reuse_evidence", None)
        session["c2_remark_code_candidates"] = ["CJP6M3R7"]
        with (
            patch.object(
                sidecar,
                "capture_wechat",
                return_value=(frame, "sessions.png"),
            ),
            patch.object(sidecar, "run_ocr", return_value=[]),
            patch.object(sidecar, "session_list_ocr_items", return_value=([], 0)),
            patch.object(sidecar, "get_window_geometry", return_value=geometry),
            patch.object(sidecar, "quick_login_like", return_value=False),
            patch.object(sidecar, "blocking_screen_reason", return_value=""),
            patch.object(
                sidecar, "parse_sessions_from_ocr", return_value=[session]
            ),
            patch.object(sidecar, "window_dpi_scale", return_value=1.25),
        ):
            payload = sidecar.sessions_payload(
                101,
                {"ok": True},
                scan_id="scan-1",
                sidecar_run_id="sessions-1",
            )

        evidence = payload["sessions"][0]["visible_frame_reuse_evidence"]
        self.assertEqual(evidence["scan_id"], "scan-1")
        self.assertEqual(evidence["sidecar_run_id"], "sessions-1")
        self.assertEqual(evidence["candidate_session_key"], "session-1")
        self.assertEqual(
            evidence["candidate_remark_code_candidates"], ["CJP6M3R7"]
        )
        self.assertEqual(
            evidence["candidate_bounds"], [120.0, 112.0, 330.0, 144.0]
        )

    def test_daemon_sessions_request_preserves_scan_identity(self):
        argv = sidecar.args_for_daemon_request(
            {
                "action": "sessions",
                "sidecar_run_id": "sessions-1",
                "scan_id": "scan-1",
            }
        )

        self.assertEqual(
            argv[argv.index("--sidecar-run-id") + 1], "sessions-1"
        )
        self.assertEqual(argv[argv.index("--scan-id") + 1], "scan-1")

    def test_preview_short_code_cannot_impersonate_another_row_title(self):
        sessions = sidecar.parse_sessions_from_ocr(
            [
                ocr_item("AI共创", 224.0),
                ocr_item(
                    "CJV6P3R8许聪：seedance 太...",
                    253.0,
                    enhanced=True,
                ),
                ocr_item("CJV6P3R8许聪", 548.0),
                ocr_item("额度不够下 claude，我来上...", 577.0, enhanced=True),
            ],
            (966, 854),
        )

        self.assertEqual(len(sessions), 2)
        self.assertEqual(sessions[0]["name"], "AI共创")
        self.assertEqual(sessions[0]["c2_remark_code_candidates"], [])
        self.assertIn("CJV6P3R8", sessions[0]["preview"])
        self.assertEqual(sessions[1]["name"], "CJV6P3R8许聪")
        self.assertEqual(
            sessions[1]["c2_remark_code_candidates"],
            ["CJV6P3R8"],
        )
        scan_payload = build_scan_result_payload({"ok": True, "sessions": sessions})
        admitted = [
            item
            for item in scan_payload["sessions"]
            if item["remark_code_candidates"] == ["CJV6P3R8"]
        ]
        self.assertEqual(len(admitted), 1)
        self.assertEqual(admitted[0]["display_name"], "CJV6P3R8许聪")

    def test_filtered_title_above_short_code_preview_still_fails_closed(self):
        for reverse in (False, True):
            with self.subTest(reverse=reverse):
                items = [
                    ocr_item("普通客户名字被截断...", 128.0),
                    ocr_item("请联系CJFAKE23处理...", 150.0, enhanced=True),
                ]
                if reverse:
                    items.reverse()

                sessions = sidecar.parse_sessions_from_ocr(items, (980, 860))

                self.assertEqual(len(sessions), 1)
                self.assertEqual(sessions[0]["name"], "普通客户名字被截断...")
                self.assertEqual(sessions[0]["c2_remark_code_candidates"], [])
                self.assertEqual(sessions[0]["conversation_type"], "unknown")

    def test_different_code_in_preview_does_not_override_title_identity(self):
        sessions = sidecar.parse_sessions_from_ocr(
            [
                ocr_item("CJV6P3R8许聪", 128.0),
                ocr_item("请联系CJFAKE23处理...", 154.0, enhanced=True),
            ],
            (980, 860),
        )

        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0]["c2_remark_code_candidates"], ["CJV6P3R8"])
        self.assertEqual(sessions[0]["conversation_type"], "private")

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
