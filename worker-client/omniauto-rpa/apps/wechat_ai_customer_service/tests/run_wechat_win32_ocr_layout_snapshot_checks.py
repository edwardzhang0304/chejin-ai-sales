"""Focused checks for per-frame layout snapshots and coordinate mapping."""

from __future__ import annotations

import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from apps.wechat_ai_customer_service.adapters.wechat_win32_ocr import window_layout  # noqa: E402


def assert_true(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def regions(width: int, height: int) -> dict[str, list[int]]:
    return {
        "left_nav_bounds": [0, 0, 120, height],
        "sidebar_bounds": [120, 0, int(width * 0.38), height],
        "sidebar_header_bounds": [120, 0, int(width * 0.38), int(height * 0.14)],
        "session_list_bounds": [120, int(height * 0.14), int(width * 0.38), height],
        "chat_header_bounds": [int(width * 0.38), 0, width, int(height * 0.12)],
        "message_viewport_bounds": [int(width * 0.38), int(height * 0.12), width, int(height * 0.82)],
        "input_bounds": [int(width * 0.38), int(height * 0.82), width, height],
    }


def snapshot(
    *,
    hwnd: int,
    image_size: tuple[int, int],
    capture_origin: list[int] | None,
    frame_id: str,
    executable: bool = True,
    capture_mode: str = window_layout.CAPTURE_MODE_WINDOW_VISIBLE_SCREEN,
) -> dict:
    width, height = image_size
    return window_layout.build_layout_snapshot(
        hwnd=hwnd,
        frame_id=frame_id,
        capture_mode=capture_mode,
        image_size=image_size,
        capture_screen_origin=capture_origin,
        window_rect=[capture_origin[0], capture_origin[1], capture_origin[0] + width, capture_origin[1] + height]
        if capture_origin
        else [0, 0, width, height],
        client_rect=[0, 0, width - 18, height - 40],
        client_screen_origin=[capture_origin[0] + 9, capture_origin[1] + 32] if capture_origin else None,
        dpi_scale=1.25,
        regions=regions(width, height),
        anchors=[{"name": "sidebar_separator", "confidence": 0.95}],
        confidence=0.95,
        executable=executable,
    )


def test_different_capture_origins_produce_different_screen_points() -> None:
    first = snapshot(hwnd=101, image_size=(1920, 1080), capture_origin=[100, 200], frame_id="frame-1920")
    second = snapshot(hwnd=101, image_size=(2560, 1440), capture_origin=[800, 40], frame_id="frame-2560")
    first_point = window_layout.image_point_to_screen(first, [400, 300])
    second_point = window_layout.image_point_to_screen(second, [400, 300])
    assert_true(first_point == [500, 500], f"unexpected first screen point: {first_point}")
    assert_true(second_point == [1200, 340], f"unexpected second screen point: {second_point}")
    assert_true(first_point != second_point, "different screenshot origins must not collapse to one coordinate")


def test_client_conversion_uses_client_screen_origin() -> None:
    current = snapshot(hwnd=102, image_size=(1920, 1080), capture_origin=[300, 120], frame_id="frame-client")
    screen_point = window_layout.image_point_to_screen(current, [600, 400])
    client_point = window_layout.screen_point_to_client(current, screen_point)
    assert_true(screen_point == [900, 520], f"screen mapping mismatch: {screen_point}")
    assert_true(client_point == [591, 368], f"client mapping mismatch: {client_point}")


def test_new_frame_invalidates_previous_snapshot() -> None:
    store = window_layout.LayoutSnapshotStore()
    first = snapshot(hwnd=103, image_size=(1920, 1080), capture_origin=[0, 0], frame_id="frame-old")
    second = snapshot(hwnd=103, image_size=(1920, 1080), capture_origin=[80, 30], frame_id="frame-new")
    store.put(first)
    store.put(second)
    store.invalidate(first["layout_snapshot_id"], reason="new_frame_captured")
    old = store.get(first["layout_snapshot_id"])
    new = store.get(second["layout_snapshot_id"])
    assert_true(bool(old and old.get("invalidated")), "old frame must be invalidated")
    assert_true(bool(new and not new.get("invalidated")), "new frame must remain executable")


def test_geometry_or_image_size_change_makes_snapshot_stale() -> None:
    current = snapshot(hwnd=104, image_size=(1920, 1080), capture_origin=[0, 0], frame_id="frame-stale")
    same = window_layout.snapshot_matches_current(
        current,
        hwnd=104,
        window_rect=[0, 0, 1920, 1080],
        client_rect=[0, 0, 1902, 1040],
        dpi_scale=1.25,
        image_size=(1920, 1080),
    )
    moved = window_layout.snapshot_matches_current(
        current,
        hwnd=104,
        window_rect=[40, 0, 1960, 1080],
        client_rect=[0, 0, 1902, 1040],
        dpi_scale=1.25,
        image_size=(1920, 1080),
    )
    resized = window_layout.snapshot_matches_current(
        current,
        hwnd=104,
        window_rect=[0, 0, 2560, 1440],
        client_rect=[0, 0, 2542, 1400],
        dpi_scale=1.25,
        image_size=(2560, 1440),
    )
    assert_true(same, "unchanged window geometry should keep the snapshot current")
    assert_true(not moved, "window movement must stale the snapshot")
    assert_true(not resized, "window or screenshot resize must stale the snapshot")


def test_unknown_capture_origin_cannot_become_physical_click() -> None:
    current = snapshot(
        hwnd=105,
        image_size=(1920, 1080),
        capture_origin=None,
        frame_id="frame-print-window",
        capture_mode=window_layout.CAPTURE_MODE_PRINT_WINDOW,
    )
    assert_true(not current["clickable"], "PrintWindow or unknown-origin frame must not be clickable")
    try:
        window_layout.image_point_to_screen(current, [500, 300])
    except window_layout.LayoutSnapshotError as exc:
        assert_true(exc.code == "LAYOUT_CAPTURE_ORIGIN_UNKNOWN", f"wrong mapping error: {exc.code}")
    else:
        raise AssertionError("unknown capture origin unexpectedly produced a screen point")


class SyntheticImage:
    def __init__(self, width: int, height: int, verticals: tuple[int, ...], horizontals: tuple[int, ...]) -> None:
        self.size = (width, height)
        self.verticals = verticals
        self.horizontals = horizontals

    def getpixel(self, point: tuple[int, int]) -> tuple[int, int, int]:
        x, y = point
        if any(x == boundary - 1 for boundary in self.verticals):
            return (20, 20, 20)
        if any(x == boundary + 1 for boundary in self.verticals):
            return (230, 230, 230)
        if any(y == boundary - 1 for boundary in self.horizontals):
            return (20, 20, 20)
        if any(y == boundary + 1 for boundary in self.horizontals):
            return (230, 230, 230)
        return (120, 120, 120)


def test_structural_regions_follow_each_image_size() -> None:
    first = window_layout.build_structural_layout_regions(
        SyntheticImage(1920, 1080, (230, 720), (130, 860, 900))
    )
    second = window_layout.build_structural_layout_regions(
        SyntheticImage(2560, 1440, (300, 960), (170, 1160, 1210))
    )
    assert_true(first.get("ok"), f"first structural layout should resolve: {first}")
    assert_true(second.get("ok"), f"second structural layout should resolve: {second}")
    assert_true(
        first["regions"]["sidebar_bounds"] != second["regions"]["sidebar_bounds"],
        "layout regions must be derived from each current image",
    )
    for result in (first, second):
        width, height = result["regions"]["input_bounds"][2], result["regions"]["input_bounds"][3]
        for name, bounds in result["regions"].items():
            assert_true(bounds[0] >= 0 and bounds[1] >= 0, f"{name} has negative origin: {bounds}")
            assert_true(bounds[2] <= width and bounds[3] <= height, f"{name} exceeds image: {bounds}")


def main() -> None:
    tests = [
        value
        for name, value in globals().items()
        if name.startswith("test_") and callable(value)
    ]
    for test in tests:
        test()
    print(f"layout snapshot checks passed: {len(tests)}")


if __name__ == "__main__":
    main()
