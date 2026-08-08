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

from apps.wechat_ai_customer_service.adapters import (
    add_friend_operator_guard,
    rpa_operator_guard,
)
from apps.wechat_ai_customer_service.scripts import run_rpa_operator_guard


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
        self.paths[key].write_text(json.dumps(payload), encoding="utf-8")

    def test_launcher_retries_transient_state_read_before_declaring_identity_fault(self) -> None:
        state_path = self.paths["state_path"]
        valid_state = json.dumps({"pid": 4321, "guard_instance_id": "guard-1"})
        with (
            mock.patch.object(
                Path,
                "read_text",
                side_effect=(OSError("temporarily locked"), valid_state),
            ) as read_text,
            mock.patch.object(rpa_operator_guard.time, "sleep") as sleep,
        ):
            payload = rpa_operator_guard.read_json(state_path)

        self.assertEqual(payload["guard_instance_id"], "guard-1")
        self.assertEqual(read_text.call_count, 2)
        sleep.assert_called_once()

    def test_guard_state_writer_does_not_block_for_entire_heartbeat_budget(self) -> None:
        state_path = self.paths["state_path"]
        transient = PermissionError(13, "reader holds state file")
        with (
            mock.patch.object(
                run_rpa_operator_guard.os,
                "replace",
                side_effect=(transient, transient, None),
            ) as replace,
            mock.patch.object(run_rpa_operator_guard.time, "sleep") as sleep,
        ):
            run_rpa_operator_guard.write_json(state_path, {"mode": "idle"})

        self.assertEqual(replace.call_count, 3)
        self.assertEqual(sleep.call_count, 2)
        self.assertLess(sum(call.args[0] for call in sleep.call_args_list), 0.1)

    def test_add_friend_sidecar_only_attaches_and_never_spawns_or_stops_guard(self) -> None:
        with mock.patch.object(
            add_friend_operator_guard,
            "attach_rpa_operator_guard",
            return_value={"ok": True, "enabled": True, "pid": 4321},
        ) as attach:
            guard = add_friend_operator_guard.start_add_friend_operator_guard(
                route="add_friend",
                artifact_dir="artifact",
            )
            release = add_friend_operator_guard.stop_add_friend_operator_guard(
                guard,
                reason="finished",
            )

        attach.assert_called_once_with()
        self.assertTrue(guard["attached"])
        self.assertTrue(release["worker_owned_guard_kept_alive"])

    def test_transition_to_active_requires_exact_lock_identity_and_ack(self) -> None:
        guard = {
            "enabled": True,
            "pid": 4321,
            "guard_instance_id": "guard-1",
            "paths": {key: str(value) for key, value in self.paths.items()},
        }
        self._write(
            "control_path",
            {"guard_instance_id": "guard-1", "control_epoch": 4, "command": {}},
        )
        ready = {
            "ok": True,
            "mode": "ready",
            "state": {
                "guard_instance_id": "guard-1",
                "mode": "ready",
                "control_epoch": 4,
                "active_ui_lock_id": "",
                "active_fencing_token": 0,
                "hooks_installed": True,
                "lock_enabled": False,
            },
        }
        active = {
            "ok": True,
            "mode": "active",
            "state": {
                "guard_instance_id": "guard-1",
                "mode": "active",
                "control_epoch": 5,
                "active_ui_lock_id": "lock-1",
                "active_fencing_token": 7,
                "hooks_installed": True,
                "lock_enabled": True,
            },
        }
        with mock.patch.object(
            rpa_operator_guard,
            "rpa_operator_guard_health",
            side_effect=(ready, active),
        ):
            result = rpa_operator_guard.transition_rpa_operator_guard(
                guard,
                mode="active",
                ui_lock_id="lock-1",
                fencing_token=7,
                operation_type="message_ingest",
                current_step="reading",
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["control_epoch"], 5)
        self.assertEqual(result["active_ui_lock_id"], "lock-1")
        self.assertEqual(result["active_fencing_token"], 7)

    def test_transition_to_resident_stopped_never_requests_process_shutdown(self) -> None:
        guard = {
            "enabled": True,
            "pid": 4321,
            "guard_instance_id": "guard-1",
            "paths": {key: str(value) for key, value in self.paths.items()},
        }
        self._write(
            "control_path",
            {
                "guard_instance_id": "guard-1",
                "control_epoch": 2,
                "shutdown_requested": True,
                "command": {},
            },
        )
        ready = {
            "ok": True,
            "mode": "ready",
            "state": {
                "guard_instance_id": "guard-1",
                "mode": "ready",
                "control_epoch": 2,
                "active_ui_lock_id": "",
                "active_fencing_token": 0,
                "hooks_installed": True,
                "lock_enabled": False,
            },
        }
        stopped = {
            "ok": True,
            "mode": "stopped",
            "state": {
                "guard_instance_id": "guard-1",
                "mode": "stopped",
                "control_epoch": 3,
                "active_ui_lock_id": "",
                "active_fencing_token": 0,
                "hooks_installed": True,
                "lock_enabled": False,
            },
        }
        with mock.patch.object(
            rpa_operator_guard,
            "rpa_operator_guard_health",
            side_effect=(ready, stopped),
        ):
            result = rpa_operator_guard.transition_rpa_operator_guard(
                guard,
                mode="stopped",
                reason="guard_rebuilt_after_fault",
            )

        control = json.loads(self.paths["control_path"].read_text(encoding="utf-8"))
        self.assertTrue(result["ok"])
        self.assertFalse(control["shutdown_requested"])
        self.assertEqual(control["command"]["action"], "stop")

    def test_first_f8_press_unlocks_immediately_and_second_press_stops(self) -> None:
        hooks = object.__new__(run_rpa_operator_guard.InputHookGuard)
        hooks.lock_enabled = True
        hooks.pending_single = False
        hooks.pending_single_deadline = 0.0
        hooks.queued_action = ""
        hooks.control_double_window_seconds = 0.42

        with mock.patch.object(run_rpa_operator_guard.time, "monotonic", return_value=10.0):
            hooks._on_control_keydown()
        self.assertFalse(hooks.lock_enabled)
        self.assertEqual(hooks.poll_action(), "toggle_pause")

        hooks.lock_enabled = True
        with mock.patch.object(run_rpa_operator_guard.time, "monotonic", return_value=10.1):
            hooks._on_control_keydown()
        self.assertFalse(hooks.lock_enabled)
        self.assertEqual(hooks.poll_action(), "stop")

    def test_indicator_uses_v083_five_fixed_states(self) -> None:
        cases = {
            "idle": "gray",
            "ready": "green",
            "active": "blue",
            "paused": "yellow",
            "stopped": "red",
            "fault": "red",
        }
        for mode, expected_theme in cases.items():
            theme, _label, _palette = run_rpa_operator_guard.indicator_state_snapshot(
                mode=mode,
                runtime_state=mode,
                locked=mode == "active",
            )
            self.assertEqual(theme, expected_theme)

    def test_indicator_mode_is_authoritative_after_manual_restart(self) -> None:
        theme, label, _palette = run_rpa_operator_guard.indicator_state_snapshot(
            mode="ready",
            runtime_state="stopped",
            locked=False,
        )
        self.assertEqual(theme, "green")
        self.assertIn("接单中", label)

    def test_active_mode_without_lock_is_rendered_as_fault(self) -> None:
        theme, label, _palette = run_rpa_operator_guard.indicator_state_snapshot(
            mode="active",
            runtime_state="thinking",
            locked=False,
        )
        self.assertEqual(theme, "red")
        self.assertIn("守护故障", label)

    def test_checkpoint_cancels_immediately_after_f8_pause(self) -> None:
        rpa_operator_guard._ACTIVE_GUARD = {"enabled": True}
        with mock.patch.object(
            rpa_operator_guard,
            "rpa_operator_guard_health",
            return_value={"ok": True, "mode": "paused", "state": {"lock_enabled": False}},
        ):
            with self.assertRaisesRegex(RuntimeError, "rpa_operator_guard_paused"):
                rpa_operator_guard.rpa_operator_guard_checkpoint(reason="before_click")

    def test_identity_mismatch_is_fail_closed_and_never_kills_unrelated_pid(self) -> None:
        self._write("pid_path", {"pid": 4321})
        self._write("state_path", {"pid": 4321})
        with (
            mock.patch.object(rpa_operator_guard.os, "name", "nt"),
            mock.patch.object(rpa_operator_guard, "rpa_operator_guard_paths", return_value=self.paths),
            mock.patch.object(rpa_operator_guard, "pid_alive", return_value=True),
            mock.patch.object(rpa_operator_guard, "terminate_pid_tree") as terminate,
        ):
            result = rpa_operator_guard.start_rpa_operator_guard()

        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "operator_guard_identity_mismatch")
        terminate.assert_not_called()

    def test_guard_identity_requires_pid_times_parent_instance_and_command(self) -> None:
        record = {
            "pid": 4321,
            "guard_instance_id": "guard-1",
            "client_instance_id": "client-1",
            "guard_process_create_time": 100.0,
            "owner_worker_pid": 1234,
            "owner_process_create_time": 90.0,
        }
        state = {
            "pid": 4321,
            "guard_instance_id": "guard-1",
            "client_instance_id": "client-1",
            "owner_worker_pid": 1234,
            "owner_process_create_time": 90.0,
        }
        process = mock.Mock()
        process.cmdline.return_value = ["worker.exe", "--rpa-operator-guard"]
        process.ppid.return_value = 1234
        with (
            mock.patch.object(rpa_operator_guard, "_process_create_time", return_value=100.0),
            mock.patch.object(rpa_operator_guard.psutil, "Process", return_value=process),
        ):
            self.assertTrue(
                rpa_operator_guard._guard_process_identity_matches(record, state)
            )
            process.ppid.return_value = 9999
            self.assertFalse(
                rpa_operator_guard._guard_process_identity_matches(record, state)
            )


if __name__ == "__main__":
    unittest.main()
