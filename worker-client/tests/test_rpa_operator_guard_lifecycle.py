from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
OMNIAUTO_ROOT = ROOT / "omniauto-rpa"
if str(OMNIAUTO_ROOT) not in sys.path:
    sys.path.insert(0, str(OMNIAUTO_ROOT))

from apps.wechat_ai_customer_service.adapters import rpa_operator_guard


class RpaOperatorGuardLifecycleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.paths = {
            "root": root,
            "control_path": root / "operator_control.json",
            "status_path": root / "runtime_status.json",
            "state_path": root / "operator_guard.state.json",
            "pid_path": root / "operator_guard.pid.json",
            "stdout_log_path": root / "operator_guard.stdout.log",
            "stderr_log_path": root / "operator_guard.stderr.log",
        }
        rpa_operator_guard._ACTIVE_GUARD = None

    def tearDown(self) -> None:
        rpa_operator_guard._ACTIVE_GUARD = None
        self.tmp.cleanup()

    def _write(self, key: str, payload: dict) -> None:
        path = self.paths[key]
        path.write_text(json.dumps(payload), encoding="utf-8")

    def test_nested_add_friend_process_reuses_ui_lock_owned_guard(self) -> None:
        self._write("pid_path", {"pid": 4321, "parent_pid": 1234})
        self._write("control_path", {"mode": "running"})
        self._write(
            "state_path",
            {"pid": 4321, "parent_pid": 1234, "phase": "running", "hooks_installed": True, "lock_enabled": True},
        )
        with (
            mock.patch.object(rpa_operator_guard.os, "name", "nt"),
            mock.patch.object(rpa_operator_guard, "rpa_operator_guard_paths", return_value=self.paths),
            mock.patch.object(rpa_operator_guard, "pid_alive", return_value=True),
            mock.patch.object(
                rpa_operator_guard,
                "verify_rpa_operator_guard",
                return_value={"ok": True, "state_pid": 4321, "state": {"hooks_installed": True}},
            ),
            mock.patch.object(rpa_operator_guard.subprocess, "Popen") as popen,
        ):
            result = rpa_operator_guard.start_rpa_operator_guard(operation="add_friend")

        self.assertTrue(result["ok"])
        self.assertTrue(result["reused_existing"])
        self.assertEqual(result["pid"], 4321)
        self.assertEqual(result["owner_parent_pid"], 1234)
        popen.assert_not_called()

    def test_f8_resume_forces_fresh_target_validation(self) -> None:
        rpa_operator_guard._ACTIVE_GUARD = {
            "enabled": True,
            "tenant_id": "default",
            "paths": {key: str(value) for key, value in self.paths.items()},
            "settings": {"pause_max_seconds": 30},
        }
        with (
            mock.patch.object(
                rpa_operator_guard,
                "read_json",
                side_effect=({"mode": "paused"}, {"mode": "running"}),
            ),
            mock.patch.object(rpa_operator_guard.time, "sleep"),
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "rpa_operator_guard_revalidation_required",
            ):
                rpa_operator_guard.rpa_operator_guard_checkpoint(reason="before_click")

    def test_health_reports_early_guard_exit(self) -> None:
        guard = {
            "enabled": True,
            "pid": 4321,
            "paths": {key: str(value) for key, value in self.paths.items()},
            "verify": {},
        }
        self._write("control_path", {"mode": "running"})
        self._write("state_path", {"pid": 4321, "phase": "running", "hooks_installed": True})
        self._write("pid_path", {"pid": 4321})
        with mock.patch.object(rpa_operator_guard, "pid_alive", return_value=False):
            health = rpa_operator_guard.rpa_operator_guard_health(guard)

        self.assertFalse(health["ok"])
        self.assertEqual(health["reason"], "guard_process_exited_early")


if __name__ == "__main__":
    unittest.main()
