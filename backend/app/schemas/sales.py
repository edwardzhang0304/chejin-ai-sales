from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic_core import PydanticCustomError


class _SalesWriteBase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="before")
    @classmethod
    def reject_server_managed_feishu_ids(cls, value):
        if isinstance(value, dict):
            forbidden = {
                key
                for key in ("feishu_user_id", "open_id", "feishu_open_id", "user_id", "union_id")
                if key in value
            }
            if forbidden:
                raise PydanticCustomError(
                    "sales_feishu_id_server_managed",
                    "飞书用户标识只能由服务端维护",
                )
        return value


class SalesCreate(_SalesWriteBase):
    sales_name: str = Field(min_length=1, max_length=50)
    phone: str = Field(min_length=1, max_length=64)
    wechat: str | None = Field(default=None, max_length=64)
    worker_id: str | None = Field(default=None, max_length=36)
    enabled: bool = True
    sort_order: int | None = None
    remark: str | None = None

    @field_validator("sales_name")
    @classmethod
    def strip_sales_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("销售姓名必填")
        return value


class SalesUpdate(_SalesWriteBase):
    sales_name: str | None = Field(default=None, min_length=1, max_length=50)
    phone: str | None = Field(default=None, min_length=1, max_length=64)
    wechat: str | None = Field(default=None, max_length=64)
    worker_id: str | None = Field(default=None, max_length=36)
    enabled: bool | None = None
    sort_order: int | None = None
    remark: str | None = None

    @model_validator(mode="before")
    @classmethod
    def reject_explicit_null_required_fields(cls, value):
        if isinstance(value, dict):
            if "phone" in value and value["phone"] is None:
                raise PydanticCustomError(
                    "sales_phone_invalid",
                    "修改手机号时必须提交完整手机号",
                )
            if "sales_name" in value and value["sales_name"] is None:
                raise ValueError("销售姓名不能为空")
            if "enabled" in value and value["enabled"] is None:
                raise ValueError("销售状态不能为空")
        return value

    @field_validator("sales_name")
    @classmethod
    def strip_optional_sales_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("销售姓名不能为空")
        return value


class SalesOut(BaseModel):
    id: str
    sales_name: str
    phone: str
    wechat: str | None
    feishu_binding_status: str
    worker_id: str | None = None
    enabled: bool
    sort_order: int | None
    remark: str | None
    lead_count: int = 0


class SalesWorkerBindRequest(BaseModel):
    worker_id: str | None = Field(default=None, max_length=36)
