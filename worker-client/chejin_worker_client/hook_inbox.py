"""Durable raw observations; business message identity is a later layer.

Uses the existing SQLite/update access gate. No AI, Hook calls, or backend
requests run here. Limits are required by the caller rather than guessed.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from dataclasses import dataclass
from typing import Any

from . import storage
from .models import utc_now_iso

INBOX_FIELDS = ("receive_seq", "event_uuid", "self_wxid", "account_epoch",
                "process_generation", "raw_digest", "raw_body", "received_at")


@dataclass(frozen=True)
class HookReceiveScope:
    self_wxid: str
    account_epoch: str
    process_generation: str
    hook_api_port: int

    def __post_init__(self) -> None:
        if any(not isinstance(value, str) or not value.strip()
               for value in (self.self_wxid, self.account_epoch, self.process_generation)):
            raise ValueError("HOOK_RECEIVE_SCOPE_INVALID")
        if type(self.hook_api_port) is not int or not 1 <= self.hook_api_port <= 65535:
            raise ValueError("HOOK_RECEIVE_SCOPE_INVALID")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("HOOK_CALLBACK_DUPLICATE_FIELD")
        result[key] = value
    return result


def _reject_constant(_: str) -> None:
    raise ValueError("HOOK_CALLBACK_NONFINITE_NUMBER")


def decode_callback(raw: bytes, scope: HookReceiveScope) -> dict[str, Any]:
    try:
        payload = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object,
                             parse_constant=_reject_constant)
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValueError("HOOK_CALLBACK_JSON_INVALID") from exc
    if not isinstance(payload, dict):
        raise ValueError("HOOK_CALLBACK_OBJECT_REQUIRED")
    if payload.get("selfwxid") != scope.self_wxid or payload.get("ServerPort") != str(scope.hook_api_port):
        raise ValueError("HOOK_CALLBACK_SCOPE_MISMATCH")
    # Unknown event types are still observations, never automatic text messages.
    return payload


class NativeInbox:
    def __init__(self, *, max_body_bytes: int, max_inbox_bytes: int) -> None:
        if (type(max_body_bytes) is not int or type(max_inbox_bytes) is not int
                or not 0 < max_body_bytes <= max_inbox_bytes):
            raise ValueError("HOOK_INBOX_LIMIT_INVALID")
        self.max_body_bytes = max_body_bytes
        self.max_inbox_bytes = max_inbox_bytes

    def persist(self, raw: bytes, scope: HookReceiveScope) -> dict[str, Any]:
        started = time.perf_counter()
        result = self._persist(raw, scope)
        duration_ms = max(0, round((time.perf_counter() - started) * 1000))
        try:
            storage.append_log('INFO', 'hook_callback_saved', '原生消息已保存。',
                metadata={'event_uuid': result['event_uuid'], 'receive_seq': result['receive_seq'],
                          'execution_duration_ms': duration_ms}, include_exception_text=False)
        except Exception:
            # The physical receipt has already committed. Diagnostic failure
            # cannot convert it into an unacknowledged callback or a retry.
            pass
        return result

    def _persist(self, raw: bytes, scope: HookReceiveScope) -> dict[str, Any]:
        if not isinstance(raw, bytes) or not 0 < len(raw) <= self.max_body_bytes:
            raise ValueError("HOOK_CALLBACK_BODY_SIZE_INVALID")
        decode_callback(raw, scope)
        digest = hashlib.sha256(raw).hexdigest()
        with storage.db_connection() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                # Additive schema, created only when Hook intake is explicitly used.
                conn.execute("""CREATE TABLE IF NOT EXISTS hook_raw_inbox (
                    receive_seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_uuid TEXT NOT NULL UNIQUE,
                    self_wxid TEXT NOT NULL, account_epoch TEXT NOT NULL,
                    process_generation TEXT NOT NULL, raw_digest TEXT NOT NULL,
                    raw_body BLOB NOT NULL, received_at TEXT NOT NULL)""")
                fields = tuple(row["name"] for row in conn.execute("PRAGMA table_info(hook_raw_inbox)"))
                if fields != INBOX_FIELDS:
                    raise RuntimeError("HOOK_INBOX_SCHEMA_UNSUPPORTED")
                # Without a provider event ID, equal bytes can also be two
                # genuine outgoing observations. Preserve every HTTP request;
                # native-message/phase deduplication belongs to the fact layer.
                used = conn.execute("SELECT COALESCE(SUM(length(raw_body)),0) FROM hook_raw_inbox").fetchone()[0]
                if used + len(raw) > self.max_inbox_bytes:
                    raise RuntimeError("HOOK_INBOX_CAPACITY_EXCEEDED")
                event_uuid = str(uuid.uuid4())
                cursor = conn.execute("""INSERT INTO hook_raw_inbox
                    (event_uuid,self_wxid,account_epoch,process_generation,raw_digest,raw_body,received_at)
                    VALUES (?,?,?,?,?,?,?)""", (event_uuid, scope.self_wxid, scope.account_epoch,
                    scope.process_generation, digest, sqlite3.Binary(raw), utc_now_iso()))
                conn.commit()
                return {"receive_seq": cursor.lastrowid, "event_uuid": event_uuid}
            except Exception:
                conn.rollback()
                raise
