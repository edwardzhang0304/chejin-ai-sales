from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .config import CONFIG
from .storage import db_connection


PROTECTED_SNAPSHOT_SCHEMA_VERSION = 1

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


def _canonical_rows(table: str, fields: tuple[str, ...]) -> list[dict[str, Any]]:
    with db_connection() as conn:
        names = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if table not in names:
            return []
        columns = {
            str(row[1])
            for row in conn.execute(f'PRAGMA table_info("{table}")').fetchall()
        }
        missing = [field for field in fields if field not in columns]
        if missing:
            raise RuntimeError("UPDATE_PROTECTED_DATABASE_SCHEMA_INCOMPATIBLE")
        projection = ", ".join(f'"{field}"' for field in fields)
        rows = conn.execute(
            f'SELECT {projection} FROM "{table}" ORDER BY {projection}'
        ).fetchall()
        return [
            {field: row[field] for field in fields}
            for row in rows
        ]


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def protected_update_snapshot() -> dict[str, Any]:
    """Hash business state that a program-only update is forbidden to change."""

    tables: dict[str, dict[str, Any]] = {}
    for table, fields in PROTECTED_TABLE_FIELDS.items():
        rows = _canonical_rows(table, fields)
        encoded = json.dumps(
            rows,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        tables[table] = {
            "fields": list(fields),
            "row_count": len(rows),
            "sha256": hashlib.sha256(encoded).hexdigest(),
        }

    files: dict[str, dict[str, Any]] = {}
    app_root = CONFIG.app_dir.resolve(strict=False)
    for relative_root in PROTECTED_FILE_ROOTS:
        root = app_root / relative_root
        if not root.exists():
            continue
        for path in sorted(item for item in root.rglob("*") if item.is_file()):
            relative = path.resolve(strict=False).relative_to(app_root).as_posix()
            files[relative] = {
                "size": path.stat().st_size,
                "sha256": _hash_file(path),
            }

    payload = {
        "snapshot_schema_version": PROTECTED_SNAPSHOT_SCHEMA_VERSION,
        "tables": tables,
        "files": files,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return {
        **payload,
        "snapshot_sha256": hashlib.sha256(encoded).hexdigest(),
    }


def assert_protected_update_snapshot(expected: dict[str, Any]) -> None:
    if (
        int(expected.get("snapshot_schema_version") or 0)
        != PROTECTED_SNAPSHOT_SCHEMA_VERSION
    ):
        raise RuntimeError("UPDATE_PROTECTED_FILE_SNAPSHOT_INVALID")
    expected_tables = expected.get("tables")
    if not isinstance(expected_tables, dict):
        raise RuntimeError("UPDATE_PROTECTED_FILE_SNAPSHOT_INVALID")
    if set(expected_tables) != set(PROTECTED_TABLE_FIELDS):
        raise RuntimeError("UPDATE_PROTECTED_FILE_SNAPSHOT_INVALID")

    actual_tables: dict[str, dict[str, Any]] = {}
    for table, frozen_fields in PROTECTED_TABLE_FIELDS.items():
        metadata = expected_tables.get(table)
        if not isinstance(metadata, dict):
            raise RuntimeError("UPDATE_PROTECTED_FILE_SNAPSHOT_INVALID")
        expected_fields = metadata.get("fields")
        if expected_fields != list(frozen_fields):
            raise RuntimeError("UPDATE_PROTECTED_FILE_SNAPSHOT_INVALID")
        rows = _canonical_rows(table, frozen_fields)
        encoded = json.dumps(
            rows,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        actual_tables[table] = {
            "fields": list(frozen_fields),
            "row_count": len(rows),
            "sha256": hashlib.sha256(encoded).hexdigest(),
        }
    if actual_tables != expected_tables:
        raise RuntimeError("UPDATE_PROTECTED_DATABASE_CHANGED")
    expected_files = expected.get("files")
    actual_files = protected_update_snapshot().get("files")
    if not isinstance(expected_files, dict) or not isinstance(actual_files, dict):
        raise RuntimeError("UPDATE_PROTECTED_FILE_SNAPSHOT_INVALID")
    if any(actual_files.get(path) != metadata for path, metadata in expected_files.items()):
        raise RuntimeError("UPDATE_PROTECTED_FILE_CHANGED")
