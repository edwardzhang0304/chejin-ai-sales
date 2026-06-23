from __future__ import annotations

import importlib
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone


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


if __name__ == "__main__":
    unittest.main()
