from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.core.database import Base, engine
from app.main import app


client = TestClient(app)
HEADERS = {
    "X-Operator-Id": "00000000-0000-0000-0000-000000000001",
    "X-Operator-Name": "Ops Tester",
    "X-Operator-Role": "admin",
}
ADMIN_HEADERS = {**HEADERS, "Authorization": "Bearer test-admin-token"}


def setup_function():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)


def _enable_auth():
    settings = get_settings()
    old_enforcement = settings.auth_enforcement
    old_admin_token = settings.admin_api_token
    settings.auth_enforcement = True
    settings.admin_api_token = "test-admin-token"
    return settings, old_enforcement, old_admin_token


def _restore_auth(settings, old_enforcement, old_admin_token):
    settings.auth_enforcement = old_enforcement
    settings.admin_api_token = old_admin_token


def test_admin_api_requires_admin_token_and_rejects_worker_token():
    settings, old_enforcement, old_admin_token = _enable_auth()
    try:
        missing = client.get("/api/sales", headers=HEADERS)
        assert missing.status_code == 401
        assert missing.json()["code"] == "ADMIN_UNAUTHORIZED"

        worker_token = client.get("/api/sales", headers={"X-Worker-Token": "wkt_leaked"})
        assert worker_token.status_code == 403
        assert worker_token.json()["code"] == "ADMIN_FORBIDDEN"

        allowed = client.get("/api/sales", headers=ADMIN_HEADERS)
        assert allowed.status_code == 200
        assert allowed.json()["code"] == "OK"
    finally:
        _restore_auth(settings, old_enforcement, old_admin_token)


def test_worker_client_bind_does_not_require_admin_token_when_auth_is_enabled():
    settings, old_enforcement, old_admin_token = _enable_auth()
    try:
        created = client.post(
            "/api/workers",
            json={"worker_name": "Windows Worker", "device_name": "Windows PC", "platform": "windows", "enabled": True},
            headers=ADMIN_HEADERS,
        )
        assert created.status_code == 200
        worker = created.json()["data"]

        bound = client.post(
            f"/api/workers/{worker['id']}/client-bind",
            json={"worker_token": worker["worker_token"], "client_instance_id": "client-a"},
        )
        assert bound.status_code == 200
        assert bound.json()["data"]["client_instance_id"] == "client-a"
    finally:
        _restore_auth(settings, old_enforcement, old_admin_token)


def test_task_list_is_admin_api_under_auth_enforcement():
    settings, old_enforcement, old_admin_token = _enable_auth()
    try:
        missing = client.get("/api/tasks")
        assert missing.status_code == 401
        assert missing.json()["code"] == "ADMIN_UNAUTHORIZED"

        worker_token = client.get("/api/tasks", headers={"X-Worker-Token": "wkt_leaked"})
        assert worker_token.status_code == 403
        assert worker_token.json()["code"] == "ADMIN_FORBIDDEN"

        allowed = client.get("/api/tasks", headers=ADMIN_HEADERS)
        assert allowed.status_code == 200
    finally:
        _restore_auth(settings, old_enforcement, old_admin_token)
