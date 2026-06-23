"""Background worker for the customer-service work queue."""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any

APP_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = APP_ROOT.parents[1]
for path in (PROJECT_ROOT, APP_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from apps.wechat_ai_customer_service.admin_backend.services.background_handlers import JOB_HANDLERS  # noqa: E402
from apps.wechat_ai_customer_service.admin_backend.services.work_queue import WorkQueueService  # noqa: E402
from apps.wechat_ai_customer_service.knowledge_paths import active_tenant_id, tenant_runtime_root  # noqa: E402

SHUTDOWN = False


def worker_pid_path(tenant_id: str, queue: str) -> Path:
    queue_key = "".join(ch if ch.isalnum() or ch in {"_", "-"} else "_" for ch in str(queue or "customer_service"))
    if queue_key == "customer_service":
        return tenant_runtime_root(tenant_id) / "customer_service" / "worker.pid.json"
    return tenant_runtime_root(tenant_id) / "workers" / f"{queue_key}.pid.json"


def worker_log_path(tenant_id: str) -> Path:
    return tenant_runtime_root(tenant_id) / "logs" / "background_worker.log"


def read_worker_pid_record(tenant_id: str, queue: str) -> dict[str, Any]:
    path = worker_pid_path(tenant_id, queue)
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def write_worker_pid_record(tenant_id: str, queue: str, payload: dict[str, Any]) -> None:
    path = worker_pid_path(tenant_id, queue)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".json.tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, path)


def clear_worker_pid_record(tenant_id: str, queue: str) -> None:
    try:
        worker_pid_path(tenant_id, queue).unlink()
    except FileNotFoundError:
        pass


def append_log(path: Path, payload: dict[str, Any]) -> None:
    record = {"created_at": datetime.now().isoformat(timespec="seconds"), **payload}
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def on_signal(signum: int, _frame: Any) -> None:
    global SHUTDOWN
    SHUTDOWN = True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--queue", default="customer_service")
    parser.add_argument("--interval-seconds", type=float, default=5.0)
    parser.add_argument("--limit", type=int, default=3)
    parser.add_argument("--lock-seconds", type=int, default=600)
    args = parser.parse_args()

    tenant_id = str(args.tenant_id).strip()
    queue = str(args.queue).strip()
    interval = max(1.0, float(args.interval_seconds))
    limit = max(1, int(args.limit))
    lock_seconds = max(30, int(args.lock_seconds))

    # singleton guard
    existing = read_worker_pid_record(tenant_id, queue)
    existing_pid = int(existing.get("pid") or 0)
    if existing_pid > 0:
        try:
            import psutil

            if psutil.Process(existing_pid).is_running():
                print(f"Background worker for {tenant_id} already running (PID={existing_pid}); exiting.", file=sys.stderr)
                return 0
        except (psutil.NoSuchProcess, ImportError):
            pass

    signal.signal(signal.SIGTERM, on_signal)
    if hasattr(signal, "SIGINT"):
        signal.signal(signal.SIGINT, on_signal)

    write_worker_pid_record(
        tenant_id,
        queue,
        {
            "pid": os.getpid(),
            "tenant_id": tenant_id,
            "queue": queue,
            "started_at": datetime.now().isoformat(timespec="seconds"),
        },
    )

    log_path = worker_log_path(tenant_id)
    append_log(
        log_path,
        {"event": "worker_start", "tenant_id": tenant_id, "queue": queue, "pid": os.getpid()},
    )
    print(f"Background worker for {tenant_id} starting with PID={os.getpid()}, queue={queue}", file=sys.stderr)

    work_queue = WorkQueueService(tenant_id=tenant_id)
    processed_count = 0
    error_count = 0

    while not SHUTDOWN:
        try:
            jobs = work_queue.claim(queue=queue, worker_id=f"bgw-{os.getpid()}", limit=limit, lock_seconds=lock_seconds)
        except Exception as exc:
            append_log(log_path, {"event": "claim_error", "error": repr(exc)})
            time.sleep(interval)
            continue

        if not jobs:
            time.sleep(interval)
            continue

        for job in jobs:
            job_id = str(job.get("job_id") or "")
            kind = str(job.get("kind") or "")
            payload = job.get("payload") or {}
            handler = JOB_HANDLERS.get(kind)

            if not handler:
                append_log(
                    log_path,
                    {"event": "unknown_kind", "job_id": job_id, "kind": kind},
                )
                work_queue.fail(job_id, f"Unknown job kind: {kind}", retry=False)
                continue

            started_at = time.time()
            try:
                result = handler(payload)
                duration = round(time.time() - started_at, 2)
                if result.get("ok"):
                    work_queue.complete(job_id, result)
                    processed_count += 1
                else:
                    work_queue.fail(job_id, result.get("error", "handler returned ok=False"), retry=True)
                    error_count += 1
                append_log(
                    log_path,
                    {
                        "event": "job_done",
                        "job_id": job_id,
                        "kind": kind,
                        "ok": result.get("ok"),
                        "duration": duration,
                        "result_summary": str(result)[:500],
                    },
                )
            except Exception as exc:
                duration = round(time.time() - started_at, 2)
                error_text = f"{exc}\n{traceback.format_exc()}"
                work_queue.fail(job_id, error_text, retry=True)
                error_count += 1
                append_log(
                    log_path,
                    {
                        "event": "job_error",
                        "job_id": job_id,
                        "kind": kind,
                        "duration": duration,
                        "error": error_text[:800],
                    },
                )

        # brief pause between batches
        time.sleep(0.5)

    append_log(
        log_path,
        {
            "event": "worker_stop",
            "tenant_id": tenant_id,
            "processed_count": processed_count,
            "error_count": error_count,
        },
    )
    clear_worker_pid_record(tenant_id, queue)
    print(f"Background worker for {tenant_id} shutting down. processed={processed_count}, errors={error_count}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
