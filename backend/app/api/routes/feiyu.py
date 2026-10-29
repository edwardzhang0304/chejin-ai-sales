import hmac
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import get_db
from app.core.request_context import ActorContext
from app.core.request_id import get_request_id
from app.errors import AppError
from app.schemas.lead import LeadCreate
from app.services.feiyu_lead_service import process_feiyu_lead


router = APIRouter(tags=["feiyu"])

FEIYU_INTEGRATION_ACTOR_ID = UUID("00000000-0000-0000-0000-00000000f001")
FEIYU_INTEGRATION_ACTOR_NAME = "Feiyu integration"


class FeiyuLeadCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    customer_name: str = Field(min_length=1, max_length=50)
    phones: list[str] = Field(min_length=1, max_length=5)
    wechats: list[str] = Field(default_factory=list, max_length=5)
    emails: list[str] = Field(default_factory=list, max_length=5)
    remark: str | None = Field(default=None, max_length=1000)
    custom_fields: dict[str, Any] = Field(default_factory=dict)

    @field_validator("customer_name")
    @classmethod
    def strip_customer_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("请输入客户名称")
        return value


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
    payload: FeiyuLeadCreate,
    request: Request,
    db: Session = Depends(get_db),
    account_id: str | None = Header(default=None, alias="X-Feiyu-Account-ID"),
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
        if "remark_code" in payload.custom_fields:
            raise AppError(
                "FEIYU_RESERVED_FIELD",
                "飞鱼资料不能指定系统内部备注短码",
                400,
            )
        settings = get_settings()
        configured_default = getattr(settings, "feiyu_account_id", "default")
        selected_account_id = account_id or configured_default
        allowed_account_ids = {
            value.strip()
            for value in getattr(settings, "feiyu_account_ids", configured_default).split(",")
            if value.strip()
        }
        if selected_account_id not in allowed_account_ids:
            raise AppError(
                "FEIYU_ACCOUNT_ID_UNAUTHORIZED",
                "该飞鱼来源账号未在服务端配置",
                403,
            )
        lead_payload = LeadCreate(**payload.model_dump())
        return process_feiyu_lead(db, lead_payload, actor, selected_account_id)
    except Exception:
        db.rollback()
        raise
