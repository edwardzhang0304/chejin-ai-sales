"""Contract checks for read-only Win32/OCR window metrics extraction."""

from __future__ import annotations

from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from apps.wechat_ai_customer_service.adapters import wechat_win32_ocr_sidecar as sidecar  # noqa: E402
from apps.wechat_ai_customer_service.adapters.wechat_win32_ocr import window_metrics  # noqa: E402


def assert_true(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


class FakeWin32Gui:
    def __init__(self, *, fail_client: bool = False) -> None:
        self.fail_client = fail_client

    @staticmethod
    def GetWindowRect(_hwnd: int) -> tuple[int, int, int, int]:
        return (10, 20, 310, 420)

    def GetClientRect(self, _hwnd: int) -> tuple[int, int, int, int]:
        if self.fail_client:
            raise RuntimeError("client boom")
        return (0, 0, 280, 360)

    @staticmethod
    def ClientToScreen(_hwnd: int, point: tuple[int, int]) -> tuple[int, int]:
        return (point[0] + 15, point[1] + 25)


class FakeUser32:
    def __init__(self, dpi: int | Exception) -> None:
        self.dpi = dpi

    def GetDpiForWindow(self, _hwnd: int) -> int:
        if isinstance(self.dpi, Exception):
            raise self.dpi
        return self.dpi


class FakeWindll:
    def __init__(self, user32: FakeUser32) -> None:
        self.user32 = user32


def test_window_metrics_module_exports_expected_helpers() -> None:
    for name in ("get_window_geometry", "get_window_client_geometry", "window_dpi_scale"):
        assert_true(callable(getattr(window_metrics, name, None)), f"window metrics helper missing: {name}")


def test_get_window_geometry_matches_sidecar_with_fake_win32gui() -> None:
    original_win32gui = sidecar.win32gui
    fake = FakeWin32Gui()
    try:
        sidecar.win32gui = fake
        sidecar_result = sidecar.get_window_geometry(1001)
    finally:
        sidecar.win32gui = original_win32gui
    extracted_result = window_metrics.get_window_geometry(1001, win32gui_module=fake)
    expected = {"left": 10, "top": 20, "right": 310, "bottom": 420, "width": 300, "height": 400}
    assert_true(sidecar_result == expected, f"sidecar geometry mismatch: {sidecar_result}")
    assert_true(extracted_result == sidecar_result, f"extracted geometry mismatch: {extracted_result}")


def test_get_window_client_geometry_matches_sidecar_with_fake_win32gui() -> None:
    original_win32gui = sidecar.win32gui
    fake = FakeWin32Gui()
    try:
        sidecar.win32gui = fake
        sidecar_result = sidecar.get_window_client_geometry(1001)
    finally:
        sidecar.win32gui = original_win32gui
    extracted_result = window_metrics.get_window_client_geometry(1001, win32gui_module=fake)
    expected = {
        "left": 0,
        "top": 0,
        "right": 280,
        "bottom": 360,
        "width": 280,
        "height": 360,
        "screen_left": 15,
        "screen_top": 25,
        "screen_right": 295,
        "screen_bottom": 385,
    }
    assert_true(sidecar_result == expected, f"sidecar client geometry mismatch: {sidecar_result}")
    assert_true(extracted_result == sidecar_result, f"extracted client geometry mismatch: {extracted_result}")


def test_get_window_client_geometry_error_shape_matches_sidecar() -> None:
    original_win32gui = sidecar.win32gui
    fake = FakeWin32Gui(fail_client=True)
    try:
        sidecar.win32gui = fake
        sidecar_result = sidecar.get_window_client_geometry(1001)
    finally:
        sidecar.win32gui = original_win32gui
    extracted_result = window_metrics.get_window_client_geometry(1001, win32gui_module=fake)
    assert_true(sidecar_result == {"error": "RuntimeError('client boom')"}, f"sidecar error mismatch: {sidecar_result}")
    assert_true(extracted_result == sidecar_result, f"extracted error mismatch: {extracted_result}")


def test_window_dpi_scale_matches_sidecar_with_fake_windll() -> None:
    had_windll = hasattr(sidecar.ctypes, "windll")
    original_windll = getattr(sidecar.ctypes, "windll", None)
    fake_windll = FakeWindll(FakeUser32(144))
    try:
        sidecar.ctypes.windll = fake_windll
        sidecar_result = sidecar.window_dpi_scale(1001)
    finally:
        if had_windll:
            sidecar.ctypes.windll = original_windll
        else:
            delattr(sidecar.ctypes, "windll")
    extracted_result = window_metrics.window_dpi_scale(1001, windll=fake_windll)
    assert_true(sidecar_result == 1.5, f"sidecar dpi scale mismatch: {sidecar_result}")
    assert_true(extracted_result == sidecar_result, f"extracted dpi scale mismatch: {extracted_result}")


def test_window_dpi_scale_falls_back_to_one_on_invalid_dpi_or_error() -> None:
    assert_true(
        window_metrics.window_dpi_scale(1001, user32=FakeUser32(0)) == 1.0,
        "zero DPI should fall back to 1.0",
    )
    assert_true(
        window_metrics.window_dpi_scale(1001, user32=FakeUser32(RuntimeError("dpi boom"))) == 1.0,
        "DPI errors should fall back to 1.0",
    )


def main() -> int:
    tests = [
        test_window_metrics_module_exports_expected_helpers,
        test_get_window_geometry_matches_sidecar_with_fake_win32gui,
        test_get_window_client_geometry_matches_sidecar_with_fake_win32gui,
        test_get_window_client_geometry_error_shape_matches_sidecar,
        test_window_dpi_scale_matches_sidecar_with_fake_windll,
        test_window_dpi_scale_falls_back_to_one_on_invalid_dpi_or_error,
    ]
    passed = 0
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
        passed += 1
    print(f"All {passed} WeChat Win32/OCR window metrics checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
