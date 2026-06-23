"""Contract checks for pure Win32/OCR humanized input extraction."""

from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import random
import sys
from typing import Iterator


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from apps.wechat_ai_customer_service.adapters import wechat_win32_ocr_sidecar as sidecar  # noqa: E402
from apps.wechat_ai_customer_service.adapters.wechat_win32_ocr import humanized_input  # noqa: E402


def assert_true(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


HUMANIZED_ENV_NAMES = (
    "WECHAT_WIN32_OCR_HUMANIZED_INPUT_ENABLED",
    "WECHAT_WIN32_OCR_HUMANIZED_INPUT_METHOD",
    "WECHAT_WIN32_OCR_HUMANIZED_TYPING_CHUNK_MIN_CHARS",
    "WECHAT_WIN32_OCR_HUMANIZED_TYPING_CHUNK_MAX_CHARS",
    "WECHAT_WIN32_OCR_HUMANIZED_TYPING_CHAR_DELAY_MIN_MS",
    "WECHAT_WIN32_OCR_HUMANIZED_TYPING_CHAR_DELAY_MAX_MS",
    "WECHAT_WIN32_OCR_HUMANIZED_TYPING_MICRO_PAUSE_EVERY_CHARS",
    "WECHAT_WIN32_OCR_HUMANIZED_TYPING_MICRO_PAUSE_MIN_MS",
    "WECHAT_WIN32_OCR_HUMANIZED_TYPING_MICRO_PAUSE_MAX_MS",
    "WECHAT_WIN32_OCR_HUMANIZED_TYPING_TYPO_PROBABILITY",
    "WECHAT_WIN32_OCR_HUMANIZED_TYPING_TYPO_MAX",
    "WECHAT_WIN32_OCR_HUMANIZED_SEND_PRE_DELAY_MIN_MS",
    "WECHAT_WIN32_OCR_HUMANIZED_SEND_PRE_DELAY_MAX_MS",
    "WECHAT_WIN32_OCR_HUMANIZED_SEND_POST_INPUT_DELAY_MIN_MS",
    "WECHAT_WIN32_OCR_HUMANIZED_SEND_POST_INPUT_DELAY_MAX_MS",
    "WECHAT_WIN32_OCR_HUMANIZED_SEND_TRIGGER_DELAY_MIN_MS",
    "WECHAT_WIN32_OCR_HUMANIZED_SEND_TRIGGER_DELAY_MAX_MS",
    "WECHAT_WIN32_OCR_HUMANIZED_SEND_AFTER_TRIGGER_DELAY_MIN_MS",
    "WECHAT_WIN32_OCR_HUMANIZED_SEND_AFTER_TRIGGER_DELAY_MAX_MS",
    "WECHAT_WIN32_OCR_HUMANIZED_INTER_CHUNK_DELAY_SCALE",
    "WECHAT_WIN32_OCR_HUMANIZED_ADAPTIVE_SPEED_ENABLED",
    "WECHAT_WIN32_OCR_ENFORCE_INTERMITTENT_TYPING",
    "WECHAT_WIN32_OCR_ALLOW_CLIPBOARD_ONCE",
)


@contextmanager
def patched_env(values: dict[str, str | None]) -> Iterator[None]:
    before = {name: os.environ.get(name) for name in values}
    try:
        for name, value in values.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        yield
    finally:
        for name, value in before.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def cleared_env(extra: dict[str, str | None] | None = None) -> dict[str, str | None]:
    values = {name: None for name in HUMANIZED_ENV_NAMES}
    if extra:
        values.update(extra)
    return values


def test_humanized_module_exports_expected_helpers() -> None:
    for name in (
        "sendinput_safe_text",
        "humanized_input_settings",
        "adapt_humanized_input_settings",
        "humanized_chunk_text",
        "choose_humanized_typo_char",
        "typed_text_delay_ms",
        "maybe_humanized_typo_allowed",
    ):
        assert_true(callable(getattr(humanized_input, name, None)), f"humanized helper missing: {name}")


def test_humanized_settings_match_sidecar() -> None:
    env_cases = [
        {},
        {
            "WECHAT_WIN32_OCR_HUMANIZED_INPUT_ENABLED": "1",
            "WECHAT_WIN32_OCR_HUMANIZED_INPUT_METHOD": "clipboard_chunks",
            "WECHAT_WIN32_OCR_HUMANIZED_TYPING_CHUNK_MIN_CHARS": "3",
            "WECHAT_WIN32_OCR_HUMANIZED_TYPING_CHUNK_MAX_CHARS": "3",
            "WECHAT_WIN32_OCR_HUMANIZED_TYPING_CHAR_DELAY_MIN_MS": "0",
            "WECHAT_WIN32_OCR_HUMANIZED_TYPING_CHAR_DELAY_MAX_MS": "0",
            "WECHAT_WIN32_OCR_HUMANIZED_TYPING_TYPO_PROBABILITY": "0",
        },
        {
            "WECHAT_WIN32_OCR_HUMANIZED_INPUT_ENABLED": "0",
            "WECHAT_WIN32_OCR_HUMANIZED_INPUT_METHOD": "clipboard_once",
            "WECHAT_WIN32_OCR_HUMANIZED_TYPING_CHUNK_MIN_CHARS": "90",
            "WECHAT_WIN32_OCR_HUMANIZED_TYPING_CHUNK_MAX_CHARS": "bad",
            "WECHAT_WIN32_OCR_HUMANIZED_TYPING_TYPO_PROBABILITY": "2.5",
            "WECHAT_WIN32_OCR_HUMANIZED_INTER_CHUNK_DELAY_SCALE": "0.1",
        },
        {
            "WECHAT_WIN32_OCR_HUMANIZED_SEND_TRIGGER_DELAY_MIN_MS": "2000",
            "WECHAT_WIN32_OCR_HUMANIZED_SEND_TRIGGER_DELAY_MAX_MS": "1000",
            "WECHAT_WIN32_OCR_HUMANIZED_SEND_AFTER_TRIGGER_DELAY_MIN_MS": "900",
            "WECHAT_WIN32_OCR_HUMANIZED_SEND_AFTER_TRIGGER_DELAY_MAX_MS": "100",
            "WECHAT_WIN32_OCR_HUMANIZED_ADAPTIVE_SPEED_ENABLED": "0",
        },
    ]
    for env_case in env_cases:
        with patched_env(cleared_env(env_case)):
            assert_true(
                humanized_input.humanized_input_settings() == sidecar.humanized_input_settings(),
                f"humanized settings mismatch: {env_case}",
            )


def test_text_and_adaptive_helpers_match_sidecar() -> None:
    texts = [
        "",
        "a\r\nb\t c",
        "这台车今天能看吗？",
        "这边先帮您确认几个点。" * 12,
        "这边先帮您确认几个点。" * 40,
    ]
    base = {
        "enabled": True,
        "method": "sendinput_unicode",
        "chunk_min_chars": 2,
        "chunk_max_chars": 6,
        "char_delay_min_ms": 50,
        "char_delay_max_ms": 180,
        "micro_pause_every_chars": 18,
        "micro_pause_min_ms": 220,
        "micro_pause_max_ms": 650,
        "typo_probability": 0.22,
        "typo_max": 1,
        "send_pre_delay_min_ms": 280,
        "send_pre_delay_max_ms": 1300,
        "send_post_input_delay_min_ms": 120,
        "send_post_input_delay_max_ms": 460,
        "send_trigger_delay_min_ms": 420,
        "send_trigger_delay_max_ms": 1350,
        "send_after_trigger_delay_min_ms": 220,
        "send_after_trigger_delay_max_ms": 760,
        "inter_chunk_delay_scale": 1.0,
        "adaptive_speed_enabled": True,
    }
    disabled = dict(base)
    disabled["adaptive_speed_enabled"] = False
    for text in texts:
        assert_true(
            humanized_input.sendinput_safe_text(text) == sidecar.sendinput_safe_text(text),
            f"safe text mismatch: {text!r}",
        )
        assert_true(
            humanized_input.adapt_humanized_input_settings(base, text)
            == sidecar.adapt_humanized_input_settings(base, text),
            f"adaptive settings mismatch: {text!r}",
        )
        assert_true(
            humanized_input.adapt_humanized_input_settings(disabled, text)
            == sidecar.adapt_humanized_input_settings(disabled, text),
            f"disabled adaptive settings mismatch: {text!r}",
        )


def test_randomized_helpers_match_sidecar_with_seed() -> None:
    for seed in (1, 20260619, 987654):
        random.seed(seed)
        extracted_chunks = humanized_input.humanized_chunk_text("abcdefghijklmnop", min_chars=2, max_chars=5)
        random.seed(seed)
        sidecar_chunks = sidecar.humanized_chunk_text("abcdefghijklmnop", min_chars=2, max_chars=5)
        assert_true(extracted_chunks == sidecar_chunks, f"chunk mismatch for seed {seed}: {extracted_chunks}")

        random.seed(seed)
        extracted_typo = humanized_input.choose_humanized_typo_char()
        random.seed(seed)
        sidecar_typo = sidecar.choose_humanized_typo_char()
        assert_true(extracted_typo == sidecar_typo, f"typo char mismatch for seed {seed}: {extracted_typo}")

        settings = {"typo_max": 2, "typo_probability": 0.45}
        random.seed(seed)
        extracted_allowed = humanized_input.maybe_humanized_typo_allowed(settings, typo_count=0, text="abcdefgh")
        random.seed(seed)
        sidecar_allowed = sidecar.maybe_humanized_typo_allowed(settings, typo_count=0, text="abcdefgh")
        assert_true(extracted_allowed == sidecar_allowed, f"typo allow mismatch for seed {seed}: {extracted_allowed}")


def test_typed_delay_helpers_match_sidecar() -> None:
    settings_cases = [
        {},
        {"char_delay_min_ms": 0, "char_delay_max_ms": 0},
        {"char_delay_min_ms": 10, "char_delay_max_ms": 50, "inter_chunk_delay_scale": 0.5},
        {"char_delay_min_ms": 10, "char_delay_max_ms": 50, "inter_chunk_delay_scale": "bad"},
        {"char_delay_min_ms": 10, "char_delay_max_ms": 5, "inter_chunk_delay_scale": 3.0},
    ]
    for segment in ("", "abc", "你好AB"):
        for settings in settings_cases:
            assert_true(
                humanized_input.typed_text_delay_ms(segment, settings)
                == sidecar.typed_text_delay_ms(segment, settings),
                f"typed delay mismatch: {(segment, settings)}",
            )


def main() -> int:
    tests = [
        test_humanized_module_exports_expected_helpers,
        test_humanized_settings_match_sidecar,
        test_text_and_adaptive_helpers_match_sidecar,
        test_randomized_helpers_match_sidecar_with_seed,
        test_typed_delay_helpers_match_sidecar,
    ]
    passed = 0
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
        passed += 1
    print(f"All {passed} WeChat Win32/OCR humanized input checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
