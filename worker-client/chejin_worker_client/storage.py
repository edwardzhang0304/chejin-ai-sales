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
from .models import Binding, utc_now_iso


APP_DIR = CONFIG.app_dir
DB_FILE = APP_DIR / "worker_client.sqlite3"
MAX_LOGS = 1000
RETENTION_DAYS = 30
DEFAULT_ACCEPT_SCHEDULE = {"enabled": False, "start": "09:00", "end": "21:00"}
TIME_RE = re.compile(r"^\d{2}:\d{2}$")


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
    conn.execute("CREATE INDEX IF NOT EXISTS idx_local_logs_created_at ON local_logs(created_at)")
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
