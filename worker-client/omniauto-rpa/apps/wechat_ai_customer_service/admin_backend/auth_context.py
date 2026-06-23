"""FastAPI auth and tenant-context integration."""

from __future__ import annotations

from typing import Callable

from fastapi import HTTPException, Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, Response

from apps.wechat_ai_customer_service.auth import AuthContext, AuthService, assert_allowed
from apps.wechat_ai_customer_service.cloud_gate import cloud_gate_error_payload, cloud_gate_status, cloud_required_enabled
from apps.wechat_ai_customer_service.knowledge_paths import active_tenant_id, reset_active_tenant_id, set_active_tenant_id


PUBLIC_PREFIXES = ("/static/",)
PUBLIC_PATHS = {
    "/",
    "/api/health",
    "/api/auth/login",
    "/api/auth/login/start",
    "/api/auth/login/bind-email/start",
    "/api/auth/login/verify",
    "/api/auth/cloud-gate/prepare",
    "/api/auth/initialize/start",
    "/api/auth/initialize/verify",
    "/api/auth/logout",
    "/v1/auth/login",
    "/v1/auth/login/start",
    "/v1/auth/login/bind-email/start",
    "/v1/auth/login/verify",
    "/v1/auth/cloud-gate/prepare",
    "/v1/auth/initialize/start",
    "/v1/auth/initialize/verify",
}
READ_METHODS = {"GET", "HEAD", "OPTIONS"}
CLOUD_GATE_ALLOWED_PATHS = {
    "/api/auth/me",
    "/api/auth/security",
    "/api/sync/status",
    "/api/sync/register-node",
    "/api/sync/shared/cloud-snapshot",
    "/api/sync/recorder/module-bindings",
    "/api/sync/commands/poll",
    "/api/customer-service/runtime/status",
    "/api/customer-service/runtime/stop",
    "/api/customer-service/runtime/start",
    "/api/recorder/runtime/status",
    "/api/recorder/runtime/stop",
    "/api/recorder/runtime/start",
}
CLOUD_GATE_ALLOWED_PREFIXES = ()
LOCAL_SAFETY_STOP_PATHS = {
    "/api/customer-service/runtime/stop",
    "/api/recorder/runtime/stop",
}
LOOPBACK_HOSTS = {"127.0.0.1", "::1", "::ffff:127.0.0.1", "localhost"}


class AuthTenantMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, auth_service: AuthService | None = None) -> None:  # type: ignore[no-untyped-def]
        super().__init__(app)
        self.auth_service = auth_service or AuthService()

    async def dispatch(self, request: Request, call_next: Callable) -> Response:  # type: ignore[override]
        path = request.url.path
        if path in PUBLIC_PATHS or any(path.startswith(prefix) for prefix in PUBLIC_PREFIXES):
            return await call_next(request)

        tenant_header = request.headers.get("X-Tenant-ID") or request.query_params.get("tenant_id") or ""
        if local_safety_stop_exempt(request, path):
            tenant_id = active_tenant_id(tenant_header or None)
            context = self.auth_service.implicit_admin_context(tenant_id=tenant_id)
            token = set_active_tenant_id(context.tenant_id)
            request.state.auth_context = context
            try:
                response = await call_next(request)
            finally:
                reset_active_tenant_id(token)
            response.headers.setdefault("X-Tenant-ID", context.tenant_id)
            response.headers.setdefault("X-Auth-Role", context.role.value)
            response.headers.setdefault("X-Local-Safety-Stop", "1")
            return response

        context = self.auth_service.resolve_context(
            authorization=request.headers.get("Authorization", ""),
            tenant_id=tenant_header or None,
            dev_role=request.headers.get("X-Role", ""),
            dev_user_id=request.headers.get("X-User-ID", ""),
        )
        if context is None:
            return JSONResponse({"ok": False, "detail": "authentication required"}, status_code=401)

        if cloud_required_enabled() and not cloud_gate_exempt(path):
            gate = cloud_gate_status()
            if not gate.get("ok"):
                return JSONResponse(cloud_gate_error_payload(), status_code=423)

        resource = resource_for_path(path)
        action = action_for_request(path, request.method)
        try:
            assert_allowed(context, resource=resource, action=action, tenant_id=context.tenant_id)
        except HTTPException as exc:
            return JSONResponse({"ok": False, "detail": exc.detail}, status_code=exc.status_code)

        token = set_active_tenant_id(context.tenant_id)
        request.state.auth_context = context
        try:
            response = await call_next(request)
        finally:
            reset_active_tenant_id(token)
        response.headers.setdefault("X-Tenant-ID", context.tenant_id)
        response.headers.setdefault("X-Auth-Role", context.role.value)
        return response


def cloud_gate_exempt(path: str) -> bool:
    if path in CLOUD_GATE_ALLOWED_PATHS:
        return True
    return any(path.startswith(prefix) for prefix in CLOUD_GATE_ALLOWED_PREFIXES)


def local_safety_stop_exempt(request: Request, path: str) -> bool:
    """Allow local emergency stop even when the admin session is stale.

    Starting or configuring the runtime still requires normal authentication.
    Only POST stop from the loopback admin console is exempt so the operator can
    always stop RPA keyboard/mouse hooks and background workers.
    """

    if request.method.upper() != "POST" or path not in LOCAL_SAFETY_STOP_PATHS:
        return False
    client_host = str(getattr(request.client, "host", "") or "").strip().lower()
    host_header = str(request.headers.get("host") or "").split(":", 1)[0].strip().lower()
    if client_host in LOOPBACK_HOSTS:
        return True
    if client_host and client_host != "testclient":
        return False
    return host_header in LOOPBACK_HOSTS


def current_auth_context(request: Request) -> AuthContext:
    context = getattr(request.state, "auth_context", None)
    if isinstance(context, AuthContext):
        return context
    service = AuthService()
    return service.implicit_admin_context(tenant_id=active_tenant_id())


def resource_for_path(path: str) -> str:
    if path.startswith("/api/sync/shared"):
        return "shared_knowledge"
    if path.startswith("/api/sync/commands"):
        return "commands"
    if path.startswith("/api/sync/update"):
        return "updates"
    if path.startswith("/api/auth/change-password") or path.startswith("/api/auth/email") or path.startswith("/api/auth/security"):
        return "account_security"
    if path.startswith("/api/sync"):
        return "backups"
    if path.startswith("/api/rag"):
        return "tenant_rag"
    if path.startswith("/api/knowledge") or path.startswith("/api/uploads") or path.startswith("/api/candidates"):
        return "tenant_knowledge"
    if path.startswith("/api/recorder/modules") or path.startswith("/api/recorder/module-bindings"):
        return "settings"
    if path.startswith("/api/system/llm-config"):
        return "llm_config"
    if path.startswith("/api/tenants") or path.startswith("/api/system"):
        return "settings"
    return "tenant_knowledge"


def action_for_request(path: str, method: str) -> str:
    method = method.upper()
    if path.startswith("/api/sync/commands"):
        return "execute"
    if path.startswith("/api/sync/update"):
        return "sync"
    if path.startswith("/api/sync/shared"):
        return "sync"
    if method in READ_METHODS:
        return "read"
    if path.startswith("/api/sync"):
        return "backup"
    if method == "DELETE":
        return "delete"
    return "write"
