from __future__ import annotations

import importlib
import unittest
from unittest import mock


class WorkerOperatorGuardServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        import chejin_worker_client.ui_operator_guard as service

        self.service = importlib.reload(service)

    def tearDown(self) -> None:
        self.service._WORKER_GUARD = None

    def test_worker_starts_one_idle_process_and_ui_locks_only_transition_it(self) -> None:
        adapter = mock.Mock()
        adapter.start_rpa_operator_guard.return_value = {
            "ok": True,
            "enabled": True,
            "started": True,
            "pid": 3210,
            "guard_instance_id": "guard-1",
            "paths": {"root": "/guard"},
        }
        adapter.rpa_operator_guard_health.side_effect = (
            {"ok": True, "mode": "ready", "state": {"mode": "ready"}},
            {"ok": True, "mode": "active", "state": {"mode": "active", "active_ui_lock_id": "lock-1", "active_fencing_token": 8}},
        )
        adapter.transition_rpa_operator_guard.side_effect = (
            {"ok": True, "mode": "ready", "control_epoch": 1},
            {"ok": True, "mode": "active", "control_epoch": 2, "active_ui_lock_id": "lock-1", "active_fencing_token": 8},
            {"ok": True, "mode": "ready", "control_epoch": 3},
        )

        with (
            mock.patch.object(self.service.os, "name", "nt"),
            mock.patch.object(self.service, "CONFIG", mock.Mock(rpa_mode="sidecar")),
            mock.patch.object(self.service, "_guard_adapter", return_value=adapter),
        ):
            started = self.service.start_worker_ui_operator_guard(client_instance_id="client-1")
            ready = self.service.set_worker_ui_operator_guard_mode("ready", reason="start_accepting")
            active = self.service.activate_worker_ui_operator_guard(
                lock_id="lock-1",
                fencing_token=8,
                operation_type="message_ingest",
                current_step="reading",
            )
            released = self.service.deactivate_worker_ui_operator_guard(
                lock_id="lock-1",
                fencing_token=8,
                reason="finished",
            )

        self.assertTrue(started["ok"])
        self.assertTrue(ready["ok"])
        self.assertTrue(active["ok"])
        self.assertTrue(released["ok"])
        adapter.start_rpa_operator_guard.assert_called_once_with(
            operation="worker_lifecycle",
            initial_mode="idle",
            client_instance_id="client-1",
        )
        adapter.stop_rpa_operator_guard.assert_not_called()

    def test_paused_guard_rejects_new_ui_lock(self) -> None:
        adapter = mock.Mock()
        adapter.rpa_operator_guard_health.return_value = {
            "ok": True,
            "mode": "paused",
            "state": {"mode": "paused", "lock_enabled": False},
        }
        self.service._WORKER_GUARD = {
            "ok": True,
            "enabled": True,
            "guard_instance_id": "guard-1",
        }
        with mock.patch.object(self.service, "_guard_adapter", return_value=adapter):
            result = self.service.activate_worker_ui_operator_guard(
                lock_id="lock-2",
                fencing_token=9,
                operation_type="session_scan",
                current_step="scan",
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "operator_guard_not_ready_for_activation")
        adapter.transition_rpa_operator_guard.assert_not_called()

    def test_worker_exit_is_the_only_normal_shutdown_boundary(self) -> None:
        adapter = mock.Mock()
        adapter.stop_rpa_operator_guard.return_value = {"ok": True, "pid": 3210}
        self.service._WORKER_GUARD = {
            "ok": True,
            "enabled": True,
            "pid": 3210,
            "guard_instance_id": "guard-1",
        }
        with mock.patch.object(self.service, "_guard_adapter", return_value=adapter):
            result = self.service.shutdown_worker_ui_operator_guard(reason="worker_exiting")

        self.assertTrue(result["ok"])
        adapter.stop_rpa_operator_guard.assert_called_once()
        self.assertIsNone(self.service._WORKER_GUARD)


if __name__ == "__main__":
    unittest.main()
