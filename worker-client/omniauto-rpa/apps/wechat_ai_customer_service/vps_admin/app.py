"""FastAPI entry point for the VPS-side admin control plane."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from apps.wechat_ai_customer_service.auth.models import AuthSession
from apps.wechat_ai_customer_service.auth.session import bearer_token

from .auth import VpsAdminAuthService, current_admin, current_session, get_auth_service
from .services import (
    BackupRestoreService,
    CommandService,
    NodeService,
    ReleaseService,
    SecurityConfigService,
    SharedKnowledgeService,
    TenantService,
    RecorderModuleAssignmentService,
    UserService,
    CustomerDataService,
    OverviewService,
)
from .store import AUDIT_RETENTION_LIMIT, VpsAdminStore


STATIC_ROOT = Path(__file__).resolve().parent / "static"


def create_app(*, state_path: Path | None = None) -> FastAPI:
    store = VpsAdminStore(path=state_path)
    auth = VpsAdminAuthService(store)
    commands = CommandService(store)

    app = FastAPI(
        title="WeChat AI Customer Service VPS Admin",
        version="0.1.0",
        description="VPS-side control plane for hidden platform admin, customer accounts, local nodes, backups, shared knowledge patches, and releases.",
    )
    app.state.vps_admin_store = store
    app.state.vps_admin_auth = auth
    app.state.overview_service = OverviewService(store)
    app.state.tenant_service = TenantService(store)
    app.state.user_service = UserService(store, auth)
    app.state.customer_data_service = CustomerDataService(store, auth)
    app.state.recorder_module_assignment_service = RecorderModuleAssignmentService(store)
    app.state.node_service = NodeService(store)
    app.state.command_service = commands
    app.state.shared_service = SharedKnowledgeService(store)
    app.state.backup_restore_service = BackupRestoreService(store, commands)
    app.state.release_service = ReleaseService(store)
    app.state.security_config_service = SecurityConfigService(store)

    if STATIC_ROOT.exists():
        app.mount("/static", StaticFiles(directory=STATIC_ROOT), name="vps_admin_static")

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC_ROOT / "index.html")

    @app.get("/v1/health")
    def health() -> dict[str, Any]:
        return {"ok": True, "app": "wechat_ai_customer_service_vps_admin", "version": "0.1.0"}

    @app.post("/api/auth/login")
    @app.post("/v1/auth/login")
    def login(payload: dict[str, Any]) -> dict[str, Any]:
        try:
            session = auth.login(
                username=str(payload.get("username") or ""),
                password=str(payload.get("password") or ""),
                tenant_id=str(payload.get("tenant_id") or ""),
            )
        except PermissionError as exc:
            if str(exc) in {"account initialization required", "email verification required"}:
                try:
                    result = auth.start_login(
                        username=str(payload.get("username") or ""),
                        password=str(payload.get("password") or ""),
                        tenant_id=str(payload.get("tenant_id") or ""),
                        device_id=str(payload.get("device_id") or ""),
                        device_name=str(payload.get("device_name") or ""),
                    )
                except PermissionError as start_exc:
                    raise HTTPException(status_code=401, detail=str(start_exc)) from start_exc
                return {"ok": True, **result}
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        return {"ok": True, "session": session.to_dict()}

    @app.post("/api/auth/login/start")
    @app.post("/v1/auth/login/start")
    def start_login(payload: dict[str, Any], request: Request) -> dict[str, Any]:
        try:
            result = auth.start_login(
                username=str(payload.get("username") or ""),
                password=str(payload.get("password") or ""),
                tenant_id=str(payload.get("tenant_id") or ""),
                device_id=str(payload.get("device_id") or ""),
                device_name=str(payload.get("device_name") or ""),
                ip_address=request.client.host if request.client else "",
            )
        except PermissionError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, **result}

    @app.post("/api/auth/login/bind-email/start")
    @app.post("/v1/auth/login/bind-email/start")
    def start_login_email_binding(payload: dict[str, Any]) -> dict[str, Any]:
        try:
            return {
                "ok": True,
                **auth.start_login_email_binding(
                    challenge_id=str(payload.get("challenge_id") or ""),
                    email=str(payload.get("email") or ""),
                ),
            }
        except PermissionError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/auth/login/verify")
    @app.post("/v1/auth/login/verify")
    def verify_login(payload: dict[str, Any]) -> dict[str, Any]:
        try:
            session = auth.verify_login(
                challenge_id=str(payload.get("challenge_id") or ""),
                code=str(payload.get("code") or ""),
                trust_device=bool(payload.get("trust_device")),
            )
        except PermissionError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        return {"ok": True, "session": session.to_dict()}

    @app.post("/api/auth/initialize/start")
    @app.post("/v1/auth/initialize/start")
    def start_account_initialization(payload: dict[str, Any]) -> dict[str, Any]:
        try:
            return {
                "ok": True,
                **auth.start_account_initialization(
                    challenge_id=str(payload.get("challenge_id") or ""),
                    email=str(payload.get("email") or ""),
                    new_password=str(payload.get("new_password") or ""),
                    smtp_config=payload.get("smtp_config") if isinstance(payload.get("smtp_config"), dict) else None,
                ),
            }
        except PermissionError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/auth/initialize/verify")
    @app.post("/v1/auth/initialize/verify")
    def verify_account_initialization(payload: dict[str, Any]) -> dict[str, Any]:
        try:
            return {
                "ok": True,
                **auth.verify_account_initialization(
                    challenge_id=str(payload.get("challenge_id") or ""),
                    code=str(payload.get("code") or ""),
                ),
            }
        except PermissionError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc

    @app.get("/v1/auth/me")
    def me(session: AuthSession = Depends(current_session)) -> dict[str, Any]:
        return {"ok": True, "session": session.to_dict()}

    @app.post("/v1/auth/change-password")
    def change_password(payload: dict[str, Any], session: AuthSession = Depends(current_session)) -> dict[str, Any]:
        try:
            return {
                "ok": True,
                **auth.change_password(
                    session,
                    current_password=str(payload.get("current_password") or ""),
                    new_password=str(payload.get("new_password") or ""),
                ),
            }
        except PermissionError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/v1/auth/security")
    def security_profile(session: AuthSession = Depends(current_session)) -> dict[str, Any]:
        return {"ok": True, "security": auth.security_profile(session)}

    @app.post("/v1/auth/email/start")
    def start_email_binding(payload: dict[str, Any], session: AuthSession = Depends(current_session)) -> dict[str, Any]:
        try:
            return {"ok": True, **auth.start_email_binding(session, email=str(payload.get("email") or ""))}
        except PermissionError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/v1/auth/email/verify")
    def verify_email_binding(payload: dict[str, Any], session: AuthSession = Depends(current_session)) -> dict[str, Any]:
        try:
            return {
                "ok": True,
                **auth.verify_email_binding(
                    session,
                    challenge_id=str(payload.get("challenge_id") or ""),
                    code=str(payload.get("code") or ""),
                ),
            }
        except PermissionError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/v1/auth/change-password/start")
    def start_password_change(payload: dict[str, Any], session: AuthSession = Depends(current_session)) -> dict[str, Any]:
        try:
            return {
                "ok": True,
                **auth.start_password_change(
                    session,
                    current_password=str(payload.get("current_password") or ""),
                    new_password=str(payload.get("new_password") or ""),
                ),
            }
        except PermissionError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/v1/auth/change-password/verify")
    def verify_password_change(payload: dict[str, Any], session: AuthSession = Depends(current_session)) -> dict[str, Any]:
        try:
            return {
                "ok": True,
                **auth.verify_password_change(
                    session,
                    challenge_id=str(payload.get("challenge_id") or ""),
                    code=str(payload.get("code") or ""),
                ),
            }
        except PermissionError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/v1/admin/security/smtp")
    def get_smtp_config(_: AuthSession = Depends(current_admin)) -> dict[str, Any]:
        return {"ok": True, "smtp": app.state.security_config_service.smtp_config()}

    @app.patch("/v1/admin/security/smtp")
    def update_smtp_config(payload: dict[str, Any], actor: AuthSession = Depends(current_admin)) -> dict[str, Any]:
        return {"ok": True, "smtp": app.state.security_config_service.update_smtp_config(payload, actor=actor)}

    @app.post("/v1/admin/security/smtp/test")
    def test_smtp_config(payload: dict[str, Any], actor: AuthSession = Depends(current_admin)) -> dict[str, Any]:
        return {"ok": True, **app.state.security_config_service.test_smtp(payload, actor=actor)}

    @app.get("/v1/admin/tenants")
    def list_tenants(_: AuthSession = Depends(current_admin)) -> dict[str, Any]:
        return {"ok": True, "tenants": app.state.tenant_service.list_tenants()}

    @app.get("/v1/admin/overview")
    def admin_overview(_: AuthSession = Depends(current_admin)) -> dict[str, Any]:
        return {"ok": True, **app.state.overview_service.overview()}

    @app.post("/v1/admin/tenants")
    def create_tenant(payload: dict[str, Any], actor: AuthSession = Depends(current_admin)) -> dict[str, Any]:
        return {"ok": True, "tenant": app.state.tenant_service.create_tenant(payload, actor=actor)}

    @app.patch("/v1/admin/tenants/{tenant_id}")
    def update_tenant(tenant_id: str, payload: dict[str, Any], actor: AuthSession = Depends(current_admin)) -> dict[str, Any]:
        return {"ok": True, "tenant": app.state.tenant_service.update_tenant(tenant_id, payload, actor=actor)}

    @app.get("/v1/admin/users")
    def list_users(_: AuthSession = Depends(current_admin)) -> dict[str, Any]:
        return {"ok": True, "users": app.state.user_service.list_users()}

    @app.get("/v1/admin/industries")
    def list_industries(_: AuthSession = Depends(current_admin)) -> dict[str, Any]:
        return {"ok": True, "industries": app.state.shared_service.industry_catalog()}

    @app.post("/v1/admin/users")
    def create_user(payload: dict[str, Any], actor: AuthSession = Depends(current_admin)) -> dict[str, Any]:
        return {"ok": True, "user": app.state.user_service.create_user(payload, actor=actor)}

    @app.get("/v1/admin/recorder-modules")
    def list_recorder_modules(
        include_inactive: bool = Query(default=True),
        _: AuthSession = Depends(current_admin),
    ) -> dict[str, Any]:
        return {"ok": True, "items": app.state.recorder_module_assignment_service.list_modules(include_inactive=include_inactive)}

    @app.post("/v1/admin/recorder-modules")
    def upsert_recorder_module(payload: dict[str, Any], actor: AuthSession = Depends(current_admin)) -> dict[str, Any]:
        return {"ok": True, "item": app.state.recorder_module_assignment_service.upsert_module(payload, actor=actor)}

    @app.get("/v1/admin/recorder-module-bindings")
    def list_recorder_module_bindings(
        scope_type: str = Query(default=""),
        scope_id: str = Query(default=""),
        tenant_id: str = Query(default=""),
        user_id: str = Query(default=""),
        _: AuthSession = Depends(current_admin),
    ) -> dict[str, Any]:
        return {
            "ok": True,
            "items": app.state.recorder_module_assignment_service.list_bindings(
                scope_type=scope_type,
                scope_id=scope_id,
                tenant_id=tenant_id,
                user_id=user_id,
            ),
        }

    @app.post("/v1/admin/recorder-module-bindings")
    def upsert_recorder_module_binding(payload: dict[str, Any], actor: AuthSession = Depends(current_admin)) -> dict[str, Any]:
        return {"ok": True, "item": app.state.recorder_module_assignment_service.upsert_binding(payload, actor=actor)}

    @app.delete("/v1/admin/recorder-module-bindings/{binding_id}")
    def delete_recorder_module_binding(binding_id: str, actor: AuthSession = Depends(current_admin)) -> dict[str, Any]:
        deleted = app.state.recorder_module_assignment_service.delete_binding(binding_id, actor=actor)
        if not deleted:
            raise HTTPException(status_code=404, detail=f"recorder module binding not found: {binding_id}")
        return {"ok": True, "deleted": True}

    @app.patch("/v1/admin/users/{user_id}")
    def update_user(user_id: str, payload: dict[str, Any], actor: AuthSession = Depends(current_admin)) -> dict[str, Any]:
        return {"ok": True, "user": app.state.user_service.update_user(user_id, payload, actor=actor)}

    @app.delete("/v1/admin/users/{user_id}")
    def delete_user(user_id: str, actor: AuthSession = Depends(current_admin)) -> dict[str, Any]:
        return {"ok": True, "user": app.state.user_service.delete_user(user_id, actor=actor)}

    @app.get("/v1/admin/customer-data")
    def list_customer_data(_: AuthSession = Depends(current_admin)) -> dict[str, Any]:
        return {"ok": True, "packages": app.state.customer_data_service.list_packages()}

    @app.post("/v1/admin/customer-data")
    def register_customer_data(payload: dict[str, Any], actor: AuthSession = Depends(current_admin)) -> dict[str, Any]:
        return {"ok": True, "package": app.state.customer_data_service.register_package(payload, actor=actor)}

    @app.post("/v1/admin/customer-data/package-customer")
    def package_customer_data(payload: dict[str, Any], actor: AuthSession = Depends(current_admin)) -> dict[str, Any]:
        return {"ok": True, "package": app.state.customer_data_service.package_for_customer(payload, actor=actor)}

    @app.get("/v1/admin/customer-data/{package_id}/download")
    def download_customer_data(package_id: str, _: AuthSession = Depends(current_admin)) -> FileResponse:
        package_path = app.state.customer_data_service.package_path(package_id)
        return FileResponse(package_path, filename=package_path.name, media_type="application/zip")

    @app.get("/v1/admin/customer-data/{package_id}/readable-download")
    def download_customer_readable_data(package_id: str, _: AuthSession = Depends(current_admin)) -> FileResponse:
        export_path = app.state.customer_data_service.readable_export_path(package_id)
        return FileResponse(
            export_path,
            filename=export_path.name,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

    @app.get("/v1/admin/customer-data/{package_id}")
    def get_customer_data(package_id: str, _: AuthSession = Depends(current_admin)) -> dict[str, Any]:
        return {"ok": True, "package": app.state.customer_data_service.get_package(package_id)}

    @app.delete("/v1/admin/customer-data/{package_id}")
    def delete_customer_data(package_id: str, actor: AuthSession = Depends(current_admin)) -> dict[str, Any]:
        return {"ok": True, **app.state.customer_data_service.delete_package(package_id, actor=actor)}

    @app.post("/v1/admin/customer-data/bootstrap-test01")
    def bootstrap_test01_customer(payload: dict[str, Any] | None = None, actor: AuthSession = Depends(current_admin)) -> dict[str, Any]:
        tenant_id = (payload or {}).get("tenant_id") or "default"
        return {"ok": True, **app.state.customer_data_service.bootstrap_test_customer(actor=actor, tenant_id=str(tenant_id))}

    @app.get("/v1/admin/nodes")
    def list_nodes(_: AuthSession = Depends(current_admin)) -> dict[str, Any]:
        return {"ok": True, "nodes": app.state.node_service.list_nodes()}

    @app.get("/v1/admin/commands")
    def list_commands(_: AuthSession = Depends(current_admin)) -> dict[str, Any]:
        return {"ok": True, "commands": app.state.command_service.list_commands()}

    @app.post("/v1/admin/commands")
    def create_command(payload: dict[str, Any], actor: AuthSession = Depends(current_admin)) -> dict[str, Any]:
        return {"ok": True, "command": app.state.command_service.create_command(payload, actor=actor)}

    @app.get("/v1/admin/shared/proposals")
    def list_shared_proposals(_: AuthSession = Depends(current_admin)) -> dict[str, Any]:
        return {"ok": True, "proposals": app.state.shared_service.list_proposals()}

    @app.get("/v1/admin/shared/library")
    def list_shared_library(
        include_inactive: bool = Query(default=False),
        industry_id: str = Query(default=""),
        _: AuthSession = Depends(current_admin),
    ) -> dict[str, Any]:
        return {"ok": True, "items": app.state.shared_service.list_library_items(include_inactive=include_inactive, industry_id=industry_id)}

    @app.post("/v1/admin/shared/library")
    def create_shared_library_item(payload: dict[str, Any], actor: AuthSession = Depends(current_admin)) -> dict[str, Any]:
        return {"ok": True, "item": app.state.shared_service.create_library_item(payload, actor=actor)}

    @app.post("/v1/admin/shared/library/draft")
    def draft_shared_library_item(payload: dict[str, Any], actor: AuthSession = Depends(current_admin)) -> dict[str, Any]:
        description = str((payload or {}).get("description") or (payload or {}).get("text") or "")
        try:
            result = app.state.shared_service.draft_from_natural_language(
                description,
                actor=actor,
                use_llm=bool((payload or {}).get("use_llm", True)),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "ok": True,
            **result,
        }

    @app.get("/v1/admin/shared/library/{item_id}")
    def get_shared_library_item(item_id: str, _: AuthSession = Depends(current_admin)) -> dict[str, Any]:
        return {"ok": True, "item": app.state.shared_service.get_library_item(item_id)}

    @app.patch("/v1/admin/shared/library/{item_id}")
    def update_shared_library_item(item_id: str, payload: dict[str, Any], actor: AuthSession = Depends(current_admin)) -> dict[str, Any]:
        return {"ok": True, "item": app.state.shared_service.update_library_item(item_id, payload, actor=actor)}

    @app.delete("/v1/admin/shared/library/{item_id}")
    def delete_shared_library_item(item_id: str, actor: AuthSession = Depends(current_admin)) -> dict[str, Any]:
        return {"ok": True, "item": app.state.shared_service.delete_library_item(item_id, actor=actor)}

    @app.get("/v1/admin/shared/overview")
    def shared_overview(_: AuthSession = Depends(current_admin)) -> dict[str, Any]:
        return {"ok": True, **app.state.shared_service.overview()}

    @app.post("/v1/admin/shared/sync-local")
    def sync_local_shared(actor: AuthSession = Depends(current_admin)) -> dict[str, Any]:
        return {"ok": True, "snapshot": app.state.shared_service.sync_local_snapshot(actor=actor)}

    @app.post("/v1/admin/shared/proposals/generate-from-formal")
    def generate_shared_proposals_from_formal(payload: dict[str, Any] | None = None, actor: AuthSession = Depends(current_admin)) -> dict[str, Any]:
        return {"ok": True, **app.state.shared_service.generate_universal_proposals(payload or {}, actor=actor)}

    @app.post("/v1/admin/shared/proposals/{proposal_id}/review")
    def review_shared_proposal(proposal_id: str, payload: dict[str, Any], actor: AuthSession = Depends(current_admin)) -> dict[str, Any]:
        return {"ok": True, **app.state.shared_service.review_proposal(proposal_id, payload, actor=actor)}

    @app.post("/v1/admin/shared/proposals/{proposal_id}/review-assist")
    def refresh_shared_proposal_review_assist(proposal_id: str, payload: dict[str, Any] | None = None, actor: AuthSession = Depends(current_admin)) -> dict[str, Any]:
        return {"ok": True, **app.state.shared_service.refresh_proposal_review_assist(proposal_id, payload or {}, actor=actor)}

    @app.get("/v1/admin/shared/patches")
    def list_shared_patches(_: AuthSession = Depends(current_admin)) -> dict[str, Any]:
        return {"ok": True, "patches": app.state.shared_service.list_patches(limit=10)}

    @app.post("/v1/admin/shared/patches/{patch_id}/push")
    def push_shared_patch(patch_id: str, payload: dict[str, Any] | None = None, actor: AuthSession = Depends(current_admin)) -> dict[str, Any]:
        return {"ok": True, **app.state.shared_service.push_patch(patch_id, payload or {}, actor=actor)}

    @app.post("/v1/admin/backups")
    def request_backup(payload: dict[str, Any], actor: AuthSession = Depends(current_admin)) -> dict[str, Any]:
        return {"ok": True, **app.state.backup_restore_service.request_backup(payload, actor=actor)}

    @app.get("/v1/admin/backups")
    def list_backups(_: AuthSession = Depends(current_admin)) -> dict[str, Any]:
        return {"ok": True, "items": app.state.backup_restore_service.list_backup_requests()}

    @app.delete("/v1/admin/backups/{request_id}")
    def delete_backup(request_id: str, actor: AuthSession = Depends(current_admin)) -> dict[str, Any]:
        return {"ok": True, **app.state.backup_restore_service.delete_backup_request(request_id, actor=actor)}

    @app.post("/v1/admin/backups/local-now")
    def build_local_backup_now(payload: dict[str, Any], actor: AuthSession = Depends(current_admin)) -> dict[str, Any]:
        return {"ok": True, **app.state.backup_restore_service.build_local_backup_now(payload, actor=actor)}

    @app.post("/v1/admin/restores")
    def request_restore(payload: dict[str, Any], actor: AuthSession = Depends(current_admin)) -> dict[str, Any]:
        return {"ok": True, **app.state.backup_restore_service.request_restore(payload, actor=actor)}

    @app.get("/v1/admin/restores")
    def list_restores(_: AuthSession = Depends(current_admin)) -> dict[str, Any]:
        return {"ok": True, "items": app.state.backup_restore_service.list_restore_requests()}

    @app.post("/v1/admin/restores/latest")
    def restore_latest(payload: dict[str, Any], actor: AuthSession = Depends(current_admin)) -> dict[str, Any]:
        return {"ok": True, **app.state.backup_restore_service.request_restore_latest(payload, actor=actor)}

    @app.get("/v1/admin/releases")
    def list_releases(_: AuthSession = Depends(current_admin)) -> dict[str, Any]:
        return {"ok": True, "releases": app.state.release_service.list_releases()}

    @app.post("/v1/admin/releases")
    def create_release(payload: dict[str, Any], actor: AuthSession = Depends(current_admin)) -> dict[str, Any]:
        return {"ok": True, "release": app.state.release_service.create_release(payload, actor=actor)}

    @app.post("/v1/admin/releases/{release_id}/push")
    def push_release(release_id: str, payload: dict[str, Any] | None = None, actor: AuthSession = Depends(current_admin)) -> dict[str, Any]:
        return {"ok": True, **app.state.release_service.push_release(release_id, payload or {}, actor=actor)}

    @app.get("/v1/admin/audit")
    def list_audit(_: AuthSession = Depends(current_admin)) -> dict[str, Any]:
        def prune(state: dict[str, Any]) -> list[dict[str, Any]]:
            state["audit_events"] = state.get("audit_events", [])[-AUDIT_RETENTION_LIMIT:]
            return list(reversed(state["audit_events"]))

        return {"ok": True, "events": store.update(prune)}

    @app.post("/v1/local/nodes/register")
    def register_node(
        payload: dict[str, Any],
        request: Request,
        x_enrollment_token: str = Header(default=""),
        authorization: str = Header(default=""),
    ) -> dict[str, Any]:
        auth_service = get_auth_service(request)
        token = bearer_token(authorization)
        session = auth_service.resolve_token(token) if token else None
        if session is None:
            auth_service.require_node_enrollment(x_enrollment_token)
        actor_id = session.user.user_id if session else "local-node"
        return {"ok": True, "node": app.state.node_service.register(payload, actor_id=actor_id)}

    @app.post("/v1/local/nodes/{node_id}/heartbeat")
    def node_heartbeat(node_id: str, payload: dict[str, Any], request: Request, x_node_token: str = Header(default="")) -> dict[str, Any]:
        require_node_token(request, node_id=node_id, token=x_node_token)
        return {"ok": True, "node": app.state.node_service.heartbeat(node_id, payload)}

    @app.get("/v1/local/commands")
    def poll_commands(
        request: Request,
        tenant_id: str = Query(default="default"),
        node_id: str = Query(default=""),
        x_node_token: str = Header(default=""),
        authorization: str = Header(default=""),
    ) -> dict[str, Any]:
        authorize_local_endpoint(request, authorization=authorization, node_id=node_id, node_token=x_node_token)
        return {"ok": True, "commands": app.state.command_service.poll(tenant_id=tenant_id, node_id=node_id)}

    @app.get("/v1/local/recorder-module-bindings")
    def local_recorder_module_bindings(
        request: Request,
        tenant_id: str = Query(default="default"),
        user_id: str = Query(default=""),
        node_id: str = Query(default=""),
        x_node_token: str = Header(default=""),
        authorization: str = Header(default=""),
    ) -> dict[str, Any]:
        authorize_local_endpoint(request, authorization=authorization, node_id=node_id, node_token=x_node_token)
        effective_user_id = str(user_id or "").strip()
        if not effective_user_id and authorization:
            session = get_auth_service(request).resolve_token(bearer_token(authorization))
            if session is not None:
                effective_user_id = str(session.user.user_id or "").strip()
        snapshot = app.state.recorder_module_assignment_service.local_sync_snapshot(
            tenant_id=tenant_id,
            user_id=effective_user_id,
        )
        return {"ok": True, "snapshot": snapshot}

    @app.post("/v1/local/commands/{command_id}/result")
    def command_result(command_id: str, payload: dict[str, Any], request: Request, x_node_token: str = Header(default="")) -> dict[str, Any]:
        node_id = command_node_id(request, command_id)
        if node_id:
            require_node_token(request, node_id=node_id, token=x_node_token)
        return {"ok": True, "command": app.state.command_service.submit_result(command_id, payload)}

    @app.post("/v1/shared/proposals")
    def submit_shared_proposal(payload: dict[str, Any], request: Request, authorization: str = Header(default="")) -> dict[str, Any]:
        session = get_auth_service(request).resolve_token(bearer_token(authorization))
        actor_id = session.user.user_id if session else "local-node"
        return {"ok": True, "proposal": app.state.shared_service.submit_proposal(payload, actor_id=actor_id)}

    @app.get("/v1/shared/knowledge")
    def get_shared_knowledge_snapshot(
        request: Request,
        tenant_id: str = Query(default="default"),
        since_version: str = Query(default=""),
        node_id: str = Query(default=""),
        x_node_token: str = Header(default=""),
        authorization: str = Header(default=""),
    ) -> dict[str, Any]:
        authorize_local_endpoint(request, authorization=authorization, node_id=node_id, node_token=x_node_token)
        snapshot = app.state.shared_service.official_snapshot(tenant_id=tenant_id, since_version=since_version)
        return {"ok": True, "snapshot": snapshot, "not_modified": bool(snapshot.get("not_modified"))}

    @app.get("/v1/shared/patches")
    def list_published_patches() -> dict[str, Any]:
        return {"ok": True, "patches": app.state.shared_service.list_patches(include_delivery=False)}

    @app.get("/v1/updates/latest")
    def latest_update(channel: str = Query(default="stable")) -> dict[str, Any]:
        return {"ok": True, "update": app.state.release_service.latest(channel=channel)}

    return app


def require_node_token(request: Request, *, node_id: str, token: str) -> None:
    state = request.app.state.vps_admin_store.read()
    record = state.get("local_nodes", {}).get(node_id)
    if not isinstance(record, dict):
        raise HTTPException(status_code=404, detail=f"node not found: {node_id}")
    expected = str(record.get("node_token") or "")
    if expected and token != expected:
        raise HTTPException(status_code=401, detail="node token required")


def authorize_local_endpoint(request: Request, *, authorization: str, node_id: str, node_token: str) -> None:
    if node_id:
        require_node_token(request, node_id=node_id, token=node_token)
        return
    session = get_auth_service(request).resolve_token(bearer_token(authorization))
    if session is None:
        raise HTTPException(status_code=401, detail="node_id or bearer session required")


def command_node_id(request: Request, command_id: str) -> str:
    state = request.app.state.vps_admin_store.read()
    record = state.get("commands", {}).get(command_id)
    if not isinstance(record, dict):
        raise HTTPException(status_code=404, detail=f"command not found: {command_id}")
    return str(record.get("node_id") or "")


app = create_app()


def main() -> None:
    import uvicorn

    uvicorn.run("apps.wechat_ai_customer_service.vps_admin.app:app", host="127.0.0.1", port=8766, reload=False)


if __name__ == "__main__":
    main()
