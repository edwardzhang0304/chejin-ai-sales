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


class WechatMessageItem(BaseModel):
    dedupe_key: str | None = Field(default=None, max_length=255)
    sender_role_hint: str = Field(min_length=1, max_length=32)
    message_type: str = Field(min_length=1, max_length=32)
    content: str | None = None
    image_local_path: str | None = None
    occurred_at: datetime | None = None
    ocr_confidence: float | None = None
    raw_payload: dict | None = None


class WechatMessageIngestRequest(BaseModel):
    read_run_id: str = Field(min_length=1, max_length=128)
    conversation_id: str = Field(min_length=1, max_length=36)
    rpa_session_key: str | None = Field(default=None, max_length=255)
    messages: list[WechatMessageItem] = Field(default_factory=list, max_length=500)
    evidence: dict | None = None
