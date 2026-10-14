"""Database-wide Brain call ownership; no capacity is held for queued sends."""
from sqlalchemy import DateTime, Integer, String, ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from app.core.database import Base
from app.models.base import utcnow


class AIGenerationGate(Base):
    __tablename__ = "ai_generation_gate"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class AIGenerationSlot(Base):
    __tablename__ = "ai_generation_slots"
    __table_args__ = (UniqueConstraint("conversation_id", name="uq_ai_generation_conversation"),)
    token: Mapped[str] = mapped_column(String(36), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(String(36), ForeignKey("conversations.conversation_id"), nullable=False)
    batch_id: Mapped[str] = mapped_column(String(36), ForeignKey("message_batches.id"), nullable=False)
    generation_attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    owner_host: Mapped[str] = mapped_column(String(64), nullable=False)
    owner_pid: Mapped[int] = mapped_column(Integer, nullable=False)
    owner_process_started: Mapped[str] = mapped_column(String(64), nullable=False)
    acquired_at: Mapped[object] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
