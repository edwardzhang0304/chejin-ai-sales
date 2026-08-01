from __future__ import annotations

import importlib
import json
import os
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch


class RuntimeSupervisionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.previous_home = os.environ.get("CHEJIN_WORKER_HOME")
        os.environ["CHEJIN_WORKER_HOME"] = self.tmp.name
        import chejin_worker_client.config as config
        import chejin_worker_client.incident_evidence as incident_evidence
        import chejin_worker_client.storage as storage
        import chejin_worker_client.runtime_supervision as runtime_supervision

        importlib.reload(config)
        self.storage = importlib.reload(storage)
        self.incidents = importlib.reload(incident_evidence)
        self.runtime = importlib.reload(runtime_supervision)
        from chejin_worker_client.emergency_stop import reset_emergency_stop_for_tests

        reset_emergency_stop_for_tests()

    def tearDown(self) -> None:
        self.runtime.reset_runtime_supervision_for_tests()
        self.incidents.stop_incident_worker(wait=True)
        if self.previous_home is None:
            os.environ.pop("CHEJIN_WORKER_HOME", None)
        else:
            os.environ["CHEJIN_WORKER_HOME"] = self.previous_home
        import chejin_worker_client.config as config
        import chejin_worker_client.incident_evidence as incident_evidence
        import chejin_worker_client.storage as storage

        importlib.reload(config)
        importlib.reload(storage)
        importlib.reload(incident_evidence)
        self.tmp.cleanup()

    def _incident_for_event(self, event: str):
        row = next(
            item
            for item in self.storage.read_logs(limit=50)
            if item.get("event") == event
        )
        path = self.incidents.wait_for_incident(
            row["metadata"]["incident_id"],
            timeout=10.0,
        )
        self.assertIsNotNone(path)
        return row, path

    def test_previous_unclean_session_creates_recovery_incident(self) -> None:
        marker = self.runtime._marker_path()
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(
            json.dumps(
                {
                    "session_id": "previous-session",
                    "status": "running",
                    "pid": 1234,
                    "started_at": "2026-08-01T00:00:00+00:00",
                }
            ),
            encoding="utf-8",
        )

        self.runtime.install_runtime_supervision()

        _, path = self._incident_for_event(
            "worker_previous_session_unclean_exit"
        )
        self.assertTrue(path.is_file())
        self.runtime.mark_runtime_clean_exit(0)
        final_marker = json.loads(marker.read_text(encoding="utf-8"))
        self.assertEqual(final_marker["status"], "clean_exit")

    def test_unhandled_exception_pauses_and_keeps_traceback(self) -> None:
        from chejin_worker_client.models import Binding

        self.storage.save_binding(
            Binding(
                worker_id="worker-runtime",
                worker_token="runtime-token",
                client_instance_id="runtime-client",
                run_status="running",
            )
        )
        try:
            raise RuntimeError("main callback crashed")
        except RuntimeError as exc:
            self.runtime.report_unhandled_exception(
                "qt_callback",
                type(exc),
                exc,
                exc.__traceback__,
            )

        self.assertEqual(self.storage.load_binding().run_status, "paused")
        _, path = self._incident_for_event("worker_unhandled_exception")
        with zipfile.ZipFile(path) as archive:
            trace = archive.read("traceback.txt").decode("utf-8")
        self.assertIn("RuntimeError: main callback crashed", trace)

    def test_unhandled_exception_stops_a_running_task_runner_in_memory(self) -> None:
        from chejin_worker_client.emergency_stop import emergency_stop_requested
        from chejin_worker_client.models import Binding
        from chejin_worker_client.task_runner import TaskRunner

        calls: list[str] = []

        class Api:
            def pull_task(self, *_args, **_kwargs):
                calls.append("pull_task")
                return "pending", None, "NO_PENDING_TASK"

        class Bridge:
            def probe(self):
                calls.append("probe")
                return "ready", "logged_in"

            def list_sessions(self, **_kwargs):
                calls.append("list_sessions")
                return {"ok": True, "sessions": []}

        binding = Binding(
            worker_id="worker-running",
            worker_token="runtime-token",
            client_instance_id="runtime-client",
            run_status="running",
        )
        self.storage.save_binding(binding)
        runner = TaskRunner(
            api=Api(),
            bridge=Bridge(),
            on_profile=lambda value: None,
            on_status=lambda value: None,
            on_step=lambda value: None,
            on_task=lambda value: None,
            on_result=lambda value: None,
            on_error=lambda value: None,
        )
        runner.binding = binding

        try:
            raise RuntimeError("qt failure while runner is active")
        except RuntimeError as exc:
            self.runtime.report_unhandled_exception(
                "qt_callback",
                type(exc),
                exc,
                exc.__traceback__,
            )

        self.assertTrue(emergency_stop_requested())
        self.assertEqual(self.storage.load_binding().run_status, "paused")
        self.assertEqual(runner.binding.run_status, "running")
        self.assertFalse(runner._ui_actions_enabled(runner.binding))
        runner.tick_once()
        runner._pull_and_execute(runner.binding)
        runner._run_c2_scan_round(runner.binding, reason="emergency-test")
        self.assertEqual(calls, [])

    def test_emergency_stop_blocks_direct_rpa_and_vision_boundaries(self) -> None:
        from chejin_worker_client.emergency_stop import trigger_emergency_stop
        from chejin_worker_client.omniauto_vision import _cancel_requested
        from chejin_worker_client.rpa_bridge import RpaBridge

        trigger_emergency_stop(
            reason="TEST_EMERGENCY_STOP",
            origin="test",
        )
        bridge = RpaBridge(Path(self.tmp.name) / "sidecar.py")
        with patch.object(bridge, "_call_omniauto_process") as process:
            result = bridge._call_omniauto(["status"])

        self.assertEqual(result["error_code"], "WORKER_EMERGENCY_STOPPED")
        process.assert_not_called()
        self.assertTrue(_cancel_requested(None))

    def test_threading_excepthook_captures_other_background_thread(self) -> None:
        self.runtime._ORIGINAL_THREADING_EXCEPTHOOK = lambda args: None
        self.runtime.install_runtime_supervision()

        def crash() -> None:
            raise RuntimeError("other background thread crashed")

        thread = threading.Thread(target=crash, name="UnsupervisedWorker")
        thread.start()
        thread.join(timeout=2.0)

        row, path = self._incident_for_event("worker_unhandled_exception")
        self.assertEqual(row["metadata"]["origin"], "thread:UnsupervisedWorker")
        self.assertTrue(path.is_file())
