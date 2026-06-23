"""Contract checks for Win32/OCR device profile diagnostics."""

from __future__ import annotations

from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from apps.wechat_ai_customer_service.adapters import wechat_win32_ocr_sidecar as sidecar  # noqa: E402
from apps.wechat_ai_customer_service.adapters.wechat_win32_ocr import device_profile, geometry  # noqa: E402


def assert_true(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def test_device_profile_module_exports_expected_helpers() -> None:
    for name in ("build_device_profile", "profile_summary", "profile_changed"):
        assert_true(callable(getattr(device_profile, name, None)), f"device profile helper missing: {name}")


def test_capture_geometry_guard_matches_sidecar() -> None:
    samples = [
        {"left": 0, "top": 0, "width": 981, "height": 860},
        {"left": -32000, "top": -32000, "width": 199, "height": 34},
        {"left": 0, "top": 0, "width": 368, "height": 484},
        {"left": 20, "top": 20, "width": 420, "height": 260},
    ]
    for item in samples:
        assert_true(
            geometry.validate_capture_geometry(item) == sidecar.validate_capture_geometry(item),
            f"capture geometry mismatch: {item}",
        )


def test_build_device_profile_shape() -> None:
    profile = device_profile.build_device_profile(
        route="add-friend-entry-click-plan-windows",
        geometry={"left": 0, "top": 0, "width": 981, "height": 860},
        screenshot_size=(981, 860),
        client_rect={"width": 981, "height": 860},
        dpi_scale=1.25,
        screen={"width": 1920, "height": 1200},
        virtual_screen={"left": 0, "top": 0, "width": 1920, "height": 1200},
        monitors=[{"left": 0, "top": 0, "right": 1920, "bottom": 1200, "width": 1920, "height": 1200}],
    )
    assert_true(profile.get("platform") == "windows", f"profile platform mismatch: {profile}")
    assert_true(profile.get("route") == "add-friend-entry-click-plan-windows", f"profile route mismatch: {profile}")
    assert_true(profile.get("window_rect", {}).get("width") == 981, f"profile geometry mismatch: {profile}")
    assert_true(profile.get("screenshot_size") == [981, 860], f"profile screenshot mismatch: {profile}")
    assert_true(profile.get("dpi_scale") == 1.25, f"profile dpi scale mismatch: {profile}")
    assert_true(profile.get("dpi") == 120, f"profile dpi mismatch: {profile}")
    assert_true(profile.get("monitor_count") == 1, f"profile monitor count mismatch: {profile}")


def test_profile_summary_and_change_detection() -> None:
    old = device_profile.build_device_profile(
        route="route-a",
        geometry={"width": 981, "height": 860},
        client_rect={"width": 981, "height": 860},
        screenshot_size=(981, 860),
        dpi_scale=1.0,
    )
    same = device_profile.build_device_profile(
        route="route-a",
        geometry={"width": 981, "height": 860},
        client_rect={"width": 981, "height": 860},
        screenshot_size=(981, 860),
        dpi_scale=1.0,
    )
    changed = device_profile.build_device_profile(
        route="route-a",
        geometry={"width": 981, "height": 860},
        client_rect={"width": 981, "height": 860},
        screenshot_size=(981, 860),
        dpi_scale=1.25,
    )
    summary = device_profile.profile_summary(old)
    assert_true(summary.get("profile_version") == device_profile.PROFILE_VERSION, f"profile version missing: {summary}")
    assert_true(device_profile.profile_changed(old, same) is False, "same profile should not be changed")
    assert_true(device_profile.profile_changed(old, changed) is True, "dpi profile change should be detected")


def main() -> int:
    tests = [
        test_device_profile_module_exports_expected_helpers,
        test_capture_geometry_guard_matches_sidecar,
        test_build_device_profile_shape,
        test_profile_summary_and_change_detection,
    ]
    passed = 0
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
        passed += 1
    print(f"All {passed} WeChat Win32/OCR device profile checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
