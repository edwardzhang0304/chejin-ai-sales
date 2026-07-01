from datetime import datetime

from pydantic import BaseModel, Field, field_validator


class MessageBatchCollectRequest(BaseModel):
    trigger_message_event_id: str = Field(min_length=1, max_length=36)
    trace_id: str | None = Field(default=None, max_length=64)


class MessageBatchGenerateRequest(BaseModel):
    force: bool = False


class ReplyActionClaimSendRequest(BaseModel):
    task_id: str = Field(min_length=1, max_length=36)
    worker_id: str = Field(min_length=1, max_length=36)


class ReplyActionSentAckRequest(BaseModel):
    send_token: str = Field(min_length=1, max_length=128)
    task_id: str = Field(min_length=1, max_length=36)
    worker_id: str = Field(min_length=1, max_length=36)
    client_instance_id: str | None = Field(default=None, max_length=128)
    send_result: str = Field(min_length=1, max_length=32)
    sent_at: datetime | None = None
    reply_text_hash: str | None = Field(default=None, max_length=64)
    sidecar_run_id: str | None = Field(default=None, max_length=128)
    evidence: dict = Field(default_factory=dict)
    error_code: str | None = Field(default=None, max_length=64)
    remark: str | None = Field(default=None, max_length=1000)

    @field_validator("send_result")
    @classmethod
    def validate_send_result(cls, value: str) -> str:
        if value not in {"sent", "failed", "unknown"}:
            raise ValueError("send_result 仅支持 sent / failed / unknown")
        return value
