"""Durable work queue for long-running admin/runtime tasks."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from apps.wechat_ai_customer_service.knowledge_paths import active_tenant_id
from apps.wechat_ai_customer_service.storage import get_postgres_store, load_storage_config


PROJECT_ROOT = Path(__file__).resolve().parents[4]
QUEUE_PATH = PROJECT_ROOT / "runtime" / "apps" / "wechat_ai_customer_service" / "admin" / "work_queue.json"
ACTIVE_STATUSES = {"pending", "running"}
TERMINAL_STATUSES = {"succeeded", "failed", "cancelled"}
VALID_STATUSES = ACTIVE_STATUSES | TERMINAL_STATUSES


class WorkQueueService:
    def __init__(self, tenant_id: str | None = None, path: Path | None = None) -> None:
        self.tenant_id = active_tenant_id(tenant_id)
        self.path = path or QUEUE_PATH

    def enqueue(
        self,
        *,
        kind: str,
        payload: dict[str, Any] | None = None,
        queue: str = "default",
        priority: int = 5,
        dedupe_key: str = "",
        max_attempts: int = 3,
        available_at: str = "",
    ) -> dict[str, Any]:
        now_text = now()
        payload = payload or {}
        queue = queue or "default"
        dedupe_key = dedupe_key or ""
        if dedupe_key:
            existing = self.find_active_dedupe(queue=queue, dedupe_key=dedupe_key)
            if existing:
                existing["deduped"] = True
                return existing
        job = {
            "tenant_id": self.tenant_id,
            "job_id": "job_" + stable_digest(f"{self.tenant_id}:{queue}:{kind}:{dedupe_key}:{now_text}:{json.dumps(payload, ensure_ascii=False, sort_keys=True)}", 20),
            "queue": queue,
            "kind": kind or "generic",
            "status": "pending",
            "priority": int(priority or 5),
            "dedupe_key": dedupe_key,
            "attempts": 0,
            "max_attempts": int(max_attempts or 3),
            "available_at": available_at or now_text,
            "locked_until": None,
            "locked_by": "",
            "payload": payload,
            "result": {},
            "error": "",
            "created_at": now_text,
            "updated_at": now_text,
            "finished_at": None,
        }
        db = self.db()
        if db:
            return db.enqueue_job(self.tenant_id, job)
        jobs = self.read_jobs()
        jobs.append(job)
        self.write_jobs(jobs)
        return job

    def claim(self, *, queue: str = "default", worker_id: str = "", limit: int = 1, lock_seconds: int = 300) -> list[dict[str, Any]]:
        db = self.db()
        if db:
            return db.claim_jobs(self.tenant_id, queue=queue, worker_id=worker_id, limit=limit, lock_seconds=lock_seconds)
        jobs = self.read_jobs()
        claimed: list[dict[str, Any]] = []
        now_text = now()
        locked_until = (datetime.now() + timedelta(seconds=max(1, int(lock_seconds or 300)))).isoformat(timespec="seconds")
        for job in sorted(jobs, key=lambda item: (int(item.get("priority", 5) or 5), str(item.get("created_at") or ""))):
            if len(claimed) >= max(1, int(limit or 1)):
                break
            if job.get("tenant_id") != self.tenant_id or job.get("queue") != queue:
                continue
            status = str(job.get("status") or "")
            claimable_pending = status == "pending" and is_due(str(job.get("available_at") or ""))
            claimable_expired = status == "running" and is_lock_expired(str(job.get("locked_until") or "")) and int(job.get("attempts", 0) or 0) < int(job.get("max_attempts", 3) or 3)
            if not (claimable_pending or claimable_expired):
                continue
            job["status"] = "running"
            job["attempts"] = int(job.get("attempts", 0) or 0) + 1
            job["locked_by"] = worker_id or "worker"
            job["locked_until"] = locked_until
            job["updated_at"] = now_text
            claimed.append(dict(job))
        self.write_jobs(jobs)
        return claimed

    def complete(self, job_id: str, result: dict[str, Any] | None = None) -> dict[str, Any] | None:
        db = self.db()
        if db:
            return db.complete_job(self.tenant_id, job_id, result or {})
        return self.update_file_job(job_id, status="succeeded", result=result or {}, error="", finished=True)

    def fail(self, job_id: str, error: str, *, retry: bool = True) -> dict[str, Any] | None:
        db = self.db()
        if db:
            return db.fail_job(self.tenant_id, job_id, error, retry=retry)
        job = self.get(job_id)
        if not job:
            return None
        attempts = int(job.get("attempts", 0) or 0)
        max_attempts = int(job.get("max_attempts", 3) or 3)
        status = "pending" if retry and attempts < max_attempts else "failed"
        updates: dict[str, Any] = {"status": status, "error": error, "finished": status == "failed"}
        if status == "pending":
            updates.update({"locked_until": None, "locked_by": "", "finished_at": None})
        return self.update_file_job(job_id, **updates)

    def cancel(self, job_id: str, reason: str = "") -> dict[str, Any] | None:
        db = self.db()
        if db:
            return db.cancel_job(self.tenant_id, job_id, reason)
        return self.update_file_job(job_id, status="cancelled", error=reason, finished=True)

    def get(self, job_id: str) -> dict[str, Any] | None:
        db = self.db()
        if db:
            return db.get_job(self.tenant_id, job_id)
        for job in self.read_jobs():
            if job.get("tenant_id") == self.tenant_id and job.get("job_id") == job_id:
                return job
        return None

    def list_jobs(self, *, status: str = "all", limit: int = 100) -> list[dict[str, Any]]:
        db = self.db()
        if db:
            return db.list_jobs(self.tenant_id, status=status, limit=limit)
        jobs = [job for job in self.read_jobs() if job.get("tenant_id") == self.tenant_id]
        if status and status != "all":
            jobs = [job for job in jobs if job.get("status") == status]
        jobs.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
        return jobs[: max(1, min(int(limit or 100), 500))]

    def summary(self) -> dict[str, Any]:
        db = self.db()
        if db:
            return db.job_summary(self.tenant_id)
        counts: dict[str, int] = {}
        stale_running = 0
        for job in self.list_jobs(status="all", limit=500):
            status = str(job.get("status") or "unknown")
            counts[status] = counts.get(status, 0) + 1
            if status == "running" and is_lock_expired(str(job.get("locked_until") or "")):
                stale_running += 1
        return {
            "total": sum(counts.values()),
            "pending": counts.get("pending", 0),
            "running": counts.get("running", 0),
            "stale_running": stale_running,
            "succeeded": counts.get("succeeded", 0),
            "failed": counts.get("failed", 0),
            "cancelled": counts.get("cancelled", 0),
            "by_status": counts,
        }

    def find_active_dedupe(self, *, queue: str, dedupe_key: str) -> dict[str, Any] | None:
        if not dedupe_key:
            return None
        for job in self.list_jobs(status="all", limit=500):
            if job.get("queue") == queue and job.get("dedupe_key") == dedupe_key and job.get("status") in ACTIVE_STATUSES:
                return job
        return None

    def update_file_job(self, job_id: str, **updates: Any) -> dict[str, Any] | None:
        jobs = self.read_jobs()
        found: dict[str, Any] | None = None
        now_text = now()
        for job in jobs:
            if job.get("tenant_id") == self.tenant_id and job.get("job_id") == job_id:
                job.update(updates)
                job["updated_at"] = now_text
                if updates.get("finished"):
                    job["finished_at"] = now_text
                    job["locked_until"] = None
                    job["locked_by"] = ""
                job.pop("finished", None)
                found = dict(job)
                break
        self.write_jobs(jobs)
        return found

    def read_jobs(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return []
        if isinstance(payload, dict):
            return list(payload.get("jobs", []) or [])
        if isinstance(payload, list):
            return payload
        return []

    def write_jobs(self, jobs: list[dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"jobs": jobs}, ensure_ascii=False, indent=2), encoding="utf-8")

    def db(self):
        config = load_storage_config()
        store = get_postgres_store(tenant_id=self.tenant_id, config=config)
        if not store.availability().ok:
            return None
        store.initialize_schema()
        return store


def stable_digest(text: str, length: int = 16) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:length]


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def is_due(value: str | None) -> bool:
    parsed = parse_time(value)
    return parsed is None or parsed <= datetime.now()


def is_lock_expired(value: str | None) -> bool:
    parsed = parse_time(value)
    return parsed is not None and parsed <= datetime.now()
