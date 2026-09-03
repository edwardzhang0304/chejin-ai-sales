from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Index, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.base import TimestampMixin, new_id, utcnow


class ManagedKnowledgeItem(Base, TimestampMixin):
    """Stable operator-facing identity for one business knowledge item."""

    __tablename__ = "managed_knowledge_items"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, default="chejin")
    title: Mapped[str] = mapped_column(String(120), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="draft")
    current_revision_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    draft_revision_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    draft_operation: Mapped[str | None] = mapped_column(String(16), nullable=True)
    last_editor_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    last_editor_name: Mapped[str] = mapped_column(String(64), nullable=False, default="system")
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint("status in ('draft', 'published', 'archived')", name="ck_managed_knowledge_item_status"),
        CheckConstraint(
            "draft_operation is null or draft_operation in ('create', 'update', 'archive')",
            name="ck_managed_knowledge_item_draft_operation",
        ),
        Index("idx_managed_knowledge_items_tenant_status_updated", "tenant_id", "status", "updated_at"),
    )


class ManagedKnowledgeRevision(Base):
    """Immutable content revision created only by a confirmed publication."""

    __tablename__ = "managed_knowledge_revisions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    item_id: Mapped[str] = mapped_column(String(36), ForeignKey("managed_knowledge_items.id", ondelete="RESTRICT"), nullable=False)
    release_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("knowledge_releases.id", ondelete="RESTRICT"), nullable=True)
    title: Mapped[str] = mapped_column(String(120), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    created_by_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_by_name: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)

    __table_args__ = (
        CheckConstraint("status in ('draft', 'published', 'archived')", name="ck_managed_knowledge_revision_status"),
        Index("idx_managed_knowledge_revisions_item_created", "item_id", "created_at"),
        Index("idx_managed_knowledge_revisions_release", "release_id"),
    )


class KnowledgeRelease(Base):
    """An immutable full snapshot used by Brain batches and release history."""

    __tablename__ = "knowledge_releases"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, default="chejin")
    version: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="published")
    action: Mapped[str] = mapped_column(String(16), nullable=False)
    source_release_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    operator_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    operator_name: Mapped[str] = mapped_column(String(64), nullable=False)
    change_summary: Mapped[str] = mapped_column(String(255), nullable=False)
    change_set: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    snapshot: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    snapshot_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    retrieval_index: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    retrieval_index_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)

    __table_args__ = (
        CheckConstraint("status in ('published', 'failed')", name="ck_knowledge_release_status"),
        CheckConstraint("action in ('bootstrap', 'create', 'update', 'archive', 'rollback')", name="ck_knowledge_release_action"),
        UniqueConstraint("tenant_id", "version", name="uq_knowledge_release_tenant_version"),
        Index("idx_knowledge_releases_tenant_published", "tenant_id", "published_at"),
    )


class CurrentKnowledgeRelease(Base):
    __tablename__ = "current_knowledge_releases"

    tenant_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    release_id: Mapped[str] = mapped_column(String(36), ForeignKey("knowledge_releases.id", ondelete="RESTRICT"), nullable=False, unique=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)


class KnowledgePublishPreview(Base):
    """Short-lived one-time preview; never visible to Brain."""

    __tablename__ = "knowledge_publish_previews"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, default="chejin")
    operation: Mapped[str] = mapped_column(String(16), nullable=False)
    item_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    target_release_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("knowledge_releases.id", ondelete="RESTRICT"), nullable=True)
    base_release_id: Mapped[str] = mapped_column(String(36), ForeignKey("knowledge_releases.id", ondelete="RESTRICT"), nullable=False)
    operator_id: Mapped[str] = mapped_column(String(36), nullable=False)
    operator_name: Mapped[str] = mapped_column(String(64), nullable=False)
    target_version: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    before_snapshot: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    after_snapshot: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    change_set: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    validation_issues: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    content_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    consumed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)

    __table_args__ = (
        CheckConstraint("operation in ('create', 'update', 'archive', 'rollback')", name="ck_knowledge_publish_preview_operation"),
        Index("idx_knowledge_publish_previews_expiry", "expires_at"),
    )
