"""Credential HTTP/auth/storage contract. All secrets below are synthetic sentinels."""
import json
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.database import Base, SessionLocal, engine
from app.main import app
from app.models.worker import Worker
from app.models.audit import OperationLog
from app.services import auth_service, worker_vision_credential_service as credentials

pytestmark = pytest.mark.real_auth
KEY = "FAKE-VISION-0967-SENTINEL-NOT-A-PROVIDER-KEY"
ORIGIN = {"Origin": "http://127.0.0.1:5173"}


@pytest.fixture
def admin():
    assert engine.url.get_backend_name() == "sqlite" or engine.url.port == 55467
    if engine.url.get_backend_name() == "sqlite":
        Base.metadata.create_all(engine)
    username = "vision-" + uuid.uuid4().hex[:12]
    with SessionLocal() as db:
        auth_service.create_account(db, username=username, display_name="Vision test", password="only isolated test password")
        db.commit()
    with TestClient(app, raise_server_exceptions=False) as client:
        assert client.post("/api/auth/login", headers=ORIGIN, json={"username": username, "password": "only isolated test password"}).status_code == 200
        client.headers.update(ORIGIN)
        yield client


def create(admin, key=KEY):
    response = admin.post("/api/workers", json={"worker_name": "Vision test", "vision_api_key": key})
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    assert KEY not in response.text
    return response.json()["data"]


def bind(admin, worker):
    instance = "instance-" + worker["id"]
    assert admin.post(f"/api/workers/{worker['id']}/client-bind", json={"worker_token": worker["worker_token"], "client_instance_id": instance}).status_code == 200
    return {"X-Worker-Token": worker["worker_token"], "X-Client-Instance-Id": instance}


def path(worker):
    return f"/api/workers/{worker['id']}/vision-credential"


def test_same_key_independent_workers_encrypted_and_never_in_admin_views(admin):
    workers = [create(admin), create(admin)]
    ciphertexts = []
    for worker in workers:
        headers = bind(admin, worker)
        response = admin.get(path(worker), headers=headers)
        assert response.json()["data"]["vision_api_key"] == KEY
        assert response.headers["cache-control"] == "no-store"
        with SessionLocal() as db:
            stored = db.get(Worker, worker["id"])
            assert KEY not in stored.vision_api_key_encrypted
            ciphertexts.append(stored.vision_api_key_encrypted)
            assert stored.vision_credential_updated_by
        for response in (admin.get("/api/workers"), admin.get(f"/api/workers/{worker['id']}")):
            assert KEY not in response.text
            assert all(cipher not in response.text for cipher in ciphertexts)
            assert "vision_api_key" not in response.text
    assert ciphertexts[0] != ciphertexts[1]
    with SessionLocal() as db:
        log_text = json.dumps([row.after_data for row in db.scalars(select(OperationLog))], default=str)
        assert KEY not in log_text
        assert all(cipher not in log_text for cipher in ciphertexts)


@pytest.mark.parametrize("case", ["unbound", "wrong_token", "wrong_instance", "instance_only", "token_only", "other_worker", "disabled", "reset", "deleted", "admin_only"])
def test_credential_read_requires_current_enabled_binding(admin, case):
    worker = create(admin)
    headers = {"X-Worker-Token": worker["worker_token"], "X-Client-Instance-Id": "unbound"}
    if case != "unbound":
        headers = bind(admin, worker)
    if case == "wrong_token": headers["X-Worker-Token"] = "wrong"
    if case == "wrong_instance": headers["X-Client-Instance-Id"] = "wrong"
    if case == "instance_only": headers.pop("X-Worker-Token")
    if case == "token_only": headers.pop("X-Client-Instance-Id")
    if case == "other_worker": headers["X-Worker-Token"] = create(admin)["worker_token"]
    if case == "admin_only": headers = {}
    if case == "disabled": assert admin.post(f"/api/workers/{worker['id']}/disable").status_code == 200
    if case == "reset": assert admin.post(f"/api/workers/{worker['id']}/reset-binding", json={"force": True}).status_code == 200
    if case == "deleted":
        from app.models.base import utcnow
        with SessionLocal() as db:
            db.get(Worker, worker["id"]).deleted_at = utcnow()
            db.commit()
    response = admin.get(path(worker), headers=headers)
    assert response.status_code in {400, 401, 404}
    assert KEY not in response.text
    assert response.headers["cache-control"] == "no-store"


def test_blank_replace_clear_and_worker_cannot_write(admin):
    worker = create(admin, "   ")
    headers = bind(admin, worker)
    assert admin.get(path(worker), headers=headers).json()["data"]["configured"] is False
    assert admin.put(path(worker), json={"vision_api_key": "  " + KEY + "  "}).json()["data"]["vision_configured"]
    for payload in ({}, {"vision_api_key": "  "}, {"vision_api_key": None}):
        assert admin.put(path(worker), json=payload).status_code == 200
        assert admin.get(path(worker), headers=headers).json()["data"]["vision_api_key"] == KEY
    assert admin.put(path(worker), json={"vision_api_key": KEY + "-NEW"}).status_code == 200
    assert admin.get(path(worker), headers=headers).json()["data"]["vision_api_key"] == KEY + "-NEW"
    assert admin.put(path(worker), headers=headers, json={"vision_api_key": KEY}).status_code == 403
    assert admin.delete(path(worker), headers=headers).status_code == 403
    assert admin.delete(path(worker)).json()["data"]["vision_configured"] is False
    assert admin.get(path(worker), headers=headers).json()["data"]["vision_api_key"] is None


@pytest.mark.parametrize("payload", [{"vision_api_key": KEY}, {"worker_name": "x", "vision_api_key": [KEY]}, {"worker_name": "x", "vision_api_key": KEY + "\n"}, {"worker_name": "x", "vision_api_key": KEY * 300}])
def test_validation_does_not_echo_secret(admin, payload, caplog):
    response = admin.post("/api/workers", json=payload)
    assert response.status_code == 400
    assert KEY not in response.text + caplog.text
    assert response.headers["cache-control"] == "no-store"


def test_creation_encryption_failure_rolls_back_without_traceback_secret(admin, monkeypatch, caplog):
    before = len(admin.get("/api/workers").json()["data"]["items"])
    def broken_cipher(): raise RuntimeError(KEY)
    monkeypatch.setattr(credentials, "_cipher", broken_cipher)
    response = admin.post("/api/workers", json={"worker_name": "atomic", "vision_api_key": KEY})
    assert response.status_code == 500
    assert KEY not in response.text + caplog.text
    assert len(admin.get("/api/workers").json()["data"]["items"]) == before


@pytest.mark.parametrize("swap", [False, True])
def test_bad_or_swapped_ciphertext_is_rejected(admin, swap):
    worker = create(admin)
    other = create(admin)
    headers = bind(admin, worker)
    with SessionLocal() as db:
        db.get(Worker, worker["id"]).vision_api_key_encrypted = db.get(Worker, other["id"]).vision_api_key_encrypted if swap else "corrupt"
        db.commit()
    response = admin.get(path(worker), headers=headers)
    assert response.status_code == 503
    assert response.json()["code"] == "VISION_CREDENTIAL_DECRYPT_FAILED"
    assert KEY not in response.text
