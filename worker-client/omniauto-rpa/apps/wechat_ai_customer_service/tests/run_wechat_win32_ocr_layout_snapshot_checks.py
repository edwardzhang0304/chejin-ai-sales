"""v0.9.30 startup-calibration and business-frame identity checks.

Synthetic frames here cover deterministic edge cases. Real user screenshots are exercised
separately by deliverables/test_runs/run_v0923_real_screenshot_calibration.py.
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from apps.wechat_ai_customer_service.adapters.wechat_win32_ocr import window_layout


def assert_true(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _bright_wechat_add_friend_frame(*, selected_row: int) -> tuple[Image.Image, dict[str, float | str]]:
    """Build a deterministic bright shell; exported for the public C1 entry tests."""

    width, height = 980, 860
    nav_x = int(width * 0.075)
    sidebar_x = int(width * 0.385)
    image = Image.new("RGB", (width, height), (250, 250, 250))
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, nav_x - 1, height - 1), fill=(246, 246, 246))
    draw.rectangle((nav_x, 0, sidebar_x - 1, height - 1), fill=(234, 234, 234))
    draw.line((nav_x, 0, nav_x, height - 1), fill=(155, 155, 155), width=2)
    draw.line((sidebar_x, 0, sidebar_x, height - 1), fill=(140, 140, 140), width=2)
    selected_top = int(height * 0.10) + (selected_row * int(height * 0.09))
    draw.rectangle((nav_x, selected_top, sidebar_x - 1, selected_top + int(height * 0.09)), fill=(20, 178, 116))
    input_y = int(height * 0.80)
    draw.line((sidebar_x, int(height * 0.10), width - 1, int(height * 0.10)), fill=(170, 170, 170), width=2)
    draw.line((sidebar_x, input_y, width - 1, input_y), fill=(170, 170, 170), width=2)
    search_top = int(height * 0.058)
    search_height = max(16, int(height * 0.022))
    search_item: dict[str, float | str] = {
        "text": "搜索",
        "left": nav_x + int((sidebar_x - nav_x) * 0.16),
        "top": search_top,
        "right": nav_x + int((sidebar_x - nav_x) * 0.28),
        "bottom": search_top + search_height,
        "confidence": 0.98,
    }
    plus_x = sidebar_x - int((sidebar_x - nav_x) * 0.11)
    plus_y = int((float(search_item["top"]) + float(search_item["bottom"])) / 2)
    radius = max(9, int(search_height * 0.55))
    draw.ellipse((plus_x - radius, plus_y - radius, plus_x + radius, plus_y + radius), outline=(70, 70, 70), width=2)
    draw.line((plus_x - 6, plus_y, plus_x + 6, plus_y), fill=(60, 60, 60), width=2)
    draw.line((plus_x, plus_y - 6, plus_x, plus_y + 6), fill=(60, 60, 60), width=2)
    return image, search_item


def calibration_for(image: Image.Image, search_item: dict[str, float | str]) -> dict:
    width, height = image.size
    return window_layout.build_startup_layout_calibration(
        hwnd=101,
        process_id=202,
        image=image,
        ocr_items=[search_item],
        window_rect=[12, 12, 12 + width, 12 + height],
        client_rect={"left": 0, "top": 0, "right": width, "bottom": height, "width": width, "height": height},
        client_screen_origin=[12, 12],
        dpi_scale=1.0,
        capture_mode=window_layout.CAPTURE_MODE_CLIENT_AREA,
    )


def test_startup_calibration_is_unique_from_business_frame() -> None:
    image, search = _bright_wechat_add_friend_frame(selected_row=3)
    calibration = calibration_for(image, search)
    assert_true(calibration.get("executable") is True, f"startup calibration failed: {calibration}")
    snapshot = window_layout.build_layout_snapshot(
        hwnd=101,
        frame_id="business-frame-1",
        capture_mode=window_layout.CAPTURE_MODE_CLIENT_AREA,
        image_size=image.size,
        capture_screen_origin=[12, 12],
        window_rect=calibration["window_rect"],
        client_rect=calibration["client_rect"],
        client_screen_origin=calibration["client_screen_origin"],
        dpi_scale=1.0,
        regions={name: calibration[name] for name in window_layout.REQUIRED_LAYOUT_REGION_NAMES},
        anchors=calibration["anchors"],
        confidence=calibration["confidence"],
        conflicts=[],
        executable=True,
    )
    snapshot["calibration_id"] = calibration["calibration_id"]
    assert_true(snapshot.get("layout_snapshot_id") != calibration.get("calibration_id"), f"frame and calibration identities collapsed: {snapshot}")
    assert_true(snapshot.get("frame_id") == "business-frame-1", f"business frame identity missing: {snapshot}")


def test_reference_points_are_region_local_and_map_inside_regions() -> None:
    image, search = _bright_wechat_add_friend_frame(selected_row=0)
    calibration = calibration_for(image, search)
    for name, contract in window_layout.REFERENCE_REGION_MAP_V0920.items():
        assert_true("screen_point" not in contract and "absolute_point" not in contract, f"{name} retained an absolute coordinate: {contract}")
        mapped = window_layout.map_reference_region_point(calibration, name)
        assert_true(window_layout.point_in_bounds(mapped["image_point"], mapped["region_bounds"]), f"{name} escaped calibrated region: {mapped}")


def test_printwindow_never_creates_executable_startup_map() -> None:
    image, search = _bright_wechat_add_friend_frame(selected_row=1)
    width, height = image.size
    result = window_layout.build_startup_layout_calibration(
        hwnd=101,
        process_id=202,
        image=image,
        ocr_items=[search],
        window_rect=[0, 0, width, height],
        client_rect={"left": 0, "top": 0, "right": width, "bottom": height, "width": width, "height": height},
        client_screen_origin=[0, 0],
        dpi_scale=1.0,
        capture_mode=window_layout.CAPTURE_MODE_PRINT_WINDOW,
    )
    assert_true(result.get("executable") is False, f"PrintWindow calibration became executable: {result}")


def main() -> int:
    tests = [
        test_startup_calibration_is_unique_from_business_frame,
        test_reference_points_are_region_local_and_map_inside_regions,
        test_printwindow_never_creates_executable_startup_map,
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"All {len(tests)} v0.9.30 calibration/frame checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
