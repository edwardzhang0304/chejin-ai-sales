"""Contract checks for pure Win32/OCR geometry extraction."""

from __future__ import annotations

import random
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


GEOMETRIES = [
    {"left": 0, "top": 0, "width": 981, "height": 860},
    {"left": 12, "top": 34, "width": 980, "height": 860},
    {"left": 0, "top": 0, "width": 1280, "height": 900},
    {"left": 0, "top": 0, "width": 1920, "height": 1200},
    {"left": -32000, "top": -32000, "width": 199, "height": 34},
]


def test_geometry_module_exports_expected_helpers() -> None:
    for name in (
        "bounded_int",
        "bounded_float",
        "center_of_bounds",
        "point_in_bounds",
        "clamp_point_to_bounds",
        "session_split_x",
        "chat_header_cutoff_y",
        "active_chat_title_cutoff_y",
        "active_chat_title_left_x",
        "search_box_point_for_geometry",
        "session_click_x_for_geometry",
        "input_text_region_bounds",
        "rect_in_input_area",
        "rect_in_input_toolbar",
        "calculate_send_points",
        "input_click_candidate_points",
        "send_click_candidate_points",
    ):
        assert_true(callable(getattr(geometry, name, None)), f"geometry helper missing: {name}")


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
    for x, y, bounds in ((5, 5, [0, 0, 10, 10]), (-5, 3, [0, 0, 10, 10]), (12, 50, [20, 10, 0, 30])):
        assert_true(geometry.point_in_bounds(x, y, bounds) == sidecar.point_in_bounds(x, y, bounds), f"point bounds mismatch: {(x, y, bounds)}")
        assert_true(geometry.clamp_point_to_bounds(x, y, bounds) == sidecar.clamp_point_to_bounds(x, y, bounds), f"clamp mismatch: {(x, y, bounds)}")


def test_window_geometry_helpers_match_sidecar() -> None:
    for width in (320, 641, 981, 1280, 1920):
        assert_true(geometry.session_split_x(width) == sidecar.session_split_x(width), f"split mismatch: {width}")
        assert_true(geometry.active_chat_title_left_x(width) == sidecar.active_chat_title_left_x(width), f"title left mismatch: {width}")
        assert_true(geometry.active_chat_title_right_x(width) == sidecar.active_chat_title_right_x(width), f"title right mismatch: {width}")
    for height in (260, 720, 860, 1080, 1200):
        assert_true(geometry.chat_header_cutoff_y(height) == sidecar.chat_header_cutoff_y(height), f"header mismatch: {height}")
        assert_true(geometry.active_chat_title_cutoff_y(height) == sidecar.active_chat_title_cutoff_y(height), f"title cutoff mismatch: {height}")
        assert_true(geometry.active_chat_title_top_cutoff_y(height) == sidecar.active_chat_title_top_cutoff_y(height), f"title top cutoff mismatch: {height}")
        assert_true(geometry.active_chat_title_top_y(height) == sidecar.active_chat_title_top_y(height), f"title top y mismatch: {height}")
        assert_true(geometry.active_chat_title_bottom_y(height) == sidecar.active_chat_title_bottom_y(height), f"title bottom y mismatch: {height}")
    for item in GEOMETRIES:
        assert_true(geometry.search_box_point_for_geometry(item) == sidecar.search_box_point_for_geometry(item), f"search point mismatch: {item}")
        assert_true(geometry.session_click_x_for_geometry(item) == sidecar.session_click_x_for_geometry(item), f"session click mismatch: {item}")
        assert_true(geometry.input_text_region_bounds(item) == sidecar.input_text_region_bounds(item), f"input bounds mismatch: {item}")


def test_rect_helpers_match_sidecar() -> None:
    rects = [
        {"left": 345, "top": 690, "right": 770, "bottom": 804},
        {"left": 345, "top": 970, "right": 770, "bottom": 1085},
        {"left": 690, "top": 1082, "right": 766, "bottom": 1125},
        {"left": 10, "top": 10, "right": 60, "bottom": 40},
    ]
    bounds = (320, 600, 900, 850)
    for item in GEOMETRIES:
        for rect in rects:
            assert_true(geometry.relative_rect(rect, item) == sidecar.relative_rect(rect, item), f"relative rect mismatch: {(rect, item)}")
            assert_true(geometry.rect_in_input_area(rect, item) == sidecar.rect_in_input_area(rect, item), f"input area mismatch: {(rect, item)}")
            assert_true(geometry.rect_in_input_toolbar(rect, item) == sidecar.rect_in_input_toolbar(rect, item), f"toolbar mismatch: {(rect, item)}")
            assert_true(geometry.rect_overlaps_region(rect, bounds) == sidecar.rect_overlaps_region(rect, bounds), f"overlap mismatch: {(rect, bounds)}")


def test_candidate_points_and_send_points_match_sidecar_with_seed() -> None:
    for item in GEOMETRIES:
        for min_points in (1, 10, 14):
            random.seed(20260619)
            extracted_input = geometry.input_click_candidate_points(item, min_points=min_points)
            random.seed(20260619)
            sidecar_input = sidecar.input_click_candidate_points(item, min_points=min_points)
            assert_true(extracted_input == sidecar_input, f"input candidates mismatch: {(item, min_points)}")

            random.seed(20260619)
            extracted_send = geometry.send_click_candidate_points(item, min_points=min_points)
            random.seed(20260619)
            sidecar_send = sidecar.send_click_candidate_points(item, min_points=min_points)
            assert_true(extracted_send == sidecar_send, f"send candidates mismatch: {(item, min_points)}")

        random.seed(20260619)
        extracted_points = geometry.calculate_send_points(item)
        random.seed(20260619)
        sidecar_points = sidecar.calculate_send_points(item)
        assert_true(extracted_points == sidecar_points, f"send points mismatch: {item}")


def main() -> int:
    tests = [
        test_geometry_module_exports_expected_helpers,
        test_scalar_geometry_helpers_match_sidecar,
        test_window_geometry_helpers_match_sidecar,
        test_rect_helpers_match_sidecar,
        test_candidate_points_and_send_points_match_sidecar_with_seed,
    ]
    passed = 0
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
        passed += 1
    print(f"All {passed} WeChat Win32/OCR geometry extraction checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
