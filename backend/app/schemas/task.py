from pydantic import BaseModel, Field, field_validator

from app.enums import TaskBlockCode, TaskErrorCode, TaskResultCode, TaskStatus, TaskType


class TaskCreate(BaseModel):
    task_type: TaskType = TaskType.add_friend
    lead_id: str = Field(min_length=1, max_length=36)
    sales_id: str | None = Field(default=None, max_length=36)
    worker_id: str | None = Field(default=None, max_length=36)
    remark: str | None = Field(default=None, max_length=1000)


class TaskCommentRequest(BaseModel):
    content: str = Field(min_length=1, max_length=1000)

    @field_validator("content")
    @classmethod
    def strip_content(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("备注内容必填")
        return value


class TaskCancelRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=1000)


class TaskRetryRequest(BaseModel):
    remark: str | None = Field(default=None, max_length=1000)


class TaskClaimRequest(BaseModel):
    worker_id: str = Field(min_length=1, max_length=36)
    current_step: str | None = Field(default=None, max_length=64)
    remark: str | None = Field(default=None, max_length=1000)


class TaskStepRequest(BaseModel):
    current_step: str = Field(min_length=1, max_length=64)
    remark: str | None = Field(default=None, max_length=1000)


class TaskCompleteRequest(BaseModel):
    remark: str | None = Field(default=None, max_length=1000)


class TaskFailRequest(BaseModel):
    error_code: str = Field(min_length=1, max_length=64)
    failure_step: str | None = Field(default=None, max_length=64)
    failure_remark: str | None = Field(default=None, max_length=1000)


class TaskEvidenceRequest(BaseModel):
    evidence_type: str = Field(min_length=1, max_length=32)
    file_name: str | None = Field(default=None, max_length=255)
    storage_url: str | None = Field(default=None, max_length=2000)
    content: str | None = Field(default=None, max_length=20000)
    error_code: str | None = Field(default=None, max_length=64)
    remark: str | None = Field(default=None, max_length=1000)
    metadata: dict = Field(default_factory=dict)


TERMINAL_TASK_STATUSES = {
    TaskStatus.completed.value,
    TaskStatus.failed.value,
    TaskStatus.cancelled.value,
}

TASK_STATUS_VALUES = {item.value for item in TaskStatus}
TASK_TYPE_VALUES = {item.value for item in TaskType}
TASK_RESULT_CODE_VALUES = {item.value for item in TaskResultCode}
TASK_ERROR_CODE_VALUES = {item.value for item in TaskErrorCode}
TASK_BLOCK_CODE_VALUES = {item.value for item in TaskBlockCode}
