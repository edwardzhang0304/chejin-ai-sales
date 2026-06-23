"""Contract checks for pure Win32/OCR window action planning."""

from __future__ import annotations

import os
from pathlib import Path
import sys
import types


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from apps.wechat_ai_customer_service.adapters import wechat_win32_ocr_sidecar as sidecar  # noqa: E402
from apps.wechat_ai_customer_service.adapters.wechat_win32_ocr import window_action_planning  # noqa: E402


def assert_true(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def plan(
    before: dict[str, int],
    *,
    enabled: bool = True,
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


def test_plan_1920x1080_keeps_default_safe_window_when_it_fits() -> None:
    before = {"left": 10, "top": 12, "width": 900, "height": 800}
    result = plan(before, screen_width=1920, screen_height=1080)
    assert_true((result.get("left"), result.get("top"), result.get("width"), result.get("height")) == (0, 0, 980, 860), f"1080p target mismatch: {result}")


def test_plan_small_screen_clamps_size_to_visible_screen() -> None:
    before = {"left": 0, "top": 0, "width": 980, "height": 860}
    result = plan(before, screen_width=900, screen_height=760)
    assert_true((result.get("left"), result.get("top"), result.get("width"), result.get("height")) == (0, 0, 888, 702), f"small screen target mismatch: {result}")


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
            "WECHAT_WIN32_OCR_WINDOW_NORMALIZE",
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
        planned = plan(dict(geometry_state), screen_width=1920, screen_height=1200)
        result = sidecar.normalize_wechat_window(1001)
        expected_move = (planned["left"], planned["top"], planned["width"], planned["height"])
        assert_true(calls == [expected_move], f"sidecar should execute planner move: calls={calls}, planned={planned}")
        assert_true(result.get("target") == planned.get("target"), f"target metadata mismatch: {result} vs {planned}")
        assert_true(result.get("screen") == planned.get("screen"), f"screen metadata mismatch: {result} vs {planned}")
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


def main() -> int:
    tests = [
        test_window_action_planning_module_exports_expected_helpers,
        test_plan_disabled_matches_sidecar_disabled_shape,
        test_plan_1920x1200_fixed_origin_matches_default_safe_window,
        test_plan_1920x1080_keeps_default_safe_window_when_it_fits,
        test_plan_small_screen_clamps_size_to_visible_screen,
        test_plan_non_fixed_origin_clamps_existing_origin,
        test_plan_recommended_floor_and_custom_origin,
        test_plan_without_screen_metrics_uses_target_and_max_bounds,
        test_sidecar_normalize_wechat_window_uses_same_planned_move_shape,
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
