from __future__ import annotations

import inspect
import hashlib
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
import tempfile
import time
from unittest.mock import patch

from PIL import Image, ImageDraw


OMNIAUTO_ROOT = Path(__file__).resolve().parents[1] / "omniauto-rpa"
if str(OMNIAUTO_ROOT) not in sys.path:
    sys.path.insert(0, str(OMNIAUTO_ROOT))

from apps.wechat_ai_customer_service.adapters import wechat_win32_ocr_sidecar as sidecar
from apps.wechat_ai_customer_service.adapters import message_viewport_projection
from chejin_worker_client.message_viewport_projection import (
    boundary_tokens_for_observations,
)


INCIDENT_LONG_REPLY = (
    "可以，10万左右可以按你的需求帮你筛选合适的二手车。"
    "你主要家用、通勤还是跑长途，更偏轿车还是SUV，"
    "对空间、油耗或能源类型有要求吗？"
)


def incident_post_send_enhanced_ocr_items() -> list[dict[str, object]]:
    """Exact seven OCR records captured from the 2026-08-15 incident."""

    texts = [
        "可以，10万左右可以按你的需求帮你筛选合适",
        "的二手车。",
        "你主要家用、",
        "通勤还是跑长途，更",
        "偏轿车还是SUV，对空间、",
        "油耗或能源类型有",
        "要求吗?",
    ]
    boxes = [
        (505.0, 552.0, 859.5, 571.5),
        (506.5, 577.0, 601.0, 594.5),
        (597.0, 577.0, 708.0, 594.5),
        (701.0, 578.0, 860.5, 594.5),
        (505.5, 600.5, 705.5, 619.0),
        (714.0, 601.0, 855.0, 619.5),
        (505.5, 623.5, 571.5, 642.5),
    ]
    confidences = [0.9969, 0.9204, 0.9975, 0.9965, 0.9760, 0.9988, 0.9276]
    return [
        {
            "text": text,
            "left": left,
            "top": top,
            "right": right,
            "bottom": bottom,
            "center_x": (left + right) / 2,
            "center_y": (top + bottom) / 2,
            "confidence": confidence,
        }
        for text, (left, top, right, bottom), confidence in zip(
            texts,
            boxes,
            confidences,
            strict=True,
        )
    ]


class WechatSendSafetyTest(unittest.TestCase):
    def _worker_send_guard(
        self,
        guard: dict,
        observations: list[dict],
        *,
        empty_top_boundary: bool = False,
    ) -> dict:
        expected = dict(guard)
        tokens = boundary_tokens_for_observations(
            observations,
            committed_only=False,
        )
        expected["worker_continuity_contract"] = {
            "schema_version": 1,
            "comparator": "compare_business_viewport_continuity",
            "old_boundary_tokens": {
                str(index): sorted(values)
                for index, values in tokens.items()
                if values
            },
            "old_top_boundary_complete": bool(empty_top_boundary),
        }
        return expected

    def _validate_worker_send_context(
        self,
        expected_guard: dict,
        current_guard: dict,
        *,
        expected_observations: list[dict],
        current_observations: list[dict],
        empty_top_boundary: bool = False,
    ) -> dict:
        """Exercise Sidecar through the serialized sole-Worker contract."""

        expected = self._worker_send_guard(
            expected_guard,
            expected_observations,
            empty_top_boundary=empty_top_boundary,
        )
        return sidecar.validate_send_context_guard(
            expected,
            current_guard,
            current_observations=current_observations,
        )

    def setUp(self) -> None:
        super().setUp()
        self._semantic_layouts: dict[int, dict] = {}
        self._latest_semantic_layout: dict | None = None
        self._layout_for_image_patch = patch.object(
            sidecar,
            "layout_snapshot_for_image",
            side_effect=self._semantic_layout_for_image,
        )
        self._current_layout_patch = patch.object(
            sidecar,
            "current_layout_snapshot",
            side_effect=lambda _hwnd: self._latest_semantic_layout,
        )
        self._layout_for_image_patch.start()
        self._current_layout_patch.start()
        self.addCleanup(self._layout_for_image_patch.stop)
        self.addCleanup(self._current_layout_patch.stop)

    def test_sidecar_uses_the_single_shared_business_projection_object(self):
        self.assertIs(
            sidecar.normalized_business_message_sequence,
            message_viewport_projection.normalized_business_message_sequence,
        )

    def _semantic_layout_for_image(self, image: Image.Image) -> dict:
        """Production snapshot shape for send semantics outside layout tests."""
        key = id(image)
        cached = self._semantic_layouts.get(key)
        if cached is not None:
            self._latest_semantic_layout = cached
            return cached
        width, height = [int(value or 0) for value in image.size]
        sidebar_right = min(max(int(round(width * 0.39)), 300), max(301, width - 420))
        header_bottom = min(max(int(round(height * 0.10)), 70), 110)
        input_top = max(header_bottom + 120, int(round(height * 0.79)))
        input_panel_width = width - sidebar_right
        input_panel_height = height - input_top
        snapshot = sidecar.win32_ocr_layout.build_layout_snapshot(
            hwnd=1,
            frame_id=f"send-semantic-frame-{key}",
            capture_mode=sidecar.win32_ocr_layout.CAPTURE_MODE_WINDOW_VISIBLE_SCREEN,
            image_size=image.size,
            capture_screen_origin=[0, 0],
            window_rect=[0, 0, width, height],
            client_rect=[0, 0, width, height],
            client_screen_origin=[0, 0],
            dpi_scale=1.0,
            regions={
                "left_nav_bounds": [0, 0, 75, height],
                "sidebar_bounds": [75, 0, sidebar_right, height],
                "sidebar_header_bounds": [75, 0, sidebar_right, header_bottom],
                "session_list_bounds": [75, header_bottom, sidebar_right, height],
                "chat_header_bounds": [sidebar_right, 0, width, header_bottom],
                "message_viewport_bounds": [sidebar_right, header_bottom, width, input_top],
                "toolbar_bounds": [
                    sidebar_right,
                    input_top + int(input_panel_height * 0.56),
                    width,
                    height,
                ],
                "input_bounds": [
                    sidebar_right + max(1, int(input_panel_width * 0.01)),
                    input_top + max(1, int(input_panel_height * 0.04)),
                    width - max(1, int(input_panel_width * 0.16)),
                    input_top + int(input_panel_height * 0.56),
                ],
            },
            anchors=[],
            confidence=1.0,
            conflicts=[],
            executable=True,
        )
        self._semantic_layouts[key] = snapshot
        self._latest_semantic_layout = snapshot
        return snapshot

    def test_chat_fact_roi_ocr_reads_only_three_calibrated_regions(self):
        frame = Image.new("RGB", (980, 860), "white")
        layout = self._semantic_layout_for_image(frame)
        expected_sizes = {
            tuple(
                sidecar.win32_ocr_layout.required_region(layout, name)[2 + index]
                - sidecar.win32_ocr_layout.required_region(layout, name)[index]
                for index in (0, 1)
            )
            for name in (
                "chat_header_bounds",
                "message_viewport_bounds",
                "input_bounds",
            )
        }
        observed_sizes: list[tuple[int, int]] = []

        def raw_ocr(image):
            observed_sizes.append(tuple(image.size))
            return [
                {
                    "text": f"roi-{len(observed_sizes)}",
                    "left": 2,
                    "top": 3,
                    "right": 22,
                    "bottom": 13,
                    "center_x": 12,
                    "center_y": 8,
                    "confidence": 0.99,
                }
            ]

        with patch.object(sidecar, "run_ocr", side_effect=raw_ocr):
            items, plan = sidecar.run_ocr_for_chat_fact_frame(
                frame,
                purpose="unit_chat_fact",
                source="unit",
                enabled=True,
            )

        self.assertEqual(set(observed_sizes), expected_sizes)
        self.assertNotIn(frame.size, observed_sizes)
        self.assertEqual(plan["source"], "chat_fact_roi")
        self.assertEqual(plan["ocr_call_count"], 3)
        self.assertEqual(
            {item["ocr_region_name"] for item in items},
            {
                "chat_header_bounds",
                "message_viewport_bounds",
                "input_bounds",
            },
        )
        for item in items:
            bounds = sidecar.win32_ocr_layout.required_region(
                layout,
                item["ocr_region_name"],
            )
            self.assertGreaterEqual(item["left"], bounds[0])
            self.assertGreaterEqual(item["top"], bounds[1])

    def test_formal_reuse_switches_restore_full_frame_ocr_paths(self):
        frame = Image.new("RGB", (980, 860), "white")
        self._semantic_layout_for_image(frame)
        observed_sizes: list[tuple[int, int]] = []

        def raw_ocr(image):
            observed_sizes.append(tuple(image.size))
            return []

        geometry = {
            "left": 0,
            "top": 0,
            "right": 980,
            "bottom": 860,
            "width": 980,
            "height": 860,
        }
        validation = {
            "ok": True,
            "online": True,
            "confirmation_confidence": "active_title_strict",
        }
        with (
            patch.dict(
                os.environ,
                {
                    "CHEJIN_C2_LOCATE_FRAME_REUSE_ENABLED": "0",
                    "CHEJIN_C3_PRE_SEND_ROI_REUSE_ENABLED": "0",
                    "CHEJIN_C3_SEND_FRAME_LOCAL_REUSE_ENABLED": "0",
                },
            ),
            patch.object(
                sidecar,
                "capture_wechat",
                return_value=(frame, "formal-switch-off.png"),
            ),
            patch.object(sidecar, "run_ocr", side_effect=raw_ocr),
            patch.object(sidecar, "get_window_geometry", return_value=geometry),
            patch.object(
                sidecar,
                "validate_active_send_target",
                return_value=validation,
            ),
            patch.object(
                sidecar,
                "active_send_guard_is_strong",
                return_value=True,
            ),
            patch.object(
                sidecar,
                "run_ocr_for_chat_fact_frame",
                wraps=sidecar.run_ocr_for_chat_fact_frame,
            ) as chat_roi,
        ):
            history = sidecar.capture_message_history_snapshots(
                1,
                target="CJTEST01",
                history_load_times=0,
                chat_fact_roi_ocr=True,
            )
            send_snapshot = sidecar.build_send_fact_snapshot_from_frame(
                1,
                target="CJTEST01",
                text="回复",
                exact=False,
                artifact_dir=None,
                label="formal_send_switch_off",
                screenshot=frame,
                screenshot_path="formal-switch-off.png",
            )
            _search_items, search_plan = (
                sidecar.run_ocr_for_sidebar_search_results(
                    frame,
                    purpose="formal_locate_switch_off",
                    source="unit",
                )
            )

        self.assertEqual(history[0]["ocr_plan"]["source"], "full")
        self.assertEqual(send_snapshot["ocr_plan"]["source"], "full")
        self.assertEqual(search_plan["source"], "full")
        self.assertEqual(observed_sizes, [frame.size, frame.size, frame.size])
        chat_roi.assert_not_called()
        self.assertNotIn(
            "WECHAT_WIN32_OCR_CHAT_FACT_ROI_OCR",
            inspect.getsource(sidecar),
        )
        self.assertNotIn(
            "WECHAT_WIN32_OCR_SEARCH_RESULT_ROI_OCR",
            inspect.getsource(sidecar),
        )

    def test_visible_miss_reuses_one_baseline_for_remark_search(self):
        baseline = Image.new("RGB", (980, 860), (255, 255, 255))
        search_results = Image.new("RGB", (980, 860), (240, 240, 240))
        confirmed_chat = Image.new("RGB", (980, 860), (220, 255, 220))
        for image in (baseline, search_results, confirmed_chat):
            self._semantic_layout_for_image(image)
        geometry = {
            "left": 0,
            "top": 0,
            "right": 980,
            "bottom": 860,
            "width": 980,
            "height": 860,
        }
        captures: list[str] = []
        clicks: list[tuple[int, int]] = []

        def row(text, left, top, right, bottom):
            return {
                "text": text,
                "left": left,
                "top": top,
                "right": right,
                "bottom": bottom,
                "center_x": (left + right) / 2,
                "center_y": (top + bottom) / 2,
                "confidence": 0.99,
            }

        def capture(_hwnd, *, artifact_dir=None, label="capture"):
            del artifact_dir
            captures.append(label)
            if label.startswith("open_chat"):
                return baseline, f"{label}.png"
            return confirmed_chat, f"{label}.png"

        def raw_ocr(image):
            pixel = image.convert("RGB").getpixel((0, 0))
            width, _height = image.size
            if pixel == (255, 255, 255):
                left = 75 if width == 980 else 12
                return [row("搜索", left, 35, left + 60, 60)]
            if pixel == (240, 240, 240):
                # Sidebar ROI coordinates are local and production code maps
                # them back into the full screenshot before selecting a row.
                return [
                    row("联系人", 20, 112, 80, 136),
                    row("CJTEST01 张三", 24, 166, 180, 194),
                ]
            return [
                row("CJTEST01 张三", 470, 28, 650, 58),
                row("发送", 850, 790, 915, 825),
            ]

        with (
            patch.object(sidecar, "capture_wechat", side_effect=capture),
            patch.object(
                sidecar,
                "capture_wechat_window_visible_screen",
                return_value=(search_results, "search-results.png"),
            ),
            patch.object(sidecar, "run_ocr", side_effect=raw_ocr),
            patch.object(sidecar, "get_window_geometry", return_value=geometry),
            patch.object(
                sidecar,
                "layout_snapshot_metadata",
                return_value={
                    "snapshot": self._semantic_layout_for_image(
                        search_results
                    )
                },
            ),
            patch.object(
                sidecar,
                "recover_send_window_guard",
                return_value={"ok": True},
            ),
            patch.object(
                sidecar,
                "clear_sidebar_search_box_without_select_all",
                return_value={"ok": True, "reason": "cleared"},
            ),
            patch.object(
                sidecar,
                "type_sidebar_search_query",
                return_value={"ok": True, "reason": "typed"},
            ),
            patch.object(
                sidecar,
                "human_window_image_click_in_bounds",
                side_effect=lambda _hwnd, x, y, **_kwargs: (
                    clicks.append((x, y)) or {"ok": True}
                ),
            ),
            patch.object(sidecar, "humanized_action_sleep"),
        ):
            locate_payload = sidecar.locate_chat_target_for_c2(
                1,
                target="CJTEST01",
                session_key="",
                remark_code="CJTEST01",
                target_mode="visible",
                visible_session_candidate=None,
                exact=False,
                artifact_dir=None,
                sidecar_run_id="unit-merged-search",
                failure_state="target_not_found",
                failure_error_code="TARGET_NOT_FOUND",
            )

        timing = dict(sidecar._LAST_OPEN_CHAT_TIMING)
        self.assertTrue(locate_payload["ok"])
        self.assertEqual(captures.count("open_chat"), 1)
        self.assertTrue(
            timing["open_chat_merged_remark_search_attempted"]
        )
        self.assertTrue(
            timing["open_chat_merged_remark_search_baseline_reused"]
        )
        self.assertFalse(
            locate_payload["targeting"]["visible_postcheck"][
                "fallback_full_ocr"
            ]
        )
        self.assertEqual(len(clicks), 1)

    def test_sidebar_search_candidate_cannot_hide_full_window_blocker(self):
        frame = Image.new("RGB", (980, 860), "white")
        layout = self._semantic_layout_for_image(frame)
        geometry = {
            "left": 0,
            "top": 0,
            "right": 980,
            "bottom": 860,
            "width": 980,
            "height": 860,
        }
        baseline_items = [
            {
                "text": "搜索",
                "left": 92,
                "top": 35,
                "right": 180,
                "bottom": 62,
                "center_x": 136,
                "center_y": 48,
                "confidence": 0.99,
            }
        ]
        sidebar_candidate = {
            "text": "CJTEST01 张三",
            "left": 100,
            "top": 166,
            "right": 280,
            "bottom": 194,
            "center_x": 190,
            "center_y": 180,
            "confidence": 0.99,
        }
        central_blocker = {
            "text": "登录异常，请重新登录",
            "left": 430,
            "top": 300,
            "right": 700,
            "bottom": 350,
            "center_x": 565,
            "center_y": 325,
            "confidence": 0.99,
        }

        def surface_state(_shot, items, **_kwargs):
            texts = {str(item.get("text") or "") for item in items}
            if central_blocker["text"] in texts:
                return {"ok": False, "reason": "central_login_blocker"}
            return {"ok": True, "reason": "normal"}

        with (
            patch.object(
                sidecar,
                "recover_send_window_guard",
                return_value={"ok": True},
            ),
            patch.object(
                sidecar,
                "clear_sidebar_search_box_without_select_all",
                return_value={"ok": True},
            ),
            patch.object(
                sidecar,
                "type_sidebar_search_query",
                return_value={"ok": True},
            ),
            patch.object(sidecar, "humanized_action_sleep"),
            patch.object(
                sidecar,
                "capture_wechat_window_visible_screen",
                return_value=(frame, "search-with-blocker.png"),
            ),
            patch.object(
                sidecar,
                "run_ocr_for_sidebar_search_results",
                return_value=(
                    [sidebar_candidate],
                    {
                        "source": "sidebar_roi",
                        "regions": ["sidebar_bounds"],
                        "ocr_call_count": 1,
                    },
                ),
            ),
            patch.object(
                sidecar,
                "run_ocr_traced",
                return_value=[central_blocker],
            ) as full_ocr,
            patch.object(
                sidecar,
                "target_switch_surface_state",
                side_effect=surface_state,
            ),
            patch.object(
                sidecar,
                "layout_snapshot_metadata",
                return_value={"snapshot": layout},
            ),
            patch.object(sidecar, "get_window_geometry", return_value=geometry),
            patch.object(
                sidecar,
                "human_window_image_click_in_bounds",
            ) as click,
        ):
            result = sidecar.open_chat_by_remark_code_search(
                1,
                target="CJTEST01",
                remark_code="CJTEST01",
                baseline_screenshot=frame,
                baseline_ocr_items=baseline_items,
                baseline_geometry=geometry,
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "central_login_blocker")
        full_ocr.assert_called_once()
        click.assert_not_called()

    def test_chat_fact_roi_insufficient_evidence_falls_back_on_same_frame(self):
        frame = Image.new("RGB", (980, 860), "white")
        self._semantic_layout_for_image(frame)
        validation_results = [
            {"ok": False, "reason": "target_title_not_confirmed"},
            {
                "ok": True,
                "reason": "target_confirmed",
                "confirmation_confidence": "active_title_strict",
            },
        ]
        full_ocr_images: list[Image.Image] = []

        def full_ocr(image, *_args, **_kwargs):
            full_ocr_images.append(image)
            return []

        with (
            patch.object(
                sidecar,
                "run_ocr_for_chat_fact_frame",
                return_value=(
                    [],
                    {
                        "source": "chat_fact_roi",
                        "regions": [
                            "chat_header_bounds",
                            "message_viewport_bounds",
                            "input_bounds",
                        ],
                        "ocr_call_count": 3,
                    },
                ),
            ),
            patch.object(
                sidecar,
                "run_ocr_traced",
                side_effect=full_ocr,
            ),
            patch.object(
                sidecar,
                "validate_active_send_target",
                side_effect=validation_results,
            ),
            patch.object(
                sidecar,
                "active_send_guard_is_strong",
                side_effect=lambda value: value.get("ok") is True,
            ),
            patch.object(
                sidecar,
                "get_window_geometry",
                return_value={
                    "left": 0,
                    "top": 0,
                    "right": 980,
                    "bottom": 860,
                    "width": 980,
                    "height": 860,
                },
            ),
            patch.object(
                sidecar,
                "parse_current_chat_frame_messages",
                return_value=[],
            ),
        ):
            snapshot = sidecar.build_send_fact_snapshot_from_frame(
                1,
                target="CJTEST01",
                text="回复",
                exact=False,
                artifact_dir=None,
                label="same_frame_fallback",
                screenshot=frame,
                screenshot_path="same-frame.png",
            )

        self.assertTrue(snapshot["ok"])
        self.assertEqual(full_ocr_images, [frame])
        self.assertEqual(snapshot["ocr_plan"]["source"], "full_fallback")
        self.assertEqual(
            snapshot["ocr_plan"]["fallback_reason"],
            "target_confirmation_insufficient",
        )

    def test_send_context_roi_message_miss_falls_back_on_same_frame(self):
        frame = Image.new("RGB", (980, 860), "white")
        self._semantic_layout_for_image(frame)
        geometry = {
            "left": 0,
            "top": 0,
            "right": 980,
            "bottom": 860,
            "width": 980,
            "height": 860,
        }
        message = {
            "id": "customer-1",
            "type": "text",
            "message_type": "text",
            "sender": "customer",
            "sender_role": "customer",
            "content": "想看十万左右的车",
            "bubble_rect": [410, 200, 650, 240],
            "avatar_alignment": {"role": "customer", "confirmed": True},
        }
        layout_evidence = {
            "ok": True,
            "message_viewport_bounds": [382, 86, 980, 679],
        }
        expected_observations = sidecar.build_message_observations_v3(
            [message]
        )
        expected_guard = sidecar.build_send_context_guard(
            expected_observations,
            screenshot=frame,
            layout_evidence=layout_evidence,
        )
        full_ocr_images: list[Image.Image] = []

        def full_ocr(image, *_args, **_kwargs):
            full_ocr_images.append(image)
            return [{"source": "full"}]

        def parse_messages(items, *_args, **_kwargs):
            return [message] if items and items[0].get("source") == "full" else []

        with (
            patch.object(
                sidecar,
                "run_ocr_for_chat_fact_frame",
                return_value=(
                    [{"source": "roi"}],
                    {
                        "source": "chat_fact_roi",
                        "regions": [
                            "chat_header_bounds",
                            "message_viewport_bounds",
                            "input_bounds",
                        ],
                        "ocr_call_count": 3,
                    },
                ),
            ),
            patch.object(sidecar, "run_ocr_traced", side_effect=full_ocr),
            patch.object(
                sidecar,
                "validate_active_send_target",
                return_value={
                    "ok": True,
                    "reason": "target_confirmed",
                    "confirmation_confidence": "active_title_strict",
                    "geometry": geometry,
                },
            ),
            patch.object(sidecar, "active_send_guard_is_strong", return_value=True),
            patch.object(sidecar, "get_window_geometry", return_value=geometry),
            patch.object(
                sidecar,
                "parse_current_chat_frame_messages",
                side_effect=parse_messages,
            ),
            patch.object(
                sidecar,
                "basic_chat_layout_evidence",
                return_value=layout_evidence,
            ),
            patch.object(
                sidecar,
                "input_text_region_state",
                return_value={"has_visible_text": False},
            ),
        ):
            snapshot = sidecar.build_send_fact_snapshot_from_frame(
                1,
                target="CJTEST01",
                text="AI回复",
                exact=False,
                artifact_dir=None,
                label="send_context_same_frame_fallback",
                screenshot=frame,
                screenshot_path="same-frame.png",
                expected_context_guard=expected_guard,
            )

        self.assertTrue(snapshot["ok"])
        self.assertEqual(snapshot["message_count"], 1)
        self.assertEqual(full_ocr_images, [frame])
        self.assertEqual(snapshot["ocr_plan"]["source"], "full_fallback")
        self.assertEqual(
            snapshot["ocr_plan"]["fallback_reason"],
            "message_context_evidence_insufficient",
        )
        self.assertTrue(
            self._validate_worker_send_context(
                expected_guard,
                snapshot["send_context_guard"],
                expected_observations=expected_observations,
                current_observations=snapshot["observations"],
            )["ok"]
        )

    def test_send_context_full_ocr_still_insufficient_blocks_before_enter(self):
        frame = Image.new("RGB", (980, 860), "white")
        self._semantic_layout_for_image(frame)
        geometry = {
            "left": 0,
            "top": 0,
            "right": 980,
            "bottom": 860,
            "width": 980,
            "height": 860,
        }
        message = {
            "id": "customer-1",
            "type": "text",
            "message_type": "text",
            "sender": "customer",
            "sender_role": "customer",
            "content": "想看十万左右的车",
            "bubble_rect": [410, 200, 650, 240],
            "avatar_alignment": {"role": "customer", "confirmed": True},
        }
        layout_evidence = {
            "ok": True,
            "message_viewport_bounds": [382, 86, 980, 679],
        }
        expected_guard = sidecar.build_send_context_guard(
            sidecar.build_message_observations_v3([message]),
            screenshot=frame,
            layout_evidence=layout_evidence,
        )
        with (
            patch.object(
                sidecar,
                "capture_wechat",
                return_value=(frame, "same-frame.png"),
            ) as capture,
            patch.object(
                sidecar,
                "run_ocr_for_chat_fact_frame",
                return_value=(
                    [],
                    {
                        "source": "chat_fact_roi",
                        "regions": [
                            "chat_header_bounds",
                            "message_viewport_bounds",
                            "input_bounds",
                        ],
                        "ocr_call_count": 3,
                    },
                ),
            ),
            patch.object(sidecar, "run_ocr_traced", return_value=[]) as full_ocr,
            patch.object(
                sidecar,
                "validate_active_send_target",
                return_value={
                    "ok": True,
                    "reason": "target_confirmed",
                    "confirmation_confidence": "active_title_strict",
                    "geometry": geometry,
                },
            ),
            patch.object(sidecar, "active_send_guard_is_strong", return_value=True),
            patch.object(sidecar, "get_window_geometry", return_value=geometry),
            patch.object(sidecar, "validate_send_geometry", return_value={"ok": True}),
            patch.object(sidecar, "recover_send_window_guard", return_value={"ok": True}),
            patch.object(sidecar, "parse_current_chat_frame_messages", return_value=[]),
            patch.object(
                sidecar,
                "basic_chat_layout_evidence",
                return_value=layout_evidence,
            ),
            patch.object(
                sidecar,
                "input_text_region_state",
                return_value={"has_visible_text": False},
            ),
            patch.object(sidecar, "send_with_visual_input") as visual_send,
            patch.object(sidecar, "safe_send_trigger") as enter,
        ):
            result = sidecar.send_payload(
                1,
                {"ok": True},
                target="CJTEST01",
                text="AI回复",
                exact=False,
                skip_send_rate_guard=True,
                expected_context_guard=expected_guard,
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["state"], "send_context_changed_before_input")
        self.assertEqual(capture.call_count, 1)
        full_ocr.assert_called_once_with(
            frame,
            "send_baseline_chat_fact_fallback_full",
            source="build_send_fact_snapshot_from_frame",
        )
        visual_send.assert_not_called()
        enter.assert_not_called()

    def test_send_receipt_roi_miss_falls_back_on_same_post_send_frame(self):
        frame = Image.new("RGB", (980, 860), "white")
        self._semantic_layout_for_image(frame)
        geometry = {
            "left": 0,
            "top": 0,
            "right": 980,
            "bottom": 860,
            "width": 980,
            "height": 860,
        }
        customer = {
            "id": "customer-1",
            "type": "text",
            "message_type": "text",
            "sender": "customer",
            "sender_role": "customer",
            "content": "在吗",
            "bubble_rect": [410, 200, 520, 240],
            "avatar_alignment": {"role": "customer", "confirmed": True},
        }
        sent = {
            "id": "self-new",
            "type": "text",
            "message_type": "text",
            "sender": "self",
            "sender_role": "self",
            "content": "AI回复",
            "bubble_rect": [700, 270, 900, 310],
            "avatar_alignment": {"role": "self", "confirmed": True},
        }
        baseline_sequence = [
            {
                "sequence_index": 0,
                "observation_id": "customer-1",
                "row_kind": "text_bubble",
                "sender_role": "customer",
                "content_normalized": "在吗",
            }
        ]

        def parse_messages(items, *_args, **_kwargs):
            return [customer, sent] if items and items[0].get("source") == "full" else [customer]

        with (
            patch.object(
                sidecar,
                "run_ocr_for_chat_fact_frame",
                return_value=(
                    [{"source": "roi"}],
                    {
                        "source": "chat_fact_roi",
                        "regions": [
                            "chat_header_bounds",
                            "message_viewport_bounds",
                            "input_bounds",
                        ],
                        "ocr_call_count": 3,
                    },
                ),
            ),
            patch.object(
                sidecar,
                "run_ocr_traced",
                return_value=[{"source": "full"}],
            ) as full_ocr,
            patch.object(
                sidecar,
                "validate_active_send_target",
                return_value={
                    "ok": True,
                    "reason": "target_confirmed",
                    "confirmation_confidence": "active_title_strict",
                    "geometry": geometry,
                },
            ),
            patch.object(sidecar, "active_send_guard_is_strong", return_value=True),
            patch.object(sidecar, "get_window_geometry", return_value=geometry),
            patch.object(
                sidecar,
                "parse_current_chat_frame_messages",
                side_effect=parse_messages,
            ),
            patch.object(
                sidecar,
                "input_text_region_state",
                return_value={"has_visible_text": False},
            ),
        ):
            snapshot = sidecar.build_send_fact_snapshot_from_frame(
                1,
                target="CJTEST01",
                text="AI回复",
                exact=False,
                artifact_dir=None,
                label="send_receipt_same_frame_fallback",
                screenshot=frame,
                screenshot_path="same-post-send-frame.png",
                recover_expected_self_text=True,
                receipt_baseline_message_sequence=baseline_sequence,
                receipt_text="AI回复",
            )

        self.assertEqual(snapshot["message_count"], 2)
        self.assertIsNotNone(
            sidecar.find_new_matching_self_message(
                baseline_sequence,
                snapshot["message_sequence"],
                "AI回复",
            )
        )
        full_ocr.assert_called_once()
        self.assertEqual(
            snapshot["ocr_plan"]["fallback_reason"],
            "send_receipt_evidence_insufficient",
        )

    def test_same_frame_full_ocr_replay_authenticates_saved_pixels_without_capture(self):
        frame = Image.new("RGB", (980, 860), "white")
        geometry = {
            "left": 0,
            "top": 0,
            "right": 980,
            "bottom": 860,
            "width": 980,
            "height": 860,
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            screenshot_path = Path(temp_dir) / "roi-frame.png"
            frame.save(screenshot_path)
            evidence = {
                "frame_id": "frame-authenticated-roi",
                "screenshot_sha256": hashlib.sha256(
                    bytes(frame.tobytes())
                ).hexdigest(),
                "screenshot_path": str(screenshot_path),
                "hwnd": 1,
                "geometry": geometry,
                "dpi_scale": 1.0,
                "captured_monotonic": time.monotonic(),
            }
            target_validation = {
                "ok": True,
                "online": True,
                "private_confirmed": True,
                "remark_code_confirmed": True,
                "conversation_type": "private",
                "confirmation_confidence": "active_title_strict",
            }
            with (
                patch.object(
                    sidecar,
                    "get_window_geometry",
                    return_value=geometry,
                ),
                patch.object(
                    sidecar,
                    "get_window_client_geometry",
                    return_value={"screen_left": 0, "screen_top": 0},
                ),
                patch.object(sidecar, "window_dpi_scale", return_value=1.0),
                patch.object(
                    sidecar,
                    "_register_layout_snapshot",
                    return_value={
                        **self._semantic_layout_for_image(frame),
                        "executable": True,
                    },
                ),
                patch.object(
                    sidecar,
                    "run_ocr_traced",
                    return_value=[],
                ) as full_ocr,
                patch.object(
                    sidecar,
                    "parse_current_chat_frame_messages",
                    return_value=[],
                ),
                patch.object(
                    sidecar,
                    "validate_active_send_target",
                    return_value=target_validation,
                ),
                patch.object(
                    sidecar,
                    "c2_target_activation_confirmed",
                    return_value=True,
                ),
                patch.object(sidecar, "capture_wechat") as capture,
            ):
                result = sidecar.load_verified_same_frame_full_ocr_seed(
                    1,
                    json.dumps(evidence),
                    artifact_dir=temp_dir,
                    target="CJTEST01",
                )

            self.assertTrue(result["ok"], result)
            self.assertEqual(result["frame_id"], "frame-authenticated-roi")
            self.assertEqual(
                result["seed_snapshot"]["frame_observation"]["frame_id"],
                "frame-authenticated-roi",
            )
            self.assertEqual(
                result["seed_snapshot"]["ocr_plan"]["source"],
                "same_frame_full_fallback",
            )
            full_ocr.assert_called_once()
            capture.assert_not_called()
            with (
                patch.object(
                    sidecar,
                    "get_window_geometry",
                    return_value=geometry,
                ),
                patch.object(
                    sidecar,
                    "validate_active_send_target",
                    return_value=target_validation,
                ),
                patch.object(
                    sidecar,
                    "c2_target_activation_confirmed",
                    return_value=True,
                ),
                patch.object(
                    sidecar,
                    "capture_message_history_snapshots",
                ) as history_capture,
            ):
                payload = sidecar.messages_payload(
                    1,
                    {"passive_probe": True},
                    target="CJTEST01",
                    history_load_times=0,
                    artifact_dir=temp_dir,
                    confirm_target="CJTEST01",
                    seed_snapshot=result["seed_snapshot"],
                )
            self.assertTrue(payload["ok"], payload)
            self.assertEqual(payload["frame_id"], "frame-authenticated-roi")
            self.assertEqual(
                payload["frame_observation"]["frame_id"],
                "frame-authenticated-roi",
            )
            self.assertEqual(
                payload["pre_send_frame_reuse"]["ocr_plan"]["source"],
                "same_frame_full_fallback",
            )
            history_capture.assert_not_called()

    def test_same_frame_full_ocr_replay_rejects_tampered_pixels_before_ocr(self):
        frame = Image.new("RGB", (980, 860), "white")
        geometry = {
            "left": 0,
            "top": 0,
            "right": 980,
            "bottom": 860,
            "width": 980,
            "height": 860,
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            screenshot_path = Path(temp_dir) / "roi-frame.png"
            frame.save(screenshot_path)
            evidence = {
                "frame_id": "frame-tampered-roi",
                "screenshot_sha256": "0" * 64,
                "screenshot_path": str(screenshot_path),
                "hwnd": 1,
                "geometry": geometry,
                "dpi_scale": 1.0,
                "captured_monotonic": time.monotonic(),
            }
            with (
                patch.object(
                    sidecar,
                    "get_window_geometry",
                    return_value=geometry,
                ),
                patch.object(sidecar, "window_dpi_scale", return_value=1.0),
                patch.object(sidecar, "run_ocr_traced") as full_ocr,
                patch.object(sidecar, "capture_wechat") as capture,
            ):
                result = sidecar.load_verified_same_frame_full_ocr_seed(
                    1,
                    json.dumps(evidence),
                    artifact_dir=temp_dir,
                    target="CJTEST01",
                )

            self.assertFalse(result["ok"])
            self.assertEqual(
                result["reason"],
                "same_frame_screenshot_digest_mismatch",
            )
            full_ocr.assert_not_called()
            capture.assert_not_called()

    def test_current_locate_reuses_three_roi_frame_as_message_seed(self):
        frame = Image.new("RGB", (980, 860), "white")
        layout = self._semantic_layout_for_image(frame)
        header_bounds = sidecar.win32_ocr_layout.required_region(
            layout,
            "chat_header_bounds",
        )
        header_size = (
            header_bounds[2] - header_bounds[0],
            header_bounds[3] - header_bounds[1],
        )
        input_bounds = sidecar.win32_ocr_layout.required_region(
            layout,
            "input_bounds",
        )
        input_size = (
            input_bounds[2] - input_bounds[0],
            input_bounds[3] - input_bounds[1],
        )
        ocr_sizes: list[tuple[int, int]] = []

        def raw_ocr(image):
            ocr_sizes.append(tuple(image.size))
            if tuple(image.size) == header_size:
                return [
                    {
                        "text": "CJTEST01",
                        "left": 20,
                        "top": 22,
                        "right": 150,
                        "bottom": 52,
                        "center_x": 85,
                        "center_y": 37,
                        "confidence": 0.99,
                    }
                ]
            if tuple(image.size) == input_size:
                return [
                    {
                        "text": "发送",
                        "left": 30,
                        "top": 20,
                        "right": 90,
                        "bottom": 50,
                        "center_x": 60,
                        "center_y": 35,
                        "confidence": 0.99,
                    }
                ]
            return []

        geometry = {
            "left": 0,
            "top": 0,
            "right": 980,
            "bottom": 860,
            "width": 980,
            "height": 860,
        }
        with (
            patch.object(
                sidecar,
                "capture_wechat",
                return_value=(frame, "current-chat-fact.png"),
            ),
            patch.object(sidecar, "run_ocr", side_effect=raw_ocr),
            patch.object(sidecar, "get_window_geometry", return_value=geometry),
        ):
            payload = sidecar.locate_chat_target_for_c2(
                1,
                target="CJTEST01",
                session_key="",
                remark_code="CJTEST01",
                target_mode="current",
                visible_session_candidate=None,
                exact=False,
                artifact_dir=None,
                sidecar_run_id="unit-current-roi",
                failure_state="target_not_found",
                failure_error_code="TARGET_NOT_FOUND",
                chat_fact_roi_ocr=True,
            )

        self.assertTrue(payload["ok"], payload)
        self.assertEqual(
            payload["_chat_fact_seed"]["ocr_plan"]["source"],
            "chat_fact_roi",
        )
        self.assertEqual(len(ocr_sizes), 3)
        self.assertNotIn(frame.size, ocr_sizes)

    def test_generic_journal_updates_matching_voice_item_before_click(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "voice-action.json"
            path.write_text(
                json.dumps(
                    {
                        "action_kind": "voice",
                        "action_phase": "not_attempted",
                        "items": {
                            "source-1": {
                                "source_message_key": "source-1",
                                "physical_anchor_keys": ["anchor-1"],
                                "action_phase": "not_attempted",
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )

            sidecar.write_action_phase_journal(
                str(path),
                "trigger_attempted",
                physical_anchor_keys=["anchor-1"],
            )

            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(
                payload["items"]["source-1"]["action_phase"],
                "trigger_attempted",
            )

    def test_voice_journal_terminal_update_covers_every_physical_alias_item(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "voice-action.json"
            path.write_text(
                json.dumps(
                    {
                        "action_kind": "voice",
                        "action_phase": "not_attempted",
                        "items": {
                            "source-stable": {
                                "source_message_key": "source-stable",
                                "physical_anchor_keys": ["voice-stable:one"],
                                "action_phase": "not_attempted",
                            },
                            "source-structural": {
                                "source_message_key": "source-structural",
                                "physical_anchor_keys": [
                                    "voice-structural:one"
                                ],
                                "action_phase": "not_attempted",
                            },
                        },
                    }
                ),
                encoding="utf-8",
            )

            sidecar.write_action_phase_journal(
                str(path),
                "confirmed",
                physical_anchor_keys=[
                    "voice-stable:one",
                    "voice-structural:one",
                ],
                business_state="completed",
                business_result_confirmed=True,
                terminal_payload={"state": "completed"},
            )

            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["action_phase"], "confirmed")
            self.assertEqual(
                {
                    item["action_phase"]
                    for item in payload["items"].values()
                },
                {"confirmed"},
            )
            self.assertTrue(
                all(
                    item["business_result_confirmed"]
                    for item in payload["items"].values()
                )
            )

    def test_generic_journal_rejects_missing_or_uninitialized_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "missing-action.json"

            with self.assertRaisesRegex(
                ValueError,
                "ACTION_JOURNAL_NOT_INITIALIZED",
            ):
                sidecar.write_action_phase_journal(
                    str(path),
                    "trigger_attempted",
                    physical_anchor_keys=["anchor-1"],
                )

            path.write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(
                ValueError,
                "ACTION_JOURNAL_ITEMS_MISSING",
            ):
                sidecar.write_action_phase_journal(
                    str(path),
                    "trigger_attempted",
                    physical_anchor_keys=["anchor-1"],
                )

    def test_send_journal_is_persisted_before_enter_trigger(self):
        events: list[str] = []

        with patch.object(
            sidecar,
            "key_press",
            side_effect=lambda *_args: events.append("enter"),
        ):
            result = sidecar.safe_send_trigger(
                100,
                trigger_mode="enter_only",
                focus_guard_func=lambda: {"ok": True},
                before_physical_trigger=lambda: events.append(
                    "trigger_attempted"
                ),
            )

        self.assertTrue(result["ok"])
        self.assertEqual(events, ["trigger_attempted", "enter"])

    def test_enter_exception_after_journal_is_trigger_attempted_unknown(self):
        events: list[str] = []
        with patch.object(
            sidecar,
            "key_press",
            side_effect=RuntimeError("keyboard disconnected"),
        ):
            result = sidecar.safe_send_trigger(
                100,
                trigger_mode="enter_only",
                focus_guard_func=lambda: {"ok": True},
                before_physical_trigger=lambda: events.append(
                    "trigger_attempted"
                ),
            )

        self.assertFalse(result["ok"])
        self.assertEqual(events, ["trigger_attempted"])
        self.assertTrue(result["physical_send_triggered"])
        self.assertEqual(result["action_phase"], "trigger_attempted")
        self.assertEqual(result["error_code"], "SEND_RESULT_UNKNOWN")

    def test_green_send_button_is_readiness_evidence_not_click_target(self):
        image = Image.new("RGB", (980, 860), (245, 245, 245))
        draw = ImageDraw.Draw(image)
        draw.rectangle((870, 790, 950, 832), fill=(26, 190, 92))

        result = sidecar.send_button_ready_evidence(
            image,
            geometry={
                "width": 980,
                "height": 860,
            },
        )

        self.assertTrue(result["ok"])
        self.assertFalse(result["used_as_click_target"])

    def test_missing_green_send_button_does_not_block_verified_enter(self):
        frame = Image.new("RGB", (960, 820), "white")
        with (
            patch.object(
                sidecar,
                "paste_text_with_confirmation",
                return_value={
                    "ok": True,
                    "input_result": {"ok": True, "typed_chars": 4},
                    "send_button_ready": {
                        "ok": False,
                        "reason": "active_green_send_button_not_observed",
                    },
                    "_post_input_screenshot": frame,
                },
            ),
            patch.object(
                sidecar,
                "safe_send_trigger",
                return_value={
                    "ok": True,
                    "method": "keyboard_enter",
                    "physical_send_triggered": True,
                },
            ) as trigger,
        ):
            result = sidecar.execute_send_transaction(
                1,
                "AI回复",
                locator={
                    "ok": True,
                    "path": "uia_input",
                    "input_point": (650, 720),
                    "input_click": {"bounds": [400, 680, 880, 800]},
                    "value_pattern": None,
                },
                geometry={"width": 960, "height": 820},
                before_send_trigger_check=lambda **_kwargs: {"ok": True},
            )

        self.assertTrue(result["ok"])
        self.assertFalse(result["send_button_ready"]["ok"])
        trigger.assert_called_once()

    def test_green_send_button_never_overrides_changed_context(self):
        frame = Image.new("RGB", (960, 820), "white")
        with (
            patch.object(
                sidecar,
                "paste_text_with_confirmation",
                return_value={
                    "ok": True,
                    "input_result": {"ok": True, "typed_chars": 4},
                    "send_button_ready": {"ok": True},
                    "_post_input_screenshot": frame,
                },
            ),
            patch.object(
                sidecar,
                "clear_confirmed_program_draft",
                return_value={
                    "ok": True,
                    "cleared": True,
                    "reason": "confirmed_program_draft_cleared",
                },
            ),
            patch.object(sidecar, "safe_send_trigger") as trigger,
        ):
            result = sidecar.execute_send_transaction(
                1,
                "AI回复",
                locator={
                    "ok": True,
                    "path": "uia_input",
                    "input_point": (650, 720),
                    "input_click": {"bounds": [400, 680, 880, 800]},
                    "value_pattern": None,
                },
                geometry={"width": 960, "height": 820},
                before_send_trigger_check=lambda **_kwargs: {
                    "ok": False,
                    "error_code": "C3_CONTEXT_CHANGED_BEFORE_SEND",
                },
            )

        self.assertFalse(result["ok"])
        self.assertEqual(
            result["error_code"],
            "C3_CONTEXT_CHANGED_BEFORE_SEND",
        )
        trigger.assert_not_called()

    def test_visual_input_uses_green_as_evidence_and_enter_as_trigger(self):
        observed_trigger: dict[str, object] = {}
        # v0.9.31 requires a real current calibration-backed frame before C3
        # can translate the input reference point.
        self._semantic_layout_for_image(Image.new("RGB", (980, 860), "white"))
        with (
            patch.object(
                sidecar,
                "locate_visual_send_input",
                return_value={
                    "ok": True,
                    "path": "visual_input",
                    "input_point": (650, 720),
                    "input_click": {"bounds": [400, 680, 880, 800]},
                    "value_pattern": None,
                    "physical_send_triggered": False,
                },
            ),
            patch.object(
                sidecar,
                "paste_text_with_confirmation",
                return_value={
                    "ok": True,
                    "input_mode": "sendinput_unicode",
                    "send_button_ready": {
                        "ok": True,
                        "reason": "active_green_send_button_observed",
                        "used_as_click_target": False,
                    },
                },
            ),
            patch.object(
                sidecar,
                "safe_send_trigger",
                side_effect=lambda *_args, **kwargs: (
                    observed_trigger.update(kwargs)
                    or {
                        "ok": True,
                        "method": "keyboard_enter",
                        "send_trigger_mode": "enter_only",
                    }
                ),
            ),
        ):
            result = sidecar.send_with_visual_input(
                100,
                "AI回复",
                geometry={"width": 980, "height": 860},
                before_input_region_seed={
                    "input_region": {
                        "has_visible_text": False,
                        "bounds": [400, 680, 880, 800],
                    }
                },
                before_send_trigger_check=lambda: {"ok": True},
            )

        self.assertTrue(result["ok"])
        self.assertEqual(observed_trigger["trigger_mode"], "enter_only")
        self.assertNotIn("send_point", observed_trigger)
        self.assertIn("keyboard_enter", result["method"])

    def test_unconfirmed_cleanup_becomes_hard_send_block(self):
        with (
            patch.object(
                sidecar,
                "paste_text_with_confirmation",
                return_value={
                    "ok": True,
                    "input_result": {
                        "ok": True,
                        "typed_chars": 4,
                    },
                    "send_button_ready": {"ok": False},
                },
            ),
            patch.object(
                sidecar,
                "clear_confirmed_program_draft",
                return_value={
                    "ok": False,
                    "cleared": False,
                    "reason": "program_draft_not_proven",
                },
            ),
        ):
            result = sidecar.execute_send_transaction(
                1,
                "AI回复",
                locator={
                    "ok": True,
                    "path": "visual_input",
                    "input_point": (650, 720),
                    "input_click": {"bounds": [400, 680, 880, 800]},
                    "value_pattern": None,
                },
                geometry={"width": 960, "height": 820},
            )

        self.assertFalse(result["ok"])
        self.assertEqual(
            result["error_code"],
            "SEND_DRAFT_CLEANUP_UNCONFIRMED",
        )
        self.assertFalse(result["physical_send_triggered"])

    def test_input_exception_never_deletes_coincidentally_matching_human_draft(self):
        value_pattern = SimpleNamespace(Value="AI回复")
        with (
            patch.object(
                sidecar,
                "paste_text_with_confirmation",
                side_effect=RuntimeError("input driver stopped"),
            ),
            patch.object(sidecar, "human_client_click") as click,
            patch.object(sidecar, "hotkey") as hotkey,
            patch.object(sidecar, "key_press") as key_press,
        ):
            result = sidecar.execute_send_transaction(
                1,
                "AI回复",
                locator={
                    "ok": True,
                    "path": "visual_input",
                    "input_point": (650, 720),
                    "input_click": {"bounds": [400, 680, 880, 800]},
                    "value_pattern": value_pattern,
                },
                geometry={"width": 960, "height": 820},
            )

        self.assertFalse(result["ok"])
        self.assertEqual(
            result["error_code"],
            "SEND_DRAFT_CLEANUP_UNCONFIRMED",
        )
        self.assertEqual(
            result["paste"]["input_result"]["input_progress"],
            "unknown",
        )
        self.assertIsNone(
            result["paste"]["input_result"]["typed_chars"],
        )
        click.assert_not_called()
        hotkey.assert_not_called()
        key_press.assert_not_called()

    def test_post_input_frame_is_reused_for_context_check(self):
        frame = Image.new("RGB", (960, 820), "white")
        observed: dict[str, object] = {}

        def context_check(**kwargs):
            observed.update(kwargs)
            return {"ok": True}

        with (
            patch.object(
                sidecar,
                "paste_text_with_confirmation",
                return_value={
                    "ok": True,
                    "input_result": {
                        "ok": True,
                        "typed_chars": 4,
                    },
                    "send_button_ready": {"ok": True},
                    "_post_input_screenshot": frame,
                    "_post_input_screenshot_path": "post-input.png",
                },
            ),
            patch.object(
                sidecar,
                "safe_send_trigger",
                return_value={
                    "ok": True,
                    "physical_send_triggered": True,
                },
            ),
        ):
            result = sidecar.execute_send_transaction(
                1,
                "AI回复",
                locator={
                    "ok": True,
                    "path": "uia_input",
                    "input_point": (650, 720),
                    "input_click": {"bounds": [400, 680, 880, 800]},
                    "value_pattern": None,
                },
                geometry={"width": 960, "height": 820},
                before_send_trigger_check=context_check,
            )

        self.assertTrue(result["ok"])
        self.assertIs(observed["screenshot"], frame)
        self.assertEqual(
            observed["screenshot_path"],
            "post-input.png",
        )
        self.assertNotIn("ocr_items", observed)

    def test_post_input_full_ocr_is_reused_for_context_check(self):
        frame = Image.new("RGB", (960, 820), "white")
        ocr_items = [{"text": "CJUAT728", "left": 400, "top": 20}]
        observed: dict[str, object] = {}

        def context_check(**kwargs):
            observed.update(kwargs)
            return {"ok": True}

        with (
            patch.object(
                sidecar,
                "paste_text_with_confirmation",
                return_value={
                    "ok": True,
                    "input_result": {
                        "ok": True,
                        "typed_chars": 4,
                    },
                    "send_button_ready": {"ok": True},
                    "_post_input_screenshot": frame,
                    "_post_input_screenshot_path": "post-input.png",
                    "_post_input_ocr_items": ocr_items,
                },
            ),
            patch.object(
                sidecar,
                "safe_send_trigger",
                return_value={
                    "ok": True,
                    "physical_send_triggered": True,
                },
            ),
        ):
            result = sidecar.execute_send_transaction(
                1,
                "AI回复",
                locator={
                    "ok": True,
                    "path": "visual_input",
                    "input_point": (650, 720),
                    "input_click": {"bounds": [400, 680, 880, 800]},
                    "value_pattern": None,
                },
                geometry={"width": 960, "height": 820},
                before_send_trigger_check=context_check,
            )

        self.assertTrue(result["ok"])
        self.assertIs(observed["screenshot"], frame)
        self.assertIs(observed["ocr_items"], ocr_items)

    def test_send_reuses_one_baseline_and_first_post_send_confirmation_frame(self):
        geometry = {
            "left": 0,
            "top": 0,
            "right": 960,
            "bottom": 820,
            "width": 960,
            "height": 820,
        }
        validation = {
            "ok": True,
            "online": True,
            "reason": "target_confirmed",
            "confirmation_confidence": "active_title_strict",
            "geometry": geometry,
        }
        context_guard = {
            "schema_version": 1,
            "sequence": [],
            "message_count": 0,
            "bottom": None,
        }
        baseline = {
            "ok": True,
            "validation": validation,
            "input_region": {"has_visible_text": False},
            "matching_self_message_count": 0,
            "message_sequence": [],
            "send_context_guard": context_guard,
        }
        post_send = {
            "ok": True,
            "validation": validation,
            "input_region": {"has_visible_text": False},
            "matching_self_message_count": 1,
            "message_sequence": [
                {
                    "sequence_index": 0,
                    "observation_id": "self-new",
                    "row_kind": "text_bubble",
                    "sender_role": "self",
                    "content_normalized": "AI回复",
                }
            ],
            "observations": [
                {
                    "observation_id": "self-new",
                    "row_kind": "text_bubble",
                    "sender_role": "self",
                    "content_clean": "AI回复",
                }
            ],
        }
        with (
            patch.object(sidecar, "recover_send_window_guard", return_value={"ok": True}),
            patch.object(sidecar, "active_send_guard_is_strong", return_value=True),
            patch.object(sidecar, "validate_send_geometry", return_value={"ok": True}),
            patch.object(
                sidecar,
                "capture_send_fact_snapshot",
                side_effect=[baseline, post_send],
            ) as capture,
            patch.object(sidecar, "validate_send_context_guard", return_value={"ok": True}),
            patch.object(
                sidecar,
                "send_with_visual_input",
                return_value={
                    "ok": True,
                    "method": "visual_input.sendinput_unicode+keyboard_enter",
                    "physical_send_triggered": True,
                },
            ),
            patch.object(sidecar, "validate_post_send_target") as old_post_guard,
            patch.object(sidecar, "humanized_action_sleep"),
        ):
            result = sidecar.send_payload(
                1,
                {"ok": True},
                target="CJUAT728",
                text="AI回复",
                exact=False,
                skip_send_rate_guard=True,
                expected_context_guard=context_guard,
            )

        self.assertTrue(result["ok"])
        self.assertEqual(capture.call_count, 2)
        self.assertEqual(
            capture.call_args_list[0].kwargs["label"],
            "send_baseline",
        )
        self.assertEqual(
            capture.call_args_list[1].kwargs["label"],
            "send_post_guard_and_result_confirm_1",
        )
        old_post_guard.assert_not_called()

    def test_send_uses_three_distinct_physical_frame_ids(self):
        geometry = {
            "left": 0,
            "top": 0,
            "right": 960,
            "bottom": 820,
            "width": 960,
            "height": 820,
        }
        validation = {
            "ok": True,
            "online": True,
            "reason": "target_confirmed",
            "confirmation_confidence": "active_title_strict",
            "geometry": geometry,
        }
        context_guard = {
            "schema_version": 1,
            "sequence": [],
            "message_count": 0,
            "bottom": None,
        }

        def snapshot(frame_id: str, *, sent: bool = False) -> dict:
            return {
                "ok": True,
                "validation": validation,
                "input_region": {"has_visible_text": False},
                "matching_self_message_count": 1 if sent else 0,
                "message_sequence": (
                    [
                        {
                            "sequence_index": 0,
                            "observation_id": "self-new",
                            "row_kind": "text_bubble",
                            "sender_role": "self",
                            "content_normalized": "AI回复",
                        }
                    ]
                    if sent
                    else []
                ),
                "observations": (
                    [
                        {
                            "observation_id": "self-new",
                            "row_kind": "text_bubble",
                            "sender_role": "self",
                            "content_clean": "AI回复",
                        }
                    ]
                    if sent
                    else []
                ),
                "send_context_guard": context_guard,
                "frame_observation": {
                    "frame_id": frame_id,
                    "screenshot_sha256": frame_id * 8,
                },
            }

        def visual_send(*_args, **kwargs):
            context_check = kwargs["before_send_trigger_check"]()
            return {
                "ok": bool(context_check.get("ok")),
                "method": "visual_input.sendinput_unicode+keyboard_enter",
                "physical_send_triggered": bool(context_check.get("ok")),
                "context_check": context_check,
                "error_code": context_check.get("error_code"),
            }

        with (
            patch.object(sidecar, "recover_send_window_guard", return_value={"ok": True}),
            patch.object(sidecar, "active_send_guard_is_strong", return_value=True),
            patch.object(sidecar, "validate_send_geometry", return_value={"ok": True}),
            patch.object(
                sidecar,
                "capture_send_fact_snapshot",
                side_effect=[snapshot("s0"), snapshot("s1"), snapshot("s2", sent=True)],
            ) as capture,
            patch.object(sidecar, "validate_send_context_guard", return_value={"ok": True}),
            patch.object(sidecar, "send_with_visual_input", side_effect=visual_send),
            patch.object(sidecar, "humanized_action_sleep"),
        ):
            result = sidecar.send_payload(
                1,
                {"ok": True},
                target="CJUAT728",
                text="AI回复",
                exact=False,
                skip_send_rate_guard=True,
                expected_context_guard=context_guard,
            )

        self.assertTrue(result["ok"])
        self.assertEqual(capture.call_count, 3)
        self.assertEqual(
            [result["send_frame_reuse"][key]["frame_id"] for key in ("s0", "s1", "s2")],
            ["s0", "s1", "s2"],
        )
        self.assertTrue(result["send_frame_reuse"]["fast_path_used"])

    def test_send_blocks_before_enter_when_s1_reuses_s0_frame_id(self):
        geometry = {
            "left": 0,
            "top": 0,
            "right": 960,
            "bottom": 820,
            "width": 960,
            "height": 820,
        }
        validation = {
            "ok": True,
            "confirmation_confidence": "active_title_strict",
            "geometry": geometry,
        }
        context_guard = {"schema_version": 1, "sequence": []}
        snapshot = {
            "ok": True,
            "validation": validation,
            "input_region": {"has_visible_text": False},
            "matching_self_message_count": 0,
            "message_sequence": [],
            "send_context_guard": context_guard,
            "frame_observation": {
                "frame_id": "same-frame",
                "screenshot_sha256": "a" * 64,
            },
        }

        def visual_send(*_args, **kwargs):
            context_check = kwargs["before_send_trigger_check"]()
            return {
                "ok": bool(context_check.get("ok")),
                "physical_send_triggered": bool(context_check.get("ok")),
                "context_check": context_check,
                "error_code": context_check.get("error_code"),
                "reason": context_check.get("reason"),
            }

        with (
            patch.object(sidecar, "recover_send_window_guard", return_value={"ok": True}),
            patch.object(sidecar, "active_send_guard_is_strong", return_value=True),
            patch.object(sidecar, "validate_send_geometry", return_value={"ok": True}),
            patch.object(
                sidecar,
                "capture_send_fact_snapshot",
                side_effect=[dict(snapshot), dict(snapshot)],
            ) as capture,
            patch.object(sidecar, "validate_send_context_guard", return_value={"ok": True}),
            patch.object(sidecar, "send_with_visual_input", side_effect=visual_send),
        ):
            result = sidecar.send_payload(
                1,
                {"ok": True},
                target="CJUAT728",
                text="AI回复",
                exact=False,
                skip_send_rate_guard=True,
                expected_context_guard=context_guard,
            )

        self.assertFalse(result["ok"])
        self.assertFalse(result["physical_send_triggered"])
        self.assertEqual(result["error_code"], "C3_SEND_FRAME_TIMEPOINT_INVALID")
        self.assertEqual(capture.call_count, 2)

        post_snapshot = {
            **snapshot,
            "matching_self_message_count": 1,
            "message_sequence": [
                {
                    "sequence_index": 0,
                    "observation_id": "self-new",
                    "row_kind": "text_bubble",
                    "sender_role": "self",
                    "content_normalized": "AI回复",
                }
            ],
            "observations": [
                {
                    "observation_id": "self-new",
                    "row_kind": "text_bubble",
                    "sender_role": "self",
                    "content_clean": "AI回复",
                }
            ],
            "frame_observation": {
                "frame_id": "s2",
                "screenshot_sha256": "b" * 64,
            },
        }
        with (
            patch.dict(
                os.environ,
                {"CHEJIN_C3_SEND_FRAME_LOCAL_REUSE_ENABLED": "0"},
            ),
            patch.object(sidecar, "recover_send_window_guard", return_value={"ok": True}),
            patch.object(sidecar, "active_send_guard_is_strong", return_value=True),
            patch.object(sidecar, "validate_send_geometry", return_value={"ok": True}),
            patch.object(
                sidecar,
                "capture_send_fact_snapshot",
                side_effect=[dict(snapshot), dict(snapshot), post_snapshot],
            ),
            patch.object(sidecar, "validate_send_context_guard", return_value={"ok": True}),
            patch.object(sidecar, "send_with_visual_input", side_effect=visual_send),
            patch.object(sidecar, "humanized_action_sleep"),
        ):
            fallback_result = sidecar.send_payload(
                1,
                {"ok": True},
                target="CJUAT728",
                text="AI回复",
                exact=False,
                skip_send_rate_guard=True,
                expected_context_guard=context_guard,
            )

        self.assertTrue(fallback_result["ok"])
        self.assertFalse(
            fallback_result["send_frame_reuse"]["fast_path_attempted"]
        )

    def test_all_pre_send_pacing_finishes_before_final_frame_capture(self):
        frame = Image.new("RGB", (960, 820), "white")
        events: list[tuple[object, ...]] = []

        def wait(low, high):
            events.append(("wait", low, high))

        def capture(_hwnd, *, artifact_dir=None, label="capture"):
            events.append(("capture", label))
            return frame, f"{label}.png"

        with (
            patch.object(sidecar, "activate_window"),
            patch.object(sidecar.time, "sleep"),
            patch.object(
                sidecar,
                "recover_send_window_guard",
                return_value={"ok": True},
            ),
            patch.object(sidecar, "human_client_click"),
            patch.object(
                sidecar,
                "type_text_with_sendinput_unicode",
                return_value={
                    "ok": True,
                    "method": "sendinput_unicode",
                    "typed_chars": 4,
                },
            ),
            patch.object(sidecar, "humanized_sleep_ms", side_effect=wait),
            patch.object(sidecar, "capture_wechat", side_effect=capture),
            patch.object(
                sidecar,
                "input_text_region_state",
                return_value={"has_visible_text": True},
            ),
            patch.object(
                sidecar,
                "input_region_visual_delta_confirms",
                return_value={"ok": True},
            ),
            patch.object(
                sidecar,
                "run_ocr_for_input_confirmation",
                return_value=([], "roi"),
            ),
            patch.object(
                sidecar,
                "send_button_ready_evidence",
                return_value={"ok": False},
            ),
        ):
            result = sidecar.paste_text_with_confirmation(
                1,
                "AI回复",
                points={"input_point": [650, 720], "send_point": None},
                geometry={"width": 960, "height": 820},
                settings={
                    "enabled": True,
                    "method": "sendinput_unicode",
                    "send_post_input_delay_min_ms": 11,
                    "send_post_input_delay_max_ms": 12,
                    "send_trigger_delay_min_ms": 21,
                    "send_trigger_delay_max_ms": 22,
                },
                before_input_region_seed={
                    "input_region": {"has_visible_text": False},
                },
                verified_input_point=(650, 720),
                verified_input_bounds=(400, 680, 880, 800),
            )

        self.assertTrue(result["ok"])
        self.assertEqual(
            events,
            [
                ("wait", 11, 12),
                ("wait", 21, 22),
                ("capture", "send_input_probe_1"),
            ],
        )

    def test_message_arriving_during_wait_is_seen_in_final_frame_and_blocks_enter(self):
        frame = Image.new("RGB", (960, 820), "white")
        frame.info["new_customer_message"] = True

        def context_check(*, screenshot=None, **_kwargs):
            return {
                "ok": not bool(
                    screenshot
                    and screenshot.info.get("new_customer_message")
                ),
                "error_code": "C3_CONTEXT_CHANGED_BEFORE_SEND",
            }

        with (
            patch.object(
                sidecar,
                "paste_text_with_confirmation",
                return_value={
                    "ok": True,
                    "input_result": {"ok": True, "typed_chars": 4},
                    "send_button_ready": {"ok": False},
                    "_post_input_screenshot": frame,
                },
            ),
            patch.object(
                sidecar,
                "clear_confirmed_program_draft",
                return_value={
                    "ok": True,
                    "cleared": True,
                    "reason": "confirmed_program_draft_cleared",
                },
            ),
            patch.object(sidecar, "safe_send_trigger") as trigger,
        ):
            result = sidecar.execute_send_transaction(
                1,
                "AI回复",
                locator={
                    "ok": True,
                    "path": "uia_input",
                    "input_point": (650, 720),
                    "input_click": {"bounds": [400, 680, 880, 800]},
                    "value_pattern": None,
                },
                geometry={"width": 960, "height": 820},
                before_send_trigger_check=context_check,
            )

        self.assertFalse(result["ok"])
        self.assertEqual(
            result["error_code"],
            "C3_CONTEXT_CHANGED_BEFORE_SEND",
        )
        trigger.assert_not_called()

    def test_action_journal_failure_never_presses_enter(self):
        with patch.object(sidecar, "key_press") as key_press:
            result = sidecar.safe_send_trigger(
                1,
                trigger_mode="enter_only",
                focus_guard_func=lambda: {"ok": True},
                before_physical_trigger=lambda: (_ for _ in ()).throw(
                    OSError("disk full")
                ),
            )

        self.assertFalse(result["ok"])
        self.assertEqual(
            result["error_code"],
            "SEND_ACTION_JOURNAL_WRITE_FAILED",
        )
        key_press.assert_not_called()

    def test_mismatched_copyback_releases_selection_without_editing_draft(self):
        with (
            patch.object(
                sidecar,
                "clipboard_read",
                side_effect=["原剪贴板", "销售人工草稿"],
            ),
            patch.object(sidecar, "clipboard_copy"),
            patch.object(sidecar, "human_client_click"),
            patch.object(
                sidecar,
                "recover_send_window_guard",
                return_value={"ok": True},
            ),
            patch.object(sidecar, "hotkey"),
            patch.object(sidecar, "key_press") as key_press,
            patch.object(sidecar, "humanized_action_sleep"),
        ):
            result = sidecar.confirm_exact_program_draft_focus(
                1,
                input_point=(650, 720),
                expected_text="AI回复",
            )

        self.assertFalse(result["ok"])
        self.assertEqual(
            result["reason"],
            "focused_input_draft_mismatch",
        )
        self.assertTrue(result["selection_released"])
        key_press.assert_called_once_with(0x27)

    def test_copyback_exception_after_select_all_releases_selection(self):
        with (
            patch.object(
                sidecar,
                "clipboard_read",
                return_value="原剪贴板",
            ),
            patch.object(sidecar, "clipboard_copy"),
            patch.object(sidecar, "human_client_click"),
            patch.object(
                sidecar,
                "recover_send_window_guard",
                return_value={"ok": True},
            ),
            patch.object(
                sidecar,
                "hotkey",
                side_effect=RuntimeError("select-all driver stopped"),
            ),
            patch.object(sidecar, "key_press") as key_press,
            patch.object(sidecar, "humanized_action_sleep"),
        ):
            result = sidecar.confirm_exact_program_draft_focus(
                1,
                input_point=(650, 720),
                expected_text="AI回复",
            )

        self.assertFalse(result["ok"])
        self.assertEqual(
            result["reason"],
            "input_focus_copyback_failed",
        )
        self.assertTrue(result["selection_released"])
        key_press.assert_called_once_with(0x27)

    def test_send_context_guard_blocks_new_message_after_final_refresh(self):
        frame = Image.new("RGB", (981, 860), "white")
        expected_rows = [
            {
                "row_kind": "text_bubble",
                "sender_role": "customer",
                "message_type": "text",
                "content_clean": "在吗",
                "bubble_rect": [480, 260, 620, 310],
            }
        ]
        current_rows = [
            *expected_rows,
            {
                "row_kind": "text_bubble",
                "sender_role": "customer",
                "message_type": "text",
                "content_clean": "补充一句",
                "bubble_rect": [480, 330, 650, 380],
            },
        ]
        expected = sidecar.build_send_context_guard(
            expected_rows,
            screenshot=frame,
        )
        current = sidecar.build_send_context_guard(
            current_rows,
            screenshot=frame,
        )

        result = self._validate_worker_send_context(
            expected,
            current,
            expected_observations=expected_rows,
            current_observations=current_rows,
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "C3_CONTEXT_CHANGED_BEFORE_SEND")
        self.assertEqual(result["expected_message_count"], 1)
        self.assertEqual(result["current_message_count"], 2)

    def test_sidecar_guard_does_not_decide_cross_frame_image_identity(self):
        layout = {
            "ok": True,
            "layout_snapshot_id": "same-slot-image-layout",
            "message_viewport_bounds": [382, 86, 980, 679],
        }

        def image_observation(fingerprint: str) -> dict[str, object]:
            return {
                "observation_id": f"image-{fingerprint}",
                "row_kind": "image_bubble",
                "sender_role": "customer",
                "message_type": "image",
                "bubble_rect": [480, 260, 720, 410],
                "image_physical_anchor": {
                    "bubble_visual_fingerprint": fingerprint,
                },
            }

        expected_rows = [image_observation("imagev2:picture-a")]
        current_rows = [image_observation("imagev2:picture-b")]
        expected = sidecar.build_send_context_guard(
            expected_rows,
            layout_evidence=layout,
        )
        current = sidecar.build_send_context_guard(
            current_rows,
            layout_evidence=layout,
        )
        comparison = self._validate_worker_send_context(
            expected,
            current,
            expected_observations=expected_rows,
            current_observations=current_rows,
        )
        # Sidecar owns the current-frame screenshot and click geometry.  It
        # must not turn a bubble crop fingerprint into a cross-frame message
        # identity decision.  The Worker checkpoint tests cover the separate
        # rule that an unproven image replacement blocks the old reply.
        self.assertFalse(comparison["ok"])
        self.assertEqual(
            comparison["continuity_relation"],
            "continuity_context_expansion_required",
        )
        self.assertEqual(
            expected["message_viewport_change_digest"],
            current["message_viewport_change_digest"],
        )

    def test_send_context_guard_absorbs_small_ocr_bounds_jitter(self):
        frame = Image.new("RGB", (981, 860), "white")
        expected_rows = [
            {
                "row_kind": "voice_transcript",
                "sender_role": "customer",
                "message_type": "voice",
                "content_clean": "我下午有空",
                "parent_voice_anchor_key": "voice:customer:4",
                "bubble_rect": [480, 260, 720, 310],
            }
        ]
        current_rows = [
            {**expected_rows[0], "bubble_rect": [481, 261, 721, 311]}
        ]
        expected = sidecar.build_send_context_guard(
            expected_rows,
            screenshot=frame,
        )
        current = sidecar.build_send_context_guard(
            current_rows,
            screenshot=frame,
        )

        result = self._validate_worker_send_context(
            expected,
            current,
            expected_observations=expected_rows,
            current_observations=current_rows,
        )

        self.assertTrue(result["ok"])

    def test_image_guard_keeps_geometry_jitter_out_of_business_and_contradiction_checks(self):
        layout = {
            "ok": True,
            "layout_snapshot_id": "same-image-jitter-layout",
            "message_viewport_bounds": [382, 86, 980, 679],
        }
        anchor = {
            "bubble_visual_fingerprint": "imagev2:same-action-evidence",
        }
        text_anchor = {
            "observation_id": "text-anchor",
            "row_kind": "text_bubble",
            "sender_role": "customer",
            "message_type": "text",
            "content_clean": "这是车辆照片",
            "bubble_rect": [480, 210, 720, 250],
        }
        expected_rows = [
            text_anchor,
            {
                "observation_id": "image-frame-a",
                "row_kind": "image_bubble",
                "sender_role": "customer",
                "message_type": "image",
                "bubble_rect": [480, 260, 720, 410],
                "image_physical_anchor": anchor,
            },
        ]
        current_rows = [
            text_anchor,
            {
                **expected_rows[1],
                "observation_id": "image-frame-b",
                "bubble_rect": [485, 265, 725, 415],
            },
        ]
        expected = sidecar.build_send_context_guard(
            expected_rows,
            layout_evidence=layout,
        )
        current = sidecar.build_send_context_guard(
            current_rows,
            layout_evidence=layout,
        )

        result = self._validate_worker_send_context(
            expected,
            current,
            expected_observations=expected_rows,
            current_observations=current_rows,
        )

        self.assertTrue(result["ok"], result)
        self.assertNotIn("bubble_rect", expected["sequence"][1])
        self.assertNotIn(
            "bubble_visual_fingerprint",
            expected["sequence"][1],
        )

    def test_send_context_guard_ignores_every_legacy_64_bucket_boundary(self):
        observation = {
            "row_kind": "text_bubble",
            "sender_role": "customer",
            "message_type": "text",
            "content_clean": "同一条好友通过消息",
        }
        total_crossings = 0
        media_geometry_changed_business_digest = False

        def legacy_relative_bucket(
            rect: list[int],
            viewport: list[int],
        ) -> list[int]:
            """Recreate the deleted 64-bucket rule only as test input.

            Production no longer exports or consumes this geometry rule.  The
            test enumerates every old boundary to prove none of them can alter
            the new business digest.
            """

            width = max(1, viewport[2] - viewport[0])
            height = max(1, viewport[3] - viewport[1])

            def bucket(value: int, origin: int, extent: int) -> int:
                return max(
                    0,
                    min(64, int(round((value - origin) / extent * 64))),
                )

            return [
                bucket(rect[0], viewport[0], width),
                bucket(rect[1], viewport[1], height),
                bucket(rect[2], viewport[0], width),
                bucket(rect[3], viewport[1], height),
            ]

        for viewport_width, viewport_height in (
            (640, 480),
            (981, 593),
            (1600, 900),
        ):
            viewport = [320, 80, 320 + viewport_width, 80 + viewport_height]
            layout = {
                "ok": True,
                "layout_snapshot_id": (
                    f"layout-{viewport_width}x{viewport_height}"
                ),
                "message_viewport_bounds": viewport,
            }
            bucket_crossings: list[tuple[int, list[int], list[int]]] = []
            for left in range(viewport[0], viewport[2] - 2):
                before_rect = [left, 180, left + 1, 220]
                after_rect = [left + 1, 180, left + 2, 220]
                before_bucket = legacy_relative_bucket(
                    before_rect,
                    viewport,
                )
                after_bucket = legacy_relative_bucket(
                    after_rect,
                    viewport,
                )
                if before_bucket[0] != after_bucket[0]:
                    bucket_crossings.append(
                        (left, before_bucket, after_bucket)
                    )
            # The legacy 0..64 quantizer exposes 64 bucket transitions.  The
            # last edge cannot hold a positive-width bubble, so at least the
            # 63 interior transitions must be exercised at every viewport.
            self.assertGreaterEqual(len(bucket_crossings), 63)
            for left, before_bucket, after_bucket in bucket_crossings:
                before = {
                    **observation,
                    "bubble_rect": [left, 180, left + 1, 220],
                }
                after = {
                    **observation,
                    "bubble_rect": [left + 1, 180, left + 2, 220],
                }
                expected = sidecar.build_send_context_guard(
                    [before],
                    layout_evidence=layout,
                )
                current = sidecar.build_send_context_guard(
                    [after],
                    layout_evidence=layout,
                )
                result = self._validate_worker_send_context(
                    expected,
                    current,
                    expected_observations=[before],
                    current_observations=[after],
                )
                self.assertTrue(
                    result["ok"],
                    (viewport, left, before_bucket, after_bucket, result),
                )
                self.assertNotIn(
                    "relative_quantized_bounds",
                    expected["sequence"][0],
                )
                total_crossings += 1

                media_before = sidecar.build_message_viewport_change_evidence(
                    [before],
                    layout_evidence=layout,
                )
                media_after = sidecar.build_message_viewport_change_evidence(
                    [after],
                    layout_evidence=layout,
                )
                if (
                    media_before["message_viewport_change_digest"]
                    != media_after["message_viewport_change_digest"]
                ):
                    media_geometry_changed_business_digest = True
        self.assertGreaterEqual(total_crossings, 63 * 3)
        # The shared projection answers only whether business facts changed.
        # Current-frame geometry remains available to Sidecar's click code,
        # but cannot create a second cross-frame change/identity decision.
        self.assertFalse(media_geometry_changed_business_digest)

    def test_send_context_guard_still_blocks_each_business_fact_change(self):
        layout = {
            "ok": True,
            "layout_snapshot_id": "business-change-layout",
            "message_viewport_bounds": [382, 86, 980, 679],
        }
        baseline = {
            "row_kind": "text_bubble",
            "sender_role": "customer",
            "message_type": "text",
            "content_clean": "想看十万左右的车",
            "bubble_rect": [480, 260, 720, 310],
        }
        expected = sidecar.build_send_context_guard(
            [baseline],
            layout_evidence=layout,
        )
        self.assertEqual(
            set(expected["sequence"][0]),
            {
                "screen_order",
                "sender_role",
                "message_type",
                "normalized_content_signature",
                "media_state",
            },
        )
        changes = {
            "role": [{**baseline, "sender_role": "self"}],
            "type": [
                {
                    **baseline,
                    "row_kind": "voice_transcript",
                    "message_type": "voice",
                }
            ],
            "content": [{**baseline, "content_clean": "改看十五万的车"}],
            "media_state": [
                {
                    **baseline,
                    "row_kind": "voice_bubble",
                    "message_type": "voice",
                    "content_clean": "",
                    "voice_duration": 3,
                }
            ],
        }
        for name, observations in changes.items():
            with self.subTest(name=name):
                current = sidecar.build_send_context_guard(
                    observations,
                    layout_evidence=layout,
                )
                result = self._validate_worker_send_context(
                    expected,
                    current,
                    expected_observations=[baseline],
                    current_observations=observations,
                )
                self.assertFalse(result["ok"])
                self.assertEqual(
                    result["error_code"],
                    "C3_CONTEXT_CHANGED_BEFORE_SEND",
                )

        first = {**baseline, "content_clean": "第一条"}
        second = {
            **baseline,
            "content_clean": "第二条",
            "bubble_rect": [480, 330, 720, 380],
        }
        expected_order = sidecar.build_send_context_guard(
            [first, second],
            layout_evidence=layout,
        )
        current_order_rows = [
            {**first, "bubble_rect": second["bubble_rect"]},
            {**second, "bubble_rect": first["bubble_rect"]},
        ]
        current_order = sidecar.build_send_context_guard(
            current_order_rows,
            layout_evidence=layout,
        )
        order_result = self._validate_worker_send_context(
            expected_order,
            current_order,
            expected_observations=[first, second],
            current_observations=current_order_rows,
        )
        self.assertFalse(order_result["ok"])
        self.assertEqual(
            order_result["error_code"],
            "C3_CONTEXT_CHANGED_BEFORE_SEND",
        )

    def test_send_business_order_never_uses_frame_local_observation_id(self):
        layout = {
            "ok": True,
            "layout_snapshot_id": "observation-id-is-diagnostic-only",
            "message_viewport_bounds": [382, 86, 980, 679],
        }
        first = {
            "observation_id": "z-frame-a",
            "row_kind": "text_bubble",
            "sender_role": "customer",
            "message_type": "text",
            "content_clean": "第一条",
            "bubble_rect": [480, 260, 720, 310],
        }
        second = {
            "observation_id": "a-frame-a",
            "row_kind": "text_bubble",
            "sender_role": "customer",
            "message_type": "text",
            "content_clean": "第二条",
            "bubble_rect": [480, 260, 720, 310],
        }

        expected = sidecar.build_send_context_guard(
            [first, second],
            layout_evidence=layout,
        )
        current_rows = [
            {**first, "observation_id": "a-frame-b"},
            {**second, "observation_id": "z-frame-b"},
        ]
        current = sidecar.build_send_context_guard(
            current_rows,
            layout_evidence=layout,
        )

        self.assertTrue(
            self._validate_worker_send_context(
                expected,
                current,
                expected_observations=[first, second],
                current_observations=current_rows,
            )["ok"]
        )

    def test_s0_s1_s2_share_geometry_free_business_comparison(self):
        layout = {
            "ok": True,
            "layout_snapshot_id": "s0-s1-s2-layout",
            "message_viewport_bounds": [382, 86, 980, 679],
        }
        business_rows = [
            {
                "row_kind": "system_message",
                "sender_role": "system",
                "message_type": "system",
                "content_clean": "我通过了你的朋友验证请求",
                "bubble_rect": [560, 250, 800, 280],
            },
            {
                "row_kind": "voice_transcript",
                "sender_role": "customer",
                "message_type": "voice",
                "content_clean": "十万左右的车有什么推荐",
                "bubble_rect": [480, 330, 760, 380],
            },
        ]
        expected = sidecar.build_send_context_guard(
            business_rows,
            layout_evidence=layout,
        )
        for stage, delta in (("S0", 0), ("S1", 1), ("S2", -2)):
            current_rows = [
                {
                    **row,
                    "bubble_rect": [
                        int(row["bubble_rect"][0]) + delta,
                        int(row["bubble_rect"][1]) + delta,
                        int(row["bubble_rect"][2]) + delta,
                        int(row["bubble_rect"][3]) + delta,
                    ],
                }
                for row in business_rows
            ]
            current = sidecar.build_send_context_guard(
                current_rows,
                layout_evidence=layout,
            )
            with self.subTest(stage=stage):
                self.assertTrue(
                    self._validate_worker_send_context(
                        expected,
                        current,
                        expected_observations=business_rows,
                        current_observations=current_rows,
                    )["ok"]
                )

    def test_send_context_guard_ignores_sidebar_only_visual_change(self):
        before = Image.new("RGB", (981, 860), "white")
        after = before.copy()
        ImageDraw.Draw(before).rectangle((40, 210, 75, 235), fill="red")
        ImageDraw.Draw(after).rectangle((40, 210, 88, 235), fill="red")
        expected_rows = [
            {
                "row_kind": "text_bubble",
                "sender_role": "customer",
                "message_type": "text",
                "content_clean": "明天继续磨，有点烦了",
                "bubble_rect": [480, 260, 760, 310],
            }
        ]
        current_rows = [
            {
                **expected_rows[0],
                "content_clean": "明天继续磨有点烦了",
            }
        ]
        expected = sidecar.build_send_context_guard(
            expected_rows,
            screenshot=before,
        )
        # Full-window OCR can vary when an unrelated sidebar badge changes.
        current = sidecar.build_send_context_guard(
            current_rows,
            screenshot=after,
        )

        result = self._validate_worker_send_context(
            expected,
            current,
            expected_observations=expected_rows,
            current_observations=current_rows,
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["reason"], "message_sequence_unchanged")
        self.assertFalse(expected["raw_rgb_hash_used"])
        self.assertEqual(
            expected["message_viewport_change_digest"],
            current["message_viewport_change_digest"],
        )

    def test_send_context_guard_blocks_new_message_without_raw_pixel_hash(self):
        before = Image.new("RGB", (981, 860), "white")
        after = before.copy()
        ImageDraw.Draw(after).rectangle((520, 520, 800, 570), fill="gray")

        expected_rows = [
            {
                "row_kind": "text_bubble",
                "sender_role": "customer",
                "message_type": "text",
                "content_clean": "在吗",
                "bubble_rect": [480, 260, 620, 310],
            }
        ]
        current_rows = [
            *expected_rows,
            {
                "row_kind": "text_bubble",
                "sender_role": "customer",
                "message_type": "text",
                "content_clean": "补充一句",
                "bubble_rect": [480, 330, 650, 380],
            },
        ]

        expected = sidecar.build_send_context_guard(
            expected_rows,
            screenshot=before,
        )
        current = sidecar.build_send_context_guard(
            current_rows,
            screenshot=after,
        )

        result = self._validate_worker_send_context(
            expected,
            current,
            expected_observations=expected_rows,
            current_observations=current_rows,
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "C3_CONTEXT_CHANGED_BEFORE_SEND")
        self.assertFalse(expected["raw_rgb_hash_used"])
        self.assertFalse(current["raw_rgb_hash_used"])

    def test_viewport_digest_does_not_repair_unmerged_visual_voice_hint(self):
        frame = Image.new("RGB", (981, 860), "white")
        ocr_voice = {
            "observation_id": "win32_ocr:voice-1",
            "row_kind": "voice_bubble",
            "sender_role": "customer",
            "message_type": "voice",
            "voice_state": "untranscribed",
            "voice_duration": 4,
            "voice_duration_text": '4"',
            "bubble_rect": [479, 403, 523, 425],
            "quality_flags": ["untranscribed_voice_placeholder"],
            "source_message": {"id": "win32_ocr:voice-1"},
        }
        visual_hint = {
            "observation_id": "voice-hint:voice-stable:one",
            "row_kind": "voice_bubble",
            "sender_role": "customer",
            "message_type": "voice",
            "voice_state": "playing",
            "bubble_rect": [479, 403, 523, 425],
            "quality_flags": ["visual_voice_hint"],
            "source_message": {},
        }

        without_hint = sidecar.build_message_viewport_change_evidence(
            [ocr_voice], screenshot=frame
        )
        with_hint = sidecar.build_message_viewport_change_evidence(
            [visual_hint, ocr_voice], screenshot=frame
        )

        self.assertEqual(without_hint["message_count"], 1)
        self.assertEqual(with_hint["message_count"], 2)
        self.assertNotEqual(
            without_hint["message_viewport_change_digest"],
            with_hint["message_viewport_change_digest"],
        )

    def test_send_business_projection_does_not_second_merge_raw_voice_rows(self):
        layout = {
            "ok": True,
            "layout_snapshot_id": "raw-duplicate-contract-fixture",
            "message_viewport_bounds": [360, 100, 980, 800],
        }
        ocr_voice = {
            "observation_id": "win32_ocr:voice-duplicate",
            "row_kind": "voice_bubble",
            "sender_role": "customer",
            "message_type": "voice",
            "voice_state": "untranscribed",
            "voice_duration": 4,
            "bubble_rect": [479, 403, 523, 425],
            "quality_flags": ["untranscribed_voice_placeholder"],
        }
        visual_hint = {
            "observation_id": "voice-hint:voice-duplicate",
            "row_kind": "voice_bubble",
            "sender_role": "customer",
            "message_type": "voice",
            "voice_state": "untranscribed",
            "voice_duration": 4,
            "bubble_rect": [479, 403, 523, 425],
            "quality_flags": ["visual_voice_hint"],
        }

        guard = sidecar.build_send_context_guard(
            [visual_hint, ocr_voice],
            layout_evidence=layout,
        )

        self.assertTrue(guard["ok"])
        self.assertEqual(guard["message_count"], 2)
        self.assertEqual(len(guard["sequence"]), 2)

    def test_viewport_digest_orders_facts_by_screen_not_detector_order(self):
        frame = Image.new("RGB", (981, 860), "white")
        first = {
            "observation_id": "row-first",
            "row_kind": "text_bubble",
            "sender_role": "customer",
            "message_type": "text",
            "content_clean": "第一条",
            "bubble_rect": [480, 260, 620, 310],
        }
        second = {
            "observation_id": "row-second",
            "row_kind": "voice_bubble",
            "sender_role": "customer",
            "message_type": "voice",
            "voice_duration": 4,
            "bubble_rect": [480, 330, 650, 380],
        }

        expected = sidecar.build_message_viewport_change_evidence(
            [first, second], screenshot=frame
        )
        reversed_detector_output = sidecar.build_message_viewport_change_evidence(
            [second, first], screenshot=frame
        )

        self.assertEqual(
            expected["message_viewport_change_digest"],
            reversed_detector_output["message_viewport_change_digest"],
        )
        self.assertEqual(
            [row["message_type"] for row in expected["sequence"]],
            ["text", "voice"],
        )

    def test_pre_enter_target_switch_blocks_even_when_message_region_is_identical(self):
        geometry = {
            "left": 0,
            "top": 0,
            "right": 981,
            "bottom": 860,
            "width": 981,
            "height": 860,
        }
        context_guard = {
            "schema_version": 3,
            "sequence": [],
            "message_count": 0,
            "bottom": None,
            "message_viewport_change_digest": "a" * 64,
            "sequence_sha256": "a" * 64,
            "raw_rgb_hash_used": False,
        }
        context_guard = self._worker_send_guard(
            context_guard,
            [],
            empty_top_boundary=True,
        )
        strict_target = {
            "ok": True,
            "online": True,
            "reason": "target_confirmed",
            "confirmation_confidence": "active_title_strict",
            "geometry": geometry,
        }
        baseline = {
            "ok": True,
            "validation": strict_target,
            "input_region": {"has_visible_text": False},
            "matching_self_message_count": 0,
            "message_sequence": [],
            "observations": [],
            "send_context_guard": context_guard,
        }
        switched_target = {
            **baseline,
            "ok": False,
            "validation": {
                "ok": False,
                "reason": "active_title_target_mismatch",
                "confirmation_confidence": "target_mismatch",
                "geometry": geometry,
            },
        }
        captured_check: dict[str, object] = {}

        def reject_after_target_switch(*_args, **kwargs):
            check = kwargs["before_send_trigger_check"]()
            captured_check.update(check)
            return {
                **check,
                "physical_send_triggered": False,
            }

        with (
            patch.object(sidecar, "recover_send_window_guard", return_value={"ok": True}),
            patch.object(sidecar, "active_send_guard_is_strong", side_effect=lambda value: value.get("confirmation_confidence") == "active_title_strict"),
            patch.object(sidecar, "get_window_geometry", return_value=geometry),
            patch.object(sidecar, "validate_send_geometry", return_value={"ok": True}),
            patch.object(sidecar, "consume_input_region_precheck_ocr_seed", return_value=None),
            patch.object(sidecar, "capture_send_fact_snapshot", side_effect=[baseline, switched_target]),
            patch.object(sidecar, "send_with_visual_input", side_effect=reject_after_target_switch),
        ):
            result = sidecar.send_payload(
                1,
                {"ok": True},
                target="CJV6P3R8",
                text="AI回复",
                exact=False,
                skip_send_rate_guard=True,
                expected_context_guard=context_guard,
                validated_guard=strict_target,
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "SEND_TARGET_NOT_CONFIRMED")
        self.assertEqual(captured_check["reason"], "send_target_not_confirmed_before_enter")
        self.assertFalse(result["physical_send_triggered"])

    def test_pre_enter_s1_roi_message_miss_uses_same_frame_full_ocr(self):
        frame = Image.new("RGB", (980, 860), "white")
        self._semantic_layout_for_image(frame)
        geometry = {
            "left": 0,
            "top": 0,
            "right": 980,
            "bottom": 860,
            "width": 980,
            "height": 860,
        }
        message = {
            "id": "customer-1",
            "type": "text",
            "message_type": "text",
            "sender": "customer",
            "sender_role": "customer",
            "content": "想看十万左右的车",
            "bubble_rect": [410, 200, 650, 240],
            "avatar_alignment": {"role": "customer", "confirmed": True},
        }
        layout_evidence = {
            "ok": True,
            "message_viewport_bounds": [382, 86, 980, 679],
        }
        expected_observations = sidecar.build_message_observations_v3(
            [message]
        )
        expected_guard = sidecar.build_send_context_guard(
            expected_observations,
            screenshot=frame,
            layout_evidence=layout_evidence,
        )
        expected_guard = self._worker_send_guard(
            expected_guard,
            expected_observations,
        )
        baseline = {
            "ok": True,
            "validation": {
                "ok": True,
                "online": True,
                "reason": "target_confirmed",
                "confirmation_confidence": "active_title_strict",
                "geometry": geometry,
            },
            "input_region": {"has_visible_text": False},
            "matching_self_message_count": 0,
            "message_sequence": [],
            "observations": expected_observations,
            "send_context_guard": expected_guard,
            "frame_observation": {
                "frame_id": "s0-frame",
                "screenshot_sha256": "a" * 64,
            },
        }
        observed_check: dict[str, object] = {}

        def parse_messages(items, *_args, **_kwargs):
            return [message] if items and items[0].get("source") == "full" else []

        def stop_after_s1(*_args, **kwargs):
            check = kwargs["before_send_trigger_check"](
                screenshot=frame,
                screenshot_path="s1-same-frame.png",
            )
            observed_check.update(check)
            return {
                "ok": False,
                "reason": "test_stop_after_s1",
                "error_code": "TEST_STOP_AFTER_S1",
                "physical_send_triggered": False,
                "context_check": check,
            }

        with (
            patch.object(sidecar, "recover_send_window_guard", return_value={"ok": True}),
            patch.object(sidecar, "active_send_guard_is_strong", return_value=True),
            patch.object(sidecar, "validate_send_geometry", return_value={"ok": True}),
            patch.object(sidecar, "capture_send_fact_snapshot", return_value=baseline),
            patch.object(sidecar, "send_with_visual_input", side_effect=stop_after_s1),
            patch.object(
                sidecar,
                "run_ocr_for_chat_fact_frame",
                return_value=(
                    [{"source": "roi"}],
                    {
                        "source": "chat_fact_roi",
                        "regions": [
                            "chat_header_bounds",
                            "message_viewport_bounds",
                            "input_bounds",
                        ],
                        "ocr_call_count": 3,
                    },
                ),
            ),
            patch.object(
                sidecar,
                "run_ocr_traced",
                return_value=[{"source": "full"}],
            ) as full_ocr,
            patch.object(
                sidecar,
                "validate_active_send_target",
                return_value={
                    "ok": True,
                    "reason": "target_confirmed",
                    "confirmation_confidence": "active_title_strict",
                    "geometry": geometry,
                },
            ),
            patch.object(sidecar, "get_window_geometry", return_value=geometry),
            patch.object(
                sidecar,
                "parse_current_chat_frame_messages",
                side_effect=parse_messages,
            ),
            patch.object(
                sidecar,
                "basic_chat_layout_evidence",
                return_value=layout_evidence,
            ),
            patch.object(
                sidecar,
                "input_text_region_state",
                return_value={"has_visible_text": False},
            ),
        ):
            result = sidecar.send_payload(
                1,
                {"ok": True},
                target="CJTEST01",
                text="AI回复",
                exact=False,
                skip_send_rate_guard=True,
                expected_context_guard=expected_guard,
            )

        self.assertFalse(result["ok"])
        self.assertTrue(observed_check["ok"])
        self.assertEqual(
            observed_check["snapshot"]["ocr_plan"]["fallback_reason"],
            "message_context_evidence_insufficient",
        )
        full_ocr.assert_called_once_with(
            frame,
            "send_pre_trigger_context_reused_chat_fact_fallback_full",
            source="build_send_fact_snapshot_from_frame",
        )

    def test_send_reply_match_count_requires_self_role_and_exact_normalized_text(self):
        messages = [
            {"sender_role": "customer", "content": "您好，可以继续沟通"},
            {"sender_role": "self", "content": "您好， 可以继续沟通"},
            {"sender_role": "self", "content": "另一条"},
        ]

        self.assertEqual(sidecar.send_reply_match_count(messages, "您好，可以继续沟通"), 1)

    def test_unknown_input_draft_is_preserved_without_any_delete_key(self):
        geometry = {"width": 960, "height": 820}
        before_seed = {
            "input_region": {
                "has_visible_text": True,
                "reason": "ocr_or_dark_pixels",
                "ocr_hits": 1,
                "dark_ratio": 0.03,
                "mean": 240.0,
            }
        }
        with (
            patch.object(sidecar, "activate_window"),
            patch.object(sidecar, "recover_send_window_guard", return_value={"ok": True}),
            patch.object(sidecar, "key_press") as key_press,
            patch.object(sidecar, "human_client_click") as click,
            patch.object(sidecar.time, "sleep"),
        ):
            result = sidecar.paste_text_with_confirmation(
                1,
                "AI回复",
                points={"input_point": [650, 720], "send_point": [900, 775]},
                geometry=geometry,
                before_input_region_seed=before_seed,
                verified_input_point=(650, 720),
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "unknown_input_draft_present")
        self.assertEqual(result["error_code"], "WECHAT_INPUT_DRAFT_PRESENT")
        key_press.assert_not_called()
        click.assert_not_called()

    def test_toolbar_pixels_are_excluded_from_draft_detection_but_click_surface_is_preserved(self):
        image = Image.new("RGB", (920, 991), "white")
        snapshot = self._semantic_layout_for_image(image)
        snapshot.update(
            {
                "message_viewport_bounds": [374, 112, 920, 835],
                "input_bounds": [379, 841, 833, 940],
                "toolbar_bounds": [374, 940, 920, 991],
            }
        )
        draw = ImageDraw.Draw(image)
        # Reproduce the incident shape: bottom-toolbar glyphs touch the old
        # draft ROI while the editable text surface itself is empty.
        for left in (399, 445, 490, 545, 600, 760):
            draw.rectangle((left, 936, left + 10, 940), fill="black")

        state = sidecar.input_text_region_state(
            image,
            [],
            geometry={"width": 938, "height": 1000},
        )

        self.assertFalse(state["has_visible_text"])
        self.assertEqual(state["click_bounds"], [379, 841, 833, 940])
        self.assertLess(state["bounds"][3], 936)
        locator = sidecar.locate_visual_send_input(
            before_input_region_seed={"input_region": state}
        )
        self.assertTrue(locator["ok"])
        self.assertEqual(
            locator["input_click_evidence"]["bounds"],
            [379, 841, 833, 940],
        )

    def test_short_real_draft_inside_text_region_still_blocks_send(self):
        image = Image.new("RGB", (920, 991), "white")
        snapshot = self._semantic_layout_for_image(image)
        snapshot.update(
            {
                "message_viewport_bounds": [374, 112, 920, 835],
                "input_bounds": [379, 841, 833, 940],
                "toolbar_bounds": [374, 940, 920, 991],
            }
        )
        text_bounds = sidecar.win32_ocr_layout.input_text_detection_bounds(snapshot)
        draw = ImageDraw.Draw(image)
        draw.rectangle(
            (
                text_bounds[0] + 18,
                text_bounds[1] + 14,
                text_bounds[0] + 27,
                text_bounds[1] + 27,
            ),
            fill="black",
        )

        state = sidecar.input_text_region_state(
            image,
            [],
            geometry={"width": 938, "height": 1000},
        )
        locator = sidecar.locate_visual_send_input(
            before_input_region_seed={"input_region": state}
        )

        self.assertTrue(state["has_visible_text"])
        self.assertEqual(state["ocr_hits"], 0)
        self.assertFalse(locator["ok"])
        self.assertEqual(locator["error_code"], "WECHAT_INPUT_DRAFT_PRESENT")

    def test_confirmed_program_draft_is_cleared_after_input_confirmation_failure(self):
        class FakeValuePattern:
            def __init__(self):
                self.value = "AI回复"

            @property
            def Value(self):
                return self.value

        value_pattern = FakeValuePattern()

        def clear_value(_key):
            value_pattern.value = ""

        with (
            patch.object(sidecar, "human_client_click"),
            patch.object(sidecar, "hotkey"),
            patch.object(sidecar, "key_press", side_effect=clear_value),
            patch.object(sidecar, "humanized_action_sleep"),
        ):
            result = sidecar.clear_confirmed_program_draft(
                1,
                value_pattern=value_pattern,
                input_point=(650, 720),
                expected_text="AI回复",
                paste_result={
                    "input_result": {
                        "ok": True,
                        "typed_chars": 4,
                        "method": "clipboard_chunks",
                    }
                },
            )

        self.assertTrue(result["ok"])
        self.assertTrue(result["cleared"])
        self.assertEqual(result["reason"], "confirmed_program_draft_cleared")
        self.assertEqual(value_pattern.value, "")

    def test_clipboard_confirmed_program_draft_reuses_existing_focus_without_second_click(self):
        class InitiallyUnreadableValuePattern:
            def __init__(self):
                self.reads = 0

            @property
            def Value(self):
                self.reads += 1
                if self.reads == 1:
                    raise RuntimeError("UIA value temporarily unavailable")
                return ""

        value_pattern = InitiallyUnreadableValuePattern()
        with (
            patch.object(
                sidecar,
                "confirm_exact_program_draft_focus",
                return_value={"ok": True, "reason": "verified_input_focused_with_exact_program_draft"},
            ),
            patch.object(sidecar, "human_client_click") as click,
            patch.object(sidecar, "hotkey") as hotkey,
            patch.object(sidecar, "key_press") as key_press,
            patch.object(sidecar, "humanized_action_sleep"),
        ):
            result = sidecar.clear_confirmed_program_draft(
                1,
                value_pattern=value_pattern,
                input_point=(650, 720),
                expected_text="AI回复",
                paste_result={
                    "input_result": {
                        "ok": True,
                        "typed_chars": 4,
                        "method": "clipboard_chunks",
                    }
                },
            )

        self.assertTrue(result["ok"])
        self.assertTrue(result["cleared"])
        click.assert_not_called()
        hotkey.assert_not_called()
        key_press.assert_called_once_with(sidecar.win32con.VK_BACK)

    def test_unproven_or_changed_draft_is_never_cleared(self):
        value_pattern = SimpleNamespace(Value="销售人工草稿")
        with (
            patch.object(sidecar, "human_client_click") as click,
            patch.object(sidecar, "hotkey") as hotkey,
            patch.object(sidecar, "key_press") as key_press,
        ):
            result = sidecar.clear_confirmed_program_draft(
                1,
                value_pattern=value_pattern,
                input_point=(650, 720),
                expected_text="AI回复",
                paste_result={
                    "input_result": {
                        "ok": True,
                        "typed_chars": 4,
                        "method": "clipboard_chunks",
                    }
                },
            )

        self.assertFalse(result["ok"])
        self.assertFalse(result["cleared"])
        self.assertEqual(result["reason"], "program_draft_not_proven")
        click.assert_not_called()
        hotkey.assert_not_called()
        key_press.assert_not_called()

    def test_sent_confirmation_requires_new_matching_bubble_and_empty_input(self):
        baseline_sequence = [
            {
                "sequence_index": 0,
                "observation_id": "customer-1",
                "row_kind": "text_bubble",
                "sender_role": "customer",
                "content_normalized": "在吗",
            }
        ]
        snapshots = [
            {
                "ok": True,
                "matching_self_message_count": 1,
                "input_region": {"has_visible_text": True},
                "message_sequence": baseline_sequence,
                "observations": [],
            },
            {
                "ok": True,
                "matching_self_message_count": 2,
                "input_region": {"has_visible_text": False},
                "message_sequence": [
                    *baseline_sequence,
                    {
                        "sequence_index": 1,
                        "observation_id": "self-new",
                        "row_kind": "text_bubble",
                        "sender_role": "self",
                        "content_normalized": "AI回复",
                    },
                ],
                "observations": [
                    {
                        "observation_id": "self-new",
                        "row_kind": "text_bubble",
                        "sender_role": "self",
                        "content_clean": "AI回复",
                    }
                ],
            },
        ]
        with (
            patch.object(sidecar, "capture_send_fact_snapshot", side_effect=snapshots),
            patch.object(sidecar.time, "sleep"),
        ):
            result = sidecar.confirm_reply_sent(
                1,
                target="CJTEST01",
                text="AI回复",
                exact=False,
                baseline_match_count=1,
                baseline_message_sequence=baseline_sequence,
                artifact_dir=None,
                max_attempts=2,
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["reason"], "new_stable_self_bubble_and_empty_input")
        self.assertEqual(result["attempt"], 2)
        self.assertEqual(result["confirmed_observation"]["observation_id"], "self-new")

    def test_post_send_enhanced_ocr_recovers_exact_self_text_from_image_candidate(self):
        frame = Image.new("RGB", (980, 860), "white")
        structural_image = {
            "id": "visual-self-candidate",
            "type": "image",
            "message_type": "image",
            "sender": "unknown",
            "sender_role": "unknown",
            "visual_side": "self",
            "is_self_image": True,
            "content": "[图片]",
            "bubble_rect": [489, 505, 878, 613],
            "avatar_alignment": {"role": "self", "confirmed": True},
        }

        def enhanced_ocr(_image):
            return [
                {
                    "text": "你好，10万左右我可以按预算帮你筛选。",
                    "left": 20,
                    "top": 16,
                    "right": 410,
                    "bottom": 48,
                    "center_x": 215,
                    "center_y": 32,
                    "confidence": 0.99,
                },
                {
                    "text": "你更偏向轿车还是SUV？",
                    "left": 20,
                    "top": 58,
                    "right": 310,
                    "bottom": 90,
                    "center_x": 165,
                    "center_y": 74,
                    "confidence": 0.98,
                },
            ]

        expected = "你好，10万左右我可以按预算帮你筛选。你更偏向轿车还是SUV？"
        messages, diagnostics = (
            sidecar.recover_expected_self_text_from_structural_candidates(
                frame,
                [structural_image],
                target="CJTEST01",
                expected_text=expected,
                ocr_runner=enhanced_ocr,
            )
        )

        self.assertTrue(diagnostics["attempted"])
        self.assertTrue(diagnostics["recovered"])
        self.assertEqual(messages[0]["type"], "text")
        self.assertEqual(messages[0]["sender_role"], "self")
        self.assertEqual(
            sidecar.normalized_send_confirmation_text(messages[0]["content"]),
            sidecar.normalized_send_confirmation_text(expected),
        )
        self.assertIn(
            "send_confirmation_enhanced_roi_ocr",
            messages[0]["quality_flags"],
        )

    def test_post_send_enhanced_ocr_recovers_incident_fullwidth_question_mark(self):
        """Regression for the 2026-08-15 long green-bubble incident.

        OCR recognized all content but emitted an ASCII question mark for the
        visible full-width Chinese question mark. Raw equality kept the text
        bubble classified as an image for all six confirmation attempts.
        """

        frame = Image.new("RGB", (980, 860), "white")
        structural_image = {
            "id": "incident-self-structural-candidate",
            "type": "image",
            "message_type": "image",
            "sender": "self",
            "sender_role": "self",
            "visual_side": "self",
            "is_self_image": True,
            "content": "[图片]",
            "bubble_rect": [489, 532, 878, 653],
            "avatar_alignment": {"role": "self", "confirmed": True},
        }

        def incident_enhanced_ocr(_image):
            return incident_post_send_enhanced_ocr_items()

        messages, diagnostics = (
            sidecar.recover_expected_self_text_from_structural_candidates(
                frame,
                [structural_image],
                target="CJMU5YT9",
                expected_text=INCIDENT_LONG_REPLY,
                ocr_runner=incident_enhanced_ocr,
            )
        )

        self.assertTrue(diagnostics["attempted"])
        self.assertTrue(diagnostics["recovered"])
        self.assertEqual(messages[0]["type"], "text")
        self.assertEqual(messages[0]["sender_role"], "self")
        self.assertEqual(
            sidecar._normalized_send_ocr_correspondence_text(
                messages[0]["content"]
            ),
            sidecar._normalized_send_ocr_correspondence_text(INCIDENT_LONG_REPLY),
        )
        self.assertEqual(diagnostics["expected_coverage"], 1.0)
        self.assertEqual(diagnostics["observed_coverage"], 1.0)
        self.assertEqual(
            diagnostics["text_correspondence_reason"],
            "unicode_normalized_exact_program_text",
        )

    def test_post_send_enhanced_ocr_one_real_character_change_is_high_overlap_text(self):
        frame = Image.new("RGB", (980, 860), "white")
        structural_image = {
            "id": "self-image-one-character-different",
            "type": "image",
            "message_type": "image",
            "sender_role": "self",
            "visual_side": "self",
            "bubble_rect": [489, 532, 878, 653],
            "avatar_alignment": {"role": "self", "confirmed": True},
        }
        changed_items = incident_post_send_enhanced_ocr_items()
        changed_items[3]["text"] = "通勤还是跑短途，更"

        messages, diagnostics = (
            sidecar.recover_expected_self_text_from_structural_candidates(
                frame,
                [structural_image],
                target="CJMU5YT9",
                expected_text=INCIDENT_LONG_REPLY,
                ocr_runner=lambda _image: changed_items,
            )
        )

        self.assertTrue(diagnostics["attempted"])
        self.assertTrue(diagnostics["reclassified_as_text"])
        self.assertTrue(diagnostics["recovered"])
        self.assertEqual(
            diagnostics["text_correspondence_reason"],
            "high_overlap_program_text",
        )
        self.assertGreater(diagnostics["expected_coverage"], 0.95)
        self.assertEqual(messages[0]["type"], "text")

    def test_post_send_enhanced_ocr_low_overlap_does_not_confirm_ai_reply(self):
        correspondence = sidecar._send_ocr_text_correspondence(
            INCIDENT_LONG_REPLY,
            "10万左右二手车推荐，点击图片查看车型和价格",
        )

        self.assertFalse(correspondence["accepted"])
        self.assertEqual(correspondence["reason"], "ocr_text_low_overlap")
        self.assertLess(correspondence["expected_coverage"], 0.80)

    def test_send_ocr_format_canonicalization_matrix(self):
        equivalent_pairs = [
            ("ＡＩ回复１２３！？", "ai回复123!?"),
            ("好的。", "好的."),
            ("“可以”", '"可以"'),
            ("‘可以’", "'可以'"),
            ("稍等……", "稍等..."),
            ("A—B–C", "a-b-c"),
            ("【推荐】", "[推荐]"),
            ("「推荐」", '"推荐"'),
            ("SUV\u00a0推荐\u200b", "suv推荐"),
            ("已完成👍️", "已完成👍"),
        ]

        for expected, observed in equivalent_pairs:
            with self.subTest(expected=expected, observed=observed):
                correspondence = sidecar._send_ocr_text_correspondence(
                    expected,
                    observed,
                )
                self.assertTrue(correspondence["accepted"])
                self.assertTrue(correspondence["exact"])

    def test_send_ocr_high_overlap_threshold_boundaries(self):
        expected = "abcdefghijklmnopqrst"

        accepted = sidecar._send_ocr_text_correspondence(
            expected,
            "abcdefghijklmnop",
        )
        rejected = sidecar._send_ocr_text_correspondence(
            expected,
            "abcdefghijklmno",
        )
        reordered = sidecar._send_ocr_text_correspondence(
            expected,
            "ponmlkjihgfedcba",
        )

        self.assertTrue(accepted["accepted"])
        self.assertEqual(accepted["expected_coverage"], 0.8)
        self.assertEqual(accepted["reason"], "high_overlap_program_text")
        self.assertFalse(rejected["accepted"])
        self.assertLess(rejected["expected_coverage"], 0.8)
        self.assertFalse(reordered["accepted"])

    def test_send_context_sequence_ignores_ocr_format_only_differences(self):
        frame = Image.new("RGB", (981, 860), "white")
        expected_rows = [
            {
                "row_kind": "text_bubble",
                "sender_role": "customer",
                "message_type": "text",
                "content_clean": "请看【车型Ａ】……",
                "bubble_rect": [480, 260, 720, 310],
            }
        ]
        current_rows = [
            {**expected_rows[0], "content_clean": "请看[车型a]..."}
        ]
        expected = sidecar.build_send_context_guard(
            expected_rows,
            screenshot=frame,
        )
        current = sidecar.build_send_context_guard(
            current_rows,
            screenshot=frame,
        )

        result = self._validate_worker_send_context(
            expected,
            current,
            expected_observations=expected_rows,
            current_observations=current_rows,
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["reason"], "message_sequence_unchanged")

    def test_format_drift_on_old_row_cannot_become_new_send_fact(self):
        baseline = [
            {
                "sequence_index": 0,
                "observation_id": "self-old",
                "row_kind": "text_bubble",
                "sender_role": "self",
                "content_normalized": "好的……",
            }
        ]
        current = [
            {
                "sequence_index": 0,
                "observation_id": "self-old-new-ocr-id",
                "row_kind": "text_bubble",
                "sender_role": "self",
                "content_normalized": "好的...",
            }
        ]

        self.assertIsNone(
            sidecar.find_new_matching_self_message(
                baseline,
                current,
                "好的……",
            )
        )

    def test_post_send_enhanced_ocr_nearby_text_is_text_but_not_ai_reply(self):
        frame = Image.new("RGB", (980, 860), "white")
        structural_image = {
            "id": "real-self-image-with-marketing-copy",
            "type": "image",
            "message_type": "image",
            "sender_role": "self",
            "visual_side": "self",
            "bubble_rect": [489, 532, 878, 653],
            "avatar_alignment": {"role": "self", "confirmed": True},
        }

        def image_text_ocr(_image):
            return [
                {
                    "text": "10万左右二手车推荐，点击图片查看车型和价格",
                    "left": 20,
                    "top": 20,
                    "right": 620,
                    "bottom": 52,
                    "center_x": 320,
                    "center_y": 36,
                    "confidence": 0.99,
                }
            ]

        messages, diagnostics = (
            sidecar.recover_expected_self_text_from_structural_candidates(
                frame,
                [structural_image],
                target="CJMU5YT9",
                expected_text=(
                    "可以，10万左右可以按你的需求帮你筛选合适的二手车。"
                    "你主要家用、通勤还是跑长途？"
                ),
                ocr_runner=image_text_ocr,
            )
        )

        self.assertTrue(diagnostics["attempted"])
        self.assertTrue(diagnostics["reclassified_as_text"])
        self.assertFalse(diagnostics["recovered"])
        self.assertFalse(diagnostics["ai_reply_correspondence_confirmed"])
        self.assertEqual(messages[0]["type"], "text")
        self.assertEqual(
            sidecar.normalized_send_confirmation_text(messages[0]["content"]),
            "10万左右二手车推荐，点击图片查看车型和价格",
        )
        self.assertIsNone(
            sidecar.find_new_matching_self_message(
                [],
                [
                    {
                        "sequence_index": 0,
                        "observation_id": "real-self-image-with-marketing-copy",
                        "row_kind": "text_bubble",
                        "sender_role": "self",
                        "content_normalized": sidecar.normalized_send_confirmation_text(
                            messages[0]["content"]
                        ),
                    }
                ],
                (
                    "可以，10万左右可以按你的需求帮你筛选合适的二手车。"
                    "你主要家用、通勤还是跑长途？"
                ),
            )
        )

    def test_incident_long_text_recovery_completes_sent_confirmation_chain(self):
        frame = Image.new("RGB", (980, 860), "white")
        geometry = {
            "left": 0,
            "top": 0,
            "right": 980,
            "bottom": 860,
            "width": 980,
            "height": 860,
        }
        validation = {
            "ok": True,
            "online": True,
            "reason": "target_confirmed",
            "confirmation_confidence": "active_title_strict",
            "geometry": geometry,
        }
        structural_image = {
            "id": "incident-self-structural-candidate",
            "message_id": "incident-self-structural-candidate",
            "type": "image",
            "message_type": "image",
            "visual_side": "self",
            "sender_role": "self",
            "bubble_rect": [489, 532, 878, 653],
            "avatar_alignment": {"role": "self", "confirmed": True},
        }
        customer_message = {
            "id": "customer-question",
            "message_id": "customer-question",
            "observation_id": "customer-question",
            "type": "text",
            "message_type": "text",
            "sender": "customer",
            "sender_role": "customer",
            "content": "你好我想问10万左右的二手车有推荐的么",
            "bubble_rect": [487, 431, 803, 451],
        }
        enhanced_items = incident_post_send_enhanced_ocr_items()
        enhanced_items[0]["text"] = "可以，10万左右可以按需求帮你筛选合适"
        baseline_sequence = [
            {
                "sequence_index": 0,
                "observation_id": "customer-question",
                "row_kind": "text_bubble",
                "sender_role": "customer",
                "content_normalized": "你好我想问10万左右的二手车有推荐的么",
            }
        ]
        with (
            patch.object(sidecar, "get_window_geometry", return_value=geometry),
            patch.object(sidecar, "validate_active_send_target", return_value=validation),
            patch.object(sidecar, "active_send_guard_is_strong", return_value=True),
            patch.object(
                sidecar,
                "parse_current_chat_frame_messages",
                return_value=[customer_message, structural_image],
            ),
            patch.object(
                sidecar,
                "enhanced_ocr_items_for_structural_chat_candidate",
                return_value=enhanced_items,
            ),
            patch.object(
                sidecar,
                "input_text_region_state",
                return_value={"has_visible_text": False},
            ),
        ):
            snapshot = sidecar.build_send_fact_snapshot_from_frame(
                1,
                target="CJMU5YT9",
                text=INCIDENT_LONG_REPLY,
                exact=False,
                artifact_dir=None,
                label="incident_send_result_confirm",
                screenshot=frame,
                screenshot_path="incident-send-result.png",
                ocr_items=[],
                recover_expected_self_text=True,
            )

        with patch.object(
            sidecar,
            "capture_send_fact_snapshot",
            side_effect=AssertionError("initial snapshot should settle the send"),
        ):
            result = sidecar.confirm_reply_sent(
                1,
                target="CJMU5YT9",
                text=INCIDENT_LONG_REPLY,
                exact=False,
                baseline_match_count=0,
                baseline_message_sequence=baseline_sequence,
                artifact_dir=None,
                max_attempts=1,
                initial_snapshot=snapshot,
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["attempt"], 1)
        self.assertEqual(
            result["confirmed_message"]["recovered_from_structural_observation_id"],
            "incident-self-structural-candidate",
        )

    def test_post_send_enhanced_ocr_retypes_readable_self_only(self):
        frame = Image.new("RGB", (980, 860), "white")
        candidates = [
            {
                "id": "self-real-image",
                "type": "image",
                "sender_role": "self",
                "bubble_rect": [520, 420, 850, 620],
                "avatar_alignment": {"role": "self"},
            },
            {
                "id": "customer-image",
                "type": "image",
                "sender_role": "customer",
                "bubble_rect": [400, 640, 710, 790],
                "avatar_alignment": {"role": "customer"},
            },
        ]
        calls = []

        def enhanced_ocr(_image):
            calls.append(True)
            return [
                {
                    "text": "图片里的其他文字",
                    "left": 10,
                    "top": 10,
                    "right": 180,
                    "bottom": 40,
                    "confidence": 0.99,
                }
            ]

        messages, diagnostics = (
            sidecar.recover_expected_self_text_from_structural_candidates(
                frame,
                candidates,
                target="CJTEST01",
                expected_text="这是程序本次真正发送的回复",
                ocr_runner=enhanced_ocr,
            )
        )

        self.assertEqual(calls, [True])
        self.assertFalse(diagnostics["recovered"])
        self.assertTrue(diagnostics["reclassified_as_text"])
        self.assertEqual([item["type"] for item in messages], ["text", "image"])

    def test_post_send_enhanced_ocr_without_readable_text_stays_image(self):
        frame = Image.new("RGB", (980, 860), "white")
        structural_image = {
            "id": "self-image-no-readable-text",
            "type": "image",
            "sender_role": "self",
            "visual_side": "self",
            "bubble_rect": [520, 420, 850, 620],
            "avatar_alignment": {"role": "self"},
        }

        messages, diagnostics = (
            sidecar.recover_expected_self_text_from_structural_candidates(
                frame,
                [structural_image],
                target="CJTEST01",
                expected_text="AI回复",
                ocr_runner=lambda _image: [
                    {
                        "text": "……",
                        "left": 10,
                        "top": 10,
                        "right": 50,
                        "bottom": 40,
                        "confidence": 0.99,
                    }
                ],
            )
        )

        self.assertFalse(diagnostics["recovered"])
        self.assertFalse(diagnostics.get("reclassified_as_text", False))
        self.assertEqual(diagnostics["reason"], "enhanced_ocr_has_no_readable_text")
        self.assertEqual(messages[0]["type"], "image")

    def test_authorized_reread_only_retypes_the_matching_confirmed_ai_reply(self):
        frame = Image.new("RGB", (980, 860), "white")
        structural_image = {
            "id": "incident-long-self-bubble",
            "type": "image",
            "message_type": "image",
            "sender_role": "self",
            "visual_side": "self",
            "bubble_rect": [489, 532, 878, 653],
            "avatar_alignment": {"role": "self", "confirmed": True},
        }
        expected = (
            "你好，10万左右可以先按你的用车需求筛选合适车型。"
            "你主要是日常通勤、家庭出行，还是更看重大空间？"
            "另外你更偏轿车、SUV，还是燃油、混动、纯电呢？"
        )
        current_items = [
            {
                "text": line,
                "left": 16,
                "top": 12 + index * 24,
                "right": 370,
                "bottom": 32 + index * 24,
                "center_y": 22 + index * 24,
                "confidence": 0.99,
            }
            for index, line in enumerate(
                [
                    "你好，10万左右可以先按你的用车需求筛选合适车型。",
                    "你主要是日常通勤、家庭出行，还是更看重大空间?",
                    "另外你更偏轿车、SUV，还是燃油、混动、纯电呢?",
                ]
            )
        ]

        recovered, diagnostics = (
            sidecar.recover_expected_self_text_from_structural_candidates(
                frame,
                [structural_image],
                target="CJNCXB8R",
                expected_text=expected,
                ocr_runner=lambda _image: current_items,
                require_correspondence=True,
            )
        )
        unrelated, unrelated_diagnostics = (
            sidecar.recover_expected_self_text_from_structural_candidates(
                frame,
                [structural_image],
                target="CJNCXB8R",
                expected_text=expected,
                ocr_runner=lambda _image: [
                    {
                        "text": "图片里的其他文字",
                        "left": 10,
                        "top": 10,
                        "right": 180,
                        "bottom": 40,
                        "confidence": 0.99,
                    }
                ],
                require_correspondence=True,
            )
        )

        self.assertTrue(diagnostics["recovered"])
        self.assertEqual(recovered[0]["type"], "text")
        self.assertFalse(unrelated_diagnostics["recovered"])
        self.assertEqual(
            unrelated_diagnostics["reason"],
            "current_text_does_not_match_confirmed_reply",
        )
        self.assertEqual(unrelated[0]["type"], "image")

    def test_messages_payload_applies_confirmed_reply_recovery_before_observations(self):
        frame = Image.new("RGB", (980, 860), "white")
        structural_image = {
            "id": "incident-structural-self-image",
            "type": "image",
            "message_type": "image",
            "sender_role": "self",
            "visual_side": "self",
            "bubble_rect": [489, 532, 878, 653],
            "avatar_alignment": {"role": "self", "confirmed": True},
        }
        expected = "你好，10万左右可以先按你的用车需求筛选合适车型。"
        enhanced_items = [
            {
                "text": expected,
                "left": 12,
                "top": 12,
                "right": 370,
                "bottom": 42,
                "center_y": 27,
                "confidence": 0.99,
            }
        ]
        seed = {
            "label": "incident-current-frame",
            "screenshot_path": "incident-current-frame.png",
            "screenshot": frame,
            "ocr_items": [],
            "messages": [],
        }
        geometry = {
            "left": 0,
            "top": 0,
            "right": 980,
            "bottom": 860,
            "width": 980,
            "height": 860,
        }

        with (
            patch.object(sidecar, "get_window_geometry", return_value=geometry),
            patch.object(sidecar, "quick_login_like", return_value=False),
            patch.object(sidecar, "blocking_screen_reason", return_value=""),
            patch.object(
                sidecar,
                "merge_structural_image_messages",
                return_value=[structural_image],
            ),
            patch.object(
                sidecar,
                "enhanced_ocr_items_for_structural_chat_candidate",
                return_value=enhanced_items,
            ),
            patch.object(
                sidecar,
                "visible_untranscribed_voice_hint",
                return_value={"detected": False},
            ),
        ):
            payload = sidecar.messages_payload(
                1,
                {},
                target="CJNCXB8R",
                history_load_times=0,
                expected_confirmed_self_text=expected,
                seed_snapshot=seed,
            )
            with patch.dict(
                os.environ,
                {"CHEJIN_C3_PRE_SEND_ROI_REUSE_ENABLED": "0"},
            ):
                fallback_payload = sidecar.messages_payload(
                    1,
                    {},
                    target="CJNCXB8R",
                    history_load_times=0,
                    expected_confirmed_self_text=expected,
                    seed_snapshot=seed,
                )

        self.assertTrue(payload["ok"])
        self.assertTrue(payload["confirmed_self_text_recovery"]["recovered"])
        self.assertEqual(
            [item["row_kind"] for item in payload["observations"]],
            ["text_bubble"],
        )
        self.assertEqual(payload["observations"][0]["sender_role"], "self")
        self.assertTrue(payload["pre_send_frame_reuse"]["fast_path_used"])
        self.assertEqual(
            payload["pre_send_frame_reuse"]["shared_consumers"],
            [
                "target_confirmation",
                "message_viewport",
                "message_sequence",
                "input_region",
            ],
        )
        self.assertIn("frame_id", payload["frame_observation"])
        self.assertNotIn("pre_send_frame_reuse", fallback_payload)
        self.assertNotIn("frame_observation", fallback_payload)

    def test_daemon_preserves_confirmed_reply_text_for_all_c2_read_actions(self):
        expected = "已确认发送的 AI 回复"

        for action in ("open-chat", "messages", "voice-transcribe"):
            with self.subTest(action=action):
                argv = sidecar.args_for_daemon_request(
                    {
                        "action": action,
                        "target": "CJNCXB8R",
                        "expected_confirmed_self_text": expected,
                    }
                )
                flag_index = argv.index("--expected-confirmed-self-text")
                self.assertEqual(argv[flag_index + 1], expected)

    def test_voice_prepare_reuses_confirmed_reply_text_recovery(self):
        frame = Image.new("RGB", (980, 860), "white")
        expected = "已确认发送的 AI 回复"
        structural_image = {
            "id": "voice-frame-self-text",
            "type": "image",
            "message_type": "image",
            "sender_role": "self",
            "visual_side": "self",
            "bubble_rect": [489, 532, 878, 653],
            "avatar_alignment": {"role": "self", "confirmed": True},
        }
        enhanced_items = [
            {
                "text": expected,
                "left": 12,
                "top": 12,
                "right": 320,
                "bottom": 42,
                "confidence": 0.99,
            }
        ]
        with (
            patch.object(
                sidecar,
                "capture_wechat",
                return_value=(frame, "voice-prepare.png"),
            ),
            patch.object(sidecar, "run_ocr", return_value=[]),
            patch.object(
                sidecar,
                "parse_current_chat_frame_messages",
                return_value=[structural_image],
            ),
            patch.object(
                sidecar,
                "enhanced_ocr_items_for_structural_chat_candidate",
                return_value=enhanced_items,
            ),
            patch.object(
                sidecar,
                "build_unified_voice_observations_v3",
                return_value=[],
            ),
        ):
            payload = sidecar.prepare_voice_action_payload(
                1,
                {},
                target="CJNCXB8R",
                expected_confirmed_self_text=expected,
            )

        self.assertEqual(payload["state"], "voice_action_prepare_empty")
        self.assertEqual(
            [item["row_kind"] for item in payload["observations"]],
            ["text_bubble"],
        )

    def test_post_send_global_ocr_text_wins_without_redundant_local_ocr(self):
        frame = Image.new("RGB", (980, 860), "white")
        global_text = {
            "id": "global-self-text",
            "type": "text",
            "sender": "self",
            "sender_role": "self",
            "content": "AI回复",
            "bubble_rect": [700, 650, 860, 710],
        }

        def unexpected_local_ocr(_image):
            self.fail("全局 OCR 已确认全文时不应再运行局部 OCR")

        messages, diagnostics = (
            sidecar.recover_expected_self_text_from_structural_candidates(
                frame,
                [global_text],
                target="CJTEST01",
                expected_text="AI回复",
                ocr_runner=unexpected_local_ocr,
            )
        )

        self.assertFalse(diagnostics["attempted"])
        self.assertFalse(diagnostics["recovered"])
        self.assertEqual(diagnostics["reason"], "already_observed_as_self_text")
        self.assertEqual(messages, [global_text])

    def test_post_send_local_ocr_cannot_replace_self_candidate_without_avatar_confirmation(self):
        frame = Image.new("RGB", (980, 860), "white")
        structural_image = {
            "id": "visual-self-without-avatar",
            "type": "image",
            "visual_side": "self",
            "sender_role": "unknown",
            "bubble_rect": [600, 520, 870, 610],
            "avatar_alignment": {"role": "unknown"},
        }

        def unexpected_local_ocr(_image):
            self.fail("同排头像未确认 self 时不应运行类型恢复")

        messages, diagnostics = (
            sidecar.recover_expected_self_text_from_structural_candidates(
                frame,
                [structural_image],
                target="CJTEST01",
                expected_text="AI回复",
                ocr_runner=unexpected_local_ocr,
            )
        )

        self.assertFalse(diagnostics["attempted"])
        self.assertFalse(diagnostics["recovered"])
        self.assertEqual(messages[0]["type"], "image")

    def test_post_send_snapshot_wires_structural_candidate_recovery_into_sequence(self):
        frame = Image.new("RGB", (980, 860), "white")
        geometry = {
            "left": 0,
            "top": 0,
            "right": 980,
            "bottom": 860,
            "width": 980,
            "height": 860,
        }
        validation = {
            "ok": True,
            "online": True,
            "reason": "target_confirmed",
            "confirmation_confidence": "active_title_strict",
            "geometry": geometry,
        }
        structural_image = {
            "id": "visual-self-candidate",
            "message_id": "visual-self-candidate",
            "type": "image",
            "message_type": "image",
            "visual_side": "self",
            "sender_role": "self",
            "bubble_rect": [489, 505, 878, 613],
            "avatar_alignment": {"role": "self", "confirmed": True},
        }
        enhanced_items = [
            {
                "text": "AI回复",
                "left": 520,
                "top": 530,
                "right": 650,
                "bottom": 560,
                "center_x": 585,
                "center_y": 545,
                "confidence": 0.99,
            }
        ]
        with (
            patch.object(sidecar, "get_window_geometry", return_value=geometry),
            patch.object(sidecar, "validate_active_send_target", return_value=validation),
            patch.object(sidecar, "active_send_guard_is_strong", return_value=True),
            patch.object(
                sidecar,
                "parse_current_chat_frame_messages",
                return_value=[structural_image],
            ),
            patch.object(
                sidecar,
                "enhanced_ocr_items_for_structural_chat_candidate",
                return_value=enhanced_items,
            ),
            patch.object(
                sidecar,
                "input_text_region_state",
                return_value={"has_visible_text": False},
            ),
        ):
            snapshot = sidecar.build_send_fact_snapshot_from_frame(
                1,
                target="CJTEST01",
                text="AI回复",
                exact=False,
                artifact_dir=None,
                label="send_result_confirm_test",
                screenshot=frame,
                screenshot_path="send-result.png",
                ocr_items=[],
                recover_expected_self_text=True,
            )

        self.assertTrue(snapshot["ok"])
        self.assertTrue(snapshot["enhanced_text_recovery"]["recovered"])
        self.assertEqual(snapshot["matching_self_message_count"], 1)
        self.assertEqual(snapshot["message_sequence"][0]["row_kind"], "text_bubble")
        self.assertEqual(snapshot["message_sequence"][0]["content_normalized"], "ai回复")

    def test_sent_confirmation_requests_enhanced_recovery_only_for_post_send_snapshot(self):
        snapshot = {
            "ok": True,
            "matching_self_message_count": 1,
            "input_region": {"has_visible_text": False},
            "message_sequence": [
                {
                    "sequence_index": 0,
                    "observation_id": "self-new",
                    "row_kind": "text_bubble",
                    "sender_role": "self",
                    "content_normalized": "AI回复",
                }
            ],
            "observations": [
                {
                    "observation_id": "self-new",
                    "row_kind": "text_bubble",
                    "sender_role": "self",
                    "content_clean": "AI回复",
                }
            ],
        }
        with patch.object(
            sidecar,
            "capture_send_fact_snapshot",
            return_value=snapshot,
        ) as capture:
            result = sidecar.confirm_reply_sent(
                1,
                target="CJTEST01",
                text="AI回复",
                exact=False,
                baseline_match_count=0,
                baseline_message_sequence=[],
                artifact_dir=None,
                max_attempts=1,
            )

        self.assertTrue(result["ok"])
        self.assertTrue(capture.call_args.kwargs["recover_expected_self_text"])

    def test_enhanced_recovery_cannot_turn_an_old_image_candidate_into_new_send_fact(self):
        baseline = [
            {
                "sequence_index": 0,
                "observation_id": "visual-self-old",
                "row_kind": "image_bubble",
                "sender_role": "self",
                "content_normalized": "[图片]",
            }
        ]
        current = [
            {
                "sequence_index": 0,
                "observation_id": "enhanced-text-old",
                "row_kind": "text_bubble",
                "sender_role": "self",
                "content_normalized": "AI回复",
                "recovered_from_structural_observation_id": "visual-self-old",
            }
        ]

        self.assertIsNone(
            sidecar.find_new_matching_self_message(
                baseline,
                current,
                "AI回复",
            )
        )

    def test_sent_confirmation_returns_unknown_when_only_window_is_readable(self):
        with (
            patch.object(
                sidecar,
                "capture_send_fact_snapshot",
                return_value={
                    "ok": True,
                    "matching_self_message_count": 2,
                    "input_region": {"has_visible_text": False},
                    "message_sequence": [
                        {
                            "sequence_index": 0,
                            "observation_id": "self-old",
                            "row_kind": "text_bubble",
                            "sender_role": "self",
                            "content_normalized": "AI回复",
                        }
                    ],
                    "observations": [],
                },
            ),
            patch.object(sidecar.time, "sleep"),
        ):
            result = sidecar.confirm_reply_sent(
                1,
                target="CJTEST01",
                text="AI回复",
                exact=False,
                baseline_match_count=1,
                baseline_message_sequence=[
                    {
                        "sequence_index": 0,
                        "observation_id": "self-old",
                        "row_kind": "text_bubble",
                        "sender_role": "self",
                        "content_normalized": "AI回复",
                    }
                ],
                artifact_dir=None,
                max_attempts=2,
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "SEND_RESULT_UNKNOWN")

    def test_blank_window_after_physical_send_is_always_unknown(self):
        geometry = {"width": 960, "height": 820}
        strict_guard = {
            "ok": True,
            "online": True,
            "geometry": geometry,
            "title": "CJTEST01",
        }
        baseline = {
            "ok": True,
            "validation": strict_guard,
            "input_region": {"has_visible_text": False},
            "send_context_guard": {"schema_version": 1, "sequence": []},
            "matching_self_message_count": 0,
            "message_sequence": [],
        }
        with (
            patch.object(
                sidecar,
                "recover_send_window_guard",
                return_value={"ok": True},
            ),
            patch.object(
                sidecar,
                "validate_active_send_target",
                return_value=strict_guard,
            ),
            patch.object(sidecar, "active_send_guard_is_strong", return_value=True),
            patch.object(sidecar, "get_window_geometry", return_value=geometry),
            patch.object(
                sidecar,
                "validate_send_geometry",
                return_value={"ok": True},
            ),
            patch.object(
                sidecar,
                "consume_input_region_precheck_ocr_seed",
                return_value=None,
            ),
            patch.object(
                sidecar,
                "capture_send_fact_snapshot",
                return_value=baseline,
            ),
            patch.object(
                sidecar,
                "validate_send_context_guard",
                return_value={"ok": True},
            ),
            patch.object(
                sidecar,
                "send_with_visual_input",
                return_value={
                    "ok": True,
                    "physical_send_triggered": True,
                },
            ),
            patch.object(
                sidecar,
                "validate_post_send_target",
                return_value={"ok": False, "reason": "blank_render"},
            ),
            patch.object(sidecar, "humanized_action_sleep"),
        ):
            result = sidecar.send_payload(
                1,
                {"ok": True},
                target="CJTEST01",
                text="AI回复",
                exact=False,
                skip_send_rate_guard=True,
                expected_context_guard={"schema_version": 1, "sequence": []},
                validated_guard=strict_guard,
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "SEND_RESULT_UNKNOWN")
        self.assertTrue(result["physical_send_triggered"])
        self.assertTrue(result["send_result"]["physical_send_triggered"])

    def test_uia_screen_coordinates_use_screen_to_client(self):
        snapshot = sidecar.win32_ocr_layout.build_layout_snapshot(
            hwnd=1001,
            frame_id="screen-to-client-frame",
            capture_mode=sidecar.win32_ocr_layout.CAPTURE_MODE_WINDOW_VISIBLE_SCREEN,
            image_size=(980, 860),
            capture_screen_origin=[108, 132],
            window_rect=[100, 100, 1080, 960],
            client_rect=[0, 0, 972, 828],
            client_screen_origin=[108, 132],
            dpi_scale=1.0,
            regions={},
            anchors=[],
            confidence=1.0,
            conflicts=[],
            executable=False,
            required_region_names=(),
        )
        self.assertEqual(
            sidecar.win32_ocr_layout.screen_point_to_client(
                snapshot,
                [910, 742],
            ),
            [802, 610],
        )

    def test_formal_send_uses_visual_input_then_enter_without_uia(self):
        geometry = {
            "left": 0,
            "top": 0,
            "right": 980,
            "bottom": 860,
            "width": 980,
            "height": 860,
        }
        strict_guard = {
            "ok": True,
            "online": True,
            "reason": "target_confirmed",
            "confirmation_confidence": "active_title_strict",
            "geometry": geometry,
        }
        baseline = {
            "ok": True,
            "input_region": {
                "has_visible_text": False,
                "bounds": [400, 680, 880, 800],
            },
            "matching_self_message_count": 0,
            "message_sequence": [],
            "send_context_guard": {
                "schema_version": 1,
                "sequence": [],
                "message_count": 0,
                "bottom": None,
            },
        }
        visual_calls: list[str] = []
        with (
            patch.object(sidecar, "recover_send_window_guard", return_value={"ok": True}),
            patch.object(sidecar, "validate_active_send_target", return_value=strict_guard),
            patch.object(sidecar, "active_send_guard_is_strong", return_value=True),
            patch.object(sidecar, "get_window_geometry", return_value=geometry),
            patch.object(sidecar, "validate_send_geometry", return_value={"ok": True}),
            patch.object(sidecar, "consume_input_region_precheck_ocr_seed", return_value=None),
            patch.object(sidecar, "capture_send_fact_snapshot", return_value=baseline),
            patch.object(sidecar, "validate_send_context_guard", return_value={"ok": True}),
            patch.object(
                sidecar,
                "send_with_visual_input",
                side_effect=lambda *_args, **_kwargs: (
                    visual_calls.append("visual")
                    or {
                        "ok": True,
                        "method": "visual_input.sendinput_unicode+keyboard_enter",
                        "physical_send_triggered": True,
                    }
                ),
            ),
            patch.object(sidecar, "validate_post_send_target", return_value={"ok": True}),
            patch.object(
                sidecar,
                "confirm_reply_sent",
                return_value={"ok": True, "reason": "new_self_bubble"},
            ),
            patch.object(sidecar, "humanized_action_sleep"),
        ):
            result = sidecar.send_payload(
                1,
                {"ok": True},
                target="CJTEST01",
                text="AI回复",
                exact=False,
                skip_send_rate_guard=True,
                expected_context_guard=baseline["send_context_guard"],
                validated_guard=strict_guard,
            )

        self.assertTrue(result["ok"])
        self.assertEqual(visual_calls, ["visual"])
        self.assertFalse(hasattr(sidecar, "send_with_uia_controls"))
        self.assertEqual(result["send_result"]["mode"], "visual_only")
        self.assertIn("keyboard_enter", result["send_result"]["method"])

    def test_focused_blank_visual_input_reaches_one_enter_with_journal(self):
        geometry = {
            "left": 0,
            "top": 0,
            "right": 981,
            "bottom": 860,
            "width": 981,
            "height": 860,
        }
        frame = Image.new("RGB", (981, 860), "white")
        draw = ImageDraw.Draw(frame)
        draw.rectangle((408, 709, 409, 731), fill="black")
        draw.rectangle((394, 798, 520, 801), fill="black")
        input_region = sidecar.input_text_region_state(
            frame,
            [],
            geometry=geometry,
        )
        self.assertFalse(input_region["has_visible_text"])
        self.assertTrue(input_region["caret_like_dark_pixels"])

        context_guard = {
            "schema_version": 1,
            "sequence": [],
            "message_count": 0,
            "bottom": None,
        }
        strict_guard = {
            "ok": True,
            "online": True,
            "reason": "target_confirmed",
            "confirmation_confidence": "active_title_strict",
            "geometry": geometry,
        }
        baseline = {
            "ok": True,
            "validation": strict_guard,
            "input_region": input_region,
            "matching_self_message_count": 0,
            "message_sequence": [],
            "send_context_guard": context_guard,
        }
        enter_events: list[str] = []
        with tempfile.TemporaryDirectory() as tmp:
            journal_path = Path(tmp) / "send-action.json"
            journal_path.write_text(
                json.dumps(
                    {
                        "action_kind": "send",
                        "action_phase": "not_attempted",
                        "items": {
                            "reply-1": {
                                "source_message_key": "reply-1",
                                "action_phase": "not_attempted",
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )

            def record_enter(_key):
                journal = json.loads(journal_path.read_text(encoding="utf-8"))
                self.assertEqual(journal["action_phase"], "trigger_attempted")
                enter_events.append("enter")

            with (
                patch.object(sidecar, "recover_send_window_guard", return_value={"ok": True}),
                patch.object(sidecar, "validate_active_send_target", return_value=strict_guard),
                patch.object(sidecar, "active_send_guard_is_strong", return_value=True),
                patch.object(sidecar, "get_window_geometry", return_value=geometry),
                patch.object(sidecar, "validate_send_geometry", return_value={"ok": True}),
                patch.object(sidecar, "consume_input_region_precheck_ocr_seed", return_value=None),
                patch.object(sidecar, "capture_send_fact_snapshot", return_value=baseline),
                patch.object(
                    sidecar,
                    "paste_text_with_confirmation",
                    return_value={
                        "ok": True,
                        "input_mode": "sendinput_unicode",
                        "input_result": {"ok": True, "typed_chars": 4},
                        "send_button_ready": {
                            "ok": False,
                            "reason": "active_green_send_button_not_observed",
                            "used_as_click_target": False,
                        },
                        "_post_input_screenshot": frame,
                    },
                ),
                patch.object(
                    sidecar,
                    "build_send_fact_snapshot_from_frame",
                    return_value=baseline,
                ),
                patch.object(sidecar, "validate_send_context_guard", return_value={"ok": True}),
                patch.object(
                    sidecar,
                    "confirm_exact_program_draft_focus",
                    return_value={"ok": True, "reason": "exact_program_draft_confirmed"},
                ),
                patch.object(sidecar, "key_press", side_effect=record_enter),
                patch.object(sidecar, "validate_post_send_target", return_value={"ok": True}),
                patch.object(
                    sidecar,
                    "confirm_reply_sent",
                    return_value={"ok": True, "reason": "new_self_bubble"},
                ),
                patch.object(sidecar, "humanized_action_sleep"),
            ):
                result = sidecar.send_payload(
                    1,
                    {"ok": True},
                    target="CJUAT728",
                    text="AI回复",
                    exact=False,
                    skip_send_rate_guard=True,
                    expected_context_guard=context_guard,
                    validated_guard=strict_guard,
                    action_journal_path=str(journal_path),
                )

        self.assertTrue(result["ok"])
        self.assertEqual(result["send_result"]["mode"], "visual_only")
        self.assertEqual(enter_events, ["enter"])

    def test_formal_send_failure_is_reported_from_the_single_visual_path(self):
        geometry = {
            "left": 0,
            "top": 0,
            "right": 980,
            "bottom": 860,
            "width": 980,
            "height": 860,
        }
        strict_guard = {
            "ok": True,
            "online": True,
            "reason": "target_confirmed",
            "confirmation_confidence": "active_title_strict",
            "geometry": geometry,
        }
        baseline = {
            "ok": True,
            "input_region": {"has_visible_text": False},
            "matching_self_message_count": 0,
            "message_sequence": [],
            "send_context_guard": {
                "schema_version": 1,
                "sequence": [],
                "message_count": 0,
                "bottom": None,
            },
        }
        with (
            patch.object(sidecar, "recover_send_window_guard", return_value={"ok": True}),
            patch.object(sidecar, "validate_active_send_target", return_value=strict_guard),
            patch.object(sidecar, "active_send_guard_is_strong", return_value=True),
            patch.object(sidecar, "get_window_geometry", return_value=geometry),
            patch.object(sidecar, "validate_send_geometry", return_value={"ok": True}),
            patch.object(sidecar, "consume_input_region_precheck_ocr_seed", return_value=None),
            patch.object(sidecar, "capture_send_fact_snapshot", return_value=baseline),
            patch.object(sidecar, "validate_send_context_guard", return_value={"ok": True}),
            patch.object(
                sidecar,
                "send_with_visual_input",
                return_value={
                    "ok": False,
                    "reason": "visual_input_not_confirmed",
                    "error_code": "SEND_INPUT_NOT_READY",
                    "physical_send_triggered": False,
                },
            ),
        ):
            result = sidecar.send_payload(
                1,
                {"ok": True},
                target="CJTEST01",
                text="AI回复",
                exact=False,
                skip_send_rate_guard=True,
                expected_context_guard=baseline["send_context_guard"],
                validated_guard=strict_guard,
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "SEND_INPUT_NOT_READY")
        self.assertFalse(result["physical_send_triggered"])
        self.assertFalse(hasattr(sidecar, "send_with_uia_controls"))

    def test_formal_send_payload_has_one_visual_locator(self):
        source = inspect.getsource(sidecar.send_payload)

        self.assertIn("send_with_visual_input(", source)
        self.assertNotIn("send_with_uia_controls(", source)
        self.assertIn("validate_send_context_guard(", source)
        self.assertIn("pre_trigger_context_check", source)
        self.assertNotIn("calculate_send_points(", source)

    def test_send_action_current_only_gate_precedes_all_send_work(self):
        source = Path(sidecar.__file__).read_text(encoding="utf-8")
        start = source.index('    if action == "send":')
        end = source.index("def use_passive_probe_mode(", start)
        send_action = source[start:end]

        gate = send_action.index("SEND_CURRENT_CHAT_ONLY_REQUIRED")
        payload_call = send_action.index("send_payload(")
        self.assertLess(gate, payload_call)
        self.assertNotIn("open_chat(", send_action)


if __name__ == "__main__":
    unittest.main()
