"""Durable manual review of ambiguous history, separate from reply ownership."""
from datetime import datetime
from sqlalchemy import DateTime, ForeignKey, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from app.core.database import Base
from app.models.base import TimestampMixin, new_id


class HistoryReview(Base, TimestampMixin):
    __tablename__ = 'history_reviews'
    __table_args__ = (UniqueConstraint('binding_id', 'boundary_key', name='uq_history_review_boundary'),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    lead_id: Mapped[str] = mapped_column(ForeignKey('leads.id'), index=True)
    binding_id: Mapped[str] = mapped_column(ForeignKey('wechat_session_bindings.id'))
    boundary_key: Mapped[str] = mapped_column(String(64))
    reason: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(24), default='pending')
    revision: Mapped[int] = mapped_column(Integer, default=1)
    evidence: Mapped[dict] = mapped_column(JSON, default=dict)
    message_ids: Mapped[list] = mapped_column(JSON, default=list)
    resolutions: Mapped[list] = mapped_column(JSON, default=list)
    note: Mapped[str | None] = mapped_column(Text)
    resolved_by: Mapped[str | None] = mapped_column(String(36))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
