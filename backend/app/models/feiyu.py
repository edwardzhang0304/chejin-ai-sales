from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, JSON, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.base import new_id, utcnow


class FeiyuLeadReceipt(Base):
    """Durable idempotency result for one account-scoped Feiyu clue."""

    __tablename__ = "feiyu_lead_receipts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    account_id: Mapped[str] = mapped_column(String(128), nullable=False)
    clue_id: Mapped[str] = mapped_column(String(128), nullable=False)
    lead_id: Mapped[str] = mapped_column(String(36), ForeignKey("leads.id"), nullable=False)
    contact_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    response_status: Mapped[int] = mapped_column(Integer, nullable=False)
    response_body: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint("account_id", "clue_id", name="uq_feiyu_lead_receipts_account_clue"),
        Index("idx_feiyu_lead_receipts_lead", "lead_id"),
    )
