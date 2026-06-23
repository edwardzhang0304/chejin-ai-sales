"""Seed the local VPS admin state with the current client data as customer test01."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from fastapi.testclient import TestClient


APP_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = APP_ROOT.parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from apps.wechat_ai_customer_service.knowledge_paths import runtime_app_root  # noqa: E402
from apps.wechat_ai_customer_service.vps_admin.app import create_app  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Seed local VPS admin state with test01 and current client data.")
    parser.add_argument("--tenant-id", default="default")
    parser.add_argument("--state-path", default=str(runtime_app_root() / "vps_admin" / "control_plane.json"))
    parser.add_argument("--admin-username", default=os.getenv("WECHAT_VPS_ADMIN_USERNAME") or "admin")
    parser.add_argument("--admin-password", default=os.getenv("WECHAT_VPS_ADMIN_PASSWORD") or "1234.abcd")
    parser.add_argument("--admin-email", default=os.getenv("WECHAT_VPS_ADMIN_EMAIL") or os.getenv("WECHAT_ADMIN_EMAIL") or "admin@example.local")
    parser.add_argument("--admin-new-password", default=os.getenv("WECHAT_VPS_ADMIN_NEW_PASSWORD") or "")
    parser.add_argument("--email-code", default="", help="Email code to use when debug_code is not enabled.")
    args = parser.parse_args()

    app = create_app(state_path=Path(args.state_path))
    client = TestClient(app)
    token = admin_token(client, args)
    if not token:
        return 1
    headers = {"Authorization": f"Bearer {token}"}

    bootstrap = client.post("/v1/admin/customer-data/bootstrap-test01", headers=headers, json={"tenant_id": args.tenant_id})
    if bootstrap.status_code != 200:
        print(f"bootstrap failed: {bootstrap.status_code} {bootstrap.text}")
        return 1

    shared = client.post("/v1/admin/shared/sync-local", headers=headers, json={})
    if shared.status_code != 200:
        print(f"shared sync failed: {shared.status_code} {shared.text}")
        return 1

    backup = client.post("/v1/admin/backups/local-now", headers=headers, json={"scope": "all", "tenant_id": args.tenant_id})
    if backup.status_code != 200:
        print(f"full backup failed: {backup.status_code} {backup.text}")
        return 1

    payload = {
        "ok": True,
        "user": bootstrap.json()["user"],
        "customer_data_package": bootstrap.json()["package"]["package_id"],
        "shared_snapshot": shared.json()["snapshot"]["snapshot_id"],
        "full_backup": backup.json()["backup"]["backup_id"],
        "state_path": str(Path(args.state_path)),
    }
    print(payload)
    return 0


def admin_token(client: TestClient, args: argparse.Namespace) -> str:
    started = client.post(
        "/v1/auth/login/start",
        json={
            "username": args.admin_username,
            "password": args.admin_password,
            "tenant_id": args.tenant_id,
            "device_id": "seed-vps-test-customer",
            "device_name": "seed_vps_test_customer.py",
        },
    )
    if started.status_code != 200:
        print(f"admin login start failed: {started.status_code} {started.text}")
        return ""
    body = started.json()
    if body.get("requires_initialization"):
        new_password = args.admin_new_password.strip()
        if not new_password:
            print("admin initialization required. Rerun with --admin-new-password and enable WECHAT_EMAIL_OTP_DEBUG=1 or pass --email-code.")
            return ""
        init_started = client.post(
            "/v1/auth/initialize/start",
            json={
                "challenge_id": body["challenge_id"],
                "email": args.admin_email,
                "new_password": new_password,
                "smtp_config": {"otp_required": True, "from_email": args.admin_email},
            },
        )
        if init_started.status_code != 200:
            print(f"admin initialization start failed: {init_started.status_code} {init_started.text}")
            return ""
        init_body = init_started.json()
        code = init_body.get("debug_code") or args.email_code
        if not code:
            print(f"admin initialization code sent to {init_body.get('masked_email')}. Rerun with --email-code <code>.")
            return ""
        init_verified = client.post(
            "/v1/auth/initialize/verify",
            json={"challenge_id": init_body["challenge_id"], "code": code},
        )
        if init_verified.status_code != 200:
            print(f"admin initialization verify failed: {init_verified.status_code} {init_verified.text}")
            return ""
        args.admin_password = new_password
        return admin_token(client, args)

    if body.get("session"):
        return str(body["session"]["token"])

    code = body.get("debug_code") or args.email_code
    if not code:
        print(f"admin login code sent to {body.get('masked_email')}. Rerun with --email-code <code>.")
        return ""
    verified = client.post(
        "/v1/auth/login/verify",
        json={"challenge_id": body["challenge_id"], "code": code, "trust_device": True},
    )
    if verified.status_code != 200:
        print(f"admin login verify failed: {verified.status_code} {verified.text}")
        return ""
    return str(verified.json()["session"]["token"])


if __name__ == "__main__":
    raise SystemExit(main())
