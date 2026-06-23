from pydantic import BaseModel, Field, field_validator


class SalesUpsert(BaseModel):
    sales_name: str = Field(min_length=1, max_length=50)
    phone: str | None = Field(default=None, max_length=64)
    wechat: str | None = Field(default=None, max_length=64)
    feishu_user_id: str | None = Field(default=None, max_length=128)
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


class SalesOut(BaseModel):
    id: str
    sales_name: str
    phone: str | None
    wechat: str | None
    feishu_user_id: str | None
    worker_id: str | None = None
    enabled: bool
    sort_order: int | None
    remark: str | None
    lead_count: int = 0


class SalesWorkerBindRequest(BaseModel):
    worker_id: str | None = Field(default=None, max_length=36)
