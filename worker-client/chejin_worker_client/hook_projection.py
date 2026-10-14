"""Transactional raw-to-observation projection. No network or business side effects."""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from typing import Any

from . import storage
from .hook_facts import callback_observations
from .hook_inbox import HookReceiveScope
from .models import utc_now_iso

PROJECTION_VERSION = "hook-observation-projection-v2"
PROJECTION_FIELDS = ("receive_seq", "contract_version", "projection_digest", "projection_json", "projected_at")


class NativeProjectionStore:
    def __init__(self, *, max_projection_bytes: int, max_entries: int) -> None:
        if type(max_projection_bytes) is not int or max_projection_bytes <= 0 or type(max_entries) is not int or max_entries <= 0:
            raise ValueError("HOOK_PROJECTION_LIMIT_INVALID")
        self.max_projection_bytes = max_projection_bytes
        self.max_entries = max_entries

    def project_next(self, scope: HookReceiveScope) -> dict[str, Any] | None:
        """Project one complete package. Repeat calls resume after committed rows.

        Scope is explicit, including the original process generation. Runtime
        recovery must present each retained scope; it cannot relabel old data
        as a newly logged-in account. This store creates no upload authorization.
        """
        with storage.db_connection() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='hook_raw_inbox'").fetchone() is None:
                    conn.rollback()
                    return None
                conn.execute("""CREATE TABLE IF NOT EXISTS hook_observation_projection (
                    receive_seq INTEGER PRIMARY KEY REFERENCES hook_raw_inbox(receive_seq),
                    contract_version TEXT NOT NULL, projection_digest TEXT NOT NULL,
                    projection_json TEXT NOT NULL, projected_at TEXT NOT NULL)""")
                fields = tuple(r["name"] for r in conn.execute("PRAGMA table_info(hook_observation_projection)"))
                if fields != PROJECTION_FIELDS:
                    raise RuntimeError("HOOK_PROJECTION_SCHEMA_UNSUPPORTED")
                row = conn.execute("""SELECT i.* FROM hook_raw_inbox i
                    LEFT JOIN hook_observation_projection p ON p.receive_seq=i.receive_seq
                    WHERE p.receive_seq IS NULL AND i.self_wxid=? AND i.account_epoch=?
                    AND i.process_generation=? ORDER BY i.receive_seq ASC LIMIT 1""",
                    (scope.self_wxid, scope.account_epoch, scope.process_generation)).fetchone()
                if row is None:
                    conn.commit()
                    return None
                raw = row["raw_body"]
                if not isinstance(raw, bytes) or hashlib.sha256(raw).hexdigest() != row["raw_digest"]:
                    raise RuntimeError("HOOK_RAW_EVIDENCE_CHANGED")
                payload = {"contract_version": PROJECTION_VERSION, "event_uuid": row["event_uuid"],
                    "receive_seq": row["receive_seq"], "raw_evidence_digest": row["raw_digest"],
                    "observed_at": row["received_at"], "status": "projected", "reason_code": None,
                    "observations": []}
                try:
                    observations = callback_observations(raw, scope, event_uuid=row["event_uuid"],
                        receive_seq=row["receive_seq"], max_entries=self.max_entries)
                    payload["observations"] = [asdict(o) for o in observations]
                except ValueError as exc:
                    payload["status"] = "quarantined"
                    code = str(exc)
                    payload["reason_code"] = code if code.startswith("HOOK_") and code.replace("_", "").isalnum() else "HOOK_PROJECTION_INVALID"
                encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
                used = conn.execute("SELECT COALESCE(SUM(length(CAST(projection_json AS BLOB))),0) FROM hook_observation_projection").fetchone()[0]
                if used + len(encoded.encode("utf-8")) > self.max_projection_bytes:
                    raise RuntimeError("HOOK_PROJECTION_CAPACITY_EXCEEDED")
                conn.execute("INSERT INTO hook_observation_projection VALUES (?,?,?,?,?)",
                    (row["receive_seq"], PROJECTION_VERSION, hashlib.sha256(encoded.encode("utf-8")).hexdigest(), encoded, utc_now_iso()))
                conn.commit()
                return payload
            except Exception:
                conn.rollback()
                raise
