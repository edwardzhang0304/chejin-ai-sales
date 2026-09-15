"""Public API and Qt bridge reproductions. Windows/Qt are explicit boundaries."""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
from pathlib import Path
import zipfile

import pytest

from test_failure_evidence_boundaries import home, package_for, archive_json
from test_web_ui_binding_behavior import _headless_web_ui_module
from chejin_worker_client import storage, incident_evidence
from chejin_worker_client.api import WorkerApiClient, ApiError


@pytest.mark.parametrize("json_response", [True, False])
def test_public_update_check_keeps_body_out_of_diagnostic(home, json_response):
    marker = "synthetic-private-error-body-20260915"
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            assert self.path.startswith('/client-releases/latest?')
            body = (json.dumps({"code": "API_ERROR", "message": marker, "data": None,
                                "trace_id": "review-trace"}).encode() if json_response
                    else ("<html>" + marker + "</html>").encode())
            self.send_response(503)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    api = WorkerApiClient(f"http://127.0.0.1:{server.server_port}")
    try:
        with pytest.raises(ApiError) as error:
            api.latest_client_release(current_version="0.9.81", client_instance_id="review-client")
        package, row = package_for("api_request_failed")
        destination = home / "export.zip"
        incident_evidence.export_diagnostic_bundle(destination, incident_evidence.latest_incident())
        with zipfile.ZipFile(package) as archive:
            members = [name for name in archive.namelist() if marker.encode() in archive.read(name)]
        with zipfile.ZipFile(destination) as archive:
            leaked_latest = marker.encode() in archive.read("logs/latest_logs.json")
        observed = {"entry": "WorkerApiClient.latest_client_release", "status_preserved": error.value.status_code,
                    "body_in_sqlite": marker in json.dumps(row), "body_in_zip_members": members,
                    "body_in_export_latest_logs": leaked_latest}
        (home / "observed.json").write_text(json.dumps(observed, indent=2))
        assert error.value.status_code == 503
        assert marker in str(error.value)  # Original business error is unchanged.
        assert row["metadata"]["frames"]
        assert row["metadata"]["http_status"] == 503
        assert row["metadata"]["exception_type"] == "ApiError"
        if json_response:
            assert row["metadata"]["trace_id"] == "review-trace"
        assert not observed["body_in_sqlite"] and not members and not leaked_latest, observed
    finally:
        api.session.close()
        server.shutdown()
        server.server_close()
        thread.join(2)


@pytest.mark.parametrize("unreadable", [False, True])
def test_public_export_keeps_available_data_when_optional_file_is_unreadable(home, monkeypatch, unreadable):
    storage.append_log("ERROR", "existing_failure", "existing failure")
    original, _ = package_for("existing_failure")
    before = original.read_bytes()
    storage.append_log("INFO", "latest_available_log", "available")
    optional = home / "incidents/evidence-recorder-failures.jsonl"
    optional.write_text('{"event":"evidence_capture_failed"}\n')
    read = Path.read_text
    def disk_read(path, *args, **kwargs):
        if unreadable and path == optional:
            raise PermissionError(13, "controlled single-file read denial", str(path))
        return read(path, *args, **kwargs)
    monkeypatch.setattr(Path, "read_text", disk_read)
    destination = home / "clicked-export.zip"
    with _headless_web_ui_module() as ui:
        monkeypatch.setattr(ui.QFileDialog, "getSaveFileName", staticmethod(lambda *a: (str(destination), "ZIP")), raising=False)
        window = ui.WorkerWebWindow.__new__(ui.WorkerWebWindow)
        window._publish = lambda: None
        errors = []
        window.on_error = errors.append
        result = ui.WorkerWebBridge(window).exportLatestIncident()
    observed = {"entry": "WorkerWebBridge.exportLatestIncident", "unreadable": unreadable,
                "result": result, "errors": errors, "export_exists": destination.is_file(),
                "original_zip_unchanged": original.read_bytes() == before}
    (home / "observed.json").write_text(json.dumps(observed, ensure_ascii=False, indent=2))
    assert result == str(destination), observed
    assert not errors and observed["original_zip_unchanged"]
    assert any(row["event"] == "latest_available_log" for row in archive_json(destination, "logs/latest_logs.json"))
    if unreadable:
        assert archive_json(destination, "evidence-index/export.json")["omissions"]


@pytest.mark.parametrize("include_exception_text", [False, True])
def test_explicit_exception_text_policy_is_honored_by_storage(home, include_exception_text):
    marker = "response-content-only-for-full-policy"
    try:
        raise ValueError(marker)
    except ValueError:
        storage.append_log(
            "WARN", "policy_failure", "safe summary", metadata={"traceback": marker},
            include_exception_text=include_exception_text,
        )
    package, row = package_for("policy_failure")
    assert (marker in json.dumps(row)) is include_exception_text
    with zipfile.ZipFile(package) as archive:
        assert any(marker.encode() in archive.read(name) for name in archive.namelist()) is include_exception_text
    if not include_exception_text:
        assert row["metadata"]["exception_type"] == "ValueError"
        assert row["metadata"]["frames"]


@pytest.mark.parametrize("source_kind", ["runtime_diagnostic", "update_diagnostic", "screenshot", "incident"])
@pytest.mark.parametrize("failure", [PermissionError, FileNotFoundError])
def test_export_uses_same_source_read_policy(home, monkeypatch, source_kind, failure):
    from PIL import Image

    update_root = home / "update"
    monkeypatch.setenv("CHEJIN_UPDATE_STAGING_ROOT", str(update_root))
    update_root.mkdir()
    screenshot = home / "artifacts" / "frame.png"
    screenshot.parent.mkdir()
    Image.new("RGB", (20, 20), "blue").save(screenshot)
    storage.append_log("ERROR", "source_failure", "original", metadata={"screenshot_path": str(screenshot)})
    original, _ = package_for("source_failure")
    original_bytes = original.read_bytes()
    storage.append_log("INFO", "newest_log", "newest")
    runtime = home / "incidents/evidence-recorder-failures.jsonl"
    update = update_root / "worker-startup.jsonl"
    runtime.write_text('{"event":"runtime"}\n')
    update.write_text('{"event":"update"}\n')
    bad = {"runtime_diagnostic": runtime, "update_diagnostic": update,
           "screenshot": screenshot, "incident": original}[source_kind]
    read_text, read_bytes = Path.read_text, Path.read_bytes

    def checked_read(reader):
        def read(path, *args, **kwargs):
            if path == bad:
                raise failure(13, "controlled source read failure", str(path))
            return reader(path, *args, **kwargs)
        return read

    # Only OS reads fail; all diagnostic collection and ZIP writing remain real.
    monkeypatch.setattr(Path, "read_text", checked_read(read_text))
    monkeypatch.setattr(Path, "read_bytes", checked_read(read_bytes))
    exported = incident_evidence.export_diagnostic_bundle(home / "partial.zip", incident_evidence.latest_incident())
    assert any(row["event"] == "newest_log" for row in archive_json(exported, "logs/latest_logs.json"))
    omissions = archive_json(exported, "evidence-index/export.json")["omissions"]
    assert any(row["path"] == str(bad) and row["reason"] == failure.__name__ for row in omissions)
    assert read_bytes(original) == original_bytes
    with zipfile.ZipFile(exported) as archive:
        assert archive.testzip() is None
        if source_kind != "incident":
            assert archive.read(f"incidents/{original.name}") == original_bytes
        if source_kind != "runtime_diagnostic":
            assert "diagnostics/evidence-recorder-failures.jsonl" in archive.namelist()
        if source_kind != "update_diagnostic":
            assert "diagnostics/update/worker-startup.jsonl" in archive.namelist()


@pytest.mark.parametrize("fail_member", ["diagnostics/", "evidence/"])
def test_public_export_reports_target_write_failure_without_replacing_destination(home, monkeypatch, fail_member):
    from PIL import Image

    screenshot = home / "artifacts" / "frame.png"
    screenshot.parent.mkdir()
    Image.new("RGB", (20, 20), "blue").save(screenshot)
    storage.append_log("ERROR", "existing_failure", "original", metadata={"screenshot_path": str(screenshot)})
    original, _ = package_for("existing_failure")
    original_bytes = original.read_bytes()
    (home / "incidents/evidence-recorder-failures.jsonl").write_text('{"event":"diagnostic"}\n')
    destination = home / "export.zip"
    destination.write_bytes(b"previous-user-file")
    real_writestr = zipfile.ZipFile.writestr

    def disk_write(archive, member, data, *args, **kwargs):
        if Path(archive.filename).name.startswith(".export.zip.") and str(member).startswith(fail_member):
            raise OSError(28, "controlled destination write failure")
        return real_writestr(archive, member, data, *args, **kwargs)

    monkeypatch.setattr(zipfile.ZipFile, "writestr", disk_write)
    with _headless_web_ui_module() as ui:
        monkeypatch.setattr(ui.QFileDialog, "getSaveFileName", staticmethod(lambda *args: (str(destination), "ZIP")), raising=False)
        window = ui.WorkerWebWindow.__new__(ui.WorkerWebWindow)
        window._publish = lambda: None
        errors = []
        window.on_error = errors.append
        result = ui.WorkerWebBridge(window).exportLatestIncident()
    assert result == ""
    assert errors == ["故障证据导出失败。"]
    assert destination.read_bytes() == b"previous-user-file"
    assert original.read_bytes() == original_bytes
    assert not list(home.glob(".export.zip.*.tmp"))
