"""Production-entry regressions for v0.9.31 startup-map add-friend targeting."""

from __future__ import annotations

from contextlib import ExitStack, contextmanager
import tempfile
import unittest
from pathlib import Path
import sys
from types import SimpleNamespace
from typing import Any, Iterator
from unittest.mock import patch

from PIL import Image, ImageDraw


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from apps.wechat_ai_customer_service.adapters import add_friend_layout  # noqa: E402
from apps.wechat_ai_customer_service.adapters import wechat_win32_ocr_sidecar as sidecar  # noqa: E402
from apps.wechat_ai_customer_service.adapters.wechat_win32_ocr import add_friend_windows  # noqa: E402
from apps.wechat_ai_customer_service.adapters.wechat_win32_ocr import window_layout  # noqa: E402
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
    capture_labels: list[str] | None = None,
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
        if capture_labels is not None:
            capture_labels.append(label)
        screenshot_path = str(Path(artifact_dir) / f"{label}.png")
        sidecar._register_layout_snapshot(
            hwnd,
            image,
            capture_mode=sidecar.win32_ocr_layout.CAPTURE_MODE_CLIENT_AREA,
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
        calibration_dir = Path(
            stack.enter_context(tempfile.TemporaryDirectory(prefix="wechat-startup-calibration-"))
        )
        calibration_path = calibration_dir / "startup_layout_calibration.json"
        stack.enter_context(patch.object(sidecar, "STARTUP_CALIBRATION_PATH", calibration_path))
        stack.enter_context(
            patch.object(
                sidecar,
                "win32process",
                type(
                    "FakeWin32Process",
                    (),
                    {"GetWindowThreadProcessId": staticmethod(lambda _hwnd: (1, 2001))},
                )(),
            )
        )
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
        if image.width >= 700 and image.height >= 720:
            calibration = window_layout.build_startup_layout_calibration(
                hwnd=1001,
                process_id=2001,
                image=image,
                ocr_items=ocr_items,
                window_rect=[0, 0, image.width, image.height],
                client_rect=_client_geometry(image),
                client_screen_origin=[0, 0],
                dpi_scale=1.0,
                capture_mode=window_layout.CAPTURE_MODE_CLIENT_AREA,
            )
            window_layout.write_startup_layout_calibration(calibration_path, calibration)
        yield


class AddFriendProductionEntryTest(unittest.TestCase):
    def test_public_run_action_ignores_hidden_weixin_and_reaches_real_c1_click(self) -> None:
        """UAT-006: public entry keeps the real 0.9.20 visible-window selector."""

        image, item = _bright_wechat_add_friend_frame(selected_row=2)
        ocr_items = [_search_item(item, "Q搜索")]
        clicks: list[dict[str, Any]] = []
        activation_calls: list[tuple[int, bool]] = []
        foreground = {"hwnd": 909}
        visible_target = {
            "hwnd": 1001,
            "pid": 2001,
            "title": "微信",
            "class_name": "Qt51514QWindowIcon",
            "visible": True,
        }
        hidden_weixin = {
            "hwnd": 1002,
            "pid": 2001,
            "title": "Weixin",
            "class_name": "Qt51514QWindowIcon",
            "visible": False,
        }
        probe = {
            "windows": [visible_target, hidden_weixin],
            "visible_windows": [visible_target],
            "main_windows": [visible_target, hidden_weixin],
            "visible_main_windows": [visible_target],
            "main_count": 2,
            "visible_main_count": 1,
        }

        def foreground_match(hwnd: int) -> dict[str, Any]:
            matched = int(foreground["hwnd"]) == int(hwnd)
            return {
                "ok": matched,
                "reason": "foreground_matches_target" if matched else "foreground_not_target",
                "target_hwnd": int(hwnd),
                "foreground_hwnd": int(foreground["hwnd"]),
            }

        def activate(hwnd: int, *, foreground_only: bool = False) -> None:
            activation_calls.append((int(hwnd), bool(foreground_only)))
            foreground["hwnd"] = int(hwnd)

        args = SimpleNamespace(
            action="add-friend-entry-click-plan-windows",
            artifact_dir="",
            phone="17368746889",
            wechat="",
            verify_message="我是车金二手车张伟",
            remark_name="客户-CJ8K2P",
            remark_code="CJ8K2P",
            calibration_only=False,
        )
        user32 = SimpleNamespace(
            IsIconic=lambda _hwnd: 0,
            IsWindowVisible=lambda hwnd: int(hwnd) == 1001,
        )
        win32gui = SimpleNamespace(IsWindow=lambda hwnd: int(hwnd) in {1001, 1002})

        with tempfile.TemporaryDirectory(prefix="add-friend-hidden-weixin-") as temp_dir:
            args.artifact_dir = temp_dir
            with production_boundary(image, ocr_items=ocr_items, click_points=clicks):
                with ExitStack() as stack:
                    stack.enter_context(patch.object(sidecar, "_WIN32_IMPORT_ERROR", ""))
                    stack.enter_context(patch.object(sidecar, "probe_wechat_windows", return_value=probe))
                    stack.enter_context(patch.object(sidecar, "foreground_window_matches_target", side_effect=foreground_match))
                    stack.enter_context(patch.object(sidecar, "activate_window", side_effect=activate))
                    stack.enter_context(patch.object(sidecar, "win32gui", win32gui))
                    stack.enter_context(patch.object(sidecar.ctypes, "windll", SimpleNamespace(user32=user32), create=True))
                    stack.enter_context(patch.object(sidecar, "humanized_action_sleep", return_value=0.0))
                    with self.assertRaises(PhysicalClickReached):
                        sidecar.run_action(args)

        self.assertEqual(activation_calls, [(1001, False)])
        self.assertEqual(foreground["hwnd"], 1001)
        self.assertEqual(len(clicks), 1, clicks)
        self.assertEqual(clicks[0]["action_name"], "plus_entry_click_1")

    def test_public_run_action_rejects_visible_hwnd_different_from_calibration(self) -> None:
        """The hidden target cannot authorize a different visible WeChat HWND."""

        image, item = _bright_wechat_add_friend_frame(selected_row=2)
        clicks: list[dict[str, Any]] = []
        visible_other = {
            "hwnd": 1002,
            "pid": 2001,
            "title": "微信",
            "class_name": "Qt51514QWindowIcon",
            "visible": True,
        }
        hidden_calibrated = {
            "hwnd": 1001,
            "pid": 2001,
            "title": "Weixin",
            "class_name": "Qt51514QWindowIcon",
            "visible": False,
        }
        probe = {
            "windows": [visible_other, hidden_calibrated],
            "visible_windows": [visible_other],
            "main_windows": [visible_other, hidden_calibrated],
            "visible_main_windows": [visible_other],
            "main_count": 2,
            "visible_main_count": 1,
        }
        args = SimpleNamespace(
            action="add-friend-entry-click-plan-windows",
            artifact_dir="",
            phone="17368746889",
            wechat="",
            verify_message="我是车金二手车张伟",
            remark_name="客户-CJ8K2P",
            remark_code="CJ8K2P",
            calibration_only=False,
        )

        with tempfile.TemporaryDirectory(prefix="add-friend-wrong-visible-hwnd-") as temp_dir:
            args.artifact_dir = temp_dir
            with production_boundary(
                image,
                ocr_items=[_search_item(item, "Q搜索")],
                click_points=clicks,
            ):
                with ExitStack() as stack:
                    stack.enter_context(patch.object(sidecar, "_WIN32_IMPORT_ERROR", ""))
                    stack.enter_context(patch.object(sidecar, "probe_wechat_windows", return_value=probe))
                    stack.enter_context(patch.object(sidecar, "activate_window", return_value=None))
                    result = sidecar.run_action(args)

        self.assertFalse(result["ok"], result)
        self.assertEqual(
            result["error_code"],
            "WECHAT_UI_STARTUP_CALIBRATION_FAILED",
            result,
        )
        self.assertEqual(result["reason"], "startup_calibration_hwnd_changed", result)
        self.assertTrue(result["no_clicks_performed"], result)
        self.assertEqual(clicks, [])

    def test_public_c1_clicks_plus_then_ocr_confirmed_menu_with_popup_foreground(self) -> None:
        """Exercise public entry through both production click mapping layers.

        Only Windows capture/OCR/mouse boundaries are replaced.  The public
        ``run_action`` dispatcher, startup-owner check, add-friend flow,
        region mapping, popup OCR target selection and bounded click mapping
        all execute as production code.
        """

        image, search_item = _bright_wechat_add_friend_frame(selected_row=2)
        search = _search_item(search_item, "Q搜索")
        physical_clicks: list[dict[str, Any]] = []
        foreground = {"hwnd": 1001}
        menu_hwnd = 77001
        visible_target = {
            "hwnd": 1001,
            "pid": 2001,
            "title": "微信",
            "class_name": "Qt51514QWindowIcon",
            "visible": True,
        }
        probe = {
            "windows": [visible_target],
            "visible_windows": [visible_target],
            "main_windows": [visible_target],
            "visible_main_windows": [visible_target],
            "main_count": 1,
            "visible_main_count": 1,
        }

        def staged_ocr(_image: Image.Image, _bounds: list[int]) -> list[dict[str, Any]]:
            if not physical_clicks:
                return [dict(search)] if _bounds == [0, 0, image.width, image.height] else []
            return [{
                "text": "添加朋友",
                "left": 338,
                "top": 160,
                "right": 414,
                "bottom": 190,
                "center_x": 376,
                "center_y": 175,
                "confidence": 0.98,
            }]

        def physical_mouse(
            screen_x: int,
            screen_y: int,
            *,
            bounds: list[int],
            action_name: str,
        ) -> dict[str, Any]:
            physical_clicks.append({
                "point": [int(screen_x), int(screen_y)],
                "bounds": list(bounds),
                "action_name": str(action_name),
                "foreground_before": int(foreground["hwnd"]),
            })
            if len(physical_clicks) == 1:
                foreground["hwnd"] = menu_hwnd
            return {"ok": True, "action_name": action_name}

        args = SimpleNamespace(
            action="add-friend-entry-click-plan-windows",
            artifact_dir="",
            phone="17368746889",
            wechat="",
            verify_message="我是车金二手车张伟",
            remark_name="客户-CJ8K2P",
            remark_code="CJ8K2P",
            calibration_only=False,
            action_journal="",
        )
        real_bounded_click = sidecar.human_window_image_click_in_bounds

        with tempfile.TemporaryDirectory(prefix="add-friend-v0924-menu-chain-") as temp_dir:
            args.artifact_dir = temp_dir
            with production_boundary(image, ocr_items=[search], click_points=[]):
                with ExitStack() as stack:
                    stack.enter_context(patch.object(sidecar, "_WIN32_IMPORT_ERROR", ""))
                    stack.enter_context(patch.object(sidecar, "probe_wechat_windows", return_value=probe))
                    stack.enter_context(patch.object(sidecar, "activate_window", side_effect=lambda hwnd, **_kwargs: foreground.__setitem__("hwnd", int(hwnd))))
                    stack.enter_context(patch.object(sidecar, "run_ocr_on_screen_region", side_effect=staged_ocr))
                    stack.enter_context(patch.object(sidecar, "human_window_image_click_in_bounds", side_effect=real_bounded_click))
                    stack.enter_context(patch.object(sidecar, "human_screen_click_in_bounds", side_effect=physical_mouse))
                    stack.enter_context(patch.object(sidecar, "ensure_left_button_released", return_value=None))
                    stack.enter_context(patch.object(sidecar, "require_active_ui_action_budget", return_value={"ok": True}))
                    stack.enter_context(patch.object(sidecar, "wait_for_add_friend_dialog_window", return_value={"ok": False, "reason": "stop_after_verified_menu_click"}))
                    stack.enter_context(patch.object(sidecar, "write_add_friend_entry_click_review", return_value="review.html"))
                    stack.enter_context(patch.object(sidecar, "win32gui", SimpleNamespace(IsWindow=lambda hwnd: int(hwnd) == 1001, GetWindowRect=lambda _hwnd: (0, 0, image.width, image.height))))
                    result = sidecar.run_action(args)

        self.assertEqual(
            [item["action_name"] for item in physical_clicks],
            ["plus_entry_click_1", "add_friend_menu_entry_click"],
            (physical_clicks, result),
        )
        self.assertEqual(physical_clicks[0]["foreground_before"], 1001)
        self.assertEqual(
            physical_clicks[1]["foreground_before"],
            menu_hwnd,
            "the popup/menu HWND must not be rejected by a main-HWND equality gate",
        )
        self.assertTrue(result["menu_click"]["menu_clicked"], result)
        self.assertEqual(
            result["menu_click"]["reason"],
            "add_friend_dialog_window_not_found_after_menu_click",
            result,
        )

    def test_frame_seed_requires_same_live_hwnd_frame_and_layout_snapshot(self) -> None:
        image, item = _bright_wechat_add_friend_frame(selected_row=2)
        ocr_items = [_search_item(item, "Q搜索")]
        with tempfile.TemporaryDirectory(prefix="add-friend-frame-seed-") as temp_dir:
            with production_boundary(image, ocr_items=ocr_items, click_points=[]):
                screenshot, path = sidecar.capture_wechat_window_visible_screen(
                    1001,
                    artifact_dir=temp_dir,
                    label="frame_seed",
                )
                sidecar.finalize_add_friend_entry_layout_snapshot(screenshot, ocr_items)
                seed = add_friend_windows._reusable_frame_seed(
                    1001,
                    screenshot,
                    path,
                    ocr_items,
                )
                self.assertIs(
                    add_friend_windows._valid_reusable_frame_seed(1001, seed),
                    seed,
                )
                self.assertIsNone(
                    add_friend_windows._valid_reusable_frame_seed(1002, seed)
                )
                sidecar.invalidate_layout_snapshot(1001, reason="test_ui_changed")
                self.assertIsNone(
                    add_friend_windows._valid_reusable_frame_seed(1001, seed)
                )

    def test_real_sidecar_query_wrapper_accepts_and_forwards_frame_seed(self) -> None:
        """Cross caller facade and implementation; mock only Windows boundaries."""

        image = Image.new("RGB", (468, 520), (245, 246, 248))
        capture_labels: list[str] = []
        with tempfile.TemporaryDirectory(prefix="add-friend-query-wrapper-") as temp_dir:
            with production_boundary(
                image,
                ocr_items=[],
                click_points=[],
                capture_labels=capture_labels,
            ):
                screenshot, path = sidecar.capture_wechat_window_visible_screen(
                    74001,
                    artifact_dir=temp_dir,
                    label="query_seed",
                    popup_window=True,
                )
                seed = add_friend_windows._reusable_frame_seed(
                    74001,
                    screenshot,
                    path,
                    [],
                )
                with self.assertRaisesRegex(
                    RuntimeError,
                    "WECHAT_UI_LAYOUT_UNRESOLVED:add_friend_search_input",
                ):
                    sidecar.input_add_friend_query_and_search(
                        74001,
                        Path(temp_dir),
                        query="17368746889",
                        frame_seed=seed,
                    )

        self.assertEqual(
            capture_labels,
            ["query_seed"],
            "the real sidecar wrapper must forward the live frame instead of recapturing",
        )

    def test_aligned_avatar_edges_do_not_replace_nav_on_production_entry(self) -> None:
        """Replay UAT-005 from public planning entry through the mouse boundary."""

        width, height = 980, 860
        nav_x, sidebar_x = 84, 382
        image = Image.new("RGB", (width, height), (250, 250, 250))
        draw = ImageDraw.Draw(image)
        draw.rectangle((0, 0, nav_x - 1, height - 1), fill=(246, 246, 246))
        draw.rectangle((nav_x, 0, sidebar_x - 1, height - 1), fill=(234, 234, 234))
        draw.line((nav_x, 0, nav_x, height - 1), fill=(155, 155, 155), width=2)
        draw.line((sidebar_x, 0, sidebar_x, height - 1), fill=(140, 140, 140), width=2)
        draw.line((sidebar_x, 88, width - 1, 88), fill=(170, 170, 170), width=2)
        draw.line((sidebar_x, 688, width - 1, 688), fill=(170, 170, 170), width=2)
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
        measured_nav = snapshot.get("left_nav_bounds", [None, None, None])[2]
        self.assertLessEqual(abs(int(measured_nav) - nav_x), 6, snapshot)
        self.assertNotEqual(measured_nav, 144, snapshot)
        self.assertEqual(snapshot.get("sidebar_bounds", [None, None, None])[2], sidebar_x, snapshot)
        self.assertEqual(len(clicks), 1, clicks)
        header_left, _header_top, header_right, _header_bottom = snapshot["sidebar_header_bounds"]
        expected_x = header_left + round((265 / 298) * (header_right - header_left))
        search_anchor = next(
            anchor
            for anchor in snapshot.get("anchors", [])
            if anchor.get("name") == "sidebar_search_anchor"
        )
        search_top = int(search_anchor["bounds"][1])
        search_bottom = int(search_anchor["bounds"][3])
        expected_y = int((search_top + search_bottom) / 2)
        self.assertEqual(clicks[0]["point"], [expected_x, expected_y], clicks)
        self.assertGreaterEqual(clicks[0]["point"][1], search_top, clicks)
        self.assertLessEqual(clicks[0]["point"][1], search_bottom, clicks)
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
                capture_labels: list[str] = []
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
                        capture_labels=capture_labels,
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
                self.assertEqual(
                    capture_labels,
                    ["add_friend_pre_click_main_window"],
                    capture_labels,
                )
                self.assertTrue(initial_snapshots[0].get("executable"), initial_snapshots[0])
                self.assertTrue(initial_snapshots[0].get("calibration_id"), initial_snapshots[0])
                self.assertEqual(clicks[0]["action_name"], "plus_entry_click_1")
                self.assertTrue(clicks[0]["layout_snapshot_id"])
                self.assertTrue(final_snapshot.get("executable"), final_snapshot)
                selected_search_anchors = [
                    anchor
                    for anchor in final_snapshot.get("anchors") or []
                    if anchor.get("name") == "sidebar_search_anchor"
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

    def test_search_icon_cannot_replace_mapped_plus_reference(self) -> None:
        image, item = _bright_wechat_add_friend_frame(selected_row=2)
        draw = ImageDraw.Draw(image)
        # Remove the real plus while leaving a magnifying-glass-shaped search
        # icon in the same dynamically discovered operation band.
        draw.rectangle((326, 29, 366, 72), fill=(234, 234, 234))
        draw.ellipse((100, 44, 114, 58), outline=(70, 70, 70), width=2)
        draw.line((112, 57, 120, 65), fill=(70, 70, 70), width=2)
        search = _search_item(item, "Q搜索")
        # Calibration-only must expose the mapped reference without clicking.
        calibration_clicks: list[dict[str, Any]] = []
        with tempfile.TemporaryDirectory(prefix="add-friend-mapped-plus-calibration-") as temp_dir:
            with production_boundary(image, ocr_items=[search], click_points=calibration_clicks):
                calibration = sidecar.add_friend_entry_click_plan_payload(
                    1001,
                    {"visible_main_windows": [{"hwnd": 1001}]},
                    phone="17368746889",
                    artifact_dir=temp_dir,
                    calibration_only=True,
                )
        target = calibration["before"]["planned_targets"][0]
        self.assertTrue(calibration["ok"], calibration)
        self.assertEqual(calibration_clicks, [])
        self.assertGreater(target["point"][0], float(search["right"]), target)
        self.assertFalse(sidecar.point_in_bounds(*target["point"], [96, 40, 122, 68]), target)

        # The public production entry must reach the mapped plus boundary, not
        # the magnifying-glass pixels. The original menu confirmation remains
        # the authority after this click.
        clicks: list[dict[str, Any]] = []
        with tempfile.TemporaryDirectory(prefix="add-friend-mapped-plus-click-") as temp_dir:
            with production_boundary(image, ocr_items=[search], click_points=clicks):
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
        self.assertEqual(len(clicks), 1, clicks)
        self.assertGreater(clicks[0]["point"][0], float(search["right"]), clicks)
        self.assertFalse(sidecar.point_in_bounds(*clicks[0]["point"], [96, 40, 122, 68]), clicks)

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
