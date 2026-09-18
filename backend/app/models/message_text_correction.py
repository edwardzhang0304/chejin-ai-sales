"""Append-only OCR corrections. MessageEvent and send receipts stay immutable."""
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, JSON, LargeBinary, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, deferred, mapped_column

from app.core.database import Base
from app.models.base import new_id, utcnow


class MessageTextCorrection(Base):
    __tablename__ = "message_text_corrections"
    __table_args__ = (
        UniqueConstraint("message_event_id", "effective_version", name="uq_message_text_correction_version"),
        UniqueConstraint("message_event_id", "proof_sha256", name="uq_message_text_correction_proof"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    message_event_id: Mapped[str] = mapped_column(ForeignKey("message_events.id"), nullable=False)
    conversation_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    worker_id: Mapped[str] = mapped_column(ForeignKey("workers.id"), nullable=False)
    effective_version: Mapped[int] = mapped_column(Integer, nullable=False)
    previous_version: Mapped[int] = mapped_column(Integer, nullable=False)
    original_text_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    effective_text: Mapped[str] = mapped_column(Text, nullable=False)
    effective_text_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    proof_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    image_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    # Never fetch the private original screenshot for normal context rendering.
    image_bytes: Mapped[bytes] = deferred(mapped_column(LargeBinary, nullable=False))
    proof: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
