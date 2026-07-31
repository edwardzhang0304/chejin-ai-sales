from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
import re
from typing import Any

from .config import CONFIG
from .c2_contract import c2_contract_v3
from .models import Binding, utc_now_iso


APP_DIR = CONFIG.app_dir
DB_FILE = APP_DIR / "worker_client.sqlite3"
MAX_LOGS = 1000
RETENTION_DAYS = 30
MAX_C2_LEDGER_ROWS_PER_CONVERSATION = 2000
DEFAULT_ACCEPT_SCHEDULE = {"enabled": False, "start": "09:00", "end": "21:00"}
TIME_RE = re.compile(r"^\d{2}:\d{2}$")


def _c2_outbox_states() -> set[str]:
    state_machine = (
        c2_contract_v3().get("outbox_recovery_contract") or {}
    ).get("state_machine")
    return {
        str(value)
        for value in (
            state_machine.get("states")
            if isinstance(state_machine, dict)
            else []
        )
    }


def _c2_outbox_terminal_states() -> set[str]:
    state_machine = (
        c2_contract_v3().get("outbox_recovery_contract") or {}
    ).get("state_machine")
    properties = (
        state_machine.get("state_properties")
        if isinstance(state_machine, dict)
        else {}
    )
    return {
        str(state)
        for state, definition in (
            properties.items()
            if isinstance(properties, dict)
            else []
        )
        if isinstance(definition, dict)
        and definition.get("automatic_retry") is False
    }


def _outbox_backoff_seconds(attempt_count: int) -> int:
    contract = c2_contract_v3().get("outbox_recovery_contract") or {}
    machine = contract.get("state_machine") or {}
    schedule = [
        max(1, int(value))
        for value in (machine.get("retry_backoff_seconds") or [])
    ]
    if not schedule:
        schedule = [1, 2, 5, 10, 30, 60]
    index = min(max(0, int(attempt_count) - 1), len(schedule) - 1)
    maximum = max(
        schedule[-1],
        int(machine.get("max_retry_interval_seconds") or schedule[-1]),
    )
    return min(schedule[index], maximum)


def _next_attempt_iso(attempt_count: int) -> str:
    return (
        datetime.now(timezone.utc)
        + timedelta(seconds=_outbox_backoff_seconds(attempt_count))
    ).isoformat()


def ensure_app_dir() -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)


def connect() -> sqlite3.Connection:
    ensure_app_dir()
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    init_db(conn)
    return conn


@contextmanager
def db_connection():
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


def init_db(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS binding (
          id INTEGER PRIMARY KEY CHECK (id = 1),
          worker_id TEXT NOT NULL,
          worker_token TEXT NOT NULL,
          client_instance_id TEXT NOT NULL,
          run_status TEXT NOT NULL,
          bound_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS local_logs (
          id TEXT PRIMARY KEY,
          created_at TEXT NOT NULL,
          level TEXT NOT NULL,
          event TEXT NOT NULL,
          task_id TEXT,
          error_code TEXT,
          message TEXT NOT NULL,
          metadata TEXT NOT NULL DEFAULT '{}'
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS client_settings (
          key TEXT PRIMARY KEY,
          value TEXT NOT NULL,
          updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS c2_runtime_state (
          key TEXT PRIMARY KEY,
          value TEXT NOT NULL,
          updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS c2_message_ledger (
          conversation_id TEXT NOT NULL,
          source_message_key TEXT NOT NULL,
          dedupe_key TEXT,
          message_type TEXT NOT NULL,
          terminal_state TEXT NOT NULL,
          ingest_state TEXT NOT NULL,
          result_json TEXT NOT NULL DEFAULT '{}',
          first_seen_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          PRIMARY KEY (conversation_id, source_message_key)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS c2_ingest_outbox (
          outbox_id TEXT PRIMARY KEY,
          conversation_id TEXT NOT NULL,
          authorization_revision TEXT NOT NULL,
          read_run_id TEXT NOT NULL,
          payload_json TEXT NOT NULL,
          status TEXT NOT NULL,
          attempt_count INTEGER NOT NULL DEFAULT 0,
          refresh_attempt_count INTEGER NOT NULL DEFAULT 0,
          last_error TEXT,
          next_attempt_at TEXT,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS c2_action_journal (
          flow_id TEXT NOT NULL,
          conversation_id TEXT NOT NULL,
          source_message_key TEXT NOT NULL,
          outcome_json TEXT NOT NULL,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          PRIMARY KEY (flow_id, source_message_key)
        )
        """
    )
    outbox_columns = {
        str(row["name"])
        for row in conn.execute("PRAGMA table_info(c2_ingest_outbox)").fetchall()
    }
    if "refresh_attempt_count" not in outbox_columns:
        conn.execute(
            "ALTER TABLE c2_ingest_outbox "
            "ADD COLUMN refresh_attempt_count INTEGER NOT NULL DEFAULT 0"
        )
    if "next_attempt_at" not in outbox_columns:
        conn.execute(
            "ALTER TABLE c2_ingest_outbox "
            "ADD COLUMN next_attempt_at TEXT"
        )
    conn.execute(
        "UPDATE c2_ingest_outbox "
        "SET status = 'capability_paused', next_attempt_at = COALESCE(next_attempt_at, ?) "
        "WHERE status IN ('quarantined', 'abandoned', 'payload_terminated')",
        (utc_now_iso(),),
    )
    conn.execute(
        "UPDATE c2_message_ledger SET ingest_state = 'waiting' "
        "WHERE ingest_state = 'quarantined'"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS reply_send_ack_outbox (
          reply_action_id TEXT PRIMARY KEY,
          task_id TEXT NOT NULL,
          send_token TEXT NOT NULL,
          status TEXT NOT NULL,
          action_phase TEXT NOT NULL DEFAULT 'not_attempted',
          reply_text_hash TEXT,
          ack_payload_json TEXT,
          attempt_count INTEGER NOT NULL DEFAULT 0,
          last_error TEXT,
          next_attempt_at TEXT,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        )
        """
    )
    send_ack_columns = {
        str(row["name"])
        for row in conn.execute(
            "PRAGMA table_info(reply_send_ack_outbox)"
        ).fetchall()
    }
    if "action_phase" not in send_ack_columns:
        conn.execute(
            "ALTER TABLE reply_send_ack_outbox "
            "ADD COLUMN action_phase TEXT NOT NULL DEFAULT 'not_attempted'"
        )
    if "reply_text_hash" not in send_ack_columns:
        conn.execute(
            "ALTER TABLE reply_send_ack_outbox "
            "ADD COLUMN reply_text_hash TEXT"
        )
    if "next_attempt_at" not in send_ack_columns:
        conn.execute(
            "ALTER TABLE reply_send_ack_outbox "
            "ADD COLUMN next_attempt_at TEXT"
        )
    conn.execute(
        """
        UPDATE reply_send_ack_outbox
        SET status = CASE
              WHEN COALESCE(ack_payload_json, '') != '' THEN 'waiting'
              ELSE 'intent'
            END,
            next_attempt_at = COALESCE(next_attempt_at, ?)
        WHERE status = 'abandoned'
        """,
        (utc_now_iso(),),
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_local_logs_created_at ON local_logs(created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_c2_ledger_updated ON c2_message_ledger(conversation_id, updated_at DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_c2_outbox_waiting ON c2_ingest_outbox(status, created_at)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_c2_action_journal_conversation "
        "ON c2_action_journal(conversation_id, created_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_reply_send_ack_outbox_waiting "
        "ON reply_send_ack_outbox(status, created_at)"
    )
    conn.commit()


def new_client_instance_id() -> str:
    return f"client_{uuid.uuid4()}"


def load_binding() -> Binding | None:
    with db_connection() as conn:
        row = conn.execute("SELECT worker_id, worker_token, client_instance_id, run_status, bound_at FROM binding WHERE id = 1").fetchone()
    if not row:
        return None
    return Binding(
        worker_id=row["worker_id"],
        worker_token=row["worker_token"],
        client_instance_id=row["client_instance_id"],
        run_status=row["run_status"],
        bound_at=row["bound_at"],
    )


def save_binding(binding: Binding) -> None:
    now = utc_now_iso()
    with db_connection() as conn:
        conn.execute(
            """
            INSERT INTO binding (id, worker_id, worker_token, client_instance_id, run_status, bound_at, updated_at)
            VALUES (1, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
              worker_id = excluded.worker_id,
              worker_token = excluded.worker_token,
              client_instance_id = excluded.client_instance_id,
              run_status = excluded.run_status,
              bound_at = excluded.bound_at,
              updated_at = excluded.updated_at
            """,
            (binding.worker_id, binding.worker_token, binding.client_instance_id, binding.run_status, binding.bound_at, now),
        )
        conn.commit()


def clear_binding() -> None:
    with db_connection() as conn:
        conn.execute("DELETE FROM binding WHERE id = 1")
        conn.commit()


def _normalize_time(value: str, fallback: str) -> str:
    value = str(value or "").strip()
    if not TIME_RE.match(value):
        return fallback
    hour_text, minute_text = value.split(":", 1)
    hour = int(hour_text)
    minute = int(minute_text)
    if hour > 23 or minute > 59:
        return fallback
    return f"{hour:02d}:{minute:02d}"


def _normalize_schedule(payload: dict[str, Any] | None) -> dict[str, Any]:
    payload = payload or {}
    return {
        "enabled": bool(payload.get("enabled", DEFAULT_ACCEPT_SCHEDULE["enabled"])),
        "start": _normalize_time(str(payload.get("start") or ""), DEFAULT_ACCEPT_SCHEDULE["start"]),
        "end": _normalize_time(str(payload.get("end") or ""), DEFAULT_ACCEPT_SCHEDULE["end"]),
    }


def load_accept_schedule() -> dict[str, Any]:
    with db_connection() as conn:
        row = conn.execute("SELECT value FROM client_settings WHERE key = 'accept_schedule'").fetchone()
    if not row:
        return dict(DEFAULT_ACCEPT_SCHEDULE)
    try:
        payload = json.loads(row["value"])
    except json.JSONDecodeError:
        return dict(DEFAULT_ACCEPT_SCHEDULE)
    return _normalize_schedule(payload if isinstance(payload, dict) else None)


def save_accept_schedule(*, enabled: bool, start: str, end: str) -> dict[str, Any]:
    schedule = _normalize_schedule({"enabled": enabled, "start": start, "end": end})
    with db_connection() as conn:
        conn.execute(
            """
            INSERT INTO client_settings (key, value, updated_at)
            VALUES ('accept_schedule', ?, ?)
            ON CONFLICT(key) DO UPDATE SET
              value = excluded.value,
              updated_at = excluded.updated_at
            """,
            (json.dumps(schedule, ensure_ascii=False), utc_now_iso()),
        )
        conn.commit()
    return schedule


def save_c2_state(key: str, value: dict[str, Any]) -> None:
    clean_key = str(key or "").strip()
    if not clean_key:
        return
    with db_connection() as conn:
        conn.execute(
            """
            INSERT INTO c2_runtime_state (key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
              value = excluded.value,
              updated_at = excluded.updated_at
            """,
            (clean_key, json.dumps(value, ensure_ascii=False), utc_now_iso()),
        )
        conn.commit()


def load_c2_state(key: str) -> dict[str, Any]:
    clean_key = str(key or "").strip()
    if not clean_key:
        return {}
    with db_connection() as conn:
        row = conn.execute("SELECT value FROM c2_runtime_state WHERE key = ?", (clean_key,)).fetchone()
    if not row:
        return {}
    try:
        payload = json.loads(row["value"])
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def clear_c2_state(key: str) -> None:
    clean_key = str(key or "").strip()
    if not clean_key:
        return
    with db_connection() as conn:
        conn.execute("DELETE FROM c2_runtime_state WHERE key = ?", (clean_key,))
        conn.commit()


_OUTBOX_FORBIDDEN_KEYS = set(
    (c2_contract_v3().get("image_persistence_policy") or {}).get("forbidden_field_names") or []
)

_OUTBOX_FORBIDDEN_KEY_PREFIXES = (
    "provider_response",
    "raw_provider_response",
    "retry_response",
    "initial_response",
)


def _assert_outbox_text_only(value: Any, *, path: str = "payload") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = str(key).strip().lower()
            if normalized in _OUTBOX_FORBIDDEN_KEYS or normalized.startswith(_OUTBOX_FORBIDDEN_KEY_PREFIXES):
                raise ValueError(f"C2_OUTBOX_FORBIDDEN_IMAGE_FIELD:{path}.{key}")
            _assert_outbox_text_only(child, path=f"{path}.{key}")
        return
    if isinstance(value, list):
        for index, child in enumerate(value):
            _assert_outbox_text_only(child, path=f"{path}[{index}]")
        return
    if isinstance(value, str):
        compact = value.strip().lower()
        image_context = any(token in path.lower() for token in ("image", "vision", "thumbnail", "asset"))
        if compact.startswith("data:image/") or (image_context and (compact.startswith("file://") or re.match(r"^[a-z]:[\\/]", compact))):
            raise ValueError(f"C2_OUTBOX_FORBIDDEN_IMAGE_VALUE:{path}")
        return
    if value is None or isinstance(value, (bool, int, float)):
        return
    raise ValueError(f"C2_OUTBOX_NON_JSON_VALUE:{path}")


def load_c2_ledger_entry(conversation_id: str, source_message_key: str) -> dict[str, Any] | None:
    with db_connection() as conn:
        row = conn.execute(
            """
            SELECT conversation_id, source_message_key, dedupe_key, message_type,
                   terminal_state, ingest_state, result_json, first_seen_at, updated_at
            FROM c2_message_ledger
            WHERE conversation_id = ? AND source_message_key = ?
            """,
            (str(conversation_id), str(source_message_key)),
        ).fetchone()
    if not row:
        return None
    item = dict(row)
    try:
        item["result"] = json.loads(item.pop("result_json") or "{}")
    except json.JSONDecodeError:
        item["result"] = {}
    return item


def list_c2_ledger_entries(
    conversation_id: str,
    *,
    message_type: str | None = None,
    ingest_state: str | None = None,
) -> list[dict[str, Any]]:
    clauses = ["conversation_id = ?"]
    values: list[Any] = [str(conversation_id)]
    if message_type:
        clauses.append("message_type = ?")
        values.append(str(message_type))
    if ingest_state:
        clauses.append("ingest_state = ?")
        values.append(str(ingest_state))
    with db_connection() as conn:
        rows = conn.execute(
            f"""
            SELECT conversation_id, source_message_key, dedupe_key, message_type,
                   terminal_state, ingest_state, result_json, first_seen_at,
                   updated_at
            FROM c2_message_ledger
            WHERE {' AND '.join(clauses)}
            ORDER BY first_seen_at ASC, source_message_key ASC
            """,
            values,
        ).fetchall()
    result: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        try:
            item["result"] = json.loads(
                item.pop("result_json") or "{}"
            )
        except json.JSONDecodeError:
            item["result"] = {}
        result.append(item)
    return result


def save_c2_ledger_terminal(
    *,
    conversation_id: str,
    source_message_key: str,
    dedupe_key: str | None,
    message_type: str,
    terminal_state: str,
    ingest_state: str,
    result: dict[str, Any] | None = None,
) -> None:
    if terminal_state not in {"completed", "failed", "ignored"}:
        raise ValueError("C2_LEDGER_TERMINAL_STATE_INVALID")
    if ingest_state not in {
        "not_required",
        "waiting",
        "confirmed",
    }:
        raise ValueError("C2_LEDGER_INGEST_STATE_INVALID")
    _assert_outbox_text_only(result or {}, path="ledger_result")
    now = utc_now_iso()
    with db_connection() as conn:
        conn.execute(
            """
            INSERT INTO c2_message_ledger (
              conversation_id, source_message_key, dedupe_key, message_type,
              terminal_state, ingest_state, result_json, first_seen_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(conversation_id, source_message_key) DO UPDATE SET
              dedupe_key = COALESCE(excluded.dedupe_key, c2_message_ledger.dedupe_key),
              message_type = excluded.message_type,
              terminal_state = excluded.terminal_state,
              ingest_state = excluded.ingest_state,
              result_json = excluded.result_json,
              updated_at = excluded.updated_at
            """,
            (
                str(conversation_id),
                str(source_message_key),
                str(dedupe_key or "") or None,
                str(message_type),
                terminal_state,
                ingest_state,
                json.dumps(result or {}, ensure_ascii=False, separators=(",", ":")),
                now,
                now,
            ),
        )
        rows = conn.execute(
            """
            SELECT source_message_key FROM c2_message_ledger
            WHERE conversation_id = ? AND ingest_state = 'confirmed'
            ORDER BY updated_at DESC
            LIMIT -1 OFFSET ?
            """,
            (str(conversation_id), MAX_C2_LEDGER_ROWS_PER_CONVERSATION),
        ).fetchall()
        if rows:
            conn.executemany(
                "DELETE FROM c2_message_ledger WHERE conversation_id = ? AND source_message_key = ?",
                [(str(conversation_id), str(row["source_message_key"])) for row in rows],
            )
        conn.commit()


def mark_c2_ledger_ingested(conversation_id: str, source_message_keys: list[str]) -> None:
    keys = [str(value).strip() for value in source_message_keys if str(value).strip()]
    if not keys:
        return
    now = utc_now_iso()
    with db_connection() as conn:
        conn.executemany(
            """
            UPDATE c2_message_ledger
            SET ingest_state = 'confirmed', updated_at = ?
            WHERE conversation_id = ? AND source_message_key = ?
            """,
            [(now, str(conversation_id), key) for key in keys],
        )
        conn.commit()


def mark_c2_ledger_rejected(conversation_id: str, source_message_keys: list[str]) -> None:
    keys = [str(value).strip() for value in source_message_keys if str(value).strip()]
    if not keys:
        return
    now = utc_now_iso()
    with db_connection() as conn:
        conn.executemany(
            """
            UPDATE c2_message_ledger
            SET terminal_state = 'failed', ingest_state = 'not_required', updated_at = ?
            WHERE conversation_id = ? AND source_message_key = ?
            """,
            [(now, str(conversation_id), key) for key in keys],
        )
        conn.commit()


def checkpoint_c2_action_outcomes(
    *,
    flow_id: str,
    conversation_id: str,
    outcomes: list[dict[str, Any]],
) -> None:
    """Persist irreversible action facts before the flow can exit or crash."""

    normalized_flow_id = str(flow_id or "").strip()
    normalized_conversation_id = str(conversation_id or "").strip()
    if not normalized_flow_id or not normalized_conversation_id:
        raise ValueError("C2_ACTION_JOURNAL_IDENTITY_MISSING")
    now = utc_now_iso()
    rows: list[tuple[str, str, str, str, str, str]] = []
    for outcome in outcomes:
        if not isinstance(outcome, dict):
            continue
        source_message_key = str(
            outcome.get("source_message_key") or ""
        ).strip()
        result = str(outcome.get("result") or "").strip().lower()
        if not source_message_key or result not in {"completed", "failed"}:
            continue
        _assert_outbox_text_only(
            outcome,
            path="c2_action_journal.outcome",
        )
        rows.append(
            (
                normalized_flow_id,
                normalized_conversation_id,
                source_message_key,
                json.dumps(
                    outcome,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                now,
                now,
            )
        )
    if not rows:
        return
    with db_connection() as conn:
        conn.executemany(
            """
            INSERT INTO c2_action_journal (
              flow_id, conversation_id, source_message_key,
              outcome_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(flow_id, source_message_key) DO UPDATE SET
              outcome_json = excluded.outcome_json,
              updated_at = excluded.updated_at
            """,
            rows,
        )
        conn.commit()


def list_c2_action_journal(
    conversation_id: str,
) -> list[dict[str, Any]]:
    with db_connection() as conn:
        rows = conn.execute(
            """
            SELECT flow_id, conversation_id, source_message_key,
                   outcome_json, created_at, updated_at
            FROM c2_action_journal
            WHERE conversation_id = ?
            ORDER BY created_at ASC, source_message_key ASC
            """,
            (str(conversation_id),),
        ).fetchall()
    results: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        try:
            item["outcome"] = json.loads(
                item.pop("outcome_json") or "{}"
            )
        except json.JSONDecodeError:
            item["outcome"] = {}
        results.append(item)
    return results


def clear_c2_action_journal(flow_id: str) -> None:
    with db_connection() as conn:
        conn.execute(
            "DELETE FROM c2_action_journal WHERE flow_id = ?",
            (str(flow_id),),
        )
        conn.commit()


def _c2_outbox_id(payload: dict[str, Any]) -> str:
    read_run_id = str(payload.get("read_run_id") or "").strip()
    evidence = (
        payload.get("evidence")
        if isinstance(payload.get("evidence"), dict)
        else {}
    )
    partition = (
        evidence.get("ingest_partition")
        if isinstance(evidence.get("ingest_partition"), dict)
        else {}
    )
    partition_index = int(partition.get("index") or 0)
    return (
        f"c2-outbox:{read_run_id}:part-{partition_index}"
        if partition_index > 0
        else f"c2-outbox:{read_run_id}"
    )


def enqueue_c2_outbox(payload: dict[str, Any]) -> str:
    _assert_outbox_text_only(payload)
    read_run_id = str(payload.get("read_run_id") or "").strip()
    conversation_id = str(payload.get("conversation_id") or "").strip()
    authorization_revision = str(payload.get("authorization_revision") or "").strip()
    if not read_run_id or not conversation_id or not authorization_revision:
        raise ValueError("C2_OUTBOX_IDENTITY_MISSING")
    outbox_id = _c2_outbox_id(payload)
    now = utc_now_iso()
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    immutable_statuses = sorted(
        _c2_outbox_terminal_states() | {"capability_paused"}
    )
    immutable_placeholders = ",".join(
        "?" for _ in immutable_statuses
    )
    with db_connection() as conn:
        conn.execute(
            f"""
            INSERT INTO c2_ingest_outbox (
              outbox_id, conversation_id, authorization_revision, read_run_id,
              payload_json, status, attempt_count, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 'waiting', 0, ?, ?)
            ON CONFLICT(outbox_id) DO UPDATE SET
              payload_json = CASE
                WHEN c2_ingest_outbox.status IN ({immutable_placeholders})
                THEN c2_ingest_outbox.payload_json
                ELSE excluded.payload_json
              END,
              updated_at = CASE
                WHEN c2_ingest_outbox.status IN ({immutable_placeholders})
                THEN c2_ingest_outbox.updated_at
                ELSE excluded.updated_at
              END
            """,
            (
                outbox_id,
                conversation_id,
                authorization_revision,
                read_run_id,
                encoded,
                now,
                now,
                *immutable_statuses,
                *immutable_statuses,
            ),
        )
        conn.commit()
    return outbox_id


def list_c2_outbox_waiting(limit: int = 20) -> list[dict[str, Any]]:
    now = utc_now_iso()
    with db_connection() as conn:
        rows = conn.execute(
            """
            SELECT outbox_id, conversation_id, authorization_revision, read_run_id,
                   payload_json, status, attempt_count, refresh_attempt_count,
                   last_error, next_attempt_at, created_at, updated_at
            FROM c2_ingest_outbox
            WHERE status IN (
              'waiting', 'retry_waiting', 'refresh_pending',
              'rebuild_pending', 'split_pending', 'capability_paused'
            )
              AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
            ORDER BY created_at ASC
            LIMIT ?
            """,
            (now, max(1, int(limit))),
        ).fetchall()
    result: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        try:
            item["payload"] = json.loads(item.pop("payload_json"))
        except json.JSONDecodeError:
            item["payload"] = {}
        result.append(item)
    return result


def has_pending_c2_outbox() -> bool:
    with db_connection() as conn:
        row = conn.execute(
            """
            SELECT 1
            FROM c2_ingest_outbox
            WHERE status IN (
              'waiting', 'retry_waiting', 'refresh_pending',
              'rebuild_pending', 'split_pending', 'capability_paused'
            )
            LIMIT 1
            """
        ).fetchone()
    return row is not None


def load_c2_outbox_entry(outbox_id: str) -> dict[str, Any] | None:
    with db_connection() as conn:
        row = conn.execute(
            """
            SELECT outbox_id, conversation_id, authorization_revision,
                   read_run_id, payload_json, status, attempt_count,
                   refresh_attempt_count, last_error, next_attempt_at,
                   created_at, updated_at
            FROM c2_ingest_outbox
            WHERE outbox_id = ?
            """,
            (str(outbox_id),),
        ).fetchone()
    if not row:
        return None
    item = dict(row)
    try:
        item["payload"] = json.loads(item.pop("payload_json"))
    except json.JSONDecodeError:
        item["payload"] = {}
    return item


def mark_c2_outbox_attempt(outbox_id: str, error: str | None = None) -> None:
    with db_connection() as conn:
        conn.execute(
            """
            UPDATE c2_ingest_outbox
            SET attempt_count = attempt_count + 1, last_error = ?, updated_at = ?
            WHERE outbox_id = ?
              AND status IN (
                'waiting', 'retry_waiting', 'refresh_pending',
                'rebuild_pending', 'split_pending', 'capability_paused'
              )
            """,
            (str(error or "") or None, utc_now_iso(), str(outbox_id)),
        )
        conn.commit()


def set_c2_outbox_error(outbox_id: str, error: str) -> None:
    with db_connection() as conn:
        conn.execute(
            """
            UPDATE c2_ingest_outbox
            SET last_error = ?, updated_at = ?
            WHERE outbox_id = ?
              AND status IN (
                'waiting', 'retry_waiting', 'refresh_pending',
                'rebuild_pending', 'split_pending', 'capability_paused'
              )
            """,
            (str(error or "") or None, utc_now_iso(), str(outbox_id)),
        )
        conn.commit()


def refresh_c2_outbox_payload(
    outbox_id: str,
    payload: dict[str, Any],
    *,
    next_status: str,
) -> None:
    _assert_outbox_text_only(payload)
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    authorization_revision = str(
        payload.get("authorization_revision") or ""
    ).strip()
    if not authorization_revision:
        raise ValueError("C2_OUTBOX_AUTHORIZATION_REVISION_MISSING")
    if str(next_status) not in _c2_outbox_states():
        raise ValueError("C2_OUTBOX_TARGET_STATE_INVALID")
    with db_connection() as conn:
        cursor = conn.execute(
            """
            UPDATE c2_ingest_outbox
            SET authorization_revision = ?, payload_json = ?,
                status = ?, last_error = NULL, next_attempt_at = NULL,
                updated_at = ?
            WHERE outbox_id = ? AND status = 'refresh_pending'
            """,
            (
                authorization_revision,
                encoded,
                str(next_status),
                utc_now_iso(),
                str(outbox_id),
            ),
        )
        if cursor.rowcount != 1:
            raise ValueError("C2_OUTBOX_NOT_WAITING")
        conn.commit()


def rebuild_c2_outbox_payload(
    outbox_id: str,
    payload: dict[str, Any],
) -> None:
    _assert_outbox_text_only(payload)
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    with db_connection() as conn:
        cursor = conn.execute(
            """
            UPDATE c2_ingest_outbox
            SET payload_json = ?, status = 'waiting', last_error = NULL,
                next_attempt_at = NULL, updated_at = ?
            WHERE outbox_id = ? AND status = 'rebuild_pending'
            """,
            (encoded, utc_now_iso(), str(outbox_id)),
        )
        if cursor.rowcount != 1:
            raise ValueError("C2_OUTBOX_NOT_REBUILD_PENDING")
        conn.commit()


def prepare_c2_outbox_payload(
    outbox_id: str,
    payload: dict[str, Any],
) -> None:
    """Persist a successfully prepared transport payload over its raw checkpoint."""

    _assert_outbox_text_only(payload)
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    with db_connection() as conn:
        cursor = conn.execute(
            """
            UPDATE c2_ingest_outbox
            SET payload_json = ?, status = 'waiting', last_error = NULL,
                next_attempt_at = NULL, updated_at = ?
            WHERE outbox_id = ?
              AND status IN (
                'waiting', 'retry_waiting', 'refresh_pending',
                'rebuild_pending', 'split_pending', 'capability_paused'
              )
            """,
            (encoded, utc_now_iso(), str(outbox_id)),
        )
        if cursor.rowcount != 1:
            raise ValueError("C2_OUTBOX_NOT_PREPARABLE")
        conn.commit()


def replace_c2_outbox_with_partitions(
    outbox_id: str,
    payloads: list[dict[str, Any]],
) -> list[str]:
    if len(payloads) < 2:
        raise ValueError("C2_OUTBOX_PARTITIONS_REQUIRED")
    now = utc_now_iso()
    rows: list[tuple[str, str, str, str, str]] = []
    for payload in payloads:
        _assert_outbox_text_only(payload)
        child_id = _c2_outbox_id(payload)
        conversation_id = str(payload.get("conversation_id") or "").strip()
        authorization_revision = str(
            payload.get("authorization_revision") or ""
        ).strip()
        read_run_id = str(payload.get("read_run_id") or "").strip()
        if (
            not child_id
            or not conversation_id
            or not authorization_revision
            or not read_run_id
        ):
            raise ValueError("C2_OUTBOX_PARTITION_IDENTITY_MISSING")
        rows.append(
            (
                child_id,
                conversation_id,
                authorization_revision,
                read_run_id,
                json.dumps(
                    payload,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            )
        )
    with db_connection() as conn:
        parent = conn.execute(
            "SELECT status FROM c2_ingest_outbox WHERE outbox_id = ?",
            (str(outbox_id),),
        ).fetchone()
        if not parent or str(parent["status"]) != "split_pending":
            raise ValueError("C2_OUTBOX_NOT_SPLIT_PENDING")
        for row in rows:
            conn.execute(
                """
                INSERT INTO c2_ingest_outbox (
                  outbox_id, conversation_id, authorization_revision,
                  read_run_id, payload_json, status, attempt_count,
                  created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'waiting', 0, ?, ?)
                ON CONFLICT(outbox_id) DO NOTHING
                """,
                (*row, now, now),
            )
        conn.execute(
            """
            UPDATE c2_ingest_outbox
            SET status = 'split_completed', last_error = NULL,
                next_attempt_at = NULL, updated_at = ?
            WHERE outbox_id = ? AND status = 'split_pending'
            """,
            (now, str(outbox_id)),
        )
        conn.commit()
    return [row[0] for row in rows]


def transition_c2_outbox(
    outbox_id: str,
    *,
    status: str,
    error: str | None = None,
    increment_refresh: bool = False,
) -> None:
    if str(status) not in _c2_outbox_states():
        raise ValueError("C2_OUTBOX_TARGET_STATE_INVALID")
    with db_connection() as conn:
        row = conn.execute(
            """
            SELECT attempt_count
            FROM c2_ingest_outbox
            WHERE outbox_id = ?
            """,
            (str(outbox_id),),
        ).fetchone()
        attempt_count = int(row["attempt_count"] or 0) if row else 0
        next_attempt_at = (
            None
            if str(status) in {"waiting", "confirmed"}
            else _next_attempt_iso(attempt_count)
        )
        cursor = conn.execute(
            """
            UPDATE c2_ingest_outbox
            SET status = ?, last_error = ?, next_attempt_at = ?, updated_at = ?,
                refresh_attempt_count = refresh_attempt_count + ?
            WHERE outbox_id = ?
              AND status IN (
                'waiting', 'retry_waiting', 'refresh_pending',
                'rebuild_pending', 'split_pending', 'capability_paused'
              )
            """,
            (
                str(status),
                str(error or "") or None,
                next_attempt_at,
                utc_now_iso(),
                1 if increment_refresh else 0,
                str(outbox_id),
            ),
        )
        if cursor.rowcount != 1:
            raise ValueError("C2_OUTBOX_TRANSITION_SOURCE_INVALID")
        conn.commit()


def mark_c2_outbox_capability_paused(
    outbox_id: str,
    error: str,
) -> None:
    transition_c2_outbox(
        outbox_id,
        status="capability_paused",
        error=error,
    )


def save_reply_send_intent(
    *,
    reply_action_id: str,
    task_id: str,
    send_token: str,
    reply_text_hash: str | None = None,
) -> None:
    if not reply_action_id or not task_id or not send_token:
        raise ValueError("REPLY_SEND_INTENT_IDENTITY_MISSING")
    now = utc_now_iso()
    with db_connection() as conn:
        conn.execute(
            """
            INSERT INTO reply_send_ack_outbox (
              reply_action_id, task_id, send_token, status, action_phase,
              reply_text_hash,
              ack_payload_json, attempt_count, created_at, updated_at
            ) VALUES (?, ?, ?, 'intent', 'not_attempted', ?, NULL, 0, ?, ?)
            ON CONFLICT(reply_action_id) DO UPDATE SET
              task_id = excluded.task_id,
              send_token = excluded.send_token,
              reply_text_hash = excluded.reply_text_hash,
              updated_at = excluded.updated_at
            WHERE reply_send_ack_outbox.status != 'confirmed'
            """,
            (
                reply_action_id,
                task_id,
                send_token,
                str(reply_text_hash or "") or None,
                now,
                now,
            ),
        )
        conn.commit()


def finalize_reply_send_ack(
    *,
    reply_action_id: str,
    ack_payload: dict[str, Any],
) -> None:
    _assert_outbox_text_only(ack_payload, path="reply_send_ack")
    encoded = json.dumps(
        ack_payload,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    with db_connection() as conn:
        cursor = conn.execute(
            """
            UPDATE reply_send_ack_outbox
            SET status = 'waiting', ack_payload_json = ?,
                action_phase = ?,
                last_error = NULL, next_attempt_at = NULL, updated_at = ?
            WHERE reply_action_id = ? AND status != 'confirmed'
            """,
            (
                encoded,
                str(ack_payload.get("action_phase") or "not_attempted"),
                utc_now_iso(),
                reply_action_id,
            ),
        )
        if cursor.rowcount != 1:
            raise ValueError("REPLY_SEND_INTENT_NOT_FOUND")
        conn.commit()


def list_reply_send_ack_outbox(limit: int = 20) -> list[dict[str, Any]]:
    now = utc_now_iso()
    with db_connection() as conn:
        rows = conn.execute(
            """
            SELECT reply_action_id, task_id, send_token, status, action_phase,
                   reply_text_hash,
                   ack_payload_json, attempt_count, last_error, next_attempt_at,
                   created_at, updated_at
            FROM reply_send_ack_outbox
            WHERE status IN ('intent', 'waiting', 'capability_paused')
              AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
            ORDER BY created_at ASC
            LIMIT ?
            """,
            (now, max(1, int(limit))),
        ).fetchall()
    items: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        encoded = item.pop("ack_payload_json") or ""
        try:
            item["ack_payload"] = json.loads(encoded) if encoded else None
        except json.JSONDecodeError:
            item["ack_payload"] = None
        items.append(item)
    return items


def has_pending_reply_send_ack_outbox() -> bool:
    with db_connection() as conn:
        row = conn.execute(
            """
            SELECT 1
            FROM reply_send_ack_outbox
            WHERE status IN ('intent', 'waiting', 'capability_paused')
            LIMIT 1
            """
        ).fetchone()
    return row is not None


def load_reply_send_ack_outbox(reply_action_id: str) -> dict[str, Any] | None:
    with db_connection() as conn:
        row = conn.execute(
            """
            SELECT reply_action_id, task_id, send_token, status, action_phase,
                   reply_text_hash,
                   ack_payload_json, attempt_count, last_error, next_attempt_at,
                   created_at, updated_at
            FROM reply_send_ack_outbox
            WHERE reply_action_id = ?
            """,
            (reply_action_id,),
        ).fetchone()
    if not row:
        return None
    item = dict(row)
    encoded = item.pop("ack_payload_json") or ""
    try:
        item["ack_payload"] = json.loads(encoded) if encoded else None
    except json.JSONDecodeError:
        item["ack_payload"] = None
    return item


def discard_reply_send_intent(reply_action_id: str) -> None:
    """Remove only a persisted send that is proven not physically attempted."""

    with db_connection() as conn:
        conn.execute(
            """
            DELETE FROM reply_send_ack_outbox
            WHERE reply_action_id = ?
              AND status = 'intent'
              AND action_phase = 'not_attempted'
            """,
            (reply_action_id,),
        )
        conn.commit()


def mark_reply_send_ack_attempt(reply_action_id: str) -> None:
    with db_connection() as conn:
        conn.execute(
            """
            UPDATE reply_send_ack_outbox
            SET attempt_count = attempt_count + 1, updated_at = ?
            WHERE reply_action_id = ?
              AND status IN ('intent', 'waiting', 'capability_paused')
            """,
            (utc_now_iso(), reply_action_id),
        )
        conn.commit()


def set_reply_send_ack_error(
    reply_action_id: str,
    error: str,
    *,
    status: str = "waiting",
) -> None:
    if status not in {"waiting", "capability_paused"}:
        raise ValueError("REPLY_SEND_ACK_STATUS_INVALID")
    with db_connection() as conn:
        row = conn.execute(
            """
            SELECT attempt_count
            FROM reply_send_ack_outbox
            WHERE reply_action_id = ?
            """,
            (reply_action_id,),
        ).fetchone()
        attempt_count = int(row["attempt_count"] or 0) if row else 0
        conn.execute(
            """
            UPDATE reply_send_ack_outbox
            SET status = ?, last_error = ?, next_attempt_at = ?, updated_at = ?
            WHERE reply_action_id = ?
              AND status IN ('intent', 'waiting', 'capability_paused')
            """,
            (
                status,
                str(error or "") or None,
                _next_attempt_iso(attempt_count),
                utc_now_iso(),
                reply_action_id,
            ),
        )
        conn.commit()


def mark_reply_send_ack_confirmed(reply_action_id: str) -> None:
    with db_connection() as conn:
        conn.execute(
            """
            UPDATE reply_send_ack_outbox
            SET status = 'confirmed', last_error = NULL,
                next_attempt_at = NULL, updated_at = ?
            WHERE reply_action_id = ?
            """,
            (utc_now_iso(), reply_action_id),
        )
        conn.commit()


def prune_terminal_outboxes(
    *,
    retention_days: int = 30,
    max_terminal_rows: int = 5000,
) -> dict[str, int]:
    """Delete only old terminal records; never remove waiting or intent work."""

    cutoff = (
        datetime.now(timezone.utc)
        - timedelta(days=max(1, int(retention_days)))
    ).isoformat()
    keep_limit = max(100, int(max_terminal_rows))
    deleted: dict[str, int] = {}
    with db_connection() as conn:
        for table, identity_column, terminal_statuses in (
            (
                "c2_ingest_outbox",
                "outbox_id",
                tuple(sorted(_c2_outbox_terminal_states())),
            ),
            (
                "reply_send_ack_outbox",
                "reply_action_id",
                ("confirmed",),
            ),
        ):
            placeholders = ",".join("?" for _ in terminal_statuses)
            cursor = conn.execute(
                f"""
                DELETE FROM {table}
                WHERE status IN ({placeholders})
                  AND updated_at < ?
                """,
                (*terminal_statuses, cutoff),
            )
            removed = max(0, int(cursor.rowcount or 0))
            terminal_rows = conn.execute(
                f"""
                SELECT {identity_column}
                FROM {table}
                WHERE status IN ({placeholders})
                ORDER BY updated_at DESC
                LIMIT -1 OFFSET ?
                """,
                (*terminal_statuses, keep_limit),
            ).fetchall()
            stale_ids = [str(row[identity_column]) for row in terminal_rows]
            if stale_ids:
                id_placeholders = ",".join("?" for _ in stale_ids)
                cursor = conn.execute(
                    f"""
                    DELETE FROM {table}
                    WHERE {identity_column} IN ({id_placeholders})
                      AND status IN ({placeholders})
                    """,
                    (*stale_ids, *terminal_statuses),
                )
                removed += max(0, int(cursor.rowcount or 0))
            deleted[table] = removed
        conn.commit()
    return deleted


def is_accept_schedule_active(schedule: dict[str, Any] | None, now: datetime | None = None) -> bool:
    schedule = _normalize_schedule(schedule)
    if not schedule["enabled"]:
        return True
    current = now or datetime.now()
    current_minutes = current.hour * 60 + current.minute
    start_hour, start_minute = [int(part) for part in schedule["start"].split(":")]
    end_hour, end_minute = [int(part) for part in schedule["end"].split(":")]
    start_minutes = start_hour * 60 + start_minute
    end_minutes = end_hour * 60 + end_minute
    if start_minutes == end_minutes:
        return True
    if start_minutes < end_minutes:
        return start_minutes <= current_minutes < end_minutes
    return current_minutes >= start_minutes or current_minutes < end_minutes


def append_log(
    level: str,
    event: str,
    message: str,
    *,
    task_id: str | None = None,
    error_code: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    record_id = str(uuid.uuid4())
    with db_connection() as conn:
        conn.execute(
            """
            INSERT INTO local_logs (id, created_at, level, event, task_id, error_code, message, metadata)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record_id,
                utc_now_iso(),
                level,
                event,
                task_id,
                error_code,
                message,
                json.dumps(metadata or {}, ensure_ascii=False),
            ),
        )
        conn.commit()
    prune_logs()


def read_logs(limit: int = 200) -> list[dict[str, Any]]:
    with db_connection() as conn:
        rows = conn.execute(
            """
            SELECT id, created_at, level, event, task_id, error_code, message, metadata
            FROM local_logs
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    result: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        try:
            item["metadata"] = json.loads(item.get("metadata") or "{}")
        except json.JSONDecodeError:
            item["metadata"] = {}
        result.append(item)
    return result


def prune_logs() -> None:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)).isoformat()
    with db_connection() as conn:
        conn.execute("DELETE FROM local_logs WHERE created_at < ?", (cutoff,))
        keep_rows = conn.execute(
            "SELECT id FROM local_logs ORDER BY created_at DESC LIMIT ?",
            (MAX_LOGS,),
        ).fetchall()
        keep_ids = [row["id"] for row in keep_rows]
        if keep_ids:
            placeholders = ",".join("?" for _ in keep_ids)
            conn.execute(f"DELETE FROM local_logs WHERE id NOT IN ({placeholders})", keep_ids)
        conn.commit()


def export_debug_snapshot() -> dict[str, Any]:
    binding = load_binding()
    return {
        "app_dir": str(APP_DIR),
        "db_file": str(DB_FILE),
        "binding": asdict(binding) if binding else None,
        "accept_schedule": load_accept_schedule(),
        "c2": {
            "last_scan": load_c2_state("last_scan"),
            "last_message_read": load_c2_state("last_message_read"),
        },
        "recent_logs": read_logs(limit=50),
    }
