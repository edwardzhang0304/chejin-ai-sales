from __future__ import annotations

import hmac
from typing import Any


MINIMUM_RUNTIME_STABLE_SECONDS = 1.0
MINIMUM_RUNTIME_HEALTH_SAMPLES = 2


def validate_runtime_health(runtime_health: dict[str, Any]) -> dict[str, Any]:
    """Validate the one shared post-update runtime-liveness contract."""

    if runtime_health.get("ready") is not True:
        raise RuntimeError("UPDATE_RUNTIME_NOT_READY")
    if runtime_health.get("ui_event_loop_alive") is not True:
        raise RuntimeError("UPDATE_RUNTIME_EVENT_LOOP_NOT_READY")
    try:
        sample_count = int(runtime_health.get("stable_sample_count") or 0)
        stable_for_ms = int(runtime_health.get("stable_for_ms") or 0)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("UPDATE_RUNTIME_HEALTH_INVALID") from exc
    if sample_count < MINIMUM_RUNTIME_HEALTH_SAMPLES:
        raise RuntimeError("UPDATE_RUNTIME_STABILITY_NOT_PROVEN")
    if stable_for_ms < round(MINIMUM_RUNTIME_STABLE_SECONDS * 1000):
        raise RuntimeError("UPDATE_RUNTIME_STABILITY_NOT_PROVEN")
    required_threads = runtime_health.get("required_threads")
    threads = runtime_health.get("threads")
    if not isinstance(required_threads, list) or not isinstance(threads, dict):
        raise RuntimeError("UPDATE_RUNTIME_HEALTH_INVALID")
    for name in required_threads:
        item = threads.get(str(name))
        if not isinstance(item, dict):
            raise RuntimeError("UPDATE_RUNTIME_THREAD_NOT_READY")
        if item.get("entered_loop") is not True or item.get("alive") is not True:
            raise RuntimeError("UPDATE_RUNTIME_THREAD_NOT_READY")
    if runtime_health.get("startup_failures") not in ([], None):
        raise RuntimeError("UPDATE_RUNTIME_STARTUP_FAILED")
    return dict(runtime_health)


def validate_authenticated_runtime_marker(
    marker: dict[str, Any],
    *,
    request_id: str,
    target_version: str,
    token_sha256: str,
) -> dict[str, Any]:
    """Bind liveness evidence to exactly one version and update request."""

    if marker.get("healthy") is not True:
        raise RuntimeError("UPDATE_HEALTH_MARKER_INVALID")
    if str(marker.get("update_request_id") or "") != str(request_id or ""):
        raise RuntimeError("UPDATE_HEALTH_MARKER_REQUEST_MISMATCH")
    if str(marker.get("version") or "") != str(target_version or ""):
        raise RuntimeError("UPDATE_HEALTH_MARKER_VERSION_MISMATCH")
    if not hmac.compare_digest(
        str(marker.get("one_time_token_sha256") or ""),
        str(token_sha256 or ""),
    ):
        raise RuntimeError("UPDATE_HEALTH_MARKER_TOKEN_MISMATCH")
    runtime_health = marker.get("runtime_health")
    if not isinstance(runtime_health, dict):
        raise RuntimeError("UPDATE_RUNTIME_HEALTH_INVALID")
    return validate_runtime_health(runtime_health)
