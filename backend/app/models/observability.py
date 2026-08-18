from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.base import utcnow


class ProcessStageRun(Base):
    """Side-channel timing fact; never participates in business decisions."""

    __tablename__ = "process_stage_runs"

    stage_run_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    process_run_id: Mapped[str] = mapped_column(String(36), nullable=False)
    parent_stage_run_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    conversation_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    worker_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("workers.id"), nullable=True
    )
    stage_name: Mapped[str] = mapped_column(String(64), nullable=False)
    component: Mapped[str] = mapped_column(String(32), nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    queued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    queue_duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    execution_duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    trace_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )

Index(
    "idx_process_stage_runs_process_started",
    ProcessStageRun.process_run_id,
    ProcessStageRun.started_at,
)
Index(
    "idx_process_stage_runs_conversation_created",
    ProcessStageRun.conversation_id,
    ProcessStageRun.created_at,
)
Index(
    "idx_process_stage_runs_worker_created",
    ProcessStageRun.worker_id,
    ProcessStageRun.created_at,
)
