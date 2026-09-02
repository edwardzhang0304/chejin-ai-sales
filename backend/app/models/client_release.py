from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.base import new_id, utcnow


class WorkerClientRelease(Base):
    """Immutable Windows client release authority.

    Download credentials are never stored here. ``artifact_storage_key`` is
    an immutable server-side locator; every public query receives a new,
    append-only short-lived download lease for this same package identity.
    """

    __tablename__ = "worker_client_releases"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    channel: Mapped[str] = mapped_column(String(16), nullable=False)
    platform: Mapped[str] = mapped_column(String(32), nullable=False)
    version: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="draft")
    artifact_storage_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    artifact_size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    artifact_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    manifest_signature: Mapped[str | None] = mapped_column(Text, nullable=True)
    signature_key_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    git_commit: Mapped[str | None] = mapped_column(String(40), nullable=True)
    package_manifest_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    release_notes: Mapped[str] = mapped_column(Text, nullable=False, default="")
    minimum_updater_version: Mapped[str] = mapped_column(String(32), nullable=False, default="0.9.59")
    rollback_safe: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint("channel", "platform", "version", name="uq_worker_client_release_channel_platform_version"),
        CheckConstraint("status in ('draft', 'published', 'withdrawn')", name="ck_worker_client_release_status"),
        CheckConstraint("artifact_size_bytes is null or artifact_size_bytes > 0", name="ck_worker_client_release_artifact_size"),
    )


Index(
    "idx_worker_client_releases_lookup",
    WorkerClientRelease.channel,
    WorkerClientRelease.platform,
    WorkerClientRelease.status,
    WorkerClientRelease.published_at,
)


class WorkerClientReleaseDownloadLease(Base):
    """Append-only audit row for one short-lived package pickup token."""

    __tablename__ = "worker_client_release_download_leases"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    release_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("worker_client_releases.id", ondelete="RESTRICT"),
        nullable=False,
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    requested_current_version: Mapped[str] = mapped_column(String(32), nullable=False)
    client_instance_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    requester_ip_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        Index(
            "idx_worker_client_release_download_leases_release_issued",
            "release_id",
            "issued_at",
        ),
        Index(
            "idx_worker_client_release_download_leases_expires_at",
            "expires_at",
        ),
    )


class WorkerClientReleaseQueryThrottle(Base):
    """Bounded public release-query counter keyed only by one-way hashes."""

    __tablename__ = "worker_client_release_query_throttles"

    scope: Mapped[str] = mapped_column(String(16), primary_key=True)
    key_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    window_started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    request_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )

    __table_args__ = (
        CheckConstraint("scope in ('ip', 'instance')", name="ck_worker_client_release_query_scope"),
        CheckConstraint("request_count >= 0", name="ck_worker_client_release_query_count"),
        Index(
            "idx_worker_client_release_query_throttles_updated_at",
            "updated_at",
        ),
    )
