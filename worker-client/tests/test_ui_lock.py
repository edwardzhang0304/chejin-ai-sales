from __future__ import annotations

import importlib
import json
import os
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock


class UiLockTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["CHEJIN_WORKER_HOME"] = self.tmp.name
        os.environ["CHEJIN_UI_LOCK_LEASE_SECONDS"] = "30"
        import chejin_worker_client.config as config
        import chejin_worker_client.ui_lock as ui_lock

        importlib.reload(config)
        self.ui_lock = importlib.reload(ui_lock)

    def tearDown(self):
        self.tmp.cleanup()

    def test_acquire_renew_release_and_summary(self):
        lease = self.ui_lock.acquire_ui_lock(operation_type="add_friend", owner="worker:client:task", current_step="start", timeout_seconds=1)

        summary = self.ui_lock.lock_summary()
        self.assertTrue(summary["locked"])
        self.assertEqual(summary["operation_type"], "add_friend")
        self.assertEqual(summary["current_step"], "start")

        lease.update_step("invite_sent")
        self.assertEqual(self.ui_lock.lock_summary()["current_step"], "invite_sent")

        lease.release()
        self.assertFalse(self.ui_lock.lock_summary()["locked"])

    def test_busy_lock_times_out_until_stale_recovered(self):
        first = self.ui_lock.acquire_ui_lock(operation_type="session_scan", owner="owner-a", current_step="scan", timeout_seconds=1)
        with self.assertRaises(self.ui_lock.UiLockError) as raised:
            self.ui_lock.acquire_ui_lock(operation_type="message_ingest", owner="owner-b", current_step="read", timeout_seconds=0.1)
        self.assertEqual(raised.exception.code, self.ui_lock.UI_LOCK_BUSY)

        payload = self.ui_lock._read_lock()
        payload["lease_expires_at"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        self.ui_lock._write_lock(payload)
        recovered = self.ui_lock.force_recover_stale_lock(reason="unit")
        self.assertTrue(recovered["recovered"])

        second = self.ui_lock.acquire_ui_lock(operation_type="message_ingest", owner="owner-b", current_step="read", timeout_seconds=1)
        second.release()
        first.lock_id = "released-by-recovery"

    def test_auto_renew_failure_is_visible_to_cancel_checks_and_steps(self):
        lease = self.ui_lock.acquire_ui_lock(
            operation_type="message_ingest",
            owner="owner-a",
            current_step="read",
            timeout_seconds=1,
        )
        lease._renew_stop = mock.Mock()
        lease._renew_stop.wait.return_value = False
        renew_error = self.ui_lock.UiLockError(
            self.ui_lock.UI_LOCK_RENEW_FAILED,
            "unit renewal failure",
        )

        with mock.patch.object(lease, "renew", side_effect=renew_error):
            lease._renew_loop(0.01)

        self.assertTrue(lease.lease_lost)
        self.assertTrue(lease.cancel_requested())
        self.assertIs(lease.lease_error, renew_error)
        with self.assertRaises(self.ui_lock.UiLockError) as raised:
            lease.update_step("should_not_continue")
        self.assertEqual(raised.exception.code, self.ui_lock.UI_LOCK_RENEW_FAILED)

    def test_lock_summary_retries_transient_windows_permission_error(self):
        lease = self.ui_lock.acquire_ui_lock(
            operation_type="message_ingest",
            owner="owner-a",
            current_step="voice_transcribe_current_chat",
            timeout_seconds=1,
        )
        real_read = self.ui_lock._read_lock
        attempts = {"count": 0}

        def transient_read(path=self.ui_lock.LOCK_FILE):
            attempts["count"] += 1
            if attempts["count"] < 3:
                raise PermissionError(13, "sharing violation", str(path))
            return real_read(path)

        with mock.patch.object(
            self.ui_lock,
            "_read_lock",
            side_effect=transient_read,
        ):
            summary = self.ui_lock.lock_summary()

        self.assertEqual(attempts["count"], 3)
        self.assertTrue(summary["locked"])
        self.assertEqual(
            summary["current_step"],
            "voice_transcribe_current_chat",
        )
        lease.release()

    def test_lock_summary_persistently_unreadable_fails_closed(self):
        with mock.patch.object(
            self.ui_lock,
            "_read_lock",
            side_effect=PermissionError(13, "sharing violation"),
        ):
            summary = self.ui_lock.lock_summary()

        self.assertTrue(summary["locked"])
        self.assertFalse(summary["expired"])
        self.assertEqual(summary["state"], "unknown")
        self.assertEqual(
            summary["error_code"],
            self.ui_lock.UI_LOCK_STATE_UNAVAILABLE,
        )
        self.assertEqual(summary["read_error"], "PermissionError")

    def test_corrupted_lock_is_never_deleted_or_treated_as_unlocked(self):
        self.ui_lock.LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
        self.ui_lock.LOCK_FILE.write_text("{", encoding="utf-8")

        summary = self.ui_lock.lock_summary()
        recovered = self.ui_lock.force_recover_stale_lock(reason="unit")

        self.assertTrue(summary["locked"])
        self.assertEqual(summary["state"], "unknown")
        self.assertFalse(recovered["recovered"])
        self.assertEqual(recovered["reason"], "lock_state_unavailable")
        self.assertTrue(self.ui_lock.LOCK_FILE.exists())
        with self.assertRaises(self.ui_lock.UiLockError) as raised:
            self.ui_lock.acquire_ui_lock(
                operation_type="message_ingest",
                owner="owner-b",
                current_step="read",
                timeout_seconds=0.1,
            )
        self.assertEqual(raised.exception.code, self.ui_lock.UI_LOCK_BUSY)

    def test_summary_and_renew_share_one_process_mutex(self):
        lease = self.ui_lock.acquire_ui_lock(
            operation_type="message_ingest",
            owner="owner-a",
            current_step="read",
            timeout_seconds=1,
        )
        errors: list[BaseException] = []

        def renew_repeatedly() -> None:
            try:
                for index in range(100):
                    lease.current_step = f"step-{index}"
                    lease.renew()
            except BaseException as exc:  # pragma: no cover - asserted below
                errors.append(exc)

        thread = threading.Thread(target=renew_repeatedly)
        thread.start()
        for _ in range(100):
            summary = self.ui_lock.lock_summary()
            self.assertTrue(summary["locked"])
        thread.join(timeout=5)

        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        payload = json.loads(
            self.ui_lock.LOCK_FILE.read_text(encoding="utf-8")
        )
        self.assertEqual(payload["lock_id"], lease.lock_id)
        lease.release()

if __name__ == "__main__":
    unittest.main()
