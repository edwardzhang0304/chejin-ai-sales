"""Contract checks for pure Win32/OCR window action planning."""

from __future__ import annotations

import os
import json
from pathlib import Path
import sys
import tempfile
import types
from typing import Any

from PIL import Image, ImageDraw


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from apps.wechat_ai_customer_service.adapters import wechat_win32_ocr_sidecar as sidecar  # noqa: E402
from apps.wechat_ai_customer_service.adapters.wechat_win32_ocr import window_action_planning  # noqa: E402
from apps.wechat_ai_customer_service.adapters.wechat_win32_ocr import window_layout  # noqa: E402


def assert_true(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def real_layout_frame(width: int = 980, height: int = 860) -> Image.Image:
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    nav_x, sidebar_x = int(width * 0.073), int(width * 0.378)
    sidebar_header_y = chat_header_y = int(height * 0.102)
    input_y = int(height * 0.814)
    draw.rectangle((0, 0, nav_x - 1, height - 1), fill=(245, 245, 245))
    draw.rectangle((nav_x, 0, sidebar_x - 1, height - 1), fill=(235, 235, 235))
    draw.line((nav_x, 0, nav_x, height - 1), fill=(150, 150, 150), width=2)
    draw.line((sidebar_x, 0, sidebar_x, height - 1), fill=(130, 130, 130), width=2)
    draw.line((nav_x, sidebar_header_y, sidebar_x, sidebar_header_y), fill=(120, 120, 120), width=2)
    draw.line((sidebar_x, chat_header_y, width - 1, chat_header_y), fill=(120, 120, 120), width=2)
    draw.line((sidebar_x, input_y, width - 1, input_y), fill=(120, 120, 120), width=2)
    return image


def real_layout_snapshot(width: int = 980, height: int = 860, *, image: Image.Image | None = None, hwnd: int = 1) -> dict:
    image = image or real_layout_frame(width, height)
    layout = window_layout.build_structural_layout_regions(image)
    assert_true(layout.get("ok"), f"real layout builder rejected frame: {layout}")
    return window_layout.build_layout_snapshot(
        hwnd=hwnd,
        frame_id=window_layout.new_frame_id(hwnd),
        capture_mode=window_layout.CAPTURE_MODE_WINDOW_VISIBLE_SCREEN,
        image_size=image.size,
        capture_screen_origin=[0, 0],
        window_rect=[0, 0, width, height],
        client_rect=[0, 0, width, height],
        client_screen_origin=[0, 0],
        dpi_scale=1.0,
        regions=layout["regions"],
        anchors=layout["anchors"],
        confidence=layout["confidence"],
        conflicts=layout["conflicts"],
        executable=True,
    )


def register_real_layout_frame(image: Image.Image, *, hwnd: int) -> dict:
    snapshot = real_layout_snapshot(image.width, image.height, image=image, hwnd=hwnd)
    sidecar._LAYOUT_SNAPSHOT_STORE.put(snapshot)
    sidecar._LATEST_LAYOUT_SNAPSHOT_BY_HWND[int(hwnd)] = str(snapshot["layout_snapshot_id"])
    sidecar._LAYOUT_SNAPSHOT_ID_BY_IMAGE_ID[id(image)] = str(snapshot["layout_snapshot_id"])
    return snapshot


def plan(
    before: dict[str, int],
    *,
    enabled: bool = True,
    dpi_scale: float = 1.0,
    requested_width: object = None,
    requested_height: object = None,
    requested_left: object = None,
    requested_top: object = None,
    enforce_recommended: bool = True,
    fixed_origin: bool = True,
    screen_width: int = 1920,
    screen_height: int = 1200,
    screen_metrics_available: bool = True,
) -> dict:
    return window_action_planning.plan_normalize_wechat_window(
        before,
        enabled=enabled,
        dpi_scale=dpi_scale,
        requested_width=requested_width,
        requested_height=requested_height,
        requested_left=requested_left,
        requested_top=requested_top,
        enforce_recommended=enforce_recommended,
        fixed_origin=fixed_origin,
        screen_width=screen_width,
        screen_height=screen_height,
        screen_metrics_available=screen_metrics_available,
        default_width=sidecar.DEFAULT_SAFE_WINDOW_WIDTH,
        default_height=sidecar.DEFAULT_SAFE_WINDOW_HEIGHT,
        min_width=sidecar.MIN_SAFE_WINDOW_WIDTH,
        min_height=sidecar.MIN_SAFE_WINDOW_HEIGHT,
        max_width=sidecar.MAX_SAFE_WINDOW_WIDTH,
        max_height=sidecar.MAX_SAFE_WINDOW_HEIGHT,
    )


def test_window_action_planning_module_exports_expected_helpers() -> None:
    assert_true(
        callable(getattr(window_action_planning, "plan_normalize_wechat_window", None)),
        "window action planner missing: plan_normalize_wechat_window",
    )
    assert_true(
        callable(getattr(window_action_planning, "recommended_window_scale_for_screen", None)),
        "window action planner missing: recommended_window_scale_for_screen",
    )


def test_plan_disabled_matches_sidecar_disabled_shape() -> None:
    before = {"left": 10, "top": 20, "width": 980, "height": 860}
    result = plan(before, enabled=False)
    assert_true(result == {"ok": True, "enabled": False, "applied": False, "before": before}, f"disabled plan mismatch: {result}")


def test_plan_1920x1200_fixed_origin_matches_default_safe_window() -> None:
    before = {"left": -180, "top": 80, "right": 800, "bottom": 940, "width": 980, "height": 860}
    result = plan(before, screen_width=1920, screen_height=1200)
    assert_true(result.get("move") is True, f"offscreen window should need normalize: {result}")
    assert_true((result.get("left"), result.get("top"), result.get("width"), result.get("height")) == (0, 0, 980, 860), f"default normalize target mismatch: {result}")
    assert_true(result.get("screen") == {"width": 1920, "height": 1200}, f"screen metadata mismatch: {result}")


def test_plan_1920x1080_promotes_observed_narrow_window_to_standard_size() -> None:
    before = {"left": 8, "top": 0, "width": 800, "height": 852}
    result = plan(before, screen_width=1920, screen_height=1080)
    assert_true((result.get("left"), result.get("top"), result.get("width"), result.get("height")) == (0, 0, 980, 860), f"1080p target mismatch: {result}")


def test_plan_high_resolution_keeps_normal_window_size() -> None:
    before = {"left": 20, "top": 24, "width": 980, "height": 860}
    result = plan(before, dpi_scale=1.5, screen_width=3840, screen_height=2160)
    assert_true(
        (result.get("left"), result.get("top"), result.get("width"), result.get("height")) == (0, 0, 980, 860),
        f"high resolution must not enlarge the WeChat window: {result}",
    )
    assert_true(result.get("target") == {"width": 980, "height": 860}, f"high resolution target metadata mismatch: {result}")
    assert_true(result.get("resolution_scale") == 1.0, f"resolution scale metadata mismatch: {result}")


def test_plan_1920_class_displays_do_not_multiply_window_by_dpi() -> None:
    cases = [
        ("1920x1080@100", 1920, 1080, 1.0, (0, 0, 980, 860), {"width": 980, "height": 860}),
        ("1920x1080@125", 1920, 1080, 1.25, (0, 0, 980, 860), {"width": 980, "height": 860}),
        ("1920x1200@125", 1920, 1200, 1.25, (0, 0, 980, 860), {"width": 980, "height": 860}),
        ("1920x1080@150", 1920, 1080, 1.5, (0, 0, 980, 860), {"width": 980, "height": 860}),
    ]
    for label, screen_width, screen_height, dpi_scale, expected_rect, expected_target in cases:
        result = plan(
            {"left": 20, "top": 24, "width": 900, "height": 800},
            dpi_scale=dpi_scale,
            screen_width=screen_width,
            screen_height=screen_height,
        )
        assert_true(
            (result.get("left"), result.get("top"), result.get("width"), result.get("height")) == expected_rect,
            f"{label} should keep the 1920-class physical window size: {result}",
        )
        assert_true(result.get("target") == expected_target, f"{label} target metadata mismatch: {result}")
        assert_true(result.get("resolution_scale") == 1.0, f"{label} must not scale by DPI: {result}")
        assert_true(result.get("dpi_scale") == dpi_scale, f"{label} should still report DPI for diagnostics: {result}")


def test_plan_resolution_dpi_matrix_stays_visible_and_safe() -> None:
    cases = [
        ("1366x768@100", 1366, 768, 1.0, (0, 0, 980, 720), {"width": 980, "height": 860}),
        ("1440x900@100", 1440, 900, 1.0, (0, 0, 980, 852), {"width": 980, "height": 860}),
        ("1920x1080@100", 1920, 1080, 1.0, (0, 0, 980, 860), {"width": 980, "height": 860}),
        ("1920x1080@125", 1920, 1080, 1.25, (0, 0, 980, 860), {"width": 980, "height": 860}),
        ("1920x1200@125", 1920, 1200, 1.25, (0, 0, 980, 860), {"width": 980, "height": 860}),
        ("2560x1440@100", 2560, 1440, 1.0, (0, 0, 980, 860), {"width": 980, "height": 860}),
        ("3840x2160@150", 3840, 2160, 1.5, (0, 0, 980, 860), {"width": 980, "height": 860}),
    ]
    for label, screen_width, screen_height, dpi_scale, expected_rect, expected_target in cases:
        result = plan(
            {"left": -20, "top": -15, "width": 700, "height": 720},
            dpi_scale=dpi_scale,
            screen_width=screen_width,
            screen_height=screen_height,
        )
        actual_rect = (result.get("left"), result.get("top"), result.get("width"), result.get("height"))
        assert_true(actual_rect == expected_rect, f"{label} planned rect mismatch: {result}")
        assert_true(result.get("target") == expected_target, f"{label} target metadata mismatch: {result}")
        assert_true(int(result.get("width") or 0) <= screen_width, f"{label} width exceeds screen: {result}")
        assert_true(int(result.get("height") or 0) <= screen_height, f"{label} height exceeds screen: {result}")
        assert_true(int(result.get("target", {}).get("width") or 0) <= sidecar.MAX_SAFE_WINDOW_WIDTH, f"{label} target width exceeds max: {result}")
        assert_true(int(result.get("target", {}).get("height") or 0) <= sidecar.MAX_SAFE_WINDOW_HEIGHT, f"{label} target height exceeds max: {result}")


def test_plan_huge_requested_window_clamps_to_safe_maximum() -> None:
    result = plan(
        {"left": 100, "top": 100, "width": 980, "height": 860},
        dpi_scale=4.0,
        requested_width="9999",
        requested_height="9999",
        screen_width=7680,
        screen_height=4320,
    )
    assert_true(
        result.get("target") == {"width": sidecar.MAX_SAFE_WINDOW_WIDTH, "height": sidecar.MAX_SAFE_WINDOW_HEIGHT},
        f"explicit huge requested target should be clamped to max safe bounds: {result}",
    )
    assert_true(
        (result.get("width"), result.get("height")) == (sidecar.MAX_SAFE_WINDOW_WIDTH, sidecar.MAX_SAFE_WINDOW_HEIGHT),
        f"explicit huge requested effective size should stay within max safe bounds: {result}",
    )


def test_plan_tiny_screen_never_exceeds_visible_screen_bounds() -> None:
    result = plan(
        {"left": 0, "top": 0, "width": 980, "height": 860},
        screen_width=500,
        screen_height=420,
    )
    assert_true(
        result.get("ok") is False
        and result.get("reason") == "screen_work_area_too_small_for_minimum_safe_window",
        f"tiny screen must block instead of squeezing the window: {result}",
    )


def test_plan_small_screen_clamps_size_to_visible_screen() -> None:
    before = {"left": 0, "top": 0, "width": 980, "height": 860}
    result = plan(before, screen_width=900, screen_height=760)
    assert_true(
        result.get("ok") is False
        and result.get("reason") == "screen_work_area_too_small_for_minimum_safe_window",
        f"small screen must block instead of squeezing the window: {result}",
    )


def test_plan_non_fixed_origin_clamps_existing_origin() -> None:
    before = {"left": 1500, "top": -30, "width": 800, "height": 700}
    result = plan(before, fixed_origin=False, screen_width=1920, screen_height=1200)
    assert_true((result.get("left"), result.get("top"), result.get("width"), result.get("height")) == (940, 0, 980, 860), f"non-fixed origin clamp mismatch: {result}")
    assert_true(result.get("fixed_origin") is False, f"fixed_origin metadata mismatch: {result}")


def test_plan_recommended_floor_and_custom_origin() -> None:
    result = plan(
        {"left": 0, "top": 0, "width": 720, "height": 720},
        requested_width="720",
        requested_height="730",
        requested_left="2000",
        requested_top="99",
        screen_width=1920,
        screen_height=1200,
    )
    assert_true(result.get("requested_target") == {"width": 720, "height": 730}, f"requested target mismatch: {result}")
    assert_true(result.get("target") == {"width": 980, "height": 860}, f"recommended target mismatch: {result}")
    assert_true(result.get("recommended_floor_applied") is True, f"recommended floor should apply: {result}")
    assert_true((result.get("left"), result.get("top")) == (940, 99), f"custom origin clamp mismatch: {result}")


def test_plan_without_screen_metrics_uses_target_and_max_bounds() -> None:
    result = plan(
        {"left": -10, "top": 70, "width": 700, "height": 720},
        requested_left="9000",
        requested_top="-5",
        screen_metrics_available=False,
        screen_width=0,
        screen_height=0,
    )
    assert_true(result.get("screen") == {"width": 0, "height": 0}, f"missing screen metadata mismatch: {result}")
    assert_true((result.get("left"), result.get("top"), result.get("width"), result.get("height")) == (2560, 0, 980, 860), f"missing screen fallback mismatch: {result}")


def test_add_friend_layout_finalization_requires_only_dynamic_search_row() -> None:
    image = real_layout_frame()
    original = register_real_layout_frame(image, hwnd=991)
    completed = sidecar.finalize_add_friend_entry_layout_snapshot(
        image,
        [
            {
                "text": "搜索",
                "left": 120,
                "top": 50,
                "right": 154,
                "bottom": 70,
                "confidence": 0.98,
            }
        ],
    )
    assert_true(bool(completed and completed.get("executable")), f"add-friend action layout should finalize: {completed}")
    assert_true(
        completed.get("required_region_names") == list(window_layout.ADD_FRIEND_ENTRY_LAYOUT_REGION_NAMES),
        f"add-friend action retained unrelated chat/input requirements: {completed}",
    )
    assert_true(
        completed.get("frame_id") == original.get("frame_id"),
        "OCR finalization must remain bound to the same captured frame",
    )
    stale_original = sidecar._LAYOUT_SNAPSHOT_STORE.get(str(original.get("layout_snapshot_id") or ""))
    assert_true(bool(stale_original and stale_original.get("invalidated")), "pre-finalization action authority must be invalidated")


def test_empty_ocr_region_reports_layout_error_instead_of_value_error() -> None:
    try:
        sidecar.run_ocr_on_screen_region(Image.new("RGB", (100, 100), "white"), [])
    except window_layout.LayoutSnapshotError as exc:
        assert_true(exc.code == window_layout.ERROR_LAYOUT_UNRESOLVED, f"wrong typed error: {exc.code}")
    else:
        raise AssertionError("empty OCR bounds unexpectedly reached the OCR engine")


def test_sidecar_normalize_wechat_window_uses_same_planned_move_shape() -> None:
    if not hasattr(sidecar.ctypes, "windll"):
        return
    original_get_window_geometry = sidecar.get_window_geometry
    original_win32gui = sidecar.win32gui
    if sidecar.win32gui is None:
        sidecar.win32gui = types.SimpleNamespace(MoveWindow=lambda *_args, **_kwargs: None)
    original_move_window = sidecar.win32gui.MoveWindow
    original_windll = sidecar.ctypes.windll
    previous_env = {
        name: os.environ.get(name)
        for name in (
            "WECHAT_WIN32_OCR_WINDOW_FIXED_ORIGIN",
            "WECHAT_WIN32_OCR_WINDOW_WIDTH",
            "WECHAT_WIN32_OCR_WINDOW_HEIGHT",
            "WECHAT_WIN32_OCR_WINDOW_LEFT",
            "WECHAT_WIN32_OCR_WINDOW_TOP",
            "WECHAT_WIN32_OCR_ENFORCE_RECOMMENDED_WINDOW",
        )
    }
    geometry_state = {"left": -180, "top": 80, "right": 800, "bottom": 940, "width": 980, "height": 860}
    calls: list[tuple[int, int, int, int]] = []

    class FakeUser32:
        @staticmethod
        def GetSystemMetrics(index: int) -> int:
            return 1920 if index == 0 else 1200

    class FakeWindll:
        user32 = FakeUser32()

    def fake_get_window_geometry(_hwnd: int) -> dict[str, int]:
        return dict(geometry_state)

    def fake_move_window(_hwnd: int, left: int, top: int, width: int, height: int, _repaint: bool) -> None:
        calls.append((left, top, width, height))
        geometry_state.update(
            {
                "left": left,
                "top": top,
                "right": left + width,
                "bottom": top + height,
                "width": width,
                "height": height,
            }
        )

    try:
        for name in previous_env:
            os.environ.pop(name, None)
        sidecar.get_window_geometry = fake_get_window_geometry
        sidecar.win32gui.MoveWindow = fake_move_window
        sidecar.ctypes.windll = FakeWindll()
        planned = plan(dict(geometry_state), fixed_origin=True, screen_width=1920, screen_height=1200)
        result = sidecar.normalize_wechat_window(1001)
        expected_move = (planned["left"], planned["top"], planned["width"], planned["height"])
        assert_true(calls == [expected_move], f"sidecar should execute planner move: calls={calls}, planned={planned}")
        assert_true(result.get("target") == planned.get("target"), f"target metadata mismatch: {result} vs {planned}")
        assert_true(result.get("screen") == planned.get("screen"), f"screen metadata mismatch: {result} vs {planned}")
        assert_true(result.get("resolution_scale") == planned.get("resolution_scale"), f"resolution scale metadata mismatch: {result} vs {planned}")
    finally:
        for name, value in previous_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        sidecar.get_window_geometry = original_get_window_geometry
        sidecar.win32gui.MoveWindow = original_move_window
        sidecar.win32gui = original_win32gui
        sidecar.ctypes.windll = original_windll


def test_verify_policy_refuses_a_required_move_without_touching_the_window() -> None:
    original_get_window_geometry = sidecar.get_window_geometry
    original_get_window_client_geometry = sidecar.get_window_client_geometry
    original_window_dpi_scale = sidecar.window_dpi_scale
    original_screen_work_area = sidecar.screen_work_area
    original_invalidate = sidecar.invalidate_layout_snapshot
    original_planner = sidecar.win32_ocr_window_actions.plan_normalize_wechat_window
    original_win32gui = sidecar.win32gui
    calls: list[tuple[Any, ...]] = []
    fake_gui = types.SimpleNamespace(
        IsZoomed=lambda _hwnd: False,
        MoveWindow=lambda *args: calls.append(tuple(args)),
    )
    try:
        sidecar.get_window_geometry = lambda _hwnd: {
            "left": 220,
            "top": 80,
            "right": 1220,
            "bottom": 940,
            "width": 1000,
            "height": 860,
        }
        sidecar.get_window_client_geometry = lambda _hwnd: {
            "left": 0,
            "top": 0,
            "right": 980,
            "bottom": 820,
            "width": 980,
            "height": 820,
            "screen_left": 230,
            "screen_top": 110,
        }
        sidecar.window_dpi_scale = lambda _hwnd: 1.25
        sidecar.screen_work_area = lambda _hwnd: {
            "left": 0,
            "top": 0,
            "right": 1920,
            "bottom": 1080,
            "width": 1920,
            "height": 1080,
        }
        sidecar.invalidate_layout_snapshot = lambda *_args, **_kwargs: None
        sidecar.win32_ocr_window_actions.plan_normalize_wechat_window = lambda *_args, **_kwargs: {
            "ok": True,
            "move": True,
            "left": 0,
            "top": 0,
            "width": 980,
            "height": 860,
            "target": {"left": 0, "top": 0, "width": 980, "height": 860},
            "requested_target": {},
            "resolution_scale": 1.0,
        }
        sidecar.win32gui = fake_gui
        result = sidecar.normalize_wechat_window(2002, allow_move=False)
        assert_true(result.get("ok") is False, f"verify mode must fail closed when geometry moved: {result}")
        assert_true(result.get("error_code") == "WECHAT_UI_LAYOUT_STALE", f"unexpected error code: {result}")
        assert_true(result.get("reason") == "window_geometry_changed_during_active_flow", f"unexpected reason: {result}")
        assert_true(calls == [], f"verify mode must never move a window: {calls}")
    finally:
        sidecar.get_window_geometry = original_get_window_geometry
        sidecar.get_window_client_geometry = original_get_window_client_geometry
        sidecar.window_dpi_scale = original_window_dpi_scale
        sidecar.screen_work_area = original_screen_work_area
        sidecar.invalidate_layout_snapshot = original_invalidate
        sidecar.win32_ocr_window_actions.plan_normalize_wechat_window = original_planner
        sidecar.win32gui = original_win32gui


def test_sidecar_promotes_current_window_to_standard_size_when_unconfigured() -> None:
    original_get_window_geometry = sidecar.get_window_geometry
    original_get_window_client_geometry = sidecar.get_window_client_geometry
    original_window_dpi_scale = sidecar.window_dpi_scale
    original_screen_work_area = sidecar.screen_work_area
    original_invalidate = sidecar.invalidate_layout_snapshot
    original_planner = sidecar.win32_ocr_window_actions.plan_normalize_wechat_window
    original_win32gui = sidecar.win32gui
    observed: dict[str, Any] = {}
    geometry = {
        "left": 0,
        "top": 0,
        "right": 900,
        "bottom": 800,
        "width": 900,
        "height": 800,
    }
    try:
        sidecar.get_window_geometry = lambda _hwnd: dict(geometry)
        sidecar.get_window_client_geometry = lambda _hwnd: {
            **geometry,
            "screen_left": 0,
            "screen_top": 0,
        }
        sidecar.window_dpi_scale = lambda _hwnd: 1.5
        sidecar.screen_work_area = lambda _hwnd: {
            "left": 0,
            "top": 0,
            "right": 3840,
            "bottom": 2160,
            "width": 3840,
            "height": 2160,
        }
        sidecar.invalidate_layout_snapshot = lambda *_args, **_kwargs: None

        def fake_plan(*_args: Any, **kwargs: Any) -> dict[str, Any]:
            observed.update(kwargs)
            return {
                "ok": True,
                "move": True,
                "left": 0,
                "top": 0,
                "width": 980,
                "height": 860,
                "target": {"width": 980, "height": 860},
                "requested_target": {"width": 980, "height": 860},
                "resolution_scale": 1.0,
            }

        sidecar.win32_ocr_window_actions.plan_normalize_wechat_window = fake_plan
        sidecar.win32gui = types.SimpleNamespace(
            IsZoomed=lambda _hwnd: False,
            MoveWindow=lambda _hwnd, left, top, width, height, _repaint: geometry.update(
                {
                    "left": left,
                    "top": top,
                    "right": left + width,
                    "bottom": top + height,
                    "width": width,
                    "height": height,
                }
            ),
        )
        result = sidecar.normalize_wechat_window(2004)
        assert_true(result.get("ok") is True, f"standard window should verify: {result}")
        assert_true(observed.get("default_width") == 980, f"standard width was not requested: {observed}")
        assert_true(observed.get("default_height") == 860, f"standard height was not requested: {observed}")
        assert_true(observed.get("enforce_recommended") is True, f"recommended floor was not enabled: {observed}")
        assert_true(result.get("after", {}).get("width") == 980, f"window was not widened to standard size: {result}")
        assert_true(result.get("after", {}).get("height") == 860, f"window was not raised to standard size: {result}")
    finally:
        sidecar.get_window_geometry = original_get_window_geometry
        sidecar.get_window_client_geometry = original_get_window_client_geometry
        sidecar.window_dpi_scale = original_window_dpi_scale
        sidecar.screen_work_area = original_screen_work_area
        sidecar.invalidate_layout_snapshot = original_invalidate
        sidecar.win32_ocr_window_actions.plan_normalize_wechat_window = original_planner
        sidecar.win32gui = original_win32gui


def test_startup_normalization_defers_full_layout_to_business_action() -> None:
    original_import_error = sidecar._WIN32_IMPORT_ERROR
    original_probe = sidecar.ensure_visible_wechat_window
    original_dismiss = sidecar.dismiss_blank_foreground_window_before_activation
    original_activate = sidecar.activate_window
    original_normalize = sidecar.normalize_wechat_window
    original_env_flag = sidecar.env_flag
    original_quick_login = sidecar.ensure_quick_login_if_available
    original_sleep = sidecar.humanized_action_sleep
    original_capture = sidecar.capture_wechat
    original_ocr = sidecar.run_ocr_traced
    original_layout = sidecar.layout_snapshot_for_image
    forbidden_calls: list[str] = []
    try:
        sidecar._WIN32_IMPORT_ERROR = ""
        sidecar.ensure_visible_wechat_window = lambda interactive=True: {
            "visible_main_windows": [{"hwnd": 2100, "title": "微信"}],
            "visible_windows": [{"hwnd": 2100, "title": "微信"}],
        }
        sidecar.dismiss_blank_foreground_window_before_activation = lambda *_args, **_kwargs: {"attempted": False}
        sidecar.activate_window = lambda _hwnd: None
        sidecar.normalize_wechat_window = lambda _hwnd, allow_move=True: {
            "ok": True,
            "enabled": True,
            "applied": True,
            "reason": "normalized",
            "after": {"left": 0, "top": 0, "width": 980, "height": 860},
        }
        sidecar.env_flag = lambda *_args, **_kwargs: False
        sidecar.ensure_quick_login_if_available = lambda *_args, **_kwargs: {
            "attempted": False,
            "detected": False,
            "reason": "wechat_already_logged_in",
        }
        sidecar.humanized_action_sleep = lambda *_args, **_kwargs: 0.0
        sidecar.capture_wechat = lambda *_args, **_kwargs: forbidden_calls.append("capture")
        sidecar.run_ocr_traced = lambda *_args, **_kwargs: forbidden_calls.append("ocr")
        sidecar.layout_snapshot_for_image = lambda *_args, **_kwargs: forbidden_calls.append("layout")

        result = sidecar.run_action(
            types.SimpleNamespace(
                action="normalize-window",
                artifact_dir=None,
                window_policy="normalize",
            )
        )

        assert_true(result.get("ok") is True, f"geometry-only startup normalization failed: {result}")
        assert_true(result.get("state") == "window_normalized", f"unexpected startup state: {result}")
        assert_true(result.get("readiness", {}).get("skipped") is True, f"layout deferral marker missing: {result}")
        assert_true(forbidden_calls == [], f"startup unexpectedly ran full layout/OCR: {forbidden_calls}")
    finally:
        sidecar._WIN32_IMPORT_ERROR = original_import_error
        sidecar.ensure_visible_wechat_window = original_probe
        sidecar.dismiss_blank_foreground_window_before_activation = original_dismiss
        sidecar.activate_window = original_activate
        sidecar.normalize_wechat_window = original_normalize
        sidecar.env_flag = original_env_flag
        sidecar.ensure_quick_login_if_available = original_quick_login
        sidecar.humanized_action_sleep = original_sleep
        sidecar.capture_wechat = original_capture
        sidecar.run_ocr_traced = original_ocr
        sidecar.layout_snapshot_for_image = original_layout


def test_normalization_rejects_dpi_change_after_geometry_check() -> None:
    original_get_window_geometry = sidecar.get_window_geometry
    original_get_window_client_geometry = sidecar.get_window_client_geometry
    original_window_dpi_scale = sidecar.window_dpi_scale
    original_screen_work_area = sidecar.screen_work_area
    original_invalidate = sidecar.invalidate_layout_snapshot
    original_planner = sidecar.win32_ocr_window_actions.plan_normalize_wechat_window
    original_win32gui = sidecar.win32gui
    dpi_values = iter((1.0, 1.25))
    geometry = {
        "left": 0,
        "top": 0,
        "right": 980,
        "bottom": 860,
        "width": 980,
        "height": 860,
    }
    client = {
        "left": 0,
        "top": 0,
        "right": 960,
        "bottom": 820,
        "width": 960,
        "height": 820,
        "screen_left": 10,
        "screen_top": 30,
    }
    try:
        sidecar.get_window_geometry = lambda _hwnd: dict(geometry)
        sidecar.get_window_client_geometry = lambda _hwnd: dict(client)
        sidecar.window_dpi_scale = lambda _hwnd: next(dpi_values)
        sidecar.screen_work_area = lambda _hwnd: {
            "left": 0,
            "top": 0,
            "right": 1920,
            "bottom": 1200,
            "width": 1920,
            "height": 1200,
        }
        sidecar.invalidate_layout_snapshot = lambda *_args, **_kwargs: None
        sidecar.win32_ocr_window_actions.plan_normalize_wechat_window = lambda *_args, **_kwargs: {
            "ok": True,
            "move": False,
            "left": 0,
            "top": 0,
            "width": 980,
            "height": 860,
            "target": {"width": 980, "height": 860},
            "requested_target": {},
            "resolution_scale": 1.0,
        }
        sidecar.win32gui = types.SimpleNamespace(IsZoomed=lambda _hwnd: False)
        result = sidecar.normalize_wechat_window(2003, allow_move=False)
        assert_true(result.get("ok") is False, f"DPI drift must fail normalization: {result}")
        assert_true(
            result.get("error_code") == "WECHAT_UI_WINDOW_NORMALIZATION_FAILED",
            f"unexpected DPI drift error code: {result}",
        )
        assert_true(
            result.get("reason") == "window_dpi_changed_during_normalization",
            f"DPI drift reason missing: {result}",
        )
    finally:
        sidecar.get_window_geometry = original_get_window_geometry
        sidecar.get_window_client_geometry = original_get_window_client_geometry
        sidecar.window_dpi_scale = original_window_dpi_scale
        sidecar.screen_work_area = original_screen_work_area
        sidecar.invalidate_layout_snapshot = original_invalidate
        sidecar.win32_ocr_window_actions.plan_normalize_wechat_window = original_planner
        sidecar.win32gui = original_win32gui


def test_sidebar_search_query_must_match_exact_remark_code() -> None:
    assert_true(
        sidecar.sidebar_search_query_matches("CJWIN01", "CJWIN01"),
        "exact remark_code search query should be accepted",
    )
    assert_true(
        sidecar.sidebar_search_query_matches("CJWIN01 ", "CJWIN01"),
        "whitespace-only OCR differences should be accepted",
    )
    assert_true(
        not sidecar.sidebar_search_query_matches("CJWCJWIN01WIN01", "CJWIN01"),
        "stale query concatenation must be rejected before clicking a search result",
    )
    assert_true(
        not sidecar.sidebar_search_query_matches("", "CJWIN01"),
        "empty OCR query must be rejected before clicking a search result",
    )
    assert_true(
        sidecar.sidebar_search_query_mismatch_allows_candidate_probe("CVOICE01", "CJVOICE01"),
        "single-character OCR omission should continue to candidate/title confirmation",
    )
    assert_true(
        sidecar.sidebar_search_query_mismatch_allows_candidate_probe("CIVOICE01", "CJVOICE01"),
        "single-character OCR substitution should continue to candidate/title confirmation",
    )
    assert_true(
        not sidecar.sidebar_search_query_mismatch_allows_candidate_probe("CJWCJWIN01WIN01", "CJWIN01"),
        "stale concatenated query must still be rejected",
    )
    assert_true(
        sidecar.sidebar_search_clear_residue_allows_candidate_probe("Q"),
        "search icon OCR residue after clear should not block candidate/title confirmation",
    )
    assert_true(
        not sidecar.sidebar_search_clear_residue_allows_candidate_probe("CJWCJWIN01WIN01"),
        "real stale query residue after clear must still be rejected",
    )


def test_sidebar_search_query_ignores_empty_placeholder_icon_text() -> None:
    query = sidecar.sidebar_search_query_text(
        [
            {"text": "Q搜索", "center_x": 138, "center_y": 70, "left": 112, "right": 166, "top": 58, "bottom": 82},
            {"text": "腾讯新闻", "center_x": 178, "center_y": 128, "left": 136, "right": 220, "top": 112, "bottom": 142},
        ],
        (980, 860),
        geometry={"width": 980, "height": 860},
    )
    assert_true(query == "", f"empty search placeholder must not be treated as query content: {query!r}")


def test_search_result_candidate_uses_window_image_click_coordinates() -> None:
    original_human_window_image_click_in_bounds = sidecar.human_window_image_click_in_bounds
    original_human_client_click = sidecar.human_client_click
    original_humanized_action_sleep = sidecar.humanized_action_sleep
    original_validate_active_send_target = sidecar.validate_active_send_target
    calls: list[tuple[str, int, int]] = []

    def fake_window_image_click(_hwnd: int, x: int, y: int, *, bounds: list[int], action_name: str, expected_snapshot_id: str = "") -> dict:
        calls.append(("window_image", int(x), int(y)))
        return {"ok": True, "bounds": bounds, "action_name": action_name, "layout_snapshot_id": expected_snapshot_id}

    def fake_client_click(_hwnd: int, x: int, y: int) -> None:
        calls.append(("client", int(x), int(y)))

    def fake_validate(_hwnd: int, _target: str, *, exact: bool, artifact_dir: str | None = None) -> dict:
        return {
            "ok": True,
            "confirmation_confidence": "active_title_strict",
            "exact": exact,
            "artifact_dir": artifact_dir,
            "conversation_type": "private",
            "conversation_type_evidence": {"short_code_confirmed": True},
        }

    try:
        sidecar.human_window_image_click_in_bounds = fake_window_image_click
        sidecar.human_client_click = fake_client_click
        sidecar.humanized_action_sleep = lambda *_args, **_kwargs: 0.0
        sidecar.validate_active_send_target = fake_validate
        result = sidecar.activate_search_result_candidate(
            1001,
            {
                "search_result_click_points": [[169, 170]],
                "center_y": 170,
                "left": 120,
                "right": 230,
            },
            remark_code="CJWIN01",
        )
    finally:
        sidecar.human_window_image_click_in_bounds = original_human_window_image_click_in_bounds
        sidecar.human_client_click = original_human_client_click
        sidecar.humanized_action_sleep = original_humanized_action_sleep
        sidecar.validate_active_send_target = original_validate_active_send_target

    assert_true(result.get("ok") is True, f"candidate activation should confirm in the fake validation path: {result}")
    assert_true(calls == [("window_image", 169, 170)], f"search result OCR point must use window-image click, calls={calls}")
    attempt = (result.get("attempts") or [{}])[0]
    assert_true(attempt.get("click_method") == "human_window_image_click", f"click method should be reported: {result}")


def test_search_contact_candidates_stop_before_favorites_section() -> None:
    def item(text: str, left: int, top: int, right: int, bottom: int) -> dict:
        return {
            "text": text,
            "left": left,
            "top": top,
            "right": right,
            "bottom": bottom,
            "center_x": int((left + right) / 2),
            "center_y": int((top + bottom) / 2),
            "confidence": 0.98,
        }

    ocr_items = [
        item("联系人", 106, 100, 156, 124),
        item("CJVOICE01", 162, 158, 246, 184),
        item("虾丸子大人", 252, 158, 360, 184),
        item("收藏", 106, 548, 154, 572),
        item("语音", 114, 594, 154, 622),
        item("来自:CJVOICE01 虾丸子大人", 162, 622, 360, 648),
        item("更多", 106, 720, 154, 744),
    ]

    matches = sidecar.search_result_contact_candidates_matching_remark_code(
        ocr_items, (980, 860), "CJVOICE01", layout_snapshot=real_layout_snapshot()
    )

    assert_true(len(matches) == 1, f"favorites section should not create extra contact candidates: {matches}")
    assert_true(matches[0].get("section") == "contacts", f"candidate should stay in contacts section: {matches}")
    assert_true("虾丸子大人" in str(matches[0].get("name") or ""), f"should keep real contact row: {matches}")


def test_search_result_does_not_fallback_without_remark_code_evidence() -> None:
    def item(text: str, left: int, top: int, right: int, bottom: int) -> dict:
        return {
            "text": text,
            "left": left,
            "top": top,
            "right": right,
            "bottom": bottom,
            "center_x": int((left + right) / 2),
            "center_y": int((top + bottom) / 2),
            "confidence": 0.81,
        }

    ocr_items = [
        item("联系人", 106, 100, 156, 124),
        item("虾丸子大人", 168, 158, 296, 184),
        item("群聊", 106, 232, 154, 256),
    ]
    candidate = sidecar.fallback_first_search_contact_candidate(
        ocr_items, (980, 860), "CJVOICE01", layout_snapshot=real_layout_snapshot()
    )
    assert_true(candidate is None, f"contact row without the target remark code must not be clicked: {candidate}")


def test_active_selected_session_can_confirm_clicked_chat_for_c2() -> None:
    ocr_items = [
        {"text": "CJVOICE01 虾丸子大人", "left": 148, "right": 336, "top": 120, "bottom": 148, "center_x": 242, "center_y": 134},
        {"text": "腾讯新闻", "left": 404, "right": 480, "top": 62, "bottom": 90, "center_x": 442, "center_y": 76},
    ]
    assert_true(
        sidecar.active_selected_session_matches(
            ocr_items,
            (980, 860),
            target="CJVOICE01",
            exact=False,
            layout_snapshot=real_layout_snapshot(),
        ),
        "left selected session row should be usable as C2 read confirmation",
    )


def test_search_by_remark_code_precheck_recovers_foreground_before_failing() -> None:
    original_recover_send_window_guard = sidecar.recover_send_window_guard
    original_basic_send_window_guard = sidecar.basic_send_window_guard
    calls: list[tuple[int, int]] = []

    def fake_recover(hwnd: int, *, max_attempts: int = 1) -> dict:
        calls.append((int(hwnd), int(max_attempts)))
        return {"ok": False, "reason": "foreground_not_wechat_target", "focus_recovery_attempts": max_attempts}

    def fake_basic(_hwnd: int) -> dict:
        raise AssertionError("search_by_remark_code should use recover_send_window_guard, not direct basic_send_window_guard")

    try:
        sidecar.recover_send_window_guard = fake_recover
        sidecar.basic_send_window_guard = fake_basic
        result = sidecar.open_chat_by_remark_code_search(1001, target="CJWIN01 陈志鹏", remark_code="CJWIN01")
    finally:
        sidecar.recover_send_window_guard = original_recover_send_window_guard
        sidecar.basic_send_window_guard = original_basic_send_window_guard

    assert_true(result.get("ok") is False, f"failed foreground recovery should stop before search: {result}")
    assert_true(result.get("reason") == "window_guard_failed_before_search", f"unexpected failure reason: {result}")
    assert_true(calls == [(1001, 2)], f"precheck should attempt foreground recovery twice before failing: {calls}")


def test_recover_send_window_guard_restores_minimized_geometry() -> None:
    original_basic_send_window_guard = sidecar.basic_send_window_guard
    original_activate_window = sidecar.activate_window
    calls: list[int] = []
    guards = [
        {
            "ok": False,
            "reason": "window_too_small_for_safe_send",
            "geometry": {"left": -32000, "top": -32000, "right": -31840, "bottom": -31966, "width": 160, "height": 34},
        },
        {"ok": True, "reason": "window_valid"},
    ]

    def fake_basic(_hwnd: int) -> dict:
        return guards.pop(0) if guards else {"ok": True, "reason": "window_valid"}

    try:
        sidecar.basic_send_window_guard = fake_basic
        sidecar.activate_window = lambda hwnd: calls.append(int(hwnd))
        result = sidecar.recover_send_window_guard(1001, max_attempts=2)
    finally:
        sidecar.basic_send_window_guard = original_basic_send_window_guard
        sidecar.activate_window = original_activate_window

    assert_true(result.get("ok") is True, f"minimized/offscreen geometry should be recoverable by activation: {result}")
    assert_true(result.get("focus_recovered") is True, f"recovery result should record focus_recovered: {result}")
    assert_true(result.get("focus_recovery_from") == "window_too_small_for_safe_send", f"recovery source mismatch: {result}")
    assert_true(calls == [1001], f"recover should activate the target hwnd once before retrying: {calls}")


def test_search_by_remark_code_precheck_does_not_bypass_failed_foreground_recovery() -> None:
    original_recover_send_window_guard = sidecar.recover_send_window_guard
    original_capture_wechat = sidecar.capture_wechat
    original_run_ocr_traced = sidecar.run_ocr_traced
    original_draw_add_friend_screen_annotation = sidecar.draw_add_friend_screen_annotation
    original_target_switch_surface_state = sidecar.target_switch_surface_state
    original_get_window_geometry = sidecar.get_window_geometry
    original_ensure_main_session_list = sidecar.ensure_main_session_list
    original_clear_sidebar_search_box_without_select_all = sidecar.clear_sidebar_search_box_without_select_all
    clear_called = False

    def fake_recover(_hwnd: int, *, max_attempts: int = 1) -> dict:
        return {
            "ok": False,
            "reason": "foreground_not_wechat_target",
            "focus_recovery_attempts": max_attempts,
            "foreground_window": {"title": "新通知", "class_name": "Windows.UI.Core.CoreWindow"},
        }

    def fake_capture(_hwnd: int, *, artifact_dir: str | None = None, label: str = "wechat") -> tuple:
        image = real_layout_frame()
        register_real_layout_frame(image, hwnd=int(_hwnd))
        path = Path(artifact_dir or ".") / f"{label}.png"
        image.save(path)
        return image, str(path)

    def fake_ocr(_image: object, _label: str, *, source: str = "") -> list[dict]:
        return [
            {"text": "搜索", "left": 112, "top": 60, "right": 170, "bottom": 84, "center_x": 141, "center_y": 72},
            {"text": "戴唯伟", "left": 406, "top": 61, "right": 462, "bottom": 82, "center_x": 434, "center_y": 71},
        ]

    def fake_draw(_screenshot: object, *, ocr_items: list[dict], targets: list[dict], output_path: Path, window_rect: list[int] | None = None) -> str:
        output_path.write_text("annotated", encoding="utf-8")
        return str(output_path)

    def fake_surface(*_args: object, **_kwargs: object) -> dict:
        return {"ok": True, "online": True, "reason": "surface_ready", "ocr_count": 2}

    def fake_geometry(_hwnd: int) -> dict:
        return {"left": 0, "top": 0, "right": 980, "bottom": 860, "width": 980, "height": 860}

    def fake_clear(*_args: object, **_kwargs: object) -> dict:
        nonlocal clear_called
        clear_called = True
        return {"ok": False, "reason": "search_clear_failed_for_test"}

    try:
        sidecar.recover_send_window_guard = fake_recover
        sidecar.capture_wechat = fake_capture
        sidecar.run_ocr_traced = fake_ocr
        sidecar.draw_add_friend_screen_annotation = fake_draw
        sidecar.target_switch_surface_state = fake_surface
        sidecar.get_window_geometry = fake_geometry
        sidecar.ensure_main_session_list = lambda *_args, **_kwargs: (real_layout_frame(), fake_ocr(None, "baseline"))
        sidecar.clear_sidebar_search_box_without_select_all = fake_clear
        with tempfile.TemporaryDirectory() as tmp:
            result = sidecar.open_chat_by_remark_code_search(
                1001,
                target="CJWIN01 陈志鹏",
                remark_code="CJWIN01",
                artifact_dir=tmp,
                sidecar_run_id="message-test-run-001",
            )
    finally:
        sidecar.recover_send_window_guard = original_recover_send_window_guard
        sidecar.capture_wechat = original_capture_wechat
        sidecar.run_ocr_traced = original_run_ocr_traced
        sidecar.draw_add_friend_screen_annotation = original_draw_add_friend_screen_annotation
        sidecar.target_switch_surface_state = original_target_switch_surface_state
        sidecar.get_window_geometry = original_get_window_geometry
        sidecar.ensure_main_session_list = original_ensure_main_session_list
        sidecar.clear_sidebar_search_box_without_select_all = original_clear_sidebar_search_box_without_select_all

    assert_true(result.get("reason") == "window_guard_failed_before_search", f"failed foreground recovery must stop before C2 keyboard actions: {result}")
    assert_true(clear_called is False, f"C2 must not continue to search clear when foreground recovery failed: {result}")
    precheck = next((item for item in result.get("step_events", []) if item.get("step") == "wechat_window_precheck"), {})
    guard = precheck.get("guard") if isinstance(precheck.get("guard"), dict) else {}
    assert_true(precheck.get("status") == "failed", f"precheck should fail when foreground recovery failed: {result}")
    assert_true(guard.get("ok") is False, f"failed recovery guard should remain failed: {result}")
    assert_true("foreground_guard_degraded" not in guard, f"C2 precheck should not bypass foreground recovery: {result}")


def test_search_clear_recovers_foreground_before_select_all() -> None:
    original_recover_send_window_guard = sidecar.recover_send_window_guard
    original_human_window_image_click_in_bounds = sidecar.human_window_image_click_in_bounds
    original_humanized_action_sleep = sidecar.humanized_action_sleep
    original_capture_wechat = sidecar.capture_wechat
    original_run_ocr_traced = sidecar.run_ocr_traced
    original_target_switch_surface_state = sidecar.target_switch_surface_state
    original_sidebar_search_state_detected = sidecar.sidebar_search_state_detected
    original_hotkey = sidecar.hotkey
    original_key_press = sidecar.key_press

    guards = [
        {"ok": True, "reason": "window_valid"},
        {
            "ok": True,
            "reason": "window_valid",
            "focus_recovered": True,
            "focus_recovery_from": "foreground_not_wechat_target",
            "focus_recovery_attempts": 1,
        },
    ]
    keys: list[str] = []

    def fake_recover(_hwnd: int, *, max_attempts: int = 1) -> dict:
        return guards.pop(0) if guards else {"ok": True, "reason": "window_valid"}

    def fake_capture(_hwnd: int, *, artifact_dir: str | None = None, label: str = "wechat") -> tuple:
        image = real_layout_frame()
        register_real_layout_frame(image, hwnd=int(_hwnd))
        path = Path(artifact_dir or ".") / f"{label}.png"
        image.save(path)
        return image, str(path)

    def fake_ocr(_image: object, _label: str, *, source: str = "") -> list[dict]:
        return [{"text": "搜索", "left": 128, "top": 59, "right": 166, "bottom": 80, "center_x": 147, "center_y": 70}]

    def fake_surface(*_args: object, **_kwargs: object) -> dict:
        return {"ok": True, "online": True, "reason": "surface_ready", "ocr_count": 1}

    def fake_search_state(*_args: object, **_kwargs: object) -> dict:
        return {"detected": True, "reason": "sidebar_search_focus_indicator"}

    try:
        sidecar.recover_send_window_guard = fake_recover
        sidecar.human_window_image_click_in_bounds = lambda *_args, **_kwargs: {"ok": True}
        sidecar.humanized_action_sleep = lambda *_args, **_kwargs: None
        sidecar.capture_wechat = fake_capture
        sidecar.run_ocr_traced = fake_ocr
        sidecar.target_switch_surface_state = fake_surface
        sidecar.sidebar_search_state_detected = fake_search_state
        sidecar.hotkey = lambda *_args, **_kwargs: keys.append("hotkey")
        sidecar.key_press = lambda *_args, **_kwargs: keys.append("backspace")
        with tempfile.TemporaryDirectory() as tmp:
            result = sidecar.clear_sidebar_search_box_without_select_all(
                1001,
                192,
                64,
                target_hint="CJWIN01",
                geometry={"left": 0, "top": 0, "right": 980, "bottom": 860, "width": 980, "height": 860},
                artifact_dir=tmp,
                recover_foreground=True,
            )
    finally:
        sidecar.recover_send_window_guard = original_recover_send_window_guard
        sidecar.human_window_image_click_in_bounds = original_human_window_image_click_in_bounds
        sidecar.humanized_action_sleep = original_humanized_action_sleep
        sidecar.capture_wechat = original_capture_wechat
        sidecar.run_ocr_traced = original_run_ocr_traced
        sidecar.target_switch_surface_state = original_target_switch_surface_state
        sidecar.sidebar_search_state_detected = original_sidebar_search_state_detected
        sidecar.hotkey = original_hotkey
        sidecar.key_press = original_key_press

    assert_true(result.get("ok") is True, f"C2 clear should continue after foreground recovery succeeds: {result}")
    guard = result.get("window_guard") if isinstance(result.get("window_guard"), dict) else {}
    assert_true(guard.get("focus_recovered") is True, f"clear result should report foreground recovery, not degradation: {result}")
    assert_true("foreground_guard_degraded" not in guard, f"clear should not bypass failed foreground recovery: {result}")
    assert_true(keys == ["hotkey", "backspace"], f"clear should proceed to select-all and backspace: {keys}, result={result}")


def test_search_clear_refocuses_empty_search_box_after_focus_drops_to_chat_input() -> None:
    original_recover_send_window_guard = sidecar.recover_send_window_guard
    original_human_window_image_click_in_bounds = sidecar.human_window_image_click_in_bounds
    original_humanized_action_sleep = sidecar.humanized_action_sleep
    original_capture_wechat = sidecar.capture_wechat
    original_run_ocr_traced = sidecar.run_ocr_traced
    original_target_switch_surface_state = sidecar.target_switch_surface_state
    original_sidebar_search_state_detected = sidecar.sidebar_search_state_detected
    original_hotkey = sidecar.hotkey
    original_key_press = sidecar.key_press

    search_states = [
        {"detected": True, "reason": "sidebar_search_focus_indicator"},
        {"detected": False, "reason": "chat_input_focused_after_clear"},
        {"detected": True, "reason": "sidebar_search_focus_indicator"},
    ]
    clicks: list[tuple[str, int, str]] = []

    def fake_capture(_hwnd: int, *, artifact_dir: str | None = None, label: str = "wechat") -> tuple:
        image = real_layout_frame()
        register_real_layout_frame(image, hwnd=int(_hwnd))
        path = Path(artifact_dir or ".") / f"{label}.png"
        image.save(path)
        return image, str(path)

    def fake_ocr(_image: object, _label: str, *, source: str = "") -> list[dict]:
        left = 208 if "cleared" in _label else 128
        return [{
            "text": "搜索",
            "left": left,
            "top": 59,
            "right": left + 38,
            "bottom": 80,
            "center_x": left + 19,
            "center_y": 70,
        }]

    def fake_surface(*_args: object, **_kwargs: object) -> dict:
        return {"ok": True, "online": True, "reason": "surface_ready", "ocr_count": 1}

    def fake_search_state(*_args: object, **_kwargs: object) -> dict:
        return search_states.pop(0) if search_states else {"detected": True, "reason": "sidebar_search_focus_indicator"}

    def fake_click(*_args: object, **kwargs: object) -> dict:
        clicks.append((
            str(kwargs.get("action_name") or "click"),
            int(_args[1]),
            str(kwargs.get("expected_snapshot_id") or ""),
        ))
        return {"ok": True}

    try:
        sidecar.recover_send_window_guard = lambda *_args, **_kwargs: {"ok": True, "reason": "window_valid"}
        sidecar.human_window_image_click_in_bounds = fake_click
        sidecar.humanized_action_sleep = lambda *_args, **_kwargs: None
        sidecar.capture_wechat = fake_capture
        sidecar.run_ocr_traced = fake_ocr
        sidecar.target_switch_surface_state = fake_surface
        sidecar.sidebar_search_state_detected = fake_search_state
        sidecar.hotkey = lambda *_args, **_kwargs: None
        sidecar.key_press = lambda *_args, **_kwargs: None
        with tempfile.TemporaryDirectory() as tmp:
            result = sidecar.clear_sidebar_search_box_without_select_all(
                1001,
                192,
                64,
                target_hint="CJWIN01",
                geometry={"left": 0, "top": 0, "right": 980, "bottom": 860, "width": 980, "height": 860},
                artifact_dir=tmp,
                recover_foreground=True,
            )
    finally:
        sidecar.recover_send_window_guard = original_recover_send_window_guard
        sidecar.human_window_image_click_in_bounds = original_human_window_image_click_in_bounds
        sidecar.humanized_action_sleep = original_humanized_action_sleep
        sidecar.capture_wechat = original_capture_wechat
        sidecar.run_ocr_traced = original_run_ocr_traced
        sidecar.target_switch_surface_state = original_target_switch_surface_state
        sidecar.sidebar_search_state_detected = original_sidebar_search_state_detected
        sidecar.hotkey = original_hotkey
        sidecar.key_press = original_key_press

    assert_true(result.get("ok") is True, f"empty search box should be refocused instead of failing: {result}")
    assert_true(result.get("refocused_after_clear") is True, f"clear result should report refocus: {result}")
    assert_true(
        [item[0] for item in clicks] == ["sidebar_search_box_click", "sidebar_search_box_refocus_after_clear"],
        f"search box should be clicked once to focus and once to refocus: {clicks}, result={result}",
    )
    assert_true(clicks[0][1] != clicks[1][1], f"refocus must reacquire the moved search box from the new frame: {clicks}")
    assert_true(clicks[0][2] != clicks[1][2], f"refocus must use the new frame snapshot id: {clicks}")


def test_search_by_remark_code_failed_precheck_writes_window_evidence() -> None:
    original_recover_send_window_guard = sidecar.recover_send_window_guard
    original_capture_wechat = sidecar.capture_wechat
    original_run_ocr_traced = sidecar.run_ocr_traced
    original_draw_add_friend_screen_annotation = sidecar.draw_add_friend_screen_annotation
    original_target_switch_surface_state = sidecar.target_switch_surface_state
    original_get_window_geometry = sidecar.get_window_geometry

    def fake_recover(_hwnd: int, *, max_attempts: int = 1) -> dict:
        return {"ok": False, "reason": "foreground_not_wechat_target", "focus_recovery_attempts": max_attempts}

    def fake_capture(_hwnd: int, *, artifact_dir: str | None = None, label: str = "wechat") -> tuple:
        image = sidecar.Image.new("RGB", (240, 120), "white")
        path = Path(artifact_dir or ".") / f"{label}.png"
        image.save(path)
        return image, str(path)

    def fake_ocr(_image: object, _label: str, *, source: str = "") -> list[dict]:
        return [{"text": "微信", "left": 20, "top": 20, "right": 60, "bottom": 40, "center_x": 40, "center_y": 30}]

    def fake_draw(_screenshot: object, *, ocr_items: list[dict], targets: list[dict], output_path: Path, window_rect: list[int] | None = None) -> str:
        output_path.write_text("annotated", encoding="utf-8")
        return str(output_path)

    def fake_surface(*_args: object, **_kwargs: object) -> dict:
        return {"ok": False, "reason": "blank_render", "ocr_count": 1}

    def fake_geometry(_hwnd: int) -> dict:
        return {"left": 0, "top": 0, "right": 240, "bottom": 120, "width": 240, "height": 120}

    try:
        sidecar.recover_send_window_guard = fake_recover
        sidecar.capture_wechat = fake_capture
        sidecar.run_ocr_traced = fake_ocr
        sidecar.draw_add_friend_screen_annotation = fake_draw
        sidecar.target_switch_surface_state = fake_surface
        sidecar.get_window_geometry = fake_geometry
        with tempfile.TemporaryDirectory() as tmp:
            result = sidecar.open_chat_by_remark_code_search(
                1001,
                target="CJWIN01 陈志鹏",
                remark_code="CJWIN01",
                artifact_dir=tmp,
            )
            report = Path(str(result.get("review_path") or ""))
            raw = Path(tmp) / "messages_window_precheck_failed.png"
            annotated = Path(tmp) / "messages_window_precheck_failed_annotated.png"
            assert_true(report.exists(), f"failed precheck should still write review report: {result}")
            assert_true(raw.exists(), f"failed precheck should save raw screenshot evidence: {result}")
            assert_true(annotated.exists(), f"failed precheck should save annotated screenshot evidence: {result}")
    finally:
        sidecar.recover_send_window_guard = original_recover_send_window_guard
        sidecar.capture_wechat = original_capture_wechat
        sidecar.run_ocr_traced = original_run_ocr_traced
        sidecar.draw_add_friend_screen_annotation = original_draw_add_friend_screen_annotation
        sidecar.target_switch_surface_state = original_target_switch_surface_state
        sidecar.get_window_geometry = original_get_window_geometry


def test_search_by_remark_code_writes_partial_report_before_mid_step_crash() -> None:
    original_recover_send_window_guard = sidecar.recover_send_window_guard
    original_ensure_main_session_list = sidecar.ensure_main_session_list
    original_get_window_geometry = sidecar.get_window_geometry
    original_save_screenshot_artifact = sidecar.save_screenshot_artifact
    original_draw_add_friend_screen_annotation = sidecar.draw_add_friend_screen_annotation
    original_clear_sidebar_search_box_without_select_all = sidecar.clear_sidebar_search_box_without_select_all

    def fake_geometry(_hwnd: int) -> dict:
        return {"left": 0, "top": 0, "right": 980, "bottom": 860, "width": 980, "height": 860}

    def fake_ensure_main_session_list(*_args: object, **_kwargs: object) -> tuple:
        image = real_layout_frame()
        register_real_layout_frame(image, hwnd=1001)
        items = [{"text": "搜索", "left": 128, "top": 59, "right": 166, "bottom": 80, "center_x": 147, "center_y": 70}]
        return image, items

    def fake_save(image: object, *, artifact_dir: str | None = None, label: str = "wechat") -> str:
        path = Path(artifact_dir or ".") / f"{label}.png"
        image.save(path)
        return str(path)

    def fake_draw(_screenshot: object, *, ocr_items: list[dict], targets: list[dict], output_path: Path, window_rect: list[int] | None = None) -> str:
        output_path.write_text("annotated", encoding="utf-8")
        return str(output_path)

    def fake_clear(*_args: object, **_kwargs: object) -> dict:
        raise RuntimeError("simulated_mid_step_crash")

    try:
        sidecar.recover_send_window_guard = lambda *_args, **_kwargs: {"ok": True, "reason": "foreground_matches_target"}
        sidecar.ensure_main_session_list = fake_ensure_main_session_list
        sidecar.get_window_geometry = fake_geometry
        sidecar.save_screenshot_artifact = fake_save
        sidecar.draw_add_friend_screen_annotation = fake_draw
        sidecar.clear_sidebar_search_box_without_select_all = fake_clear
        with tempfile.TemporaryDirectory() as tmp:
            try:
                sidecar.open_chat_by_remark_code_search(
                    1001,
                    target="CJWIN01 陈志鹏",
                    remark_code="CJWIN01",
                    artifact_dir=tmp,
                    sidecar_run_id="message-test-run-001",
                )
            except RuntimeError as exc:
                assert_true("simulated_mid_step_crash" in str(exc), f"unexpected crash: {exc!r}")
            report = Path(tmp) / "wechat_messages_targeting_review.json"
            html = Path(tmp) / "wechat_messages_targeting_review.html"
            assert_true(report.exists(), "mid-step crash should still leave a json targeting report")
            assert_true(html.exists(), "mid-step crash should still leave an html targeting report")
            data = json.loads(report.read_text(encoding="utf-8"))
            summary = data.get("summary") if isinstance(data.get("summary"), dict) else {}
            assert_true(summary.get("partial") is True, f"partial report should be marked partial=true: {summary}")
            assert_true(summary.get("sidecar_run_id") == "message-test-run-001", f"partial report should keep sidecar_run_id: {summary}")
            assert_true(str(summary.get("reason") or "").startswith("partial_after_"), f"partial report should name last completed step: {summary}")
            events = data.get("events") if isinstance(data.get("events"), list) else []
            assert_true(
                any(((event.get("result") or {}).get("sidecar_run_id") == "message-test-run-001") for event in events if isinstance(event, dict)),
                f"report rows should keep sidecar_run_id in step results: {events}",
            )
    finally:
        sidecar.recover_send_window_guard = original_recover_send_window_guard
        sidecar.ensure_main_session_list = original_ensure_main_session_list
        sidecar.get_window_geometry = original_get_window_geometry
        sidecar.save_screenshot_artifact = original_save_screenshot_artifact
        sidecar.draw_add_friend_screen_annotation = original_draw_add_friend_screen_annotation
        sidecar.clear_sidebar_search_box_without_select_all = original_clear_sidebar_search_box_without_select_all


def test_search_by_remark_code_captures_visible_screen_for_search_overlay() -> None:
    source = Path(sidecar.__file__).read_text(encoding="utf-8")
    start = source.index("def open_chat_by_remark_code_search(")
    end = source.index("\ndef open_chat(", start)
    body = source[start:end]
    assert_true(
        'label="messages_search_by_remark_code_results")' in body
        and "capture_wechat_window_visible_screen(" in body,
        "C2 search results must use visible-screen capture so WeChat search overlays appear in reports.",
    )
    assert_true(
        'capture_wechat(hwnd, artifact_dir=artifact_dir, label="messages_search_by_remark_code_results")' not in body,
        "C2 search results must not use PrintWindow-style main-window capture for overlay screenshots.",
    )
    assert_true(
        "messages_search_by_remark_code_results_after_nudge" in body
        and "ocr_search_candidates_after_nudge" in body,
        "C2 search should recapture visible search results after nudging the query when no candidates are found.",
    )


def main() -> int:
    tests = [
        test_window_action_planning_module_exports_expected_helpers,
        test_plan_disabled_matches_sidecar_disabled_shape,
        test_plan_1920x1200_fixed_origin_matches_default_safe_window,
        test_plan_1920x1080_promotes_observed_narrow_window_to_standard_size,
        test_plan_high_resolution_keeps_normal_window_size,
        test_plan_1920_class_displays_do_not_multiply_window_by_dpi,
        test_plan_resolution_dpi_matrix_stays_visible_and_safe,
        test_plan_huge_requested_window_clamps_to_safe_maximum,
        test_plan_tiny_screen_never_exceeds_visible_screen_bounds,
        test_plan_small_screen_clamps_size_to_visible_screen,
        test_plan_non_fixed_origin_clamps_existing_origin,
        test_plan_recommended_floor_and_custom_origin,
        test_plan_without_screen_metrics_uses_target_and_max_bounds,
        test_add_friend_layout_finalization_requires_only_dynamic_search_row,
        test_empty_ocr_region_reports_layout_error_instead_of_value_error,
        test_sidecar_normalize_wechat_window_uses_same_planned_move_shape,
        test_verify_policy_refuses_a_required_move_without_touching_the_window,
        test_sidecar_promotes_current_window_to_standard_size_when_unconfigured,
        test_startup_normalization_defers_full_layout_to_business_action,
        test_normalization_rejects_dpi_change_after_geometry_check,
        test_sidebar_search_query_must_match_exact_remark_code,
        test_sidebar_search_query_ignores_empty_placeholder_icon_text,
        test_search_result_candidate_uses_window_image_click_coordinates,
        test_search_contact_candidates_stop_before_favorites_section,
        test_search_result_does_not_fallback_without_remark_code_evidence,
        test_active_selected_session_can_confirm_clicked_chat_for_c2,
        test_search_by_remark_code_precheck_recovers_foreground_before_failing,
        test_recover_send_window_guard_restores_minimized_geometry,
        test_search_by_remark_code_precheck_does_not_bypass_failed_foreground_recovery,
        test_search_clear_recovers_foreground_before_select_all,
        test_search_clear_refocuses_empty_search_box_after_focus_drops_to_chat_input,
        test_search_by_remark_code_failed_precheck_writes_window_evidence,
        test_search_by_remark_code_writes_partial_report_before_mid_step_crash,
        test_search_by_remark_code_captures_visible_screen_for_search_overlay,
    ]
    passed = 0
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
        passed += 1
    print(f"All {passed} WeChat Win32/OCR window action planning checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
