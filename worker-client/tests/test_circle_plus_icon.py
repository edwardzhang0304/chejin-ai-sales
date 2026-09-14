from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest

from PIL import Image, ImageDraw, ImageEnhance

OMNIAUTO_ROOT = Path(__file__).resolve().parents[1] / "omniauto-rpa"
if str(OMNIAUTO_ROOT) not in sys.path:
    sys.path.insert(0, str(OMNIAUTO_ROOT))

from apps.wechat_ai_customer_service.adapters.circle_plus_icon import circle_plus_icon_candidates
from apps.wechat_ai_customer_service.adapters import wechat_win32_ocr_sidecar as sidecar
from apps.wechat_ai_customer_service.adapters.wechat_win32_ocr import add_friend_windows, window_layout
from apps.wechat_ai_customer_service.tests.test_add_friend_flow_runtime import (
    PhysicalClickReached,
    _search_item,
    production_boundary,
)
from apps.wechat_ai_customer_service.tests.run_wechat_win32_ocr_layout_snapshot_checks import (
    _bright_wechat_add_friend_frame,
)


class CirclePlusIconTest(unittest.TestCase):
    def setUp(self) -> None:
        with Image.open(Path(__file__).parent / "fixtures/circle_plus/sidebar_header.png") as image:
            self.header = image.convert("RGB")

    def candidates(self, image=None, bounds=None):
        image = self.header if image is None else image
        return circle_plus_icon_candidates(image, search_bounds=bounds or [0, 0, *image.size])

    def test_real_gray_strokes_are_recognized_without_search_icon_false_positive(self):
        candidates = self.candidates()
        self.assertEqual(len(candidates), 1, candidates)
        self.assertEqual(candidates[0]["point"], [214, 57])
        self.assertEqual(candidates[0]["method"], "circle_plus_template")

    def test_translated_region_returns_image_coordinates(self):
        image = Image.new("RGB", (600, 300), "white")
        image.paste(self.header, (173, 104))
        candidates = self.candidates(image, [173, 104, 413, 197])
        self.assertEqual([c["point"] for c in candidates], [[387, 161]])

    def test_old_narrow_region_is_not_expanded(self):
        self.assertEqual(self.candidates(bounds=[0, 0, 199, 93]), [])

    def test_visible_search_control_without_plus_does_not_match(self):
        self.assertEqual(self.candidates(self.header.crop((0, 0, 190, 93))), [])
        self.assertEqual(self.candidates(Image.new("RGB", (240, 93), "white")), [])
        self.assertEqual(circle_plus_icon_candidates(None, search_bounds=[0, 0, 240, 93]), [])

    def test_brightness_and_supported_scale_changes(self):
        for scale in (0.75, 1.0, 1.25, 1.5, 2.0):
            with self.subTest(scale=scale):
                image = self.header.resize((round(240 * scale), round(93 * scale)), Image.Resampling.BILINEAR)
                image = ImageEnhance.Brightness(image).enhance(0.75)
                candidates = self.candidates(image)
                self.assertEqual(len(candidates), 1, candidates)
                self.assertLessEqual(abs(candidates[0]["point"][0] - 214 * scale), 2)
                self.assertLessEqual(abs(candidates[0]["point"][1] - 57 * scale), 2)

    def test_multiple_icons_remain_multiple_candidates(self):
        image = Image.new("RGB", (520, 93), "white")
        image.paste(self.header, (0, 0))
        image.paste(self.header, (260, 0))
        self.assertEqual(len(self.candidates(image)), 2)

    def test_public_click_route_uses_match_or_unchanged_region_estimate(self):
        for has_icon in (True, False):
            with self.subTest(has_icon=has_icon):
                image, search = _bright_wechat_add_friend_frame(selected_row=2)
                search = _search_item(search, "Q搜索")
                # Replace the old synthetic icon with a held-out native icon,
                # deliberately away from the estimated X to detect overwrites.
                ImageDraw.Draw(image).rectangle((322, 38, 365, 79), fill=(234, 234, 234))
                if has_icon:
                    icon = self.header.crop((204, 47, 225, 68))
                    image.paste(icon, (285, 48))
                clicks = []
                with tempfile.TemporaryDirectory() as directory, production_boundary(
                    image, ocr_items=[search], click_points=clicks,
                ):
                    with self.assertRaises(PhysicalClickReached):
                        sidecar.add_friend_entry_click_plan_payload(
                            1001, {"visible_main_windows": [{"hwnd": 1001}]},
                            phone="13000000000", verify_message="测试", remark_name="测试-CJTEST01",
                            remark_code="CJTEST01", artifact_dir=directory,
                        )
                    self.assertEqual(len(clicks), 1, clicks)
                    snapshot = sidecar.layout_snapshot_for_image(image)
                    target = add_friend_windows.add_friend_plus_entry_target(
                        {}, image.size, screenshot=image, layout_snapshot=snapshot,
                    )
                    if has_icon:
                        self.assertEqual(target["source"], "vision_plus_icon")
                        self.assertEqual(clicks[0]["point"], [295, 58])
                    else:
                        expected = window_layout.map_reference_region_point(snapshot, "plus_entry")["image_point"]
                        expected[1] = int((search["top"] + search["bottom"]) / 2)
                        self.assertEqual(target["source"], "startup_calibration_region_map")
                        self.assertEqual(clicks[0]["point"], expected)
                    self.assertEqual(target["point"], clicks[0]["point"])


if __name__ == "__main__":
    unittest.main()
