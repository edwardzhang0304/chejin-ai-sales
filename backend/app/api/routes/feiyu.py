import hmac
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request
from sqlalchemy.orm import Session

from app.api.response import ok
from app.core.config import get_settings
from app.core.database import get_db
from app.core.request_context import ActorContext
from app.core.request_id import get_request_id
from app.errors import AppError, DuplicateLeadError
from app.schemas.lead import LeadCreate
from app.services import lead_service


router = APIRouter(tags=["feiyu"])

FEIYU_INTEGRATION_ACTOR_ID = UUID("00000000-0000-0000-0000-00000000f001")
FEIYU_INTEGRATION_ACTOR_NAME = "Feiyu integration"


def require_feiyu_forward_secret(
    provided_secret: str | None = Header(default=None, alias="X-Feiyu-Forward-Secret"),
) -> None:
    expected_secret = get_settings().feiyu_forward_secret
    if (
        not isinstance(expected_secret, str)
        or not expected_secret.strip()
        or not provided_secret
        or not hmac.compare_digest(
            expected_secret.encode("utf-8"),
            provided_secret.encode("utf-8"),
        )
    ):
        raise AppError("FEIYU_FORWARD_UNAUTHORIZED", "飞鱼接入认证失败", 401)


@router.post("/integrations/feiyu/leads", dependencies=[Depends(require_feiyu_forward_secret)])
def create_feiyu_lead(
    payload: LeadCreate,
    request: Request,
    db: Session = Depends(get_db),
):
    actor = ActorContext(
        operator_id=FEIYU_INTEGRATION_ACTOR_ID,
        operator_name=FEIYU_INTEGRATION_ACTOR_NAME,
        role="integration",
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
        request_id=getattr(request.state, "request_id", None) or get_request_id(),
    )
    try:
        data = lead_service.create_lead(db, payload, actor)
        db.commit()
        return ok(data)
    except DuplicateLeadError:
        db.commit()
        raise
    except Exception:
        db.rollback()
        raise
