"""Phase-A regression checks for recorder runtime/query/export foundations."""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any

os.environ.setdefault("WECHAT_STORAGE_BACKEND", "file")
os.environ.setdefault("WECHAT_CLOUD_REQUIRED", "0")
os.environ.setdefault("WECHAT_CLOUD_STRICT_ONLINE", "0")

from fastapi.testclient import TestClient


APP_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = APP_ROOT.parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from apps.wechat_ai_customer_service.admin_backend.app import create_app  # noqa: E402
from apps.wechat_ai_customer_service.admin_backend.services import recorder_runtime as recorder_runtime_module  # noqa: E402
from apps.wechat_ai_customer_service.admin_backend.services.background_handlers import handle_recorder_export_run_execute  # noqa: E402
from apps.wechat_ai_customer_service.admin_backend.services.work_queue import WorkQueueService  # noqa: E402
from apps.wechat_ai_customer_service.knowledge_paths import tenant_context, tenant_runtime_root  # noqa: E402


TEST_TENANT = "recorder_phase_a_test"


def main() -> int:
    try:
        with tenant_context(TEST_TENANT):
            cleanup_runtime()
            run_checks()
        print(json.dumps({"ok": True, "tenant": TEST_TENANT}, ensure_ascii=False, indent=2))
        return 0
    finally:
        with tenant_context(TEST_TENANT):
            cleanup_runtime()


def run_checks() -> None:
    client = TestClient(create_app())
    headers = {"X-Tenant-ID": TEST_TENANT}

    seed_messages(client, headers)
    check_advanced_query_filters(client, headers)
    check_recorder_enabled_switch(client, headers)
    check_runtime_lifecycle(client, headers)
    check_module_registry_and_binding(client, headers)
    check_async_export_run(client, headers)


def seed_messages(client: TestClient, headers: dict[str, str]) -> None:
    payload = {
        "conversation": {
            "target_name": "PhaseA测试群",
            "display_name": "PhaseA测试群",
            "conversation_type": "group",
            "selected_by_user": True,
            "learning_enabled": True,
            "notify_enabled": False,
            "status": "active",
        },
        "messages": [
            {
                "id": "phase_a_001",
                "type": "text",
                "sender": "Alice",
                "content": "张三-李四老师 试剂A 2盒 98元",
                "time": "2026-05-09 10:00:00",
            },
            {
                "id": "phase_a_002",
                "type": "text",
                "sender": "Bob",
                "content": "这条不是订单，仅测试过滤",
                "time": "2026-05-09 10:05:00",
            },
            {
                "id": "phase_a_003",
                "type": "quote",
                "sender": "Alice",
                "content": "订单确认：试剂A 2盒",
                "time": "2026-05-09 10:10:00",
            },
        ],
        "source_module": "phase_a_test",
        "auto_learn": False,
    }
    response = client.post("/api/raw-messages/messages", headers=headers, json=payload)
    assert_equal(response.status_code, 200, "seed raw messages endpoint status")
    result = response.json()
    assert_true(result.get("ok") is True, "seed raw messages ok")
    assert_true(int(result.get("inserted_count", 0) or 0) >= 2, "seed messages inserted")


def check_advanced_query_filters(client: TestClient, headers: dict[str, str]) -> None:
    response = client.get(
        "/api/raw-messages/messages",
        headers=headers,
        params={
            "sender": "Alice",
            "content_type": "text",
            "keywords": "试剂A,98元",
            "conversation_type": "group",
            "limit": 20,
        },
    )
    assert_equal(response.status_code, 200, "advanced query endpoint status")
    payload = response.json()
    assert_true(payload.get("ok") is True, "advanced query ok")
    items = payload.get("items", [])
    assert_true(len(items) >= 1, "advanced query should return filtered messages")
    first = items[0]
    assert_true("Alice".lower() in str(first.get("sender") or "").lower(), "sender filter respected")


def check_runtime_lifecycle(client: TestClient, headers: dict[str, str]) -> None:
    before = client.get("/api/recorder/runtime/status", headers=headers)
    assert_equal(before.status_code, 200, "runtime status before start")

    original_startup_check = recorder_runtime_module.RecorderRuntime._wechat_startup_self_check
    original_operator_guard = recorder_runtime_module.RecorderRuntime._launch_operator_guard_for_loop

    def fake_startup_check(self: Any, *, wxauto_update: dict[str, Any]) -> dict[str, Any]:
        return {
            "ok": True,
            "detail": "phase_a_stub_wechat_ready",
            "scheme": "win32_ocr_guarded_click",
            "message": "Phase-A runtime test uses a deterministic WeChat startup stub.",
            "wxauto_update": wxauto_update,
        }

    def fake_operator_guard(self: Any, *, parent_pid: int, settings: dict[str, Any]) -> dict[str, Any]:
        return {
            "ok": True,
            "enabled": True,
            "pid": parent_pid,
            "test_stub": True,
            "settings": {
                "floating_indicator_enabled": True,
                "block_manual_input": True,
            },
        }

    recorder_runtime_module.RecorderRuntime._wechat_startup_self_check = fake_startup_check  # type: ignore[assignment]
    recorder_runtime_module.RecorderRuntime._launch_operator_guard_for_loop = fake_operator_guard  # type: ignore[assignment]
    try:
        started = client.post("/api/recorder/runtime/start", headers=headers, json={})
        assert_equal(started.status_code, 200, "runtime start status")
        started_payload = started.json()
        assert_true(started_payload.get("ok") is True, f"runtime start ok: {started_payload}")

        status = client.get("/api/recorder/runtime/status", headers=headers)
        assert_equal(status.status_code, 200, "runtime status after start")
        status_payload = status.json().get("item", {})
        assert_true(status_payload.get("running") is True, "runtime should be running after start")
    finally:
        recorder_runtime_module.RecorderRuntime._wechat_startup_self_check = original_startup_check  # type: ignore[assignment]
        recorder_runtime_module.RecorderRuntime._launch_operator_guard_for_loop = original_operator_guard  # type: ignore[assignment]
        stopped = client.post("/api/recorder/runtime/stop", headers=headers, json={})
        assert_equal(stopped.status_code, 200, "runtime stop status")
        assert_true(stopped.json().get("ok") is True, "runtime stop ok")


def check_recorder_enabled_switch(client: TestClient, headers: dict[str, str]) -> None:
    settings_before = client.get("/api/recorder/settings", headers=headers)
    assert_equal(settings_before.status_code, 200, "recorder settings before update status")
    before_item = settings_before.json().get("item", {})
    assert_true("enabled" in before_item, "recorder settings should include enabled switch")

    disabled = client.put("/api/recorder/settings", headers=headers, json={"enabled": False})
    assert_equal(disabled.status_code, 200, "disable recorder switch status")
    disabled_item = disabled.json().get("item", {})
    assert_true(disabled_item.get("enabled") is False, "recorder switch should be disabled")

    capture = client.post("/api/recorder/capture", headers=headers, json={"send_notifications": False})
    assert_equal(capture.status_code, 200, "capture status while disabled")
    capture_payload = capture.json()
    assert_true(capture_payload.get("enabled") is False, "capture should report disabled status")
    assert_equal(capture_payload.get("inserted_count"), 0, "capture should skip inserts while disabled")

    blocked_start = client.post("/api/recorder/runtime/start", headers=headers, json={})
    assert_equal(blocked_start.status_code, 200, "runtime start status while disabled")
    blocked_payload = blocked_start.json()
    assert_true(blocked_payload.get("ok") is False, "runtime start should fail when disabled")
    assert_equal(blocked_payload.get("detail"), "recorder_disabled", "runtime start should return recorder_disabled detail")

    enabled = client.put("/api/recorder/settings", headers=headers, json={"enabled": True})
    assert_equal(enabled.status_code, 200, "enable recorder switch status")
    enabled_item = enabled.json().get("item", {})
    assert_true(enabled_item.get("enabled") is True, "recorder switch should be enabled")


def check_module_registry_and_binding(client: TestClient, headers: dict[str, str]) -> None:
    modules = client.get("/api/recorder/modules", headers=headers)
    assert_equal(modules.status_code, 200, "list modules status")
    module_items = modules.json().get("items", [])
    assert_true(any(str(item.get("module_key") or "") == "raw_message_log_v1" for item in module_items), "builtin module exists")

    bind_payload = {
        "binding_id": "phase_a_local_admin_binding",
        "scope_type": "user",
        "scope_id": "local-admin",
        "user_id": "local-admin",
        "module_key": "raw_message_log_v1",
        "enabled": True,
    }
    binding = client.post("/api/recorder/module-bindings", headers=headers, json=bind_payload)
    assert_equal(binding.status_code, 200, "upsert binding status")
    assert_true(binding.json().get("ok") is True, "upsert binding ok")


def check_async_export_run(client: TestClient, headers: dict[str, str]) -> None:
    run_payload = {
        "target_names": ["PhaseA测试群"],
        "limit": 200,
        "keywords": ["试剂A"],
    }
    created = client.post("/api/recorder/exports/runs", headers=headers, json=run_payload)
    assert_equal(created.status_code, 200, "create export run status")
    item = created.json().get("item", {})
    run_id = str(item.get("run_id") or "")
    assert_true(run_id.startswith("recorder_export_run_"), "run id should be generated")
    assert_equal(item.get("status"), "queued", "run should start queued")

    queue = WorkQueueService(tenant_id=TEST_TENANT)
    target_job = None
    for _ in range(20):
        current_detail = client.get(f"/api/recorder/exports/runs/{run_id}", headers=headers)
        assert_equal(current_detail.status_code, 200, "poll export run detail status")
        current_item = current_detail.json().get("item", {})
        if current_item.get("status") == "succeeded":
            artifacts = current_item.get("artifacts") if isinstance(current_item.get("artifacts"), dict) else {}
            xlsx_path = Path(str(artifacts.get("xlsx_path") or ""))
            report_path = Path(str(artifacts.get("report_path") or ""))
            assert_true(xlsx_path.exists(), "xlsx artifact should exist")
            assert_true(report_path.exists(), "report artifact should exist")
            return
        claimed = queue.claim(queue="recorder_exports", worker_id="phase-a-test", limit=50, lock_seconds=120)
        target_job = next((job for job in claimed if str((job.get("payload") or {}).get("run_id") or "") == run_id), None)
        if target_job is not None:
            break
    assert_true(target_job is not None, "run job should be claimable in recorder_exports queue")

    result = handle_recorder_export_run_execute(target_job.get("payload") if isinstance(target_job.get("payload"), dict) else {})
    if result.get("ok"):
        queue.complete(str(target_job.get("job_id") or ""), result=result)
    else:
        queue.fail(str(target_job.get("job_id") or ""), error=str(result.get("error") or "export failed"), retry=False)
        raise AssertionError(f"export handler failed: {result}")

    run_detail = client.get(f"/api/recorder/exports/runs/{run_id}", headers=headers)
    assert_equal(run_detail.status_code, 200, "get export run detail status")
    run_item = run_detail.json().get("item", {})
    assert_equal(run_item.get("status"), "succeeded", "run should complete successfully")
    artifacts = run_item.get("artifacts") if isinstance(run_item.get("artifacts"), dict) else {}
    xlsx_path = Path(str(artifacts.get("xlsx_path") or ""))
    report_path = Path(str(artifacts.get("report_path") or ""))
    assert_true(xlsx_path.exists(), "xlsx artifact should exist")
    assert_true(report_path.exists(), "report artifact should exist")


def cleanup_runtime() -> None:
    root = tenant_runtime_root(TEST_TENANT)
    if root.exists():
        shutil.rmtree(root)


def assert_true(value: bool, message: str) -> None:
    if not value:
        raise AssertionError(message)


def assert_equal(actual: Any, expected: Any, message: str) -> None:
    if actual != expected:
        raise AssertionError(f"{message}: expected {expected!r}, got {actual!r}")


if __name__ == "__main__":
    raise SystemExit(main())
