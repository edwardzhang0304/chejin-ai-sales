"""Contract checks for generic Win32/OCR geometry helpers."""

from __future__ import annotations

from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from apps.wechat_ai_customer_service.adapters import wechat_win32_ocr_sidecar as sidecar  # noqa: E402
from apps.wechat_ai_customer_service.adapters.wechat_win32_ocr import geometry  # noqa: E402


def assert_true(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def test_geometry_module_exports_only_generic_helpers() -> None:
    expected = {
        "bounded_int",
        "bounded_float",
        "center_of_bounds",
        "point_in_bounds",
        "clamp_point_to_bounds",
        "rect_overlaps_region",
        "relative_rect",
        "validate_send_geometry",
        "validate_capture_geometry",
    }
    missing = sorted(name for name in expected if not callable(getattr(geometry, name, None)))
    assert_true(not missing, f"generic geometry helpers missing: {missing}")


def test_scalar_geometry_helpers_match_sidecar() -> None:
    for value in ("42", "bad", None, -999, 999):
        assert_true(
            geometry.bounded_int(value, default=7, minimum=1, maximum=50)
            == sidecar.bounded_int(value, default=7, minimum=1, maximum=50),
            f"bounded_int mismatch for {value!r}",
        )
        assert_true(
            geometry.bounded_float(value, default=7.5, minimum=1.25, maximum=50.5)
            == sidecar.bounded_float(value, default=7.5, minimum=1.25, maximum=50.5),
            f"bounded_float mismatch for {value!r}",
        )
    for bounds in ([1, 2, 11, 22], [11, 22], [-10, -20, 10, 20]):
        assert_true(geometry.center_of_bounds(bounds) == sidecar.center_of_bounds(bounds), f"center mismatch: {bounds}")
    for x, y, bounds in (
        (5, 5, [0, 0, 10, 10]),
        (-5, 3, [0, 0, 10, 10]),
        (12, 50, [20, 10, 0, 30]),
    ):
        assert_true(
            geometry.point_in_bounds(x, y, bounds) == sidecar.point_in_bounds(x, y, bounds),
            f"point bounds mismatch: {(x, y, bounds)}",
        )
        assert_true(
            geometry.clamp_point_to_bounds(x, y, bounds) == sidecar.clamp_point_to_bounds(x, y, bounds),
            f"clamp mismatch: {(x, y, bounds)}",
        )


def test_rect_helpers_are_coordinate_system_agnostic() -> None:
    geometries = (
        {"left": 0, "top": 0, "width": 981, "height": 860},
        {"left": 120, "top": 80, "width": 1920, "height": 1200},
        {"left": -480, "top": 40, "width": 2560, "height": 1440},
    )
    screen_rect = {"left": 345, "top": 690, "right": 770, "bottom": 804}
    bounds = (320, 600, 900, 850)
    for current in geometries:
        assert_true(
            geometry.relative_rect(screen_rect, current) == sidecar.relative_rect(screen_rect, current),
            f"relative rect mismatch: {(screen_rect, current)}",
        )
        assert_true(
            geometry.rect_overlaps_region(screen_rect, bounds) == sidecar.rect_overlaps_region(screen_rect, bounds),
            f"overlap mismatch: {(screen_rect, bounds)}",
        )


def test_window_safety_validation_has_no_click_planning() -> None:
    valid_cases = (
        {"left": 0, "top": 0, "width": 981, "height": 860},
        {"left": 120, "top": 80, "width": 1920, "height": 1200},
        {"left": -480, "top": 40, "width": 3840, "height": 2160},
    )
    for current in valid_cases:
        assert_true(geometry.validate_capture_geometry(current).get("ok") is True, f"capture rejected: {current}")
        assert_true(geometry.validate_send_geometry(current).get("ok") is True, f"send rejected: {current}")

    too_small = {"left": 0, "top": 0, "width": 640, "height": 600}
    offscreen = {"left": -32000, "top": -32000, "width": 981, "height": 860}
    assert_true(geometry.validate_send_geometry(too_small).get("reason") == "window_too_small_for_safe_send", "small send geometry must fail closed")
    assert_true(geometry.validate_capture_geometry(offscreen).get("reason") == "window_offscreen_or_minimized", "offscreen capture must fail closed")
    for result in (
        geometry.validate_capture_geometry(valid_cases[0]),
        geometry.validate_send_geometry(valid_cases[0]),
    ):
        assert_true("input_point" not in result and "send_point" not in result, f"geometry validation planned a click: {result}")


def main() -> int:
    tests = [
        test_geometry_module_exports_only_generic_helpers,
        test_scalar_geometry_helpers_match_sidecar,
        test_rect_helpers_are_coordinate_system_agnostic,
        test_window_safety_validation_has_no_click_planning,
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"All {len(tests)} WeChat Win32/OCR generic geometry checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
