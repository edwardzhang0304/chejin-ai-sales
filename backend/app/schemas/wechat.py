from datetime import datetime

from pydantic import BaseModel, Field


class WechatSessionScanItem(BaseModel):
    rpa_session_key: str = Field(min_length=1, max_length=255)
    display_name: str = Field(min_length=1, max_length=255)
    remark_code_candidates: list[str] = Field(default_factory=list, max_length=10)
    row_fingerprint: str | None = Field(default=None, max_length=255)
    unread_hint: bool = False
    last_message_preview: str | None = Field(default=None, max_length=1000)
    ocr_confidence: float | None = None


class WechatSessionScanResultRequest(BaseModel):
    scan_id: str = Field(min_length=1, max_length=128)
    sidecar_run_id: str = Field(min_length=1, max_length=128)
    wechat_account_hint: str | None = Field(default=None, max_length=128)
    started_at: datetime
    finished_at: datetime
    sessions: list[WechatSessionScanItem] = Field(default_factory=list, max_length=200)
    evidence: dict | None = None
    scan_failed: bool = False
    error_code: str | None = Field(default=None, max_length=64)


class WechatMessagePosition(BaseModel):
    screen_order: int = Field(ge=1)
    visual_top: int | None = Field(default=None, ge=0)
    visual_bottom: int | None = Field(default=None, ge=0)
    frame_source: str = Field(min_length=1, max_length=32)
    order_source: str | None = Field(default=None, max_length=64)


class WechatMessageItem(BaseModel):
    dedupe_key: str | None = Field(default=None, max_length=255)
    source_message_key: str | None = Field(default=None, max_length=255)
    sender_role_hint: str = Field(min_length=1, max_length=32)
    message_type: str = Field(min_length=1, max_length=32)
    content: str | None = None
    image_local_path: str | None = None
    occurred_at: datetime | None = None
    ocr_confidence: float | None = None
    item_state: str | None = Field(default=None, max_length=32)
    flow_state: str | None = Field(default=None, max_length=32)
    message_position: WechatMessagePosition | None = None
    raw_payload: dict | None = None


class WechatMessageIngestRequest(BaseModel):
    contract_version: int = Field(default=1, ge=1, le=3)
    contract_revision: str | None = Field(default=None, max_length=32)
    contract_sha256: str | None = Field(default=None, min_length=64, max_length=64)
    observation_schema_version: int | None = Field(default=None, ge=1)
    read_run_id: str = Field(min_length=1, max_length=128)
    conversation_id: str = Field(min_length=1, max_length=36)
    remark_code: str | None = Field(default=None, max_length=64)
    rpa_session_key: str | None = Field(default=None, max_length=255)
    authorization_revision: str | None = Field(default=None, max_length=128)
    messages: list[WechatMessageItem] = Field(default_factory=list, max_length=500)
    evidence: dict | None = None
