"""Focused checks for password changes and email verification login."""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient


APP_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = APP_ROOT.parents[1]
TEST_ROOT = PROJECT_ROOT / "runtime" / "apps" / "wechat_ai_customer_service" / "test_artifacts" / "auth_security"
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
os.environ.setdefault("WECHAT_CLOUD_REQUIRED", "0")
os.environ.setdefault("WECHAT_CLOUD_STRICT_ONLINE", "0")

from apps.wechat_ai_customer_service.admin_backend.app import create_app as create_local_app  # noqa: E402
from apps.wechat_ai_customer_service.vps_admin.app import create_app as create_vps_app  # noqa: E402


def main() -> int:
    cleanup()
    old_env = snapshot_env()
    results = []
    try:
        check_vps_email_otp_and_password_change()
        results.append({"name": "check_vps_email_otp_and_password_change", "ok": True})
        check_local_email_otp_and_password_change()
        results.append({"name": "check_local_email_otp_and_password_change", "ok": True})
    except Exception as exc:
        results.append({"name": "auth_security", "ok": False, "error": repr(exc)})
    finally:
        restore_env(old_env)
    failures = [item for item in results if not item.get("ok")]
    print(json.dumps({"ok": not failures, "count": len(results), "failures": failures, "results": results}, ensure_ascii=False, indent=2))
    return 0 if not failures else 1


def check_vps_email_otp_and_password_change() -> None:
    set_common_otp_env()
    os.environ["WECHAT_VPS_ADMIN_USERNAME"] = "admin"
    os.environ["WECHAT_VPS_ADMIN_PASSWORD"] = "1234.abcd"
    os.environ["WECHAT_VPS_ADMIN_EMAIL"] = "admin@example.local"
    client = TestClient(create_vps_app(state_path=TEST_ROOT / "vps_state.json"))

    direct = client.post("/v1/auth/login", json={"username": "admin", "password": "1234.abcd"})
    assert_status(direct, 200, "legacy direct login returns initialization state")
    assert_true(direct.json().get("requires_initialization"), "legacy direct login does not bypass initialization")
    alias_direct = client.post("/api/auth/login/start", json={"username": "admin", "password": "1234.abcd"})
    assert_status(alias_direct, 200, "VPS /api auth alias returns initialization state")
    assert_true(alias_direct.json().get("requires_initialization"), "VPS alias does not return Not Found")

    started = client.post("/v1/auth/login/start", json={"username": "admin", "password": "1234.abcd", "device_id": "admin-device"})
    assert_status(started, 200, "start admin initialization")
    assert_true(started.json().get("requires_initialization"), "admin first login requires initialization")
    initialize_vps_account(client, started.json(), email="admin@example.local", new_password="admin.5678", smtp_config={"otp_required": True, "from_email": "admin@example.local"})

    started = client.post("/v1/auth/login/start", json={"username": "admin", "password": "admin.5678", "device_id": "admin-device"})
    assert_status(started, 200, "start admin email login after initialization")
    body = started.json()
    assert_true(body.get("requires_verification"), "admin login requires email verification")
    assert_true(body.get("debug_code"), "debug code is exposed only for local test mode")

    wrong = client.post("/v1/auth/login/verify", json={"challenge_id": body["challenge_id"], "code": "000000"})
    assert_equal(wrong.status_code, 401, "wrong email code is rejected")

    verified = client.post("/v1/auth/login/verify", json={"challenge_id": body["challenge_id"], "code": body["debug_code"], "trust_device": True})
    assert_status(verified, 200, "correct email code creates session")
    token = verified.json()["session"]["token"]

    trusted = client.post("/v1/auth/login/start", json={"username": "admin", "password": "admin.5678", "device_id": "admin-device"})
    assert_status(trusted, 200, "trusted admin device skips OTP")
    assert_true(trusted.json().get("trusted_device"), "trusted device marker returned")

    changed = start_and_verify_password_change(
        client,
        "/v1/auth",
        token,
        current_password="admin.5678",
        new_password="abcd.5678",
    )
    assert_true(changed.get("changed"), "change admin password")

    old_direct_change = client.post(
        "/v1/auth/change-password",
        headers=auth_headers(token),
        json={"current_password": "abcd.5678", "new_password": "abcd.9999"},
    )
    assert_equal(old_direct_change.status_code, 400, "direct password change requires email verification")

    old_started = client.post("/v1/auth/login/start", json={"username": "admin", "password": "1234.abcd"})
    assert_equal(old_started.status_code, 401, "old admin password no longer works")
    new_started = client.post("/v1/auth/login/start", json={"username": "admin", "password": "abcd.5678"})
    assert_status(new_started, 200, "new admin password starts OTP login")

    admin_token = verify_started_login(client, new_started.json())
    smtp = client.get("/v1/admin/security/smtp", headers=auth_headers(admin_token))
    assert_status(smtp, 200, "read SMTP config")
    saved_smtp = client.patch(
        "/v1/admin/security/smtp",
        headers=auth_headers(admin_token),
        json={
            "server": "",
            "port": 465,
            "username": "",
            "password": "",
            "from_email": "admin@example.local",
            "sender_name": "OmniAuto Test",
            "otp_required": True,
            "use_ssl": True,
            "use_tls": False,
            "code_length": 4,
            "ttl_minutes": 15,
            "trusted_device_days": 30,
        },
    )
    assert_status(saved_smtp, 200, "save SMTP config")
    smtp_test = client.post("/v1/admin/security/smtp/test", headers=auth_headers(admin_token), json={"to_email": "admin@example.local"})
    assert_status(smtp_test, 200, "test SMTP config through outbox fallback")

    created_needs_email = client.post(
        "/v1/admin/users",
        headers=auth_headers(admin_token),
        json={"username": "customer_needs_email", "password": "abcd.1234", "role": "customer"},
    )
    assert_status(created_needs_email, 200, "create customer without email")
    needs_email = client.post(
        "/v1/auth/login/start",
        json={"username": "customer_needs_email", "password": "abcd.1234", "tenant_id": "customer_needs_email"},
    )
    assert_status(needs_email, 200, "customer without email enters initialization step")
    assert_true(needs_email.json().get("requires_initialization"), "initialization is requested after password check")
    initialize_vps_account(client, needs_email.json(), email="needs@example.local", new_password="needs.5678")
    needs_login = client.post(
        "/v1/auth/login/start",
        json={"username": "customer_needs_email", "password": "needs.5678", "tenant_id": "customer_needs_email"},
    )
    assert_status(needs_login, 200, "initialized customer starts OTP login")
    bound_token = verify_started_login(client, needs_login.json())
    security = client.get("/v1/auth/security", headers=auth_headers(bound_token))
    assert_status(security, 200, "bound customer security profile")
    assert_equal(security.json()["security"]["email"], "needs@example.local", "email is bound during login")

    created = client.post(
        "/v1/admin/users",
        headers=auth_headers(admin_token),
        json={"username": "customer_secure", "password": "abcd.1234", "email": "customer@example.local", "role": "customer"},
    )
    assert_status(created, 200, "create customer with email")

    customer_started = client.post(
        "/v1/auth/login/start",
        json={"username": "customer_secure", "password": "abcd.1234", "tenant_id": "customer_secure"},
    )
    assert_status(customer_started, 200, "start customer initialization")
    assert_true(customer_started.json().get("requires_initialization"), "new customer must initialize")
    initialize_vps_account(client, customer_started.json(), email="customer@example.local", new_password="customer.5678")
    customer_started = client.post(
        "/v1/auth/login/start",
        json={"username": "customer_secure", "password": "customer.5678", "tenant_id": "customer_secure"},
    )
    assert_status(customer_started, 200, "start customer email login")
    customer_token = verify_started_login(client, customer_started.json())
    customer_changed = start_and_verify_password_change(
        client,
        "/v1/auth",
        customer_token,
        current_password="customer.5678",
        new_password="abcd.9999",
    )
    assert_true(customer_changed.get("changed"), "customer can change own password")

    relaxed_smtp = client.patch(
        "/v1/admin/security/smtp",
        headers=auth_headers(admin_token),
        json={
            "server": "",
            "port": 465,
            "username": "",
            "password": "",
            "from_email": "admin@example.local",
            "sender_name": "OmniAuto Test",
            "otp_required": False,
            "use_ssl": True,
            "use_tls": False,
            "code_length": 4,
            "ttl_minutes": 15,
            "trusted_device_days": 30,
        },
    )
    assert_status(relaxed_smtp, 200, "save relaxed SMTP config")
    admin_still_otp = client.post(
        "/v1/auth/login/start",
        json={"username": "admin", "password": "abcd.5678", "tenant_id": "default", "device_id": "admin-fresh-device"},
    )
    assert_status(admin_still_otp, 200, "admin login still starts OTP when global OTP is relaxed")
    assert_true(admin_still_otp.json().get("requires_verification"), "admin always requires email verification")


def check_local_email_otp_and_password_change() -> None:
    set_common_otp_env()
    os.environ["WECHAT_AUTH_REQUIRED"] = "1"
    os.environ.pop("WECHAT_VPS_BASE_URL", None)
    os.environ["WECHAT_LOCAL_SESSION_PATH"] = str(TEST_ROOT / "local_sessions.json")
    os.environ["WECHAT_LOCAL_ACCOUNTS_STATE_PATH"] = str(TEST_ROOT / "local_accounts.json")
    os.environ["WECHAT_LOCAL_AUTH_CHALLENGE_PATH"] = str(TEST_ROOT / "local_challenges.json")
    os.environ["WECHAT_LOCAL_TRUSTED_DEVICE_PATH"] = str(TEST_ROOT / "local_trusted_devices.json")
    client = TestClient(create_local_app())

    legacy_direct = client.post("/api/auth/login", json={"username": "test01", "password": "1234.abcd", "tenant_id": "default"})
    assert_status(legacy_direct, 200, "legacy local direct login returns initialization state")
    assert_true(legacy_direct.json().get("requires_initialization"), "legacy local direct login does not bypass initialization")
    alias_direct = client.post("/v1/auth/login/start", json={"username": "test01", "password": "1234.abcd", "tenant_id": "default"})
    assert_status(alias_direct, 200, "local /v1 auth alias returns initialization state")
    assert_true(alias_direct.json().get("requires_initialization"), "local alias does not return Not Found")

    started = client.post("/api/auth/login/start", json={"username": "test01", "password": "1234.abcd", "tenant_id": "default", "device_id": "local-device"})
    assert_status(started, 200, "start local initialization")
    assert_true(started.json().get("requires_initialization"), "local customer first login requires initialization")
    initialize_local_account(client, started.json(), email="test01@example.local", new_password="test01.5678")
    started = client.post("/api/auth/login/start", json={"username": "test01", "password": "test01.5678", "tenant_id": "default", "device_id": "local-device"})
    assert_status(started, 200, "start local email login")
    token = verify_started_local_login(client, started.json(), trust_device=True)

    trusted = client.post("/api/auth/login/start", json={"username": "test01", "password": "test01.5678", "tenant_id": "default", "device_id": "local-device"})
    assert_status(trusted, 200, "trusted local device skips OTP")
    assert_true(trusted.json().get("trusted_device"), "trusted local device marker returned")

    changed = start_and_verify_password_change(
        client,
        "/api/auth",
        token,
        current_password="test01.5678",
        new_password="abcd.7777",
    )
    assert_true(changed.get("changed"), "change local customer password")

    old_started = client.post("/api/auth/login/start", json={"username": "test01", "password": "1234.abcd", "tenant_id": "default"})
    assert_equal(old_started.status_code, 401, "old local password no longer works")
    new_started = client.post("/api/auth/login/start", json={"username": "test01", "password": "abcd.7777", "tenant_id": "default"})
    assert_status(new_started, 200, "new local password starts OTP login")

    guest_started = client.post("/api/auth/login/start", json={"username": "guest", "password": "guest-local-dev", "tenant_id": "default"})
    assert_status(guest_started, 200, "start guest initialization")
    assert_true(guest_started.json().get("requires_initialization"), "guest first login requires initialization")
    initialize_local_account(client, guest_started.json(), email="guest@example.local", new_password="guest.5678")
    guest_started = client.post("/api/auth/login/start", json={"username": "guest", "password": "guest.5678", "tenant_id": "default"})
    assert_status(guest_started, 200, "start guest email login")
    guest_token = verify_started_local_login(client, guest_started.json())
    guest_changed = start_and_verify_password_change(
        client,
        "/api/auth",
        guest_token,
        current_password="guest.5678",
        new_password="guest.8888",
    )
    assert_true(guest_changed.get("changed"), "guest can change own password without content-write permission")

    os.environ["WECHAT_EMAIL_OTP_REQUIRED"] = "0"
    os.environ["WECHAT_LOCAL_SESSION_PATH"] = str(TEST_ROOT / "local_admin_sessions.json")
    os.environ["WECHAT_LOCAL_ACCOUNTS_STATE_PATH"] = str(TEST_ROOT / "local_admin_accounts.json")
    os.environ["WECHAT_LOCAL_AUTH_CHALLENGE_PATH"] = str(TEST_ROOT / "local_admin_challenges.json")
    os.environ["WECHAT_LOCAL_TRUSTED_DEVICE_PATH"] = str(TEST_ROOT / "local_admin_trusted_devices.json")
    admin_client = TestClient(create_local_app())
    admin_started = admin_client.post("/api/auth/login/start", json={"username": "admin", "password": "1234.abcd", "tenant_id": "default"})
    assert_equal(admin_started.status_code, 401, "local client admin cannot initialize without VPS")
    assert_true("服务端统一账号" in admin_started.text, "local client admin points to server authorization")
    stale_session_path = Path(os.environ["WECHAT_LOCAL_SESSION_PATH"])
    stale_session_path.parent.mkdir(parents=True, exist_ok=True)
    stale_session_path.write_text(
        json.dumps(
            [
                {
                    "session_id": "stale-local-admin",
                    "token": "stale-local-admin",
                    "user": {"user_id": "local-admin", "role": "admin", "tenant_ids": ["*"], "display_name": "Local Admin", "username": "admin"},
                    "active_tenant_id": "default",
                    "source": "local",
                }
            ],
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    stale_me = admin_client.get("/api/auth/me", headers={"Authorization": "Bearer stale-local-admin"})
    assert_equal(stale_me.status_code, 401, "stale local admin sessions are rejected")


def verify_started_login(client: TestClient, body: dict[str, Any], *, trust_device: bool = False) -> str:
    verified = client.post("/v1/auth/login/verify", json={"challenge_id": body["challenge_id"], "code": body["debug_code"], "trust_device": trust_device})
    assert_status(verified, 200, "verify email login")
    return str(verified.json()["session"]["token"])


def initialize_vps_account(
    client: TestClient,
    body: dict[str, Any],
    *,
    email: str,
    new_password: str,
    smtp_config: dict[str, Any] | None = None,
) -> None:
    started = client.post(
        "/v1/auth/initialize/start",
        json={
            "challenge_id": body["challenge_id"],
            "email": email,
            "new_password": new_password,
            "smtp_config": smtp_config or {},
        },
    )
    assert_status(started, 200, "start account initialization verification")
    payload = started.json()
    assert_true(payload.get("debug_code"), "initialization debug code exists")
    verified = client.post(
        "/v1/auth/initialize/verify",
        json={"challenge_id": payload["challenge_id"], "code": payload["debug_code"]},
    )
    assert_status(verified, 200, "complete account initialization")
    assert_true(verified.json().get("initialized"), "account is initialized")


def verify_started_local_login(client: TestClient, body: dict[str, Any], *, trust_device: bool = False) -> str:
    verified = client.post("/api/auth/login/verify", json={"challenge_id": body["challenge_id"], "code": body["debug_code"], "trust_device": trust_device})
    assert_status(verified, 200, "verify local email login")
    return str(verified.json()["session"]["token"])


def initialize_local_account(client: TestClient, body: dict[str, Any], *, email: str, new_password: str) -> None:
    started = client.post(
        "/api/auth/initialize/start",
        json={"challenge_id": body["challenge_id"], "email": email, "new_password": new_password},
    )
    assert_status(started, 200, "start local account initialization verification")
    payload = started.json()
    assert_true(payload.get("debug_code"), "local initialization debug code exists")
    verified = client.post(
        "/api/auth/initialize/verify",
        json={"challenge_id": payload["challenge_id"], "code": payload["debug_code"]},
    )
    assert_status(verified, 200, "complete local account initialization")
    assert_true(verified.json().get("initialized"), "local account is initialized")


def start_and_verify_password_change(
    client: TestClient,
    auth_prefix: str,
    token: str,
    *,
    current_password: str,
    new_password: str,
) -> dict[str, Any]:
    started = client.post(
        f"{auth_prefix}/change-password/start",
        headers=auth_headers(token),
        json={"current_password": current_password, "new_password": new_password},
    )
    assert_status(started, 200, "start verified password change")
    body = started.json()
    verified = client.post(
        f"{auth_prefix}/change-password/verify",
        headers=auth_headers(token),
        json={"challenge_id": body["challenge_id"], "code": body["debug_code"]},
    )
    assert_status(verified, 200, "verify password change")
    return verified.json()


def set_common_otp_env() -> None:
    os.environ["WECHAT_EMAIL_OTP_REQUIRED"] = "1"
    os.environ["WECHAT_EMAIL_OTP_DEBUG"] = "1"
    os.environ["WECHAT_EMAIL_OUTBOX_PATH"] = str(TEST_ROOT / "email_outbox.jsonl")
    os.environ["WECHAT_VPS_AUTO_DISCOVER"] = "0"


def auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def assert_status(response: Any, expected: int, message: str) -> None:
    assert_equal(response.status_code, expected, f"{message}: {response.text}")


def assert_equal(actual: Any, expected: Any, message: str) -> None:
    if actual != expected:
        raise AssertionError(f"{message}: expected {expected!r}, got {actual!r}")


def assert_true(value: Any, message: str) -> None:
    if not value:
        raise AssertionError(message)


def cleanup() -> None:
    if TEST_ROOT.exists():
        shutil.rmtree(TEST_ROOT)
    TEST_ROOT.mkdir(parents=True, exist_ok=True)


def snapshot_env() -> dict[str, str | None]:
    keys = [
        "WECHAT_AUTH_REQUIRED",
        "WECHAT_VPS_BASE_URL",
        "WECHAT_VPS_AUTO_DISCOVER",
        "WECHAT_VPS_ADMIN_USERNAME",
        "WECHAT_VPS_ADMIN_PASSWORD",
        "WECHAT_VPS_ADMIN_EMAIL",
        "WECHAT_EMAIL_OTP_REQUIRED",
        "WECHAT_EMAIL_OTP_DEBUG",
        "WECHAT_EMAIL_OUTBOX_PATH",
        "WECHAT_LOCAL_SESSION_PATH",
        "WECHAT_LOCAL_ACCOUNTS_STATE_PATH",
        "WECHAT_LOCAL_AUTH_CHALLENGE_PATH",
        "WECHAT_LOCAL_TRUSTED_DEVICE_PATH",
    ]
    return {key: os.environ.get(key) for key in keys}


def restore_env(values: dict[str, str | None]) -> None:
    for key, value in values.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


if __name__ == "__main__":
    raise SystemExit(main())
