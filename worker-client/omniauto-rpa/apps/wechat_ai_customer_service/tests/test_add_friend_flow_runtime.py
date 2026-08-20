"""Production-entry regressions for dynamic add-friend layout and plus targeting."""

from __future__ import annotations

from contextlib import ExitStack, contextmanager
import tempfile
import unittest
from pathlib import Path
import sys
from typing import Any, Iterator
from unittest.mock import patch

from PIL import Image, ImageDraw


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from apps.wechat_ai_customer_service.adapters import add_friend_layout  # noqa: E402
from apps.wechat_ai_customer_service.adapters import wechat_win32_ocr_sidecar as sidecar  # noqa: E402
from apps.wechat_ai_customer_service.adapters.wechat_win32_ocr import window_layout  # noqa: E402
from apps.wechat_ai_customer_service.adapters.add_friend_result_mapping import (  # noqa: E402
    ERROR_PLUS_ENTRY_NOT_FOUND,
)
from apps.wechat_ai_customer_service.tests.run_wechat_win32_ocr_layout_snapshot_checks import (  # noqa: E402
    _bright_wechat_add_friend_frame,
)


class PhysicalClickReached(RuntimeError):
    """Test sentinel raised only at the mocked physical mouse boundary."""


def _geometry(image: Image.Image) -> dict[str, int]:
    width, height = image.size
    return {
        "left": 0,
        "top": 0,
        "right": width,
        "bottom": height,
        "width": width,
        "height": height,
    }


def _client_geometry(image: Image.Image) -> dict[str, int]:
    geometry = _geometry(image)
    return {**geometry, "screen_left": 0, "screen_top": 0}


def _search_item(item: dict[str, Any], text: str) -> dict[str, Any]:
    result = {**item, "text": text}
    result["center_x"] = (float(result["left"]) + float(result["right"])) / 2.0
    result["center_y"] = (float(result["top"]) + float(result["bottom"])) / 2.0
    return result


@contextmanager
def production_boundary(
    image: Image.Image,
    *,
    ocr_items: list[dict[str, Any]],
    click_points: list[dict[str, Any]],
    initial_snapshots: list[dict[str, Any]] | None = None,
) -> Iterator[None]:
    """Mock Windows capture, OCR engine and physical mouse only.

    Layout construction, snapshot finalization, readiness, plus vision and the
    public ``add_friend_entry_click_plan_payload`` route all remain production
    implementations.
    """

    geometry = _geometry(image)

    def capture(
        hwnd: int,
        *,
        artifact_dir: str,
        label: str,
        popup_window: bool = False,
    ) -> tuple[Image.Image, str]:
        screenshot_path = str(Path(artifact_dir) / f"{label}.png")
        sidecar._register_layout_snapshot(
            hwnd,
            image,
            capture_mode=sidecar.win32_ocr_layout.CAPTURE_MODE_WINDOW_VISIBLE_SCREEN,
            screenshot_path=screenshot_path,
            capture_screen_origin=[0, 0],
            generic_popup=popup_window,
        )
        if initial_snapshots is not None:
            initial_snapshots.append(
                dict(sidecar.layout_snapshot_for_image(image) or {})
            )
        return image, screenshot_path

    def ocr_engine(crop: Image.Image, *_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
        return [dict(item) for item in ocr_items] if crop.size == image.size else []

    def physical_click(
        _hwnd: int,
        x: int,
        y: int,
        *,
        bounds: list[int],
        action_name: str = "",
        expected_snapshot_id: str = "",
    ) -> dict[str, Any]:
        click_points.append(
            {
                "point": [x, y],
                "bounds": list(bounds),
                "action_name": action_name,
                "layout_snapshot_id": expected_snapshot_id,
            }
        )
        raise PhysicalClickReached(action_name)

    sidecar._LAYOUT_SNAPSHOT_STORE._items.clear()
    sidecar._LATEST_LAYOUT_SNAPSHOT_BY_HWND.clear()
    sidecar._LAYOUT_SNAPSHOT_ID_BY_IMAGE_ID.clear()
    with ExitStack() as stack:
        stack.enter_context(patch.object(sidecar, "get_window_geometry", return_value=geometry))
        stack.enter_context(
            patch.object(
                sidecar,
                "get_window_client_geometry",
                return_value=_client_geometry(image),
            )
        )
        stack.enter_context(patch.object(sidecar, "window_dpi_scale", return_value=1.0))
        stack.enter_context(
            patch.object(
                sidecar,
                "screen_work_area",
                return_value={**geometry, "metrics_available": True},
            )
        )
        stack.enter_context(
            patch.object(
                sidecar,
                "capture_wechat_window_visible_screen",
                side_effect=capture,
            )
        )
        stack.enter_context(patch.object(sidecar, "run_ocr_traced", side_effect=ocr_engine))
        stack.enter_context(
            patch.object(
                sidecar,
                "foreground_window_matches_target",
                return_value={"ok": True, "reason": "foreground_matches_target"},
            )
        )
        stack.enter_context(
            patch.object(sidecar, "human_window_image_hover", return_value={"ok": True})
        )
        stack.enter_context(
            patch.object(
                sidecar,
                "human_window_image_click_in_bounds",
                side_effect=physical_click,
            )
        )
        stack.enter_context(patch.object(sidecar, "add_friend_paced_pause", return_value=0.0))
        yield


class AddFriendProductionEntryTest(unittest.TestCase):
    def test_aligned_avatar_edges_do_not_replace_nav_on_production_entry(self) -> None:
        """Replay UAT-005 from public planning entry through the mouse boundary."""

        width, height = 980, 860
        nav_x, sidebar_x = 84, 382
        image = Image.new("RGB", (width, height), (250, 250, 250))
        draw = ImageDraw.Draw(image)
        draw.rectangle((0, 0, nav_x - 1, height - 1), fill=(246, 246, 246))
        draw.rectangle((nav_x, 0, sidebar_x - 1, height - 1), fill=(234, 234, 234))
        for sample_y in (86, 154, 292, 447, 602, 722):
            draw.rectangle((99, sample_y - 18, 143, sample_y + 18), fill=(90, 90, 90))
        plus_x, plus_y, radius = 349, 70, 11
        draw.ellipse(
            (plus_x - radius, plus_y - radius, plus_x + radius, plus_y + radius),
            outline=(70, 70, 70),
            width=2,
        )
        draw.line((plus_x - 6, plus_y, plus_x + 6, plus_y), fill=(60, 60, 60), width=2)
        draw.line((plus_x, plus_y - 6, plus_x, plus_y + 6), fill=(60, 60, 60), width=2)
        ocr_items = [
            {
                "text": "Q搜索",
                "left": 106,
                "top": 58,
                "right": 168,
                "bottom": 85,
                "center_x": 137,
                "center_y": 71.5,
                "confidence": 0.9555,
            },
            {
                "text": "文件传输助手",
                "left": 154,
                "top": 766,
                "right": 265,
                "bottom": 792,
                "center_x": 209.5,
                "center_y": 779,
                "confidence": 0.999,
            },
        ]
        clicks: list[dict[str, Any]] = []
        with tempfile.TemporaryDirectory(prefix="add-friend-uat-avatar-edge-") as temp_dir:
            with production_boundary(image, ocr_items=ocr_items, click_points=clicks):
                with self.assertRaises(PhysicalClickReached):
                    sidecar.add_friend_entry_click_plan_payload(
                        1001,
                        {"visible_main_windows": [{"hwnd": 1001}]},
                        phone="17368746889",
                        verify_message="我是车金二手车张伟",
                        remark_name="客户-CJ8K2P",
                        remark_code="CJ8K2P",
                        artifact_dir=temp_dir,
                    )

        snapshot = sidecar.current_layout_snapshot(1001) or {}
        self.assertEqual(snapshot.get("left_nav_bounds", [None, None, None])[2], nav_x, snapshot)
        self.assertEqual(snapshot.get("sidebar_bounds", [None, None, None])[2], sidebar_x, snapshot)
        self.assertEqual(len(clicks), 1, clicks)
        self.assertLessEqual(abs(clicks[0]["point"][0] - plus_x), 2, clicks)
        self.assertLessEqual(abs(clicks[0]["point"][1] - plus_y), 2, clicks)
        self.assertGreater(clicks[0]["point"][0], ocr_items[0]["right"], clicks)

    def test_no_header_line_search_noise_and_extra_verticals_reach_real_plus_click(self) -> None:
        for search_text in ("Q搜索", "O搜索", "0搜索"):
            with self.subTest(search_text=search_text):
                image, item = _bright_wechat_add_friend_frame(selected_row=3)
                draw = ImageDraw.Draw(image)
                # Extra chat-panel verticals must not replace the first dynamic
                # sidebar separator.
                draw.line((520, 0, 520, image.height - 1), fill=(50, 50, 50), width=1)
                draw.line((522, 0, 522, image.height - 1), fill=(245, 245, 245), width=1)
                clicks: list[dict[str, Any]] = []
                initial_snapshots: list[dict[str, Any]] = []
                final_snapshot: dict[str, Any] = {}
                search_ocr = _search_item(item, search_text)
                preview_top = int(search_ocr["bottom"]) + max(
                    40,
                    int(search_ocr["bottom"] - search_ocr["top"]) * 3,
                )
                preview_ocr = {
                    "text": "可以搜索库存",
                    "left": search_ocr["left"],
                    "top": preview_top,
                    "right": search_ocr["right"] + 90,
                    "bottom": preview_top + int(search_ocr["bottom"] - search_ocr["top"]),
                    "confidence": 0.96,
                }
                with tempfile.TemporaryDirectory(prefix="add-friend-production-entry-") as temp_dir:
                    with production_boundary(
                        image,
                        ocr_items=[search_ocr, preview_ocr],
                        click_points=clicks,
                        initial_snapshots=initial_snapshots,
                    ):
                        with self.assertRaises(PhysicalClickReached):
                            sidecar.add_friend_entry_click_plan_payload(
                                1001,
                                {"visible_main_windows": [{"hwnd": 1001}]},
                                phone="17368746889",
                                verify_message="我是车金二手车张伟",
                                remark_name="客户-CJ8K2P",
                                remark_code="CJ8K2P",
                                artifact_dir=temp_dir,
                            )
                        final_snapshot = dict(
                            sidecar.layout_snapshot_for_image(image) or {}
                        )

                self.assertEqual(len(clicks), 1, clicks)
                self.assertFalse(initial_snapshots[0].get("executable"))
                self.assertIn(
                    "shared_header_boundary_missing",
                    (initial_snapshots[0].get("layout_builder") or {}).get("conflicts") or [],
                )
                self.assertEqual(clicks[0]["action_name"], "plus_entry_click_1")
                self.assertTrue(clicks[0]["layout_snapshot_id"])
                self.assertTrue(final_snapshot.get("executable"), final_snapshot)
                selected_search_anchors = [
                    anchor
                    for anchor in final_snapshot.get("anchors") or []
                    if anchor.get("name") == "search_text"
                ]
                self.assertEqual(len(selected_search_anchors), 1, final_snapshot)
                self.assertEqual(
                    selected_search_anchors[0].get("text"),
                    search_text,
                    final_snapshot,
                )
                header_bounds = final_snapshot["sidebar_header_bounds"]
                self.assertTrue(
                    sidecar.point_in_bounds(*clicks[0]["point"], header_bounds),
                    (clicks[0], header_bounds),
                )
                self.assertGreater(clicks[0]["point"][0], float(item["right"]))

    def test_search_icon_cannot_replace_missing_plus_in_preclick_or_calibration(self) -> None:
        image, item = _bright_wechat_add_friend_frame(selected_row=2)
        draw = ImageDraw.Draw(image)
        # Remove the real plus while leaving a magnifying-glass-shaped search
        # icon in the same dynamically discovered operation band.
        draw.rectangle((326, 29, 366, 72), fill=(234, 234, 234))
        draw.ellipse((100, 44, 114, 58), outline=(70, 70, 70), width=2)
        draw.line((112, 57, 120, 65), fill=(70, 70, 70), width=2)
        for calibration_only in (False, True):
            with self.subTest(calibration_only=calibration_only):
                clicks: list[dict[str, Any]] = []
                with tempfile.TemporaryDirectory(prefix="add-friend-no-plus-") as temp_dir:
                    with production_boundary(
                        image,
                        ocr_items=[_search_item(item, "Q搜索")],
                        click_points=clicks,
                    ):
                        result = sidecar.add_friend_entry_click_plan_payload(
                            1001,
                            {"visible_main_windows": [{"hwnd": 1001}]},
                            phone="17368746889",
                            verify_message="我是车金二手车张伟",
                            remark_name="客户-CJ8K2P",
                            remark_code="CJ8K2P",
                            artifact_dir=temp_dir,
                            calibration_only=calibration_only,
                        )

                self.assertFalse(result["ok"], result)
                self.assertEqual(result["error_code"], ERROR_PLUS_ENTRY_NOT_FOUND)
                if calibration_only:
                    target = result["before"]["planned_targets"][0]
                else:
                    target = result["window_probe"][
                        "add_friend_pre_click_main_window_readiness"
                    ]["planned_targets"][0]
                self.assertFalse(target["executable"])
                self.assertEqual(clicks, [])

    def test_user_bright_screenshots_pixel_layout_and_plus_only_when_available(self) -> None:
        """Replay real pixels without claiming that local OCR was exercised."""

        evidence_root = Path(
            "/Users/zhangwentao/Library/Containers/com.tencent.qq/Data/Library/Application Support/QQ/"
            "nt_qq_87656dd28e800b4cd304fb179d678a3c/nt_data/Pic/2026-08/Ori"
        )
        names = (
            "21ae6f01eaf547f8e1d2bf2e630b5876.png",
            "0f0d9568f374f098574c418408939dcf.png",
            "ee34bbd365d17205f82baa7e23379e47.png",
            "5bea6fc7a3cf952befccd9dc2ff174c6.png",
        )
        if not all((evidence_root / name).is_file() for name in names):
            self.skipTest("user-provided bright-theme screenshots are unavailable")
        for name in names:
            with self.subTest(name=name):
                image = Image.open(evidence_root / name).convert("RGB")
                item = {
                    "text": "Q搜索",
                    "left": 100,
                    "top": 60,
                    "right": 154,
                    "bottom": 82,
                    "confidence": 0.92,
                    "center_x": 127,
                    "center_y": 71,
                }
                layout = window_layout.build_add_friend_entry_layout_regions(
                    image,
                    search_anchor_items=[item],
                )
                self.assertTrue(layout["ok"], layout)
                candidates = add_friend_layout.vision_plus_icon_candidates(
                    image,
                    image.size,
                    search_bounds=layout["regions"]["sidebar_header_bounds"],
                )
                self.assertEqual(len(candidates), 1, candidates)
                self.assertEqual(candidates[0]["source"], "vision_plus_icon")


if __name__ == "__main__":
    unittest.main()
