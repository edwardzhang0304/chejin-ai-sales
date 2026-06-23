"""Checks that cloud-authoritative mode does not fall back to implicit local admin."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

from fastapi.testclient import TestClient


APP_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = APP_ROOT.parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from apps.wechat_ai_customer_service.admin_backend.app import create_app  # noqa: E402
from apps.wechat_ai_customer_service.auth import AuthService  # noqa: E402


def main() -> int:
    old_cloud_required = os.environ.get("WECHAT_CLOUD_REQUIRED")
    old_auth_required = os.environ.get("WECHAT_AUTH_REQUIRED")
    results: list[dict[str, Any]] = []
    try:
        results.append(check_cloud_required_defaults_to_login_required())
        results.append(check_explicit_dev_auth_bypass_still_requires_opt_in())
        results.append(check_runtime_start_requires_login_in_cloud_mode())
        results.append(check_local_safety_stop_does_not_require_login())
        results.append(check_recorder_local_safety_stop_uses_query_tenant_id())
    finally:
        restore_env("WECHAT_CLOUD_REQUIRED", old_cloud_required)
        restore_env("WECHAT_AUTH_REQUIRED", old_auth_required)

    failures = [item for item in results if not item.get("ok")]
    print(json.dumps({"ok": not failures, "count": len(results), "results": results, "failures": failures}, ensure_ascii=False, indent=2))
    return 1 if failures else 0


def check_cloud_required_defaults_to_login_required() -> dict[str, Any]:
    os.environ["WECHAT_CLOUD_REQUIRED"] = "1"
    os.environ.pop("WECHAT_AUTH_REQUIRED", None)
    service = AuthService()
    assert_equal(service.settings.required, True, "cloud-required mode should require login by default")
    assert_equal(service.resolve_context(), None, "cloud-required mode must not issue implicit admin without login")
    return {"name": "check_cloud_required_defaults_to_login_required", "ok": True}


def check_explicit_dev_auth_bypass_still_requires_opt_in() -> dict[str, Any]:
    os.environ["WECHAT_CLOUD_REQUIRED"] = "1"
    os.environ["WECHAT_AUTH_REQUIRED"] = "0"
    service = AuthService()
    assert_equal(service.settings.required, False, "explicit WECHAT_AUTH_REQUIRED=0 should remain an opt-in dev bypass")
    context = service.resolve_context()
    assert_true(context is not None and not context.authenticated, "explicit dev bypass may still use implicit local admin")
    return {"name": "check_explicit_dev_auth_bypass_still_requires_opt_in", "ok": True}


def check_runtime_start_requires_login_in_cloud_mode() -> dict[str, Any]:
    os.environ["WECHAT_CLOUD_REQUIRED"] = "1"
    os.environ.pop("WECHAT_AUTH_REQUIRED", None)
    client = TestClient(create_app())
    response = client.post("/api/customer-service/runtime/start", json={})
    assert_equal(response.status_code, 401, "runtime start should require a logged-in local client in cloud mode")
    payload = response.json()
    assert_equal(payload.get("detail"), "authentication required", "runtime start should fail with authentication required")
    return {"name": "check_runtime_start_requires_login_in_cloud_mode", "ok": True}


def check_local_safety_stop_does_not_require_login() -> dict[str, Any]:
    os.environ["WECHAT_CLOUD_REQUIRED"] = "1"
    os.environ.pop("WECHAT_AUTH_REQUIRED", None)
    client = TestClient(create_app())
    with patch(
        "apps.wechat_ai_customer_service.admin_backend.services.customer_service_runtime.CustomerServiceRuntime.stop",
        return_value={"ok": True, "message": "已停止。", "item": {"running": False, "state": "stopped"}},
    ) as customer_stop, patch(
        "apps.wechat_ai_customer_service.admin_backend.services.recorder_runtime.RecorderRuntime.stop",
        return_value={"ok": True, "message": "AI智能记录员监听已停止。", "item": {"running": False, "state": "stopped"}},
    ) as recorder_stop:
        customer_response = client.post("/api/customer-service/runtime/stop", headers={"Host": "127.0.0.1", "X-Tenant-ID": "chejin"}, json={})
        assert_equal(customer_response.status_code, 200, "local customer-service safety stop should bypass stale login")
        assert_equal(customer_response.headers.get("X-Local-Safety-Stop"), "1", "local safety stop should be explicit")
        customer_stop.assert_called_once()
        recorder_response = client.post("/api/recorder/runtime/stop", headers={"Host": "127.0.0.1", "X-Tenant-ID": "test02"}, json={})
        assert_equal(recorder_response.status_code, 200, "local recorder safety stop should bypass stale login")
        assert_equal(recorder_response.headers.get("X-Tenant-ID"), "test02", "recorder safety stop should preserve tenant context")
        recorder_stop.assert_called_once()
        remote_response = client.post("/api/customer-service/runtime/stop", headers={"Host": "example.com", "X-Tenant-ID": "chejin"}, json={})
        assert_equal(remote_response.status_code, 401, "non-local runtime stop should still require login")
    return {"name": "check_local_safety_stop_does_not_require_login", "ok": True}


def check_recorder_local_safety_stop_uses_query_tenant_id() -> dict[str, Any]:
    os.environ["WECHAT_CLOUD_REQUIRED"] = "1"
    os.environ.pop("WECHAT_AUTH_REQUIRED", None)
    client = TestClient(create_app())
    with patch("apps.wechat_ai_customer_service.admin_backend.api.recorder.RecorderRuntime") as recorder_runtime:
        recorder_runtime.return_value.stop.return_value = {
            "ok": True,
            "message": "AI智能记录员监听已停止。",
            "item": {"running": False, "state": "stopped"},
        }
        response = client.post(
            "/api/recorder/runtime/stop?tenant_id=test02",
            headers={"Host": "127.0.0.1"},
            json={},
        )
        assert_equal(response.status_code, 200, "local recorder safety stop with query tenant should bypass stale login")
        assert_equal(response.headers.get("X-Local-Safety-Stop"), "1", "query tenant safety stop should be explicit")
        recorder_runtime.assert_called_once_with(tenant_id="test02")
    return {"name": "check_recorder_local_safety_stop_uses_query_tenant_id", "ok": True}


def restore_env(name: str, value: str | None) -> None:
    if value is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = value


def assert_equal(actual: Any, expected: Any, message: str) -> None:
    if actual != expected:
        raise AssertionError(f"{message}: expected {expected!r}, got {actual!r}")


def assert_true(value: bool, message: str) -> None:
    if not value:
        raise AssertionError(message)


if __name__ == "__main__":
    raise SystemExit(main())
