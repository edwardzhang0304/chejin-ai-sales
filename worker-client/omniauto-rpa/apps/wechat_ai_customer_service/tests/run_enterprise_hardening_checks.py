"""Enterprise hardening checks for queue, handoff, monitoring, and DB mode."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable


PROJECT_ROOT = Path(__file__).resolve().parents[3]
APP_ROOT = PROJECT_ROOT / "apps" / "wechat_ai_customer_service"
TEST_ROOT = PROJECT_ROOT / "runtime" / "apps" / "wechat_ai_customer_service" / "test_artifacts" / "enterprise_hardening"
for path in (PROJECT_ROOT, APP_ROOT, APP_ROOT / "workflows"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from apps.wechat_ai_customer_service.admin_backend.services.work_queue import WorkQueueService  # noqa: E402
from apps.wechat_ai_customer_service.admin_backend.services.handoff_store import HandoffStore  # noqa: E402
from apps.wechat_ai_customer_service.admin_backend.services.runtime_monitor import RuntimeMonitor  # noqa: E402
from apps.wechat_ai_customer_service.storage import get_postgres_store, load_storage_config  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsn", default="")
    args = parser.parse_args()

    TEST_ROOT.mkdir(parents=True, exist_ok=True)
    checks: list[Callable[[], None]] = [
        check_json_work_queue_roundtrip,
        check_json_handoff_roundtrip,
        check_json_runtime_monitor_readiness,
    ]
    if args.dsn:
        def pg_work_queue() -> None:
            check_postgres_work_queue_roundtrip(args.dsn)

        def pg_handoff() -> None:
            check_postgres_handoff_roundtrip(args.dsn)

        def pg_monitor() -> None:
            check_postgres_runtime_monitor_readiness(args.dsn)

        pg_work_queue.__name__ = "check_postgres_work_queue_roundtrip"
        pg_handoff.__name__ = "check_postgres_handoff_roundtrip"
        pg_monitor.__name__ = "check_postgres_runtime_monitor_readiness"
        checks.append(pg_work_queue)
        checks.append(pg_handoff)
        checks.append(pg_monitor)
    failures = []
    for check in checks:
        try:
            check()
        except Exception as exc:  # noqa: BLE001
            failures.append({"name": check_name(check), "error": repr(exc)})
    result = {
        "ok": not failures,
        "count": len(checks),
        "failures": failures,
        "results": [{"name": check_name(check), "ok": check_name(check) not in {item["name"] for item in failures}} for check in checks],
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


def check_json_work_queue_roundtrip() -> None:
    old_backend = os.environ.get("WECHAT_STORAGE_BACKEND")
    os.environ["WECHAT_STORAGE_BACKEND"] = "json"
    path = TEST_ROOT / "json_work_queue.json"
    if path.exists():
        path.unlink()
    try:
        service = WorkQueueService(tenant_id="hardening_json_test", path=path)
        job = service.enqueue(kind="rag_rebuild", payload={"source": "unit"}, dedupe_key="rag:unit")
        duplicate = service.enqueue(kind="rag_rebuild", payload={"source": "unit"}, dedupe_key="rag:unit")
        assert_true(duplicate.get("job_id") == job.get("job_id"), "dedupe should return existing active job")
        claimed = service.claim(worker_id="json-worker")
        assert_equal(len(claimed), 1, "one job should be claimed")
        assert_equal(claimed[0].get("status"), "running", "claimed job should be running")
        completed = service.complete(str(job["job_id"]), {"ok": True})
        assert_true(completed is not None, "completed job should exist")
        assert_equal(completed.get("status"), "succeeded", "completed job should succeed")
        stale = service.enqueue(kind="diagnostics", payload={"mode": "full"}, dedupe_key="diag:stale", max_attempts=3)
        service.update_file_job(
            str(stale["job_id"]),
            status="running",
            attempts=1,
            locked_by="dead-json-worker",
            locked_until=past_time(),
        )
        stale_summary = service.summary()
        assert_equal(stale_summary.get("stale_running"), 1, "summary should count stale running jobs")
        reclaimed = service.claim(worker_id="json-recovery")
        assert_equal(reclaimed[0].get("job_id"), stale.get("job_id"), "expired JSON job lock should be reclaimable")
        service.complete(str(stale["job_id"]), {"recovered": True})
        summary = service.summary()
        assert_equal(summary.get("succeeded"), 2, "summary should count recovered job")
    finally:
        restore_env("WECHAT_STORAGE_BACKEND", old_backend)


def check_postgres_work_queue_roundtrip(dsn: str) -> None:
    old_backend = os.environ.get("WECHAT_STORAGE_BACKEND")
    old_dsn = os.environ.get("WECHAT_POSTGRES_DSN")
    os.environ["WECHAT_STORAGE_BACKEND"] = "postgres"
    os.environ["WECHAT_POSTGRES_DSN"] = dsn
    tenant_id = "hardening_pg_test"
    try:
        config = load_storage_config()
        store = get_postgres_store(tenant_id=tenant_id, config=config)
        store.initialize_schema()
        store.execute(f"DELETE FROM {store.schema}.work_queue_jobs WHERE tenant_id = %s", [tenant_id])
        service = WorkQueueService(tenant_id=tenant_id)
        job = service.enqueue(kind="upload_learning", payload={"upload_id": "u1"}, dedupe_key="upload:u1")
        duplicate = service.enqueue(kind="upload_learning", payload={"upload_id": "u1"}, dedupe_key="upload:u1")
        assert_equal(duplicate.get("job_id"), job.get("job_id"), "PostgreSQL dedupe should return existing job")
        claimed = service.claim(worker_id="pg-worker")
        assert_equal(len(claimed), 1, "PostgreSQL should claim one job")
        failed = service.fail(str(job["job_id"]), "temporary", retry=True)
        assert_equal(failed.get("status"), "pending", "retryable failure should return to pending")
        claimed_again = service.claim(worker_id="pg-worker")
        assert_equal(len(claimed_again), 1, "retried job should be claimable")
        completed = service.complete(str(job["job_id"]), {"learned": True})
        assert_equal(completed.get("status"), "succeeded", "PostgreSQL job should complete")
        stale = service.enqueue(kind="rag_rebuild", payload={"source": "stale"}, dedupe_key="rag:stale", max_attempts=3)
        store.execute(
            f"""
            UPDATE {store.schema}.work_queue_jobs
            SET status = 'running',
                attempts = 1,
                locked_by = 'dead-pg-worker',
                locked_until = now() - interval '10 seconds',
                updated_at = now()
            WHERE tenant_id = %s AND job_id = %s
            """,
            [tenant_id, stale["job_id"]],
        )
        stale_summary = service.summary()
        assert_equal(stale_summary.get("stale_running"), 1, "PostgreSQL summary should count stale running jobs")
        reclaimed = service.claim(worker_id="pg-recovery")
        assert_equal(reclaimed[0].get("job_id"), stale.get("job_id"), "expired PostgreSQL job lock should be reclaimable")
        service.complete(str(stale["job_id"]), {"recovered": True})
        summary = service.summary()
        assert_equal(summary.get("succeeded"), 2, "PostgreSQL summary should count recovered job")
    finally:
        restore_env("WECHAT_STORAGE_BACKEND", old_backend)
        restore_env("WECHAT_POSTGRES_DSN", old_dsn)


def check_json_handoff_roundtrip() -> None:
    old_backend = os.environ.get("WECHAT_STORAGE_BACKEND")
    os.environ["WECHAT_STORAGE_BACKEND"] = "json"
    path = TEST_ROOT / "json_handoff_cases.json"
    if path.exists():
        path.unlink()
    try:
        store = HandoffStore(tenant_id="hardening_json_test", path=path)
        case = store.create_case(
            {
                "target": "文件传输助手",
                "reason": "discount_requires_approval",
                "message_ids": ["m1"],
                "message_contents": ["能不能再便宜点"],
                "reply_text": "我先帮您请示一下。",
            }
        )
        duplicate = store.create_case(
            {
                "target": "文件传输助手",
                "reason": "discount_requires_approval",
                "message_ids": ["m1"],
                "message_contents": ["能不能再便宜点"],
                "reply_text": "我先帮您请示一下。",
            }
        )
        assert_equal(duplicate.get("case_id"), case.get("case_id"), "JSON handoff should dedupe by message id")
        assert_true(duplicate.get("deduped") is True, "JSON duplicate handoff should be marked")
        assert_equal(store.summary().get("open"), 1, "JSON handoff summary should count open case")
        resolved = store.update_status(str(case["case_id"]), "resolved", {"operator": "test"})
        assert_equal(resolved.get("status"), "resolved", "JSON handoff case should resolve")
    finally:
        restore_env("WECHAT_STORAGE_BACKEND", old_backend)


def check_postgres_handoff_roundtrip(dsn: str) -> None:
    old_backend = os.environ.get("WECHAT_STORAGE_BACKEND")
    old_dsn = os.environ.get("WECHAT_POSTGRES_DSN")
    os.environ["WECHAT_STORAGE_BACKEND"] = "postgres"
    os.environ["WECHAT_POSTGRES_DSN"] = dsn
    tenant_id = "hardening_pg_test"
    try:
        config = load_storage_config()
        store = get_postgres_store(tenant_id=tenant_id, config=config)
        store.initialize_schema()
        store.execute(f"DELETE FROM {store.schema}.handoff_cases WHERE tenant_id = %s", [tenant_id])
        handoffs = HandoffStore(tenant_id=tenant_id)
        case = handoffs.create_case(
            {
                "target": "文件传输助手",
                "reason": "payment_terms_requires_approval",
                "message_ids": ["risk-1"],
                "message_contents": ["能不能月结"],
                "reply_text": "我先帮您请示一下。",
            }
        )
        duplicate = handoffs.create_case(
            {
                "target": "文件传输助手",
                "reason": "payment_terms_requires_approval",
                "message_ids": ["risk-1"],
                "message_contents": ["能不能月结"],
                "reply_text": "我先帮您请示一下。",
            }
        )
        assert_equal(duplicate.get("case_id"), case.get("case_id"), "PostgreSQL handoff should dedupe by message id")
        assert_true(duplicate.get("deduped") is True, "PostgreSQL duplicate handoff should be marked")
        assert_equal(handoffs.summary().get("open"), 1, "PostgreSQL handoff summary should count open case")
        ignored = handoffs.update_status(str(case["case_id"]), "ignored", {"operator": "test"})
        assert_equal(ignored.get("status"), "ignored", "PostgreSQL handoff case should ignore")
    finally:
        restore_env("WECHAT_STORAGE_BACKEND", old_backend)
        restore_env("WECHAT_POSTGRES_DSN", old_dsn)


def check_json_runtime_monitor_readiness() -> None:
    old_backend = os.environ.get("WECHAT_STORAGE_BACKEND")
    os.environ["WECHAT_STORAGE_BACKEND"] = "json"
    path = TEST_ROOT / "json_heartbeats.json"
    if path.exists():
        path.unlink()
    try:
        monitor = RuntimeMonitor(tenant_id="hardening_json_monitor", path=path)
        item = monitor.heartbeat("listener", status="ok", message="alive")
        assert_equal(item.get("component_id"), "listener", "heartbeat should store component")
        monitor.heartbeat("llm", status="warning", message="rate limit")
        report = monitor.readiness()
        assert_true("storage" in report, "readiness should include storage")
        assert_true("work_queue" in report, "readiness should include queue")
        assert_true("handoffs" in report, "readiness should include handoffs")
        assert_true(report.get("attention_items"), "readiness should include attention items for warning heartbeat")
    finally:
        restore_env("WECHAT_STORAGE_BACKEND", old_backend)


def check_postgres_runtime_monitor_readiness(dsn: str) -> None:
    old_backend = os.environ.get("WECHAT_STORAGE_BACKEND")
    old_dsn = os.environ.get("WECHAT_POSTGRES_DSN")
    os.environ["WECHAT_STORAGE_BACKEND"] = "postgres"
    os.environ["WECHAT_POSTGRES_DSN"] = dsn
    tenant_id = "hardening_pg_monitor"
    try:
        config = load_storage_config()
        store = get_postgres_store(tenant_id=tenant_id, config=config)
        store.initialize_schema()
        store.execute(f"DELETE FROM {store.schema}.runtime_heartbeats WHERE tenant_id = %s", [tenant_id])
        monitor = RuntimeMonitor(tenant_id=tenant_id)
        item = monitor.heartbeat("admin", status="ok", payload={"port": 8765})
        assert_equal(item.get("component_id"), "admin", "PostgreSQL heartbeat should store component")
        monitor.heartbeat("listener", status="warning", message="stale poll")
        report = monitor.readiness()
        assert_true(report.get("storage", {}).get("postgres_ok"), "PostgreSQL readiness should confirm storage")
        assert_true(any(hb.get("component_id") == "admin" for hb in report.get("heartbeats", [])), "readiness should list heartbeat")
        assert_true(report.get("attention_items"), "PostgreSQL readiness should include attention items")
    finally:
        restore_env("WECHAT_STORAGE_BACKEND", old_backend)
        restore_env("WECHAT_POSTGRES_DSN", old_dsn)


def restore_env(name: str, value: str | None) -> None:
    if value is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = value


def past_time() -> str:
    return (datetime.now() - timedelta(seconds=10)).isoformat(timespec="seconds")


def check_name(check: Callable[..., Any]) -> str:
    return getattr(check, "__name__", "unknown_check")


def assert_true(value: bool, message: str) -> None:
    if not value:
        raise AssertionError(message)


def assert_equal(actual: Any, expected: Any, message: str) -> None:
    if actual != expected:
        raise AssertionError(f"{message}: expected {expected!r}, got {actual!r}")


if __name__ == "__main__":
    raise SystemExit(main())
