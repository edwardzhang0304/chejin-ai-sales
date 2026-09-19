"""Retention/export of input files referenced by existing journals and Outbox."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path

from . import storage
from .action_journal import list_action_journals, read_action_journal, action_journal_path
from .config import CONFIG
from apps.wechat_ai_customer_service.adapters import send_request_file as files
from apps.wechat_ai_customer_service.adapters.send_launch_journal import journal_references


def with_journal_references(reply_action_id, payload, previous=None):
    """Transfer ownership before freezing any outcome and deleting its journal.

    The existing Outbox owns retention for normal, read-failure and ambiguous
    recovery alike. No receipt proof, identity or result is reconstructed here.
    """
    journal = read_action_journal(action_journal_path("send", reply_action_id))
    refs = {}
    for ref in [*ack_references({"ack_payload": previous}),
                *journal_references(journal), *ack_references({"ack_payload": payload})]:
        refs[(ref["path"], ref["sha256"])] = dict(ref)
    if not refs:
        return payload
    return {**payload, "evidence": {**(payload.get("evidence") or {}), "send_request_files": list(refs.values())}}


def ack_references(row):
    payload = row.get("ack_payload")
    if payload is None:
        payload = json.loads(row.get("ack_payload_json") or "{}")
    return ((payload or {}).get("evidence") or {}).get("send_request_files") or []


def _rows():
    with storage.db_connection() as conn:
        return [dict(row) for row in conn.execute("SELECT * FROM reply_send_ack_outbox ORDER BY updated_at DESC")]


def checked_path(ref):
    path = files.normalized_path(ref["path"])
    if (path.name != ref["request_id"] + ".json" or str(path.parent) != ref["selected_root"]
            or ref["root_choice"] not in (0, 1)):
        raise ValueError("SEND_REQUEST_REFERENCE_INVALID")
    return path


def referenced_paths(ref):
    final = checked_path(ref)
    paths = [final]
    if ref.get("temporary_path"):
        temporary = files.normalized_path(ref["temporary_path"])
        if temporary != final.with_suffix(".tmp"):
            raise ValueError("SEND_REQUEST_REFERENCE_INVALID")
        paths.append(temporary)
    return paths


def retains_files(row):
    try:
        for ref in ack_references(row):
            for path in referenced_paths(ref):
                try:
                    path.stat()
                    return True
                except FileNotFoundError:
                    continue
        return False
    except (OSError, ValueError, TypeError, KeyError):
        return True


def cleanup(runner, *, now=None):
    # A conservative whole-worker barrier also covers pending stop and Flow
    # responses. No file directory scan and no interpretation as send work.
    if (runner.bridge.sidecar_active() or not runner.binding or runner.binding.run_status == "faulted"
            or runner._pending_run_status_sync is not None
            or storage.load_runtime_control().get("inflight_flow_id")):
        return 0
    now = now or datetime.now(timezone.utc)
    deleted = 0
    for row in _rows():
        if row["status"] != "confirmed":
            continue
        try:
            payload = json.loads(row.get("ack_payload_json") or "{}")
            days = (CONFIG.artifact_success_retention_days if payload.get("send_result") == "sent"
                    else CONFIG.artifact_critical_retention_days)
            if datetime.fromisoformat(row["updated_at"]) + timedelta(days=max(1, days)) > now:
                continue
            for ref in ack_references(row):
                for path in referenced_paths(ref):
                    try:
                        raw = path.read_bytes()
                    except FileNotFoundError:
                        continue
                    if hashlib.sha256(raw).hexdigest() != ref["sha256"]:
                        raise ValueError("SEND_REQUEST_DIGEST_INVALID")
                    path.unlink()
                    deleted += 1
        except (OSError, ValueError, TypeError, KeyError) as exc:
            storage.append_log("WARN", "send_request_cleanup_failed", str(exc),
                               task_id=row["task_id"], error_code=type(exc).__name__)
    return deleted


def export_files(archive, *, secrets, max_bytes, omissions):
    refs = []
    for row in _rows():
        try:
            refs.extend(ack_references(row))
        except (ValueError, TypeError, AttributeError) as exc:
            omissions.append({"task_id": row.get("task_id"), "reason": type(exc).__name__})
    for _, journal in list_action_journals(action_kinds=("send",)):
        refs.extend(journal_references(journal))
    entries, seen = [], set(archive.namelist())
    remaining = max_bytes - sum(item.file_size for item in archive.infolist()
                               if item.filename.startswith(("ipc/", "evidence/")))
    for ref in refs:
        try:
            paths = referenced_paths(ref)
            path = paths[0]
            try:
                size = path.stat().st_size
            except FileNotFoundError:
                if len(paths) != 2:
                    raise
                path = paths[1]
                size = path.stat().st_size
            temporary = path != paths[0]
            name = "ipc/" + path.name
            if name in seen:
                continue
            if size > remaining:
                raise ValueError("SEND_REQUEST_EXPORT_SIZE_LIMIT")
            if temporary:
                # Diagnostic copy only. Formal Sidecar admission still requires
                # the committed .json; never rename or replay this input.
                raw = path.read_bytes()
            else:
                _, raw = files.read_package(path)
            if len(raw) > remaining:
                raise ValueError("SEND_REQUEST_EXPORT_SIZE_LIMIT")
            digest = hashlib.sha256(raw).hexdigest()
            if digest != ref["sha256"]:
                raise ValueError("SEND_REQUEST_DIGEST_INVALID")
            # Preserve transport bytes/SHA. Never silently redact the file
            # and present that different data as the original request.
            if any(secret and secret.encode("utf-8") in raw for secret in secrets):
                raise ValueError("SEND_REQUEST_EXPORT_CONTAINS_SECRET")
        except (OSError, ValueError, TypeError, KeyError) as exc:
            omissions.append({"request_id": ref.get("request_id") if isinstance(ref, dict) else None,
                              "reason": str(exc)})
            continue
        archive.writestr(name, raw)
        seen.add(name); remaining -= len(raw)
        entries.append({"source_path": str(path), "archive_path": name, "sha256": digest,
                        "request_id": ref["request_id"], "scope": "original_send_request",
                        "package_state": "temporary_uncommitted" if temporary else "committed"})
    return entries
