from __future__ import annotations

import importlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch


class IncidentEvidenceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.previous_home = os.environ.get("CHEJIN_WORKER_HOME")
        os.environ["CHEJIN_WORKER_HOME"] = self.tmp.name
        import chejin_worker_client.config as config
        import chejin_worker_client.incident_evidence as incident_evidence
        import chejin_worker_client.storage as storage

        importlib.reload(config)
        self.storage = importlib.reload(storage)
        self.incidents = importlib.reload(incident_evidence)
        self.incidents.INCIDENT_SETTLE_WINDOW_SECONDS = 0.0

    def tearDown(self) -> None:
        self.incidents.stop_incident_worker(wait=True)
        os.environ.pop("CUSTOMER_IMAGE_UNDERSTANDING_API_KEY", None)
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

    def _completed_path(self, result: dict) -> Path:
        path = self.incidents.wait_for_incident(result["incident_id"], timeout=10.0)
        self.assertIsNotNone(path)
        assert path is not None
        return path

    def test_incident_packages_are_redacted_and_include_allowed_evidence(self) -> None:
        from chejin_worker_client.models import Binding

        worker_token = "worker-token-must-not-leak"
        vision_key = "TEST_VISION_SECRET_DO_NOT_EXPORT"
        os.environ["CUSTOMER_IMAGE_UNDERSTANDING_API_KEY"] = vision_key
        self.storage.save_binding(
            Binding(
                worker_id="worker-incident",
                worker_token=worker_token,
                client_instance_id="client-incident",
                run_status="paused",
            )
        )
        evidence = Path(self.tmp.name) / "artifacts" / "review.json"
        evidence.parent.mkdir(parents=True)
        evidence.write_text(
            json.dumps({"worker_token": worker_token, "api_key": vision_key, "ok": False}),
            encoding="utf-8",
        )
        screenshot = evidence.with_name("failure-screen.png")
        screenshot.write_bytes(b"\x89PNG\r\n\x1a\nincident-test")
        results = [
            self.storage.append_log(
                "ERROR",
                "evidence_redaction_test",
                f"capture test {worker_token}",
                error_code="EVIDENCE_REDACTION_TEST",
                metadata={
                    "conversation_id": "conversation-incident",
                    "sidecar_run_id": "sidecar-redaction",
                    "review_path": str(evidence),
                    "screenshot_path": str(screenshot),
                    "traceback": f"Traceback: {vision_key}",
                },
            )
        ]

        self.assertEqual(len({item["incident_id"] for item in results}), 1)
        for item in results:
            self.assertTrue(item["incident_id"].startswith("INC-"))
            package = self._completed_path(item)
            self.assertTrue(package.is_file())
            with zipfile.ZipFile(package) as archive:
                names = archive.namelist()
                self.assertIn("manifest.json", names)
                self.assertIn("logs/recent_logs.json", names)
                self.assertIn("state/outbox.json", names)
                self.assertIn("state/action_journals.json", names)
                self.assertIn("traceback.txt", names)
                self.assertTrue(any(name.endswith("review.json") for name in names))
                self.assertTrue(any(name.endswith("failure-screen.png") for name in names))
                self.assertFalse(any("sqlite" in name.lower() or name.lower().endswith(".db") for name in names))
                text_payload = "\n".join(
                    archive.read(name).decode("utf-8", errors="ignore")
                    for name in names
                    if not name.lower().endswith((".png", ".jpg", ".jpeg", ".webp", ".bmp"))
                )
                self.assertNotIn(worker_token, text_payload)
                self.assertNotIn(vision_key, text_payload)
                self.assertIn("[REDACTED]", text_payload)

        with zipfile.ZipFile(self._completed_path(results[-1])) as archive:
            manifest = json.loads(archive.read("manifest.json"))
            self.assertEqual(manifest["error_code"], "EVIDENCE_REDACTION_TEST")
            self.assertEqual(manifest["sidecar_run_id"], "sidecar-redaction")
            self.assertRegex(manifest["build"]["git_commit"], r"^[0-9a-f]{40}$")

        latest = self.incidents.latest_incident()
        self.assertIsNotNone(latest)
        assert latest is not None
        self.assertEqual(latest["incident_id"], results[-1]["incident_id"])
        last_log = self.storage.read_logs(limit=1)[0]
        self.assertEqual(last_log["metadata"]["incident_id"], results[-1]["incident_id"])
        self.assertEqual(last_log["metadata"]["evidence_path"], results[-1]["evidence_path"])

    def test_embedded_vision_key_is_redacted_without_environment_copy(self) -> None:
        embedded_key = "embedded-vision-unit-key-never-export"
        with tempfile.TemporaryDirectory() as credential_temp:
            credential_path = Path(credential_temp) / "vision-runtime.json"
            credential_path.write_text(
                json.dumps(
                    {"schema_version": 1, "vision_api_key": embedded_key}
                ),
                encoding="utf-8",
            )
            with patch.dict(
                os.environ,
                {
                    "CHEJIN_BUILD_KIND": "official",
                    "CHEJIN_VISION_CREDENTIAL_PATH": str(credential_path),
                },
                clear=False,
            ):
                os.environ.pop("CUSTOMER_IMAGE_UNDERSTANDING_API_KEY", None)
                redacted = self.incidents.redact_diagnostic(
                    {"traceback": f"provider failed: {embedded_key}"}
                )

        self.assertNotIn(embedded_key, json.dumps(redacted))
        self.assertIn("[REDACTED]", redacted["traceback"])

    def test_image_menu_failure_zip_contains_full_roi_and_ocr_evidence(self) -> None:
        artifact_dir = Path(self.tmp.name) / "artifacts" / "wechat_c2" / "messages" / "menu-run"
        artifact_dir.mkdir(parents=True)
        full_screen = artifact_dir / "vision_image_context_menu_1.png"
        roi_screen = artifact_dir / "vision_image_context_menu_ocr_roi_1.png"
        full_screen.write_bytes(b"\x89PNG\r\n\x1a\nfull-menu-evidence")
        roi_screen.write_bytes(b"\x89PNG\r\n\x1a\nroi-menu-evidence")

        result = self.storage.append_log(
            "WARN",
            "c2_image_slot_terminalized",
            "image menu operation failed",
            error_code="C2_IMAGE_MENU_OPERATION_FAILED",
            force_incident=True,
            metadata={
                "conversation_id": "conversation-menu-evidence",
                "sidecar_run_id": "sidecar-menu-evidence",
                "artifact_dir": str(artifact_dir),
                "diagnostics": {
                    "events": [
                        {
                            "stage": "frame_capture",
                            "phase": "image_context_menu",
                            "screenshot_path": str(full_screen),
                            "roi_screenshot_path": str(roi_screen),
                            "local_ocr_evidence": [
                                {
                                    "text": "复制",
                                    "confidence": 0.97,
                                    "bounds": [540, 272, 640, 320],
                                }
                            ],
                        }
                    ]
                },
            },
        )

        with zipfile.ZipFile(self._completed_path(result)) as archive:
            names = archive.namelist()
            occurrence = json.loads(archive.read("occurrences/initial.json"))
        self.assertTrue(any(name.endswith(full_screen.name) for name in names))
        self.assertTrue(any(name.endswith(roi_screen.name) for name in names))
        event = occurrence["metadata"]["diagnostics"]["events"][0]
        self.assertEqual(event["local_ocr_evidence"][0]["text"], "复制")

        later = self.storage.append_log(
            "ERROR",
            "backend_connection_failed",
            "later unrelated incident",
            error_code="BACKEND_CONNECTION_FAILED",
        )
        self._completed_path(later)
        selected = self.incidents.incident_by_id(result["incident_id"])
        self.assertIsNotNone(selected)
        assert selected is not None
        self.assertEqual(selected["incident_id"], result["incident_id"])
        with zipfile.ZipFile(selected["evidence_path"]) as archive:
            selected_manifest = json.loads(archive.read("manifest.json"))
        self.assertEqual(selected_manifest["event"], "c2_image_slot_terminalized")
        self.assertIsNone(self.incidents.incident_by_id("../latest"))

    def test_external_paths_and_raw_database_are_not_exported(self) -> None:
        external = Path(self.tmp.name).parent / "external-incident-secret.txt"
        external.write_text("external secret", encoding="utf-8")
        try:
            result = self.storage.append_log(
                "ERROR",
                "path_scope_failed",
                "scope test",
                metadata={"evidence_path": str(external)},
            )
            with zipfile.ZipFile(self._completed_path(result)) as archive:
                self.assertFalse(any(name.endswith(external.name) for name in archive.namelist()))
                self.assertFalse(any("worker_client.sqlite3" in name for name in archive.namelist()))
        finally:
            external.unlink(missing_ok=True)

    def test_related_paused_outbox_and_public_revision_are_preserved(self) -> None:
        payload = {
            "contract_revision": "3.12.4",
            "conversation_id": "conversation-paused",
            "authorization_revision": "authorization-revision-17",
            "read_run_id": "read-paused",
            "messages": [],
            "evidence": {},
        }
        outbox_id = self.storage.enqueue_c2_outbox(payload)
        self.storage.mark_c2_outbox_capability_paused(
            outbox_id,
            "MESSAGE_CONTRACT_REVISION_MISMATCH",
        )

        result = self.storage.append_log(
            "ERROR",
            "c2_outbox_capability_paused",
            "contract mismatch",
            error_code="MESSAGE_CONTRACT_REVISION_MISMATCH",
            metadata={
                "outbox_id": outbox_id,
                "conversation_id": "conversation-paused",
                "trace_id": "trace-contract-409",
            },
        )

        with zipfile.ZipFile(self._completed_path(result)) as archive:
            outbox = json.loads(archive.read("state/outbox.json"))
            occurrence = json.loads(archive.read("occurrences/initial.json"))
        self.assertEqual(
            outbox["related_c2_outbox"]["outbox_id"],
            outbox_id,
        )
        self.assertEqual(
            outbox["related_c2_outbox"]["authorization_revision"],
            "authorization-revision-17",
        )
        self.assertEqual(
            outbox["related_c2_outbox"]["payload"]["authorization_revision"],
            "authorization-revision-17",
        )
        self.assertEqual(
            occurrence["metadata"]["trace_id"],
            "trace-contract-409",
        )

    def test_error_inside_except_captures_complete_traceback_automatically(self) -> None:
        try:
            raise ValueError("automatic traceback marker")
        except ValueError as exc:
            result = self.storage.append_log(
                "ERROR",
                "automatic_traceback",
                str(exc),
                error_code="AUTOMATIC_TRACEBACK_TEST",
            )

        with zipfile.ZipFile(self._completed_path(result)) as archive:
            traceback_text = archive.read("traceback.txt").decode("utf-8")
        self.assertIn("ValueError: automatic traceback marker", traceback_text)
        self.assertIn("test_error_inside_except_captures_complete_traceback_automatically", traceback_text)

    def test_zip_capture_is_async_and_does_not_block_log_caller(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        caller_done = threading.Event()
        caller_result: dict[str, object] = {}
        original = self.incidents._create_incident_package

        def slow_capture(request):
            entered.set()
            release.wait(timeout=5.0)
            return original(request)

        def append_error_log() -> None:
            try:
                caller_result["result"] = self.storage.append_log(
                    "ERROR",
                    "async_capture_failure",
                    "capture must not block caller",
                )
            except BaseException as exc:  # pragma: no cover - asserted below
                caller_result["error"] = exc
            finally:
                caller_done.set()

        with patch.object(self.incidents, "_create_incident_package", side_effect=slow_capture):
            caller = threading.Thread(target=append_error_log, daemon=True)
            caller.start()
            self.assertTrue(entered.wait(timeout=2.0))
            try:
                self.assertTrue(
                    caller_done.wait(timeout=2.0),
                    "append_log waited for the blocked incident package capture",
                )
            finally:
                release.set()
                caller.join(timeout=5.0)
            self.assertFalse(caller.is_alive())
            self.assertNotIn("error", caller_result)
            result = caller_result.get("result")
            self.assertIsInstance(result, dict)
            assert isinstance(result, dict)
            self.assertFalse(Path(result["evidence_path"]).exists())
            self.assertIsNotNone(
                self.incidents.wait_for_incident(result["incident_id"], timeout=10.0)
            )

    def test_repeated_fault_is_deduplicated_until_recovery(self) -> None:
        first = self.storage.append_log(
            "ERROR",
            "heartbeat_failed",
            "network unavailable",
            error_code="BACKEND_NETWORK_UNAVAILABLE",
            metadata={"traceback": "first heartbeat stack"},
        )
        self.assertIsNotNone(self._completed_path(first))
        second = self.storage.append_log(
            "ERROR",
            "heartbeat_failed",
            "network unavailable again",
            error_code="BACKEND_NETWORK_UNAVAILABLE",
            metadata={"traceback": "second heartbeat stack"},
        )
        self.assertEqual(first["incident_id"], second["incident_id"])
        package = self._completed_path(first)
        deadline = time.monotonic() + 5.0
        occurrence_payload = ""
        while time.monotonic() < deadline:
            with zipfile.ZipFile(package) as archive:
                occurrence_names = [
                    name
                    for name in archive.namelist()
                    if name.startswith("occurrences/")
                ]
                occurrence_payload = "\n".join(
                    archive.read(name).decode("utf-8", errors="replace")
                    for name in occurrence_names
                )
            if len(occurrence_names) >= 2:
                break
            time.sleep(0.05)
        self.assertGreaterEqual(len(occurrence_names), 2)
        self.assertIn("first heartbeat stack", occurrence_payload)
        self.assertIn("second heartbeat stack", occurrence_payload)
        self.assertEqual(
            len(list(self.incidents.incident_directory().glob("INC-*.zip"))),
            1,
        )

        self.assertEqual(
            self.incidents.mark_incident_recovered("heartbeat_failed"),
            1,
        )
        third = self.storage.append_log(
            "ERROR",
            "heartbeat_failed",
            "network failed after recovery",
            error_code="BACKEND_NETWORK_UNAVAILABLE",
        )
        self.assertNotEqual(first["incident_id"], third["incident_id"])
        self.assertIsNotNone(self._completed_path(third))

    def test_occurrence_append_failure_keeps_original_zip_and_pending_record(self) -> None:
        first = self.storage.append_log(
            "ERROR",
            "atomic_occurrence_failure",
            "first occurrence",
            error_code="ATOMIC_OCCURRENCE_FAILURE",
            metadata={"traceback": "first intact stack"},
        )
        package = self._completed_path(first)
        original_bytes = package.read_bytes()

        self.incidents.stop_incident_worker(wait=True)
        second = self.incidents.schedule_incident(
            event="atomic_occurrence_failure",
            error_code="ATOMIC_OCCURRENCE_FAILURE",
            message="second occurrence",
            metadata={"traceback": "second pending stack"},
            traceback_text="second pending stack",
            start_worker=False,
        )
        pending = next(
            self.incidents._pending_occurrence_directory().glob("OCC-*.json")
        )
        with patch.object(
            self.incidents.os,
            "replace",
            side_effect=OSError("simulated append interruption"),
        ):
            self.assertFalse(self.incidents._process_pending_occurrence(pending))

        self.assertEqual(first["incident_id"], second["incident_id"])
        self.assertEqual(package.read_bytes(), original_bytes)
        self.assertTrue(pending.is_file())
        with zipfile.ZipFile(package) as archive:
            self.assertIsNone(archive.testzip())
            self.assertIn(
                "first intact stack",
                archive.read("occurrences/initial.json").decode(),
            )

        self.assertTrue(self.incidents._process_pending_occurrence(pending))
        with zipfile.ZipFile(package) as archive:
            payload = "\n".join(
                archive.read(name).decode("utf-8", errors="replace")
                for name in archive.namelist()
                if name.startswith("occurrences/")
            )
        self.assertIn("second pending stack", payload)

    def test_settle_window_includes_logs_written_after_failure(self) -> None:
        self.incidents.INCIDENT_SETTLE_WINDOW_SECONDS = 0.2
        result = self.storage.append_log(
            "ERROR",
            "settle_window_failure",
            "failure starts cleanup",
            error_code="SETTLE_WINDOW_FAILURE",
        )
        self.storage.append_log(
            "INFO",
            "settle_window_cleanup_completed",
            "pause and outbox cleanup completed",
        )

        with zipfile.ZipFile(self._completed_path(result)) as archive:
            logs = json.loads(archive.read("logs/recent_logs.json"))
        self.assertTrue(
            any(row.get("event") == "settle_window_cleanup_completed" for row in logs)
        )

    def test_merge_window_expiry_creates_a_new_incident(self) -> None:
        first = self.storage.append_log(
            "ERROR",
            "windowed_failure",
            "first occurrence",
            error_code="WINDOWED_FAILURE",
        )
        state = self.incidents._incident_state()
        entry = next(iter(state["fingerprints"].values()))
        entry["first_seen_at"] = "2026-07-31T00:00:00+00:00"
        self.incidents._write_incident_state(state)

        second = self.storage.append_log(
            "ERROR",
            "windowed_failure",
            "after merge window",
            error_code="WINDOWED_FAILURE",
        )

        self.assertNotEqual(first["incident_id"], second["incident_id"])

    def test_low_disk_falls_back_to_small_json(self) -> None:
        usage = shutil.disk_usage(self.tmp.name)
        with patch.object(
            self.incidents.shutil,
            "disk_usage",
            return_value=usage._replace(free=0),
        ):
            result = self.storage.append_log(
                "ERROR",
                "disk_low_failure",
                "full ZIP cannot be created",
                metadata={"traceback": "low disk traceback"},
            )
            path = self._completed_path(result)
        self.assertEqual(path.suffix, ".json")
        payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertTrue(payload["degraded"])
        self.assertEqual(payload["traceback"], "low disk traceback")

    def test_retention_keeps_at_most_fifty_packages(self) -> None:
        root = self.incidents.incident_directory()
        for index in range(55):
            path = root / f"INC-retention-{index:03d}.zip"
            path.write_bytes(b"incident")
            timestamp = time.time() - (55 - index)
            os.utime(path, (timestamp, timestamp))
        result = self.incidents.prune_incidents()
        self.assertEqual(result["remaining"], 50)
        self.assertEqual(result["removed"], 5)

    def test_vision_child_failure_returns_redacted_traceback(self) -> None:
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "chejin_worker_client.vision_provider_worker",
            ],
            input="{}",
            text=True,
            capture_output=True,
            check=False,
            timeout=10,
        )
        self.assertEqual(completed.returncode, 1)
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["error_code"], "VISION_PROVIDER_WORKER_FAILED")
        self.assertIn("VISION_PROVIDER_WORKER_REQUEST_INVALID", payload["traceback"])


class BackgroundThreadSupervisionTest(unittest.TestCase):
    def setUp(self) -> None:
        from chejin_worker_client.emergency_stop import reset_emergency_stop_for_tests

        reset_emergency_stop_for_tests()

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        cls.previous_home = os.environ.get("CHEJIN_WORKER_HOME")
        os.environ["CHEJIN_WORKER_HOME"] = cls.tmp.name
        import chejin_worker_client.config as config
        import chejin_worker_client.incident_evidence as incident_evidence
        import chejin_worker_client.storage as storage

        importlib.reload(config)
        cls.storage = importlib.reload(storage)
        cls.incidents = importlib.reload(incident_evidence)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.incidents.stop_incident_worker(wait=True)
        if cls.previous_home is None:
            os.environ.pop("CHEJIN_WORKER_HOME", None)
        else:
            os.environ["CHEJIN_WORKER_HOME"] = cls.previous_home
        import chejin_worker_client.config as config
        import chejin_worker_client.incident_evidence as incident_evidence
        import chejin_worker_client.storage as storage

        importlib.reload(config)
        importlib.reload(storage)
        importlib.reload(incident_evidence)
        cls.tmp.cleanup()

    def _runner(self):
        from chejin_worker_client.models import Binding
        from chejin_worker_client.task_runner import TaskRunner

        errors: list[str] = []
        runner = TaskRunner(
            api=object(),
            bridge=object(),
            on_profile=lambda value: None,
            on_status=lambda value: None,
            on_step=lambda value: None,
            on_task=lambda value: None,
            on_result=lambda value: None,
            on_error=errors.append,
        )
        runner.binding = Binding(
            worker_id="worker-thread",
            worker_token="token-thread",
            client_instance_id="client-thread",
            run_status="running",
        )
        return runner, errors

    def test_task_runner_crash_is_captured_and_pauses_client(self) -> None:
        runner, errors = self._runner()

        def crash() -> None:
            raise RuntimeError("supervised crash")

        runner._run_supervised_loop("task_runner", crash)

        self.assertEqual(runner.binding.run_status, "paused")
        self.assertEqual(runner._pending_run_status_sync, "paused")
        row = next(
            item
            for item in self.storage.read_logs(limit=20)
            if item.get("event") == "worker_background_thread_failed"
            and (item.get("metadata") or {}).get("thread_kind") == "task_runner"
        )
        self.assertEqual(row["error_code"], "WORKER_BACKGROUND_THREAD_CRASHED")
        self.assertIn("RuntimeError: supervised crash", row["metadata"]["traceback"])
        self.assertIsNotNone(
            self.incidents.wait_for_incident(
                row["metadata"]["incident_id"], timeout=10.0
            )
        )
        self.assertIn(row["metadata"]["incident_id"], errors[0])

    def test_c2_listener_crash_uses_the_real_supervised_exit(self) -> None:
        runner, _ = self._runner()

        def crash() -> None:
            raise RuntimeError("c2 listener crash")

        runner._run_supervised_loop("c2_listener", crash)

        row = next(
            item
            for item in self.storage.read_logs(limit=20)
            if item.get("event") == "worker_background_thread_failed"
            and (item.get("metadata") or {}).get("thread_kind") == "c2_listener"
        )
        self.assertIsNotNone(
            self.incidents.wait_for_incident(
                row["metadata"]["incident_id"], timeout=10.0
            )
        )

    def test_task_runner_and_c2_crashes_have_distinct_incidents(self) -> None:
        self.incidents.mark_incident_recovered("worker_background_thread_failed")
        runner, _ = self._runner()

        def task_crash() -> None:
            raise RuntimeError("task runner distinct stack")

        def c2_crash() -> None:
            raise RuntimeError("c2 listener distinct stack")

        runner._run_supervised_loop("task_runner", task_crash)
        runner._run_supervised_loop("c2_listener", c2_crash)

        rows = {
            row["metadata"]["thread_kind"]: row
            for row in self.storage.read_logs(limit=50)
            if row.get("event") == "worker_background_thread_failed"
            and (row.get("metadata") or {}).get("thread_kind")
            in {"task_runner", "c2_listener"}
            and "distinct stack"
            in str((row.get("metadata") or {}).get("traceback") or "")
        }
        self.assertEqual(set(rows), {"task_runner", "c2_listener"})
        self.assertNotEqual(
            rows["task_runner"]["metadata"]["incident_id"],
            rows["c2_listener"]["metadata"]["incident_id"],
        )
        for thread_kind, marker in (
            ("task_runner", "task runner distinct stack"),
            ("c2_listener", "c2 listener distinct stack"),
        ):
            path = self.incidents.wait_for_incident(
                rows[thread_kind]["metadata"]["incident_id"],
                timeout=10.0,
            )
            self.assertIsNotNone(path)
            assert path is not None
            with zipfile.ZipFile(path) as archive:
                trace = archive.read("traceback.txt").decode("utf-8")
            self.assertIn(marker, trace)

    def test_backend_network_failure_uses_real_heartbeat_path(self) -> None:
        runner, _ = self._runner()
        runner.binding.run_status = "paused"

        class Bridge:
            @staticmethod
            def probe():
                return "ready", "logged_in"

        class Api:
            @staticmethod
            def heartbeat(*args, **kwargs):
                raise ConnectionError("backend disconnected")

        runner.bridge = Bridge()
        runner.api = Api()
        runner.tick_once()

        row = next(
            item
            for item in self.storage.read_logs(limit=20)
            if item.get("event") == "heartbeat_failed"
        )
        self.assertIsNotNone(
            self.incidents.wait_for_incident(
                row["metadata"]["incident_id"], timeout=10.0
            )
        )

    def test_wechat_window_missing_uses_real_probe_path(self) -> None:
        from chejin_worker_client.models import WorkerProfile

        runner, _ = self._runner()
        runner.binding.run_status = "paused"

        class Bridge:
            @staticmethod
            def probe():
                return "ready", "not_found"

        class Api:
            @staticmethod
            def heartbeat(binding, **kwargs):
                return WorkerProfile(
                    id=binding.worker_id,
                    worker_name="incident-test",
                    run_status="paused",
                )

        runner.bridge = Bridge()
        runner.api = Api()
        runner.tick_once()

        row = next(
            item
            for item in self.storage.read_logs(limit=20)
            if item.get("event") == "wechat_window_missing"
        )
        self.assertIsNotNone(
            self.incidents.wait_for_incident(
                row["metadata"]["incident_id"], timeout=10.0
            )
        )

    def test_monitor_reports_dead_c2_listener_once(self) -> None:
        runner, _ = self._runner()
        runner.thread = threading.current_thread()
        runner.c2_thread = threading.Thread(target=lambda: None)
        calls: list[str] = []

        def record(thread_kind: str, **kwargs) -> None:
            calls.append(thread_kind)
            runner.stop_event.set()

        with patch.object(runner, "_handle_background_thread_failure", side_effect=record):
            runner._monitor_background_threads()

        self.assertEqual(calls, ["c2_listener"])
