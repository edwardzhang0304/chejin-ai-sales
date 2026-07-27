"""Non-blocking host-side jobs for the optional vehicle-image retrieval module.

This is deliberately outside the portable core.  Upload and Dafengche sync
callers only enqueue work; their successful persistence never depends on a
vision provider, network request, or image index outcome.
"""

from __future__ import annotations

import threading
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Callable

from apps.wechat_ai_customer_service.admin_backend.services.customer_service_settings import (
    CustomerServiceSettings,
    vehicle_image_retrieval_api_key_present,
)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


IndexRunner = Callable[[str, str], dict[str, Any]]


def _default_runner(product_id: str, tenant_id: str) -> dict[str, Any]:
    # Lazy imports keep the queue importable without product-master/vision code
    # and prevent the integration facade from importing this job module back.
    from apps.wechat_ai_customer_service.product_master import ProductMasterStore
    from .integration import index_product_vehicle_images

    settings = CustomerServiceSettings(tenant_id=tenant_id).get()
    return index_product_vehicle_images(
        product_id,
        force=True,
        store=ProductMasterStore(tenant_id=tenant_id),
        config=settings,
    )


class VehicleImageIndexTaskQueue:
    """Coalescing in-process task queue with no reply/RPA ownership."""

    def __init__(self, *, runner: IndexRunner | None = None, max_workers: int = 1) -> None:
        self._runner = runner or _default_runner
        self._executor = ThreadPoolExecutor(max_workers=max(1, int(max_workers or 1)), thread_name_prefix="vehicle-image-index")
        self._lock = threading.RLock()
        self._jobs: dict[tuple[str, str], dict[str, Any]] = {}

    def enqueue(self, product_id: str, *, tenant_id: str, cause: str) -> dict[str, Any]:
        product = str(product_id or "").strip()
        tenant = str(tenant_id or "").strip()
        if not product or not tenant:
            return {"accepted": False, "state": "rejected", "reason": "vehicle_image_index_scope_missing"}
        key = (tenant, product)
        with self._lock:
            job = self._jobs.setdefault(
                key,
                {
                    "tenant_id": tenant,
                    "product_id": product,
                    "requested_version": 0,
                    "completed_version": 0,
                    "state": "idle",
                    "causes": [],
                    "last_result": {},
                    "last_error": "",
                    "updated_at": "",
                    "future": None,
                },
            )
            job["requested_version"] = int(job.get("requested_version") or 0) + 1
            if str(cause or "") and str(cause) not in job["causes"]:
                job["causes"].append(str(cause))
            job["updated_at"] = now_iso()
            future = job.get("future")
            if isinstance(future, Future) and not future.done():
                job["state"] = "queued"
                return self._public_job(job, accepted=True, reason="vehicle_image_index_coalesced")
            job["state"] = "queued"
            job["future"] = self._executor.submit(self._run, key)
            return self._public_job(job, accepted=True, reason="vehicle_image_index_queued")

    def status(self, product_id: str, *, tenant_id: str) -> dict[str, Any]:
        key = (str(tenant_id or "").strip(), str(product_id or "").strip())
        with self._lock:
            job = self._jobs.get(key)
            if not job:
                return {"state": "not_queued", "tenant_id": key[0], "product_id": key[1], "causes": []}
            return self._public_job(job, accepted=True, reason="")

    def _run(self, key: tuple[str, str]) -> None:
        for _ in range(4):
            with self._lock:
                job = self._jobs.get(key)
                if not job:
                    return
                requested_version = int(job.get("requested_version") or 0)
                job["state"] = "running"
                job["updated_at"] = now_iso()
            try:
                result = self._runner(key[1], key[0])
                success = bool(isinstance(result, dict) and result.get("ok"))
                error = "" if success else str((result or {}).get("reason") or "vehicle_image_index_failed")
            except Exception as exc:  # noqa: BLE001 - failure must stay isolated from caller
                result = {"ok": False, "reason": "vehicle_image_index_runner_failed", "error_type": type(exc).__name__}
                success = False
                error = str(result["reason"])
            with self._lock:
                job = self._jobs.get(key)
                if not job:
                    return
                job["completed_version"] = requested_version
                job["last_result"] = _compact_result(result)
                job["last_error"] = error
                job["updated_at"] = now_iso()
                newer_request = int(job.get("requested_version") or 0) > requested_version
                if newer_request:
                    job["state"] = "queued"
                    continue
                job["state"] = "completed" if success else "failed"
                return
        with self._lock:
            job = self._jobs.get(key)
            if job:
                job["state"] = "queued"
                job["last_error"] = "vehicle_image_index_coalesce_limit_reached"
                job["updated_at"] = now_iso()
                # A burst that lasts longer than one worker turn must still
                # get a final refresh.  The prior worker is returning now;
                # the single-worker executor serializes this follow-up task.
                job["future"] = self._executor.submit(self._run, key)

    @staticmethod
    def _public_job(job: dict[str, Any], *, accepted: bool, reason: str) -> dict[str, Any]:
        return {
            "accepted": accepted,
            "reason": reason,
            "state": str(job.get("state") or "idle"),
            "tenant_id": str(job.get("tenant_id") or ""),
            "product_id": str(job.get("product_id") or ""),
            "requested_version": int(job.get("requested_version") or 0),
            "completed_version": int(job.get("completed_version") or 0),
            "causes": [str(item) for item in (job.get("causes") or []) if str(item)],
            "last_error": str(job.get("last_error") or ""),
            "last_result": dict(job.get("last_result") or {}),
            "updated_at": str(job.get("updated_at") or ""),
        }

    def shutdown(self) -> None:
        self._executor.shutdown(wait=True, cancel_futures=False)


def _compact_result(value: dict[str, Any] | None) -> dict[str, Any]:
    result = value if isinstance(value, dict) else {}
    state = result.get("state") if isinstance(result.get("state"), dict) else {}
    return {
        "ok": bool(result.get("ok", False)),
        "reason": str(result.get("reason") or ""),
        "indexed_count": int(result.get("indexed_count") or 0),
        "source_count": int(result.get("source_count") or 0),
        "index_state": str(state.get("status") or ""),
        "index_current": bool(state.get("current", False)),
    }


_DEFAULT_QUEUE = VehicleImageIndexTaskQueue()


def enqueue_vehicle_image_index(product_id: str, *, tenant_id: str, cause: str) -> dict[str, Any]:
    """Queue only configured tenant jobs; caller persistence remains independent."""

    settings = CustomerServiceSettings(tenant_id=tenant_id).get()
    if not bool((settings.get("vehicle_image_retrieval") or {}).get("enabled", True)):
        return {"accepted": False, "state": "not_queued", "reason": "vehicle_image_retrieval_disabled"}
    if not vehicle_image_retrieval_api_key_present(settings):
        return {"accepted": False, "state": "not_queued", "reason": "vehicle_image_retrieval_provider_not_configured"}
    return _DEFAULT_QUEUE.enqueue(product_id, tenant_id=tenant_id, cause=cause)


def vehicle_image_index_job_status(product_id: str, *, tenant_id: str) -> dict[str, Any]:
    return _DEFAULT_QUEUE.status(product_id, tenant_id=tenant_id)


def reset_vehicle_image_index_jobs_for_tests() -> None:
    """Replace the global queue after tests; production callers never use this."""

    global _DEFAULT_QUEUE
    _DEFAULT_QUEUE.shutdown()
    _DEFAULT_QUEUE = VehicleImageIndexTaskQueue()
