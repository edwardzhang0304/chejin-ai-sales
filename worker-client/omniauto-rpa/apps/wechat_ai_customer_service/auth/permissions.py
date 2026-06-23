"""Role-based access decisions for local admin APIs and workflows."""

from __future__ import annotations

from fastapi import HTTPException

from .models import AuthContext, Role
from apps.wechat_ai_customer_service.knowledge_paths import active_tenant_id


WRITE_ACTIONS = {"write", "delete", "backup", "restore", "sync", "approve", "publish", "execute"}
READ_ACTIONS = {"read"}


def can_access(context: AuthContext, *, resource: str, action: str, tenant_id: str | None = None) -> bool:
    tenant = active_tenant_id(tenant_id or context.tenant_id)
    action = str(action or "read").lower()
    resource = str(resource or "").strip() or "tenant_knowledge"
    user = context.user

    if user.role == Role.ADMIN:
        return True

    if resource == "account_security":
        return context.authenticated and action in READ_ACTIONS | {"write"}

    if resource == "llm_config":
        return action in READ_ACTIONS | {"write"}

    if action in WRITE_ACTIONS and user.role == Role.GUEST:
        return False

    if resource == "shared_knowledge":
        if action in READ_ACTIONS:
            return user.has_tenant(tenant)
        if user.role == Role.CUSTOMER and action == "sync":
            return user.has_tenant(tenant)
        return False

    if resource == "commands":
        return user.role == Role.CUSTOMER and action == "execute" and user.has_tenant(tenant)

    if resource == "updates":
        return user.role == Role.CUSTOMER and action == "sync" and user.has_tenant(tenant)

    if resource in {"commands", "updates"}:
        return False

    if resource == "settings" and action != "read":
        return False

    if not user.has_tenant(tenant):
        return False

    if user.role == Role.CUSTOMER:
        return action in READ_ACTIONS | {"write", "delete", "backup", "sync"}

    if user.role == Role.GUEST:
        return action in READ_ACTIONS

    return False


def assert_allowed(context: AuthContext, *, resource: str, action: str, tenant_id: str | None = None) -> None:
    if can_access(context, resource=resource, action=action, tenant_id=tenant_id):
        return
    raise HTTPException(status_code=403, detail=f"permission denied: {context.role.value} cannot {action} {resource}")
