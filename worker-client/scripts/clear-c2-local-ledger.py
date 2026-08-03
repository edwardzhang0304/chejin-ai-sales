from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import sys


C2_LEDGER_TABLES = (
    "c2_action_journal",
    "c2_ingest_outbox",
    "c2_message_ledger",
)
MEDIA_ACTION_KINDS = ("image", "voice")


def _app_dir() -> Path:
    configured = str(os.environ.get("CHEJIN_WORKER_HOME") or "").strip()
    if configured:
        return Path(configured)
    local_app_data = str(os.environ.get("LOCALAPPDATA") or "").strip()
    if local_app_data:
        return Path(local_app_data) / "CheJinWorker"
    return Path.home() / ".chejin-worker"


def _existing_tables(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }


def clear_c2_local_ledger(app_dir: Path) -> dict[str, object]:
    database_path = app_dir / "worker_client.sqlite3"
    if not database_path.is_file():
        return {
            "ok": True,
            "database_found": False,
            "database_path": str(database_path),
            "binding_preserved": True,
            "deleted_rows": {},
            "deleted_media_action_files": 0,
        }

    connection = sqlite3.connect(database_path, timeout=10)
    try:
        tables = _existing_tables(connection)
        binding_row = None
        if "binding" in tables:
            binding_row = connection.execute(
                "SELECT worker_id, run_status FROM binding WHERE id = 1"
            ).fetchone()
        if binding_row and str(binding_row[1] or "").strip() != "paused":
            raise RuntimeError("WORKER_MUST_BE_PAUSED_BEFORE_LEDGER_CLEAR")

        deleted_rows: dict[str, int] = {}
        connection.execute("BEGIN IMMEDIATE")
        for table in C2_LEDGER_TABLES:
            if table not in tables:
                continue
            before = int(
                connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            )
            connection.execute(f"DELETE FROM {table}")
            deleted_rows[table] = before
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()

    deleted_files = 0
    action_root = app_dir / "transactions" / "actions"
    for action_kind in MEDIA_ACTION_KINDS:
        action_dir = action_root / action_kind
        if not action_dir.is_dir():
            continue
        for pattern in ("*.json", "*.json.tmp-*"):
            for path in action_dir.glob(pattern):
                if not path.is_file():
                    continue
                path.unlink()
                deleted_files += 1

    return {
        "ok": True,
        "database_found": True,
        "database_path": str(database_path),
        "binding_preserved": True,
        "worker_id": str(binding_row[0]) if binding_row else None,
        "deleted_rows": deleted_rows,
        "deleted_media_action_files": deleted_files,
        "preserved": [
            "binding",
            "client_settings",
            "local_logs",
            "c2_runtime_state",
            "reply_send_ack_outbox",
            "add_friend_action_journal",
            "send_action_journal",
        ],
    }


def main() -> int:
    try:
        result = clear_c2_local_ledger(_app_dir())
    except Exception as exc:
        print(
            json.dumps(
                {"ok": False, "error": str(exc)},
                ensure_ascii=False,
            )
        )
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
