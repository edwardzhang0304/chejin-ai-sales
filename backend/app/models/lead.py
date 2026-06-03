from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy import JSON
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.models.base import TimestampMixin, new_id, utcnow


class Lead(Base, TimestampMixin):
    __tablename__ = "leads"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    customer_name: Mapped[str] = mapped_column(String(50), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="unassigned")
    source_type: Mapped[str] = mapped_column(String(32), nullable=False, default="manual")
    source_name_snapshot: Mapped[str] = mapped_column(String(64), nullable=False, default="人工录入")
    sales_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("sales.id"), nullable=True)
    assigned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    assign_status: Mapped[str] = mapped_column(String(32), nullable=False, default="unassigned")
    assign_failure_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    remark: Mapped[str | None] = mapped_column(Text, nullable=True)
    invalid_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    invalid_remark: Mapped[str | None] = mapped_column(Text, nullable=True)
    invalid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    invalid_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    duplicate_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_duplicate_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    custom_fields: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_by: Mapped[str] = mapped_column(String(36), nullable=False)
    updated_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    sales: Mapped["Sales | None"] = relationship(back_populates="leads")
    contacts: Mapped[list["LeadContact"]] = relationship(back_populates="lead", cascade="all, delete-orphan")
    notes: Mapped[list["LeadNote"]] = relationship(back_populates="lead", cascade="all, delete-orphan")
    assignments: Mapped[list["LeadAssignment"]] = relationship(back_populates="lead", cascade="all, delete-orphan")
    duplicate_events: Mapped[list["LeadDuplicateEvent"]] = relationship(back_populates="lead", cascade="all, delete-orphan")


Index("idx_leads_status_created_at", Lead.status, Lead.created_at.desc())
Index("idx_leads_sales_id_status", Lead.sales_id, Lead.status)
Index("idx_leads_created_by_created_at", Lead.created_by, Lead.created_at.desc())
Index("idx_leads_last_duplicate_at", Lead.last_duplicate_at.desc())


class LeadContact(Base):
    __tablename__ = "lead_contacts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    lead_id: Mapped[str] = mapped_column(String(36), ForeignKey("leads.id"), nullable=False)
    contact_type: Mapped[str] = mapped_column(String(16), nullable=False)
    contact_value_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    contact_value_normalized: Mapped[str] = mapped_column(String(128), nullable=False)
    contact_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    masked_value: Mapped[str] = mapped_column(String(128), nullable=False)
    is_primary: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    lead: Mapped[Lead] = relationship(back_populates="contacts")


Index("idx_lead_contacts_lead_id", LeadContact.lead_id)
Index("idx_lead_contacts_type_hash", LeadContact.contact_type, LeadContact.contact_hash)


class LeadNote(Base):
    __tablename__ = "lead_notes"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    lead_id: Mapped[str] = mapped_column(String(36), ForeignKey("leads.id"), nullable=False)
    note_type: Mapped[str] = mapped_column(String(32), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    operator_id: Mapped[str] = mapped_column(String(36), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    lead: Mapped[Lead] = relationship(back_populates="notes")


class LeadDuplicateEvent(Base):
    __tablename__ = "lead_duplicate_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    lead_id: Mapped[str] = mapped_column(String(36), ForeignKey("leads.id"), nullable=False)
    matched_contact_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    submitted_customer_name: Mapped[str] = mapped_column(String(50), nullable=False)
    submitted_phone_masked: Mapped[str] = mapped_column(String(64), nullable=False)
    submitted_wechat_masked: Mapped[str | None] = mapped_column(Text, nullable=True)
    submitted_email_masked: Mapped[str | None] = mapped_column(Text, nullable=True)
    submitted_remark: Mapped[str | None] = mapped_column(Text, nullable=True)
    submitted_payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    operator_id: Mapped[str] = mapped_column(String(36), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    lead: Mapped[Lead] = relationship(back_populates="duplicate_events")


class LeadAssignment(Base):
    __tablename__ = "lead_assignments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    lead_id: Mapped[str] = mapped_column(String(36), ForeignKey("leads.id"), nullable=False)
    from_sales_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("sales.id"), nullable=True)
    to_sales_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("sales.id"), nullable=True)
    assignment_type: Mapped[str] = mapped_column(String(32), nullable=False)
    assignment_status: Mapped[str] = mapped_column(String(32), nullable=False)
    failure_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    round_robin_cursor_before: Mapped[str | None] = mapped_column(String(36), nullable=True)
    round_robin_cursor_after: Mapped[str | None] = mapped_column(String(36), nullable=True)
    operator_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    remark: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    lead: Mapped[Lead] = relationship(back_populates="assignments")


class AssignmentRoundRobinState(Base):
    __tablename__ = "assignment_round_robin_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    current_sales_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("sales.id"), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    __table_args__ = (UniqueConstraint("id", name="uq_assignment_round_robin_state_id"),)
