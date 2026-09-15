from __future__ import annotations

from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch
import zipfile

from PIL import Image
import pytest
import requests

from chejin_worker_client import config, failure_evidence, incident_evidence, rpa_bridge, storage
from chejin_worker_client.api import ApiError, WorkerApiClient
from chejin_worker_client.models import Binding, Task


@pytest.fixture
def home(tmp_path, monkeypatch):
    incident_evidence.stop_incident_worker(wait=True)
    settings = replace(config.CONFIG, app_dir=tmp_path)
    monkeypatch.setenv("CHEJIN_WORKER_HOME", str(tmp_path))
    monkeypatch.setattr(config, "CONFIG", settings)
    monkeypatch.setattr(rpa_bridge, "CONFIG", settings)
    monkeypatch.setattr(storage, "APP_DIR", tmp_path)
    monkeypatch.setattr(storage, "DB_FILE", tmp_path / "worker_client.sqlite3")
    monkeypatch.setattr(storage, "_post_update_initialized_database", None)
    monkeypatch.setattr(incident_evidence, "INCIDENT_SETTLE_WINDOW_SECONDS", 0)
    yield tmp_path
    incident_evidence.stop_incident_worker(wait=True)


def package_for(event):
    row = next(row for row in storage.read_logs(limit=1000) if row["event"] == event)
    path = incident_evidence.wait_for_incident(row["metadata"]["incident_id"], timeout=10)
    assert path is not None
    return path, row


def archive_json(path, member):
    with zipfile.ZipFile(path) as z:
        return json.loads(z.read(member))


@pytest.mark.parametrize("mode,state", [("blank", "blank_render_detected"), ("ocr_error", "win32_ocr_failed")])
def test_runtime_probe_real_child_retains_exact_frame_and_cause(home, monkeypatch, mode, state):
    driver = Path(__file__).parent / "fixtures" / "status_probe_process.py"
    bridge = rpa_bridge.RpaBridge(driver)
    bridge.mode = "omniauto"
    bridge._startup_window_normalization_state = "completed"
    monkeypatch.setenv("PROBE_TEST_MODE", mode)
    # Executable selection and platform are OS boundaries. No business or
    # diagnostic method is substituted: actual Popen/CLI/capture/save/SQLite/ZIP.
    monkeypatch.setattr(rpa_bridge, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(bridge, "_sidecar_command", lambda args: [sys.executable, str(driver), *args])
    assert bridge.probe() == ("unavailable", "unknown")
    path, row = package_for("rpa_action_failed")
    result = row["metadata"]["result"]
    assert result["state"] == state
    assert row["metadata"]["origin"] == "status"
    index = archive_json(path, "evidence-index/initial.json")
    assert index["screenshot_status"] == "included"
    with zipfile.ZipFile(path) as z:
        with Image.open(io.BytesIO(z.read(index["origin_screenshots"][0]))) as image:
            assert image.size == (800, 812)
            assert image.tobytes() == Image.new("RGB", (800, 812), "white").tobytes()
        saved = json.loads(z.read("occurrences/initial.json"))
        assert saved["metadata"]["result"]["state"] == state
    assert bridge.last_probe_payload["state"] == state
    assert bridge.active_artifact_dirs() == set()


def test_successful_probe_does_not_accumulate_screenshots(home, monkeypatch):
    driver = Path(__file__).parent / "fixtures" / "status_probe_process.py"
    bridge = rpa_bridge.RpaBridge(driver)
    bridge.mode = "omniauto"
    bridge._startup_window_normalization_state = "completed"
    monkeypatch.setenv("PROBE_TEST_MODE", "success")
    monkeypatch.setattr(rpa_bridge, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(bridge, "_sidecar_command", lambda args: [sys.executable, str(driver), *args])
    assert bridge.probe() == ("ready", "logged_in")
    assert list((home / "artifacts" / "rpa_probes").iterdir()) == []
    assert not list((home / "incidents").glob("INC-*.zip"))


@pytest.mark.parametrize("level,code", [("WARN", "RPA_COMPONENT_UNAVAILABLE"), ("INFO", "C2_REPLY_CONTEXT_RECOVERY_FAILED"), ("WARN", None)])
def test_recoverable_failures_are_captured_regardless_of_level(home, level, code):
    record = storage.append_log(level, "recoverable_failure", "failure", error_code=code)
    assert record["incident_id"]
    path, row = package_for("recoverable_failure")
    assert row["level"] == level
    assert archive_json(path, "manifest.json")["error_code"] == code


def test_merged_failure_includes_new_frame_and_explicit_missing_frame(home):
    root = home / "artifacts"
    root.mkdir()
    one = root / "first.png"
    two = root / "second.png"
    Image.new("RGB", (20, 20), "red").save(one)
    Image.new("RGB", (20, 20), "blue").save(two)
    first = storage.append_log("WARN", "same_failure", "first", metadata={"screenshot_path": str(one)})
    package, _ = package_for("same_failure")
    second = storage.append_log("WARN", "same_failure", "second", metadata={"screenshot_path": str(two)})
    assert first["incident_id"] == second["incident_id"]
    deadline = time.monotonic() + 5
    while list((home / "incidents" / "pending-occurrences").glob("*.json")) and time.monotonic() < deadline:
        time.sleep(.02)
    with zipfile.ZipFile(package) as z:
        images = [name for name in z.namelist() if name.endswith(".png")]
        assert len(images) == 2
        assert {z.read(name) for name in images} == {one.read_bytes(), two.read_bytes()}
    missing = root / "unwritten.png"
    storage.append_log("WARN", "no_frame_failure", "failure", metadata={"screenshot_path": str(missing)})
    package, _ = package_for("no_frame_failure")
    index = archive_json(package, "evidence-index/initial.json")
    assert index["screenshot_status"] == "unavailable"
    assert any(item["reason"] == "source_missing" for item in index["omissions"])
    # Old frames may be context, but must never masquerade as this failure's frame.
    assert not index["origin_screenshots"]


def test_export_adds_latest_logs_even_when_incident_is_old_or_missing(home):
    storage.append_log("ERROR", "old_failure", "failure")
    original, _ = package_for("old_failure")
    original_bytes = original.read_bytes()
    storage.append_log("INFO", "after_incident_pause", "暂停接单。")
    for selected in (incident_evidence.latest_incident(), None):
        destination = home / f"export-{bool(selected)}.zip"
        incident_evidence.export_diagnostic_bundle(destination, selected)
        records = archive_json(destination, "logs/latest_logs.json")
        assert any(row["event"] == "after_incident_pause" for row in records)
        assert archive_json(destination, "export.json")["log_last_at"] == records[0]["created_at"]
        with zipfile.ZipFile(destination) as z:
            assert not any(name.endswith((".sqlite3", ".env")) for name in z.namelist())
    assert original.read_bytes() == original_bytes


def test_exact_http_409_is_saved_without_tokens_or_response_body(home):
    token = "private-worker-credential-do-not-export"
    binding = Binding(worker_id="worker-test", worker_token=token, client_instance_id="client-test", run_status="running")
    storage.save_binding(binding)

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            assert self.headers.get("X-Worker-Token") == token
            response = json.dumps({"code": "RPA_COMPONENT_UNAVAILABLE", "message": token,
                                   "trace_id": "request-trace-123", "data": None}).encode()
            self.send_response(409)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = WorkerApiClient(f"http://127.0.0.1:{server.server_port}")
    try:
        with pytest.raises(ApiError) as caught:
            client.claim_task(binding, Task(id="task-test", task_type="chat_reply", status="pending"),
                              claim_source="c2_conversation_flow", conversation_id="conversation-test")
        assert caught.value.code == "RPA_COMPONENT_UNAVAILABLE"
        assert caught.value.status_code == 409
        path, row = package_for("api_request_failed")
        assert row["metadata"]["trace_id"] == "request-trace-123"
        assert row["metadata"]["http_status"] == 409
        with zipfile.ZipFile(path) as z:
            assert all(token.encode() not in z.read(name) for name in z.namelist())
    finally:
        client.session.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_transport_failure_and_recorder_failure_preserve_original_exception(home, monkeypatch):
    client = WorkerApiClient()
    original = requests.Timeout("private request payload must not be copied")
    monkeypatch.setattr(client.session, "request", lambda *a, **kw: (_ for _ in ()).throw(original))
    with pytest.raises(requests.Timeout) as caught:
        client._request("GET", "/read-authorization?secret=private-query")
    assert caught.value is original
    package, row = package_for("api_request_failed")
    assert row["metadata"]["exception_type"] == "Timeout"
    assert row["metadata"]["origin"] == "GET /read-authorization"
    monkeypatch.setattr(storage, "append_log", lambda *a, **kw: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(requests.Timeout) as caught:
        client._request("GET", "/read-authorization")
    assert caught.value is original
    fallback = home / "incidents" / "evidence-recorder-failures.jsonl"
    assert fallback.is_file()
    assert "OSError" in fallback.read_text()
    assert "private" not in fallback.read_text()


def test_package_write_failure_retains_original_failure_context(home, monkeypatch):
    def broken_zip(*args, **kwargs):
        raise OSError("zip unavailable")
    monkeypatch.setattr(incident_evidence.zipfile, "ZipFile", broken_zip)
    record = storage.append_log("WARN", "probe_failure", "failed", error_code="RPA_ACTION_FAILED",
                                metadata={"state": "blank_render_detected"})
    path = incident_evidence.wait_for_incident(record["incident_id"], timeout=5)
    assert path and path.suffix == ".json"
    payload = json.loads(path.read_text())
    assert payload["failure_context"]["state"] == "blank_render_detected"
    assert payload["evidence_capture_error"] == "OSError"


def test_screenshot_save_failure_does_not_change_successful_status(home):
    from test_startup_calibration_evidence import StartupCalibrationEvidenceTest, sidecar, search_ocr

    fixture = StartupCalibrationEvidenceTest()
    try:
        fixture.setUp()
        assert fixture.run_startup()["ok"]
        with patch.object(sidecar, "run_ocr", lambda _image: search_ocr()), patch.object(
            Image.Image, "save", side_effect=PermissionError("read-only diagnostic directory")
        ):
            result = sidecar.run_sidecar_cli(["status", "--artifact-dir", str(home / "artifacts")])
        assert result["ok"] is True
        assert result["screenshot_evidence"]["status"] == "save_failed"
        assert result["screenshot_evidence"]["exception_type"] == "PermissionError"
    finally:
        fixture.doCleanups()


def test_public_export_bridge_produces_fresh_logs_without_an_old_incident(home, monkeypatch):
    from test_web_ui_binding_behavior import _headless_web_ui_module

    storage.append_log("INFO", "current_pause", "暂停接单。")
    destination = home / "clicked-export.zip"
    with _headless_web_ui_module() as ui:
        monkeypatch.setattr(ui.QFileDialog, "getSaveFileName", staticmethod(lambda *args: (str(destination), "ZIP")), raising=False)
        window = ui.WorkerWebWindow.__new__(ui.WorkerWebWindow)
        window._publish = lambda: None
        errors = []
        window.on_error = errors.append
        bridge = ui.WorkerWebBridge(window)
        assert bridge.exportLatestIncident() == str(destination)
        assert not errors
    assert archive_json(destination, "logs/latest_logs.json")[0]["event"] == "current_pause"


def test_corrupt_local_state_and_telemetry_failure_leave_independent_evidence(home, monkeypatch):
    from chejin_worker_client import action_journal, telemetry

    with storage.db_connection() as connection:
        connection.execute("INSERT INTO client_settings(key,value,updated_at) VALUES (?,?,?)",
                           (storage.RUNTIME_CONTROL_KEY, "broken-json", "2026-09-15"))
        connection.commit()
    assert storage.load_runtime_control() == storage.DEFAULT_RUNTIME_CONTROL
    journal = home / "broken-journal.json"
    journal.write_text("broken-json")
    assert action_journal.read_action_journal(journal) == {}
    # Disk/SQLite boundary, retaining the production telemetry failure path.
    monkeypatch.setattr(telemetry.sqlite3, "connect", lambda *a, **kw: (_ for _ in ()).throw(OSError("test disk error")))
    assert telemetry.pending_stage_events(db_path=home / "telemetry.sqlite3") == []
    records = [json.loads(line) for line in (home / "incidents" / "evidence-recorder-failures.jsonl").read_text().splitlines()]
    assert {r["origin"] for r in records} >= {
        "storage.load_runtime_control", "read_action_journal", "telemetry.pending_stage_events",
    }


def test_repeated_evidence_does_not_bypass_package_size_limit(home, monkeypatch):
    root = home / "artifacts"
    root.mkdir()
    one, two = root / "one.txt", root / "two.txt"
    one.write_text("a" * 80)
    two.write_text("b" * 80)
    monkeypatch.setattr(incident_evidence, "MAX_EVIDENCE_BYTES", 100)
    path = home / "bounded.zip"
    with zipfile.ZipFile(path, "w") as z:
        first = incident_evidence._write_evidence_files(z, [one], set(), origin_files={one}, omissions=[])
        duplicate = incident_evidence._write_evidence_files(z, [one], set(), origin_files={one}, omissions=[])
        next_frame = incident_evidence._write_evidence_files(z, [two], set(), origin_files={two}, omissions=[])
        assert len([n for n in z.namelist() if n.startswith("evidence/")]) == 1
    assert first["files"] == duplicate["files"]
    assert next_frame["omissions"][0]["reason"] == "evidence_size_limit"


def test_public_ui_bridge_records_frame_and_redacts_payload(home):
    from test_web_ui_binding_behavior import _headless_web_ui_module

    class Frame:
        def isNull(self):
            return False
        def save(self, path, format):
            Image.new("RGB", (316, 628), "blue").save(path, format)
            return True

    with _headless_web_ui_module() as module:
        window = SimpleNamespace(binding=object(), active_page="workbench", grab=lambda: Frame())
        bridge = module.WorkerWebBridge(window)
        bridge.reportUiFailure(json.dumps({"kind": "javascript_error", "file": "worker-web-app.js",
                                          "line": 30, "message": "SECRET_TOKEN"}))
    path, row = package_for("ui_runtime_failed")
    assert row["metadata"]["line"] == "30"
    assert row["metadata"]["screenshot_status"] == "saved"
    index = archive_json(path, "evidence-index/initial.json")
    assert index["screenshot_status"] == "included"
    with zipfile.ZipFile(path) as archive:
        with Image.open(io.BytesIO(archive.read(index["origin_screenshots"][0]))) as frame:
            assert frame.size == (316, 628)
        assert all(b"SECRET_TOKEN" not in archive.read(name) for name in archive.namelist())


def test_ui_evidence_capture_failure_does_not_raise_or_capture_binding_secrets(home):
    def fail_capture():
        raise OSError("capture failed")
    window = SimpleNamespace(binding=object(), active_page="workbench", grab=fail_capture)
    failure_evidence.record_ui_failure(window, '{"kind":"renderer_terminated","exit_code":7}')
    _, row = package_for("ui_runtime_failed")
    assert row["metadata"]["screenshot_reason"] == "capture_failed"
    assert row["metadata"]["capture_error"]["exception_type"] == "OSError"
    window.active_page = "bind"
    failure_evidence.record_ui_failure(window, '{"kind":"bridge_invalid_json"}')
    row = next(r for r in storage.read_logs() if r["metadata"].get("origin") == "bridge_invalid_json")
    assert row["metadata"]["screenshot_reason"] == "sensitive_binding_screen"


def test_diagnostic_export_survives_unreadable_database(home, monkeypatch):
    monkeypatch.setattr(storage, "read_logs", lambda **kw: (_ for _ in ()).throw(OSError("database unavailable")))
    path = incident_evidence.export_diagnostic_bundle(home / "export.zip")
    assert archive_json(path, "logs/latest_logs.json") == []
    index = archive_json(path, "evidence-index/export.json")
    assert any(item["reason"] == "latest_logs_unavailable" for item in index["omissions"])
    with zipfile.ZipFile(path) as archive:
        assert "diagnostics/evidence-recorder-failures.jsonl" in archive.namelist()


def test_invalid_update_state_keeps_rejection_and_bounded_diagnostic(home, monkeypatch):
    from chejin_worker_client.client_update import ClientUpdateError, UpdateStateStore
    from chejin_worker_client import update_diagnostics
    store = UpdateStateStore(home / "update")
    store.state_path.parent.mkdir(parents=True, exist_ok=True)
    store.state_path.write_text("broken-json")
    monkeypatch.setattr(update_diagnostics, "MAX_DIAGNOSTIC_BYTES", 1)
    for _ in range(2):
        with pytest.raises(ClientUpdateError) as error:
            store.load()
        assert error.value.code == "UPDATE_STATE_INVALID"
    records = list(store.state_path.parent.glob("worker-startup*.jsonl"))
    assert len(records) == 2
    assert all(json.loads(path.read_text())["phase"] == "update_state_read" for path in records)


def test_uncaught_exception_keeps_evidence_when_sqlite_logger_fails(home, monkeypatch):
    from chejin_worker_client import runtime_supervision
    monkeypatch.setattr(runtime_supervision, "CONFIG", replace(config.CONFIG, app_dir=home))
    monkeypatch.setattr(runtime_supervision, "append_log", lambda *a, **kw: (_ for _ in ()).throw(OSError("database unavailable")))
    try:
        try:
            raise ValueError("PRIVATE_MESSAGE")
        except ValueError as exc:
            result = runtime_supervision.report_unhandled_exception("test_thread", type(exc), exc, exc.__traceback__)
        assert result == {}
        records = [json.loads(line) for line in (home / "incidents/evidence-recorder-failures.jsonl").read_text().splitlines()]
        primary = next(row for row in records if row["origin"] == "worker_unhandled_exception")
        assert primary["exception_type"] == "ValueError"
        assert primary["original_error_code"] == "WORKER_UNHANDLED_EXCEPTION"
        assert "PRIVATE_MESSAGE" not in json.dumps(records)
    finally:
        runtime_supervision.reset_runtime_supervision_for_tests()
