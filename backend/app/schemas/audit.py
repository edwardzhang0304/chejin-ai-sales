from datetime import datetime
from typing import Any

from pydantic import BaseModel


class OperationLogOut(BaseModel):
    id: str
    event_type: str
    event_name: str
    module: str
    target_type: str
    target_id: str | None
    lead_id: str | None
    lead_customer_name: str | None = None
    operator_id: str | None
    operator_name: str | None
    ip_address: str | None
    created_at: datetime
    metadata: dict[str, Any]


class OperationLogListOut(BaseModel):
    items: list[OperationLogOut]
    page: int
    page_size: int
    total: int

