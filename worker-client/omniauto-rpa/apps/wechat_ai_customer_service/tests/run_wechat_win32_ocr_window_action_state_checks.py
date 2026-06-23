"""Contract checks for pure Win32/OCR window action state helpers."""

from __future__ import annotations

from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from apps.wechat_ai_customer_service.adapters import wechat_win32_ocr_sidecar as sidecar  # noqa: E402
from apps.wechat_ai_customer_service.adapters.wechat_win32_ocr import window_action_state  # noqa: E402


def assert_true(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def test_window_action_state_module_exports_expected_helpers() -> None:
    for name in (
        "FOREGROUND_READY_REASONS",
        "foreground_guard_ready",
        "tray_hidden_from_probe",
        "tray_hidden_from_counts",
        "activate_window_settings",
        "activate_debounce_active",
    ):
        assert_true(hasattr(window_action_state, name), f"window action state helper missing: {name}")


def test_foreground_guard_ready_accepts_only_confirmed_target_reasons() -> None:
    ready_cases = [
        {"ok": True, "reason": "foreground_matches_target"},
        {"ok": True, "reason": "foreground_root_matches_target"},
    ]
    blocked_cases = [
        {},
        None,
        {"ok": False, "reason": "foreground_matches_target"},
        {"ok": True, "reason": "foreground_guard_unavailable"},
        {"ok": True, "reason": "foreground_unknown_guard_degraded"},
        {"ok": False, "reason": "foreground_not_wechat_target"},
    ]
    for case in ready_cases:
        assert_true(window_action_state.foreground_guard_ready(case), f"ready case should pass: {case}")
    for case in blocked_cases:
        assert_true(not window_action_state.foreground_guard_ready(case), f"blocked case should fail: {case}")


def test_tray_hidden_from_probe_matches_sidecar() -> None:
    probes = [
        {"main_count": 1, "visible_main_count": 0},
        {"main_count": 2, "visible_main_count": 1},
        {"main_count": 0, "visible_main_count": 0},
        {"main_count": "bad", "visible_main_count": 0},
    ]
    for probe in probes:
        assert_true(
            window_action_state.tray_hidden_from_probe(probe) == sidecar.wechat_main_window_is_tray_hidden(probe),
            f"tray-hidden probe mismatch: {probe}",
        )


def test_activate_window_settings_match_sidecar_env_semantics() -> None:
    normal = window_action_state.activate_window_settings(
        aggressive_focus=False,
        attach_thread_input=False,
        debounce_seconds=2.5,
    )
    aggressive = window_action_state.activate_window_settings(
        aggressive_focus=True,
        attach_thread_input=False,
        debounce_seconds="99",
    )
    explicit_attach = window_action_state.activate_window_settings(
        aggressive_focus=False,
        attach_thread_input=True,
        debounce_seconds="-1",
    )
    assert_true(normal == {"aggressive_focus": False, "attach_thread_input": False, "debounce_seconds": 2.5}, f"normal settings mismatch: {normal}")
    assert_true(aggressive == {"aggressive_focus": True, "attach_thread_input": True, "debounce_seconds": 10.0}, f"aggressive settings mismatch: {aggressive}")
    assert_true(explicit_attach == {"aggressive_focus": False, "attach_thread_input": True, "debounce_seconds": 0.0}, f"explicit attach settings mismatch: {explicit_attach}")


def test_activate_debounce_active_matches_sidecar_window() -> None:
    assert_true(
        window_action_state.activate_debounce_active(now_monotonic=12.0, last_monotonic=10.0, debounce_seconds=2.5),
        "recent activation should be inside debounce window",
    )
    assert_true(
        not window_action_state.activate_debounce_active(now_monotonic=13.0, last_monotonic=10.0, debounce_seconds=2.5),
        "old activation should be outside debounce window",
    )
    assert_true(
        not window_action_state.activate_debounce_active(now_monotonic=1.0, last_monotonic=0.0, debounce_seconds=2.5),
        "missing last activation should not debounce",
    )
    assert_true(
        not window_action_state.activate_debounce_active(now_monotonic=12.0, last_monotonic=10.0, debounce_seconds=0.0),
        "disabled debounce should not debounce",
    )


def test_sidecar_focus_ready_source_uses_extracted_helper() -> None:
    source_path = PROJECT_ROOT / "apps" / "wechat_ai_customer_service" / "adapters" / "wechat_win32_ocr_sidecar.py"
    activation_path = PROJECT_ROOT / "apps" / "wechat_ai_customer_service" / "adapters" / "wechat_win32_ocr" / "window_activation.py"
    source = source_path.read_text(encoding="utf-8")
    activation_source = activation_path.read_text(encoding="utf-8")
    activate_body = source[source.find("def activate_window") : source.find("def configure_dpi_awareness")]
    activation_body = activation_source[activation_source.find("def activate_window_with_dependencies") :]
    focus_body = source[source.find("def focus_wechat_window") : source.find("def activate_window")]
    assert_true("activate_window_with_dependencies" in activate_body, "activate_window should delegate execution helper")
    assert_true("foreground_guard_ready" in activation_body, "activate_window execution should use extracted focus-ready helper")
    assert_true("foreground_guard_ready" in focus_body, "focus_wechat_window should use extracted focus-ready helper")


def main() -> int:
    tests = [
        test_window_action_state_module_exports_expected_helpers,
        test_foreground_guard_ready_accepts_only_confirmed_target_reasons,
        test_tray_hidden_from_probe_matches_sidecar,
        test_activate_window_settings_match_sidecar_env_semantics,
        test_activate_debounce_active_matches_sidecar_window,
        test_sidecar_focus_ready_source_uses_extracted_helper,
    ]
    passed = 0
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
        passed += 1
    print(f"All {passed} WeChat Win32/OCR window action state checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
