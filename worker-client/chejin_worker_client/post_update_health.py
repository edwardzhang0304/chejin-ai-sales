from __future__ import annotations

import hashlib
import hmac
import json
import os
from pathlib import Path
import sys
import time
from typing import Any, Callable

from . import __version__
from .client_update import PACKAGE_MANIFEST_NAME, hash_file
from .config import CONFIG
from .storage import connect
from .update_data_snapshot import assert_protected_update_snapshot
from .update_runtime_health_contract import (
    MINIMUM_RUNTIME_HEALTH_SAMPLES,
    MINIMUM_RUNTIME_STABLE_SECONDS,
    validate_authenticated_runtime_marker,
    validate_runtime_health,
)


def _load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError("UPDATE_STARTUP_DOCUMENT_INVALID")
    return payload


def _token_sha256(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def verify_post_update_startup(plan_path: Path, token: str) -> dict[str, Any]:
    plan = _load_json(plan_path)
    if not hmac.compare_digest(
        str(plan.get("one_time_token_sha256") or ""),
        _token_sha256(token),
    ):
        raise RuntimeError("UPDATE_STARTUP_TOKEN_INVALID")
    if str(plan.get("target_version") or "") != __version__:
        raise RuntimeError("UPDATE_STARTUP_VERSION_MISMATCH")

    current_dir = Path(str(plan.get("current_program_dir") or "")).resolve(strict=True)
    executable = Path(sys.executable).resolve(strict=True)
    if executable.parent != current_dir:
        raise RuntimeError("UPDATE_STARTUP_PROGRAM_DIR_MISMATCH")

    manifest_path = current_dir / PACKAGE_MANIFEST_NAME
    manifest = _load_json(manifest_path)
    if str(manifest.get("version") or "") != __version__:
        raise RuntimeError("UPDATE_STARTUP_MANIFEST_VERSION_MISMATCH")
    files = manifest.get("files")
    if not isinstance(files, dict) or not files:
        raise RuntimeError("UPDATE_STARTUP_MANIFEST_FILES_MISSING")
    for relative, expected in files.items():
        relative_path = Path(str(relative or ""))
        if relative_path.is_absolute() or ".." in relative_path.parts:
            raise RuntimeError("UPDATE_STARTUP_MANIFEST_PATH_INVALID")
        target = (current_dir / relative_path).resolve(strict=True)
        target.relative_to(current_dir)
        if hash_file(target) != str(expected or "").lower():
            raise RuntimeError("UPDATE_STARTUP_FILE_HASH_MISMATCH")

    if not CONFIG.app_dir.exists():
        CONFIG.app_dir.mkdir(parents=True, exist_ok=True)
    conn = connect()
    try:
        conn.execute("SELECT 1").fetchone()
    finally:
        conn.close()
    protected = plan.get("protected_data_snapshot")
    if not isinstance(protected, dict):
        raise RuntimeError("UPDATE_STARTUP_DATA_SNAPSHOT_MISSING")
    assert_protected_update_snapshot(protected)
    return plan


def authenticated_healthy_marker(
    plan: dict[str, Any],
    token: str,
) -> dict[str, Any] | None:
    """Return a valid marker for this exact update request, otherwise None."""

    try:
        path = Path(str(plan.get("healthy_marker_path") or ""))
        if not path.is_absolute():
            return None
        marker = _load_json(path)
        validate_authenticated_runtime_marker(
            marker,
            request_id=str(plan.get("update_request_id") or ""),
            target_version=str(plan.get("target_version") or ""),
            token_sha256=_token_sha256(token),
        )
        return marker
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, RuntimeError):
        return None


def write_healthy_marker(
    plan: dict[str, Any],
    token: str,
    *,
    runtime_health: dict[str, Any],
) -> Path:
    path = Path(str(plan.get("healthy_marker_path") or ""))
    if not path.is_absolute():
        raise RuntimeError("UPDATE_HEALTH_MARKER_PATH_INVALID")
    proven_health = validate_runtime_health(runtime_health)
    payload = {
        "schema_version": 2,
        "healthy": True,
        "update_request_id": plan.get("update_request_id"),
        "version": __version__,
        "one_time_token_sha256": _token_sha256(token),
        "pid": os.getpid(),
        "runtime_health": proven_health,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)
    return path


class RuntimeHealthGate:
    """Require repeated live production-loop samples before declaring health.

    The UI event loop calls :meth:`observe` on a short timer.  A single good
    instant can never write the marker, and a loop that starts and immediately
    exits resets the stable window instead of being mistaken for a successful
    update.
    """

    def __init__(
        self,
        plan: dict[str, Any],
        token: str,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        minimum_stable_seconds: float = MINIMUM_RUNTIME_STABLE_SECONDS,
        minimum_samples: int = MINIMUM_RUNTIME_HEALTH_SAMPLES,
        timeout_seconds: float | None = None,
    ) -> None:
        self.plan = dict(plan)
        self.token = str(token)
        self.monotonic = monotonic
        self.minimum_stable_seconds = max(0.0, float(minimum_stable_seconds))
        self.minimum_samples = max(2, int(minimum_samples))
        planned_timeout = (
            timeout_seconds
            if timeout_seconds is not None
            else self.plan.get("health_timeout_seconds")
        )
        if planned_timeout is None:
            raise RuntimeError("UPDATE_RUNTIME_HEALTH_TIMEOUT_MISSING")
        try:
            self.timeout_seconds = float(planned_timeout)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("UPDATE_RUNTIME_HEALTH_TIMEOUT_INVALID") from exc
        if self.timeout_seconds < self.minimum_stable_seconds:
            raise RuntimeError("UPDATE_RUNTIME_HEALTH_TIMEOUT_INVALID")
        self.started_at = self.monotonic()
        self.ready_since: float | None = None
        self.ready_samples = 0
        self.completed = False

    def observe(self, snapshot: dict[str, Any]) -> Path | None:
        if self.completed:
            return Path(str(self.plan.get("healthy_marker_path") or ""))
        now = self.monotonic()
        if now - self.started_at > self.timeout_seconds:
            raise RuntimeError("UPDATE_RUNTIME_HEALTH_TIMEOUT")
        current = dict(snapshot) if isinstance(snapshot, dict) else {}
        if current.get("ready") is not True:
            self.ready_since = None
            self.ready_samples = 0
            return None
        if self.ready_since is None:
            self.ready_since = now
            self.ready_samples = 0
        self.ready_samples += 1
        stable_seconds = max(0.0, now - self.ready_since)
        if (
            self.ready_samples < self.minimum_samples
            or stable_seconds < self.minimum_stable_seconds
        ):
            return None
        evidence = {
            **current,
            "stable_sample_count": self.ready_samples,
            "stable_for_ms": round(stable_seconds * 1000),
        }
        marker = write_healthy_marker(
            self.plan,
            self.token,
            runtime_health=evidence,
        )
        self.completed = True
        return marker
