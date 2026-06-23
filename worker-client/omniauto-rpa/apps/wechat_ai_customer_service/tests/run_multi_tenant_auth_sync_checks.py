"""Focused checks for multi-tenant auth/RBAC and VPS-LOCAL sync scaffolding."""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from fastapi.testclient import TestClient


APP_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = APP_ROOT.parents[1]
TEST_ROOT = PROJECT_ROOT / "runtime" / "apps" / "wechat_ai_customer_service" / "test_artifacts" / "multi_tenant_auth_sync"
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
os.environ.setdefault("WECHAT_CLOUD_REQUIRED", "0")
os.environ.setdefault("WECHAT_CLOUD_STRICT_ONLINE", "0")

from apps.wechat_ai_customer_service.admin_backend.app import create_app  # noqa: E402
from apps.wechat_ai_customer_service.auth import AuthContext, AuthSession, AuthUser, Role, can_access  # noqa: E402
from apps.wechat_ai_customer_service.knowledge_paths import active_tenant_id, runtime_knowledge_roots, shared_runtime_cache_root, shared_runtime_snapshot_path, tenant_context, tenant_knowledge_base_root  # noqa: E402
from apps.wechat_ai_customer_service.sync import BackupService, SharedPatchService, VpsLocalSyncService  # noqa: E402
from apps.wechat_ai_customer_service.sync.vps_sync import local_node_cache_path, shared_formal_scan_cache_path  # noqa: E402


_PASSWORD_OVERRIDES: dict[str, str] = {}
WINDOWS_FILE_RETRY_DELAYS = (0.05, 0.1, 0.2, 0.35, 0.5, 0.75, 1.0, 1.25)


def main() -> int:
    cleanup_test_root()
    checks: list[Callable[[], None]] = [
        check_tenant_context,
        check_permission_rules,
        check_dev_mode_compatibility,
        check_strict_auth_and_roles,
        check_backup_manifest_package,
        check_shared_patch_safety,
        check_vps_local_offline_and_mock_command,
        check_vps_poll_uses_cached_node_identity,
        check_shared_cloud_snapshot_pull_with_mock_vps,
        check_expired_shared_cloud_cache_is_not_runtime_root,
        check_formal_shared_candidate_upload_with_mock_vps,
    ]
    results = []
    try:
        for check in checks:
            try:
                check()
                results.append({"name": check.__name__, "ok": True})
            except Exception as exc:
                results.append({"name": check.__name__, "ok": False, "error": repr(exc)})
                break
    finally:
        restore_auth_env()
    failures = [item for item in results if not item.get("ok")]
    print(json.dumps({"ok": not failures, "count": len(results), "failures": failures, "results": results}, ensure_ascii=False, indent=2))
    return 0 if not failures else 1


def check_tenant_context() -> None:
    assert_equal(active_tenant_id(), "default", "default tenant")
    with tenant_context("tenant_alpha"):
        assert_equal(active_tenant_id(), "tenant_alpha", "context tenant")
    assert_equal(active_tenant_id(), "default", "context reset")


def check_permission_rules() -> None:
    admin = context_for(Role.ADMIN, "*", "default")
    customer = context_for(Role.CUSTOMER, "default", "default")
    guest = context_for(Role.GUEST, "default", "default")
    assert_true(can_access(admin, resource="shared_knowledge", action="publish", tenant_id="other"), "admin publishes shared")
    assert_true(can_access(customer, resource="tenant_knowledge", action="write", tenant_id="default"), "customer writes own tenant")
    assert_true(not can_access(customer, resource="tenant_knowledge", action="write", tenant_id="other"), "customer blocked from other tenant")
    assert_true(not can_access(customer, resource="shared_knowledge", action="write", tenant_id="default"), "customer cannot write shared")
    assert_true(can_access(customer, resource="shared_knowledge", action="sync", tenant_id="default"), "customer can upload shared candidates")
    assert_true(can_access(customer, resource="commands", action="execute", tenant_id="default"), "customer can poll own sync commands")
    assert_true(can_access(customer, resource="updates", action="sync", tenant_id="default"), "customer can check own updates")
    assert_true(not can_access(customer, resource="commands", action="execute", tenant_id="other"), "customer cannot poll other tenant commands")
    assert_true(can_access(guest, resource="tenant_knowledge", action="read", tenant_id="default"), "guest reads")
    assert_true(not can_access(guest, resource="tenant_knowledge", action="write", tenant_id="default"), "guest write denied")
    assert_true(not can_access(guest, resource="shared_knowledge", action="sync", tenant_id="default"), "guest cannot upload shared candidates")
    assert_true(not can_access(guest, resource="commands", action="execute", tenant_id="default"), "guest cannot poll commands")


def check_dev_mode_compatibility() -> None:
    set_auth_env(required=False)
    client = TestClient(create_app())
    assert_equal(client.get("/api/auth/me").status_code, 200, "dev auth me")
    assert_equal(client.get("/api/tenants").status_code, 200, "dev tenants")
    status = client.get("/api/sync/status")
    assert_equal(status.status_code, 200, "dev sync status")
    assert_equal(status.json().get("mode"), "offline_unconfigured", "dev sync offline mode")


def check_strict_auth_and_roles() -> None:
    set_auth_env(required=True)
    client = TestClient(create_app())
    assert_equal(client.get("/api/knowledge/overview").status_code, 401, "strict auth blocks anonymous")

    admin_token = seed_admin_session()
    assert_equal(client.get("/api/auth/me", headers=auth_headers(admin_token)).status_code, 200, "admin me")
    assert_equal(client.get("/api/tenants", headers=auth_headers(admin_token, tenant_id="default")).status_code, 200, "admin tenants")

    guest_token = login(client, "guest", "guest-local-dev")
    assert_equal(client.get("/api/knowledge/overview", headers=auth_headers(guest_token)).status_code, 200, "guest read")
    assert_equal(client.post("/api/rag/rebuild", headers=auth_headers(guest_token)).status_code, 403, "guest write blocked")

    customer_token = login(client, "customer", "customer-local-dev")
    assert_equal(client.get("/api/knowledge/overview", headers=auth_headers(customer_token, tenant_id="default")).status_code, 200, "customer own tenant")
    assert_equal(client.get("/api/tenants", headers=auth_headers(customer_token, tenant_id="other_tenant")).status_code, 403, "customer other tenant blocked")
    assert_equal(client.post("/api/sync/commands/poll", headers=auth_headers(customer_token, tenant_id="default")).status_code, 200, "customer can poll own commands")
    assert_equal(client.get("/api/sync/update/check", headers=auth_headers(customer_token, tenant_id="default")).status_code, 200, "customer can check updates")
    assert_equal(client.post("/api/sync/commands/poll", headers=auth_headers(guest_token, tenant_id="default")).status_code, 403, "guest cannot poll commands")


def check_backup_manifest_package() -> None:
    output = TEST_ROOT / "backups"
    result = BackupService(output_root=output).build_backup(scope="tenant", tenant_id="default")
    assert_true(result.get("ok") is True, "backup ok")
    package_path = Path(str(result.get("package_path")))
    assert_true(package_path.exists(), "backup package exists")
    with zipfile.ZipFile(package_path) as package:
        names = set(package.namelist())
        assert_true("manifest.json" in names, "manifest in package")
        manifest = json.loads(package.read("manifest.json").decode("utf-8"))
        assert_equal(manifest.get("tenant_id"), "default", "backup tenant")
        assert_true(any(item.get("path", "").endswith("tenant.json") for item in manifest.get("files", [])), "tenant metadata backed up")


def check_shared_patch_safety() -> None:
    root = TEST_ROOT / "shared_patch_root"
    patch = {
        "schema_version": 1,
        "patch_id": "shared_patch_test",
        "version": "test.1",
        "operations": [
            {
                "op": "upsert_json",
                "path": "global_guidelines/items/test_guideline.json",
                "content": {"schema_version": 1, "id": "test_guideline", "data": {"title": "test"}},
            }
        ],
    }
    service = SharedPatchService(root=root)
    preview = service.preview(patch)
    assert_equal(preview.get("operation_count"), 1, "patch preview count")
    applied = service.apply(patch)
    assert_true(applied.get("ok") is True, "patch apply ok")
    assert_true((root / "global_guidelines" / "items" / "test_guideline.json").exists(), "patch target written")
    risk_patch = {
        "schema_version": 1,
        "patch_id": "shared_patch_risk_control_test",
        "version": "test.2",
        "operations": [
            {
                "op": "upsert_json",
                "path": "risk_control/items/test_risk_control.json",
                "content": {
                    "schema_version": 1,
                    "id": "test_risk_control",
                    "category_id": "risk_control",
                    "status": "active",
                    "data": {
                        "title": "shared risk control test",
                        "keywords": ["risk"],
                        "guideline_text": "handoff",
                    },
                    "runtime": {"requires_handoff": True, "allow_auto_reply": False},
                },
            }
        ],
    }
    risk_applied = service.apply(risk_patch)
    assert_true(risk_applied.get("ok") is True, "risk patch apply ok")
    assert_true((root / "registry.json").exists(), "shared registry auto-created")
    assert_true((root / "risk_control" / "schema.json").exists(), "risk control schema auto-created")
    assert_true((root / "risk_control" / "resolver.json").exists(), "risk control resolver auto-created")
    assert_true((root / "risk_control" / "items" / "test_risk_control.json").exists(), "risk control target written")
    unsafe = {**patch, "operations": [{**patch["operations"][0], "path": "../escape.json"}]}
    try:
        service.preview(unsafe)
    except ValueError:
        return
    raise AssertionError("unsafe shared patch path should be rejected")


def check_vps_local_offline_and_mock_command() -> None:
    service = VpsLocalSyncService(
        vps_base_url="",
        backup_service=BackupService(output_root=TEST_ROOT / "command_backups"),
        shared_patch_service=SharedPatchService(root=TEST_ROOT / "command_shared_root"),
    )
    status = service.status(tenant_id="default")
    assert_equal(status.get("mode"), "offline_unconfigured", "offline status")
    registered = service.register_node(token="customer-token", tenant_id="default", display_name="Default Local")
    assert_equal(registered.get("mode"), "offline_unconfigured", "offline node registration")
    poll = service.poll_commands(tenant_id="default")
    assert_true(poll.get("ok") is True and poll.get("commands") == [], "offline poll explicit")
    formal_sync = service.upload_formal_knowledge_candidates(tenant_id="default", use_llm=False)
    assert_equal(formal_sync.get("mode"), "offline_unconfigured", "offline formal shared sync skips upload")
    result = service.handle_command({"command_id": "cmd_test", "type": "backup_tenant", "tenant_id": "default"}, tenant_id="default")
    assert_true(result.get("accepted") is True, "mock backup command accepted")
    assert_true(Path(result.get("result", {}).get("package_path", "")).exists(), "mock command created package")
    patch_result = service.handle_command(
        {
            "command_id": "cmd_patch",
            "type": "pull_shared_patch",
            "tenant_id": "default",
            "payload": {
                "patch": {
                    "schema_version": 1,
                    "patch_id": "shared_cmd_patch",
                    "version": "cmd.1",
                    "operations": [
                        {
                            "op": "upsert_json",
                            "path": "global_guidelines/items/cmd_patch_guideline.json",
                            "content": {"schema_version": 1, "id": "cmd_patch_guideline", "data": {"title": "Command Patch"}},
                        }
                    ],
                },
                "apply": True,
            },
        },
        tenant_id="default",
    )
    assert_true(patch_result.get("accepted") is True, "shared patch command accepted")
    assert_equal(patch_result.get("result", {}).get("mode"), "cloud_shared_snapshot_refresh", "shared patch command refreshes cloud snapshot")
    assert_equal(patch_result.get("result", {}).get("snapshot", {}).get("mode"), "offline_unconfigured", "offline patch command does not mutate local shared library")
    assert_true(not (TEST_ROOT / "command_shared_root" / "global_guidelines" / "items" / "cmd_patch_guideline.json").exists(), "shared patch command no longer writes local shared library")


def check_vps_poll_uses_cached_node_identity() -> None:
    cache_path = local_node_cache_path()
    old_cache = cache_path.read_text(encoding="utf-8") if cache_path.exists() else None
    old_node_id = os.environ.pop("WECHAT_LOCAL_NODE_ID", None)
    old_node_token = os.environ.pop("WECHAT_LOCAL_NODE_TOKEN", None)
    service = VpsLocalSyncService(vps_base_url="https://vps.example.local")

    class FakeVps:
        configured = True

        def __init__(self) -> None:
            self.gets: list[dict[str, Any]] = []

        def get_json(self, path: str, *, token: str = "", headers: dict[str, str] | None = None) -> dict[str, Any]:
            self.gets.append({"path": path, "token": token, "headers": headers})
            return {"ok": True, "commands": []}

    fake = FakeVps()
    service.vps = fake  # type: ignore[assignment]
    try:
        service.write_node_cache({"node_id": "cached_node_01", "node_token": "cached-node-token"})
        poll = service.poll_commands(tenant_id="default")
        assert_true(poll.get("ok") is True, "cached node poll ok")
        assert_true(fake.gets, "cached node poll calls VPS")
        assert_true("node_id=cached_node_01" in fake.gets[0]["path"], "poll query includes cached node id")
        assert_equal(fake.gets[0]["headers"], {"X-Node-Token": "cached-node-token"}, "poll sends cached node token")
    finally:
        if old_cache is None:
            if cache_path.exists():
                remove_file(cache_path)
        else:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            write_text(cache_path, old_cache)
        if old_node_id is not None:
            os.environ["WECHAT_LOCAL_NODE_ID"] = old_node_id
        if old_node_token is not None:
            os.environ["WECHAT_LOCAL_NODE_TOKEN"] = old_node_token


def check_shared_cloud_snapshot_pull_with_mock_vps() -> None:
    cache_root = shared_runtime_cache_root()
    backup_root = TEST_ROOT / "previous_shared_cloud_cache"
    if backup_root.exists():
        remove_tree(backup_root)
    had_cache = cache_root.exists()
    if had_cache:
        shutil.copytree(cache_root, backup_root)
        remove_tree(cache_root)
    service = VpsLocalSyncService(vps_base_url="https://vps.example.local")

    class FakeVps:
        configured = True

        def __init__(self) -> None:
            self.gets: list[dict[str, Any]] = []
            self.lease_counter = 0

        def get_json(self, path: str, *, token: str = "", headers: dict[str, str] | None = None) -> dict[str, Any]:
            self.gets.append({"path": path, "token": token, "headers": headers})
            self.lease_counter += 1
            now = datetime.now(timezone.utc)
            if "since_version=shared-test-cache.1" in path:
                return {
                    "ok": True,
                    "not_modified": True,
                    "snapshot": {
                        "schema_version": 1,
                        "source": "cloud_official_shared_library",
                        "version": "shared-test-cache.1",
                        "tenant_id": "default",
                        "generated_at": now.isoformat(timespec="seconds"),
                        "issued_at": now.isoformat(timespec="seconds"),
                        "refresh_after_at": (now + timedelta(minutes=5)).isoformat(timespec="seconds"),
                        "expires_at": (now + timedelta(minutes=30)).isoformat(timespec="seconds"),
                        "ttl_seconds": 1800,
                        "refresh_after_seconds": 300,
                        "lease_id": f"lease-renewed-{self.lease_counter}",
                    },
                }
            return {
                "ok": True,
                "snapshot": {
                    "schema_version": 1,
                    "source": "cloud_official_shared_library",
                    "version": "shared-test-cache.1",
                    "tenant_id": "default",
                    "generated_at": now.isoformat(timespec="seconds"),
                    "issued_at": now.isoformat(timespec="seconds"),
                    "refresh_after_at": (now + timedelta(minutes=5)).isoformat(timespec="seconds"),
                    "expires_at": (now + timedelta(minutes=30)).isoformat(timespec="seconds"),
                    "ttl_seconds": 1800,
                    "refresh_after_seconds": 300,
                    "lease_id": "lease-initial",
                    "categories": [{"category_id": "global_guidelines", "item_count": 1}],
                    "items": [
                        {
                            "item_id": "cloud_cache_guideline",
                            "category_id": "global_guidelines",
                            "status": "active",
                            "title": "Cloud Cache Guideline",
                            "content": "Use the cloud official shared cache.",
                            "keywords": ["cloud", "shared"],
                            "data": {"title": "Cloud Cache Guideline", "guideline_text": "Use the cloud official shared cache."},
                        }
                    ],
                },
            }

    fake = FakeVps()
    service.vps = fake  # type: ignore[assignment]
    try:
        result = service.fetch_shared_knowledge_snapshot(tenant_id="default", token="customer-token", force=True)
        assert_true(result.get("ok") is True, "cloud shared snapshot pull ok")
        assert_equal(result.get("snapshot_version"), "shared-test-cache.1", "cloud shared snapshot version")
        assert_true(result.get("cache_valid") is True, "cloud shared snapshot lease is valid")
        assert_true(bool(result.get("expires_at")), "cloud shared snapshot exposes expiry")
        assert_true(fake.gets and fake.gets[0]["path"].startswith("/v1/shared/knowledge?"), "cloud shared endpoint called")
        assert_true((cache_root / "registry.json").exists(), "cloud shared cache registry written")
        assert_true((cache_root / "global_guidelines" / "items" / "cloud_cache_guideline.json").exists(), "cloud shared cache item written")
        renewed = service.fetch_shared_knowledge_snapshot(tenant_id="default", token="customer-token")
        assert_true(renewed.get("not_modified") is True, "not modified response renews local lease")
        assert_true(renewed.get("cache_valid") is True, "renewed cloud shared cache remains valid")
        cached = json.loads(shared_runtime_snapshot_path().read_text(encoding="utf-8"))
        assert_true(str(cached.get("lease_id") or "").startswith("lease-renewed-"), "renewed lease persisted to snapshot cache")
    finally:
        if cache_root.exists():
            remove_tree(cache_root)
        if had_cache and backup_root.exists():
            shutil.copytree(backup_root, cache_root)
        if backup_root.exists():
            remove_tree(backup_root)


def check_expired_shared_cloud_cache_is_not_runtime_root() -> None:
    cache_root = shared_runtime_cache_root()
    backup_root = TEST_ROOT / "expired_shared_cloud_cache_backup"
    if backup_root.exists():
        remove_tree(backup_root)
    had_cache = cache_root.exists()
    if had_cache:
        shutil.copytree(cache_root, backup_root)
        remove_tree(cache_root)
    try:
        now = datetime.now(timezone.utc)
        (cache_root / "global_guidelines" / "items").mkdir(parents=True, exist_ok=True)
        write_json(cache_root / "registry.json", {"schema_version": 1, "categories": [{"id": "global_guidelines", "path": "global_guidelines"}]})
        write_json(
            cache_root / "snapshot.json",
            {
                "schema_version": 1,
                "source": "cloud_official_shared_library",
                "version": "expired-shared-cache",
                "generated_at": (now - timedelta(hours=2)).isoformat(timespec="seconds"),
                "issued_at": (now - timedelta(hours=2)).isoformat(timespec="seconds"),
                "expires_at": (now - timedelta(minutes=30)).isoformat(timespec="seconds"),
                "ttl_seconds": 1800,
                "lease_id": "expired-lease",
            },
        )
        assert_true(cache_root not in runtime_knowledge_roots("default"), "expired cloud shared cache must not be a runtime root")
    finally:
        if cache_root.exists():
            remove_tree(cache_root)
        if had_cache and backup_root.exists():
            shutil.copytree(backup_root, cache_root)
        if backup_root.exists():
            remove_tree(backup_root)


def check_formal_shared_candidate_upload_with_mock_vps() -> None:
    tenant_id = "shared_sync_probe_tenant"
    item_dir = tenant_knowledge_base_root(tenant_id) / "policies" / "items"
    item_path = item_dir / "shared_sync_probe.json"
    private_item_path = item_dir / "shared_sync_private_probe.json"
    cache_path = shared_formal_scan_cache_path()
    old_cache = cache_path.read_text(encoding="utf-8") if cache_path.exists() else None
    service = VpsLocalSyncService(vps_base_url="")

    class FakeVps:
        configured = True

        def __init__(self) -> None:
            self.posts: list[dict[str, Any]] = []

        def post_json(self, path: str, payload: dict[str, Any], *, token: str = "", headers: dict[str, str] | None = None) -> dict[str, Any]:
            self.posts.append({"path": path, "payload": payload, "token": token, "headers": headers})
            return {"ok": True, "proposal": {"proposal_id": payload.get("proposal_id"), "status": "pending_review"}}

    fake = FakeVps()
    service.vps = fake  # type: ignore[assignment]
    try:
        item_dir.mkdir(parents=True, exist_ok=True)
        item_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "id": "shared_sync_probe",
                    "category_id": "policies",
                    "status": "active",
                    "data": {
                        "title": "通用人工转接说明",
                        "answer": "当客户明确要求人工服务时，应说明已经转接人工并请客户稍等。",
                        "keywords": ["人工客服", "转接"],
                        "applicability_scope": "global",
                    },
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        private_item_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "id": "shared_sync_private_probe",
                    "category_id": "policies",
                    "status": "active",
                    "data": {
                        "title": "江苏车金门店跟进规则",
                        "answer": "江苏车金南京门店客户张三手机号13800138000，咨询二手车过户和试驾，由门店销售跟进。",
                        "keywords": ["江苏车金", "南京门店", "二手车"],
                        "applicability_scope": "global",
                    },
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        if cache_path.exists():
            remove_file(cache_path)
        uploaded = service.upload_formal_knowledge_candidates(tenant_id=tenant_id, use_llm=False, limit=10, token="customer-token")
        assert_true(uploaded.get("ok") is True, "formal shared candidate upload ok")
        assert_true(uploaded.get("uploaded"), "mock VPS should receive at least one formal shared candidate")
        assert_equal(len(uploaded.get("uploaded", [])), 1, "private customer formal knowledge must not be uploaded as shared candidate")
        assert_equal(fake.posts[0]["path"], "/v1/shared/proposals", "formal shared upload endpoint")
        assert_true("江苏车金" not in json.dumps(fake.posts[0]["payload"], ensure_ascii=False), "uploaded shared candidate must not contain private customer details")
        assert_true(fake.posts[0]["payload"].get("source_meta", {}).get("source_items"), "upload carries source items")
        uploaded_again = service.upload_formal_knowledge_candidates(tenant_id=tenant_id, use_llm=False, limit=10, token="customer-token")
        assert_equal(uploaded_again.get("uploaded", []), [], "formal shared scan cache prevents duplicate uploads")
    finally:
        for path in (item_path, private_item_path):
            if path.exists():
                remove_file(path)
        tenant_root_path = item_dir.parents[2]
        if tenant_root_path.name == tenant_id and tenant_root_path.exists():
            remove_tree(tenant_root_path)
        if cache_path.exists():
            remove_file(cache_path)
        if old_cache is not None:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            write_text(cache_path, old_cache)


def context_for(role: Role, tenant_scope: str, active_tenant: str) -> AuthContext:
    user = AuthUser(user_id=f"test-{role.value}", role=role, tenant_ids=(tenant_scope,))
    session = AuthSession(session_id=f"sess-{role.value}", user=user, active_tenant_id=active_tenant)
    return AuthContext(session=session, tenant_id=active_tenant)


def login(client: TestClient, username: str, password: str) -> str:
    candidate_passwords = []
    if username in _PASSWORD_OVERRIDES:
        candidate_passwords.append(_PASSWORD_OVERRIDES[username])
    candidate_passwords.append(password)
    for candidate_password in dict.fromkeys(candidate_passwords):
        response = client.post(
            "/api/auth/login/start",
            json={"username": username, "password": candidate_password, "tenant_id": "default", "device_id": f"multi-test-{username}"},
        )
        if response.status_code == 401:
            continue
        assert_equal(response.status_code, 200, f"{username} login start")
        body = response.json()
        if body.get("requires_initialization"):
            new_password = initialized_password(username)
            initialize_local_account(client, body, email=f"{username}@example.local", new_password=new_password)
            _PASSWORD_OVERRIDES[username] = new_password
            return login(client, username, password)
        if body.get("session"):
            token = body.get("session", {}).get("token")
            assert_true(bool(token), f"{username} token")
            return str(token)
        assert_true(bool(body.get("requires_verification")), f"{username} requires verification")
        assert_true(bool(body.get("debug_code")), f"{username} debug code")
        verified = client.post(
            "/api/auth/login/verify",
            json={"challenge_id": body["challenge_id"], "code": body["debug_code"], "trust_device": True},
        )
        assert_equal(verified.status_code, 200, f"{username} login verify")
        token = verified.json().get("session", {}).get("token")
        assert_true(bool(token), f"{username} token")
        return str(token)
    raise AssertionError(f"{username} login failed")


def initialize_local_account(client: TestClient, body: dict[str, Any], *, email: str, new_password: str) -> None:
    started = client.post(
        "/api/auth/initialize/start",
        json={"challenge_id": body["challenge_id"], "email": email, "new_password": new_password},
    )
    assert_equal(started.status_code, 200, "start local initialization")
    payload = started.json()
    assert_true(bool(payload.get("debug_code")), "local initialization debug code")
    verified = client.post(
        "/api/auth/initialize/verify",
        json={"challenge_id": payload["challenge_id"], "code": payload["debug_code"]},
    )
    assert_equal(verified.status_code, 200, "verify local initialization")


def initialized_password(username: str) -> str:
    return {
        "admin": "admin.5678",
        "customer": "customer.5678",
        "guest": "guest.5678",
        "test01": "test01.5678",
    }.get(username, f"{username}.5678")


def seed_admin_session() -> str:
    token = "multi-test-admin-session"
    session = AuthSession(
        session_id=token,
        token=token,
        user=AuthUser(user_id="vps-admin", role=Role.ADMIN, tenant_ids=("*",), display_name="Admin", username="admin"),
        active_tenant_id="default",
        source="vps",
    )
    path = Path(os.environ["WECHAT_LOCAL_SESSION_PATH"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([session.to_dict()], ensure_ascii=False, indent=2), encoding="utf-8")
    return token


def auth_headers(token: str, *, tenant_id: str = "default") -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "X-Tenant-ID": tenant_id}


OLD_AUTH_ENV = {
    "WECHAT_AUTH_REQUIRED": os.environ.get("WECHAT_AUTH_REQUIRED"),
    "WECHAT_VPS_BASE_URL": os.environ.get("WECHAT_VPS_BASE_URL"),
    "WECHAT_VPS_AUTO_DISCOVER": os.environ.get("WECHAT_VPS_AUTO_DISCOVER"),
    "WECHAT_LOCAL_SESSION_PATH": os.environ.get("WECHAT_LOCAL_SESSION_PATH"),
    "WECHAT_LOCAL_ACCOUNTS_STATE_PATH": os.environ.get("WECHAT_LOCAL_ACCOUNTS_STATE_PATH"),
    "WECHAT_LOCAL_AUTH_CHALLENGE_PATH": os.environ.get("WECHAT_LOCAL_AUTH_CHALLENGE_PATH"),
    "WECHAT_LOCAL_TRUSTED_DEVICE_PATH": os.environ.get("WECHAT_LOCAL_TRUSTED_DEVICE_PATH"),
    "WECHAT_EMAIL_OTP_REQUIRED": os.environ.get("WECHAT_EMAIL_OTP_REQUIRED"),
    "WECHAT_EMAIL_OTP_DEBUG": os.environ.get("WECHAT_EMAIL_OTP_DEBUG"),
    "WECHAT_EMAIL_OUTBOX_PATH": os.environ.get("WECHAT_EMAIL_OUTBOX_PATH"),
}


def set_auth_env(*, required: bool) -> None:
    os.environ["WECHAT_AUTH_REQUIRED"] = "1" if required else "0"
    os.environ.pop("WECHAT_VPS_BASE_URL", None)
    os.environ["WECHAT_VPS_AUTO_DISCOVER"] = "0"
    os.environ["WECHAT_LOCAL_SESSION_PATH"] = str(TEST_ROOT / "sessions.json")
    os.environ["WECHAT_LOCAL_ACCOUNTS_STATE_PATH"] = str(TEST_ROOT / "local_accounts.json")
    os.environ["WECHAT_LOCAL_AUTH_CHALLENGE_PATH"] = str(TEST_ROOT / "local_challenges.json")
    os.environ["WECHAT_LOCAL_TRUSTED_DEVICE_PATH"] = str(TEST_ROOT / "local_trusted_devices.json")
    if required:
        os.environ["WECHAT_EMAIL_OTP_REQUIRED"] = "1"
        os.environ["WECHAT_EMAIL_OTP_DEBUG"] = "1"
        os.environ["WECHAT_EMAIL_OUTBOX_PATH"] = str(TEST_ROOT / "email_outbox.jsonl")
    else:
        os.environ.pop("WECHAT_EMAIL_OTP_REQUIRED", None)
        os.environ.pop("WECHAT_EMAIL_OTP_DEBUG", None)
        os.environ.pop("WECHAT_EMAIL_OUTBOX_PATH", None)


def restore_auth_env() -> None:
    for key, value in OLD_AUTH_ENV.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def cleanup_test_root() -> None:
    resolved = TEST_ROOT.resolve()
    expected_parent = (PROJECT_ROOT / "runtime" / "apps" / "wechat_ai_customer_service" / "test_artifacts").resolve()
    if expected_parent not in resolved.parents and resolved != expected_parent:
        raise RuntimeError(f"unsafe test cleanup path: {resolved}")
    if resolved.exists():
        remove_tree(resolved)
    resolved.mkdir(parents=True, exist_ok=True)


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    write_text(path, serialized)


def write_text(path: Path, serialized: str) -> None:
    temp = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    temp.write_text(serialized, encoding="utf-8")
    try:
        replace_path(temp, path)
    finally:
        if temp.exists():
            remove_file(temp)


def is_transient_windows_file_error(exc: OSError) -> bool:
    return (
        os.name == "nt"
        and (
            int(getattr(exc, "errno", 0) or 0) in {13, 22}
            or int(getattr(exc, "winerror", 0) or 0) in {5, 32, 33}
        )
    )


def replace_path(source: Path, destination: Path) -> None:
    last_error: OSError | None = None
    for index, delay in enumerate(WINDOWS_FILE_RETRY_DELAYS):
        try:
            source.replace(destination)
            return
        except FileNotFoundError:
            return
        except OSError as exc:
            last_error = exc
            if not is_transient_windows_file_error(exc):
                raise
            if index < len(WINDOWS_FILE_RETRY_DELAYS) - 1:
                time.sleep(delay)
    if last_error is not None:
        raise last_error


def remove_tree(path: Path) -> None:
    last_error: OSError | None = None
    for index, delay in enumerate(WINDOWS_FILE_RETRY_DELAYS):
        try:
            shutil.rmtree(path)
            return
        except FileNotFoundError:
            return
        except OSError as exc:
            last_error = exc
            if not is_transient_windows_file_error(exc):
                raise
            if index < len(WINDOWS_FILE_RETRY_DELAYS) - 1:
                time.sleep(delay)
    if last_error is not None:
        raise last_error


def remove_file(path: Path) -> None:
    last_error: OSError | None = None
    for index, delay in enumerate(WINDOWS_FILE_RETRY_DELAYS):
        try:
            path.unlink()
            return
        except FileNotFoundError:
            return
        except OSError as exc:
            last_error = exc
            if not is_transient_windows_file_error(exc):
                raise
            if index < len(WINDOWS_FILE_RETRY_DELAYS) - 1:
                time.sleep(delay)
    if last_error is not None:
        raise last_error


def assert_equal(actual: Any, expected: Any, message: str) -> None:
    if actual != expected:
        raise AssertionError(f"{message}: expected {expected!r}, got {actual!r}")


def assert_true(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


if __name__ == "__main__":
    raise SystemExit(main())
