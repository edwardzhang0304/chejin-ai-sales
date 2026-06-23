"""Contract checks for ensure_visible_wechat_window planning."""

from __future__ import annotations

from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from apps.wechat_ai_customer_service.adapters import wechat_win32_ocr_sidecar as sidecar  # noqa: E402
from apps.wechat_ai_customer_service.adapters.wechat_win32_ocr import window_action_planning  # noqa: E402
from apps.wechat_ai_customer_service.adapters.wechat_win32_ocr import window_visibility  # noqa: E402


def assert_true(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def test_ensure_visible_planning_exports_expected_helpers() -> None:
    for name in (
        "ENSURE_VISIBLE_ACTION_RETURN",
        "ENSURE_VISIBLE_ACTION_FOCUS",
        "ENSURE_VISIBLE_ACTION_RESTORE",
        "ENSURE_VISIBLE_ACTION_MANUAL_TRAY",
        "plan_ensure_visible_wechat_window",
    ):
        assert_true(hasattr(window_action_planning, name), f"ensure-visible planner missing: {name}")
    for name in ("EnsureVisibleDependencies", "ensure_visible_wechat_window_with_dependencies"):
        assert_true(hasattr(window_visibility, name), f"ensure-visible executor missing: {name}")


def test_plan_focuses_usable_visible_window_only_when_interactive() -> None:
    probe = {"visible_main_windows": [{"hwnd": 1001}]}
    active = window_action_planning.plan_ensure_visible_wechat_window(
        probe,
        interactive=True,
        usable_visible=True,
        tray_hidden=False,
    )
    passive = window_action_planning.plan_ensure_visible_wechat_window(
        probe,
        interactive=False,
        usable_visible=True,
        tray_hidden=False,
    )
    assert_true(active.get("action") == window_action_planning.ENSURE_VISIBLE_ACTION_FOCUS, f"interactive visible plan mismatch: {active}")
    assert_true(passive.get("action") == window_action_planning.ENSURE_VISIBLE_ACTION_RETURN, f"passive visible plan mismatch: {passive}")


def test_plan_restores_invalid_visible_window_only_when_interactive() -> None:
    probe = {"visible_main_windows": [{"hwnd": 1001}]}
    active = window_action_planning.plan_ensure_visible_wechat_window(
        probe,
        interactive=True,
        usable_visible=False,
        tray_hidden=False,
    )
    passive = window_action_planning.plan_ensure_visible_wechat_window(
        probe,
        interactive=False,
        usable_visible=False,
        tray_hidden=False,
    )
    assert_true(active.get("action") == window_action_planning.ENSURE_VISIBLE_ACTION_RESTORE, f"interactive invalid visible plan mismatch: {active}")
    assert_true(active.get("visible_main_window_geometry_invalid") is True, f"invalid geometry flag missing: {active}")
    assert_true(passive.get("action") == window_action_planning.ENSURE_VISIBLE_ACTION_RETURN, f"passive invalid visible plan mismatch: {passive}")
    assert_true(passive.get("visible_main_window_geometry_invalid") is True, f"passive invalid geometry flag missing: {passive}")


def test_plan_manual_tray_when_no_visible_main_window_and_tray_hidden() -> None:
    probe = {"main_count": 1, "visible_main_count": 0, "visible_main_windows": []}
    plan = window_action_planning.plan_ensure_visible_wechat_window(
        probe,
        interactive=True,
        usable_visible=False,
        tray_hidden=True,
    )
    assert_true(plan.get("action") == window_action_planning.ENSURE_VISIBLE_ACTION_MANUAL_TRAY, f"tray plan mismatch: {plan}")
    assert_true((plan.get("probe_updates") or {}).get("manual_action_required") == "open_wechat_main_window", f"tray manual action mismatch: {plan}")


def test_sidecar_ensure_visible_matches_planned_focus_and_restore_paths() -> None:
    sidecar_mod = sidecar
    originals = {
        "probe_wechat_windows": sidecar_mod.probe_wechat_windows,
        "probe_has_usable_visible_main_window": sidecar_mod.probe_has_usable_visible_main_window,
        "focus_wechat_window": sidecar_mod.focus_wechat_window,
        "restore_wechat_window": sidecar_mod.restore_wechat_window,
        "sleep": sidecar_mod.humanized_action_sleep,
    }
    calls = {"probe": 0, "focus": 0, "restore": 0}

    def fake_probe():
        calls["probe"] += 1
        return {"visible_main_windows": [{"hwnd": 1001}], "windows": [{"hwnd": 1001}]}

    try:
        sidecar_mod.probe_wechat_windows = fake_probe
        sidecar_mod.probe_has_usable_visible_main_window = lambda _probe: True
        sidecar_mod.focus_wechat_window = lambda _probe: calls.__setitem__("focus", calls["focus"] + 1) or {"hwnd": 1001}
        sidecar_mod.restore_wechat_window = lambda _probe: calls.__setitem__("restore", calls["restore"] + 1) or {"hwnd": 1001}
        sidecar_mod.humanized_action_sleep = lambda *_args, **_kwargs: 0.0
        result = sidecar_mod.ensure_visible_wechat_window(interactive=True)
        assert_true(calls["focus"] == 1 and calls["restore"] == 0, f"usable visible should focus only: calls={calls}, result={result}")
        assert_true(result.get("focused_window") == {"hwnd": 1001}, f"focused window should be recorded: {result}")

        calls.update({"probe": 0, "focus": 0, "restore": 0})
        sidecar_mod.probe_has_usable_visible_main_window = lambda _probe: False
        result = sidecar_mod.ensure_visible_wechat_window(interactive=True)
        assert_true(calls["restore"] == 1 and calls["focus"] == 1, f"invalid visible should restore then focus: calls={calls}, result={result}")
    finally:
        sidecar_mod.probe_wechat_windows = originals["probe_wechat_windows"]
        sidecar_mod.probe_has_usable_visible_main_window = originals["probe_has_usable_visible_main_window"]
        sidecar_mod.focus_wechat_window = originals["focus_wechat_window"]
        sidecar_mod.restore_wechat_window = originals["restore_wechat_window"]
        sidecar_mod.humanized_action_sleep = originals["sleep"]


def test_sidecar_ensure_visible_matches_planned_tray_path() -> None:
    sidecar_mod = sidecar
    originals = {
        "probe_wechat_windows": sidecar_mod.probe_wechat_windows,
        "wechat_main_window_is_tray_hidden": sidecar_mod.wechat_main_window_is_tray_hidden,
        "restore_wechat_window": sidecar_mod.restore_wechat_window,
    }
    calls = {"restore": 0}
    try:
        sidecar_mod.probe_wechat_windows = lambda: {"main_count": 1, "visible_main_count": 0, "visible_main_windows": []}
        sidecar_mod.wechat_main_window_is_tray_hidden = lambda _probe: True
        sidecar_mod.restore_wechat_window = lambda _probe: calls.__setitem__("restore", calls["restore"] + 1) or {"hwnd": 1001}
        result = sidecar_mod.ensure_visible_wechat_window(interactive=True)
        assert_true(calls["restore"] == 0, f"tray-hidden window should not auto-restore: calls={calls}, result={result}")
        assert_true(result.get("main_window_in_tray") is True, f"tray flag missing: {result}")
        assert_true(result.get("manual_action_required") == "open_wechat_main_window", f"manual action mismatch: {result}")
    finally:
        sidecar_mod.probe_wechat_windows = originals["probe_wechat_windows"]
        sidecar_mod.wechat_main_window_is_tray_hidden = originals["wechat_main_window_is_tray_hidden"]
        sidecar_mod.restore_wechat_window = originals["restore_wechat_window"]


def main() -> int:
    tests = [
        test_ensure_visible_planning_exports_expected_helpers,
        test_plan_focuses_usable_visible_window_only_when_interactive,
        test_plan_restores_invalid_visible_window_only_when_interactive,
        test_plan_manual_tray_when_no_visible_main_window_and_tray_hidden,
        test_sidecar_ensure_visible_matches_planned_focus_and_restore_paths,
        test_sidecar_ensure_visible_matches_planned_tray_path,
    ]
    passed = 0
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
        passed += 1
    print(f"All {passed} WeChat Win32/OCR ensure-visible planning checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
