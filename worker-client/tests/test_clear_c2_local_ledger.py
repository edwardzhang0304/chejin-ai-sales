from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "clear-c2-local-ledger.py"


class ClearC2LocalLedgerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.app_dir = Path(self.temporary.name)
        self.database = self.app_dir / "worker_client.sqlite3"
        with sqlite3.connect(self.database) as connection:
            connection.executescript(
                """
                CREATE TABLE binding (
                  id INTEGER PRIMARY KEY,
                  worker_id TEXT,
                  worker_token TEXT,
                  client_instance_id TEXT,
                  run_status TEXT
                );
                CREATE TABLE client_settings (key TEXT PRIMARY KEY, value TEXT);
                CREATE TABLE local_logs (id TEXT PRIMARY KEY, message TEXT);
                CREATE TABLE c2_runtime_state (key TEXT PRIMARY KEY, value TEXT);
                CREATE TABLE c2_message_ledger (id TEXT PRIMARY KEY);
                CREATE TABLE c2_ingest_outbox (id TEXT PRIMARY KEY);
                CREATE TABLE c2_action_journal (id TEXT PRIMARY KEY);
                CREATE TABLE reply_send_ack_outbox (id TEXT PRIMARY KEY);
                INSERT INTO binding VALUES (
                  1, 'worker-preserved', 'token-preserved',
                  'client-preserved', 'paused'
                );
                INSERT INTO client_settings VALUES ('schedule', '{}');
                INSERT INTO local_logs VALUES ('log-1', 'preserved');
                INSERT INTO c2_runtime_state VALUES (
                  'possible_ai_sends:conversation-1', '{}'
                );
                INSERT INTO c2_message_ledger VALUES ('ledger-1');
                INSERT INTO c2_ingest_outbox VALUES ('outbox-1');
                INSERT INTO c2_action_journal VALUES ('journal-1');
                INSERT INTO reply_send_ack_outbox VALUES ('ack-1');
                """
            )
        for action_kind in ("image", "voice", "send", "add_friend"):
            action_dir = (
                self.app_dir / "transactions" / "actions" / action_kind
            )
            action_dir.mkdir(parents=True, exist_ok=True)
            (action_dir / f"{action_kind}.json").write_text(
                "{}", encoding="utf-8"
            )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _run(self) -> subprocess.CompletedProcess[str]:
        environment = dict(os.environ)
        environment["CHEJIN_WORKER_HOME"] = str(self.app_dir)
        return subprocess.run(
            [sys.executable, str(SCRIPT)],
            cwd=ROOT,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_clear_preserves_binding_token_settings_and_send_safety(self) -> None:
        completed = self._run()
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout)
        self.assertTrue(result["binding_preserved"])

        with sqlite3.connect(self.database) as connection:
            binding = connection.execute(
                "SELECT worker_id, worker_token, client_instance_id FROM binding"
            ).fetchone()
            self.assertEqual(
                binding,
                ("worker-preserved", "token-preserved", "client-preserved"),
            )
            for table in (
                "c2_message_ledger",
                "c2_ingest_outbox",
                "c2_action_journal",
            ):
                self.assertEqual(
                    connection.execute(
                        f"SELECT COUNT(*) FROM {table}"
                    ).fetchone()[0],
                    0,
                )
            for table in (
                "client_settings",
                "local_logs",
                "c2_runtime_state",
                "reply_send_ack_outbox",
            ):
                self.assertEqual(
                    connection.execute(
                        f"SELECT COUNT(*) FROM {table}"
                    ).fetchone()[0],
                    1,
                )

        self.assertFalse(
            (
                self.app_dir
                / "transactions"
                / "actions"
                / "image"
                / "image.json"
            ).exists()
        )
        self.assertFalse(
            (
                self.app_dir
                / "transactions"
                / "actions"
                / "voice"
                / "voice.json"
            ).exists()
        )
        self.assertTrue(
            (
                self.app_dir
                / "transactions"
                / "actions"
                / "send"
                / "send.json"
            ).exists()
        )
        self.assertTrue(
            (
                self.app_dir
                / "transactions"
                / "actions"
                / "add_friend"
                / "add_friend.json"
            ).exists()
        )

    def test_running_worker_is_not_modified(self) -> None:
        with sqlite3.connect(self.database) as connection:
            connection.execute(
                "UPDATE binding SET run_status = 'running' WHERE id = 1"
            )
            connection.commit()

        completed = self._run()
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn(
            "WORKER_MUST_BE_PAUSED_BEFORE_LEDGER_CLEAR",
            completed.stdout,
        )
        with sqlite3.connect(self.database) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM c2_message_ledger"
                ).fetchone()[0],
                1,
            )


if __name__ == "__main__":
    unittest.main()
