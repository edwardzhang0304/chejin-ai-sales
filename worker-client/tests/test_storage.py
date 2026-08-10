from __future__ import annotations

import importlib
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone


class StorageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.previous_home = os.environ.get("CHEJIN_WORKER_HOME")
        os.environ["CHEJIN_WORKER_HOME"] = self.tmp.name
        import chejin_worker_client.config as config
        import chejin_worker_client.storage as storage

        importlib.reload(config)
        self.storage = importlib.reload(storage)

    def tearDown(self):
        import chejin_worker_client.incident_evidence as incident_evidence

        incident_evidence.stop_incident_worker(wait=True)
        if self.previous_home is None:
            os.environ.pop("CHEJIN_WORKER_HOME", None)
        else:
            os.environ["CHEJIN_WORKER_HOME"] = self.previous_home
        self.tmp.cleanup()

    def test_binding_and_logs_are_persisted_in_sqlite(self):
        from chejin_worker_client.models import Binding

        binding = Binding(worker_id="worker-1", worker_token="token", client_instance_id="client-1", run_status="paused")
        self.storage.save_binding(binding)
        loaded = self.storage.load_binding()

        self.assertEqual(loaded.worker_id, "worker-1")
        self.assertTrue(self.storage.DB_FILE.exists())

        self.storage.append_log("INFO", "client_started", "客户端启动")
        self.storage.append_log("ERROR", "task_failed", "任务失败", task_id="task-1", error_code="PHONE_NOT_FOUND")
        logs = self.storage.read_logs()

        self.assertEqual(logs[0]["event"], "task_failed")
        self.assertEqual(logs[0]["error_code"], "PHONE_NOT_FOUND")
        self.assertEqual(logs[1]["event"], "client_started")

        snapshot = self.storage.export_debug_snapshot()
        self.assertEqual(snapshot["binding"]["worker_id"], "worker-1")
        self.assertEqual(len(snapshot["recent_logs"]), 2)

    def test_accept_schedule_is_persisted_and_checks_cross_day_window(self):
        default_schedule = self.storage.load_accept_schedule()
        self.assertFalse(default_schedule["enabled"])
        self.assertTrue(self.storage.is_accept_schedule_active(default_schedule, datetime(2026, 6, 19, 1, 0)))

        saved = self.storage.save_accept_schedule(enabled=True, start="22:30", end="06:15")
        self.assertEqual(saved, {"enabled": True, "start": "22:30", "end": "06:15"})
        self.assertEqual(self.storage.load_accept_schedule(), saved)
        self.assertTrue(self.storage.is_accept_schedule_active(saved, datetime(2026, 6, 19, 23, 0)))
        self.assertTrue(self.storage.is_accept_schedule_active(saved, datetime(2026, 6, 19, 5, 0)))
        self.assertFalse(self.storage.is_accept_schedule_active(saved, datetime(2026, 6, 19, 12, 0)))

        sanitized = self.storage.save_accept_schedule(enabled=True, start="99:99", end="bad")
        self.assertEqual(sanitized["start"], "09:00")
        self.assertEqual(sanitized["end"], "21:00")

    def test_action_journal_survives_until_common_flow_finalize(self):
        outcome = {
            "source_message_key": "voice-action-1",
            "result": "completed",
            "evidence": {"action_kind": "voice"},
            "terminal_payload": {"content": "已完成"},
        }
        self.storage.checkpoint_c2_action_outcomes(
            flow_id="flow-action-1",
            conversation_id="conv-action-1",
            origin_read_run_id="read-action-1",
            outcomes=[outcome],
        )

        pending = self.storage.list_c2_action_journal("conv-action-1")
        self.assertEqual(len(pending), 1)
        self.assertEqual(
            pending[0]["outcome"],
            {**outcome, "origin_read_run_id": "read-action-1"},
        )
        with self.assertRaisesRegex(
            ValueError,
            "C2_ACTION_JOURNAL_ORIGIN_READ_RUN_ID_CONFLICT",
        ):
            self.storage.checkpoint_c2_action_outcomes(
                flow_id="flow-action-1",
                conversation_id="conv-action-1",
                origin_read_run_id="read-action-2",
                outcomes=[outcome],
            )

        self.storage.clear_c2_action_journal("flow-action-1")
        self.assertEqual(
            self.storage.list_c2_action_journal("conv-action-1"),
            [],
        )

    def test_ledger_origin_read_run_is_required_and_immutable(self):
        self.storage.save_c2_ledger_terminal(
            conversation_id="conv-origin",
            source_message_key="source-origin",
            origin_read_run_id="read-origin-1",
            dedupe_key=None,
            message_type="image",
            terminal_state="completed",
            ingest_state="waiting",
            result={},
        )
        stored = self.storage.load_c2_ledger_entry(
            "conv-origin",
            "source-origin",
        )
        self.assertEqual(stored["origin_read_run_id"], "read-origin-1")
        with self.assertRaisesRegex(
            ValueError,
            "C2_LEDGER_ORIGIN_READ_RUN_ID_CONFLICT",
        ):
            self.storage.save_c2_ledger_terminal(
                conversation_id="conv-origin",
                source_message_key="source-origin",
                origin_read_run_id="read-origin-2",
                dedupe_key=None,
                message_type="image",
                terminal_state="completed",
                ingest_state="waiting",
                result={},
            )

    def test_outbox_source_origin_is_the_original_read_run(self):
        payload = {
            "conversation_id": "conv-outbox-origin",
            "authorization_revision": "revision-outbox-origin",
            "read_run_id": "read-current-outbox-envelope",
            "messages": [{"source_message_key": "source-outbox-origin"}],
            "evidence": {
                "slot_ledger_states": [
                    {
                        "source_message_key": "source-outbox-origin",
                        "origin_read_run_id": "read-outbox-origin",
                    }
                ]
            },
        }
        self.storage.enqueue_c2_outbox(payload)
        self.assertEqual(
            self.storage.load_c2_outbox_origin_read_run_ids(
                "conv-outbox-origin"
            ),
            {"source-outbox-origin": "read-outbox-origin"},
        )

    def test_outbox_storage_rejects_states_outside_machine_contract(self):
        payload = {
            "conversation_id": "conv-state-contract",
            "authorization_revision": "revision-state-contract",
            "read_run_id": "read-state-contract",
            "messages": [],
        }
        outbox_id = self.storage.enqueue_c2_outbox(payload)

        with self.assertRaisesRegex(
            ValueError,
            "C2_OUTBOX_TARGET_STATE_INVALID",
        ):
            self.storage.transition_c2_outbox(
                outbox_id,
                status="abandoned",
            )

    def test_unreleased_image_outbox_migration_is_not_product_code(self):
        self.assertFalse(
            hasattr(
                self.storage,
                "_migrate_retired_flow_gate_outboxes",
            )
        )
        self.assertFalse(
            hasattr(
                self.storage,
                "_retired_flow_gate_codes",
            )
        )
        self.assertNotIn(
            "retired_migrated",
            self.storage._c2_outbox_states(),
        )

    def test_capability_paused_outbox_payload_is_immutable(self):
        original = {
            "conversation_id": "conv-quarantine-freeze",
            "authorization_revision": "revision-freeze",
            "read_run_id": "read-freeze",
            "messages": [{"content": "original"}],
        }
        outbox_id = self.storage.enqueue_c2_outbox(original)
        self.storage.mark_c2_outbox_capability_paused(
            outbox_id,
            "VALIDATION_ERROR",
        )

        replacement = {
            **original,
            "messages": [{"content": "replacement"}],
        }
        self.storage.enqueue_c2_outbox(replacement)
        stored = self.storage.load_c2_outbox_entry(outbox_id)

        self.assertEqual(stored["status"], "capability_paused")
        self.assertEqual(
            stored["payload"]["messages"][0]["content"],
            "original",
        )

    def test_legacy_payload_terminated_outbox_is_restored_to_safe_pause(self):
        with self.storage.db_connection() as conn:
            conn.execute(
                """
                INSERT INTO c2_ingest_outbox (
                  outbox_id, conversation_id, authorization_revision,
                  read_run_id, payload_json, status, attempt_count,
                  created_at, updated_at
                ) VALUES (
                  'legacy-payload-terminal', 'conv-legacy',
                  'revision-legacy', 'read-legacy', '{}',
                  'payload_terminated', 0, ?, ?
                )
                """,
                (
                    datetime.now(timezone.utc).isoformat(),
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            conn.commit()
            self.storage.init_db(conn)
            row = conn.execute(
                """
                SELECT status
                FROM c2_ingest_outbox
                WHERE outbox_id = 'legacy-payload-terminal'
                """
            ).fetchone()

        self.assertEqual(row["status"], "capability_paused")

    def test_outbox_cleanup_removes_only_old_terminal_rows(self):
        old_at = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
        now_at = datetime.now(timezone.utc).isoformat()
        with self.storage.db_connection() as conn:
            conn.executemany(
                """
                INSERT INTO c2_ingest_outbox (
                  outbox_id, conversation_id, authorization_revision, read_run_id,
                  payload_json, status, attempt_count, created_at, updated_at
                ) VALUES (?, 'conv-1', 'revision-1', ?, '{}', ?, 0, ?, ?)
                """,
                [
                    ("c2-old-confirmed", "read-old-confirmed", "confirmed", old_at, old_at),
                    ("c2-old-split", "read-old-split", "split_completed", old_at, old_at),
                    ("c2-old-target-terminal", "read-old-target-terminal", "target_terminated", old_at, old_at),
                    ("c2-old-conversation-terminal", "read-old-conversation-terminal", "conversation_terminated", old_at, old_at),
                    ("c2-old-quarantined", "read-old-quarantined", "quarantined", old_at, old_at),
                    ("c2-old-waiting", "read-old-waiting", "waiting", old_at, old_at),
                    ("c2-recent-confirmed", "read-recent-confirmed", "confirmed", now_at, now_at),
                    ("c2-recent-target-terminal", "read-recent-target-terminal", "target_terminated", now_at, now_at),
                ],
            )
            conn.executemany(
                """
                INSERT INTO reply_send_ack_outbox (
                  reply_action_id, task_id, send_token, status, ack_payload_json,
                  attempt_count, created_at, updated_at
                ) VALUES (?, 'task-1', 'token-1', ?, '{}', 0, ?, ?)
                """,
                [
                    ("reply-old-abandoned", "abandoned", old_at, old_at),
                    ("reply-old-intent", "intent", old_at, old_at),
                    ("reply-recent-confirmed", "confirmed", now_at, now_at),
                ],
            )
            conn.commit()

        result = self.storage.prune_terminal_outboxes(
            retention_days=30,
            max_terminal_rows=5000,
        )

        self.assertEqual(result["c2_ingest_outbox"], 4)
        self.assertEqual(result["reply_send_ack_outbox"], 0)
        with self.storage.db_connection() as conn:
            c2_rows = {
                row["outbox_id"]: row["status"]
                for row in conn.execute(
                    "SELECT outbox_id, status FROM c2_ingest_outbox"
                ).fetchall()
            }
            reply_rows = {
                row["reply_action_id"]: row["status"]
                for row in conn.execute(
                    "SELECT reply_action_id, status FROM reply_send_ack_outbox"
                ).fetchall()
            }
        self.assertEqual(
            c2_rows,
            {
                "c2-old-quarantined": "capability_paused",
                "c2-old-waiting": "waiting",
                "c2-recent-confirmed": "confirmed",
                "c2-recent-target-terminal": "target_terminated",
            },
        )
        self.assertEqual(
            reply_rows,
            {
                "reply-old-abandoned": "waiting",
                "reply-old-intent": "intent",
                "reply-recent-confirmed": "confirmed",
            },
        )

    def test_legacy_quarantine_and_abandoned_rows_migrate_without_data_loss(self):
        now_at = datetime.now(timezone.utc).isoformat()
        with self.storage.db_connection() as conn:
            conn.execute(
                """
                INSERT INTO c2_ingest_outbox (
                  outbox_id, conversation_id, authorization_revision, read_run_id,
                  payload_json, status, attempt_count, refresh_attempt_count,
                  last_error, created_at, updated_at
                ) VALUES (
                  'legacy-c2', 'conv-legacy', 'revision-legacy', 'read-legacy',
                  '{"messages":[{"content":"original"}]}', 'quarantined',
                  27, 9, 'VALIDATION_ERROR', ?, ?
                )
                """,
                (now_at, now_at),
            )
            conn.execute(
                """
                INSERT INTO reply_send_ack_outbox (
                  reply_action_id, task_id, send_token, status, action_phase,
                  ack_payload_json, attempt_count, last_error, created_at, updated_at
                ) VALUES (
                  'legacy-ack', 'task-legacy', 'token-legacy', 'abandoned',
                  'trigger_attempted',
                  '{"send_result":"unknown","action_phase":"trigger_attempted"}',
                  31, 'NETWORK_ERROR', ?, ?
                )
                """,
                (now_at, now_at),
            )
            conn.commit()

        c2_row = self.storage.load_c2_outbox_entry("legacy-c2")
        ack_row = self.storage.load_reply_send_ack_outbox("legacy-ack")
        self.assertEqual(c2_row["status"], "capability_paused")
        self.assertEqual(c2_row["attempt_count"], 27)
        self.assertEqual(c2_row["refresh_attempt_count"], 9)
        self.assertEqual(c2_row["last_error"], "VALIDATION_ERROR")
        self.assertEqual(
            c2_row["payload"]["messages"][0]["content"],
            "original",
        )
        self.assertEqual(ack_row["status"], "waiting")
        self.assertEqual(ack_row["attempt_count"], 31)
        self.assertEqual(ack_row["last_error"], "NETWORK_ERROR")
        self.assertEqual(ack_row["ack_payload"]["send_result"], "unknown")

    def test_retry_backoff_never_discards_c2_facts_after_twenty_attempts(self):
        payload = {
            "conversation_id": "conv-retry-forever",
            "authorization_revision": "revision-retry-forever",
            "read_run_id": "read-retry-forever",
            "messages": [{"content": "must survive"}],
        }
        outbox_id = self.storage.enqueue_c2_outbox(payload)

        for attempt in range(25):
            self.storage.mark_c2_outbox_attempt(
                outbox_id,
                f"NETWORK_ERROR_{attempt}",
            )
            self.storage.transition_c2_outbox(
                outbox_id,
                status="retry_waiting",
                error=f"NETWORK_ERROR_{attempt}",
            )

        stored = self.storage.load_c2_outbox_entry(outbox_id)
        self.assertEqual(stored["status"], "retry_waiting")
        self.assertEqual(stored["attempt_count"], 25)
        self.assertEqual(
            stored["payload"]["messages"][0]["content"],
            "must survive",
        )
        self.assertIsNotNone(stored["next_attempt_at"])
        self.assertTrue(self.storage.has_pending_c2_outbox())


if __name__ == "__main__":
    unittest.main()
