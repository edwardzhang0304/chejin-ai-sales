from __future__ import annotations

import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
import zipfile
from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path
from typing import Any, Iterable

from . import __version__
from .action_journal import read_action_journal


INCIDENT_SCHEMA_VERSION = 1
MAX_LOG_ROWS = 200
MAX_EVIDENCE_BYTES = 100 * 1024 * 1024
INCIDENT_MERGE_WINDOW_SECONDS = 10 * 60
INCIDENT_SETTLE_WINDOW_SECONDS = 3.0
INCIDENT_RETENTION_DAYS = 30
INCIDENT_MAX_PACKAGES = 50
INCIDENT_MAX_TOTAL_BYTES = 2 * 1024 * 1024 * 1024
INCIDENT_MIN_FREE_BYTES = 128 * 1024 * 1024
INCIDENT_QUEUE_MAXSIZE = 64
_LOCK = threading.RLock()
_WORKER_LOCK = threading.Lock()
_WORKER_STOP = threading.Event()
_WORKER_WAKEUP: queue.Queue[str] = queue.Queue(maxsize=INCIDENT_QUEUE_MAXSIZE)
_WORKER_THREAD: threading.Thread | None = None
_SECRET_KEY_RE = re.compile(
    r"(?:token|api[_-]?key|authorization|password|passwd|secret|cookie)",
    re.IGNORECASE,
)
_PUBLIC_DIAGNOSTIC_KEYS = {"authorization_revision"}
_BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+\-/=]+")
_SK_RE = re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b")
_ALLOWED_TEXT_SUFFIXES = {".json", ".jsonl", ".log", ".txt", ".md", ".html", ".htm"}
_ALLOWED_BINARY_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
_FORBIDDEN_FILE_RE = re.compile(
    r"(?:\.sqlite3?(?:-|$)|\.db(?:-|$)|\.env(?:\.|$)|worker[_-]?token|api[_-]?key|secret)",
    re.IGNORECASE,
)


def _storage():
    from . import storage

    return storage


def incident_directory() -> Path:
    root = _storage().APP_DIR / "incidents"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _pending_directory() -> Path:
    root = incident_directory() / "pending"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _pending_occurrence_directory() -> Path:
    root = incident_directory() / "pending-occurrences"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _state_path() -> Path:
    return incident_directory() / "incident-state.json"


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.parent.mkdir(parents=True, exist_ok=True)
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _incident_state() -> dict[str, Any]:
    payload = _read_json(_state_path())
    fingerprints = payload.get("fingerprints")
    if not isinstance(fingerprints, dict):
        fingerprints = {}
    return {"fingerprints": fingerprints}


def _write_incident_state(payload: dict[str, Any]) -> None:
    _atomic_write_json(_state_path(), payload)


def _known_secret_values() -> set[str]:
    values = {
        value.strip()
        for key, value in os.environ.items()
        if _SECRET_KEY_RE.search(key) and value and len(value.strip()) >= 6
    }
    try:
        binding = _storage().load_binding()
    except Exception:
        binding = None
    if binding and str(binding.worker_token or "").strip():
        values.add(str(binding.worker_token).strip())
    return values


def _redact_text(value: str, secrets: set[str]) -> str:
    redacted = _BEARER_RE.sub("Bearer [REDACTED]", str(value or ""))
    redacted = _SK_RE.sub("sk-[REDACTED]", redacted)
    for secret in sorted(secrets, key=len, reverse=True):
        redacted = redacted.replace(secret, "[REDACTED]")
    return redacted


def _sanitize(value: Any, secrets: set[str]) -> Any:
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            result[key_text] = (
                "[REDACTED]"
                if key_text not in _PUBLIC_DIAGNOSTIC_KEYS
                and _SECRET_KEY_RE.search(key_text)
                else _sanitize(item, secrets)
            )
        return result
    if isinstance(value, (list, tuple, set)):
        return [_sanitize(item, secrets) for item in value]
    if isinstance(value, Path):
        return _redact_text(str(value), secrets)
    if isinstance(value, str):
        return _redact_text(value, secrets)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _redact_text(str(value), secrets)


def redact_diagnostic(value: Any) -> Any:
    """Return a secret-safe copy suitable for local logs and incident files."""

    return _sanitize(value, _known_secret_values())


def _build_identity() -> dict[str, str]:
    roots = [
        Path(getattr(sys, "_MEIPASS", "")),
        Path(__file__).resolve().parents[1],
        Path(sys.executable).resolve().parent,
    ]
    for root in roots:
        if not str(root):
            continue
        path = root / "runtime-build-identity.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict):
            return {
                "version": str(payload.get("version") or __version__),
                "git_commit": str(payload.get("git_commit") or "unknown"),
                "git_branch": str(payload.get("git_branch") or "unknown"),
            }
    root = Path(__file__).resolve().parents[2]
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
            timeout=2,
        ).stdout.strip()
        branch = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
            timeout=2,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        commit, branch = "unknown", "unknown"
    return {
        "version": __version__,
        "git_commit": commit or "unknown",
        "git_branch": branch or "detached",
    }


def _inside_app_dir(path: Path) -> bool:
    try:
        path.resolve().relative_to(_storage().APP_DIR.resolve())
        return True
    except (OSError, ValueError):
        return False


def _path_candidates(value: Any, *, key: str = "") -> Iterable[Path]:
    if isinstance(value, dict):
        for child_key, child in value.items():
            yield from _path_candidates(child, key=str(child_key))
        return
    if isinstance(value, (list, tuple)):
        for child in value:
            yield from _path_candidates(child, key=key)
        return
    if not isinstance(value, str) or not any(
        marker in key.lower()
        for marker in ("path", "screenshot", "review", "artifact", "evidence")
    ):
        return
    candidate = Path(value)
    if candidate.exists() and _inside_app_dir(candidate):
        yield candidate


def _evidence_files(paths: Iterable[Path]) -> list[Path]:
    files: list[Path] = []
    seen: set[Path] = set()
    total = 0
    for candidate in paths:
        children = candidate.rglob("*") if candidate.is_dir() else (candidate,)
        for path in children:
            if not path.is_file() or not _inside_app_dir(path):
                continue
            if _FORBIDDEN_FILE_RE.search(path.name):
                continue
            if path.suffix.lower() not in _ALLOWED_TEXT_SUFFIXES | _ALLOWED_BINARY_SUFFIXES:
                continue
            resolved = path.resolve()
            if resolved in seen:
                continue
            try:
                size = resolved.stat().st_size
            except OSError:
                continue
            if size > MAX_EVIDENCE_BYTES or total + size > MAX_EVIDENCE_BYTES:
                continue
            seen.add(resolved)
            files.append(resolved)
            total += size
    return files


def _write_json(archive: zipfile.ZipFile, name: str, value: Any, secrets: set[str]) -> None:
    archive.writestr(
        name,
        json.dumps(_sanitize(value, secrets), ensure_ascii=False, indent=2, sort_keys=True),
    )


def _action_journal_snapshot() -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    root = _storage().APP_DIR / "transactions" / "actions"
    for action_kind in ("send", "voice", "image", "add_friend"):
        for path in sorted((root / action_kind).glob("*.json")):
            payload = read_action_journal(path)
            if payload:
                result.append({"path": str(path), "payload": payload})
    return result


def _related_ids(value: Any) -> dict[str, str]:
    result: dict[str, str] = {}
    if not isinstance(value, dict):
        return result
    for key, item in value.items():
        key_text = str(key)
        if isinstance(item, dict):
            result.update(_related_ids(item))
        elif (
            key_text.endswith("_id")
            and not _SECRET_KEY_RE.search(key_text)
            and item is not None
        ):
            result[key_text] = str(item)
    return result


def _incident_scope(
    *,
    event: str,
    error_code: str | None,
    task_id: str | None,
    metadata: dict[str, Any],
) -> dict[str, str]:
    related = _related_ids(metadata)
    stable_related = {
        key: value
        for key, value in related.items()
        if key
        not in {
            "incident_id",
            "scan_id",
            "sidecar_run_id",
            "trace_id",
        }
    }
    stable_context = {
        key: str(metadata.get(key) or "")
        for key in (
            "thread_kind",
            "origin",
            "reason",
            "exception_type",
            "failure_step",
        )
        if metadata.get(key) not in (None, "")
        and not isinstance(metadata.get(key), (dict, list, tuple, set))
    }
    return {
        "event": str(event or "incident"),
        "error_code": str(error_code or ""),
        "task_id": str(task_id or ""),
        **stable_context,
        **stable_related,
    }


def _incident_fingerprint(scope: dict[str, str]) -> str:
    encoded = json.dumps(scope, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _within_merge_window(existing: dict[str, Any], now: datetime) -> bool:
    try:
        first_seen = datetime.fromisoformat(
            str(existing.get("first_seen_at") or "").replace("Z", "+00:00")
        )
    except ValueError:
        return False
    if first_seen.tzinfo is None:
        first_seen = first_seen.replace(tzinfo=timezone.utc)
    age = (now - first_seen.astimezone(timezone.utc)).total_seconds()
    return age <= INCIDENT_MERGE_WINDOW_SECONDS


def _occurrence_record(
    *,
    incident_id: str,
    occurred_at: datetime,
    message: str,
    metadata: dict[str, Any],
    traceback_text: str,
) -> dict[str, Any]:
    return {
        "occurrence_id": f"OCC-{occurred_at.strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:10]}",
        "incident_id": incident_id,
        "occurred_at": occurred_at.isoformat(),
        "message": message,
        "metadata": metadata,
        "traceback": traceback_text,
    }


def _queue_occurrence(occurrence: dict[str, Any]) -> None:
    occurrence_id = str(occurrence.get("occurrence_id") or "")
    if not occurrence_id:
        return
    _atomic_write_json(
        _pending_occurrence_directory() / f"{occurrence_id}.json",
        occurrence,
    )


def schedule_incident(
    *,
    event: str,
    error_code: str | None,
    message: str,
    task_id: str | None = None,
    metadata: dict[str, Any] | None = None,
    traceback_text: str | None = None,
    log_record_id: str | None = None,
    start_worker: bool = True,
) -> dict[str, Any]:
    """Persist a small request and let the evidence worker build the ZIP."""

    created_at = datetime.now(timezone.utc)
    safe_metadata = dict(redact_diagnostic(metadata or {}))
    safe_message = str(redact_diagnostic(message))
    safe_traceback = str(redact_diagnostic(traceback_text or ""))
    scope = _incident_scope(
        event=event,
        error_code=error_code,
        task_id=task_id,
        metadata=safe_metadata,
    )
    fingerprint = _incident_fingerprint(scope)
    with _LOCK:
        state = _incident_state()
        fingerprints = state["fingerprints"]
        existing = fingerprints.get(fingerprint)
        if (
            isinstance(existing, dict)
            and existing.get("active") is True
            and _within_merge_window(existing, created_at)
        ):
            incident_id = str(existing.get("incident_id") or "")
            existing["last_seen_at"] = created_at.isoformat()
            existing["occurrence_count"] = int(existing.get("occurrence_count") or 1) + 1
            fingerprints[fingerprint] = existing
            _write_incident_state(state)
            _queue_occurrence(
                _occurrence_record(
                    incident_id=incident_id,
                    occurred_at=created_at,
                    message=safe_message,
                    metadata=safe_metadata,
                    traceback_text=safe_traceback,
                )
            )
            if start_worker:
                start_incident_worker()
                _wake_incident_worker(incident_id)
            return {
                "incident_id": incident_id,
                "evidence_path": str(existing.get("evidence_path") or ""),
                "deduplicated": True,
                "pending": not Path(str(existing.get("evidence_path") or "")).is_file(),
            }
        if isinstance(existing, dict) and existing.get("active") is True:
            existing["active"] = False
            existing["merge_window_expired_at"] = created_at.isoformat()

        incident_id = f"INC-{created_at.strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:10]}"
        output = incident_directory() / f"{incident_id}.zip"
        request = {
            "schema_version": INCIDENT_SCHEMA_VERSION,
            "incident_id": incident_id,
            "created_at": created_at.isoformat(),
            "capture_not_before": (
                created_at + timedelta(seconds=INCIDENT_SETTLE_WINDOW_SECONDS)
            ).isoformat(),
            "event": str(event or "incident"),
            "error_code": str(error_code or "") or None,
            "message": safe_message,
            "task_id": str(task_id or "") or None,
            "metadata": safe_metadata,
            "traceback": safe_traceback,
            "fingerprint": fingerprint,
            "scope": scope,
            "log_record_id": str(log_record_id or "") or None,
            "evidence_path": str(output),
            "initial_occurrence": _occurrence_record(
                incident_id=incident_id,
                occurred_at=created_at,
                message=safe_message,
                metadata=safe_metadata,
                traceback_text=safe_traceback,
            ),
        }
        _atomic_write_json(_pending_directory() / f"{incident_id}.json", request)
        fingerprints[fingerprint] = {
            "incident_id": incident_id,
            "event": str(event or "incident"),
            "scope": scope,
            "active": True,
            "first_seen_at": created_at.isoformat(),
            "last_seen_at": created_at.isoformat(),
            "occurrence_count": 1,
            "evidence_path": str(output),
        }
        _write_incident_state(state)
    if start_worker:
        start_incident_worker()
        _wake_incident_worker(incident_id)
    return {
        "incident_id": incident_id,
        "evidence_path": str(output),
        "deduplicated": False,
        "pending": True,
    }


def mark_incident_recovered(
    event: str,
    *,
    metadata: dict[str, Any] | None = None,
) -> int:
    """Close active dedupe groups so a later recurrence gets a new ID."""

    expected_ids = _related_ids(metadata or {})
    changed = 0
    with _LOCK:
        state = _incident_state()
        for value in state["fingerprints"].values():
            if not isinstance(value, dict) or value.get("active") is not True:
                continue
            if str(value.get("event") or "") != str(event or ""):
                continue
            scope = value.get("scope") if isinstance(value.get("scope"), dict) else {}
            if expected_ids and any(
                str(scope.get(key) or "") != str(item)
                for key, item in expected_ids.items()
            ):
                continue
            value["active"] = False
            value["recovered_at"] = datetime.now(timezone.utc).isoformat()
            changed += 1
        if changed:
            _write_incident_state(state)
    return changed


def _wake_incident_worker(incident_id: str) -> None:
    try:
        _WORKER_WAKEUP.put_nowait(incident_id)
    except queue.Full:
        # Requests are durable on disk; the worker also scans the pending folder.
        pass


def start_incident_worker() -> None:
    global _WORKER_THREAD
    with _WORKER_LOCK:
        if _WORKER_THREAD is not None and _WORKER_THREAD.is_alive():
            return
        _WORKER_STOP.clear()
        _WORKER_THREAD = threading.Thread(
            target=_incident_worker_loop,
            name="CheJinIncidentEvidence",
            daemon=True,
        )
        _WORKER_THREAD.start()


def stop_incident_worker(*, wait: bool = True) -> None:
    global _WORKER_THREAD
    _WORKER_STOP.set()
    _wake_incident_worker("__stop__")
    thread = _WORKER_THREAD
    if wait and thread is not None and thread.is_alive():
        thread.join(timeout=5.0)
    if thread is None or not thread.is_alive():
        _WORKER_THREAD = None


def wait_for_incident(incident_id: str, timeout: float = 10.0) -> Path | None:
    deadline = time.monotonic() + max(0.0, float(timeout))
    while time.monotonic() <= deadline:
        for path in (
            incident_directory() / f"{incident_id}.zip",
            incident_directory() / f"{incident_id}.incident.json",
        ):
            if path.is_file():
                return path
        time.sleep(0.02)
    return None


def _update_latest(incident_id: str, path: Path, created_at: str) -> None:
    latest_path = incident_directory() / "latest.json"
    with _LOCK:
        current = _read_json(latest_path)
        if str(current.get("created_at") or "") > str(created_at or ""):
            return
        _atomic_write_json(
            latest_path,
            {
                "incident_id": incident_id,
                "path": str(path),
                "created_at": created_at,
            },
        )


def _minimal_incident(request: dict[str, Any], error: BaseException) -> Path | None:
    path = incident_directory() / f"{request['incident_id']}.incident.json"
    payload = {
        "schema_version": INCIDENT_SCHEMA_VERSION,
        "incident_id": request.get("incident_id"),
        "created_at": request.get("created_at"),
        "event": request.get("event"),
        "error_code": request.get("error_code"),
        "message": request.get("message"),
        "task_id": request.get("task_id"),
        "related_ids": _related_ids(request.get("metadata") or {}),
        "build": _build_identity(),
        "traceback": request.get("traceback") or "",
        "evidence_capture_error": type(error).__name__,
        "degraded": True,
    }
    try:
        _atomic_write_json(path, dict(redact_diagnostic(payload)))
    except OSError:
        return None
    return path


def _create_incident_package(request: dict[str, Any]) -> Path:
    incident_id = str(request.get("incident_id") or "")
    if not incident_id:
        raise ValueError("INCIDENT_ID_MISSING")
    output = incident_directory() / f"{incident_id}.zip"
    temporary = output.with_suffix(".zip.tmp")
    if shutil.disk_usage(incident_directory()).free < INCIDENT_MIN_FREE_BYTES:
        raise OSError("INCIDENT_DISK_SPACE_LOW")
    secrets = _known_secret_values()
    storage = _storage()
    logs = storage.read_logs(limit=MAX_LOG_ROWS)
    context = request.get("metadata") if isinstance(request.get("metadata"), dict) else {}
    evidence_paths = list(_path_candidates(context))
    for row in logs[:50]:
        evidence_paths.extend(_path_candidates(row.get("metadata") or {}))
    evidence_files = _evidence_files(evidence_paths)
    sidecar_run_id = str(context.get("sidecar_run_id") or "") or None
    state = _incident_state()
    fingerprint_state = state["fingerprints"].get(str(request.get("fingerprint") or ""))
    occurrence_count = (
        int(fingerprint_state.get("occurrence_count") or 1)
        if isinstance(fingerprint_state, dict)
        else 1
    )
    manifest = {
        "schema_version": INCIDENT_SCHEMA_VERSION,
        "incident_id": incident_id,
        "created_at": str(request.get("created_at") or ""),
        "event": str(request.get("event") or "incident"),
        "error_code": request.get("error_code"),
        "message": str(request.get("message") or ""),
        "task_id": request.get("task_id"),
        "sidecar_run_id": sidecar_run_id,
        "related_ids": _related_ids(context),
        "build": _build_identity(),
        "evidence_files": [path.name for path in evidence_files],
        "fault_fingerprint": str(request.get("fingerprint") or ""),
        "merge_window_seconds": INCIDENT_MERGE_WINDOW_SECONDS,
        "occurrence_count_at_capture": occurrence_count,
    }
    related_ids = _related_ids(context)
    reply_action_id = str(related_ids.get("reply_action_id") or "")
    c2_messages = storage.list_c2_outbox_waiting(limit=100)
    related_outbox_id = str(related_ids.get("outbox_id") or "")
    related_c2_outbox = (
        storage.load_c2_outbox_entry(related_outbox_id)
        if related_outbox_id
        else None
    )
    if related_c2_outbox and all(
        str(item.get("outbox_id") or "") != related_outbox_id
        for item in c2_messages
    ):
        c2_messages.append(related_c2_outbox)
    outbox_snapshot = {
        "c2_messages": c2_messages,
        "related_c2_outbox": related_c2_outbox,
        "sent_ack": storage.list_reply_send_ack_outbox(limit=100),
        "related_sent_ack": (
            storage.load_reply_send_ack_outbox(reply_action_id)
            if reply_action_id
            else None
        ),
    }
    temporary.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        _write_json(archive, "manifest.json", manifest, secrets)
        _write_json(
            archive,
            "occurrences/initial.json",
            request.get("initial_occurrence") or {},
            secrets,
        )
        _write_json(archive, "logs/recent_logs.json", logs, secrets)
        _write_json(
            archive,
            "state/outbox.json",
            outbox_snapshot,
            secrets,
        )
        _write_json(
            archive,
            "state/action_journals.json",
            _action_journal_snapshot(),
            secrets,
        )
        archive.writestr(
            "traceback.txt",
            _redact_text(
                str(request.get("traceback") or context.get("traceback") or ""),
                secrets,
            ),
        )
        for index, path in enumerate(evidence_files, start=1):
            name = f"evidence/{index:03d}-{path.name}"
            try:
                if path.suffix.lower() in _ALLOWED_TEXT_SUFFIXES:
                    archive.writestr(
                        name,
                        _redact_text(
                            path.read_text(encoding="utf-8", errors="replace"),
                            secrets,
                        ),
                    )
                else:
                    archive.write(path, name)
            except OSError:
                continue
    os.replace(temporary, output)
    return output


def _capture_wait_seconds(request: dict[str, Any]) -> float:
    try:
        not_before = datetime.fromisoformat(
            str(request.get("capture_not_before") or "").replace("Z", "+00:00")
        )
    except ValueError:
        return 0.0
    if not_before.tzinfo is None:
        not_before = not_before.replace(tzinfo=timezone.utc)
    return max(
        0.0,
        (
            not_before.astimezone(timezone.utc) - datetime.now(timezone.utc)
        ).total_seconds(),
    )


def _record_completed_request(request: dict[str, Any], path: Path) -> None:
    incident_id = str(request.get("incident_id") or "")
    created_at = str(request.get("created_at") or "")
    fingerprint = str(request.get("fingerprint") or "")
    with _LOCK:
        state = _incident_state()
        entry = state["fingerprints"].get(fingerprint)
        if isinstance(entry, dict):
            entry["evidence_path"] = str(path)
            entry["capture_completed_at"] = datetime.now(timezone.utc).isoformat()
            _write_incident_state(state)
    _update_latest(incident_id, path, created_at)
    record_id = str(request.get("log_record_id") or "")
    if record_id:
        try:
            _storage().update_log_incident_path(record_id, incident_id, str(path))
        except Exception:
            pass


def _process_pending_request(path: Path) -> bool:
    request = _read_json(path)
    if not request:
        path.unlink(missing_ok=True)
        return True
    if _capture_wait_seconds(request) > 0:
        return False
    output: Path | None = None
    try:
        output = _create_incident_package(request)
    except BaseException as exc:
        output = _minimal_incident(request, exc)
    if output is not None:
        _record_completed_request(request, output)
        path.unlink(missing_ok=True)
        prune_incidents()
        return True
    return False


def _append_occurrence_atomically(
    package: Path,
    occurrence: dict[str, Any],
    secrets: set[str],
) -> None:
    occurrence_id = str(occurrence.get("occurrence_id") or "")
    temporary = package.with_name(f".{package.name}.{occurrence_id}.tmp")
    temporary.unlink(missing_ok=True)
    try:
        shutil.copy2(package, temporary)
        with zipfile.ZipFile(
            temporary,
            "a",
            compression=zipfile.ZIP_DEFLATED,
        ) as archive:
            _write_json(
                archive,
                f"occurrences/{occurrence_id}.json",
                occurrence,
                secrets,
            )
        with zipfile.ZipFile(temporary, "r") as archive:
            if archive.testzip() is not None:
                raise zipfile.BadZipFile("INCIDENT_OCCURRENCE_APPEND_CRC_FAILED")
            if f"occurrences/{occurrence_id}.json" not in archive.namelist():
                raise zipfile.BadZipFile("INCIDENT_OCCURRENCE_APPEND_MISSING")
        os.replace(temporary, package)
    finally:
        temporary.unlink(missing_ok=True)


def _process_pending_occurrence(path: Path) -> bool:
    occurrence = _read_json(path)
    if not occurrence:
        path.unlink(missing_ok=True)
        return True
    incident_id = str(occurrence.get("incident_id") or "")
    occurrence_id = str(occurrence.get("occurrence_id") or "")
    if not incident_id or not occurrence_id:
        path.unlink(missing_ok=True)
        return True
    package = incident_directory() / f"{incident_id}.zip"
    minimal = incident_directory() / f"{incident_id}.incident.json"
    try:
        if package.is_file():
            secrets = _known_secret_values()
            _append_occurrence_atomically(package, occurrence, secrets)
        elif minimal.is_file():
            payload = _read_json(minimal)
            occurrences = payload.get("occurrences")
            if not isinstance(occurrences, list):
                occurrences = []
            occurrences.append(occurrence)
            payload["occurrences"] = occurrences
            _atomic_write_json(minimal, dict(redact_diagnostic(payload)))
        else:
            return False
    except (OSError, zipfile.BadZipFile):
        return False
    path.unlink(missing_ok=True)
    return True


def _incident_worker_loop() -> None:
    while not _WORKER_STOP.is_set():
        pending = sorted(_pending_directory().glob("INC-*.json"))
        if pending:
            for path in pending:
                if _WORKER_STOP.is_set():
                    return
                try:
                    completed = _process_pending_request(path)
                except BaseException:
                    completed = False
                if not completed:
                    request = _read_json(path)
                    capture_wait = _capture_wait_seconds(request)
                    retry_delay = (
                        min(max(capture_wait, 0.05), 5.0)
                        if capture_wait > 0
                        else 5.0
                    )
                    if _WORKER_STOP.wait(retry_delay):
                        return
            continue
        occurrences = sorted(_pending_occurrence_directory().glob("OCC-*.json"))
        if occurrences:
            for path in occurrences:
                if _WORKER_STOP.is_set():
                    return
                try:
                    completed = _process_pending_occurrence(path)
                except BaseException:
                    completed = False
                if not completed and _WORKER_STOP.wait(1.0):
                    return
            continue
        try:
            _WORKER_WAKEUP.get(timeout=1.0)
        except queue.Empty:
            continue


def prune_incidents() -> dict[str, int]:
    root = incident_directory()
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=INCIDENT_RETENTION_DAYS)
    candidates: list[tuple[float, Path, int]] = []
    removed = 0
    for path in list(root.glob("INC-*.zip")) + list(root.glob("INC-*.incident.json")):
        try:
            stat = path.stat()
        except OSError:
            continue
        modified = datetime.fromtimestamp(stat.st_mtime, timezone.utc)
        if modified < cutoff:
            try:
                path.unlink()
                removed += 1
            except OSError:
                pass
            continue
        candidates.append((stat.st_mtime, path, stat.st_size))
    candidates.sort(key=lambda item: item[0])
    total = sum(item[2] for item in candidates)
    while candidates and (
        len(candidates) > INCIDENT_MAX_PACKAGES
        or total > INCIDENT_MAX_TOTAL_BYTES
    ):
        _, path, size = candidates.pop(0)
        try:
            path.unlink()
        except OSError:
            continue
        total -= size
        removed += 1
    if removed:
        with _LOCK:
            state = _incident_state()
            changed = False
            for entry in state["fingerprints"].values():
                if not isinstance(entry, dict):
                    continue
                evidence_path = Path(str(entry.get("evidence_path") or ""))
                if entry.get("active") is True and not evidence_path.is_file():
                    entry["active"] = False
                    entry["retired_at"] = now.isoformat()
                    changed = True
            if changed:
                _write_incident_state(state)
    return {"removed": removed, "remaining": len(candidates), "bytes": total}


def create_incident(
    *,
    event: str,
    error_code: str | None,
    message: str,
    task_id: str | None = None,
    metadata: dict[str, Any] | None = None,
    traceback_text: str | None = None,
) -> dict[str, str]:
    """Compatibility helper for explicit synchronous callers and tests."""

    scheduled = schedule_incident(
        event=event,
        error_code=error_code,
        message=message,
        task_id=task_id,
        metadata=metadata,
        traceback_text=traceback_text,
    )
    path = wait_for_incident(str(scheduled.get("incident_id") or ""), timeout=15.0)
    return {
        "incident_id": str(scheduled.get("incident_id") or ""),
        "evidence_path": str(path or scheduled.get("evidence_path") or ""),
    }


def latest_incident() -> dict[str, str] | None:
    try:
        payload = json.loads((incident_directory() / "latest.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    path = Path(str(payload.get("path") or ""))
    if not path.is_file() or not _inside_app_dir(path):
        return None
    return {
        "incident_id": str(payload.get("incident_id") or ""),
        "evidence_path": str(path),
        "created_at": str(payload.get("created_at") or ""),
    }


def incident_by_id(incident_id: str) -> dict[str, str] | None:
    normalized = str(incident_id or "").strip()
    if not re.fullmatch(r"INC-[A-Za-z0-9-]+", normalized):
        return None
    path = incident_directory() / f"{normalized}.zip"
    if not path.is_file():
        return None
    return {
        "incident_id": normalized,
        "evidence_path": str(path),
    }


def export_latest_incident(destination: str | Path) -> Path:
    latest = latest_incident()
    if not latest:
        raise FileNotFoundError("INCIDENT_EVIDENCE_NOT_FOUND")
    source = Path(latest["evidence_path"])
    target = Path(destination)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    return target
