"""Auth model primitives used by local and VPS-backed sessions."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
try:
    from enum import StrEnum
except ImportError:  # Python 3.10 on lightweight VPS images.
    from enum import Enum

    class StrEnum(str, Enum):
        def __str__(self) -> str:
            return self.value
from typing import Any

from apps.wechat_ai_customer_service.knowledge_paths import DEFAULT_TENANT_ID, active_tenant_id


class Role(StrEnum):
    ADMIN = "admin"
    CUSTOMER = "customer"
    GUEST = "guest"


READ_ACTIONS = {"read"}
WRITE_ACTIONS = {"write", "delete", "backup", "restore", "sync", "approve", "publish", "execute"}


@dataclass(frozen=True)
class AuthUser:
    user_id: str
    role: Role
    tenant_ids: tuple[str, ...] = (DEFAULT_TENANT_ID,)
    display_name: str = ""
    username: str = ""
    resource_scopes: tuple[str, ...] = ("*",)

    @property
    def is_admin(self) -> bool:
        return self.role == Role.ADMIN

    @property
    def is_guest(self) -> bool:
        return self.role == Role.GUEST

    def has_tenant(self, tenant_id: str) -> bool:
        return self.is_admin or "*" in self.tenant_ids or active_tenant_id(tenant_id) in self.tenant_ids

    def to_dict(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "role": self.role.value,
            "tenant_ids": list(self.tenant_ids),
            "display_name": self.display_name,
            "username": self.username,
            "resource_scopes": list(self.resource_scopes),
        }


@dataclass(frozen=True)
class AuthSession:
    session_id: str
    user: AuthUser
    active_tenant_id: str = DEFAULT_TENANT_ID
    issued_at: str = ""
    expires_at: str = ""
    source: str = "local"
    token: str = ""

    def expired(self) -> bool:
        if not self.expires_at:
            return False
        try:
            value = datetime.fromisoformat(self.expires_at.replace("Z", "+00:00"))
        except ValueError:
            return False
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value <= datetime.now(timezone.utc)

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "user": self.user.to_dict(),
            "active_tenant_id": self.active_tenant_id,
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
            "source": self.source,
            "token": self.token,
        }


@dataclass(frozen=True)
class AuthContext:
    session: AuthSession
    tenant_id: str
    strict: bool = False
    authenticated: bool = True
    claims: dict[str, Any] = field(default_factory=dict)

    @property
    def user(self) -> AuthUser:
        return self.session.user

    @property
    def role(self) -> Role:
        return self.user.role

    def to_dict(self) -> dict[str, Any]:
        return {
            "authenticated": self.authenticated,
            "strict": self.strict,
            "tenant_id": self.tenant_id,
            "session": self.session.to_dict(),
            "claims": self.claims,
        }


def role_from_value(value: Any) -> Role:
    text = str(value or Role.GUEST.value).strip().lower()
    if text == Role.ADMIN.value:
        return Role.ADMIN
    if text == Role.CUSTOMER.value:
        return Role.CUSTOMER
    return Role.GUEST


def user_from_payload(payload: dict[str, Any]) -> AuthUser:
    tenant_values = payload.get("tenant_ids")
    if not isinstance(tenant_values, list):
        tenant_values = [payload.get("tenant_id") or DEFAULT_TENANT_ID]
    return AuthUser(
        user_id=str(payload.get("user_id") or payload.get("id") or ""),
        role=role_from_value(payload.get("role")),
        tenant_ids=tuple(str(item) for item in tenant_values if str(item)),
        display_name=str(payload.get("display_name") or payload.get("name") or ""),
        username=str(payload.get("username") or ""),
        resource_scopes=tuple(str(item) for item in payload.get("resource_scopes", ["*"]) if str(item)),
    )


def session_from_payload(payload: dict[str, Any]) -> AuthSession:
    user_payload = payload.get("user") if isinstance(payload.get("user"), dict) else payload
    session_id = str(payload.get("session_id") or payload.get("token") or "")
    tenant_id = active_tenant_id(payload.get("active_tenant_id") or payload.get("tenant_id") or DEFAULT_TENANT_ID)
    return AuthSession(
        session_id=session_id,
        token=str(payload.get("token") or session_id),
        user=user_from_payload(user_payload),
        active_tenant_id=tenant_id,
        issued_at=str(payload.get("issued_at") or ""),
        expires_at=str(payload.get("expires_at") or ""),
        source=str(payload.get("source") or "vps"),
    )
