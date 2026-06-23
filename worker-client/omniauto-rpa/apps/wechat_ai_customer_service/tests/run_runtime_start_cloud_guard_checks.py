"""Checks for runtime start guard in cloud-required mode."""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import patch


APP_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = APP_ROOT.parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from apps.wechat_ai_customer_service.admin_backend.services.customer_service_runtime import CustomerServiceRuntime  # noqa: E402
from apps.wechat_ai_customer_service.customer_service_live_safety import CustomerServiceLiveSafetyError  # noqa: E402
from apps.wechat_ai_customer_service.scripts.run_customer_service_listener import (  # noqa: E402
    evaluate_runtime_target_guard,
    parse_last_json,
)


def main() -> int:
    old_cloud_required = os.environ.get("WECHAT_CLOUD_REQUIRED")
    os.environ["WECHAT_CLOUD_REQUIRED"] = "1"
    results: list[dict[str, Any]] = []
    try:
        results.append(check_runtime_start_requires_snapshot_refresh())
        results.append(check_runtime_start_requires_node_registration())
        results.append(check_runtime_start_requires_cloud_gate())
        results.append(check_runtime_start_refreshes_stale_bootstrap_guard_by_default())
        results.append(check_runtime_start_can_opt_out_of_stale_bootstrap_refresh())
        results.append(check_runtime_start_refreshes_stale_bootstrap_guard_when_explicitly_opted_in())
        results.append(check_bootstrap_refresh_targets_are_explicit_and_verified())
        results.append(check_runtime_target_guard_accepts_ascii_escaped_chinese_target())
        results.append(check_live_safety_guard_auto_syncs_settings_targets())
    finally:
        if old_cloud_required is None:
            os.environ.pop("WECHAT_CLOUD_REQUIRED", None)
        else:
            os.environ["WECHAT_CLOUD_REQUIRED"] = old_cloud_required
    failures = [item for item in results if not item.get("ok")]
    print(json.dumps({"ok": not failures, "count": len(results), "results": results, "failures": failures}, ensure_ascii=False, indent=2))
    return 1 if failures else 0


def check_runtime_start_requires_snapshot_refresh() -> dict[str, Any]:
    with patch(
        "apps.wechat_ai_customer_service.admin_backend.services.customer_service_runtime.cloud_required_enabled",
        return_value=True,
    ), patch(
        "apps.wechat_ai_customer_service.admin_backend.services.customer_service_runtime.write_runtime_status",
        return_value={},
    ), patch.object(
        CustomerServiceRuntime,
        "status",
        return_value={"running": False, "state": "stopped", "tenant_id": "default"},
    ), patch(
        "apps.wechat_ai_customer_service.admin_backend.services.customer_service_runtime.VpsLocalSyncService",
    ) as sync_cls:
        sync_instance = sync_cls.return_value
        sync_instance.register_node.return_value = {"ok": True, "node": {"node_id": "node-default"}}
        sync_instance.fetch_shared_knowledge_snapshot.return_value = {"ok": False, "error": "network_down"}
        runtime = CustomerServiceRuntime(tenant_id="default")
        result = runtime.start(token="token-example")
        assert_equal(result.get("ok"), False, "runtime start should fail when snapshot refresh fails")
        assert_equal(result.get("detail"), "cloud_snapshot_refresh_failed", "should return refresh-failed detail")
        sync_instance.register_node.assert_called_once_with(
            token="token-example",
            tenant_id="default",
            display_name="Local Customer Service Runtime",
        )
        sync_instance.fetch_shared_knowledge_snapshot.assert_called_once_with(
            token="token-example",
            tenant_id="default",
            force=True,
        )
    return {"name": "check_runtime_start_requires_snapshot_refresh", "ok": True}


def check_runtime_start_requires_node_registration() -> dict[str, Any]:
    with patch(
        "apps.wechat_ai_customer_service.admin_backend.services.customer_service_runtime.cloud_required_enabled",
        return_value=True,
    ), patch(
        "apps.wechat_ai_customer_service.admin_backend.services.customer_service_runtime.write_runtime_status",
        return_value={},
    ), patch.object(
        CustomerServiceRuntime,
        "status",
        return_value={"running": False, "state": "stopped", "tenant_id": "default"},
    ), patch(
        "apps.wechat_ai_customer_service.admin_backend.services.customer_service_runtime.VpsLocalSyncService",
    ) as sync_cls:
        sync_instance = sync_cls.return_value
        sync_instance.register_node.return_value = {"ok": False, "error": "node_tenant_forbidden"}
        runtime = CustomerServiceRuntime(tenant_id="default")
        result = runtime.start(token="token-example")
        assert_equal(result.get("ok"), False, "runtime start should fail when node registration fails")
        assert_equal(result.get("detail"), "cloud_node_register_failed", "should return node-register detail")
        sync_instance.fetch_shared_knowledge_snapshot.assert_not_called()
    return {"name": "check_runtime_start_requires_node_registration", "ok": True}


def check_runtime_start_requires_cloud_gate() -> dict[str, Any]:
    with patch(
        "apps.wechat_ai_customer_service.admin_backend.services.customer_service_runtime.cloud_required_enabled",
        return_value=True,
    ), patch(
        "apps.wechat_ai_customer_service.admin_backend.services.customer_service_runtime.cloud_gate_status",
        return_value={"ok": False, "reason": "cloud_server_unreachable"},
    ), patch(
        "apps.wechat_ai_customer_service.admin_backend.services.customer_service_runtime.write_runtime_status",
        return_value={},
    ), patch.object(
        CustomerServiceRuntime,
        "status",
        return_value={"running": False, "state": "stopped", "tenant_id": "default"},
    ), patch(
        "apps.wechat_ai_customer_service.admin_backend.services.customer_service_runtime.VpsLocalSyncService",
    ) as sync_cls:
        sync_instance = sync_cls.return_value
        sync_instance.register_node.return_value = {"ok": True, "node": {"node_id": "node-default"}}
        sync_instance.fetch_shared_knowledge_snapshot.return_value = {"ok": True, "updated": True}
        runtime = CustomerServiceRuntime(tenant_id="default")
        result = runtime.start(token="token-example")
        assert_equal(result.get("ok"), False, "runtime start should fail when cloud gate is closed")
        assert_equal(result.get("detail"), "cloud_authoritative_access_required", "should return cloud gate detail")
    return {"name": "check_runtime_start_requires_cloud_gate", "ok": True}


def check_runtime_start_refreshes_stale_bootstrap_guard_by_default() -> dict[str, Any]:
    stale_summary = {
        "enabled": True,
        "ok": False,
        "fail_reasons": ["recent_bootstrap_stale"],
        "allowed_targets": ["许聪"],
        "stale_targets": ["许聪"],
    }
    with tempfile.TemporaryDirectory() as tmp_dir:
        config_path = Path(tmp_dir) / "listener_config.json"
        config_path.write_text(
            json.dumps(
                {
                    "live_safety_guard": {
                        "enabled": True,
                        "allowed_targets": ["许聪"],
                        "require_recent_bootstrap": True,
                    }
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        with patch(
            "apps.wechat_ai_customer_service.admin_backend.services.customer_service_runtime.cloud_required_enabled",
            return_value=False,
        ), patch(
            "apps.wechat_ai_customer_service.admin_backend.services.customer_service_runtime.write_runtime_status",
            return_value={},
        ), patch.object(
            CustomerServiceRuntime,
            "status",
            return_value={"running": False, "state": "stopped", "tenant_id": "default"},
        ), patch.object(
            CustomerServiceRuntime,
            "_resolve_config_path",
            return_value=config_path,
        ), patch.object(
            CustomerServiceRuntime,
            "_validate_live_safety_guard",
            side_effect=[
                CustomerServiceLiveSafetyError(stale_summary),
                {"enabled": True, "ok": True, "fail_reasons": [], "bootstrap_guard": {"ok": True}},
            ],
        ) as validate_guard, patch.object(
            CustomerServiceRuntime,
            "_refresh_recent_bootstrap_guard",
            return_value={"ok": True, "returncode": 0},
        ) as refresh_guard, patch.object(
            CustomerServiceRuntime,
            "_managed_listener_interval_seconds",
            return_value=1.0,
        ), patch.object(
            CustomerServiceRuntime,
            "_auto_update_wxauto4",
            return_value={"ok": True, "updated": False},
        ), patch.object(
            CustomerServiceRuntime,
            "_wechat_startup_self_check",
            return_value={"ok": True, "message": "ok"},
        ), patch.object(
            CustomerServiceRuntime,
            "_write_pid_record",
            return_value=None,
        ), patch.object(
            CustomerServiceRuntime,
            "_start_worker",
            return_value={"ok": True},
        ), patch(
            "apps.wechat_ai_customer_service.admin_backend.services.customer_service_runtime.subprocess.Popen",
        ) as popen_cls, patch(
            "apps.wechat_ai_customer_service.admin_backend.services.customer_service_runtime.time.sleep",
            return_value=None,
        ):
            popen_cls.return_value.pid = 12345
            runtime = CustomerServiceRuntime(tenant_id="default")
            result = runtime.start(token="token-example")
            assert_equal(result.get("ok"), True, "runtime start should preserve automatic bootstrap recovery by default")
            assert_equal(validate_guard.call_count, 2, "bootstrap guard should be revalidated after default refresh")
            refresh_guard.assert_called_once()
    return {"name": "check_runtime_start_refreshes_stale_bootstrap_guard_by_default", "ok": True}


def check_runtime_start_can_opt_out_of_stale_bootstrap_refresh() -> dict[str, Any]:
    stale_summary = {
        "enabled": True,
        "ok": False,
        "fail_reasons": ["recent_bootstrap_stale"],
        "allowed_targets": ["许聪"],
        "stale_targets": ["许聪"],
    }
    with tempfile.TemporaryDirectory() as tmp_dir:
        config_path = Path(tmp_dir) / "listener_config.json"
        config_path.write_text(
            json.dumps(
                {
                    "live_safety_guard": {
                        "enabled": True,
                        "allowed_targets": ["许聪"],
                        "require_recent_bootstrap": True,
                        "startup_auto_bootstrap_refresh_enabled": False,
                    }
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        with patch(
            "apps.wechat_ai_customer_service.admin_backend.services.customer_service_runtime.cloud_required_enabled",
            return_value=False,
        ), patch(
            "apps.wechat_ai_customer_service.admin_backend.services.customer_service_runtime.write_runtime_status",
            return_value={},
        ), patch.object(
            CustomerServiceRuntime,
            "status",
            return_value={"running": False, "state": "stopped", "tenant_id": "default"},
        ), patch.object(
            CustomerServiceRuntime,
            "_resolve_config_path",
            return_value=config_path,
        ), patch.object(
            CustomerServiceRuntime,
            "_validate_live_safety_guard",
            side_effect=CustomerServiceLiveSafetyError(stale_summary),
        ) as validate_guard, patch.object(
            CustomerServiceRuntime,
            "_refresh_recent_bootstrap_guard",
            return_value={"ok": True, "returncode": 0},
        ) as refresh_guard:
            runtime = CustomerServiceRuntime(tenant_id="default")
            result = runtime.start(token="token-example")
            assert_equal(result.get("ok"), False, "runtime start should fail closed when bootstrap is stale")
            assert_equal(validate_guard.call_count, 1, "bootstrap guard should not be revalidated after skipped refresh")
            refresh_guard.assert_not_called()
            refresh = result.get("live_safety_guard", {}).get("bootstrap_refresh", {})
            assert_equal(
                refresh.get("reason"),
                "startup_auto_bootstrap_refresh_disabled",
                "startup should not run foreground RPA bootstrap refresh by default",
            )
            assert_equal(
                refresh.get("safe_manual_action_required"),
                True,
                "startup should require manual low-disturbance confirmation when bootstrap is stale",
            )
    return {"name": "check_runtime_start_can_opt_out_of_stale_bootstrap_refresh", "ok": True}


def check_runtime_start_refreshes_stale_bootstrap_guard_when_explicitly_opted_in() -> dict[str, Any]:
    stale_summary = {
        "enabled": True,
        "ok": False,
        "fail_reasons": ["recent_bootstrap_stale"],
        "allowed_targets": ["许聪"],
        "stale_targets": ["许聪"],
    }
    with tempfile.TemporaryDirectory() as tmp_dir:
        config_path = Path(tmp_dir) / "listener_config.json"
        config_path.write_text(
            json.dumps(
                {
                    "live_safety_guard": {
                        "enabled": True,
                        "allowed_targets": ["许聪"],
                        "require_recent_bootstrap": True,
                        "startup_auto_bootstrap_refresh_enabled": True,
                    }
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        with patch(
            "apps.wechat_ai_customer_service.admin_backend.services.customer_service_runtime.cloud_required_enabled",
            return_value=False,
        ), patch(
            "apps.wechat_ai_customer_service.admin_backend.services.customer_service_runtime.write_runtime_status",
            return_value={},
        ), patch.object(
            CustomerServiceRuntime,
            "status",
            return_value={"running": False, "state": "stopped", "tenant_id": "default"},
        ), patch.object(
            CustomerServiceRuntime,
            "_resolve_config_path",
            return_value=config_path,
        ), patch.object(
            CustomerServiceRuntime,
            "_validate_live_safety_guard",
            side_effect=[
                CustomerServiceLiveSafetyError(stale_summary),
                {"enabled": True, "ok": True, "fail_reasons": [], "bootstrap_guard": {"ok": True}},
            ],
        ) as validate_guard, patch.object(
            CustomerServiceRuntime,
            "_refresh_recent_bootstrap_guard",
            return_value={"ok": True, "returncode": 0},
        ) as refresh_guard, patch.object(
            CustomerServiceRuntime,
            "_managed_listener_interval_seconds",
            return_value=1.0,
        ), patch.object(
            CustomerServiceRuntime,
            "_auto_update_wxauto4",
            return_value={"ok": True, "updated": False},
        ), patch.object(
            CustomerServiceRuntime,
            "_wechat_startup_self_check",
            return_value={"ok": True, "message": "ok"},
        ), patch.object(
            CustomerServiceRuntime,
            "_write_pid_record",
            return_value=None,
        ), patch.object(
            CustomerServiceRuntime,
            "_start_worker",
            return_value={"ok": True},
        ), patch(
            "apps.wechat_ai_customer_service.admin_backend.services.customer_service_runtime.subprocess.Popen",
        ) as popen_cls, patch(
            "apps.wechat_ai_customer_service.admin_backend.services.customer_service_runtime.time.sleep",
            return_value=None,
        ):
            popen_cls.return_value.pid = 12345
            runtime = CustomerServiceRuntime(tenant_id="default")
            result = runtime.start(token="token-example")
            assert_equal(result.get("ok"), True, "runtime start may continue only after explicit bootstrap-refresh opt-in")
            assert_equal(validate_guard.call_count, 2, "bootstrap guard should be revalidated after opted-in refresh")
            refresh_guard.assert_called_once()
            assert_true(
                result.get("live_safety_guard", {}).get("bootstrap_refresh", {}).get("ok") is True,
                "start result should include successful bootstrap refresh details",
            )
    return {"name": "check_runtime_start_refreshes_stale_bootstrap_guard_when_explicitly_opted_in", "ok": True}


def check_bootstrap_refresh_targets_are_explicit_and_verified() -> dict[str, Any]:
    config_payload = {
        "live_safety_guard": {
            "enabled": True,
            "allowed_targets": ["许聪"],
            "require_recent_bootstrap": True,
        },
        "targets": [{"name": "许聪", "enabled": True}],
    }
    with tempfile.TemporaryDirectory() as tmp_dir:
        config_path = Path(tmp_dir) / "listener_config.json"
        config_path.write_text(json.dumps(config_payload, ensure_ascii=False), encoding="utf-8")
        stdout = json.dumps(
            {
                "ok": True,
                "events": [
                    {
                        "target": "许聪",
                        "action": "bootstrapped",
                        "marked_count": 0,
                    }
                ],
            },
            ensure_ascii=False,
        )
        pending_stdout = json.dumps(
            {
                "ok": True,
                "events": [
                    {
                        "target": "许聪",
                        "action": "pending_visible",
                        "reason": "target_not_visible_waiting_for_unread",
                    }
                ],
            },
            ensure_ascii=False,
        )
        missing_stdout = json.dumps({"ok": True, "events": []}, ensure_ascii=False)
        captured: dict[str, Any] = {}

        def fake_run(command: list[str], **kwargs: Any) -> Any:
            captured["command"] = command
            captured["env"] = kwargs.get("env") or {}
            return type(
                "Completed",
                (),
                {
                    "returncode": 0,
                    "stdout": stdout,
                    "stderr": "",
                },
            )()

        runtime = CustomerServiceRuntime(tenant_id="default")
        with patch(
            "apps.wechat_ai_customer_service.admin_backend.services.customer_service_runtime.write_runtime_status",
            return_value={},
        ), patch(
            "apps.wechat_ai_customer_service.admin_backend.services.customer_service_runtime.subprocess.run",
            side_effect=fake_run,
        ):
            result = runtime._refresh_recent_bootstrap_guard(config_path)

        with patch(
            "apps.wechat_ai_customer_service.admin_backend.services.customer_service_runtime.write_runtime_status",
            return_value={},
        ), patch(
            "apps.wechat_ai_customer_service.admin_backend.services.customer_service_runtime.subprocess.run",
            return_value=type(
                "Completed",
                (),
                {
                    "returncode": 0,
                    "stdout": pending_stdout,
                    "stderr": "",
                },
            )(),
        ):
            pending_result = runtime._refresh_recent_bootstrap_guard(config_path)

        with patch(
            "apps.wechat_ai_customer_service.admin_backend.services.customer_service_runtime.write_runtime_status",
            return_value={},
        ), patch(
            "apps.wechat_ai_customer_service.admin_backend.services.customer_service_runtime.subprocess.run",
            return_value=type(
                "Completed",
                (),
                {
                    "returncode": 0,
                    "stdout": missing_stdout,
                    "stderr": "",
                },
            )(),
        ):
            missing_result = runtime._refresh_recent_bootstrap_guard(config_path)

    command = captured.get("command") or []
    assert_equal(result.get("ok"), True, "bootstrap refresh should pass when expected target is bootstrapped")
    assert_true("--target" in command, "bootstrap refresh command should include explicit --target")
    target_index = command.index("--target")
    assert_equal(command[target_index + 1], "许聪", "bootstrap refresh should target the allowed session")
    env = captured.get("env") or {}
    assert_equal(env.get("WECHAT_WIN32_OCR_TARGET_SEARCH_FALLBACK"), "0", "startup bootstrap refresh must not use sidebar search fallback")
    assert_equal(
        env.get("WECHAT_WIN32_OCR_TARGET_SEARCH_ENTER_FALLBACK"),
        "0",
        "startup bootstrap refresh must not press Enter for search fallback",
    )
    assert_equal(
        pending_result.get("ok"),
        True,
        "pending-visible target should be accepted so startup can wait for unread auto-identification",
    )
    assert_equal(
        pending_result.get("verified", {}).get("pending_visible_targets"),
        ["许聪"],
        "pending-visible targets should be reported separately from bootstrapped targets",
    )
    assert_equal(
        missing_result.get("ok"),
        False,
        "bootstrap refresh should fail closed when stdout has no bootstrapped target event",
    )
    assert_equal(
        missing_result.get("reason"),
        "bootstrap_refresh_target_not_bootstrapped",
        "missing target should have explicit failure reason",
    )
    return {"name": "check_bootstrap_refresh_targets_are_explicit_and_verified", "ok": True}


def check_runtime_target_guard_accepts_ascii_escaped_chinese_target() -> dict[str, Any]:
    stdout = json.dumps(
        {
            "ok": True,
            "events": [
                {
                    "target": "许聪",
                    "action": "skipped",
                    "reason": "no eligible unprocessed text messages",
                }
            ],
        },
        ensure_ascii=True,
    )
    payload = parse_last_json(stdout)
    assert_equal(payload.get("events", [{}])[0].get("target"), "许聪", "escaped stdout should round-trip Chinese target")
    verdict = evaluate_runtime_target_guard(
        payload,
        settings={"enabled": True, "allowed_targets": ["许聪"]},
    )
    assert_equal(verdict.get("stop"), False, "target guard should not stop for allowed Chinese target")
    assert_equal(verdict.get("ok"), True, "target guard should accept allowed Chinese target")
    return {"name": "check_runtime_target_guard_accepts_ascii_escaped_chinese_target", "ok": True}


def check_live_safety_guard_auto_syncs_settings_targets() -> dict[str, Any]:
    config_payload = {
        "live_safety_guard": {
            "enabled": True,
            "allowed_targets": ["许聪"],
            "require_exact_targets": True,
            "disable_respond_all_unread_sessions": True,
            "require_recent_bootstrap": False,
        },
        "targets": [{"name": "许聪", "enabled": True}],
    }
    with tempfile.TemporaryDirectory() as tmp_dir:
        config_path = Path(tmp_dir) / "listener_config.json"
        config_path.write_text(json.dumps(config_payload, ensure_ascii=False, indent=2), encoding="utf-8")
        runtime = CustomerServiceRuntime(tenant_id="default")
        settings_payload: dict[str, Any] = {
            "enabled": True,
            "reply_mode": "full_auto",
            "record_messages": True,
            "auto_learn": False,
            "use_llm": True,
            "rag_enabled": True,
            "data_capture_enabled": True,
            "handoff_enabled": True,
            "operator_alert_enabled": True,
            "identity_guard_enabled": True,
            "style_adapter_enabled": True,
            "final_visible_llm_polish_enabled": True,
            "respond_all_unread_sessions": True,
            "session_targets_managed": True,
            "session_targets": [
                {
                    "name": "文件传输助手",
                    "display_name": "文件传输助手",
                    "enabled": True,
                    "exact": True,
                    "conversation_type": "file_transfer",
                    "source": "discovered",
                    "updated_at": "2026-05-31T13:50:16",
                },
                {
                    "name": "许聪",
                    "display_name": "许聪",
                    "enabled": False,
                    "exact": True,
                    "conversation_type": "private",
                    "source": "discovered",
                    "updated_at": "2026-05-24T12:44:27",
                },
            ],
        }

        def fake_get(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
            return json.loads(json.dumps(settings_payload, ensure_ascii=False))

        def fake_save(*args: Any, **_kwargs: Any) -> dict[str, Any]:
            patch = args[-1] if args else {}
            settings_payload.update(patch or {})
            return json.loads(json.dumps(settings_payload, ensure_ascii=False))

        with patch(
            "apps.wechat_ai_customer_service.admin_backend.services.customer_service_settings.CustomerServiceSettings.get",
            side_effect=fake_get,
        ), patch(
            "apps.wechat_ai_customer_service.admin_backend.services.customer_service_settings.CustomerServiceSettings.save",
            side_effect=fake_save,
        ):
            summary = runtime._validate_live_safety_guard(config_path)

    assert_equal(summary.get("ok"), True, "guard should pass after auto-sync settings targets")
    sync = summary.get("settings_sync") if isinstance(summary.get("settings_sync"), dict) else {}
    assert_true(bool(sync.get("changed")), "auto-sync summary should record changes")
    guard_sync = summary.get("guard_target_sync") if isinstance(summary.get("guard_target_sync"), dict) else {}
    assert_true(bool(guard_sync.get("changed")), "guard-target sync should align allowed targets from managed settings")
    effective = set(summary.get("effective_enabled_targets") or [])
    assert_equal(effective, {"文件传输助手"}, "effective enabled targets should match managed settings selection")
    return {"name": "check_live_safety_guard_auto_syncs_settings_targets", "ok": True}


def assert_equal(actual: Any, expected: Any, message: str) -> None:
    if actual != expected:
        raise AssertionError(f"{message}: expected {expected!r}, got {actual!r}")


def assert_true(value: bool, message: str) -> None:
    if not value:
        raise AssertionError(message)


if __name__ == "__main__":
    raise SystemExit(main())
