"""Contract checks for Win32/OCR render diagnostics extraction."""

from __future__ import annotations

from pathlib import Path
import sys

from PIL import Image, ImageDraw


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from apps.wechat_ai_customer_service.adapters import wechat_win32_ocr_sidecar as sidecar  # noqa: E402
from apps.wechat_ai_customer_service.adapters.wechat_win32_ocr import render_diagnostics  # noqa: E402


def assert_true(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def test_render_diagnostics_module_exports_expected_helpers() -> None:
    for name in (
        "detect_blank_render",
        "image_information_score",
        "likely_foreign_overlay_capture",
        "window_content_health_score_from_signals",
    ):
        assert_true(callable(getattr(render_diagnostics, name, None)), f"render helper missing: {name}")


def test_blank_render_detection_matches_sidecar() -> None:
    samples = [
        (Image.new("RGB", (980, 860), "white"), [], {"width": 980, "height": 860}),
        (Image.new("RGB", (980, 860), "black"), [], {"width": 980, "height": 860}),
        (Image.new("RGB", (300, 200), "white"), [], {"width": 300, "height": 200}),
        (Image.new("RGB", (980, 860), "white"), [{"text": "微信"}], {"width": 980, "height": 860}),
    ]
    noisy = Image.new("RGB", (980, 860), "white")
    draw = ImageDraw.Draw(noisy)
    draw.rectangle((20, 20, 260, 160), fill="black")
    samples.append((noisy, [], {"width": 980, "height": 860}))
    for screenshot, ocr_items, geometry in samples:
        assert_true(
            render_diagnostics.detect_blank_render(screenshot, ocr_items, geometry=geometry)
            == sidecar.detect_blank_render(screenshot, ocr_items, geometry=geometry),
            f"blank render mismatch: {(ocr_items, geometry)}",
        )


def test_image_information_score_matches_sidecar() -> None:
    images = [
        Image.new("RGB", (120, 80), "white"),
        Image.new("RGB", (120, 80), "black"),
    ]
    contrast = Image.new("RGB", (120, 80), "white")
    draw = ImageDraw.Draw(contrast)
    draw.rectangle((10, 10, 70, 50), fill="black")
    images.append(contrast)
    for image in images:
        assert_true(
            render_diagnostics.image_information_score(image) == sidecar.image_information_score(image),
            "image information score mismatch",
        )


def test_foreign_overlay_detection_matches_sidecar() -> None:
    samples = [
        [],
        [{"text": "文件传输助手"}, {"text": "搜索"}],
        [{"text": "apps/wechat_ai_customer_servic"}, {"text": "文件已更改"}],
        [{"text": "serverchan"}, {"text": "要求后续变更"}, {"text": "展开显示"}],
    ]
    for items in samples:
        assert_true(
            render_diagnostics.likely_foreign_overlay_capture(items) == sidecar.likely_foreign_overlay_capture(items),
            f"foreign overlay mismatch: {items}",
        )


def test_window_content_health_score_from_signals_matches_sidecar_shape() -> None:
    items = [{"text": "搜索"}, {"text": "文件传输助手"}, {"text": "发送"}]
    score = render_diagnostics.window_content_health_score_from_signals(
        items,
        blank_render_detected=False,
        quick_login_detected=False,
        auxiliary_shell_detected=False,
        blocking_reason="",
        text_normalizer=sidecar.normalize_ocr_text,
    )
    assert_true(score == 38, f"unexpected healthy chat score: {score}")

    base_kwargs = {
        "ocr_items": items,
        "text_normalizer": sidecar.normalize_ocr_text,
    }
    assert_true(
        render_diagnostics.window_content_health_score_from_signals(
            **base_kwargs,
            blank_render_detected=True,
            quick_login_detected=False,
            auxiliary_shell_detected=False,
            blocking_reason="",
        )
        == -100,
        "blank render should dominate health score",
    )
    assert_true(
        render_diagnostics.window_content_health_score_from_signals(
            **base_kwargs,
            blank_render_detected=False,
            quick_login_detected=True,
            auxiliary_shell_detected=True,
            blocking_reason="blocked",
        )
        == -20,
        "quick login should keep previous priority over auxiliary/blocking",
    )
    assert_true(
        render_diagnostics.window_content_health_score_from_signals(
            **base_kwargs,
            blank_render_detected=False,
            quick_login_detected=False,
            auxiliary_shell_detected=True,
            blocking_reason="blocked",
        )
        == -50,
        "auxiliary shell should keep previous priority over blocking",
    )
    assert_true(
        render_diagnostics.window_content_health_score_from_signals(
            **base_kwargs,
            blank_render_detected=False,
            quick_login_detected=False,
            auxiliary_shell_detected=False,
            blocking_reason="blocking_screen",
        )
        == -10,
        "blocking screen should score as previously",
    )


def test_sidecar_window_content_health_score_delegates_signal_scoring() -> None:
    original_capture_wechat = sidecar.capture_wechat
    original_run_ocr = sidecar.run_ocr
    original_detect_blank_render = sidecar.detect_blank_render
    original_quick_login_like = sidecar.quick_login_like
    original_auxiliary_wechat_shell_like = sidecar.auxiliary_wechat_shell_like
    original_blocking_screen_reason = sidecar.blocking_screen_reason
    screenshot = Image.new("RGB", (980, 860), "white")
    items = [{"text": "搜索"}, {"text": "文件传输助手"}, {"text": "发送"}]
    try:
        sidecar.capture_wechat = lambda hwnd, artifact_dir=None, label="": (screenshot, None)
        sidecar.run_ocr = lambda image: list(items)
        sidecar.detect_blank_render = lambda image, ocr_items, *, geometry: {"detected": False}
        sidecar.quick_login_like = lambda ocr_items, *, geometry=None: False
        sidecar.auxiliary_wechat_shell_like = lambda ocr_items, *, geometry=None: {"detected": False}
        sidecar.blocking_screen_reason = lambda ocr_items: ""
        score = sidecar.window_content_health_score(1001, {"width": 980, "height": 860})
        assert_true(score == 38, f"sidecar health score did not preserve delegated scoring: {score}")
    finally:
        sidecar.capture_wechat = original_capture_wechat
        sidecar.run_ocr = original_run_ocr
        sidecar.detect_blank_render = original_detect_blank_render
        sidecar.quick_login_like = original_quick_login_like
        sidecar.auxiliary_wechat_shell_like = original_auxiliary_wechat_shell_like
        sidecar.blocking_screen_reason = original_blocking_screen_reason


def main() -> int:
    tests = [
        test_render_diagnostics_module_exports_expected_helpers,
        test_blank_render_detection_matches_sidecar,
        test_image_information_score_matches_sidecar,
        test_foreign_overlay_detection_matches_sidecar,
        test_window_content_health_score_from_signals_matches_sidecar_shape,
        test_sidecar_window_content_health_score_delegates_signal_scoring,
    ]
    passed = 0
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
        passed += 1
    print(f"All {passed} WeChat Win32/OCR render diagnostics checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
