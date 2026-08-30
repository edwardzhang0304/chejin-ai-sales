from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class WorkerCreate(BaseModel):
    worker_name: str = Field(min_length=1, max_length=64)
    device_name: str | None = Field(default=None, max_length=128)
    platform: str = Field(default="windows", max_length=32)
    enabled: bool = True
    remark: str | None = Field(default=None, max_length=1000)

    @field_validator("worker_name")
    @classmethod
    def strip_worker_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Worker 名称必填")
        return value


class WorkerUpdate(BaseModel):
    worker_name: str | None = Field(default=None, min_length=1, max_length=64)
    device_name: str | None = Field(default=None, max_length=128)
    platform: str | None = Field(default=None, max_length=32)
    enabled: bool | None = None
    remark: str | None = Field(default=None, max_length=1000)


class WorkerHeartbeat(BaseModel):
    running_status: str = Field(default="idle", max_length=32)
    current_task: str | None = Field(default=None, max_length=255)
    client_binding_state: str | None = Field(default=None, max_length=64)
    client_instance_id: str | None = Field(default=None, max_length=128)
    run_status: str | None = Field(default=None, max_length=32)
    rpa_component_status: str | None = Field(default=None, max_length=32)
    wechat_status: str | None = Field(default=None, max_length=32)
    current_step: str | None = Field(default=None, max_length=64)
    local_lock_summary: dict | None = None


class WorkerClientBindRequest(BaseModel):
    worker_token: str = Field(min_length=1, max_length=128)
    client_instance_id: str = Field(min_length=1, max_length=128)


class WorkerRunStatusRequest(BaseModel):
    run_status: str = Field(min_length=1, max_length=32)
    client_instance_id: str | None = Field(default=None, max_length=128)


class WorkerInflightFlowStartRequest(BaseModel):
    flow_id: str = Field(min_length=1, max_length=128)
    flow_kind: str = Field(min_length=1, max_length=32)
    conversation_id: str | None = Field(default=None, max_length=36)
    unread_generation: int | None = Field(default=None, ge=0)

    @field_validator("conversation_id")
    @classmethod
    def strip_optional_start_conversation(
        cls,
        value: str | None,
    ) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        return cleaned or None

    @model_validator(mode="after")
    def validate_inflight_flow_scope(self):
        if self.flow_kind == "c2_read" and (
            not self.conversation_id or self.unread_generation is None
        ):
            raise ValueError(
                "C2 读取在途流程必须绑定 conversation_id 和 unread_generation"
            )
        if self.flow_kind == "chat_reply" and not self.conversation_id:
            raise ValueError("C3 回复在途流程必须绑定 conversation_id")
        if self.flow_kind != "c2_read" and self.unread_generation is not None:
            raise ValueError("非 C2 读取流程不得声明 unread_generation")
        return self


class WorkerInflightFlowFinishRequest(BaseModel):
    flow_id: str = Field(min_length=1, max_length=128)
    terminal_kind: str = Field(
        min_length=1,
        max_length=64,
        pattern="^(task_terminal|read_confirmed|retry_required|failed_before_message_action|read_failed_no_fact|technical_failed)$",
    )
    conversation_id: str | None = Field(default=None, max_length=36)
    error_code: str | None = Field(default=None, max_length=64)

    @field_validator("conversation_id", "error_code")
    @classmethod
    def strip_optional_finish_value(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        return cleaned or None


class WorkerLegacyMediaRecoverySettleRequest(BaseModel):
    flow_id: str = Field(min_length=1, max_length=128)
    legacy_record_digest: str = Field(
        min_length=64,
        max_length=64,
        pattern="^[0-9a-f]{64}$",
    )
    resolution: Literal[
        "legacy_cancelled_before_trigger",
        "legacy_identity_unresolved_handoff",
        "legacy_owner_unknown_incident",
    ]
    conversation_id: str | None = Field(default=None, max_length=36)
    record_summary: dict = Field(default_factory=dict)

    @field_validator("flow_id", "conversation_id")
    @classmethod
    def strip_legacy_recovery_value(
        cls,
        value: str | None,
    ) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        return cleaned or None

    @model_validator(mode="after")
    def validate_legacy_recovery_scope(self):
        if (
            self.resolution == "legacy_identity_unresolved_handoff"
            and not self.conversation_id
        ):
            raise ValueError("客户级旧媒体终态必须携带 conversation_id")
        if (
            self.resolution == "legacy_owner_unknown_incident"
            and self.conversation_id
        ):
            raise ValueError("Worker 级旧媒体事故不得猜测 conversation_id")
        return self


class WorkerResetBindingRequest(BaseModel):
    force: bool = True
