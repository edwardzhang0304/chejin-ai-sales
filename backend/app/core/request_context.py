from dataclasses import dataclass
from uuid import UUID

from fastapi import Depends, Request

from app.core.auth import require_admin_auth
from app.core.request_id import get_request_id
from app.errors import AppError


@dataclass(frozen=True)
class ActorContext:
    operator_id: UUID
    operator_name: str
    role: str
    ip_address: str | None
    user_agent: str | None
    request_id: str


def get_actor_context(
    request: Request,
    _admin_auth: None = Depends(require_admin_auth),
) -> ActorContext:
    identity = getattr(request.state, "auth_actor", None)
    if not identity:
        raise AppError("ADMIN_UNAUTHORIZED", "登录已失效，请重新登录", 401)
    try:
        operator_id = UUID(str(identity.get("operator_id") or ""))
    except ValueError as exc:
        raise AppError("AUTHENTICATED_OPERATOR_ID_INVALID", "认证操作人 ID 配置不正确", 500) from exc

    return ActorContext(
        operator_id=operator_id,
        operator_name=str(identity.get("operator_name") or "已认证操作人"),
        # Kept only as an internal compatibility field for existing audit and
        # service signatures. It is not sourced from the browser or used for
        # authorization.
        role="authenticated",
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
        request_id=get_request_id(),
    )


def worker_actor_context(request: Request, *, worker_id: str, worker_name: str) -> ActorContext:
    try:
        operator_id = UUID(worker_id)
    except ValueError as exc:
        raise AppError("WORKER_ID_INVALID", "Worker ID 格式不正确", 400) from exc
    return ActorContext(
        operator_id=operator_id,
        operator_name=worker_name,
        role="worker",
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
        request_id=get_request_id(),
    )
