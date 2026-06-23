from __future__ import annotations

import importlib
import os
import tempfile
import unittest
from datetime import datetime


class StorageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["CHEJIN_WORKER_HOME"] = self.tmp.name
        import chejin_worker_client.config as config
        import chejin_worker_client.storage as storage

        importlib.reload(config)
        self.storage = importlib.reload(storage)

    def tearDown(self):
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


if __name__ == "__main__":
    unittest.main()
