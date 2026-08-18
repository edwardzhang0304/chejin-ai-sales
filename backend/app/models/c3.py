from datetime import date, datetime

from sqlalchemy import Boolean, CheckConstraint, Date, DateTime, Float, Index, Integer, JSON, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.base import TimestampMixin, new_id, utcnow


class Conversation(Base, TimestampMixin):
    __tablename__ = "conversations"

    conversation_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    lead_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    sales_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    worker_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="ai_active")
    ai_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    friend_state: Mapped[str] = mapped_column(String(32), nullable=False, default="friend_active")
    reply_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    recall_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    recall_daily_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    recall_daily_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    recall_cycle_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    recall_origin_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    next_recall_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    handoff_reason_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    handoff_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_inbound_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_outbound_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_ai_reply_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_sales_reply_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sales_first_reply_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    close_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


Index("idx_conversations_lead_status", Conversation.lead_id, Conversation.status)
Index("idx_conversations_sales_status", Conversation.sales_id, Conversation.status)
Index("idx_conversations_worker_status", Conversation.worker_id, Conversation.status)
Index("idx_conversations_last_inbound", Conversation.last_inbound_at.desc())


class MessageBatch(Base, TimestampMixin):
    __tablename__ = "message_batches"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    conversation_id: Mapped[str] = mapped_column(String(36), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="collecting")
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    trigger_type: Mapped[str] = mapped_column(String(32), nullable=False, default="customer_message")
    trigger_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    recall_cycle_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    retryable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    trigger_message_event_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    message_event_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    message_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    generation_no: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    generation_attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    generation_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    continuation_authorization_revision: Mapped[str | None] = mapped_column(
        String(128),
        nullable=True,
    )
    continuation_read_reason: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
    )
    origin_conversation_status: Mapped[str | None] = mapped_column(
        String(32),
        nullable=True,
    )
    trace_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    decision: Mapped[str | None] = mapped_column(String(32), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    suggested_action: Mapped[str | None] = mapped_column(String(64), nullable=True)
    superseded_by_batch_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    ai_request_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    ai_response_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    generated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


Index("idx_message_batches_conversation_created", MessageBatch.conversation_id, MessageBatch.created_at.desc())
Index("idx_message_batches_status_created", MessageBatch.status, MessageBatch.created_at.desc())
Index(
    "uq_message_batches_conversation_trigger",
    MessageBatch.conversation_id,
    MessageBatch.trigger_type,
    MessageBatch.trigger_key,
    unique=True,
    sqlite_where=text("trigger_key IS NOT NULL AND deleted_at IS NULL"),
    postgresql_where=text("trigger_key IS NOT NULL AND deleted_at IS NULL"),
)
Index(
    "uq_message_batches_active_conversation",
    MessageBatch.conversation_id,
    unique=True,
    sqlite_where=MessageBatch.active.is_(True),
    postgresql_where=MessageBatch.active.is_(True),
)


class ReplyAction(Base, TimestampMixin):
    __tablename__ = "reply_actions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    batch_id: Mapped[str] = mapped_column(String(36), nullable=False)
    conversation_id: Mapped[str] = mapped_column(String(36), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="draft")
    current: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    generation_no: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    decision: Mapped[str] = mapped_column(String(32), nullable=False, default="send_reply")
    reply_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    reply_text_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    risk_flags: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    evidence_refs: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    guard_result: Mapped[str | None] = mapped_column(String(32), nullable=True)
    handoff_reason_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    suggested_action: Mapped[str | None] = mapped_column(String(64), nullable=True)
    expire_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    send_token: Mapped[str | None] = mapped_column(String(128), nullable=True)
    claimed_by_worker_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    claimed_task_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    sending_claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ai_payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


Index("idx_reply_actions_batch_status", ReplyAction.batch_id, ReplyAction.status)
Index("idx_reply_actions_conversation_created", ReplyAction.conversation_id, ReplyAction.created_at.desc())
Index("idx_reply_actions_status_expire", ReplyAction.status, ReplyAction.expire_at)
Index("uq_reply_actions_batch_generation", ReplyAction.batch_id, ReplyAction.generation_no, unique=True)
Index(
    "uq_reply_actions_current_batch",
    ReplyAction.batch_id,
    unique=True,
    sqlite_where=ReplyAction.current.is_(True),
    postgresql_where=ReplyAction.current.is_(True),
)


class ReplyActionVehicleFact(Base):
    __tablename__ = "reply_action_vehicle_facts"

    reply_action_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    vehicle_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    fact_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    vehicle_updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


Index(
    "idx_reply_action_vehicle_facts_vehicle_action",
    ReplyActionVehicleFact.vehicle_id,
    ReplyActionVehicleFact.reply_action_id,
)


class SentAck(Base):
    __tablename__ = "sent_acks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    reply_action_id: Mapped[str] = mapped_column(String(36), nullable=False)
    task_id: Mapped[str] = mapped_column(String(36), nullable=False)
    worker_id: Mapped[str] = mapped_column(String(36), nullable=False)
    client_instance_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    send_token: Mapped[str] = mapped_column(String(128), nullable=False)
    send_result: Mapped[str] = mapped_column(String(32), nullable=False)
    action_phase: Mapped[str] = mapped_column(String(32), nullable=False)
    reply_text_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    sidecar_run_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    evidence: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    remark: Mapped[str | None] = mapped_column(Text, nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


Index("uq_sent_acks_reply_action", SentAck.reply_action_id, unique=True)
Index("idx_sent_acks_task_created", SentAck.task_id, SentAck.created_at.desc())


class HandoffEvent(Base, TimestampMixin):
    __tablename__ = "handoff_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    conversation_id: Mapped[str] = mapped_column(String(36), nullable=False)
    batch_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="created")
    handoff_reason_code: Mapped[str] = mapped_column(String(64), nullable=False)
    reason_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    trigger_message_event_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    risk_flags: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    evidence_refs: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    ai_payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    notify_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    notify_attempted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    notify_completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    notify_error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    notify_error_summary: Mapped[str | None] = mapped_column(String(512), nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint(
            "notify_status IS NULL OR notify_status IN ('pending','sending','succeeded','failed')",
            name="ck_handoff_events_notify_status",
        ),
    )


Index("idx_handoff_events_conversation_created", HandoffEvent.conversation_id, HandoffEvent.created_at.desc())
Index("idx_handoff_events_status_created", HandoffEvent.status, HandoffEvent.created_at.desc())
Index("idx_handoff_events_notify_status", HandoffEvent.notify_status, HandoffEvent.created_at)
Index(
    "uq_handoff_events_open_conversation",
    HandoffEvent.conversation_id,
    unique=True,
    sqlite_where=text("closed_at IS NULL AND deleted_at IS NULL"),
    postgresql_where=text("closed_at IS NULL AND deleted_at IS NULL"),
)
