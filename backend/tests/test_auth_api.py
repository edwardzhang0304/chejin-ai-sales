from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from fastapi.routing import APIRoute
from sqlalchemy import select

from app.core.auth import ADMIN_SESSION_COOKIE
from app.core.config import get_settings
from app.core.database import Base, SessionLocal, engine
from app.errors import AppError
from app.main import app
from app.models.audit import OperationLog
from app.models.auth import AdminAccount, AdminSession
from app.models.base import utcnow
from app.services import auth_service
from app.core.auth import require_admin_auth, require_internal_service_auth


pytestmark = pytest.mark.real_auth
ORIGIN = "http://127.0.0.1:5173"
PASSWORD = "correct horse battery staple"


def setup_function():
    app.dependency_overrides.clear()
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)


def _create_account(username: str = "ops", display_name: str = "运营人员", password: str = PASSWORD) -> AdminAccount:
    with SessionLocal() as db:
        account = auth_service.create_account(
            db,
            username=username,
            display_name=display_name,
            password=password,
        )
        db.commit()
        db.refresh(account)
        return account


def _login(
    client: TestClient,
    username: str = "ops",
    password: str = PASSWORD,
    *,
    headers: dict[str, str] | None = None,
):
    return client.post(
        "/api/auth/login",
        json={"username": username, "password": password},
        headers={"Origin": ORIGIN, **(headers or {})},
    )


def test_correct_credentials_create_hashed_session_and_secure_cookie_contract():
    account = _create_account()
    with TestClient(app) as client:
        response = _login(client, headers={"X-Request-Id": "auth-login-success"})

        assert response.status_code == 200
        assert response.json()["data"] == {
            "operator_id": account.id,
            "operator_name": "运营人员",
        }
        assert "role" not in response.json()["data"]
        cookie_header = response.headers["set-cookie"]
        assert "chejin_admin_session=" in cookie_header
        assert "HttpOnly" in cookie_header
        assert "SameSite=strict" in cookie_header
        assert "Path=/api" in cookie_header
        assert "Max-Age=604800" in cookie_header

        raw_token = client.cookies.get(ADMIN_SESSION_COOKIE)
        assert raw_token and raw_token.startswith("cjs_")
        assert raw_token not in response.text
        with SessionLocal() as db:
            stored = db.scalar(select(AdminSession))
            assert stored is not None
            assert stored.token_hash == auth_service.hash_session_token(raw_token)
            assert stored.token_hash != raw_token
            persisted_account = db.get(AdminAccount, account.id)
            assert persisted_account.password_hash.startswith("$argon2id$")
            assert PASSWORD not in persisted_account.password_hash

        session = client.get("/api/auth/session")
        assert session.status_code == 200
        assert session.json()["data"] == response.json()["data"]


def test_wrong_unknown_and_disabled_accounts_return_same_error_without_account_disclosure():
    _create_account()
    disabled = _create_account("disabled", "停用账号")
    with SessionLocal() as db:
        row = db.get(AdminAccount, disabled.id)
        auth_service.set_account_enabled(db, username=row.username_normalized, enabled=False)
        db.commit()

    with TestClient(app) as client:
        responses = [
            _login(client, password="wrong password value"),
            _login(client, username="does-not-exist", password="wrong password value"),
            _login(client, username="disabled", password=PASSWORD),
        ]
    for response in responses:
        assert response.status_code == 401
        assert response.json()["code"] == "AUTH_INVALID_CREDENTIALS"
        assert response.json()["message"] == "账号或密码错误"


def test_account_username_is_unique_after_server_side_normalization():
    _create_account("Ops", "账号一")
    with SessionLocal() as db, pytest.raises(AppError) as exc:
        auth_service.create_account(
            db,
            username="  ＯＰＳ  ",
            display_name="账号二",
            password=PASSWORD,
        )
    assert exc.value.code == "ADMIN_ACCOUNT_DUPLICATED"


@pytest.mark.parametrize(
    "method,path,payload",
    [
        ("GET", "/api/leads", None),
        ("GET", "/api/sales", None),
        ("GET", "/api/workers", None),
        ("GET", "/api/tasks", None),
        ("GET", "/api/operation-logs", None),
        ("GET", "/api/assignment/round-robin-state", None),
        ("GET", "/api/vehicles", None),
        ("GET", "/api/conversations/missing/messages", None),
    ],
)
def test_all_admin_business_surfaces_reject_unauthenticated_requests(method, path, payload):
    with TestClient(app) as client:
        response = client.request(method, path, json=payload, headers={"Origin": ORIGIN})
    assert response.status_code == 401, (path, response.text)
    assert response.json()["code"] == "ADMIN_UNAUTHORIZED"


def test_logout_and_expired_or_revoked_sessions_cannot_be_reused():
    _create_account()
    with TestClient(app) as client:
        assert _login(client).status_code == 200
        raw_token = client.cookies.get(ADMIN_SESSION_COOKIE)
        logout = client.post("/api/auth/logout", headers={"Origin": ORIGIN})
        assert logout.status_code == 200
        assert logout.json()["data"] == {"logged_out": True}
        assert ADMIN_SESSION_COOKIE not in client.cookies
        client.cookies.set(ADMIN_SESSION_COOKIE, raw_token, path="/api")
        assert client.get("/api/auth/session").status_code == 401

    with TestClient(app) as second:
        assert _login(second).status_code == 200
        token = second.cookies.get(ADMIN_SESSION_COOKIE)
        with SessionLocal() as db:
            session = db.scalar(
                select(AdminSession).where(AdminSession.token_hash == auth_service.hash_session_token(token))
            )
            session.idle_expires_at = utcnow() - timedelta(seconds=1)
            db.commit()
        assert second.get("/api/auth/session").status_code == 401


def test_disable_and_password_reset_immediately_invalidate_all_sessions():
    _create_account()
    first = TestClient(app)
    second = TestClient(app)
    try:
        assert _login(first).status_code == 200
        assert _login(second).status_code == 200
        with SessionLocal() as db:
            auth_service.reset_account_password(db, username="ops", password="new correct horse battery")
            db.commit()
        assert first.get("/api/auth/session").status_code == 401
        assert second.get("/api/auth/session").status_code == 401

        assert _login(first, password="new correct horse battery").status_code == 200
        with SessionLocal() as db:
            auth_service.set_account_enabled(db, username="ops", enabled=False)
            db.commit()
        assert first.get("/api/auth/session").status_code == 401
    finally:
        first.close()
        second.close()


def test_all_logged_in_accounts_have_same_vehicle_write_permission():
    _create_account("first", "账号一")
    _create_account("second", "账号二")
    first = TestClient(app)
    second = TestClient(app)
    try:
        assert _login(first, "first").status_code == 200
        created = first.post(
            "/api/vehicles",
            json={"display_name": "同权限测试车"},
            headers={"Origin": ORIGIN},
        )
        assert created.status_code == 200
        vehicle_id = created.json()["data"]["vehicle_code"]

        assert _login(second, "second").status_code == 200
        updated = second.put(
            f"/api/vehicles/{vehicle_id}",
            json={"brand": "账号二可编辑"},
            headers={"Origin": ORIGIN},
        )
        assert updated.status_code == 200
        assert updated.json()["data"]["brand"] == "账号二可编辑"
    finally:
        first.close()
        second.close()


def test_worker_token_and_admin_cookie_are_not_interchangeable():
    _create_account()
    with TestClient(app) as client:
        assert _login(client).status_code == 200
        created = client.post(
            "/api/workers",
            json={"worker_name": "Auth Worker", "device_name": "Windows", "platform": "windows", "enabled": True},
            headers={"Origin": ORIGIN},
        )
        assert created.status_code == 200
        worker = created.json()["data"]

        worker_to_admin = TestClient(app).get(
            "/api/sales", headers={"X-Worker-Token": worker["worker_token"]}
        )
        assert worker_to_admin.status_code == 403
        assert worker_to_admin.json()["code"] == "ADMIN_FORBIDDEN"

        admin_to_worker = client.post(
            f"/api/workers/{worker['id']}/heartbeat",
            json={
                "client_instance_id": "client-auth",
                "run_status": "running",
                "rpa_component_status": "ready",
                "running_status": "idle",
            },
            headers={"Origin": ORIGIN},
        )
        assert admin_to_worker.status_code == 401
        assert admin_to_worker.json()["code"] == "WORKER_TOKEN_INVALID"

        admin_to_internal = client.post(
            "/api/internal/conversations/missing/message-batches/collect",
            json={"trigger_message_event_id": "missing", "trace_id": "auth-test"},
            headers={"Origin": ORIGIN},
        )
        assert admin_to_internal.status_code == 403
        assert admin_to_internal.json()["code"] == "INTERNAL_SERVICE_FORBIDDEN"

        worker_to_internal = TestClient(app).post(
            "/api/internal/conversations/missing/message-batches/collect",
            json={"trigger_message_event_id": "missing", "trace_id": "auth-test"},
            headers={"X-Worker-Token": worker["worker_token"]},
        )
        assert worker_to_internal.status_code == 403
        assert worker_to_internal.json()["code"] == "INTERNAL_SERVICE_FORBIDDEN"


def test_internal_service_endpoint_requires_its_independent_token():
    path = "/api/internal/conversations/missing/message-batches/collect"
    payload = {"trigger_message_event_id": "missing", "trace_id": "auth-test"}
    with TestClient(app) as client:
        missing = client.post(path, json=payload)
        invalid = client.post(path, json=payload, headers={"X-Internal-Service-Token": "invalid"})
        accepted = client.post(
            path,
            json=payload,
            headers={"X-Internal-Service-Token": get_settings().internal_service_token},
        )
    assert missing.status_code == 401
    assert missing.json()["code"] == "INTERNAL_SERVICE_UNAUTHORIZED"
    assert invalid.status_code == 401
    assert invalid.json()["code"] == "INTERNAL_SERVICE_UNAUTHORIZED"
    assert accepted.status_code == 404
    assert accepted.json()["code"] == "CONVERSATION_NOT_ELIGIBLE"


def test_legacy_bearer_and_forged_operator_headers_never_authenticate_browser_requests():
    with TestClient(app) as client:
        response = client.get(
            "/api/sales",
            headers={
                "Authorization": "Bearer former-admin-token",
                "X-Operator-Id": "00000000-0000-0000-0000-000000000001",
                "X-Operator-Name": "Forged Admin",
                "X-Operator-Role": "admin",
            },
        )
    assert response.status_code == 401
    assert response.json()["code"] == "ADMIN_UNAUTHORIZED"


def test_mutating_cookie_requests_require_an_allowed_origin():
    _create_account()
    with TestClient(app) as client:
        missing = client.post("/api/auth/login", json={"username": "ops", "password": PASSWORD})
        assert missing.status_code == 403
        untrusted = client.post(
            "/api/auth/login",
            json={"username": "ops", "password": PASSWORD},
            headers={"Origin": "https://attacker.example"},
        )
        assert untrusted.status_code == 403
        assert _login(client).status_code == 200
        blocked_write = client.post(
            "/api/sales",
            json={"sales_name": "不应创建", "enabled": True},
            headers={"Origin": "https://attacker.example"},
        )
        assert blocked_write.status_code == 403
        assert blocked_write.json()["code"] == "AUTH_ORIGIN_FORBIDDEN"


def test_login_failures_are_rate_limited_by_normalized_username_and_audited_without_secrets():
    _create_account()
    settings = get_settings()
    old_limit = settings.admin_login_username_max_failures
    settings.admin_login_username_max_failures = 2
    secret = "never-log-this-password"
    try:
        with TestClient(app) as client:
            first = _login(client, username=" OPS ", password=secret, headers={"X-Request-Id": "failed-login-1"})
            second = _login(client, username="ops", password=secret, headers={"X-Request-Id": "failed-login-2"})
            blocked = _login(client, username="Ops", password=PASSWORD, headers={"X-Request-Id": "failed-login-3"})
        assert first.status_code == second.status_code == 401
        assert blocked.status_code == 429
        assert blocked.json()["code"] == "AUTH_RATE_LIMITED"
        with SessionLocal() as db:
            logs = list(db.scalars(select(OperationLog).where(OperationLog.event_type == "admin_login_failed")))
            assert len(logs) == 3
            serialized = " ".join(
                str(value)
                for log in logs
                for value in (log.before_data, log.after_data, log.extra_metadata, log.user_agent)
            )
            assert secret not in serialized
            assert all(log.ip_address and log.request_id for log in logs)
            assert {log.extra_metadata["username_normalized"] for log in logs} == {"ops"}
    finally:
        settings.admin_login_username_max_failures = old_limit


def test_login_failures_are_also_rate_limited_by_source_ip_across_usernames():
    _create_account()
    settings = get_settings()
    old_username_limit = settings.admin_login_username_max_failures
    old_ip_limit = settings.admin_login_ip_max_failures
    settings.admin_login_username_max_failures = 100
    settings.admin_login_ip_max_failures = 2
    try:
        with TestClient(app) as client:
            first = _login(client, username="unknown-one", password="wrong password")
            second = _login(client, username="unknown-two", password="wrong password")
            blocked = _login(client, username="ops", password=PASSWORD)
        assert first.status_code == second.status_code == 401
        assert blocked.status_code == 429
        assert blocked.json()["code"] == "AUTH_RATE_LIMITED"
    finally:
        settings.admin_login_username_max_failures = old_username_limit
        settings.admin_login_ip_max_failures = old_ip_limit


def test_health_endpoints_do_not_require_admin_session():
    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200
        assert client.get("/readyz").status_code == 200


def _dependency_calls(route: APIRoute) -> set:
    calls = set()
    pending = [route.dependant]
    while pending:
        dependant = pending.pop()
        if dependant.call:
            calls.add(dependant.call)
        pending.extend(dependant.dependencies)
    return calls


def test_route_inventory_enforces_admin_and_worker_identity_boundaries():
    public_names = {"login", "logout", "raise_internal_error"}
    worker_names = {
        "heartbeat_worker",
        "bind_worker_client",
        "set_worker_run_status",
        "pull_worker_task",
        "scan_result",
        "read_targets",
        "read_authorization",
        "confirm_friend_activation",
        "ingest_messages",
        "get_message_batch",
        "claim_send",
        "sent_ack",
        "claim_task",
        "renew_task_lease",
        "update_step",
        "invite_sent",
        "already_friend",
        "fail_task",
        "add_task_evidence",
    }
    internal_names = {"collect_message_batch", "generate_message_batch"}
    api_routes = [
        route
        for route in app.routes
        if isinstance(route, APIRoute) and route.path.startswith("/api/")
    ]
    for route in api_routes:
        has_admin_session = require_admin_auth in _dependency_calls(route)
        has_internal_service = require_internal_service_auth in _dependency_calls(route)
        if route.name in internal_names:
            assert has_internal_service, f"{route.name} is missing the internal service dependency"
            assert not has_admin_session, f"{route.name} must not accept an admin session"
            continue
        if route.name in public_names or route.name in worker_names:
            assert not has_admin_session, f"{route.name} must not accept an admin session"
        else:
            assert has_admin_session, f"{route.name} is missing the admin session dependency"
