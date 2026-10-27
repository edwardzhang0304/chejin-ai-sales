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


def _correction_columns_snapshot(conn, digest_key):
    """Optional extension: old/default-only databases keep their v2 shape."""
    columns = {row["name"] for row in conn.execute('PRAGMA table_info("c2_ingest_outbox")')}
    if not {"operation", "correction_image"}.issubset(columns):
        return {}
    result = {}
    for row in conn.execute("""SELECT outbox_id,operation,correction_image FROM c2_ingest_outbox
            WHERE operation != 'ingest' OR correction_image IS NOT NULL"""):
        blob = row["correction_image"]
        result[authenticate(row["outbox_id"], digest_key)] = authenticate({
            "operation": row["operation"],
            "image_sha256": hashlib.sha256(blob).hexdigest() if blob is not None else None,
            "image_bytes": len(blob) if blob is not None else None,
        }, digest_key)
    return result


def _reject_evidence_alias(path: Path) -> None:
    if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
        raise RuntimeError("UPDATE_PROTECTED_FILE_SNAPSHOT_INVALID")


def _hook_inbox_snapshot(conn: sqlite3.Connection, digest_key: str) -> dict[str, Any] | None:
    """Optional extension leaves OCR snapshots unchanged when no Hook table exists.

    Hash BLOBs locally; exported update evidence never contains callback bodies.
    The AUTOINCREMENT high-water mark must survive even after future cleanup.
    """
    from .hook_inbox import INBOX_FIELDS

    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='hook_raw_inbox'").fetchone() is None:
        return None
    columns = tuple(row["name"] for row in conn.execute("PRAGMA table_info(hook_raw_inbox)"))
    if columns != INBOX_FIELDS:
        raise RuntimeError("UPDATE_PROTECTED_DATABASE_SCHEMA_INVALID")
    rows = {}
    for row in conn.execute("SELECT * FROM hook_raw_inbox ORDER BY receive_seq"):
        raw = row["raw_body"]
        if not isinstance(raw, bytes):
            raise RuntimeError("UPDATE_PROTECTED_DATABASE_SCHEMA_INVALID")
        rows[authenticate(row["receive_seq"], digest_key)] = {
            field: authenticate(hashlib.sha256(raw).hexdigest() if field == "raw_body" else row[field], digest_key)
            for field in INBOX_FIELDS
        }
    sequence = conn.execute("SELECT seq FROM sqlite_sequence WHERE name='hook_raw_inbox'").fetchone()
    return {"fields": list(INBOX_FIELDS), "row_count": len(rows), "rows": rows,
            "receive_seq_high_water": authenticate(sequence["seq"] if sequence else 0, digest_key)}


def _hook_projection_snapshot(conn: sqlite3.Connection, digest_key: str) -> dict[str, Any] | None:
    from .hook_projection import PROJECTION_FIELDS

    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='hook_observation_projection'").fetchone() is None:
        return None
    columns = tuple(row["name"] for row in conn.execute("PRAGMA table_info(hook_observation_projection)"))
    if columns != PROJECTION_FIELDS:
        raise RuntimeError("UPDATE_PROTECTED_DATABASE_SCHEMA_INVALID")
    rows = {authenticate(row["receive_seq"], digest_key): {
        field: authenticate(row[field], digest_key) for field in PROJECTION_FIELDS
    } for row in conn.execute("SELECT * FROM hook_observation_projection ORDER BY receive_seq")}
    return {"fields": list(PROJECTION_FIELDS), "row_count": len(rows), "rows": rows}


def _hook_outbox_snapshot(conn: sqlite3.Connection, digest_key: str) -> dict[str, Any] | None:
    from .hook_outbox import OUTBOX_FIELDS
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='hook_delivery_outbox'").fetchone() is None:
        return None
    fields = tuple(row['name'] for row in conn.execute('PRAGMA table_info(hook_delivery_outbox)'))
    if fields != OUTBOX_FIELDS:
        raise RuntimeError('UPDATE_PROTECTED_DATABASE_SCHEMA_INVALID')
    rows = {authenticate(row['event_uuid'], digest_key): {
        field: authenticate(row[field], digest_key) for field in OUTBOX_FIELDS
    } for row in conn.execute('SELECT * FROM hook_delivery_outbox ORDER BY event_uuid')}
    return {'fields': list(OUTBOX_FIELDS), 'row_count': len(rows), 'rows': rows}


def _hook_retired_snapshot(conn,digest_key):
    from .hook_retention import FIELDS
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name='hook_retired_observations'").fetchone() is None:return None
    if tuple(row['name'] for row in conn.execute('PRAGMA table_info(hook_retired_observations)'))!=FIELDS:
        raise RuntimeError('UPDATE_PROTECTED_DATABASE_SCHEMA_INVALID')
    rows=[dict(row) for row in conn.execute('SELECT * FROM hook_retired_observations ORDER BY receive_seq')]
    deliveries=[dict(row) for row in conn.execute('SELECT * FROM hook_retired_deliveries ORDER BY event_uuid')] if conn.execute("SELECT 1 FROM sqlite_master WHERE name='hook_retired_deliveries'").fetchone() else []
    return authenticate({'observations':rows,'deliveries':deliveries},digest_key)


def _hook_active_calls_snapshot(conn,digest_key):
    from .hook_active_calls import FIELDS
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name='hook_active_calls'").fetchone() is None:return None
    if tuple(row['name'] for row in conn.execute('PRAGMA table_info(hook_active_calls)'))!=FIELDS:
        raise RuntimeError('UPDATE_PROTECTED_DATABASE_SCHEMA_INVALID')
    return authenticate([dict(row) for row in conn.execute('SELECT * FROM hook_active_calls ORDER BY process_generation,action_id')],digest_key)


def _hook_recovery_snapshot(conn,digest_key):
    from .hook_read_retry import TABLE_FIELDS, validate_schema
    result={}
    for table in TABLE_FIELDS:
        if not conn.execute('SELECT 1 FROM sqlite_master WHERE name=?',(table,)).fetchone():
            continue
        validate_schema(conn,table)
        result[table]=authenticate([dict(row) for row in conn.execute(f'SELECT * FROM {table} ORDER BY rowid')],digest_key)
    return result


def _hook_voice_snapshot(conn: sqlite3.Connection, digest_key: str) -> dict[str, Any] | None:
    from .hook_voice import VOICE_FIELDS
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name='hook_voice_attempts'").fetchone() is None:
        return None
    if tuple(row["name"] for row in conn.execute("PRAGMA table_info(hook_voice_attempts)")) != VOICE_FIELDS:
        raise RuntimeError("HOOK_VOICE_SCHEMA_UNSUPPORTED")
    rows = [dict(row) for row in conn.execute("SELECT * FROM hook_voice_attempts ORDER BY self_wxid,account_epoch,native_msg_id")]
    return {"rows": len(rows), "digest": authenticate(rows, digest_key)}


def _hook_history_snapshot(conn: sqlite3.Connection, digest_key: str) -> dict[str, Any] | None:
    from .hook_history_store import HISTORY_PAGE_FIELDS
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='hook_history_pages'").fetchone() is None:
        return None
    if tuple(row['name'] for row in conn.execute('PRAGMA table_info(hook_history_pages)')) != HISTORY_PAGE_FIELDS:
        raise RuntimeError('UPDATE_PROTECTED_DATABASE_SCHEMA_INVALID')
    rows = {}
    for row in conn.execute('SELECT * FROM hook_history_pages ORDER BY history_seq'):
        raw = row['raw_body']
        if not isinstance(raw, bytes):
            raise RuntimeError('UPDATE_PROTECTED_DATABASE_SCHEMA_INVALID')
        rows[authenticate(row['history_seq'], digest_key)] = {
            field: authenticate(hashlib.sha256(raw).hexdigest() if field == 'raw_body' else row[field], digest_key)
            for field in HISTORY_PAGE_FIELDS}
    sequence = conn.execute("SELECT seq FROM sqlite_sequence WHERE name='hook_history_pages'").fetchone()
    return {'fields': list(HISTORY_PAGE_FIELDS), 'row_count': len(rows), 'rows': rows,
        'history_seq_high_water': authenticate(sequence['seq'] if sequence else 0, digest_key)}


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
        corrections = _correction_columns_snapshot(conn, digest_key)
        hook_inbox = _hook_inbox_snapshot(conn, digest_key)
        hook_projection = _hook_projection_snapshot(conn, digest_key)
        hook_outbox = _hook_outbox_snapshot(conn, digest_key)
        hook_history = _hook_history_snapshot(conn, digest_key)
        hook_voice = _hook_voice_snapshot(conn, digest_key)
        hook_active_calls = _hook_active_calls_snapshot(conn,digest_key)
        hook_recovery = _hook_recovery_snapshot(conn,digest_key)
        hook_retired = _hook_retired_snapshot(conn,digest_key)
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
    if corrections:
        payload["correction_columns_v1"] = corrections
    if hook_inbox is not None:
        payload["hook_inbox_v1"] = hook_inbox
    if hook_projection is not None:
        payload["hook_projection_v1"] = hook_projection
    if hook_outbox is not None:
        payload['hook_outbox_v1'] = hook_outbox
    if hook_history is not None:
        payload['hook_history_v1'] = hook_history
    if hook_voice is not None:
        payload['hook_voice_v1'] = hook_voice
    if hook_retired is not None:
        payload['hook_retired_v1']=hook_retired
    if hook_active_calls is not None:
        payload['hook_active_calls_v1']=hook_active_calls
    if hook_recovery:
        payload['hook_recovery_v1']=hook_recovery
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
    if expected.get('hook_recovery_v1',{})!=actual.get('hook_recovery_v1',{}):
        differences.append({'table':'hook_recovery','recovery_evidence_changed':True})
    if expected.get('hook_retired_v1')!=actual.get('hook_retired_v1'):
        differences.append({'table':'hook_retired_observations','retired_observations_changed':True})
    if expected.get('hook_active_calls_v1')!=actual.get('hook_active_calls_v1'):
        differences.append({'table':'hook_active_calls','active_calls_changed':True})
    if expected.get("hook_voice_v1") != actual.get("hook_voice_v1"):
        differences.append({"table": "hook_voice_attempts", "hook_voice_changed": True})
    if expected.get("hook_inbox_v1") != actual.get("hook_inbox_v1"):
        differences.append({"table": "hook_raw_inbox", "hook_inbox_changed": True})
    if expected.get("hook_projection_v1") != actual.get("hook_projection_v1"):
        differences.append({"table": "hook_observation_projection", "hook_projection_changed": True})
    if expected.get('hook_history_v1') != actual.get('hook_history_v1'):
        differences.append({'table': 'hook_history_pages', 'hook_history_changed': True})
    if expected.get('hook_outbox_v1') != actual.get('hook_outbox_v1'):
        differences.append({'table': 'hook_delivery_outbox', 'hook_outbox_changed': True})
    if expected.get("correction_columns_v1", {}) != actual.get("correction_columns_v1", {}):
        differences.append({"table": "c2_ingest_outbox", "correction_columns_changed": True})
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
    handoff = (plan.get('safe_boundary') or {}).get('pending_read_handoff')
    if handoff is not None:
        from .pending_read_recovery import inspect_pending_read
        if inspect_pending_read(Path(plan['data_dir'])) != handoff:
            raise RuntimeError('UPDATE_PENDING_READ_HANDOFF_CHANGED')
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
    if handoff is not None:
        payload['pending_read_handoff'] = handoff
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
