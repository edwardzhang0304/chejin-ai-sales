from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator

from app.enums import InvalidReason


class LeadCreate(BaseModel):
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


class LeadUpdate(BaseModel):
    customer_name: str | None = Field(default=None, min_length=1, max_length=50)
    phones: list[str] | None = Field(default=None, max_length=5)
    wechats: list[str] | None = Field(default=None, max_length=5)
    emails: list[str] | None = Field(default=None, max_length=5)
    remark: str | None = Field(default=None, max_length=1000)
    custom_fields: dict[str, Any] | None = None


class MarkInvalidRequest(BaseModel):
    invalid_reason: InvalidReason
    invalid_remark: str | None = Field(default=None, max_length=1000)


class BatchMarkInvalidRequest(MarkInvalidRequest):
    lead_ids: list[str] = Field(min_length=1)


class RevealContactRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=200)


class DuplicatePreviewRequest(BaseModel):
    phones: list[str] = Field(min_length=1, max_length=5)


class RetryAutoAssignRequest(BaseModel):
    lead_ids: list[str] = Field(default_factory=list)


class LeadExportRequest(BaseModel):
    lead_ids: list[str] = Field(min_length=1)
    fields: list[str] = Field(default_factory=list)


class ContactOut(BaseModel):
    id: str
    contact_type: str
    masked_value: str
    is_primary: bool


class LeadListItem(BaseModel):
    id: str
    customer_name: str
    status: str
    source_type: str
    source_name_snapshot: str
    primary_phone_masked: str | None
    primary_wechat_masked: str | None
    sales_id: str | None
    sales_name: str | None
    assign_status: str
    assign_failure_reason: str | None
    remark_summary: str | None
    duplicate_count: int
    last_duplicate_at: datetime | None
    created_by_name: str | None
    created_at: datetime
    updated_at: datetime


class LeadListOut(BaseModel):
    items: list[LeadListItem]
    page: int
    page_size: int
    total: int


class LeadStatsOut(BaseModel):
    today_new_count: int
    today_assigned_count: int
    today_unassigned_count: int
    assignment_success_rate: float | None
    assigned_count: int
    unassigned_count: int
    duplicate_event_count: int
