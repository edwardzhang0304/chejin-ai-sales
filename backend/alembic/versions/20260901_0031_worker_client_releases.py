"""Add the authoritative worker client release registry.

Revision ID: 20260901_0031
Revises: 20260830_0030
"""

from alembic import op
import sqlalchemy as sa


revision = "20260901_0031"
down_revision = "20260830_0030"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "worker_client_releases",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("channel", sa.String(length=16), nullable=False),
        sa.Column("platform", sa.String(length=32), nullable=False),
        sa.Column("version", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("artifact_storage_key", sa.String(length=512), nullable=True),
        sa.Column("artifact_size_bytes", sa.Integer(), nullable=True),
        sa.Column("artifact_sha256", sa.String(length=64), nullable=True),
        sa.Column("manifest_signature", sa.Text(), nullable=True),
        sa.Column("signature_key_id", sa.String(length=64), nullable=True),
        sa.Column("git_commit", sa.String(length=40), nullable=True),
        sa.Column("package_manifest_sha256", sa.String(length=64), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("release_notes", sa.Text(), nullable=False),
        sa.Column("minimum_updater_version", sa.String(length=32), nullable=False),
        sa.Column("rollback_safe", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("status in ('draft', 'published', 'withdrawn')", name="ck_worker_client_release_status"),
        sa.CheckConstraint("artifact_size_bytes is null or artifact_size_bytes > 0", name="ck_worker_client_release_artifact_size"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("channel", "platform", "version", name="uq_worker_client_release_channel_platform_version"),
    )
    op.create_index(
        "idx_worker_client_releases_lookup",
        "worker_client_releases",
        ["channel", "platform", "status", "published_at"],
        unique=False,
    )
    op.create_table(
        "worker_client_release_download_leases",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("release_id", sa.String(length=36), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("requested_current_version", sa.String(length=32), nullable=False),
        sa.Column("client_instance_hash", sa.String(length=64), nullable=True),
        sa.Column("requester_ip_hash", sa.String(length=64), nullable=True),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["release_id"],
            ["worker_client_releases.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash"),
    )
    op.create_index(
        "idx_worker_client_release_download_leases_release_issued",
        "worker_client_release_download_leases",
        ["release_id", "issued_at"],
        unique=False,
    )
    op.create_index(
        "idx_worker_client_release_download_leases_expires_at",
        "worker_client_release_download_leases",
        ["expires_at"],
        unique=False,
    )
    op.create_table(
        "worker_client_release_query_throttles",
        sa.Column("scope", sa.String(length=16), nullable=False),
        sa.Column("key_hash", sa.String(length=64), nullable=False),
        sa.Column("window_started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("request_count", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "scope in ('ip', 'instance')",
            name="ck_worker_client_release_query_scope",
        ),
        sa.CheckConstraint(
            "request_count >= 0",
            name="ck_worker_client_release_query_count",
        ),
        sa.PrimaryKeyConstraint("scope", "key_hash"),
    )
    op.create_index(
        "idx_worker_client_release_query_throttles_updated_at",
        "worker_client_release_query_throttles",
        ["updated_at"],
        unique=False,
    )


def downgrade() -> None:
    raise RuntimeError(
        "IRREVERSIBLE_MIGRATION_20260901_0031: worker client release "
        "publication records are authoritative data. Take a verified backup "
        "and use an explicit forward migration instead of automatic downgrade."
    )
