from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.models.base import TimestampMixin, new_id, utcnow


class Task(Base, TimestampMixin):
    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    task_type: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    result_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    block_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lead_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("leads.id"), nullable=True)
    sales_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("sales.id"), nullable=True)
    worker_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("workers.id"), nullable=True)
    original_task_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("tasks.id"), nullable=True)
    reply_action_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    current_step: Mapped[str | None] = mapped_column(String(64), nullable=True)
    failure_step: Mapped[str | None] = mapped_column(String(64), nullable=True)
    failure_remark: Mapped[str | None] = mapped_column(Text, nullable=True)
    cancel_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    remark: Mapped[str | None] = mapped_column(Text, nullable=True)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    updated_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    lead: Mapped["Lead | None"] = relationship()
    sales: Mapped["Sales | None"] = relationship()
    worker: Mapped["Worker | None"] = relationship()
    original_task: Mapped["Task | None"] = relationship(remote_side=[id])
    events: Mapped[list["TaskEvent"]] = relationship(back_populates="task", cascade="all, delete-orphan")
    notes: Mapped[list["TaskNote"]] = relationship(back_populates="task", cascade="all, delete-orphan")
    evidences: Mapped[list["TaskEvidence"]] = relationship(back_populates="task", cascade="all, delete-orphan")


Index("idx_tasks_type_status_created_at", Task.task_type, Task.status, Task.created_at.desc())
Index("idx_tasks_status_created_at", Task.status, Task.created_at.desc())
Index("idx_tasks_sales_status", Task.sales_id, Task.status)
Index("idx_tasks_worker_status", Task.worker_id, Task.status)
Index("idx_tasks_lead_type_status", Task.lead_id, Task.task_type, Task.status)
Index("idx_tasks_result_code", Task.result_code)
Index("idx_tasks_error_code", Task.error_code)
Index("idx_tasks_block_code", Task.block_code)
Index("idx_tasks_original_task_id", Task.original_task_id)
Index(
    "uq_tasks_reply_action_id",
    Task.reply_action_id,
    unique=True,
    sqlite_where=Task.reply_action_id.is_not(None),
    postgresql_where=Task.reply_action_id.is_not(None),
)


class TaskEvent(Base):
    __tablename__ = "task_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    task_id: Mapped[str] = mapped_column(String(36), ForeignKey("tasks.id"), nullable=False)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    from_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    to_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    result_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    block_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    current_step: Mapped[str | None] = mapped_column(String(64), nullable=True)
    operator_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    operator_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    worker_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("workers.id"), nullable=True)
    remark: Mapped[str | None] = mapped_column(Text, nullable=True)
    extra_metadata: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    task: Mapped[Task] = relationship(back_populates="events")
    worker: Mapped["Worker | None"] = relationship()


Index("idx_task_events_task_created_at", TaskEvent.task_id, TaskEvent.created_at.desc())
Index("idx_task_events_type_created_at", TaskEvent.event_type, TaskEvent.created_at.desc())


class TaskNote(Base):
    __tablename__ = "task_notes"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    task_id: Mapped[str] = mapped_column(String(36), ForeignKey("tasks.id"), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    operator_id: Mapped[str] = mapped_column(String(36), nullable=False)
    operator_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    task: Mapped[Task] = relationship(back_populates="notes")


Index("idx_task_notes_task_created_at", TaskNote.task_id, TaskNote.created_at.desc())


class TaskEvidence(Base):
    __tablename__ = "task_evidences"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    task_id: Mapped[str] = mapped_column(String(36), ForeignKey("tasks.id"), nullable=False)
    worker_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("workers.id"), nullable=True)
    evidence_type: Mapped[str] = mapped_column(String(32), nullable=False)
    file_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    storage_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    remark: Mapped[str | None] = mapped_column(Text, nullable=True)
    extra_metadata: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    task: Mapped[Task] = relationship(back_populates="evidences")
    worker: Mapped["Worker | None"] = relationship()


Index("idx_task_evidences_task_created_at", TaskEvidence.task_id, TaskEvidence.created_at.desc())
Index("idx_task_evidences_worker_created_at", TaskEvidence.worker_id, TaskEvidence.created_at.desc())
