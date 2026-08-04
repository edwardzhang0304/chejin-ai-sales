from datetime import datetime

from pydantic import BaseModel, Field, field_validator, model_validator

from app.contracts.c2 import contract_values


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
    action_phase: str = Field(min_length=1, max_length=32)
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

    @field_validator("action_phase")
    @classmethod
    def validate_action_phase(cls, value: str) -> str:
        if value not in contract_values("action_phases"):
            raise ValueError(
                "action_phase 仅支持 not_attempted / trigger_attempted / confirmed"
            )
        return value

    @model_validator(mode="after")
    def validate_send_result_phase(self):
        allowed = {
            ("sent", "confirmed"),
            ("failed", "not_attempted"),
            ("unknown", "trigger_attempted"),
        }
        if (self.send_result, self.action_phase) not in allowed:
            raise ValueError("send_result 与 action_phase 组合不合法")
        return self
