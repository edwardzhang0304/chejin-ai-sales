from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
import traceback
import uuid
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
import re
from typing import Any, Callable, TypeVar

from .config import CONFIG
from .c2_contract import c2_contract_v3
from .models import Binding, utc_now_iso


APP_DIR = CONFIG.app_dir
DB_FILE = APP_DIR / "worker_client.sqlite3"
MAX_LOGS = 1000
RETENTION_DAYS = 30
MAX_C2_LEDGER_ROWS_PER_CONVERSATION = 2000
DEFAULT_ACCEPT_SCHEDULE = {"enabled": False, "start": "09:00", "end": "21:00"}
RUNTIME_CONTROL_KEY = "runtime_control_v1"
LEGACY_MEDIA_CUTOVER_KEY = "legacy_media_recovery_cutover_v1"
LEGACY_MEDIA_RECOVERY_STATE_PREFIX = "legacy_media_recovery_v1:"
DEFAULT_RUNTIME_CONTROL = {
    "pause_requested": False,
    "pause_requested_at": None,
    "inflight_flow_id": None,
    "inflight_flow_kind": None,
    "inflight_started_at": None,
}
TIME_RE = re.compile(r"^\d{2}:\d{2}$")
T = TypeVar("T")


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
    # This marker is written exactly once on the first 0.9.31-capable open.
    # Rows/files older than it are eligible for the bounded cross-version
    # recovery path; records produced afterwards must satisfy the current
    # identity contract and are never silently downgraded to "legacy".
    conn.execute(
        """
        INSERT OR IGNORE INTO c2_runtime_state (key, value, updated_at)
        VALUES (?, ?, ?)
        """,
        (
            LEGACY_MEDIA_CUTOVER_KEY,
            json.dumps(
                {"cutover_at": utc_now_iso()},
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            utc_now_iso(),
        ),
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS c2_message_ledger (
          conversation_id TEXT NOT NULL,
          source_message_key TEXT NOT NULL,
          origin_read_run_id TEXT NOT NULL,
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
          origin_read_run_id TEXT NOT NULL,
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
    ledger_columns = {
        str(row["name"])
        for row in conn.execute("PRAGMA table_info(c2_message_ledger)").fetchall()
    }
    if "origin_read_run_id" not in ledger_columns:
        conn.execute(
            "ALTER TABLE c2_message_ledger "
            "ADD COLUMN origin_read_run_id TEXT NOT NULL DEFAULT ''"
        )
    action_journal_columns = {
        str(row["name"])
        for row in conn.execute("PRAGMA table_info(c2_action_journal)").fetchall()
    }
    if "origin_read_run_id" not in action_journal_columns:
        conn.execute(
            "ALTER TABLE c2_action_journal "
            "ADD COLUMN origin_read_run_id TEXT NOT NULL DEFAULT ''"
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


def _normalize_runtime_control(payload: dict[str, Any] | None) -> dict[str, Any]:
    source = payload if isinstance(payload, dict) else {}
    flow_id = str(source.get("inflight_flow_id") or "").strip() or None
    flow_kind = str(source.get("inflight_flow_kind") or "").strip() or None
    if flow_kind not in {None, "task", "c2_read", "chat_reply"}:
        flow_kind = None
    if not flow_id:
        flow_kind = None
    return {
        "pause_requested": bool(source.get("pause_requested")),
        "pause_requested_at": (
            str(source.get("pause_requested_at") or "").strip() or None
        ),
        "inflight_flow_id": flow_id,
        "inflight_flow_kind": flow_kind,
        "inflight_started_at": (
            str(source.get("inflight_started_at") or "").strip()
            or None
        ) if flow_id else None,
    }


def load_runtime_control() -> dict[str, Any]:
    with db_connection() as conn:
        row = conn.execute(
            "SELECT value FROM client_settings WHERE key = ?",
            (RUNTIME_CONTROL_KEY,),
        ).fetchone()
    if not row:
        return dict(DEFAULT_RUNTIME_CONTROL)
    try:
        payload = json.loads(row["value"])
    except (json.JSONDecodeError, TypeError):
        return dict(DEFAULT_RUNTIME_CONTROL)
    return _normalize_runtime_control(payload)


def save_runtime_control(payload: dict[str, Any]) -> dict[str, Any]:
    state = _normalize_runtime_control(payload)
    with db_connection() as conn:
        conn.execute(
            """
            INSERT INTO client_settings (key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
              value = excluded.value,
              updated_at = excluded.updated_at
            """,
            (
                RUNTIME_CONTROL_KEY,
                json.dumps(state, ensure_ascii=False, sort_keys=True),
                utc_now_iso(),
            ),
        )
        conn.commit()
    return state


def _mutate_runtime_control(
    mutate: Callable[[dict[str, Any]], dict[str, Any] | None],
) -> dict[str, Any]:
    """Read, validate and write runtime control under one SQLite lock."""

    with db_connection() as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT value FROM client_settings WHERE key = ?",
                (RUNTIME_CONTROL_KEY,),
            ).fetchone()
            if row:
                try:
                    decoded = json.loads(row["value"])
                except (json.JSONDecodeError, TypeError):
                    decoded = {}
            else:
                decoded = {}
            current = _normalize_runtime_control(decoded)
            candidate = mutate(dict(current))
            state = _normalize_runtime_control(
                candidate if isinstance(candidate, dict) else current
            )
            conn.execute(
                """
                INSERT INTO client_settings (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                  value = excluded.value,
                  updated_at = excluded.updated_at
                """,
                (
                    RUNTIME_CONTROL_KEY,
                    json.dumps(state, ensure_ascii=False, sort_keys=True),
                    utc_now_iso(),
                ),
            )
            conn.commit()
            return state
        except Exception:
            conn.rollback()
            raise


def request_runtime_pause() -> dict[str, Any]:
    def mutate(state: dict[str, Any]) -> dict[str, Any]:
        state["pause_requested"] = True
        state["pause_requested_at"] = (
            state.get("pause_requested_at") or utc_now_iso()
        )
        return state

    return _mutate_runtime_control(mutate)


def clear_runtime_pause() -> dict[str, Any]:
    def mutate(state: dict[str, Any]) -> dict[str, Any]:
        state["pause_requested"] = False
        state["pause_requested_at"] = None
        return state

    return _mutate_runtime_control(mutate)


def begin_runtime_flow(flow_id: str, flow_kind: str) -> dict[str, Any]:
    clean_flow_id = str(flow_id or "").strip()
    if not clean_flow_id or flow_kind not in {"task", "c2_read", "chat_reply"}:
        raise ValueError("RUNTIME_INFLIGHT_FLOW_INVALID")

    def mutate(state: dict[str, Any]) -> dict[str, Any]:
        existing = str(state.get("inflight_flow_id") or "")
        if existing and existing != clean_flow_id:
            raise ValueError("RUNTIME_INFLIGHT_FLOW_CONFLICT")
        state.update(
            {
                "inflight_flow_id": clean_flow_id,
                "inflight_flow_kind": flow_kind,
                "inflight_started_at": (
                    state.get("inflight_started_at") or utc_now_iso()
                ),
            }
        )
        return state

    return _mutate_runtime_control(mutate)


def finish_runtime_flow(flow_id: str) -> dict[str, Any]:
    clean_flow_id = str(flow_id or "").strip()

    def mutate(state: dict[str, Any]) -> dict[str, Any]:
        if str(state.get("inflight_flow_id") or "") != clean_flow_id:
            raise ValueError("RUNTIME_INFLIGHT_FLOW_MISMATCH")
        state.update(
            {
                "inflight_flow_id": None,
                "inflight_flow_kind": None,
                "inflight_started_at": None,
            }
        )
        return state

    return _mutate_runtime_control(mutate)


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


def update_c2_state_atomic(
    key: str,
    updater: Callable[[dict[str, Any]], tuple[dict[str, Any], T]],
) -> T:
    """Read, update and persist one C2 state row under one write lock.

    Identity sequence allocation uses this path so concurrent reader threads
    cannot observe the same ``next_sequence`` value.
    """

    clean_key = str(key or "").strip()
    if not clean_key:
        raise ValueError("C2_STATE_KEY_MISSING")
    with db_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT value FROM c2_runtime_state WHERE key = ?",
            (clean_key,),
        ).fetchone()
        current: dict[str, Any] = {}
        if row:
            try:
                decoded = json.loads(row["value"])
            except (TypeError, json.JSONDecodeError):
                decoded = {}
            if isinstance(decoded, dict):
                current = decoded
        updated, result = updater(current)
        if not isinstance(updated, dict):
            raise ValueError("C2_STATE_UPDATE_INVALID")
        conn.execute(
            """
            INSERT INTO c2_runtime_state (key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
              value = excluded.value,
              updated_at = excluded.updated_at
            """,
            (
                clean_key,
                json.dumps(updated, ensure_ascii=False),
                utc_now_iso(),
            ),
        )
        conn.commit()
        return result


def clear_c2_state(key: str) -> None:
    clean_key = str(key or "").strip()
    if not clean_key:
        return
    with db_connection() as conn:
        conn.execute("DELETE FROM c2_runtime_state WHERE key = ?", (clean_key,))
        conn.commit()


def legacy_media_recovery_cutover_at() -> str:
    state = load_c2_state(LEGACY_MEDIA_CUTOVER_KEY)
    return str(state.get("cutover_at") or "").strip()


def legacy_media_recovery_state_key(flow_id: str) -> str:
    clean_flow_id = str(flow_id or "").strip()
    if not clean_flow_id:
        raise ValueError("LEGACY_MEDIA_FLOW_ID_MISSING")
    return f"{LEGACY_MEDIA_RECOVERY_STATE_PREFIX}{clean_flow_id}"


def load_legacy_media_recovery(flow_id: str) -> dict[str, Any]:
    return load_c2_state(legacy_media_recovery_state_key(flow_id))


def save_legacy_media_recovery_decision(
    *,
    flow_id: str,
    legacy_record_digest: str,
    decision: str,
    conversation_id: str | None,
    record_summary: dict[str, Any],
) -> dict[str, Any]:
    """Persist one immutable legacy classification before any side effect."""

    clean_digest = str(legacy_record_digest or "").strip().lower()
    clean_decision = str(decision or "").strip()
    clean_conversation_id = str(conversation_id or "").strip()
    if not re.fullmatch(r"[0-9a-f]{64}", clean_digest):
        raise ValueError("LEGACY_MEDIA_RECORD_DIGEST_INVALID")
    if clean_decision not in {
        "legacy_cancelled_before_trigger",
        "legacy_proven_identity_migration",
        "legacy_identity_unresolved_handoff",
        "legacy_owner_unknown_incident",
    }:
        raise ValueError("LEGACY_MEDIA_RECOVERY_DECISION_INVALID")
    if (
        clean_decision == "legacy_identity_unresolved_handoff"
        and not clean_conversation_id
    ):
        raise ValueError("LEGACY_MEDIA_RECOVERY_CONVERSATION_MISSING")
    now = utc_now_iso()

    def update(current: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        if current:
            if (
                str(current.get("legacy_record_digest") or "").strip()
                != clean_digest
                or str(current.get("decision") or "").strip()
                != clean_decision
                or str(current.get("conversation_id") or "").strip()
                != clean_conversation_id
            ):
                raise ValueError("LEGACY_MEDIA_RECOVERY_DECISION_COLLISION")
            return current, current
        persisted = {
            "schema_version": 1,
            "flow_id": str(flow_id or "").strip(),
            "legacy_record_digest": clean_digest,
            "decision": clean_decision,
            "conversation_id": clean_conversation_id or None,
            "record_summary": dict(record_summary or {}),
            "status": "classified",
            "attempt_count": 0,
            "next_attempt_at": None,
            "last_error": None,
            "classified_at": now,
            "backend_confirmed_at": None,
            "archived_at": None,
        }
        return persisted, persisted

    return update_c2_state_atomic(
        legacy_media_recovery_state_key(flow_id),
        update,
    )


def mark_legacy_media_recovery_retry(
    flow_id: str,
    *,
    error_code: str,
) -> dict[str, Any]:
    """Persist retry/backoff without changing the frozen classification."""

    def update(current: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        if not current:
            raise ValueError("LEGACY_MEDIA_RECOVERY_DECISION_MISSING")
        attempt_count = int(current.get("attempt_count") or 0) + 1
        updated = {
            **current,
            "status": "retry_waiting",
            "attempt_count": attempt_count,
            "next_attempt_at": _next_attempt_iso(attempt_count),
            "last_error": str(error_code or "LEGACY_MEDIA_RECOVERY_RETRY"),
        }
        return updated, updated

    return update_c2_state_atomic(
        legacy_media_recovery_state_key(flow_id),
        update,
    )


def mark_legacy_media_recovery_manual_review(
    flow_id: str,
    *,
    error_code: str,
    error_detail: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Stop automatic retry after a permanent contract/response failure."""

    now = utc_now_iso()

    def update(current: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        if not current:
            raise ValueError("LEGACY_MEDIA_RECOVERY_DECISION_MISSING")
        if current.get("status") in {"backend_confirmed", "archived"}:
            raise ValueError("LEGACY_MEDIA_RECOVERY_ALREADY_CONFIRMED")
        updated = {
            **current,
            "status": "manual_review_required",
            "next_attempt_at": None,
            "last_error": str(
                error_code or "LEGACY_MEDIA_RECOVERY_PERMANENT_FAILURE"
            ),
            "manual_review_detail": dict(error_detail or {}),
            "manual_review_required_at": (
                current.get("manual_review_required_at") or now
            ),
        }
        return updated, updated

    return update_c2_state_atomic(
        legacy_media_recovery_state_key(flow_id),
        update,
    )


def mark_legacy_media_recovery_confirmed(
    flow_id: str,
    *,
    backend_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    now = utc_now_iso()

    def update(current: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        if not current:
            raise ValueError("LEGACY_MEDIA_RECOVERY_DECISION_MISSING")
        result = dict(backend_result or {})
        decision = str(current.get("decision") or "").strip()
        confirmed_resolution = str(result.get("resolution") or "").strip()
        allowed_resolutions = {decision}
        if decision == "legacy_identity_unresolved_handoff":
            allowed_resolutions.add("legacy_owner_unknown_incident")
        if (
            result.get("confirmed") is not True
            or str(result.get("legacy_record_digest") or "").strip()
            != str(current.get("legacy_record_digest") or "").strip()
            or confirmed_resolution not in allowed_resolutions
        ):
            raise ValueError("LEGACY_MEDIA_RECOVERY_CONFIRMATION_INVALID")
        updated = {
            **current,
            "status": "backend_confirmed",
            "next_attempt_at": None,
            "last_error": None,
            "backend_result": result,
            "backend_confirmed_at": (
                current.get("backend_confirmed_at") or now
            ),
        }
        return updated, updated

    return update_c2_state_atomic(
        legacy_media_recovery_state_key(flow_id),
        update,
    )


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
                   origin_read_run_id, terminal_state, ingest_state, result_json,
                   first_seen_at, updated_at
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
                   origin_read_run_id, terminal_state, ingest_state, result_json,
                   first_seen_at, updated_at
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


def list_waiting_c2_ledger_conversation_ids(
    *,
    message_type: str | None = None,
) -> list[str]:
    clauses = ["ingest_state = 'waiting'"]
    values: list[Any] = []
    if message_type:
        clauses.append("message_type = ?")
        values.append(str(message_type))
    with db_connection() as conn:
        rows = conn.execute(
            f"""
            SELECT conversation_id, result_json, first_seen_at
            FROM c2_message_ledger
            WHERE {' AND '.join(clauses)}
            ORDER BY first_seen_at ASC, conversation_id ASC
            """,
            values,
        ).fetchall()
    ordered: list[str] = []
    seen: set[str] = set()
    for row in rows:
        conversation_id = str(row["conversation_id"] or "").strip()
        if not conversation_id or conversation_id in seen:
            continue
        try:
            result = json.loads(row["result_json"] or "{}")
        except json.JSONDecodeError:
            result = {}
        if not isinstance(result.get("replayable_observation"), dict):
            continue
        ordered.append(conversation_id)
        seen.add(conversation_id)
    return ordered


def save_c2_ledger_terminal(
    *,
    conversation_id: str,
    source_message_key: str,
    origin_read_run_id: str,
    dedupe_key: str | None,
    message_type: str,
    terminal_state: str,
    ingest_state: str,
    result: dict[str, Any] | None = None,
) -> None:
    clean_origin_read_run_id = str(origin_read_run_id or "").strip()
    if not clean_origin_read_run_id:
        raise ValueError("C2_LEDGER_ORIGIN_READ_RUN_ID_MISSING")
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
        existing_origin = conn.execute(
            """
            SELECT origin_read_run_id FROM c2_message_ledger
            WHERE conversation_id = ? AND source_message_key = ?
            """,
            (str(conversation_id), str(source_message_key)),
        ).fetchone()
        if (
            existing_origin
            and str(existing_origin["origin_read_run_id"] or "").strip()
            and str(existing_origin["origin_read_run_id"]).strip()
            != clean_origin_read_run_id
        ):
            raise ValueError("C2_LEDGER_ORIGIN_READ_RUN_ID_CONFLICT")
        conn.execute(
            """
            INSERT INTO c2_message_ledger (
              conversation_id, source_message_key, origin_read_run_id,
              dedupe_key, message_type,
              terminal_state, ingest_state, result_json, first_seen_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(conversation_id, source_message_key) DO UPDATE SET
              origin_read_run_id = CASE
                WHEN c2_message_ledger.origin_read_run_id = ''
                THEN excluded.origin_read_run_id
                ELSE c2_message_ledger.origin_read_run_id
              END,
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
                clean_origin_read_run_id,
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


def terminate_waiting_c2_image_ledger(
    conversation_id: str,
    *,
    reason: str,
) -> int:
    """Close only waiting image facts after backend confirms target termination."""

    now = utc_now_iso()
    updates: list[tuple[str, str, str, str]] = []
    with db_connection() as conn:
        rows = conn.execute(
            """
            SELECT source_message_key, result_json
            FROM c2_message_ledger
            WHERE conversation_id = ?
              AND message_type = 'image'
              AND ingest_state = 'waiting'
            ORDER BY first_seen_at ASC, source_message_key ASC
            """,
            (str(conversation_id),),
        ).fetchall()
        for row in rows:
            try:
                result = json.loads(row["result_json"] or "{}")
            except json.JSONDecodeError:
                result = {}
            if not isinstance(result, dict):
                result = {}
            result["recovery"] = {
                "state": "target_terminated",
                "reason": str(reason or "backend_confirmed"),
                "confirmed_at": now,
            }
            _assert_outbox_text_only(result, path="ledger_result")
            updates.append(
                (
                    json.dumps(
                        result,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    now,
                    str(conversation_id),
                    str(row["source_message_key"]),
                )
            )
        if updates:
            conn.executemany(
                """
                UPDATE c2_message_ledger
                SET ingest_state = 'not_required',
                    result_json = ?,
                    updated_at = ?
                WHERE conversation_id = ?
                  AND source_message_key = ?
                  AND ingest_state = 'waiting'
                """,
                updates,
            )
        conn.commit()
    return len(updates)


def checkpoint_c2_action_outcomes(
    *,
    flow_id: str,
    conversation_id: str,
    origin_read_run_id: str,
    outcomes: list[dict[str, Any]],
) -> None:
    """Persist irreversible action facts before the flow can exit or crash."""

    normalized_flow_id = str(flow_id or "").strip()
    normalized_conversation_id = str(conversation_id or "").strip()
    normalized_origin_read_run_id = str(origin_read_run_id or "").strip()
    if (
        not normalized_flow_id
        or not normalized_conversation_id
        or not normalized_origin_read_run_id
    ):
        raise ValueError("C2_ACTION_JOURNAL_IDENTITY_MISSING")
    now = utc_now_iso()
    rows: list[tuple[str, str, str, str, str, str, str]] = []
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
                normalized_origin_read_run_id,
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
        for row in rows:
            existing = conn.execute(
                """
                SELECT origin_read_run_id FROM c2_action_journal
                WHERE flow_id = ? AND source_message_key = ?
                """,
                (row[0], row[2]),
            ).fetchone()
            if (
                existing
                and str(existing["origin_read_run_id"] or "").strip()
                and str(existing["origin_read_run_id"]).strip() != row[3]
            ):
                raise ValueError(
                    "C2_ACTION_JOURNAL_ORIGIN_READ_RUN_ID_CONFLICT"
                )
        conn.executemany(
            """
            INSERT INTO c2_action_journal (
              flow_id, conversation_id, source_message_key, origin_read_run_id,
              outcome_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
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
                   origin_read_run_id, outcome_json, created_at, updated_at
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
        item["outcome"]["origin_read_run_id"] = str(
            item.get("origin_read_run_id") or ""
        )
        results.append(item)
    return results


def c2_flow_conversation_ids(read_run_id: str) -> list[str]:
    """Return durable conversation owners for one local C2 flow.

    Restart reconciliation must not guess a conversation from UI state.  The
    only admissible owners are the conversation ids already persisted by the
    Ledger, Outbox or action journal for the exact read run.
    """

    clean_read_run_id = str(read_run_id or "").strip()
    if not clean_read_run_id:
        return []
    with db_connection() as conn:
        rows = conn.execute(
            """
            SELECT conversation_id
            FROM c2_message_ledger
            WHERE origin_read_run_id = ?
            UNION
            SELECT conversation_id
            FROM c2_ingest_outbox
            WHERE read_run_id = ?
            UNION
            SELECT conversation_id
            FROM c2_action_journal
            WHERE origin_read_run_id = ? OR flow_id = ?
            ORDER BY conversation_id ASC
            """,
            (
                clean_read_run_id,
                clean_read_run_id,
                clean_read_run_id,
                clean_read_run_id,
            ),
        ).fetchall()
    return sorted(
        {
            str(row["conversation_id"] or "").strip()
            for row in rows
            if str(row["conversation_id"] or "").strip()
        }
    )


def legacy_media_flow_snapshot(read_run_id: str) -> dict[str, Any]:
    """Load the immutable local facts used to classify one old media flow."""

    clean_read_run_id = str(read_run_id or "").strip()
    if not clean_read_run_id:
        raise ValueError("LEGACY_MEDIA_FLOW_ID_MISSING")
    with db_connection() as conn:
        ledger_rows = conn.execute(
            """
            SELECT conversation_id, source_message_key, origin_read_run_id,
                   dedupe_key, message_type, terminal_state, ingest_state,
                   result_json, first_seen_at
            FROM c2_message_ledger
            WHERE origin_read_run_id = ?
              AND message_type IN ('voice', 'image')
            ORDER BY conversation_id, source_message_key
            """,
            (clean_read_run_id,),
        ).fetchall()
        action_rows = conn.execute(
            """
            SELECT flow_id, conversation_id, source_message_key,
                   origin_read_run_id, outcome_json, created_at
            FROM c2_action_journal
            WHERE origin_read_run_id = ? OR flow_id = ?
            ORDER BY conversation_id, source_message_key
            """,
            (clean_read_run_id, clean_read_run_id),
        ).fetchall()
        outbox_rows = conn.execute(
            """
            SELECT outbox_id, conversation_id, authorization_revision,
                   read_run_id, payload_json, status, created_at
            FROM c2_ingest_outbox
            WHERE read_run_id = ?
            ORDER BY outbox_id
            """,
            (clean_read_run_id,),
        ).fetchall()

    def decode_rows(
        rows: list[sqlite3.Row],
        *,
        json_column: str,
        decoded_column: str,
    ) -> list[dict[str, Any]]:
        decoded: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            try:
                value = json.loads(item.pop(json_column) or "{}")
            except (json.JSONDecodeError, TypeError):
                value = {}
            item[decoded_column] = value if isinstance(value, dict) else {}
            decoded.append(item)
        return decoded

    return {
        "flow_id": clean_read_run_id,
        "ledger": decode_rows(
            ledger_rows,
            json_column="result_json",
            decoded_column="result",
        ),
        "action_journal": decode_rows(
            action_rows,
            json_column="outcome_json",
            decoded_column="outcome",
        ),
        "outbox": decode_rows(
            outbox_rows,
            json_column="payload_json",
            decoded_column="payload",
        ),
    }


def flow_has_pre_cutover_media_records(read_run_id: str) -> bool:
    """Return whether SQLite facts predate the 0.9.31 recovery cutover."""

    clean_read_run_id = str(read_run_id or "").strip()
    cutover_at = legacy_media_recovery_cutover_at()
    if not clean_read_run_id or not cutover_at:
        return False
    with db_connection() as conn:
        row = conn.execute(
            """
            SELECT 1
            FROM (
              SELECT first_seen_at AS created_at
              FROM c2_message_ledger
              WHERE origin_read_run_id = ?
                AND message_type IN ('voice', 'image')
              UNION ALL
              SELECT created_at
              FROM c2_action_journal
              WHERE origin_read_run_id = ? OR flow_id = ?
              UNION ALL
              SELECT created_at
              FROM c2_ingest_outbox
              WHERE read_run_id = ?
            ) AS legacy_candidates
            WHERE created_at < ?
            LIMIT 1
            """,
            (
                clean_read_run_id,
                clean_read_run_id,
                clean_read_run_id,
                clean_read_run_id,
                cutover_at,
            ),
        ).fetchone()
    return row is not None


def archive_legacy_media_flow_records(
    flow_id: str,
    *,
    legacy_record_digest: str,
    resolution: str,
    backend_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Atomically terminalize old local facts after the frozen exit is safe."""

    clean_flow_id = str(flow_id or "").strip()
    clean_digest = str(legacy_record_digest or "").strip().lower()
    state_key = legacy_media_recovery_state_key(clean_flow_id)
    now = utc_now_iso()
    with db_connection() as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")
            state_row = conn.execute(
                "SELECT value FROM c2_runtime_state WHERE key = ?",
                (state_key,),
            ).fetchone()
            if not state_row:
                raise ValueError("LEGACY_MEDIA_RECOVERY_DECISION_MISSING")
            try:
                state = json.loads(state_row["value"] or "{}")
            except (json.JSONDecodeError, TypeError) as exc:
                raise ValueError("LEGACY_MEDIA_RECOVERY_STATE_INVALID") from exc
            if str(state.get("legacy_record_digest") or "").strip() != clean_digest:
                raise ValueError("LEGACY_MEDIA_RECOVERY_DECISION_COLLISION")
            decision = str(state.get("decision") or "").strip()
            clean_resolution = str(resolution or "").strip()
            allowed_resolutions = {decision}
            if decision == "legacy_identity_unresolved_handoff":
                allowed_resolutions.add("legacy_owner_unknown_incident")
            if clean_resolution not in allowed_resolutions:
                raise ValueError("LEGACY_MEDIA_RECOVERY_DECISION_COLLISION")
            if (
                decision != "legacy_proven_identity_migration"
                and str(state.get("status") or "").strip()
                not in {"backend_confirmed", "archived"}
            ):
                raise ValueError(
                    "LEGACY_MEDIA_RECOVERY_BACKEND_CONFIRMATION_MISSING"
                )

            ledger_rows = conn.execute(
                """
                SELECT conversation_id, source_message_key, result_json
                FROM c2_message_ledger
                WHERE origin_read_run_id = ?
                  AND message_type IN ('voice', 'image')
                  AND ingest_state != 'confirmed'
                """,
                (clean_flow_id,),
            ).fetchall()
            for row in ledger_rows:
                try:
                    result = json.loads(row["result_json"] or "{}")
                except (json.JSONDecodeError, TypeError):
                    result = {}
                if not isinstance(result, dict):
                    result = {}
                result["legacy_media_recovery"] = {
                    "state": "archived",
                    "resolution": clean_resolution,
                    "legacy_record_digest": clean_digest,
                    "archived_at": now,
                }
                conn.execute(
                    """
                    UPDATE c2_message_ledger
                    SET ingest_state = 'not_required',
                        result_json = ?, updated_at = ?
                    WHERE conversation_id = ? AND source_message_key = ?
                    """,
                    (
                        json.dumps(
                            result,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                        now,
                        str(row["conversation_id"]),
                        str(row["source_message_key"]),
                    ),
                )
            conn.execute(
                "DELETE FROM c2_action_journal "
                "WHERE origin_read_run_id = ? OR flow_id = ?",
                (clean_flow_id, clean_flow_id),
            )
            conn.execute(
                "DELETE FROM c2_ingest_outbox WHERE read_run_id = ?",
                (clean_flow_id,),
            )
            archived = {
                **state,
                "status": "archived",
                "next_attempt_at": None,
                "last_error": None,
                "backend_result": dict(
                    backend_result
                    if backend_result is not None
                    else state.get("backend_result") or {}
                ),
                "backend_confirmed_at": (
                    state.get("backend_confirmed_at")
                    or (now if backend_result is not None else None)
                ),
                "archived_at": state.get("archived_at") or now,
            }
            conn.execute(
                "UPDATE c2_runtime_state SET value = ?, updated_at = ? "
                "WHERE key = ?",
                (
                    json.dumps(
                        archived,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    now,
                    state_key,
                ),
            )
            conn.commit()
            return archived
        except Exception:
            conn.rollback()
            raise


def clear_c2_action_journal(flow_id: str) -> None:
    with db_connection() as conn:
        conn.execute(
            "DELETE FROM c2_action_journal WHERE flow_id = ?",
            (str(flow_id),),
        )
        conn.commit()


_OUTBOX_BATCH_NAMESPACE = "chejin:c2-local-outbox:v1"
_OUTBOX_IMMUTABLE_MESSAGE_FIELDS = (
    "source_message_key",
    "dedupe_key",
    "sender_role_hint",
    "message_type",
    "content",
    "item_state",
    "flow_state",
)
_OUTBOX_STABLE_VOICE_META_FIELDS = (
    "state",
    "action_phase",
    "ui_action_performed",
    "business_state",
    "business_result_confirmed",
    "canonical_voice_action_id",
    "voice_action_stage",
    "selected_pre_observation_id",
    "selected_action_token",
    "selected_target_fingerprint",
    "message_viewport_change_digest",
    "reserved_worker_stable_id",
    "transcript_binding_status",
    "transcript_binding_method",
    "binding_candidate_count",
    "native_source_message_id",
    "confirmed_action_mapping",
)
_OUTBOX_STABLE_IMAGE_UNDERSTANDING_FIELDS = (
    "schema_version",
    "enabled",
    "applied",
    "adoptable",
    "reason",
    "vision_summary",
    "image_ocr_text",
    "classification",
    "entities",
    "intent_hints",
    "bridge",
    "catalog_alignment",
)


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _sorted_unique_strings(values: Any) -> list[str]:
    return sorted(
        {
            str(value or "").strip()
            for value in (values if isinstance(values, list) else [])
            if str(value or "").strip()
        }
    )


def _c2_outbox_message_keys(payload: dict[str, Any]) -> list[str]:
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
    expected_keys = partition.get("expected_source_message_keys")
    if isinstance(expected_keys, list):
        return _sorted_unique_strings(expected_keys)
    return _sorted_unique_strings(
        [
            item.get("source_message_key")
            for item in (payload.get("messages") or [])
            if isinstance(item, dict)
        ]
    )


def c2_outbox_batch_key(payload: dict[str, Any]) -> str:
    """Return the deterministic local identity for one immutable fact set."""

    evidence = (
        payload.get("evidence")
        if isinstance(payload.get("evidence"), dict)
        else {}
    )
    message_keys = _c2_outbox_message_keys(payload)
    flow_gate_errors = _sorted_unique_strings(
        evidence.get("flow_gate_errors")
    )
    authorization_scope = str(
        payload.get("authorization_scope") or ""
    ).strip()
    if message_keys:
        payload_kind = "messages"
    elif authorization_scope == "fact_settlement":
        payload_kind = "fact_settlement"
    elif flow_gate_errors:
        payload_kind = "flow_gate"
    else:
        payload_kind = "control_read"

    def evidence_or_payload(key: str) -> str:
        return str(evidence.get(key) or payload.get(key) or "").strip()

    flow_gate_identity_key = evidence_or_payload(
        "flow_gate_identity_key"
    ) or "\n".join(flow_gate_errors)
    control_key = ""
    if payload_kind == "control_read":
        control_key = ":".join(
            (
                evidence_or_payload("authorization_read_reason"),
                evidence_or_payload("continuation_batch_id"),
                evidence_or_payload("recall_cycle_id"),
            )
        )
    seed = {
        "namespace": _OUTBOX_BATCH_NAMESPACE,
        "conversation_id": str(
            payload.get("conversation_id") or ""
        ).strip(),
        "read_run_id": str(payload.get("read_run_id") or "").strip(),
        "payload_kind": payload_kind,
        "source_message_keys": message_keys,
        "flow_gate_identity_key": flow_gate_identity_key,
        "recovery_transaction_id": evidence_or_payload(
            "recovery_transaction_id"
        ),
        "source_message_key_digest": evidence_or_payload(
            "source_message_key_digest"
        ),
        "control_key": control_key,
    }
    return hashlib.sha256(
        _canonical_json(seed).encode("utf-8")
    ).hexdigest()


def c2_outbox_id(payload: dict[str, Any]) -> str:
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
    base = (
        f"c2-outbox:{read_run_id}:batch-{c2_outbox_batch_key(payload)}"
    )
    return f"{base}:part-{partition_index}" if partition_index > 0 else base


def _stable_media_fact(message: dict[str, Any]) -> dict[str, Any]:
    raw_payload = (
        message.get("raw_payload")
        if isinstance(message.get("raw_payload"), dict)
        else {}
    )
    voice_meta = (
        raw_payload.get("voice_transcription_meta")
        if isinstance(raw_payload.get("voice_transcription_meta"), dict)
        else {}
    )
    image_understanding = (
        raw_payload.get("customer_image_understanding")
        if isinstance(
            raw_payload.get("customer_image_understanding"), dict
        )
        else {}
    )
    stable: dict[str, Any] = {}
    if voice_meta:
        stable["voice_transcription_meta"] = {
            key: voice_meta.get(key)
            for key in _OUTBOX_STABLE_VOICE_META_FIELDS
            if key in voice_meta
        }
    if image_understanding:
        stable["customer_image_understanding"] = {
            key: image_understanding.get(key)
            for key in _OUTBOX_STABLE_IMAGE_UNDERSTANDING_FIELDS
            if key in image_understanding
        }
    if "visual_bridge_input" in raw_payload:
        stable["visual_bridge_input"] = raw_payload.get(
            "visual_bridge_input"
        )
    for key in ("error_code", "reason_detail"):
        if key in raw_payload:
            stable[key] = raw_payload.get(key)
    return stable


def _c2_outbox_immutable_fact_digest(payload: dict[str, Any]) -> str:
    messages = []
    for item in (payload.get("messages") or []):
        if not isinstance(item, dict):
            continue
        messages.append(
            {
                **{
                    key: item.get(key)
                    for key in _OUTBOX_IMMUTABLE_MESSAGE_FIELDS
                },
                "stable_media_result": _stable_media_fact(item),
            }
        )
    messages.sort(
        key=lambda item: (
            str(item.get("source_message_key") or ""),
            _canonical_json(item),
        )
    )
    return hashlib.sha256(
        _canonical_json(messages).encode("utf-8")
    ).hexdigest()


def _assert_existing_outbox_fact_matches(
    existing_payload_json: str,
    payload: dict[str, Any],
) -> None:
    try:
        existing_payload = json.loads(existing_payload_json or "{}")
    except json.JSONDecodeError as exc:
        raise ValueError("C2_OUTBOX_LOGICAL_FACT_COLLISION") from exc
    if _c2_outbox_immutable_fact_digest(
        existing_payload
    ) != _c2_outbox_immutable_fact_digest(payload):
        raise ValueError("C2_OUTBOX_LOGICAL_FACT_COLLISION")


def enqueue_c2_outbox(payload: dict[str, Any]) -> str:
    _assert_outbox_text_only(payload)
    read_run_id = str(payload.get("read_run_id") or "").strip()
    conversation_id = str(payload.get("conversation_id") or "").strip()
    authorization_revision = str(payload.get("authorization_revision") or "").strip()
    if not read_run_id or not conversation_id or not authorization_revision:
        raise ValueError("C2_OUTBOX_IDENTITY_MISSING")
    outbox_id = c2_outbox_id(payload)
    now = utc_now_iso()
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    with db_connection() as conn:
        cursor = conn.execute(
            """
            INSERT INTO c2_ingest_outbox (
              outbox_id, conversation_id, authorization_revision, read_run_id,
              payload_json, status, attempt_count, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 'waiting', 0, ?, ?)
            ON CONFLICT(outbox_id) DO NOTHING
            """,
            (
                outbox_id,
                conversation_id,
                authorization_revision,
                read_run_id,
                encoded,
                now,
                now,
            ),
        )
        if cursor.rowcount == 0:
            existing = conn.execute(
                "SELECT payload_json FROM c2_ingest_outbox WHERE outbox_id = ?",
                (outbox_id,),
            ).fetchone()
            if not existing:
                raise ValueError("C2_OUTBOX_LOGICAL_FACT_COLLISION")
            _assert_existing_outbox_fact_matches(
                str(existing["payload_json"] or "{}"),
                payload,
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


def has_pending_c2_outbox_for_read_run_id(read_run_id: str) -> bool:
    clean_id = str(read_run_id or "").strip()
    if not clean_id:
        return False
    with db_connection() as conn:
        row = conn.execute(
            """
            SELECT 1 FROM c2_ingest_outbox
            WHERE read_run_id = ?
              AND status IN (
                'waiting', 'retry_waiting', 'refresh_pending',
                'rebuild_pending', 'split_pending', 'capability_paused'
              )
            LIMIT 1
            """,
            (clean_id,),
        ).fetchone()
    return row is not None


def has_c2_outbox_for_read_run_id(read_run_id: str) -> bool:
    """Return whether the read run has created any durable Outbox artifact."""

    clean_id = str(read_run_id or "").strip()
    if not clean_id:
        return False
    with db_connection() as conn:
        row = conn.execute(
            """
            SELECT 1 FROM c2_ingest_outbox
            WHERE read_run_id = ?
            LIMIT 1
            """,
            (clean_id,),
        ).fetchone()
    return row is not None


def has_c2_ledger_for_origin_read_run_id(
    read_run_id: str,
    *,
    pending_only: bool = False,
) -> bool:
    clean_id = str(read_run_id or "").strip()
    if not clean_id:
        return False
    pending_clause = "AND ingest_state = 'waiting'" if pending_only else ""
    with db_connection() as conn:
        row = conn.execute(
            f"""
            SELECT 1 FROM c2_message_ledger
            WHERE origin_read_run_id = ? {pending_clause}
            LIMIT 1
            """,
            (clean_id,),
        ).fetchone()
    return row is not None


def has_c2_action_journal_for_origin_read_run_id(read_run_id: str) -> bool:
    clean_id = str(read_run_id or "").strip()
    if not clean_id:
        return False
    with db_connection() as conn:
        row = conn.execute(
            """
            SELECT 1 FROM c2_action_journal
            WHERE origin_read_run_id = ?
            LIMIT 1
            """,
            (clean_id,),
        ).fetchone()
    return row is not None


def has_c2_outbox_for_source_keys(
    conversation_id: str,
    source_message_keys: list[str] | set[str] | tuple[str, ...],
) -> bool:
    """Return whether any durable Outbox contains one of the exact facts."""

    expected = {
        str(value).strip()
        for value in source_message_keys
        if str(value).strip()
    }
    if not expected:
        return False
    with db_connection() as conn:
        rows = conn.execute(
            """
            SELECT payload_json
            FROM c2_ingest_outbox
            WHERE conversation_id = ?
            """,
            (str(conversation_id),),
        ).fetchall()
    for row in rows:
        try:
            payload = json.loads(row["payload_json"] or "{}")
        except (json.JSONDecodeError, TypeError):
            return True
        actual = {
            str(item.get("source_message_key") or "").strip()
            for item in (payload.get("messages") or [])
            if isinstance(item, dict)
        }
        if expected & actual:
            return True
    return False


def load_c2_outbox_origin_read_run_ids(
    conversation_id: str,
) -> dict[str, str]:
    """Return immutable fact ownership recorded by every local Outbox.

    A source key appearing under different read runs is an identity conflict;
    callers receive an empty origin so they can fail closed as unknown.
    """

    with db_connection() as conn:
        rows = conn.execute(
            """
            SELECT read_run_id, payload_json
            FROM c2_ingest_outbox
            WHERE conversation_id = ?
            ORDER BY created_at ASC, outbox_id ASC
            """,
            (str(conversation_id),),
        ).fetchall()
    origins: dict[str, str] = {}
    conflicts: set[str] = set()
    for row in rows:
        try:
            payload = json.loads(row["payload_json"] or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        evidence = (
            payload.get("evidence")
            if isinstance(payload.get("evidence"), dict)
            else {}
        )
        slot_origins = {
            str(item.get("source_message_key") or "").strip(): str(
                item.get("origin_read_run_id") or ""
            ).strip()
            for item in (evidence.get("slot_ledger_states") or [])
            if isinstance(item, dict)
            and str(item.get("source_message_key") or "").strip()
        }
        for item in payload.get("messages") or []:
            if not isinstance(item, dict):
                continue
            source_key = str(item.get("source_message_key") or "").strip()
            if not source_key:
                continue
            origin_read_run_id = str(
                slot_origins.get(source_key) or ""
            ).strip()
            if not origin_read_run_id:
                conflicts.add(source_key)
                continue
            previous = origins.get(source_key)
            if previous and previous != origin_read_run_id:
                conflicts.add(source_key)
            else:
                origins[source_key] = origin_read_run_id
    for source_key in conflicts:
        origins[source_key] = ""
    return origins


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
    if c2_outbox_id(payload) != str(outbox_id):
        raise ValueError("C2_OUTBOX_IDENTITY_MISMATCH")
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    authorization_revision = str(
        payload.get("authorization_revision") or ""
    ).strip()
    if not authorization_revision:
        raise ValueError("C2_OUTBOX_AUTHORIZATION_REVISION_MISSING")
    if str(next_status) not in _c2_outbox_states():
        raise ValueError("C2_OUTBOX_TARGET_STATE_INVALID")
    with db_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            """
            SELECT payload_json
            FROM c2_ingest_outbox
            WHERE outbox_id = ? AND status = 'refresh_pending'
            """,
            (str(outbox_id),),
        ).fetchone()
        if not existing:
            raise ValueError("C2_OUTBOX_NOT_WAITING")
        _assert_existing_outbox_fact_matches(
            str(existing["payload_json"] or "{}"),
            payload,
        )
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


def prepare_c2_outbox_payload(
    outbox_id: str,
    payload: dict[str, Any],
) -> None:
    """Persist a successfully prepared transport payload over its raw checkpoint."""

    _assert_outbox_text_only(payload)
    if c2_outbox_id(payload) != str(outbox_id):
        raise ValueError("C2_OUTBOX_IDENTITY_MISMATCH")
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    with db_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            """
            SELECT payload_json
            FROM c2_ingest_outbox
            WHERE outbox_id = ?
              AND status IN (
                'waiting', 'retry_waiting', 'refresh_pending',
                'rebuild_pending', 'split_pending', 'capability_paused'
              )
            """,
            (str(outbox_id),),
        ).fetchone()
        if not existing:
            raise ValueError("C2_OUTBOX_NOT_PREPARABLE")
        _assert_existing_outbox_fact_matches(
            str(existing["payload_json"] or "{}"),
            payload,
        )
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
        child_id = c2_outbox_id(payload)
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
        for row, payload in zip(rows, payloads, strict=True):
            cursor = conn.execute(
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
            if cursor.rowcount == 0:
                existing = conn.execute(
                    "SELECT payload_json FROM c2_ingest_outbox WHERE outbox_id = ?",
                    (row[0],),
                ).fetchone()
                if not existing:
                    raise ValueError("C2_OUTBOX_LOGICAL_FACT_COLLISION")
                _assert_existing_outbox_fact_matches(
                    str(existing["payload_json"] or "{}"),
                    payload,
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
            if str(status) == "waiting"
            or str(status) in _c2_outbox_terminal_states()
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


def mark_c2_outbox_identity_quarantined(
    outbox_id: str,
    error: str,
) -> None:
    transition_c2_outbox(
        outbox_id,
        status="identity_quarantined",
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


def has_pending_reply_send_ack_for_task_id(task_id: str) -> bool:
    clean_id = str(task_id or "").strip()
    if not clean_id:
        return False
    with db_connection() as conn:
        row = conn.execute(
            """
            SELECT 1 FROM reply_send_ack_outbox
            WHERE task_id = ?
              AND status IN ('intent', 'waiting', 'capability_paused')
            LIMIT 1
            """,
            (clean_id,),
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
    force_incident: bool = False,
) -> dict[str, Any]:
    record_id = str(uuid.uuid4())
    stored_metadata = dict(metadata or {})
    if str(level or "").upper() == "ERROR" or force_incident:
        if not stored_metadata.get("traceback"):
            exc_type, exc, exc_traceback = sys.exc_info()
            if exc_type is not None and exc is not None and exc_traceback is not None:
                stored_metadata["traceback"] = "".join(
                    traceback.format_exception(exc_type, exc, exc_traceback)
                )
        try:
            from .incident_evidence import redact_diagnostic

            message = str(redact_diagnostic(message))
            stored_metadata = dict(redact_diagnostic(stored_metadata))
        except Exception:
            pass
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
                json.dumps(stored_metadata, ensure_ascii=False),
            ),
        )
        conn.commit()
    prune_logs()
    incident: dict[str, Any] | None = None
    if str(level or "").upper() == "ERROR" or force_incident:
        try:
            from .incident_evidence import schedule_incident, start_incident_worker

            incident = schedule_incident(
                event=event,
                error_code=error_code,
                message=message,
                task_id=task_id,
                metadata=stored_metadata,
                traceback_text=str(stored_metadata.get("traceback") or ""),
                log_record_id=record_id,
                start_worker=False,
            )
        except Exception as exc:
            stored_metadata["incident_capture_error"] = type(exc).__name__
        else:
            stored_metadata.update(incident)
        with db_connection() as conn:
            conn.execute(
                "UPDATE local_logs SET metadata = ? WHERE id = ?",
                (json.dumps(stored_metadata, ensure_ascii=False), record_id),
            )
            conn.commit()
        if incident:
            start_incident_worker()
    return {
        "id": record_id,
        "incident_id": str((incident or {}).get("incident_id") or ""),
        "evidence_path": str((incident or {}).get("evidence_path") or ""),
    }


def update_log_incident_path(
    record_id: str,
    incident_id: str,
    evidence_path: str,
) -> None:
    """Update an existing log after asynchronous incident capture completes."""

    with db_connection() as conn:
        row = conn.execute(
            "SELECT metadata FROM local_logs WHERE id = ?",
            (record_id,),
        ).fetchone()
        if not row:
            return
        try:
            metadata = json.loads(row["metadata"] or "{}")
        except json.JSONDecodeError:
            metadata = {}
        if not isinstance(metadata, dict):
            metadata = {}
        metadata.update(
            {
                "incident_id": str(incident_id or ""),
                "evidence_path": str(evidence_path or ""),
                "incident_pending": False,
            }
        )
        conn.execute(
            "UPDATE local_logs SET metadata = ? WHERE id = ?",
            (json.dumps(metadata, ensure_ascii=False), record_id),
        )
        conn.commit()


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
