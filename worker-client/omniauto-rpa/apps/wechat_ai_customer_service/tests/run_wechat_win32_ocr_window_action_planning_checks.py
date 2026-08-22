"""v0.9.29 checks for the pure startup window planner."""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from apps.wechat_ai_customer_service.adapters.wechat_win32_ocr import window_action_planning


def assert_true(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def plan(*, dpi: float, work_area: dict[str, int], before: dict[str, int] | None = None) -> dict:
    return window_action_planning.plan_normalize_wechat_window(
        dict(before or {}),
        dpi_scale=dpi,
        work_area=work_area,
    )


def test_dpi_profiles_and_secondary_monitor_origin() -> None:
    work = {"left": 1920, "top": -80, "width": 2560, "height": 1440}
    cases = [
        (1.0, (1932, -68, 800, 852)),
        (1.25, (1935, -65, 1000, 1065)),
        (1.5, (1938, -62, 1200, 1278)),
    ]
    for dpi, expected in cases:
        result = plan(dpi=dpi, work_area=work)
        actual = (result.get("left"), result.get("top"), result.get("width"), result.get("height"))
        assert_true(result.get("ok") is True and actual == expected, f"{dpi=} profile mismatch: {result}")


def test_other_dpi_is_scaled_from_800x852() -> None:
    result = plan(dpi=1.75, work_area={"left": 0, "top": 0, "width": 3000, "height": 2000})
    assert_true(result.get("requested_target") == {"width": 1400, "height": 1491}, f"scaled profile mismatch: {result}")
    assert_true((result.get("left"), result.get("top")) == (21, 21), f"DPI margin mismatch: {result}")


def test_work_area_fit_preserves_aspect_ratio_and_minimum() -> None:
    result = plan(dpi=1.5, work_area={"left": 0, "top": 0, "width": 1000, "height": 1000})
    assert_true(result.get("ok") is True, f"fit profile rejected: {result}")
    requested = result["requested_target"]
    width, height = int(result["width"]), int(result["height"])
    margin = round(12 * 1.5)
    assert_true(width <= 1000 - 2 * margin and height <= 1000 - 2 * margin, f"fit escaped work area: {result}")
    assert_true(result["right"] <= 1000 - margin and result["bottom"] <= 1000 - margin, f"safe margin missing: {result}")
    ratio_error = abs((width / height) - (requested["width"] / requested["height"]))
    assert_true(ratio_error < 0.002, f"fit changed aspect ratio: {result}")


def test_work_area_below_minimum_fails_without_move() -> None:
    result = plan(dpi=1.0, work_area={"left": 0, "top": 0, "width": 690, "height": 710})
    assert_true(result.get("ok") is False and result.get("move") is False, f"undersized work area must fail closed: {result}")


def test_already_at_profile_does_not_move() -> None:
    result = plan(
        dpi=1.0,
        work_area={"left": 100, "top": 200, "width": 1920, "height": 1080},
        before={"left": 112, "top": 212, "width": 800, "height": 852},
    )
    assert_true(result.get("ok") is True and result.get("move") is False, f"stable startup profile should not move: {result}")


def test_taskbar_work_area_and_negative_secondary_coordinates_keep_four_margins() -> None:
    work = {"left": -1600, "top": 35, "width": 1200, "height": 965}
    result = plan(dpi=1.25, work_area=work)
    margin = round(12 * 1.25)
    assert_true(result.get("ok") is True, f"negative secondary monitor rejected: {result}")
    assert_true((result["left"], result["top"]) == (-1600 + margin, 35 + margin), str(result))
    assert_true(result["right"] <= -400 - margin, f"right margin missing: {result}")
    assert_true(result["bottom"] <= 1000 - margin, f"taskbar/bottom margin missing: {result}")


def main() -> int:
    tests = [
        test_dpi_profiles_and_secondary_monitor_origin,
        test_other_dpi_is_scaled_from_800x852,
        test_work_area_fit_preserves_aspect_ratio_and_minimum,
        test_work_area_below_minimum_fails_without_move,
        test_already_at_profile_does_not_move,
        test_taskbar_work_area_and_negative_secondary_coordinates_keep_four_margins,
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"All {len(tests)} v0.9.29 window planner checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
