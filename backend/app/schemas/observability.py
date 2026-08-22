from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator
from uuid import UUID


StageStatus = Literal["running", "succeeded", "failed", "cancelled", "abandoned"]


class ProcessStageEventIn(BaseModel):
    process_run_id: str = Field(min_length=36, max_length=36)
    stage_run_id: str = Field(min_length=36, max_length=36)
    parent_stage_run_id: str | None = Field(default=None, min_length=36, max_length=36)
    conversation_id: str | None = Field(default=None, min_length=1, max_length=36)
    stage_name: str = Field(min_length=1, max_length=64)
    component: str = Field(min_length=1, max_length=32)
    attempt: int = Field(default=1, ge=1, le=1000)
    queued_at: datetime | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    queue_duration_ms: int | None = Field(default=None, ge=0)
    execution_duration_ms: int | None = Field(default=None, ge=0)
    status: StageStatus
    error_code: str | None = Field(default=None, max_length=64)
    trace_id: str | None = Field(default=None, max_length=128)

    @field_validator(
        "process_run_id",
        "stage_run_id",
        "parent_stage_run_id",
        mode="before",
    )
    @classmethod
    def validate_uuid_ids(cls, value):
        if value is None:
            return None
        try:
            return str(UUID(str(value)))
        except (TypeError, ValueError) as exc:
            raise ValueError("必须是标准 UUID") from exc

    @model_validator(mode="after")
    def validate_terminal_shape(self):
        if self.status == "running" and self.ended_at is not None:
            raise ValueError("running 阶段不得包含 ended_at")
        if self.status != "running" and self.started_at is None:
            raise ValueError("终态阶段必须包含 started_at")
        if self.status != "running" and self.ended_at is None:
            raise ValueError("终态阶段必须包含 ended_at")
        if (
            self.started_at is not None
            and self.ended_at is not None
            and self.ended_at < self.started_at
        ):
            raise ValueError("ended_at 不得早于 started_at")
        if self.status == "succeeded" and self.error_code:
            raise ValueError("成功阶段不得携带 error_code")
        if self.status == "failed" and not str(self.error_code or "").strip():
            raise ValueError("失败阶段必须携带 error_code")
        return self


class ProcessStageEventsRequest(BaseModel):
    events: list[ProcessStageEventIn] = Field(min_length=1, max_length=200)
