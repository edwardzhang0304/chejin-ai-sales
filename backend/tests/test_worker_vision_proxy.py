"""Real HTTP proxy-trust regression; loopback replaces the host Docker gateway.

Only server startup background jobs are disabled. Authentication, credential
middleware, schema parsing, encryption, database writes and responses are real.
"""
from contextlib import contextmanager
import socket
import threading
import time
import uuid

import httpx
import pytest
import uvicorn

from app.core.database import Base, SessionLocal, engine
from app.main import app, settings
from app.services import auth_service

pytestmark = pytest.mark.real_auth


@contextmanager
def server(trusted):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    config = uvicorn.Config(app, log_level="error", access_log=False, lifespan="off",
                            proxy_headers=True, forwarded_allow_ips=trusted)
    runtime = uvicorn.Server(config)
    thread = threading.Thread(target=runtime.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not runtime.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(.01)
        assert runtime.started
        with httpx.Client(base_url=f"http://127.0.0.1:{sock.getsockname()[1]}", timeout=5) as client:
            yield client
    finally:
        runtime.should_exit = True
        thread.join(10)
        sock.close()
        assert not thread.is_alive()


@pytest.mark.parametrize("trusted,forwarded,status", [
    ("127.0.0.2", "https", 400),  # Before: actual gateway is not trusted.
    ("127.0.0.1", "https", 401),  # After: reaches real authentication.
    ("127.0.0.1", "http", 400),
    ("127.0.0.1", None, 400),
])
def test_credential_gate_uses_only_trusted_proxy(trusted, forwarded, status, monkeypatch):
    monkeypatch.setattr(settings, "environment", "production")
    with server(trusted) as client:
        headers = {"Origin": "http://127.0.0.1:5173"}
        if forwarded:
            headers["X-Forwarded-Proto"] = forwarded
        for method, path in [("POST", "/api/workers"), ("PUT", "/api/workers/missing/vision-credential")]:
            response = client.request(method, path, headers=headers)
            assert response.status_code == status
            assert (response.json()["code"] == "HTTPS_REQUIRED") == (status == 400)
            assert response.headers["cache-control"] == "no-store"


def test_trusted_proxy_login_create_and_encrypted_save(monkeypatch):
    assert engine.url.get_backend_name() == "sqlite"
    Base.metadata.create_all(engine)
    username = "proxy-review-" + uuid.uuid4().hex
    password = "isolated-proxy-review-password"
    key = "FAKE-PROXY-REVIEW-VISION-KEY"
    with SessionLocal() as db:
        auth_service.create_account(db, username=username, display_name="Proxy test", password=password)
        db.commit()
    monkeypatch.setattr(settings, "environment", "production")
    # Test HTTP substitutes for the external TLS transport; no production cookies.
    monkeypatch.setattr(settings, "admin_cookie_secure", False)
    with server("127.0.0.1") as client:
        client.headers.update({"X-Forwarded-Proto": "https", "Origin": "http://127.0.0.1:5173"})
        assert client.post("/api/auth/login", json={"username": username, "password": password}).status_code == 200
        response = client.post("/api/workers", json={"worker_name": "windows测试机", "vision_api_key": key})
        assert response.status_code == 200
        assert key not in response.text
        from app.models.worker import Worker
        from app.services.worker_vision_credential_service import read_credential
        with SessionLocal() as db:
            worker = db.get(Worker, response.json()["data"]["id"])
            assert key not in worker.vision_api_key_encrypted
            assert read_credential(worker)["vision_api_key"] == key
