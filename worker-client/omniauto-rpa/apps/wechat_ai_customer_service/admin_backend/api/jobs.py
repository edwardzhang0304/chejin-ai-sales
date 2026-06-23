"""Durable work-queue APIs."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from ..services.work_queue import WorkQueueService


router = APIRouter(prefix="/api/jobs", tags=["jobs"])
service = WorkQueueService()


@router.get("")
def list_jobs(status: str = Query("all"), limit: int = Query(100, ge=1, le=500)) -> dict[str, Any]:
    return {"ok": True, "items": service.list_jobs(status=status, limit=limit)}


@router.get("/summary")
def summary() -> dict[str, Any]:
    return {"ok": True, "summary": service.summary()}


@router.post("")
def enqueue(payload: dict[str, Any]) -> dict[str, Any]:
    job = service.enqueue(
        kind=str(payload.get("kind") or "generic"),
        payload=payload.get("payload") if isinstance(payload.get("payload"), dict) else {},
        queue=str(payload.get("queue") or "default"),
        priority=int(payload.get("priority", 5) or 5),
        dedupe_key=str(payload.get("dedupe_key") or ""),
        max_attempts=int(payload.get("max_attempts", 3) or 3),
        available_at=str(payload.get("available_at") or ""),
    )
    return {"ok": True, "item": job}


@router.post("/claim")
def claim(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = payload or {}
    return {
        "ok": True,
        "items": service.claim(
            queue=str(payload.get("queue") or "default"),
            worker_id=str(payload.get("worker_id") or "admin"),
            limit=int(payload.get("limit", 1) or 1),
            lock_seconds=int(payload.get("lock_seconds", 300) or 300),
        ),
    }


@router.post("/{job_id}/complete")
def complete(job_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    item = service.complete(job_id, result=(payload or {}).get("result") if isinstance((payload or {}).get("result"), dict) else {})
    if not item:
        raise HTTPException(status_code=404, detail=f"job not found: {job_id}")
    return {"ok": True, "item": item}


@router.post("/{job_id}/fail")
def fail(job_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = payload or {}
    item = service.fail(job_id, error=str(payload.get("error") or "failed"), retry=payload.get("retry", True) is not False)
    if not item:
        raise HTTPException(status_code=404, detail=f"job not found: {job_id}")
    return {"ok": True, "item": item}


@router.post("/{job_id}/cancel")
def cancel(job_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    item = service.cancel(job_id, reason=str((payload or {}).get("reason") or "cancelled"))
    if not item:
        raise HTTPException(status_code=404, detail=f"job not found: {job_id}")
    return {"ok": True, "item": item}

