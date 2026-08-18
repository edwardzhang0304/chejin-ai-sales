from pydantic import BaseModel, Field, field_validator


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


class WorkerInflightFlowFinishRequest(BaseModel):
    flow_id: str = Field(min_length=1, max_length=128)
    terminal_kind: str = Field(
        min_length=1,
        max_length=64,
        pattern="^(task_terminal|read_confirmed|failed_before_message_action|read_failed_no_fact)$",
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


class WorkerResetBindingRequest(BaseModel):
    force: bool = True
