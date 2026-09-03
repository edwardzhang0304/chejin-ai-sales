"""Add immutable knowledge publication and batch release binding.

Revision ID: 20260903_0032
Revises: 20260901_0031
"""

from alembic import op
import sqlalchemy as sa


revision = "20260903_0032"
down_revision = "20260901_0031"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "managed_knowledge_items",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("title", sa.String(length=120), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("current_revision_id", sa.String(length=36), nullable=True),
        sa.Column("draft_revision_id", sa.String(length=36), nullable=True),
        sa.Column("draft_operation", sa.String(length=16), nullable=True),
        sa.Column("last_editor_id", sa.String(length=36), nullable=True),
        sa.Column("last_editor_name", sa.String(length=64), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("status in ('draft', 'published', 'archived')", name="ck_managed_knowledge_item_status"),
        sa.CheckConstraint(
            "draft_operation is null or draft_operation in ('create', 'update', 'archive')",
            name="ck_managed_knowledge_item_draft_operation",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_managed_knowledge_items_tenant_status_updated", "managed_knowledge_items", ["tenant_id", "status", "updated_at"])

    op.create_table(
        "knowledge_releases",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("version", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("action", sa.String(length=16), nullable=False),
        sa.Column("source_release_id", sa.String(length=36), nullable=True),
        sa.Column("operator_id", sa.String(length=36), nullable=True),
        sa.Column("operator_name", sa.String(length=64), nullable=False),
        sa.Column("change_summary", sa.String(length=255), nullable=False),
        sa.Column("change_set", sa.JSON(), nullable=False),
        sa.Column("snapshot", sa.JSON(), nullable=False),
        sa.Column("snapshot_sha256", sa.String(length=64), nullable=False),
        sa.Column("retrieval_index", sa.JSON(), nullable=False),
        sa.Column("retrieval_index_sha256", sa.String(length=64), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("status in ('published', 'failed')", name="ck_knowledge_release_status"),
        sa.CheckConstraint("action in ('bootstrap', 'create', 'update', 'archive', 'rollback')", name="ck_knowledge_release_action"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "version", name="uq_knowledge_release_tenant_version"),
    )
    op.create_index("idx_knowledge_releases_tenant_published", "knowledge_releases", ["tenant_id", "published_at"])

    op.create_table(
        "current_knowledge_releases",
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("release_id", sa.String(length=36), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["release_id"], ["knowledge_releases.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("tenant_id"),
        sa.UniqueConstraint("release_id"),
    )

    op.create_table(
        "managed_knowledge_revisions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("item_id", sa.String(length=36), nullable=False),
        sa.Column("release_id", sa.String(length=36), nullable=True),
        sa.Column("title", sa.String(length=120), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("created_by_id", sa.String(length=36), nullable=True),
        sa.Column("created_by_name", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("status in ('draft', 'published', 'archived')", name="ck_managed_knowledge_revision_status"),
        sa.ForeignKeyConstraint(["item_id"], ["managed_knowledge_items.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["release_id"], ["knowledge_releases.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_managed_knowledge_revisions_item_created", "managed_knowledge_revisions", ["item_id", "created_at"])
    op.create_index("idx_managed_knowledge_revisions_release", "managed_knowledge_revisions", ["release_id"])

    op.create_table(
        "knowledge_publish_previews",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("operation", sa.String(length=16), nullable=False),
        sa.Column("item_id", sa.String(length=36), nullable=True),
        sa.Column("target_release_id", sa.String(length=36), nullable=True),
        sa.Column("base_release_id", sa.String(length=36), nullable=False),
        sa.Column("operator_id", sa.String(length=36), nullable=False),
        sa.Column("operator_name", sa.String(length=64), nullable=False),
        sa.Column("target_version", sa.String(length=32), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("before_snapshot", sa.JSON(), nullable=False),
        sa.Column("after_snapshot", sa.JSON(), nullable=False),
        sa.Column("change_set", sa.JSON(), nullable=False),
        sa.Column("validation_issues", sa.JSON(), nullable=False),
        sa.Column("content_digest", sa.String(length=64), nullable=False),
        sa.Column("consumed", sa.Boolean(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("operation in ('create', 'update', 'archive', 'rollback')", name="ck_knowledge_publish_preview_operation"),
        sa.ForeignKeyConstraint(["base_release_id"], ["knowledge_releases.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["target_release_id"], ["knowledge_releases.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_knowledge_publish_previews_expiry", "knowledge_publish_previews", ["expires_at"])

    op.add_column("message_batches", sa.Column("knowledge_release_id", sa.String(length=36), nullable=True))
    op.create_foreign_key(
        "fk_message_batches_knowledge_release_id",
        "message_batches",
        "knowledge_releases",
        ["knowledge_release_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index("idx_message_batches_knowledge_release", "message_batches", ["knowledge_release_id"])


def downgrade() -> None:
    raise RuntimeError(
        "IRREVERSIBLE_MIGRATION_20260903_0032: knowledge releases and batch "
        "bindings are authoritative history. Restore a verified backup or use "
        "an explicit forward migration."
    )
