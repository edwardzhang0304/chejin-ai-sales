"""Contract checks for pure Win32/OCR windowing metadata extraction."""

from __future__ import annotations

from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from apps.wechat_ai_customer_service.adapters import wechat_win32_ocr_sidecar as sidecar  # noqa: E402
from apps.wechat_ai_customer_service.adapters.wechat_win32_ocr import windowing  # noqa: E402


def assert_true(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


WINDOW_ITEMS = [
    {"title": "微信", "class_name": "Qt51514QWindowIcon"},
    {"title": "(2) 微信", "class_name": "Qt51514QWindowIcon"},
    {"title": "（3） 微信", "class_name": "Qt51514QWindowIcon"},
    {"title": "WeChat", "class_name": "Qt51514QWindowIcon"},
    {"title": "Weixin", "class_name": "Qt51514QWindowIcon"},
    {"title": "WeChat Login", "class_name": "Qt51514QWindowIcon"},
    {"title": "微信登录", "class_name": "Qt51514QWindowIcon"},
    {"title": "微信", "class_name": "OtherClass"},
    {"title": "", "class_name": "Qt51514QWindowIcon"},
    {"title": "添加朋友", "class_name": "WechatMainWndForPC"},
]


def test_windowing_module_exports_expected_helpers() -> None:
    for name in ("normalize_wechat_title", "is_wechat_main_window", "wechat_window_title_score"):
        assert_true(callable(getattr(windowing, name, None)), f"windowing helper missing: {name}")


def test_window_title_helpers_match_sidecar() -> None:
    titles = ["", "微信", "(2) 微信", "（3） 微信", "WeChat", "Weixin", "  微信  "]
    for title in titles:
        assert_true(
            windowing.normalize_wechat_title(title) == sidecar.normalize_wechat_title(title),
            f"title normalization mismatch: {title!r}",
        )


def test_main_window_detection_matches_sidecar() -> None:
    for item in WINDOW_ITEMS:
        assert_true(
            windowing.is_wechat_main_window(item) == sidecar.is_wechat_main_window(item),
            f"main window detection mismatch: {item}",
        )


def test_window_title_score_matches_sidecar() -> None:
    for item in WINDOW_ITEMS:
        assert_true(
            windowing.wechat_window_title_score(item) == sidecar.wechat_window_title_score(item),
            f"title score mismatch: {item}",
        )


def main() -> int:
    tests = [
        test_windowing_module_exports_expected_helpers,
        test_window_title_helpers_match_sidecar,
        test_main_window_detection_matches_sidecar,
        test_window_title_score_matches_sidecar,
    ]
    passed = 0
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
        passed += 1
    print(f"All {passed} WeChat Win32/OCR windowing checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
