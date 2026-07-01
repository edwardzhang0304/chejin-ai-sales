"""Contract checks for pure Win32/OCR text normalization extraction."""

from __future__ import annotations

from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from apps.wechat_ai_customer_service.adapters import wechat_win32_ocr_sidecar as sidecar  # noqa: E402
from apps.wechat_ai_customer_service.adapters.wechat_win32_ocr import text_normalization  # noqa: E402


def assert_true(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


TEXTS = [
    "",
    "  许聪  ",
    "：新数据测试",
    ". 文件传输助手",
    "文件传输..昨天02:57",
    "文件传输.…．昨天23:05",
    "文件传输助手（12）",
    "WeChat File Transfer",
    "当前会话：许聪的聊天",
    "Chat with File Transfer Assistant",
    "星期三",
    "今天 12:20",
    "[图片]",
    "搜索",
    "新数据测试昨天",
]


def test_text_module_exports_expected_helpers() -> None:
    for name in (
        "normalize_ocr_text",
        "normalize_session_name",
        "strip_chat_unread_suffix",
        "normalize_chat_title_for_match",
        "canonical_session_name",
        "is_file_transfer_session_alias",
        "normalize_message_content",
        "quick_login_like",
        "session_name_matches",
        "strip_session_time_suffix",
        "is_session_name_candidate",
        "is_session_time_text",
        "is_message_noise",
        "infer_conversation_type",
    ):
        assert_true(callable(getattr(text_normalization, name, None)), f"text helper missing: {name}")


def test_normalization_helpers_match_sidecar() -> None:
    for text in TEXTS:
        assert_true(text_normalization.normalize_ocr_text(text) == sidecar.normalize_ocr_text(text), f"ocr text mismatch: {text!r}")
        assert_true(text_normalization.normalize_session_name(text) == sidecar.normalize_session_name(text), f"session name mismatch: {text!r}")
        assert_true(text_normalization.strip_chat_unread_suffix(text) == sidecar.strip_chat_unread_suffix(text), f"unread suffix mismatch: {text!r}")
        assert_true(text_normalization.normalize_chat_title_for_match(text) == sidecar.normalize_chat_title_for_match(text), f"title match mismatch: {text!r}")
        assert_true(text_normalization.canonical_session_name(text) == sidecar.canonical_session_name(text), f"canonical mismatch: {text!r}")
        assert_true(text_normalization.normalize_message_content(text) == sidecar.normalize_message_content(text), f"message content mismatch: {text!r}")
        assert_true(text_normalization.strip_session_time_suffix(text) == sidecar.strip_session_time_suffix(text), f"time suffix mismatch: {text!r}")
        assert_true(text_normalization.is_session_name_candidate(text) == sidecar.is_session_name_candidate(text), f"name candidate mismatch: {text!r}")
        assert_true(text_normalization.is_session_time_text(text) == sidecar.is_session_time_text(text), f"time text mismatch: {text!r}")
        assert_true(text_normalization.is_message_noise(text) == sidecar.is_message_noise(text), f"message noise mismatch: {text!r}")
        assert_true(text_normalization.infer_conversation_type(text) == sidecar.infer_conversation_type(text), f"conversation type mismatch: {text!r}")


def test_file_transfer_aliases_match_sidecar() -> None:
    samples = [
        "文件传输助手",
        "文件传输助",
        "文件传输..昨天02:57",
        "文件传输.…．昨天23:05",
        "仅传输文件",
        "File Transfer Assistant",
        "file-transfer",
        "transfer assistant",
        "许聪",
    ]
    for text in samples:
        assert_true(
            text_normalization.is_file_transfer_session_alias(text) == sidecar.is_file_transfer_session_alias(text),
            f"file-transfer alias mismatch: {text!r}",
        )


def test_session_name_matching_matches_sidecar() -> None:
    pairs = [
        ("许聪", "许聪"),
        ("许聪昨天", "许聪"),
        ("新数据测试昨天", "新数据测试"),
        ("文件传输..昨天02:57", "文件传输助手"),
        ("文件传输.…．昨天23:05", "文件传输助手"),
        ("当前会话：许聪的聊天", "许聪"),
        ("许聪", "聪"),
        ("客户A", "客户B"),
        ("", "许聪"),
    ]
    for name, target in pairs:
        for exact in (False, True):
            assert_true(
                text_normalization.session_name_matches(name, target, exact=exact)
                == sidecar.session_name_matches(name, target, exact=exact),
                f"session match mismatch: {(name, target, exact)}",
            )


def test_quick_login_like_matches_sidecar() -> None:
    samples = [
        (
            [{"text": "进入微信"}, {"text": "切换账号"}, {"text": "仅传输文件"}],
            {"width": 368, "height": 484},
        ),
        (
            [{"text": "进入微信"}, {"text": "切换账号"}],
            {"width": 981, "height": 860},
        ),
        (
            [{"text": "文件传输助手"}, {"text": "搜索"}],
            {"width": 368, "height": 484},
        ),
    ]
    for ocr_items, geometry in samples:
        assert_true(
            text_normalization.quick_login_like(ocr_items, geometry=geometry)
            == sidecar.quick_login_like(ocr_items, geometry=geometry),
            f"quick-login mismatch: {(ocr_items, geometry)}",
        )


def main() -> int:
    tests = [
        test_text_module_exports_expected_helpers,
        test_normalization_helpers_match_sidecar,
        test_file_transfer_aliases_match_sidecar,
        test_session_name_matching_matches_sidecar,
        test_quick_login_like_matches_sidecar,
    ]
    passed = 0
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
        passed += 1
    print(f"All {passed} WeChat Win32/OCR text normalization checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
