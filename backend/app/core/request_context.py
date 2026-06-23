from dataclasses import dataclass
from uuid import UUID

from fastapi import Header, Request

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
    x_operator_id: str | None = Header(default=None, alias="X-Operator-Id"),
    x_operator_name: str | None = Header(default=None, alias="X-Operator-Name"),
    x_operator_role: str | None = Header(default=None, alias="X-Operator-Role"),
    x_request_id: str | None = Header(default=None, alias="X-Request-Id"),
) -> ActorContext:
    try:
        operator_id = UUID(x_operator_id) if x_operator_id else UUID("00000000-0000-0000-0000-000000000000")
    except ValueError as exc:
        raise AppError("OPERATOR_ID_INVALID", "操作人 ID 格式不正确", 400) from exc

    return ActorContext(
        operator_id=operator_id,
        operator_name=x_operator_name or "临时操作人",
        role=x_operator_role or "admin",
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
        request_id=x_request_id or get_request_id(),
    )
