from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

from .api import ApiError
from .config import CONFIG
from .c2_contract import c2_contract_v3
from .models import Binding


def _contract_standard_stage_names() -> frozenset[str]:
    contract = c2_contract_v3().get("observability_contract")
    values = contract.get("standard_stage_names") if isinstance(contract, dict) else None
    if not isinstance(values, list) or not values:
        raise RuntimeError("Invalid observability standard stage contract")
    names = frozenset(str(value).strip() for value in values if str(value).strip())
    if len(names) != len(values):
        raise RuntimeError("Duplicate or empty observability standard stage name")
    return names


STANDARD_STAGE_NAMES = _contract_standard_stage_names()
TERMINAL_STATUSES = frozenset({"succeeded", "failed", "cancelled", "abandoned"})
_UPLOAD_LOCK = threading.Lock()
_UPLOAD_ACTIVE = False
DELIVERY_PENDING = "pending"
DELIVERY_QUARANTINED = "quarantined"
MAX_BUFFERED_STAGE_EVENTS = 5000
MAX_EVENT_PAYLOAD_BYTES = 64 * 1024
MAX_TELEMETRY_STORAGE_BYTES = 64 * 1024 * 1024
MAX_PROCESS_LINK_ROWS = 5000
MAX_STAGE_ATTEMPT_ROWS = 5000
MAX_AUTHORITY_SNAPSHOTS = 500
MAX_AUTHORITY_SNAPSHOT_BYTES = 512 * 1024


def utc_iso_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def telemetry_db_path() -> Path:
    return CONFIG.app_dir / "worker_telemetry.sqlite3"


def _telemetry_storage_bytes(path: Path) -> int:
    total = 0
    for candidate in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
        try:
            total += candidate.stat().st_size
        except OSError:
            continue
    return total


def _storage_has_room_for_row(path: Path) -> bool:
    return (
        _telemetry_storage_bytes(path) + 4096
        <= MAX_TELEMETRY_STORAGE_BYTES
    )


def _prune_process_links(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        DELETE FROM telemetry_process_links
         WHERE rowid IN (
            SELECT rowid
              FROM telemetry_process_links
             ORDER BY created_at DESC, rowid DESC
             LIMIT -1 OFFSET ?
         )
        """,
        (MAX_PROCESS_LINK_ROWS,),
    )


def _prune_stage_attempts(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        DELETE FROM telemetry_stage_attempts
         WHERE rowid IN (
            SELECT rowid
              FROM telemetry_stage_attempts
             ORDER BY updated_at DESC, rowid DESC
             LIMIT -1 OFFSET ?
         )
        """,
        (MAX_STAGE_ATTEMPT_ROWS,),
    )


def _ensure_column(
    conn: sqlite3.Connection,
    table: str,
    column: str,
    declaration: str,
) -> None:
    existing = {
        str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")
    }
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")


@contextmanager
def _connect(path: Path) -> Iterator[sqlite3.Connection]:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=0.2)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=200")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS telemetry_stage_events (
                stage_run_id TEXT PRIMARY KEY,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                upload_attempt_count INTEGER NOT NULL DEFAULT 0,
                next_attempt_at REAL NOT NULL DEFAULT 0,
                delivery_state TEXT NOT NULL DEFAULT 'pending',
                last_error_code TEXT,
                last_http_status INTEGER,
                quarantined_at TEXT
            )
            """
        )
        _ensure_column(
            conn,
            "telemetry_stage_events",
            "delivery_state",
            "TEXT NOT NULL DEFAULT 'pending'",
        )
        _ensure_column(
            conn,
            "telemetry_stage_events",
            "last_error_code",
            "TEXT",
        )
        _ensure_column(
            conn,
            "telemetry_stage_events",
            "last_http_status",
            "INTEGER",
        )
        _ensure_column(
            conn,
            "telemetry_stage_events",
            "quarantined_at",
            "TEXT",
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS telemetry_process_links (
                local_run_id TEXT PRIMARY KEY,
                process_run_id TEXT NOT NULL,
                conversation_id TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS telemetry_stage_attempts (
                process_run_id TEXT NOT NULL,
                stage_name TEXT NOT NULL,
                last_attempt INTEGER NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (process_run_id, stage_name)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS telemetry_authority_snapshots (
                process_run_id TEXT PRIMARY KEY,
                report_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def remember_process_run(
    local_run_id: str,
    process_run_id: str,
    *,
    conversation_id: str | None = None,
    db_path: Path | None = None,
) -> bool:
    if not CONFIG.observability_enabled:
        return False
    try:
        uuid.UUID(str(process_run_id))
        path = db_path or telemetry_db_path()
        with _connect(path) as conn:
            existing = conn.execute(
                "SELECT process_run_id FROM telemetry_process_links WHERE local_run_id = ?",
                (str(local_run_id),),
            ).fetchone()
            if existing is not None:
                return str(existing["process_run_id"]) == str(process_run_id)
            if not _storage_has_room_for_row(path):
                return False
            conn.execute(
                """
                INSERT INTO telemetry_process_links (
                    local_run_id, process_run_id, conversation_id, created_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(local_run_id) DO NOTHING
                """,
                (
                    str(local_run_id),
                    str(process_run_id),
                    conversation_id,
                    utc_iso_now(),
                ),
            )
            _prune_process_links(conn)
            row = conn.execute(
                "SELECT process_run_id FROM telemetry_process_links WHERE local_run_id = ?",
                (str(local_run_id),),
            ).fetchone()
        return bool(row and str(row["process_run_id"]) == str(process_run_id))
    except Exception:
        return False


def load_process_run(
    local_run_id: str,
    *,
    db_path: Path | None = None,
) -> str | None:
    if not CONFIG.observability_enabled:
        return None
    try:
        with _connect(db_path or telemetry_db_path()) as conn:
            row = conn.execute(
                "SELECT process_run_id FROM telemetry_process_links WHERE local_run_id = ?",
                (str(local_run_id),),
            ).fetchone()
        return str(row["process_run_id"]) if row else None
    except Exception:
        return None


def _valid_event(event: dict[str, Any]) -> bool:
    if event.get("stage_name") not in STANDARD_STAGE_NAMES:
        return False
    if event.get("status") not in {"running", *TERMINAL_STATUSES}:
        return False
    if int(event.get("attempt") or 0) < 1:
        return False
    try:
        uuid.UUID(str(event.get("process_run_id") or ""))
        uuid.UUID(str(event.get("stage_run_id") or ""))
    except (ValueError, TypeError, AttributeError):
        return False
    return True


def next_local_stage_attempt(
    process_run_id: str,
    stage_name: str,
    *,
    db_path: Path | None = None,
) -> int:
    if not CONFIG.observability_enabled:
        return 1
    try:
        path = db_path or telemetry_db_path()
        with _connect(path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT last_attempt FROM telemetry_stage_attempts "
                "WHERE process_run_id = ? AND stage_name = ?",
                (process_run_id, stage_name),
            ).fetchone()
            attempt = max(1, int((row[0] if row else 0) or 0) + 1)
            if row is None and not _storage_has_room_for_row(path):
                return 1
            conn.execute(
                """
                INSERT INTO telemetry_stage_attempts (
                    process_run_id, stage_name, last_attempt, updated_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(process_run_id, stage_name) DO UPDATE SET
                    last_attempt = excluded.last_attempt,
                    updated_at = excluded.updated_at
                """,
                (process_run_id, stage_name, attempt, utc_iso_now()),
            )
            _prune_stage_attempts(conn)
            conn.commit()
        return attempt
    except Exception:
        return 1


def allocate_stage_attempt(
    process_run_id: str,
    stage_name: str,
    stable_key: str,
    *,
    db_path: Path | None = None,
) -> tuple[int, str]:
    """Allocate one retry identity without reusing the prior stage row."""

    if not str(stable_key or "").strip():
        raise ValueError("stable stage attempt key is required")
    attempt = next_local_stage_attempt(
        process_run_id,
        stage_name,
        db_path=db_path,
    )
    # A fresh identity is required even if a very old bounded attempt counter
    # has already been pruned. The attempt remains an ordered diagnostic; it
    # must never be the sole uniqueness source for a persisted stage row.
    stage_run_id = str(uuid.uuid4())
    return attempt, stage_run_id


def enqueue_stage_event(
    event: dict[str, Any],
    *,
    db_path: Path | None = None,
) -> bool:
    """Persist telemetry best-effort. This function never raises into business code."""

    if not CONFIG.observability_enabled or not _valid_event(event):
        return False
    try:
        path = db_path or telemetry_db_path()
        payload = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
        payload_bytes = len(payload.encode("utf-8"))
        if payload_bytes > MAX_EVENT_PAYLOAD_BYTES:
            return False
        with _connect(path) as conn:
            existing = conn.execute(
                "SELECT 1 FROM telemetry_stage_events WHERE stage_run_id = ?",
                (event["stage_run_id"],),
            ).fetchone()
            if existing is None:
                buffered_count = int(
                    conn.execute(
                        "SELECT count(*) FROM telemetry_stage_events"
                    ).fetchone()[0]
                )
                if (
                    buffered_count >= MAX_BUFFERED_STAGE_EVENTS
                    # Reserve one SQLite page in addition to the encoded row so
                    # the configured total is a real admission limit, not a
                    # threshold checked only after the next row has overflowed it.
                    or _telemetry_storage_bytes(path) + payload_bytes + 4096
                    > MAX_TELEMETRY_STORAGE_BYTES
                ):
                    return False
            conn.execute(
                """
                INSERT INTO telemetry_stage_events (
                    stage_run_id, payload_json, created_at,
                    upload_attempt_count, next_attempt_at, delivery_state
                ) VALUES (?, ?, ?, 0, 0, 'pending')
                ON CONFLICT(stage_run_id) DO UPDATE SET
                    payload_json = CASE
                        WHEN telemetry_stage_events.delivery_state = 'pending'
                         AND json_extract(telemetry_stage_events.payload_json, '$.status') = 'running'
                         AND json_extract(excluded.payload_json, '$.status') != 'running'
                        THEN excluded.payload_json
                        ELSE telemetry_stage_events.payload_json
                    END
                """,
                (event["stage_run_id"], payload, utc_iso_now()),
            )
        return True
    except Exception:
        return False


def pending_stage_events(
    *,
    db_path: Path | None = None,
    limit: int | None = None,
    now_monotonic: float | None = None,
) -> list[dict[str, Any]]:
    try:
        path = db_path or telemetry_db_path()
        batch_size = int(limit or CONFIG.observability_upload_batch_size)
        ready_at = time.time() if now_monotonic is None else now_monotonic
        with _connect(path) as conn:
            rows = conn.execute(
                """
                SELECT payload_json
                 FROM telemetry_stage_events
                 WHERE delivery_state = 'pending'
                   AND next_attempt_at <= ?
                 ORDER BY created_at, stage_run_id
                 LIMIT ?
                """,
                (ready_at, batch_size),
            ).fetchall()
        return [json.loads(str(row["payload_json"])) for row in rows]
    except Exception:
        return []


def quarantined_stage_events(
    *,
    db_path: Path | None = None,
    limit: int = MAX_BUFFERED_STAGE_EVENTS,
) -> list[dict[str, Any]]:
    try:
        path = db_path or telemetry_db_path()
        with _connect(path) as conn:
            rows = conn.execute(
                """
                SELECT payload_json, last_error_code, last_http_status,
                       quarantined_at
                  FROM telemetry_stage_events
                 WHERE delivery_state = 'quarantined'
                 ORDER BY created_at, stage_run_id
                 LIMIT ?
                """,
                (max(1, min(int(limit), MAX_BUFFERED_STAGE_EVENTS)),),
            ).fetchall()
        return [
            {
                "payload": json.loads(str(row["payload_json"])),
                "last_error_code": row["last_error_code"],
                "last_http_status": row["last_http_status"],
                "quarantined_at": row["quarantined_at"],
            }
            for row in rows
        ]
    except Exception:
        return []


def remember_authority_snapshots(
    snapshots: list[dict[str, Any]],
    *,
    db_path: Path | None = None,
) -> int:
    """Cache bounded backend-computed reports for evidence export only."""

    if not CONFIG.observability_enabled or not snapshots:
        return 0
    accepted: list[tuple[str, str, str]] = []
    for snapshot in snapshots:
        if not isinstance(snapshot, dict):
            continue
        process_run_id = str(snapshot.get("process_run_id") or "").strip()
        try:
            uuid.UUID(process_run_id)
        except (TypeError, ValueError, AttributeError):
            continue
        encoded = json.dumps(
            snapshot,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        if len(encoded.encode("utf-8")) > MAX_AUTHORITY_SNAPSHOT_BYTES:
            continue
        accepted.append((process_run_id, encoded, utc_iso_now()))
    if not accepted:
        return 0
    try:
        path = db_path or telemetry_db_path()
        with _connect(path) as conn:
            remaining_bytes = max(
                0,
                MAX_TELEMETRY_STORAGE_BYTES
                - _telemetry_storage_bytes(path),
            )
            bounded: list[tuple[str, str, str]] = []
            for item in accepted:
                estimated_bytes = len(item[1].encode("utf-8")) + 256
                if estimated_bytes > remaining_bytes:
                    continue
                bounded.append(item)
                remaining_bytes -= estimated_bytes
            if not bounded:
                return 0
            conn.executemany(
                """
                INSERT INTO telemetry_authority_snapshots (
                    process_run_id, report_json, updated_at
                ) VALUES (?, ?, ?)
                ON CONFLICT(process_run_id) DO UPDATE SET
                    report_json = excluded.report_json,
                    updated_at = excluded.updated_at
                """,
                bounded,
            )
            conn.execute(
                """
                DELETE FROM telemetry_authority_snapshots
                 WHERE process_run_id NOT IN (
                    SELECT process_run_id
                      FROM telemetry_authority_snapshots
                     ORDER BY updated_at DESC, process_run_id DESC
                     LIMIT ?
                 )
                """,
                (MAX_AUTHORITY_SNAPSHOTS,),
            )
        return len(bounded)
    except Exception:
        return 0


def authority_snapshots(
    *,
    db_path: Path | None = None,
) -> list[dict[str, Any]]:
    try:
        path = db_path or telemetry_db_path()
        with _connect(path) as conn:
            rows = conn.execute(
                """
                SELECT report_json
                  FROM telemetry_authority_snapshots
                 ORDER BY updated_at, process_run_id
                """
            ).fetchall()
        return [json.loads(str(row["report_json"])) for row in rows]
    except Exception:
        return []


def _delete_uploaded(stage_run_ids: list[str], *, db_path: Path | None = None) -> None:
    if not stage_run_ids:
        return
    path = db_path or telemetry_db_path()
    with _connect(path) as conn:
        conn.executemany(
            "DELETE FROM telemetry_stage_events WHERE stage_run_id = ?",
            ((item,) for item in stage_run_ids),
        )


def _defer_failed(stage_run_ids: list[str], *, db_path: Path | None = None) -> None:
    if not stage_run_ids:
        return
    path = db_path or telemetry_db_path()
    with _connect(path) as conn:
        for stage_run_id in stage_run_ids:
            row = conn.execute(
                "SELECT upload_attempt_count FROM telemetry_stage_events WHERE stage_run_id = ?",
                (stage_run_id,),
            ).fetchone()
            if row is None:
                continue
            attempt = int(row["upload_attempt_count"] or 0) + 1
            delay = min(300.0, float(2 ** min(attempt, 8)))
            conn.execute(
                """
                UPDATE telemetry_stage_events
                   SET upload_attempt_count = ?, next_attempt_at = ?
                 WHERE stage_run_id = ? AND delivery_state = 'pending'
                """,
                (attempt, time.time() + delay, stage_run_id),
            )


def _quarantine_rejected(
    stage_run_ids: list[str],
    *,
    error: ApiError,
    db_path: Path | None = None,
) -> None:
    if not stage_run_ids:
        return
    path = db_path or telemetry_db_path()
    with _connect(path) as conn:
        conn.executemany(
            """
            UPDATE telemetry_stage_events
               SET delivery_state = 'quarantined',
                   last_error_code = ?,
                   last_http_status = ?,
                   quarantined_at = ?,
                   next_attempt_at = 0
             WHERE stage_run_id = ? AND delivery_state = 'pending'
            """,
            (
                (
                    str(error.code or "OBSERVABILITY_REJECTED"),
                    int(error.status_code or 0),
                    utc_iso_now(),
                    stage_run_id,
                )
                for stage_run_id in stage_run_ids
            ),
        )


def _permanent_rejection(error: Exception) -> bool:
    status_code = (
        int(error.status_code or 0)
        if isinstance(error, ApiError)
        else 0
    )
    return (
        isinstance(error, ApiError)
        and 400 <= status_code < 500
        and status_code not in {408, 425, 429}
        and error.retryable is not True
    )


def flush_stage_events(
    api: Any,
    binding: Binding,
    *,
    db_path: Path | None = None,
) -> int:
    """Best-effort one-batch upload. All failures remain outside the business path."""

    if not CONFIG.observability_enabled:
        return 0
    events = pending_stage_events(db_path=db_path)
    if not events:
        return 0
    stage_run_ids = [str(item["stage_run_id"]) for item in events]
    try:
        response = api.post_observability_stage_events(
            binding,
            events,
            timeout=CONFIG.observability_upload_timeout_seconds,
        )
        remember_authority_snapshots(
            list(response.get("authority_snapshots") or [])
            if isinstance(response, dict)
            else [],
            db_path=db_path,
        )
        _delete_uploaded(stage_run_ids, db_path=db_path)
        return len(events)
    except Exception as error:
        if _permanent_rejection(error):
            uploaded = 0
            # A rejected batch does not identify the invalid member. Retry
            # each item once so valid events are not quarantined with it.
            for event in events:
                stage_run_id = str(event["stage_run_id"])
                try:
                    response = api.post_observability_stage_events(
                        binding,
                        [event],
                        timeout=CONFIG.observability_upload_timeout_seconds,
                    )
                    remember_authority_snapshots(
                        list(response.get("authority_snapshots") or [])
                        if isinstance(response, dict)
                        else [],
                        db_path=db_path,
                    )
                    _delete_uploaded([stage_run_id], db_path=db_path)
                    uploaded += 1
                except Exception as item_error:
                    try:
                        if _permanent_rejection(item_error):
                            _quarantine_rejected(
                                [stage_run_id],
                                error=item_error,
                                db_path=db_path,
                            )
                        else:
                            _defer_failed([stage_run_id], db_path=db_path)
                    except Exception:
                        pass
            return uploaded
        try:
            _defer_failed(stage_run_ids, db_path=db_path)
        except Exception:
            pass
        return 0


def schedule_stage_event_upload(
    api: Any,
    binding: Binding,
    *,
    db_path: Path | None = None,
    thread_factory: Callable[..., threading.Thread] = threading.Thread,
) -> bool:
    """Start at most one daemon uploader; the caller never waits for network IO."""

    global _UPLOAD_ACTIVE
    if not CONFIG.observability_enabled:
        return False
    with _UPLOAD_LOCK:
        if _UPLOAD_ACTIVE:
            return False
        _UPLOAD_ACTIVE = True

    def run() -> None:
        global _UPLOAD_ACTIVE
        try:
            flush_stage_events(api, binding, db_path=db_path)
        finally:
            with _UPLOAD_LOCK:
                _UPLOAD_ACTIVE = False

    try:
        thread_factory(target=run, name="chejin-telemetry-upload", daemon=True).start()
        return True
    except Exception:
        with _UPLOAD_LOCK:
            _UPLOAD_ACTIVE = False
        return False


@dataclass
class StageTimer:
    process_run_id: str
    conversation_id: str | None
    stage_name: str
    component: str
    attempt: int = 0
    parent_stage_run_id: str | None = None
    trace_id: str | None = None
    queued_at: str | None = None
    db_path: Path | None = None
    stage_run_id: str = ""
    started_at: str = ""
    _started_monotonic: float = 0.0

    def __post_init__(self) -> None:
        if self.stage_name not in STANDARD_STAGE_NAMES:
            raise ValueError(f"unknown standard stage: {self.stage_name}")
        if self.attempt < 1:
            self.attempt = next_local_stage_attempt(
                self.process_run_id,
                self.stage_name,
                db_path=self.db_path,
            )
        self.stage_run_id = self.stage_run_id or str(uuid.uuid4())
        self.started_at = self.started_at or utc_iso_now()
        self._started_monotonic = time.perf_counter()
        enqueue_stage_event(
            {
                "process_run_id": self.process_run_id,
                "stage_run_id": self.stage_run_id,
                "parent_stage_run_id": self.parent_stage_run_id,
                "conversation_id": self.conversation_id,
                "stage_name": self.stage_name,
                "component": self.component,
                "attempt": self.attempt,
                "queued_at": self.queued_at,
                "started_at": self.started_at,
                "ended_at": None,
                "queue_duration_ms": None,
                "execution_duration_ms": None,
                "status": "running",
                "error_code": None,
                "trace_id": self.trace_id,
            },
            db_path=self.db_path,
        )

    def finish(
        self,
        *,
        status: str,
        error_code: str | None = None,
        db_path: Path | None = None,
    ) -> dict[str, Any]:
        if status not in TERMINAL_STATUSES:
            raise ValueError("terminal telemetry status is required")
        execution_ms = max(
            0,
            int(round((time.perf_counter() - self._started_monotonic) * 1000)),
        )
        event = {
            "process_run_id": self.process_run_id,
            "stage_run_id": self.stage_run_id,
            "parent_stage_run_id": self.parent_stage_run_id,
            "conversation_id": self.conversation_id,
            "stage_name": self.stage_name,
            "component": self.component,
            "attempt": self.attempt,
            "queued_at": self.queued_at,
            "started_at": self.started_at,
            "ended_at": utc_iso_now(),
            "queue_duration_ms": None,
            "execution_duration_ms": execution_ms,
            "status": status,
            "error_code": error_code,
            "trace_id": self.trace_id,
        }
        enqueue_stage_event(event, db_path=db_path or self.db_path)
        return event


def abandon_buffered_running_stages(*, db_path: Path | None = None) -> int:
    """Close pre-crash open stages without inventing an elapsed duration."""

    if not CONFIG.observability_enabled:
        return 0
    try:
        path = db_path or telemetry_db_path()
        count = 0
        with _connect(path) as conn:
            rows = conn.execute(
                "SELECT stage_run_id, payload_json FROM telemetry_stage_events"
            ).fetchall()
            for row in rows:
                payload = json.loads(str(row["payload_json"]))
                if payload.get("status") != "running":
                    continue
                payload["status"] = "abandoned"
                payload["ended_at"] = utc_iso_now()
                payload["execution_duration_ms"] = None
                payload["error_code"] = "PROCESS_RESTARTED_DURING_STAGE"
                conn.execute(
                    "UPDATE telemetry_stage_events SET payload_json = ? WHERE stage_run_id = ?",
                    (
                        json.dumps(
                            payload,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                        str(row["stage_run_id"]),
                    ),
                )
                count += 1
        return count
    except Exception:
        return 0


def enqueue_existing_duration(
    *,
    process_run_id: str,
    conversation_id: str | None,
    stage_name: str,
    component: str,
    execution_duration_ms: int | None,
    status: str,
    error_code: str | None = None,
    trace_id: str | None = None,
    attempt: int = 1,
    stage_run_id: str | None = None,
    db_path: Path | None = None,
) -> dict[str, Any] | None:
    """Map an existing monotonic duration without starting a second timer."""

    if status not in TERMINAL_STATUSES or stage_name not in STANDARD_STAGE_NAMES:
        return None
    ended = datetime.now(timezone.utc)
    duration_ms = (
        max(0, int(execution_duration_ms))
        if execution_duration_ms is not None
        else None
    )
    started = (
        datetime.fromtimestamp(
            ended.timestamp() - (duration_ms / 1000.0),
            tz=timezone.utc,
        )
        if duration_ms is not None
        else ended
    )
    event = {
        "process_run_id": process_run_id,
        "stage_run_id": stage_run_id or str(uuid.uuid4()),
        "parent_stage_run_id": None,
        "conversation_id": conversation_id,
        "stage_name": stage_name,
        "component": component,
        "attempt": attempt,
        "queued_at": None,
        "started_at": started.isoformat().replace("+00:00", "Z"),
        "ended_at": ended.isoformat().replace("+00:00", "Z"),
        "queue_duration_ms": None,
        "execution_duration_ms": duration_ms,
        "status": status,
        "error_code": error_code,
        "trace_id": trace_id,
    }
    enqueue_stage_event(event, db_path=db_path)
    return event


def enqueue_c2_flow_timing_stages(
    *,
    process_run_id: str,
    conversation_id: str,
    read_run_id: str,
    flow_timing: dict[str, Any],
    trace_id: str | None,
    db_path: Path | None = None,
) -> list[dict[str, Any]]:
    """Map the existing C2 monotonic ledger without starting another timer."""

    mapping = {
        "target_chat_locate": "c2.target_locate",
        "initial_message_read": "c2.message_read",
        "voice_transcribe": "c2.voice_transcription",
        "image_understanding": "c2.image_vision",
    }
    emitted: list[dict[str, Any]] = []
    for phase in flow_timing.get("phases") or []:
        if not isinstance(phase, dict):
            continue
        stage_name = mapping.get(str(phase.get("name") or ""))
        if not stage_name:
            continue
        # Every repeated phase is a real attempt. Persist the increment for
        # each emitted event so a later read cannot reuse an attempt number
        # after the earlier telemetry rows have already been uploaded.
        attempt = next_local_stage_attempt(
            process_run_id,
            stage_name,
            db_path=db_path,
        )
        phase_error_code = str(
            phase.get("error_code")
            or phase.get("failure_code")
            or ""
        ).strip()
        failed_marker = phase.get("failed")
        failed_count_present = bool(
            isinstance(failed_marker, (int, float))
            and not isinstance(failed_marker, bool)
            and failed_marker > 0
        )
        failed = bool(
            failed_marker is True
            or failed_count_present
            or phase.get("completed") is False
            or phase_error_code
        )
        event = enqueue_existing_duration(
            process_run_id=process_run_id,
            conversation_id=conversation_id,
            stage_name=stage_name,
            component="worker",
            execution_duration_ms=int(
                round(float(phase.get("duration_seconds") or 0) * 1000)
            ),
            status="failed" if failed else "succeeded",
            error_code=(
                phase_error_code or "C2_STAGE_FAILED"
                if failed
                else None
            ),
            trace_id=trace_id,
            attempt=attempt,
            stage_run_id=str(
                uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"chejin:{read_run_id}:{stage_name}:{attempt}",
                )
            ),
            db_path=db_path,
        )
        if event is not None:
            emitted.append(event)
    return emitted
