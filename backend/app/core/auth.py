from urllib.parse import urlsplit
import secrets

from fastapi import Cookie, Depends, Header, Request
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import get_db
from app.errors import AppError
from app.services.auth_service import authenticate_session


ADMIN_SESSION_COOKIE = "chejin_admin_session"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def _normalized_origin(value: str) -> str:
    parsed = urlsplit(value.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.path not in {"", "/"}:
        return ""
    return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"


def validate_admin_origin(request: Request) -> None:
    if request.method.upper() in SAFE_METHODS:
        return
    origin = _normalized_origin(request.headers.get("origin") or "")
    allowed = {_normalized_origin(value) for value in get_settings().cors_origins}
    allowed.discard("")
    if not origin or origin not in allowed:
        raise AppError("AUTH_ORIGIN_FORBIDDEN", "请求来源不受信任", 403)


def validate_admin_auth(
    request: Request,
    db: Session,
    session_token: str | None,
    *,
    x_worker_token: str | None = None,
    authorization: str | None = None,
) -> None:
    if x_worker_token:
        raise AppError("ADMIN_FORBIDDEN", "Worker Token 无权访问后台管理接口", 403)
    # Browser Bearer credentials are intentionally not accepted, even when a
    # valid-looking legacy token is supplied.
    if authorization and not session_token:
        raise AppError("ADMIN_UNAUTHORIZED", "登录已失效，请重新登录", 401)
    validate_admin_origin(request)
    account, admin_session = authenticate_session(db, session_token)
    request.state.auth_actor = {
        "operator_id": account.id,
        "operator_name": account.display_name,
        "actor_type": "admin_account",
        "session_id": admin_session.id,
    }


def require_admin_auth(
    request: Request,
    db: Session = Depends(get_db),
    session_token: str | None = Cookie(default=None, alias=ADMIN_SESSION_COOKIE),
    x_worker_token: str | None = Header(default=None, alias="X-Worker-Token"),
    authorization: str | None = Header(default=None, alias="Authorization"),
) -> None:
    try:
        validate_admin_auth(
            request,
            db,
            session_token,
            x_worker_token=x_worker_token,
            authorization=authorization,
        )
        db.commit()
    except AppError:
        # Expiration checks can revoke an invalid session as part of the same
        # authentication transaction.
        db.commit()
        raise
    except Exception:
        db.rollback()
        raise


def require_internal_service_auth(
    request: Request,
    x_internal_service_token: str | None = Header(default=None, alias="X-Internal-Service-Token"),
    x_worker_token: str | None = Header(default=None, alias="X-Worker-Token"),
    session_token: str | None = Cookie(default=None, alias=ADMIN_SESSION_COOKIE),
    authorization: str | None = Header(default=None, alias="Authorization"),
) -> None:
    if x_worker_token or session_token or authorization:
        raise AppError("INTERNAL_SERVICE_FORBIDDEN", "当前身份无权调用内部服务接口", 403)
    expected = get_settings().internal_service_token
    if not x_internal_service_token:
        raise AppError("INTERNAL_SERVICE_UNAUTHORIZED", "缺少内部服务凭据", 401)
    if not secrets.compare_digest(x_internal_service_token, expected):
        raise AppError("INTERNAL_SERVICE_UNAUTHORIZED", "内部服务凭据无效", 401)
    request.state.auth_actor = {
        "operator_id": "omniauto-brain-service",
        "operator_name": "OmniAuto Brain Service",
        "actor_type": "internal_service",
    }
