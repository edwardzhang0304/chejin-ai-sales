"""Regression checks for wxauto4 package update logic."""

from __future__ import annotations

import os
from pathlib import Path
import sys
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from apps.wechat_ai_customer_service.adapters.wxauto_package_manager import (  # noqa: E402
    WxautoPackageManager,
    compare_versions,
    parse_json_object,
    parse_latest_version,
    version_key,
)


def assert_true(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


class FakeManager(WxautoPackageManager):
    def __init__(self, *, installed: str, latest: str, upgrade_ok: bool = True) -> None:
        super().__init__(sidecar_python=Path(sys.executable), include_prerelease=True)
        self._installed = installed
        self._latest = latest
        self._upgrade_ok = upgrade_ok
        self.upgrade_called = False

    def installed_version(self) -> dict[str, Any]:
        return {"ok": True, "version": self._latest if self.upgrade_called and self._upgrade_ok else self._installed}

    def latest_version(self) -> dict[str, Any]:
        return {"ok": True, "version": self._latest}

    def upgrade(self) -> dict[str, Any]:
        self.upgrade_called = True
        return {"ok": self._upgrade_ok}

    def write_status(self, payload: dict[str, Any]) -> None:
        return None


def test_parse_latest_version() -> None:
    text = "wxauto4 (41.1.2)\nAvailable versions: 41.1.2\n  INSTALLED: 41.1.1\n  LATEST:    41.1.2"
    assert_true(parse_latest_version(text) == "41.1.2", "should parse headline version")
    assert_true(parse_latest_version("Available versions: 42.0.0, 41.1.2") == "42.0.0", "should parse first available version")
    assert_true(parse_latest_version("LATEST:    43.0.1") == "43.0.1", "should parse LATEST fallback")


def test_version_key() -> None:
    assert_true(version_key("41.1.10") > version_key("41.1.2"), "numeric version compare should not be lexical")
    assert_true(version_key("") == (0,), "empty version should be comparable")
    assert_true(compare_versions("41.1.3b1", "41.1.2") > 0, "beta above installed stable should update")
    assert_true(compare_versions("41.1.3", "41.1.3b1") > 0, "final release should beat beta")


def test_parse_json_object_with_logs() -> None:
    payload = parse_json_object("notice\n{\"ok\": true, \"version\": \"41.1.2\"}")
    assert_true(payload == {"ok": True, "version": "41.1.2"}, f"unexpected payload: {payload}")


def test_update_skips_when_latest() -> None:
    manager = FakeManager(installed="41.1.2", latest="41.1.2")
    result = manager.check_and_update()
    assert_true(result["ok"], f"latest check should be ok: {result}")
    assert_true(not result["updated"], f"already latest should not update: {result}")
    assert_true(not manager.upgrade_called, "upgrade should not be called when already latest")


def test_update_runs_when_newer_available() -> None:
    manager = FakeManager(installed="41.1.2", latest="41.1.3")
    result = manager.check_and_update()
    assert_true(result["ok"], f"upgrade should be ok: {result}")
    assert_true(result["updated"], f"newer version should be marked updated: {result}")
    assert_true(manager.upgrade_called, "upgrade should be called when newer version exists")


def test_update_runs_for_prerelease_available() -> None:
    manager = FakeManager(installed="41.1.2", latest="41.1.3b1")
    result = manager.check_and_update()
    assert_true(result["ok"], f"pre-release upgrade should be ok: {result}")
    assert_true(result["updated"], f"pre-release newer version should be marked updated: {result}")
    assert_true(result["include_prerelease"] is True, "pre-release policy should be visible in status")
    assert_true(manager.upgrade_called, "upgrade should be called for newer beta version")


def test_auto_update_is_disabled_when_reserve_is_off() -> None:
    previous_reserve = os.environ.pop("WECHAT_ENABLE_WXAUTO4", None)
    previous_update = os.environ.pop("WECHAT_WXAUTO_AUTO_UPDATE", None)
    try:
        manager = FakeManager(installed="41.1.2", latest="41.1.3")
        result = manager.auto_update_on_wechat_module_start()
        assert_true(result["ok"], f"disabled reserve should still be a safe ok result: {result}")
        assert_true(result["enabled"] is False, f"reserve-disabled result should not enable wxauto4: {result}")
        assert_true(result["reason"] == "wxauto4_reserve_disabled", f"unexpected disable reason: {result}")
        assert_true(manager.upgrade_called is False, "upgrade must not run while wxauto4 reserve is disabled")
    finally:
        if previous_reserve is not None:
            os.environ["WECHAT_ENABLE_WXAUTO4"] = previous_reserve
        if previous_update is not None:
            os.environ["WECHAT_WXAUTO_AUTO_UPDATE"] = previous_update


def test_auto_update_requires_update_flag_when_reserve_is_on() -> None:
    previous_reserve = os.environ.get("WECHAT_ENABLE_WXAUTO4")
    previous_update = os.environ.get("WECHAT_WXAUTO_AUTO_UPDATE")
    try:
        os.environ["WECHAT_ENABLE_WXAUTO4"] = "1"
        os.environ["WECHAT_WXAUTO_AUTO_UPDATE"] = "0"
        manager = FakeManager(installed="41.1.2", latest="41.1.3")
        disabled = manager.auto_update_on_wechat_module_start()
        assert_true(disabled["enabled"] is False, f"auto-update flag should disable update: {disabled}")
        assert_true(
            disabled["reason"] == "disabled_by_WECHAT_WXAUTO_AUTO_UPDATE",
            f"unexpected disabled reason: {disabled}",
        )
        assert_true(manager.upgrade_called is False, "upgrade must not run when auto-update flag is off")

        os.environ["WECHAT_WXAUTO_AUTO_UPDATE"] = "1"
        manager = FakeManager(installed="41.1.2", latest="41.1.3")
        enabled = manager.auto_update_on_wechat_module_start()
        assert_true(enabled["updated"] is True, f"explicit reserve should allow update path: {enabled}")
        assert_true(manager.upgrade_called is True, "upgrade should run only after reserve and update flags are on")
    finally:
        if previous_reserve is None:
            os.environ.pop("WECHAT_ENABLE_WXAUTO4", None)
        else:
            os.environ["WECHAT_ENABLE_WXAUTO4"] = previous_reserve
        if previous_update is None:
            os.environ.pop("WECHAT_WXAUTO_AUTO_UPDATE", None)
        else:
            os.environ["WECHAT_WXAUTO_AUTO_UPDATE"] = previous_update


def main() -> int:
    tests = [
        test_parse_latest_version,
        test_version_key,
        test_parse_json_object_with_logs,
        test_update_skips_when_latest,
        test_update_runs_when_newer_available,
        test_update_runs_for_prerelease_available,
        test_auto_update_is_disabled_when_reserve_is_off,
        test_auto_update_requires_update_flag_when_reserve_is_on,
    ]
    passed = 0
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
        passed += 1
    print(f"All {passed} wxauto package manager checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
