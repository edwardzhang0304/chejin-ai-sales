from __future__ import annotations

import hashlib
import json
import sqlite3
import hmac
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .config import CONFIG
from .update_data_access import authenticate, canonical


PROTECTED_SNAPSHOT_SCHEMA_VERSION = 2

# These are the business fields whose values an executable-only update is
# forbidden to change.  The list is deliberately frozen and versioned.  A
# later client may add a backward-compatible SQLite column with a default
# value without making an otherwise untouched installation look corrupted;
# changing or removing any field below still fails closed.
PROTECTED_TABLE_FIELDS: dict[str, tuple[str, ...]] = {
    "binding": (
        "id",
        "worker_id",
        "worker_token",
        "client_instance_id",
        "run_status",
        "bound_at",
        "updated_at",
    ),
    "client_settings": ("key", "value", "updated_at"),
    "c2_runtime_state": ("key", "value", "updated_at"),
    "c2_message_ledger": (
        "conversation_id",
        "source_message_key",
        "origin_read_run_id",
        "dedupe_key",
        "message_type",
        "terminal_state",
        "ingest_state",
        "result_json",
        "first_seen_at",
        "updated_at",
    ),
    "c2_ingest_outbox": (
        "outbox_id",
        "conversation_id",
        "authorization_revision",
        "read_run_id",
        "payload_json",
        "status",
        "attempt_count",
        "refresh_attempt_count",
        "last_error",
        "next_attempt_at",
        "created_at",
        "updated_at",
    ),
    "c2_action_journal": (
        "flow_id",
        "conversation_id",
        "source_message_key",
        "origin_read_run_id",
        "outcome_json",
        "created_at",
        "updated_at",
    ),
    "reply_send_ack_outbox": (
        "reply_action_id",
        "task_id",
        "send_token",
        "status",
        "action_phase",
        "reply_text_hash",
        "ack_payload_json",
        "attempt_count",
        "last_error",
        "next_attempt_at",
        "created_at",
        "updated_at",
    ),
}
PROTECTED_FILE_ROOTS = (
    "transactions",
    "incidents",
    "artifacts",
    "diagnostics",
)


ROW_KEYS = {
    "binding": ("id",), "client_settings": ("key",), "c2_runtime_state": ("key",),
    "c2_message_ledger": ("conversation_id", "source_message_key"),
    "c2_ingest_outbox": ("outbox_id",), "c2_action_journal": ("flow_id", "source_message_key"),
    "reply_send_ack_outbox": ("reply_action_id",),
}


class SnapshotMismatch(RuntimeError):
    def __init__(self, code: str, differences: list[dict[str, Any]]):
        super().__init__(code)
        self.differences = differences


@contextmanager
def _read_transaction(data_dir: Path):
    path = data_dir.resolve() / "worker_client.sqlite3"
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        yield conn
    finally:
        conn.rollback()
        conn.close()


def _canonical_rows(conn: sqlite3.Connection, table: str, fields: tuple[str, ...]) -> list[dict[str, Any]]:
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
        return []
    columns = {str(row["name"]) for row in conn.execute(f'PRAGMA table_info("{table}")')}
    if any(field not in columns for field in fields):
        raise RuntimeError("UPDATE_PROTECTED_DATABASE_SCHEMA_INCOMPATIBLE")
    projection = ", ".join(f'"{field}"' for field in fields)
    return [dict(row) for row in conn.execute(f'SELECT {projection} FROM "{table}" ORDER BY {projection}')]


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _reject_evidence_alias(path: Path) -> None:
    if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
        raise RuntimeError("UPDATE_PROTECTED_FILE_SNAPSHOT_INVALID")


def protected_update_snapshot(*, data_dir: Path | None = None, digest_key: str = "") -> dict[str, Any]:
    directory = (data_dir or CONFIG.app_dir).resolve()
    tables = {}
    # One read transaction pins a single committed SQLite/WAL snapshot for all tables.
    with _read_transaction(directory) as conn:
        for table, fields in PROTECTED_TABLE_FIELDS.items():
            rows = _canonical_rows(conn, table, fields)
            tables[table] = {
                "present": conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None,
                "fields": list(fields), "row_count": len(rows),
                "sha256": authenticate(rows, digest_key),
                "rows": {
                    authenticate([row[key] for key in ROW_KEYS[table]], digest_key): {
                        field: authenticate(row[field], digest_key) for field in fields
                    } for row in rows
                },
            }
    files = {}
    for relative_root in PROTECTED_FILE_ROOTS:
        root = directory / relative_root
        _reject_evidence_alias(root)
        def fail_walk(error):
            raise error
        if root.exists():
            if not root.is_dir():
                raise RuntimeError("UPDATE_PROTECTED_FILE_SNAPSHOT_INVALID")
            for parent, dirs, names in os.walk(root, onerror=fail_walk, followlinks=False):
                for name in dirs:
                    _reject_evidence_alias(Path(parent) / name)
                for name in sorted(names):
                    path = Path(parent) / name
                    _reject_evidence_alias(path)
                    if not path.is_file():
                        raise RuntimeError("UPDATE_PROTECTED_FILE_SNAPSHOT_INVALID")
                    relative = path.relative_to(directory).as_posix()
                    files[relative] = {"size": path.stat().st_size, "sha256": _hash_file(path)}
    payload = {"snapshot_schema_version": PROTECTED_SNAPSHOT_SCHEMA_VERSION, "tables": tables, "files": files}
    return {**payload, "snapshot_sha256": authenticate(payload, digest_key)}


def assert_protected_update_snapshot(expected: dict[str, Any], *, data_dir: Path | None = None, digest_key: str = "") -> None:
    if expected.get("snapshot_schema_version") != PROTECTED_SNAPSHOT_SCHEMA_VERSION:
        raise RuntimeError("UPDATE_PROTECTED_FILE_SNAPSHOT_INVALID")
    tables = expected.get("tables")
    if not isinstance(tables, dict) or set(tables) != set(PROTECTED_TABLE_FIELDS):
        raise RuntimeError("UPDATE_PROTECTED_FILE_SNAPSHOT_INVALID")
    for name, fields in PROTECTED_TABLE_FIELDS.items():
        if not isinstance(tables[name], dict) or tables[name].get("fields") != list(fields) or not isinstance(tables[name].get("rows"), dict):
            raise RuntimeError("UPDATE_PROTECTED_FILE_SNAPSHOT_INVALID")
    actual = protected_update_snapshot(data_dir=data_dir, digest_key=digest_key)
    differences = []
    for table, metadata in tables.items():
        current = actual["tables"][table]
        if metadata != current:
            old, new = metadata["rows"], current["rows"]
            shared = old.keys() & new.keys()
            differences.append({"table": table, "added": len(new.keys()-old.keys()),
                "table_presence_changed": metadata.get("present") != current.get("present"),
                "missing": len(old.keys()-new.keys()),
                "changed": sum(old[key] != new[key] for key in shared),
                "fields": {field: sum(old[key].get(field) != new[key].get(field) for key in shared)
                           for field in PROTECTED_TABLE_FIELDS[table]
                           if any(old[key].get(field) != new[key].get(field) for key in shared)}})
    if differences:
        raise SnapshotMismatch("UPDATE_PROTECTED_DATABASE_CHANGED", differences)
    files = expected.get("files")
    if not isinstance(files, dict):
        raise RuntimeError("UPDATE_PROTECTED_FILE_SNAPSHOT_INVALID")
    if any(actual["files"].get(path) != metadata for path, metadata in files.items()):
        raise SnapshotMismatch("UPDATE_PROTECTED_FILE_CHANGED", [{"changed_files": sum(actual["files"].get(p) != m for p, m in files.items())}])


BASELINE_PLAN_SCHEMA = 2
MINIMUM_BASELINE_UPDATER_VERSION = "0.9.69"


def baseline_path(plan: dict[str, Any], plan_path: Path) -> Path:
    expected = plan_path.resolve().parent / "protected-data-baseline.json"
    if plan.get("schema_version") != BASELINE_PLAN_SCHEMA or Path(str(plan.get("data_baseline_path") or "")).resolve() != expected:
        raise RuntimeError("UPDATE_DATA_BASELINE_PLAN_INVALID")
    if "protected_data_snapshot" in plan:
        raise RuntimeError("UPDATE_DATA_BASELINE_PLAN_INVALID")
    return expected


def _binding(plan: dict[str, Any]) -> dict[str, Any]:
    return {"schema_version": BASELINE_PLAN_SCHEMA, "update_request_id": plan["update_request_id"],
            "current_version": plan["current_version"], "target_version": plan["target_version"],
            "data_dir": str(Path(plan["data_dir"]).resolve()), "snapshot_schema_version": PROTECTED_SNAPSHOT_SCHEMA_VERSION}


def capture_data_baseline(plan: dict[str, Any], plan_path: Path, token: str) -> dict[str, Any]:
    path = baseline_path(plan, plan_path)
    # A permanent create-once marker makes interrupted capture fail closed too.
    try:
        with path.with_suffix(".capture-started").open("x", encoding="utf-8") as f:
            f.write("capture-after-old-exit\n")
            f.flush()
            os.fsync(f.fileno())
    except FileExistsError as exc:
        raise RuntimeError("UPDATE_DATA_BASELINE_ALREADY_EXISTS") from exc
    if path.exists():
        raise RuntimeError("UPDATE_DATA_BASELINE_ALREADY_EXISTS")
    payload = {**_binding(plan), "captured_after_old_exit": True,
               "snapshot": protected_update_snapshot(data_dir=Path(plan["data_dir"]), digest_key=token)}
    temporary = path.with_suffix(".tmp")
    with temporary.open("x", encoding="utf-8") as f:
        json.dump({"payload": payload, "auth": authenticate(payload, token)}, f, ensure_ascii=False)
        f.flush(); os.fsync(f.fileno())
    os.replace(temporary, path)
    return payload


def load_data_baseline(plan: dict[str, Any], plan_path: Path, token: str) -> dict[str, Any]:
    try:
        record = json.loads(baseline_path(plan, plan_path).read_text(encoding="utf-8"))
        payload = record["payload"]
        if not hmac.compare_digest(record["auth"], authenticate(payload, token)):
            raise ValueError()
        if any(payload.get(k) != v for k, v in _binding(plan).items()) or payload.get("captured_after_old_exit") is not True:
            raise ValueError()
        if payload["snapshot"]["snapshot_schema_version"] != PROTECTED_SNAPSHOT_SCHEMA_VERSION:
            raise ValueError()
        return payload
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise RuntimeError("UPDATE_DATA_BASELINE_INVALID") from exc
