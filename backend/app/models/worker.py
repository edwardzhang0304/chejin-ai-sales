from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.base import TimestampMixin, new_id


class Worker(Base, TimestampMixin):
    __tablename__ = "workers"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    worker_name: Mapped[str] = mapped_column(String(64), nullable=False)
    device_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    platform: Mapped[str] = mapped_column(String(32), nullable=False, default="mac")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    online_status: Mapped[str] = mapped_column(String(32), nullable=False, default="offline")
    running_status: Mapped[str] = mapped_column(String(32), nullable=False, default="idle")
    run_status: Mapped[str] = mapped_column(String(32), nullable=False, default="paused")
    rpa_component_status: Mapped[str] = mapped_column(String(32), nullable=False, default="unavailable")
    wechat_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    current_task: Mapped[str | None] = mapped_column(String(255), nullable=True)
    current_step: Mapped[str | None] = mapped_column(String(64), nullable=True)
    local_lock_summary: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    worker_token_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    worker_token_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    client_binding_state: Mapped[str | None] = mapped_column(String(64), nullable=True)
    client_instance_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    bound_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    remark: Mapped[str | None] = mapped_column(Text, nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


Index("idx_workers_enabled_status", Worker.enabled, Worker.online_status, Worker.running_status)
Index("idx_workers_last_heartbeat_at", Worker.last_heartbeat_at)
Index("idx_workers_client_instance_id", Worker.client_instance_id)
Index("idx_workers_run_status", Worker.run_status, Worker.rpa_component_status)


class WorkerHeartbeatLog(Base):
    __tablename__ = "worker_heartbeats"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    worker_id: Mapped[str] = mapped_column(String(36), ForeignKey("workers.id"), nullable=False)
    client_instance_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    online_status: Mapped[str] = mapped_column(String(32), nullable=False)
    run_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    runtime_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    rpa_component_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    wechat_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    current_task_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    current_step: Mapped[str | None] = mapped_column(String(64), nullable=True)
    local_lock_summary: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


Index("idx_worker_heartbeats_worker_created_at", WorkerHeartbeatLog.worker_id, WorkerHeartbeatLog.created_at.desc())
