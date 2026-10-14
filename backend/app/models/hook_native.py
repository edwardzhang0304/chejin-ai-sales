"""Durable observation receipts awaiting the native domain ingestion adapter."""
from datetime import datetime
from sqlalchemy import DateTime, ForeignKey, Index, JSON, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from app.core.database import Base
from app.models.base import new_id, utcnow


class HookNativeReceipt(Base):
    __tablename__ = 'hook_native_receipts'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    worker_id: Mapped[str] = mapped_column(String(36), ForeignKey('workers.id'), nullable=False)
    client_instance_id: Mapped[str] = mapped_column(String(128), nullable=False)
    self_wxid: Mapped[str] = mapped_column(String(64), nullable=False)
    account_epoch: Mapped[str] = mapped_column(String(36), nullable=False)
    binding_id: Mapped[str] = mapped_column(String(36), ForeignKey('wechat_session_bindings.id'), nullable=False)
    binding_revision: Mapped[str] = mapped_column(String(32), nullable=False)
    event_uuid: Mapped[str] = mapped_column(String(36), nullable=False)
    payload_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    receipt_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    dispatch_status: Mapped[str] = mapped_column(String(32), nullable=False, default='pending', server_default='pending')
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    __table_args__ = (UniqueConstraint('worker_id', 'event_uuid', name='uq_hook_native_receipt_event'),)


Index('idx_hook_native_receipt_dispatch', HookNativeReceipt.dispatch_status, HookNativeReceipt.created_at)


class HookHistoryReceipt(Base):
    __tablename__ = 'hook_history_receipts'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    worker_id: Mapped[str] = mapped_column(String(36), ForeignKey('workers.id'), nullable=False)
    binding_id: Mapped[str] = mapped_column(String(36), ForeignKey('wechat_session_bindings.id'), nullable=False)
    capture_id: Mapped[str] = mapped_column(String(36), nullable=False)
    payload_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    result_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    __table_args__ = (UniqueConstraint('worker_id', 'capture_id', name='uq_hook_history_capture'),)


class HookVoiceReceipt(Base):
    __tablename__='hook_voice_receipts'
    __table_args__=(UniqueConstraint('worker_id','message_event_id',name='uq_hook_voice_parent'),)
    id: Mapped[str]=mapped_column(String(36),primary_key=True,default=new_id)
    worker_id: Mapped[str]=mapped_column(String(36),ForeignKey('workers.id'),nullable=False)
    message_event_id: Mapped[str]=mapped_column(String(36),ForeignKey('message_events.id'),nullable=False)
    payload_digest: Mapped[str]=mapped_column(String(64),nullable=False)
    payload_json: Mapped[dict]=mapped_column(JSON,nullable=False)
    result_json: Mapped[dict]=mapped_column(JSON,nullable=False)
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True),nullable=False,default=utcnow)
