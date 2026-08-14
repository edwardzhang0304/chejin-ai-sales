from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.models.base import TimestampMixin, new_id, utcnow


class WechatSessionBinding(Base, TimestampMixin):
    __tablename__ = "wechat_session_bindings"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    conversation_id: Mapped[str] = mapped_column(String(36), nullable=False, default=new_id)
    lead_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("leads.id"), nullable=True)
    sales_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("sales.id"), nullable=True)
    worker_id: Mapped[str] = mapped_column(String(36), ForeignKey("workers.id"), nullable=False)
    remark_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    rpa_session_key: Mapped[str] = mapped_column(String(255), nullable=False)
    row_fingerprint: Mapped[str] = mapped_column(String(255), nullable=False)
    bind_status: Mapped[str] = mapped_column(String(32), nullable=False, default="unbound")
    listen_status: Mapped[str] = mapped_column(String(32), nullable=False, default="not_started")
    allow_listening: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    authorization_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    disable_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    disabled_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    replacement_binding_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("wechat_session_bindings.id"),
        nullable=True,
    )
    unread_hint: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    last_message_preview: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_message_preview_time: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
    )
    last_message_observation_id: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
    )
    last_observed_unread_hint: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
    )
    unread_evidence_key: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
    )
    unread_generation: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
    )
    consumed_unread_generation: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
    )
    ocr_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    last_ingested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_read_dispatched_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    last_read_completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    last_read_result: Mapped[str | None] = mapped_column(String(32), nullable=True)
    last_read_run_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    no_change_read_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_read_due_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    last_read_conversation_status: Mapped[str | None] = mapped_column(
        String(32),
        nullable=True,
    )
    last_scan_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    lead: Mapped["Lead | None"] = relationship()
    sales: Mapped["Sales | None"] = relationship()
    worker: Mapped["Worker"] = relationship()

    __table_args__ = (
        UniqueConstraint("conversation_id", name="uq_wechat_session_bindings_conversation_id"),
        UniqueConstraint("worker_id", "rpa_session_key", name="uq_wechat_session_bindings_worker_session"),
    )


Index("idx_wechat_bindings_lead_status", WechatSessionBinding.lead_id, WechatSessionBinding.bind_status)
Index("idx_wechat_bindings_worker_status", WechatSessionBinding.worker_id, WechatSessionBinding.bind_status, WechatSessionBinding.listen_status)
Index("idx_wechat_bindings_remark_code", WechatSessionBinding.remark_code)
Index(
    "idx_wechat_bindings_worker_read_due",
    WechatSessionBinding.worker_id,
    WechatSessionBinding.bind_status,
    WechatSessionBinding.listen_status,
    WechatSessionBinding.next_read_due_at,
)
Index(
    "uq_wechat_bindings_effective_remark_code",
    WechatSessionBinding.remark_code,
    unique=True,
    sqlite_where=text(
        "deleted_at IS NULL AND remark_code IS NOT NULL AND remark_code <> '' "
        "AND bind_status = 'bound'"
    ),
    postgresql_where=text(
        "deleted_at IS NULL AND remark_code IS NOT NULL AND remark_code <> '' "
        "AND bind_status = 'bound'"
    ),
)


class MessageEvent(Base):
    __tablename__ = "message_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    conversation_id: Mapped[str] = mapped_column(String(36), nullable=False)
    binding_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("wechat_session_bindings.id"), nullable=True)
    lead_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("leads.id"), nullable=True)
    sales_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("sales.id"), nullable=True)
    worker_id: Mapped[str] = mapped_column(String(36), ForeignKey("workers.id"), nullable=False)
    rpa_session_key: Mapped[str] = mapped_column(String(255), nullable=False)
    read_run_id: Mapped[str] = mapped_column(String(128), nullable=False)
    contract_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    source_message_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    dedupe_key: Mapped[str] = mapped_column(String(255), nullable=False)
    sender_role: Mapped[str] = mapped_column(String(32), nullable=False)
    message_type: Mapped[str] = mapped_column(String(32), nullable=False)
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    evidence: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    ocr_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    item_state: Mapped[str | None] = mapped_column(String(32), nullable=True)
    flow_state: Mapped[str | None] = mapped_column(String(32), nullable=True)
    occurred_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    observation_order: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)

    binding: Mapped["WechatSessionBinding | None"] = relationship()
    worker: Mapped["Worker"] = relationship()

    __table_args__ = (
        UniqueConstraint("conversation_id", "dedupe_key", name="uq_message_events_conversation_dedupe"),
        UniqueConstraint("worker_id", "conversation_id", "dedupe_key", name="uq_message_events_worker_conversation_dedupe"),
        UniqueConstraint("conversation_id", "read_run_id", "source_message_key", name="uq_message_events_read_source"),
    )


Index("idx_message_events_conversation_ingested", MessageEvent.conversation_id, MessageEvent.ingested_at.desc())
Index(
    "idx_message_events_conversation_observed",
    MessageEvent.conversation_id,
    MessageEvent.observed_at,
    MessageEvent.observation_order,
)
Index("idx_message_events_worker_ingested", MessageEvent.worker_id, MessageEvent.ingested_at.desc())
Index("idx_message_events_lead_ingested", MessageEvent.lead_id, MessageEvent.ingested_at.desc())


class WechatRecoverySettlement(Base):
    """Backend-owned terminal record for UI-free media fact recovery."""

    __tablename__ = "wechat_recovery_settlements"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    worker_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("workers.id"), nullable=False
    )
    conversation_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    recovery_transaction_id: Mapped[str] = mapped_column(String(128), nullable=False)
    action_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    source_message_key_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    settlement_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="authorized")
    source_results_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    trace_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    settled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    worker: Mapped["Worker"] = relationship()

    __table_args__ = (
        UniqueConstraint(
            "worker_id",
            "recovery_transaction_id",
            name="uq_wechat_recovery_settlement_worker_transaction",
        ),
    )


Index(
    "idx_wechat_recovery_settlement_conversation",
    WechatRecoverySettlement.conversation_id,
    WechatRecoverySettlement.settled_at.desc(),
)


class WechatScanRun(Base):
    __tablename__ = "wechat_scan_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    worker_id: Mapped[str] = mapped_column(String(36), ForeignKey("workers.id"), nullable=False)
    scan_id: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="processed")
    response_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    worker: Mapped["Worker"] = relationship()

    __table_args__ = (UniqueConstraint("scan_id", name="uq_wechat_scan_runs_scan_id"),)


Index("idx_wechat_scan_runs_worker_created", WechatScanRun.worker_id, WechatScanRun.created_at.desc())
