"""Contract checks for pure Win32/OCR window action planning."""

from __future__ import annotations

import os
import json
from pathlib import Path
import sys
import tempfile
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


def test_plan_1920x1080_keeps_default_safe_window_when_it_fits() -> None:
    before = {"left": 10, "top": 12, "width": 900, "height": 800}
    result = plan(before, screen_width=1920, screen_height=1080)
    assert_true((result.get("left"), result.get("top"), result.get("width"), result.get("height")) == (0, 0, 980, 860), f"1080p target mismatch: {result}")


def test_plan_high_resolution_scales_recommended_window_when_screen_allows() -> None:
    before = {"left": 20, "top": 24, "width": 980, "height": 860}
    result = plan(before, dpi_scale=1.5, screen_width=3840, screen_height=2160)
    assert_true(
        (result.get("left"), result.get("top"), result.get("width"), result.get("height")) == (0, 0, 1470, 1290),
        f"high resolution target should scale recommended geometry: {result}",
    )
    assert_true(result.get("target") == {"width": 1470, "height": 1290}, f"high resolution target metadata mismatch: {result}")
    assert_true(result.get("resolution_scale") == 1.5, f"resolution scale metadata mismatch: {result}")


def test_plan_1920_class_displays_ignore_dpi_scale_for_default_window() -> None:
    cases = [
        ("1920x1080@100", 1920, 1080, 1.0, (0, 0, 980, 860)),
        ("1920x1080@125-logical", 1536, 864, 1.25, (0, 0, 980, 816)),
        ("local-1920x1200@125-logical", 1536, 960, 1.25, (0, 0, 980, 860)),
        ("1920x1080@150-logical", 1280, 720, 1.5, (0, 0, 980, 672)),
    ]
    for label, screen_width, screen_height, dpi_scale, expected_rect in cases:
        result = plan(
            {"left": 20, "top": 24, "width": 900, "height": 800},
            dpi_scale=dpi_scale,
            screen_width=screen_width,
            screen_height=screen_height,
        )
        assert_true(
            (result.get("left"), result.get("top"), result.get("width"), result.get("height")) == expected_rect,
            f"{label} should keep the 980x860 default class and only clamp to visible bounds: {result}",
        )
        assert_true(result.get("target") == {"width": 980, "height": 860}, f"{label} target metadata mismatch: {result}")
        assert_true(result.get("resolution_scale") == 1.0, f"{label} should not scale by DPI: {result}")
        assert_true(result.get("dpi_scale") == dpi_scale, f"{label} should still report DPI for diagnostics: {result}")


def test_plan_resolution_dpi_matrix_stays_visible_and_safe() -> None:
    cases = [
        ("1366x768@100", 1366, 768, 1.0, (0, 0, 980, 720), {"width": 980, "height": 860}),
        ("1440x900@100", 1440, 900, 1.0, (0, 0, 980, 852), {"width": 980, "height": 860}),
        ("1536x864@125", 1536, 864, 1.25, (0, 0, 980, 816), {"width": 980, "height": 860}),
        ("1920x1080@100", 1920, 1080, 1.0, (0, 0, 980, 860), {"width": 980, "height": 860}),
        ("1920x1080@125", 1920, 1080, 1.25, (0, 0, 980, 860), {"width": 980, "height": 860}),
        ("1920x1080@150", 1920, 1080, 1.5, (0, 0, 980, 860), {"width": 980, "height": 860}),
        ("2560x1440@100", 2560, 1440, 1.0, (0, 0, 1225, 1075), {"width": 1225, "height": 1075}),
        ("3840x2160@150", 3840, 2160, 1.5, (0, 0, 1470, 1290), {"width": 1470, "height": 1290}),
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
    assert_true((result.get("left"), result.get("top"), result.get("width"), result.get("height")) == (0, 0, 500, 420), f"tiny screen should clamp to visible bounds: {result}")


def test_plan_small_screen_clamps_size_to_visible_screen() -> None:
    before = {"left": 0, "top": 0, "width": 980, "height": 860}
    result = plan(before, screen_width=900, screen_height=760)
    assert_true((result.get("left"), result.get("top"), result.get("width"), result.get("height")) == (0, 0, 888, 712), f"small screen target mismatch: {result}")


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
    original_human_window_image_click = sidecar.human_window_image_click
    original_human_client_click = sidecar.human_client_click
    original_humanized_action_sleep = sidecar.humanized_action_sleep
    original_validate_active_send_target = sidecar.validate_active_send_target
    calls: list[tuple[str, int, int]] = []

    def fake_window_image_click(_hwnd: int, x: int, y: int) -> None:
        calls.append(("window_image", int(x), int(y)))

    def fake_client_click(_hwnd: int, x: int, y: int) -> None:
        calls.append(("client", int(x), int(y)))

    def fake_validate(_hwnd: int, _target: str, *, exact: bool, artifact_dir: str | None = None) -> dict:
        return {"ok": True, "confirmation_confidence": "active_title_strict", "exact": exact, "artifact_dir": artifact_dir}

    try:
        sidecar.human_window_image_click = fake_window_image_click
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
        sidecar.human_window_image_click = original_human_window_image_click
        sidecar.human_client_click = original_human_client_click
        sidecar.humanized_action_sleep = original_humanized_action_sleep
        sidecar.validate_active_send_target = original_validate_active_send_target

    assert_true(result.get("ok") is True, f"candidate activation should confirm in the fake validation path: {result}")
    assert_true(calls == [("window_image", 169, 170)], f"search result OCR point must use window-image click, calls={calls}")
    attempt = (result.get("attempts") or [{}])[0]
    assert_true(attempt.get("click_method") == "human_window_image_click", f"click method should be reported: {result}")


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
        image = sidecar.Image.new("RGB", (980, 860), "white")
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
        sidecar.ensure_main_session_list = lambda *_args, **_kwargs: (sidecar.Image.new("RGB", (980, 860), "white"), fake_ocr(None, "baseline"))
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
    assert_true("operator_guard" not in result, f"C2 read-only targeting should not report floating operator guard: {result}")
    assert_true(not any(item.get("step") == "operator_guard" for item in result.get("step_events", [])), f"C2 report should not include operator guard steps: {result}")
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
        image = sidecar.Image.new("RGB", (980, 860), "white")
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
    clicks: list[str] = []

    def fake_capture(_hwnd: int, *, artifact_dir: str | None = None, label: str = "wechat") -> tuple:
        image = sidecar.Image.new("RGB", (980, 860), "white")
        path = Path(artifact_dir or ".") / f"{label}.png"
        image.save(path)
        return image, str(path)

    def fake_ocr(_image: object, _label: str, *, source: str = "") -> list[dict]:
        return [{"text": "搜索", "left": 128, "top": 59, "right": 166, "bottom": 80, "center_x": 147, "center_y": 70}]

    def fake_surface(*_args: object, **_kwargs: object) -> dict:
        return {"ok": True, "online": True, "reason": "surface_ready", "ocr_count": 1}

    def fake_search_state(*_args: object, **_kwargs: object) -> dict:
        return search_states.pop(0) if search_states else {"detected": True, "reason": "sidebar_search_focus_indicator"}

    def fake_click(*_args: object, **kwargs: object) -> dict:
        clicks.append(str(kwargs.get("action_name") or "click"))
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
        clicks == ["sidebar_search_box_click", "sidebar_search_box_refocus_after_clear"],
        f"search box should be clicked once to focus and once to refocus: {clicks}, result={result}",
    )


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
        image = sidecar.Image.new("RGB", (980, 860), "white")
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
        test_plan_1920x1080_keeps_default_safe_window_when_it_fits,
        test_plan_high_resolution_scales_recommended_window_when_screen_allows,
        test_plan_1920_class_displays_ignore_dpi_scale_for_default_window,
        test_plan_resolution_dpi_matrix_stays_visible_and_safe,
        test_plan_huge_requested_window_clamps_to_safe_maximum,
        test_plan_tiny_screen_never_exceeds_visible_screen_bounds,
        test_plan_small_screen_clamps_size_to_visible_screen,
        test_plan_non_fixed_origin_clamps_existing_origin,
        test_plan_recommended_floor_and_custom_origin,
        test_plan_without_screen_metrics_uses_target_and_max_bounds,
        test_sidecar_normalize_wechat_window_uses_same_planned_move_shape,
        test_sidebar_search_query_must_match_exact_remark_code,
        test_sidebar_search_query_ignores_empty_placeholder_icon_text,
        test_search_result_candidate_uses_window_image_click_coordinates,
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
