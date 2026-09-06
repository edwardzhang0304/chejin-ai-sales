"""Real PostgreSQL -> admin/bind HTTP -> Worker -> original Vision subprocess HTTP.

Only the provider server is artificial. It receives original Windows fixture bytes.
Thread scheduling is suppressed during start() so no physical Windows UI is invoked.
This is a local credential integration test, not a Windows package/UAT result.
"""
import argparse
import base64
import hashlib
import importlib.util
import json
import io
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
import uuid
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import requests
from sqlalchemy import text

from app.core.database import SessionLocal, engine
from app.models.worker import Worker
from app.services import auth_service

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.real_auth
KEY = "FAKE-VISION-0967-HTTP-SENTINEL"


def unused_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_postgres_http_worker_provider_and_lifecycle(tmp_path, monkeypatch):
    if os.environ.get("CHEJIN_VISION_PG_TEST") != "1":
        pytest.skip("explicit isolated PostgreSQL HTTP test requires CHEJIN_VISION_PG_TEST=1")
    assert engine.url.port == 55467 and engine.url.database == "vision_test"
    assert Path(os.environ["CHEJIN_WORKER_HOME"]).is_relative_to(Path("/private/tmp"))
    sys.path.insert(0, str(ROOT / "worker-client"))
    from chejin_worker_client import storage, vision_credentials as credentials
    from chejin_worker_client.api import WorkerApiClient, ApiError
    from chejin_worker_client.models import Binding
    from chejin_worker_client.omniauto_vision import _CancellableVisionProvider, explicit_vision_config
    from chejin_worker_client.task_runner import TaskRunner
    from chejin_worker_client.rpa_bridge import RpaBridge

    username = "http-" + uuid.uuid4().hex[:12]
    with SessionLocal() as db:
        assert db.scalar(text("select version_num from alembic_version")) == "20260906_0033"
        auth_service.create_account(db, username=username, display_name="HTTP test", password="isolated HTTP password")
        db.commit()
    port = unused_port()
    log_path = tmp_path / "backend.log"
    env = {**os.environ, "PYTHONPATH": str(ROOT / "backend"), "C3_BATCH_RECOVERY_POLL_SECONDS": "0"}
    log = log_path.open("w")
    server = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)], cwd=ROOT / "backend", env=env, stdout=log, stderr=log)
    base = f"http://127.0.0.1:{port}"
    received = []
    class Provider(BaseHTTPRequestHandler):
        def log_message(self, *_): pass
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            received.append((self.path, self.headers.get("x-api-key"), body))
            parsed = {"vision_summary": "测试图片结果不变", "image_ocr_text": [], "classification": {"is_vehicle": False, "vehicle_confidence": 0, "unknown": True, "non_vehicle_reason": "聊天截图"}, "entities": {}, "intent_hints": [], "bridge": {}, "catalog_alignment": {}}
            result = json.dumps({"content": [{"type": "text", "text": json.dumps(parsed, ensure_ascii=False)}]}).encode()
            self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers(); self.wfile.write(result)
    provider = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    provider_thread = threading.Thread(target=provider.serve_forever, daemon=True)
    provider_thread.start()
    try:
        for _ in range(100):
            try:
                if requests.get(base + "/healthz", timeout=.3).status_code == 200: break
            except requests.RequestException: pass
            time.sleep(.1)
        else: pytest.fail("isolated backend did not start")
        admin = requests.Session()
        admin.headers["Origin"] = "http://127.0.0.1:5173"
        assert admin.post(base + "/api/auth/login", json={"username": username, "password": "isolated HTTP password"}).status_code == 200
        workers = []
        for name in ("HTTP-A", "HTTP-B"):
            response = admin.post(base + "/api/workers", json={"worker_name": name, "vision_api_key": KEY})
            assert response.status_code == 200
            assert KEY not in response.text
            workers.append(response.json()["data"])
        api = WorkerApiClient(base + "/api")
        bindings = []
        for worker in workers:
            binding = Binding(worker["id"], worker["worker_token"], "http-" + worker["id"])
            profile = api.bind(binding.worker_id, binding.worker_token, binding.client_instance_id)
            assert profile.client_binding_state == "bound"
            assert api.get_vision_credential(binding) == KEY
            bindings.append(binding)
        with SessionLocal() as db:
            ciphers = [db.get(Worker, worker["id"]).vision_api_key_encrypted for worker in workers]
            assert all(KEY not in cipher for cipher in ciphers)
            assert ciphers[0] != ciphers[1]
        errors = []
        runner = TaskRunner(api, RpaBridge(), on_profile=lambda _: None, on_status=lambda _: None, on_step=lambda _: None, on_task=lambda _: None, on_result=lambda _: None, on_error=errors.append)
        binding = bindings[0]
        storage.save_binding(binding)
        with patch.object(threading.Thread, "start"):
            runner.start(binding)
        assert credentials.resolve_vision_api_key() == KEY
        assert runner.c2_vision_preflight_ready
        # Network destination is the test boundary; config parser and provider
        # subprocess, payload construction and HTTP auth are production code.
        monkeypatch.setenv("CUSTOMER_IMAGE_UNDERSTANDING_BASE_URL", f"http://127.0.0.1:{provider.server_port}/v1")
        config, missing = explicit_vision_config()
        assert not missing
        images = sorted((ROOT / "worker-client/tests/fixtures/avatars_20260904").glob("*.png"))
        assert images
        image_bytes = images[0].read_bytes()
        image = SimpleNamespace(image_bytes=image_bytes, mime_type="image/png", width=0, height=0)
        request = {"image": image, "config": config, "customer_text": "原实机图片的凭据传递验证", "message_id": "credential-http-test"}
        first = _CancellableVisionProvider(None).understand(request)
        assert first.get("applied") is True, first
        assert first["vision_summary"] == "测试图片结果不变"
        assert len(received) == 1 and received[0][1] == KEY
        image_part = next(part for part in received[0][2]["messages"][0]["content"] if part["type"] == "image")
        from PIL import Image
        # The unchanged memory adapter normalizes PNG encoding. Compare pixels,
        # then require byte-identical provider payloads across credential changes.
        with Image.open(io.BytesIO(image_bytes)) as original, Image.open(io.BytesIO(base64.b64decode(image_part["source"]["data"]))) as delivered:
            assert original.size == delivered.size
            assert original.convert("RGB").tobytes() == delivered.convert("RGB").tobytes()
        credential_url = base + f"/api/workers/{binding.worker_id}/vision-credential"
        assert admin.put(credential_url, json={"vision_api_key": KEY + "-NEW"}).status_code == 200
        assert credentials.resolve_vision_api_key() == KEY
        with credentials.vision_credential_snapshot():
            assert runner.set_run_status("paused")
            assert runner.set_run_status("running")
            # This flow continues with its original key despite the new global key.
            second = _CancellableVisionProvider(None).understand(request)
            assert second["vision_summary"] == first["vision_summary"]
            assert received[-1][1] == KEY
        assert credentials.resolve_vision_api_key() == KEY + "-NEW"
        third = _CancellableVisionProvider(None).understand(request)
        assert received[-1][1] == KEY + "-NEW" and len(received) == 3
        assert received[0][2] == received[1][2] == received[2][2]
        storage.append_log("INFO", "vision_redaction_test", "test boundary " + credentials.resolve_vision_api_key(), metadata={"provider_error": credentials.resolve_vision_api_key()})
        runner.stop()
        assert credentials.resolve_vision_api_key() == ""
        with patch.object(threading.Thread, "start"):
            runner.start(binding)
        assert credentials.resolve_vision_api_key() == KEY + "-NEW"
        # Dedicated credential network failure: ordinary run-status/recovery HTTP remains reachable.
        with patch.object(api.session, "get", side_effect=requests.ConnectionError(KEY)):
            assert runner.set_run_status("paused")
            assert runner.set_run_status("running")
        assert credentials.resolve_vision_api_key() == ""
        assert not runner.c2_vision_preflight_ready
        assert binding.run_status == "running" and "VISION_CREDENTIAL_NETWORK_FAILED" in runner.c2_stats["vision_missing_configuration"]
        release = api.latest_client_release(current_version="0.9.66", client_instance_id=binding.client_instance_id)
        assert release.update_available is False
        assert admin.delete(credential_url).status_code == 200
        assert runner.set_run_status("paused") and runner.set_run_status("running")
        assert not runner.c2_vision_preflight_ready
        runner.stop()
        # Fetch is safe even if a malicious error response includes the key.
        assert not errors
        log.flush()
        assert KEY not in log_path.read_text()
        for local in storage.APP_DIR.rglob("*"):
            if local.is_file():
                assert KEY.encode() not in local.read_bytes(), local.name
                assert all(cipher.encode() not in local.read_bytes() for cipher in ciphers), local.name
        collector_path = ROOT / "worker-client/packaging/collect_uat_evidence.py"
        spec = importlib.util.spec_from_file_location("vision_test_collector", collector_path)
        collector = importlib.util.module_from_spec(spec); spec.loader.exec_module(collector)
        package = tmp_path / "unpackaged-local-test"; package.mkdir()
        archive_path = collector.collect(argparse.Namespace(app_dir=str(storage.APP_DIR), package_dir=str(package), output=str(tmp_path / "diagnostics.zip"), from_iso="2026-01-01T00:00:00Z", to_iso="2027-01-01T00:00:00Z"))
        with zipfile.ZipFile(archive_path) as archive:
            for member in archive.namelist():
                assert KEY.encode() not in archive.read(member)
                assert all(cipher.encode() not in archive.read(member) for cipher in ciphers)
        evidence = {"postgres_migration": "20260906_0033", "same_key_workers": 2, "provider_requests": 3, "image_sha256": hashlib.sha256(image_bytes).hexdigest(), "image_source": str(images[0].relative_to(ROOT)), "image_payloads_unchanged": True, "lifecycle": ["bind", "startup", "pause_start", "restart", "network_failure", "clear", "check_update_without_key"], "local_sqlite_and_diagnostics_scan": "passed", "windows_uat": False}
        destination = os.environ.get("CHEJIN_VISION_EVIDENCE_DIR")
        if destination:
            target = Path(destination); target.mkdir(parents=True, exist_ok=True)
            (target / "http-postgres-provider.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2))
    finally:
        credentials.clear_vision_credential()
        provider.shutdown(); provider.server_close(); provider_thread.join(3)
        server.terminate(); server.wait(15); log.close()
