"""Contract checks for Win32/OCR capture planning helpers."""

from __future__ import annotations

from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from apps.wechat_ai_customer_service.adapters import wechat_win32_ocr_sidecar as sidecar  # noqa: E402
from apps.wechat_ai_customer_service.adapters.wechat_win32_ocr import capture  # noqa: E402


def assert_true(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


class FakeWin32Gui:
    @staticmethod
    def GetWindowRect(_hwnd: int) -> tuple[int, int, int, int]:
        return (100, 50, 1081, 910)


class PrintWindowFixture:
    def __init__(
        self,
        *,
        rect: tuple[int, int, int, int] = (0, 0, 4, 3),
        hwnd_dc: int | None = 101,
        print_results: list[int] | None = None,
        fail_at: str = "",
    ) -> None:
        self.rect = rect
        self.hwnd_dc = hwnd_dc
        self.print_results = list(print_results if print_results is not None else [1])
        self.fail_at = fail_at
        self.events: list[str] = []
        self.print_flags: list[int] = []
        self.bitmap = FakeBitmap(self)
        self.src_dc = FakeSrcDC(self)
        self.mem_dc = FakeMemDC(self)
        self.win32gui = FakePrintWindowGui(self)
        self.win32ui = FakePrintWindowUi(self)
        self.user32 = FakePrintWindowUser32(self)


class FakePrintWindowGui:
    def __init__(self, fixture: PrintWindowFixture) -> None:
        self.fixture = fixture

    def GetWindowRect(self, _hwnd: int) -> tuple[int, int, int, int]:
        self.fixture.events.append("GetWindowRect")
        return self.fixture.rect

    def GetWindowDC(self, _hwnd: int) -> int | None:
        self.fixture.events.append("GetWindowDC")
        return self.fixture.hwnd_dc

    def DeleteObject(self, handle: int) -> None:
        self.fixture.events.append(f"DeleteObject:{handle}")

    def ReleaseDC(self, _hwnd: int, hwnd_dc: int) -> None:
        self.fixture.events.append(f"ReleaseDC:{hwnd_dc}")


class FakePrintWindowUi:
    def __init__(self, fixture: PrintWindowFixture) -> None:
        self.fixture = fixture

    def CreateDCFromHandle(self, _hwnd_dc: int) -> "FakeSrcDC":
        self.fixture.events.append("CreateDCFromHandle")
        return self.fixture.src_dc

    def CreateBitmap(self) -> "FakeBitmap":
        self.fixture.events.append("CreateBitmap")
        return self.fixture.bitmap


class FakeSrcDC:
    def __init__(self, fixture: PrintWindowFixture) -> None:
        self.fixture = fixture

    def CreateCompatibleDC(self) -> "FakeMemDC":
        self.fixture.events.append("CreateCompatibleDC")
        return self.fixture.mem_dc

    def DeleteDC(self) -> None:
        self.fixture.events.append("src.DeleteDC")


class FakeMemDC:
    def __init__(self, fixture: PrintWindowFixture) -> None:
        self.fixture = fixture

    def SelectObject(self, _bitmap: "FakeBitmap") -> None:
        self.fixture.events.append("SelectObject")

    def GetSafeHdc(self) -> int:
        self.fixture.events.append("GetSafeHdc")
        return 202

    def DeleteDC(self) -> None:
        self.fixture.events.append("mem.DeleteDC")


class FakeBitmap:
    def __init__(self, fixture: PrintWindowFixture) -> None:
        self.fixture = fixture

    def CreateCompatibleBitmap(self, _src_dc: FakeSrcDC, width: int, height: int) -> None:
        self.fixture.events.append(f"CreateCompatibleBitmap:{width}x{height}")
        if self.fixture.fail_at == "CreateCompatibleBitmap":
            raise RuntimeError("bitmap boom")

    def GetInfo(self) -> dict[str, int]:
        self.fixture.events.append("GetInfo")
        return {"bmWidth": 4, "bmHeight": 3}

    def GetBitmapBits(self, _signed: bool) -> bytes:
        self.fixture.events.append("GetBitmapBits")
        if self.fixture.fail_at == "GetBitmapBits":
            raise RuntimeError("bits boom")
        return b"\x00\x00\x00\x00" * 12

    def GetHandle(self) -> int:
        self.fixture.events.append("GetHandle")
        return 303


class FakePrintWindowUser32:
    def __init__(self, fixture: PrintWindowFixture) -> None:
        self.fixture = fixture

    def PrintWindow(self, _hwnd: int, _hdc: int, flag: int) -> int:
        self.fixture.events.append(f"PrintWindow:{flag}")
        self.fixture.print_flags.append(flag)
        if self.fixture.print_results:
            return int(self.fixture.print_results.pop(0))
        return 0


class FakeImageFactory:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def frombuffer(self, *args):
        self.calls.append(args)
        return {"image": args[1]}


def test_capture_module_exports_expected_helpers() -> None:
    for name in (
        "capture_rect_candidates",
        "collect_capture_candidates",
        "capture_window_image",
        "capture_window_by_rect",
        "try_image_grab",
        "select_best_capture_candidate",
    ):
        assert_true(callable(getattr(capture, name, None)), f"capture helper missing: {name}")


def test_capture_rect_candidates_preserve_base_only_when_scale_is_normal() -> None:
    rects = capture.capture_rect_candidates((100, 50, 1081, 910), dpi_scale=1.0)
    assert_true(rects == [(100, 50, 1081, 910)], f"normal scale rect candidates mismatch: {rects}")


def test_capture_rect_candidates_preserve_scaled_order() -> None:
    rects = capture.capture_rect_candidates((100, 50, 1081, 910), dpi_scale=1.25)
    expected = [
        (100, 50, 1081, 910),
        (80, 40, 865, 728),
        (125, 62, 1351, 1138),
    ]
    assert_true(rects == expected, f"scaled rect candidates mismatch: {rects}")


def test_collect_capture_candidates_matches_sidecar_order_with_fake_grabber() -> None:
    original_win32gui = sidecar.win32gui
    original_dpi = sidecar.window_dpi_scale
    original_grab = sidecar.try_image_grab
    calls: list[tuple[int, int, int, int]] = []

    def fake_grab(rect: tuple[int, int, int, int]) -> str | None:
        calls.append(rect)
        return None if rect == (80, 40, 865, 728) else f"image:{rect}"

    try:
        sidecar.win32gui = FakeWin32Gui()
        sidecar.window_dpi_scale = lambda _hwnd: 1.25
        sidecar.try_image_grab = fake_grab
        sidecar_result = sidecar.capture_window_by_rect(1001)
    finally:
        sidecar.win32gui = original_win32gui
        sidecar.window_dpi_scale = original_dpi
        sidecar.try_image_grab = original_grab

    expected_calls = [
        (100, 50, 1081, 910),
        (80, 40, 865, 728),
        (125, 62, 1351, 1138),
    ]
    assert_true(calls == expected_calls, f"sidecar grab order mismatch: {calls}")
    extracted_result = capture.collect_capture_candidates(
        (100, 50, 1081, 910),
        dpi_scale=1.25,
        grabber=lambda rect: None if rect == (80, 40, 865, 728) else f"image:{rect}",
    )
    assert_true(
        sidecar_result == extracted_result == ["image:(100, 50, 1081, 910)", "image:(125, 62, 1351, 1138)"],
        f"capture candidates mismatch: sidecar={sidecar_result}, extracted={extracted_result}",
    )


def test_capture_window_by_rect_wrapper_matches_sidecar_with_fake_dependencies() -> None:
    original_win32gui = sidecar.win32gui
    original_dpi = sidecar.window_dpi_scale
    original_grab = sidecar.try_image_grab
    try:
        sidecar.win32gui = FakeWin32Gui()
        sidecar.window_dpi_scale = lambda _hwnd: 1.25
        sidecar.try_image_grab = lambda rect: None if rect == (80, 40, 865, 728) else f"image:{rect}"
        sidecar_result = sidecar.capture_window_by_rect(1001)
    finally:
        sidecar.win32gui = original_win32gui
        sidecar.window_dpi_scale = original_dpi
        sidecar.try_image_grab = original_grab
    extracted_result = capture.capture_window_by_rect(
        1001,
        rect_provider=lambda _hwnd: (100, 50, 1081, 910),
        dpi_scale_provider=lambda _hwnd: 1.25,
        grabber=lambda rect: None if rect == (80, 40, 865, 728) else f"image:{rect}",
    )
    assert_true(extracted_result == sidecar_result, f"capture_window_by_rect mismatch: {extracted_result} != {sidecar_result}")


def test_try_image_grab_matches_sidecar_for_small_rect_success_and_failure() -> None:
    original_grab = sidecar.ImageGrab.grab
    calls: list[tuple[int, int, int, int]] = []

    def fake_grab(*, bbox):
        calls.append(bbox)
        if bbox == (10, 10, 50, 50):
            return "image-ok"
        raise RuntimeError("grab boom")

    try:
        sidecar.ImageGrab.grab = fake_grab
        assert_true(sidecar.try_image_grab((0, 0, 2, 50)) is None, "small width should not grab")
        assert_true(sidecar.try_image_grab((0, 0, 50, 2)) is None, "small height should not grab")
        assert_true(sidecar.try_image_grab((10, 10, 50, 50)) == "image-ok", "successful grab mismatch")
        assert_true(sidecar.try_image_grab((20, 20, 60, 60)) is None, "grab exception should return None")
    finally:
        sidecar.ImageGrab.grab = original_grab

    assert_true(calls == [(10, 10, 50, 50), (20, 20, 60, 60)], f"unexpected grab calls: {calls}")
    assert_true(
        capture.try_image_grab((0, 0, 2, 50), image_grabber=fake_grab) is None,
        "extracted small width should not grab",
    )
    assert_true(
        capture.try_image_grab((10, 10, 50, 50), image_grabber=fake_grab) == "image-ok",
        "extracted successful grab mismatch",
    )


def run_sidecar_capture_window_image_with_fixture(fixture: PrintWindowFixture, image_factory: FakeImageFactory):
    original_win32gui = sidecar.win32gui
    original_win32ui = sidecar.win32ui
    original_windll = sidecar.ctypes.windll
    original_frombuffer = sidecar.Image.frombuffer
    try:
        sidecar.win32gui = fixture.win32gui
        sidecar.win32ui = fixture.win32ui
        sidecar.ctypes.windll = type("FakeWindll", (), {"user32": fixture.user32})()
        sidecar.Image.frombuffer = image_factory.frombuffer
        return sidecar.capture_window_image(1001)
    finally:
        sidecar.win32gui = original_win32gui
        sidecar.win32ui = original_win32ui
        sidecar.ctypes.windll = original_windll
        sidecar.Image.frombuffer = original_frombuffer


def assert_resources_released(fixture: PrintWindowFixture, *, bitmap_created: bool = True, dc_created: bool = True) -> None:
    if bitmap_created:
        assert_true("DeleteObject:303" in fixture.events, f"bitmap handle should be deleted: {fixture.events}")
    if dc_created:
        assert_true("mem.DeleteDC" in fixture.events, f"memory DC should be deleted: {fixture.events}")
        assert_true("src.DeleteDC" in fixture.events, f"source DC should be deleted: {fixture.events}")
    if fixture.hwnd_dc:
        assert_true(f"ReleaseDC:{fixture.hwnd_dc}" in fixture.events, f"window DC should be released: {fixture.events}")


def test_capture_window_image_printwindow_full_content_success_releases_resources() -> None:
    fixture = PrintWindowFixture(print_results=[1])
    image_factory = FakeImageFactory()
    result = run_sidecar_capture_window_image_with_fixture(fixture, image_factory)
    extracted_fixture = PrintWindowFixture(print_results=[1])
    extracted_image_factory = FakeImageFactory()
    extracted_result = capture.capture_window_image(
        1001,
        win32gui_module=extracted_fixture.win32gui,
        win32ui_module=extracted_fixture.win32ui,
        user32=extracted_fixture.user32,
        image_factory=extracted_image_factory,
    )
    assert_true(result == {"image": (4, 3)}, f"unexpected image result: {result}")
    assert_true(extracted_result == result, f"extracted full-content result mismatch: {extracted_result}")
    assert_true(fixture.print_flags == [0x2], f"full-content success should not call classic fallback: {fixture.print_flags}")
    assert_resources_released(fixture)
    assert_resources_released(extracted_fixture)


def test_capture_window_image_printwindow_classic_fallback_releases_resources() -> None:
    fixture = PrintWindowFixture(print_results=[0, 1])
    image_factory = FakeImageFactory()
    result = run_sidecar_capture_window_image_with_fixture(fixture, image_factory)
    extracted_fixture = PrintWindowFixture(print_results=[0, 1])
    extracted_result = capture.capture_window_image(
        1001,
        win32gui_module=extracted_fixture.win32gui,
        win32ui_module=extracted_fixture.win32ui,
        user32=extracted_fixture.user32,
        image_factory=image_factory,
    )
    assert_true(result == {"image": (4, 3)}, f"unexpected fallback image result: {result}")
    assert_true(extracted_result == result, f"extracted fallback result mismatch: {extracted_result}")
    assert_true(fixture.print_flags == [0x2, 0], f"classic fallback order mismatch: {fixture.print_flags}")
    assert_true(extracted_fixture.print_flags == [0x2, 0], f"extracted fallback order mismatch: {extracted_fixture.print_flags}")
    assert_resources_released(fixture)
    assert_resources_released(extracted_fixture)


def test_capture_window_image_printwindow_failure_returns_none_and_releases_resources() -> None:
    fixture = PrintWindowFixture(print_results=[0, 0])
    image_factory = FakeImageFactory()
    result = run_sidecar_capture_window_image_with_fixture(fixture, image_factory)
    assert_true(result is None, f"print failure should return None: {result}")
    assert_true(fixture.print_flags == [0x2, 0], f"print failure order mismatch: {fixture.print_flags}")
    assert_resources_released(fixture)


def test_capture_window_image_bitmap_exception_returns_none_and_releases_partial_resources() -> None:
    fixture = PrintWindowFixture(fail_at="CreateCompatibleBitmap")
    image_factory = FakeImageFactory()
    result = run_sidecar_capture_window_image_with_fixture(fixture, image_factory)
    assert_true(result is None, f"bitmap exception should return None: {result}")
    assert_true("PrintWindow:2" not in fixture.events, f"should not print after bitmap setup failure: {fixture.events}")
    assert_resources_released(fixture)


def test_capture_window_image_get_bits_exception_returns_none_and_releases_resources() -> None:
    fixture = PrintWindowFixture(print_results=[1], fail_at="GetBitmapBits")
    image_factory = FakeImageFactory()
    result = run_sidecar_capture_window_image_with_fixture(fixture, image_factory)
    assert_true(result is None, f"bitmap bits exception should return None: {result}")
    assert_true(fixture.print_flags == [0x2], f"unexpected print flags before bits failure: {fixture.print_flags}")
    assert_resources_released(fixture)


def test_capture_window_image_no_window_dc_or_tiny_rect_skips_resource_creation() -> None:
    no_dc_fixture = PrintWindowFixture(hwnd_dc=None)
    image_factory = FakeImageFactory()
    result = run_sidecar_capture_window_image_with_fixture(no_dc_fixture, image_factory)
    assert_true(result is None, f"missing window DC should return None: {result}")
    assert_true("CreateDCFromHandle" not in no_dc_fixture.events, f"should not create DC without hwnd_dc: {no_dc_fixture.events}")

    tiny_fixture = PrintWindowFixture(rect=(0, 0, 2, 50))
    result = run_sidecar_capture_window_image_with_fixture(tiny_fixture, image_factory)
    assert_true(result is None, f"tiny rect should return None: {result}")
    assert_true("GetWindowDC" not in tiny_fixture.events, f"tiny rect should not get DC: {tiny_fixture.events}")


def test_select_best_capture_candidate_matches_sidecar_max_score_semantics() -> None:
    candidates = ["low", "high", "mid"]
    scores = {"low": 0.2, "high": 9.5, "mid": 3.0}
    selected = capture.select_best_capture_candidate(candidates, score=lambda image: scores[image])
    assert_true(selected == "high", f"best candidate mismatch: {selected}")
    assert_true(capture.select_best_capture_candidate([], score=lambda _image: 1.0) is None, "empty candidates should return None")


def main() -> int:
    tests = [
        test_capture_module_exports_expected_helpers,
        test_capture_rect_candidates_preserve_base_only_when_scale_is_normal,
        test_capture_rect_candidates_preserve_scaled_order,
        test_collect_capture_candidates_matches_sidecar_order_with_fake_grabber,
        test_capture_window_by_rect_wrapper_matches_sidecar_with_fake_dependencies,
        test_try_image_grab_matches_sidecar_for_small_rect_success_and_failure,
        test_capture_window_image_printwindow_full_content_success_releases_resources,
        test_capture_window_image_printwindow_classic_fallback_releases_resources,
        test_capture_window_image_printwindow_failure_returns_none_and_releases_resources,
        test_capture_window_image_bitmap_exception_returns_none_and_releases_partial_resources,
        test_capture_window_image_get_bits_exception_returns_none_and_releases_resources,
        test_capture_window_image_no_window_dc_or_tiny_rect_skips_resource_creation,
        test_select_best_capture_candidate_matches_sidecar_max_score_semantics,
    ]
    passed = 0
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
        passed += 1
    print(f"All {passed} WeChat Win32/OCR capture checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
