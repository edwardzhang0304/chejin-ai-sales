from __future__ import annotations

import importlib
import os
import tempfile
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

    def test_every_ui_lock_operation_starts_and_stops_the_shared_guard(self):
        operations = (
            "add_friend",
            "session_scan",
            "message_ingest",
            "c3_send",
            "voice_transcribe",
            "image_copy",
        )
        with (
            mock.patch(
                "chejin_worker_client.ui_operator_guard.start_ui_operator_guard",
                side_effect=lambda **kwargs: {
                    "ok": True,
                    "enabled": True,
                    "started": True,
                    "pid": 4312,
                    **kwargs,
                },
            ) as start_guard,
            mock.patch(
                "chejin_worker_client.ui_operator_guard.stop_ui_operator_guard",
                return_value={"ok": True, "process_alive_after_stop": False},
            ) as stop_guard,
        ):
            for operation in operations:
                lease = self.ui_lock.acquire_ui_lock(
                    operation_type=operation,
                    owner=f"owner:{operation}",
                    current_step="starting",
                    timeout_seconds=1,
                )
                self.assertEqual(lease.operator_guard_pid, 4312)
                lease.release()

        self.assertEqual(
            [call.kwargs["operation_type"] for call in start_guard.call_args_list],
            list(operations),
        )
        self.assertEqual(stop_guard.call_count, len(operations))

    def test_guard_start_failure_removes_lock_and_blocks_ui_action(self):
        with mock.patch(
            "chejin_worker_client.ui_operator_guard.start_ui_operator_guard",
            return_value={
                "ok": False,
                "enabled": True,
                "started": False,
                "reason": "guard_process_exited_early",
            },
        ):
            with self.assertRaises(self.ui_lock.UiLockError) as raised:
                self.ui_lock.acquire_ui_lock(
                    operation_type="session_scan",
                    owner="owner-a",
                    current_step="scan",
                    timeout_seconds=1,
                )

        self.assertEqual(raised.exception.code, self.ui_lock.OPERATOR_GUARD_NOT_READY)
        self.assertFalse(self.ui_lock.lock_summary()["locked"])

    def test_guard_unexpected_exit_cancels_current_lease(self):
        with (
            mock.patch(
                "chejin_worker_client.ui_operator_guard.start_ui_operator_guard",
                return_value={"ok": True, "enabled": True, "started": True, "pid": 4312},
            ),
            mock.patch(
                "chejin_worker_client.ui_operator_guard.ui_operator_guard_health",
                return_value={"ok": False, "reason": "guard_process_exited_early"},
            ),
            mock.patch(
                "chejin_worker_client.ui_operator_guard.stop_ui_operator_guard",
                return_value={"ok": True},
            ),
        ):
            lease = self.ui_lock.acquire_ui_lock(
                operation_type="message_ingest",
                owner="owner-a",
                current_step="read",
                timeout_seconds=1,
            )
            self.assertTrue(lease.cancel_requested())
            self.assertEqual(lease.lease_error.code, self.ui_lock.OPERATOR_GUARD_EXITED)
            lease.release()

    def test_resume_after_f8_requires_fresh_window_and_conversation_validation(self):
        health = mock.Mock(
            side_effect=(
                {"ok": True, "mode": "paused", "reason": "guard_ready"},
                {"ok": True, "mode": "running", "reason": "guard_ready"},
            )
        )
        with (
            mock.patch(
                "chejin_worker_client.ui_operator_guard.start_ui_operator_guard",
                return_value={"ok": True, "enabled": True, "started": True, "pid": 4312},
            ),
            mock.patch(
                "chejin_worker_client.ui_operator_guard.ui_operator_guard_health",
                health,
            ),
            mock.patch(
                "chejin_worker_client.ui_operator_guard.stop_ui_operator_guard",
                return_value={"ok": True},
            ),
        ):
            lease = self.ui_lock.acquire_ui_lock(
                operation_type="c3_send",
                owner="owner-a",
                current_step="brain_waiting",
                timeout_seconds=1,
            )
            self.assertFalse(lease.cancel_requested())
            self.assertTrue(lease.cancel_requested())
            self.assertEqual(
                lease.lease_error.code,
                self.ui_lock.OPERATOR_GUARD_REVALIDATION_REQUIRED,
            )
            lease.release()


if __name__ == "__main__":
    unittest.main()
