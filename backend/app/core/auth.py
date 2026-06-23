import hmac

from fastapi import Header, Request

from app.core.config import get_settings
from app.errors import AppError


def _bearer_token(authorization: str | None) -> str | None:
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    return token.strip()


def validate_admin_auth(
    request: Request,
    authorization: str | None,
    x_worker_token: str | None = None,
) -> None:
    settings = get_settings()
    if not settings.auth_enforcement:
        return

    if x_worker_token:
        raise AppError("ADMIN_FORBIDDEN", "Worker Token 无权访问后台管理接口", 403)

    expected = settings.admin_api_token
    if not expected:
        raise AppError("ADMIN_AUTH_NOT_CONFIGURED", "后台鉴权未配置", 500)

    token = _bearer_token(authorization)
    if not token:
        raise AppError("ADMIN_UNAUTHORIZED", "缺少后台访问凭证", 401)

    if not hmac.compare_digest(token, expected):
        raise AppError("ADMIN_UNAUTHORIZED", "后台访问凭证无效", 401)

    request.state.auth_role = "admin"


def require_admin_auth(
    request: Request,
    authorization: str | None = Header(default=None, alias="Authorization"),
    x_worker_token: str | None = Header(default=None, alias="X-Worker-Token"),
) -> None:
    validate_admin_auth(request, authorization, x_worker_token)
