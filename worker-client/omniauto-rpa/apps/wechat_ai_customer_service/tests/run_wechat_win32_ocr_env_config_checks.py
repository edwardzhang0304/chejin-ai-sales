"""Contract checks for pure Win32/OCR environment config extraction."""

from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import sys
from typing import Iterator


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from apps.wechat_ai_customer_service.adapters import wechat_win32_ocr_sidecar as sidecar  # noqa: E402
from apps.wechat_ai_customer_service.adapters.wechat_win32_ocr import env_config  # noqa: E402


def assert_true(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


ENV_NAMES = (
    "WECHAT_WIN32_OCR_ENFORCE_INTERMITTENT_TYPING",
    "WECHAT_WIN32_OCR_ALLOW_CLIPBOARD_ONCE",
    "WECHAT_WIN32_OCR_ALLOW_CLICK_SEND_TRIGGER",
    "WECHAT_WIN32_OCR_STRICT_SEND_FOCUS_GUARD",
    "WECHAT_WIN32_OCR_FOCUS_CLICK_FALLBACK",
    "WECHAT_WIN32_OCR_ALLOW_UNKNOWN_FOREGROUND",
    "WECHAT_WIN32_OCR_SEND_INPUT_CONFIRM_ATTEMPTS",
    "WECHAT_WIN32_OCR_BLANK_INPUT_FOCUS_RETRY",
    "WECHAT_WIN32_OCR_UI_ACTION_PACING_ENABLED",
    "WECHAT_WIN32_OCR_TEST_INT",
    "WECHAT_WIN32_OCR_TEST_FLOAT",
    "WECHAT_WIN32_OCR_TEST_FLAG",
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
    values = {name: None for name in ENV_NAMES}
    if extra:
        values.update(extra)
    return values


def test_env_module_exports_expected_helpers() -> None:
    for name in (
        "env_int",
        "env_float",
        "env_flag",
        "normalize_humanized_input_method",
        "normalize_send_trigger_mode",
        "strict_send_focus_guard_enabled",
        "focus_click_fallback_enabled",
        "allow_unknown_foreground_guard",
        "send_input_confirm_attempt_count",
        "rpa_action_pacing_enabled",
    ):
        assert_true(callable(getattr(env_config, name, None)), f"env helper missing: {name}")


def test_scalar_env_helpers_match_sidecar() -> None:
    for value in (None, "", "42", " 17 ", "bad"):
        with patched_env(cleared_env({"WECHAT_WIN32_OCR_TEST_INT": value})):
            assert_true(
                env_config.env_int("WECHAT_WIN32_OCR_TEST_INT", 7)
                == sidecar.env_int("WECHAT_WIN32_OCR_TEST_INT", 7),
                f"env_int mismatch: {value!r}",
            )
        with patched_env(cleared_env({"WECHAT_WIN32_OCR_TEST_FLOAT": value})):
            assert_true(
                env_config.env_float("WECHAT_WIN32_OCR_TEST_FLOAT", 7.5)
                == sidecar.env_float("WECHAT_WIN32_OCR_TEST_FLOAT", 7.5),
                f"env_float mismatch: {value!r}",
            )

    for value in (None, "", "0", "false", "no", "off", "1", "true", "yes", "bad"):
        with patched_env(cleared_env({"WECHAT_WIN32_OCR_TEST_FLAG": value})):
            assert_true(
                env_config.env_flag("WECHAT_WIN32_OCR_TEST_FLAG", default=False)
                == sidecar.env_flag("WECHAT_WIN32_OCR_TEST_FLAG", default=False),
                f"env_flag false-default mismatch: {value!r}",
            )
            assert_true(
                env_config.env_flag("WECHAT_WIN32_OCR_TEST_FLAG", default=True)
                == sidecar.env_flag("WECHAT_WIN32_OCR_TEST_FLAG", default=True),
                f"env_flag true-default mismatch: {value!r}",
            )


def test_mode_normalizers_match_sidecar() -> None:
    env_cases = [
        {},
        {
            "WECHAT_WIN32_OCR_ENFORCE_INTERMITTENT_TYPING": "1",
            "WECHAT_WIN32_OCR_ALLOW_CLIPBOARD_ONCE": "0",
        },
        {
            "WECHAT_WIN32_OCR_ENFORCE_INTERMITTENT_TYPING": "0",
            "WECHAT_WIN32_OCR_ALLOW_CLIPBOARD_ONCE": "0",
        },
        {
            "WECHAT_WIN32_OCR_ALLOW_CLICK_SEND_TRIGGER": "1",
        },
    ]
    methods = (None, "", "auto", "SENDINPUT_UNICODE", "clipboard_once", "bad")
    trigger_modes = (None, "", "click_only", "enter_only", "enter_then_click", "bad")
    for env_case in env_cases:
        with patched_env(cleared_env(env_case)):
            for method in methods:
                assert_true(
                    env_config.normalize_humanized_input_method(method)
                    == sidecar.normalize_humanized_input_method(method),
                    f"humanized input method mismatch: {(env_case, method)}",
                )
            for mode in trigger_modes:
                assert_true(
                    env_config.normalize_send_trigger_mode(mode) == sidecar.normalize_send_trigger_mode(mode),
                    f"send trigger mode mismatch: {(env_case, mode)}",
                )


def test_guard_toggles_match_sidecar() -> None:
    cases = [
        {},
        {"WECHAT_WIN32_OCR_STRICT_SEND_FOCUS_GUARD": "0"},
        {"WECHAT_WIN32_OCR_FOCUS_CLICK_FALLBACK": "false"},
        {"WECHAT_WIN32_OCR_ALLOW_UNKNOWN_FOREGROUND": "off"},
        {"WECHAT_WIN32_OCR_UI_ACTION_PACING_ENABLED": "no"},
        {
            "WECHAT_WIN32_OCR_STRICT_SEND_FOCUS_GUARD": "1",
            "WECHAT_WIN32_OCR_FOCUS_CLICK_FALLBACK": "1",
            "WECHAT_WIN32_OCR_ALLOW_UNKNOWN_FOREGROUND": "1",
            "WECHAT_WIN32_OCR_UI_ACTION_PACING_ENABLED": "1",
        },
    ]
    for env_case in cases:
        with patched_env(cleared_env(env_case)):
            assert_true(
                env_config.strict_send_focus_guard_enabled() == sidecar.strict_send_focus_guard_enabled(),
                f"strict guard mismatch: {env_case}",
            )
            assert_true(
                env_config.focus_click_fallback_enabled() == sidecar.focus_click_fallback_enabled(),
                f"focus fallback mismatch: {env_case}",
            )
            assert_true(
                env_config.allow_unknown_foreground_guard() == sidecar.allow_unknown_foreground_guard(),
                f"unknown foreground mismatch: {env_case}",
            )
            assert_true(
                env_config.rpa_action_pacing_enabled() == sidecar.rpa_action_pacing_enabled(),
                f"action pacing mismatch: {env_case}",
            )


def test_send_input_confirm_attempt_count_matches_sidecar() -> None:
    cases = [
        ({}, 1),
        ({}, 3),
        ({"WECHAT_WIN32_OCR_SEND_INPUT_CONFIRM_ATTEMPTS": "1"}, 3),
        (
            {
                "WECHAT_WIN32_OCR_SEND_INPUT_CONFIRM_ATTEMPTS": "1",
                "WECHAT_WIN32_OCR_BLANK_INPUT_FOCUS_RETRY": "0",
            },
            3,
        ),
        ({"WECHAT_WIN32_OCR_SEND_INPUT_CONFIRM_ATTEMPTS": "99"}, 3),
        ({"WECHAT_WIN32_OCR_SEND_INPUT_CONFIRM_ATTEMPTS": "bad"}, 2),
    ]
    for env_case, total_attempts in cases:
        with patched_env(cleared_env(env_case)):
            assert_true(
                env_config.send_input_confirm_attempt_count(total_attempts)
                == sidecar.send_input_confirm_attempt_count(total_attempts),
                f"send input confirm attempts mismatch: {(env_case, total_attempts)}",
            )


def main() -> int:
    tests = [
        test_env_module_exports_expected_helpers,
        test_scalar_env_helpers_match_sidecar,
        test_mode_normalizers_match_sidecar,
        test_guard_toggles_match_sidecar,
        test_send_input_confirm_attempt_count_matches_sidecar,
    ]
    passed = 0
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
        passed += 1
    print(f"All {passed} WeChat Win32/OCR env config checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
