from fastapi import APIRouter, Cookie, Depends, Header, Request, Response
from sqlalchemy.orm import Session

from app.api.response import ok
from app.core.auth import ADMIN_SESSION_COOKIE, require_admin_auth, validate_admin_origin
from app.core.config import get_settings
from app.core.database import get_db
from app.core.request_context import ActorContext, get_actor_context
from app.core.request_id import get_request_id
from app.errors import AppError
from app.schemas.auth import AdminLoginRequest
from app.services import auth_service


router = APIRouter(tags=["auth-session"])


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _reject_worker_credential(x_worker_token: str | None) -> None:
    if x_worker_token:
        raise AppError("ADMIN_FORBIDDEN", "Worker Token 无权访问后台登录接口", 403)


def _set_session_cookie(response: Response, raw_token: str) -> None:
    settings = get_settings()
    response.set_cookie(
        key=ADMIN_SESSION_COOKIE,
        value=raw_token,
        max_age=settings.admin_session_absolute_seconds,
        path=settings.admin_session_cookie_path,
        secure=settings.admin_cookie_secure,
        httponly=True,
        samesite="strict",
    )


def _clear_session_cookie(response: Response) -> None:
    settings = get_settings()
    response.delete_cookie(
        key=ADMIN_SESSION_COOKIE,
        path=settings.admin_session_cookie_path,
        secure=settings.admin_cookie_secure,
        httponly=True,
        samesite="strict",
    )


@router.post("/auth/login")
def login(
    payload: AdminLoginRequest,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    x_worker_token: str | None = Header(default=None, alias="X-Worker-Token"),
):
    _reject_worker_credential(x_worker_token)
    validate_admin_origin(request)
    try:
        account, raw_token, _ = auth_service.authenticate_credentials(
            db,
            username=payload.username,
            password=payload.password,
            ip_address=_client_ip(request),
            user_agent=request.headers.get("user-agent"),
            request_id=get_request_id(),
        )
        db.commit()
    except AppError:
        db.commit()
        raise
    except Exception:
        db.rollback()
        raise
    _set_session_cookie(response, raw_token)
    return ok({"operator_id": account.id, "operator_name": account.display_name})


@router.get("/auth/session", dependencies=[Depends(require_admin_auth)])
def read_auth_session(actor: ActorContext = Depends(get_actor_context)):
    return ok({"operator_id": str(actor.operator_id), "operator_name": actor.operator_name})


@router.post("/auth/logout")
def logout(
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    session_token: str | None = Cookie(default=None, alias=ADMIN_SESSION_COOKIE),
    x_worker_token: str | None = Header(default=None, alias="X-Worker-Token"),
):
    _reject_worker_credential(x_worker_token)
    validate_admin_origin(request)
    try:
        account = auth_service.revoke_session(db, session_token)
        username = account.username_normalized if account else "unknown"
        auth_service.write_auth_audit(
            db,
            event_type="admin_logout",
            username_normalized=username,
            ip_address=_client_ip(request),
            user_agent=request.headers.get("user-agent"),
            request_id=get_request_id(),
            result="success",
            account=account,
        )
        db.commit()
    except Exception:
        db.rollback()
        raise
    _clear_session_cookie(response)
    return ok({"logged_out": True})
