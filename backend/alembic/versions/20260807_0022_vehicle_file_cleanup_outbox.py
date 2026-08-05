"""add durable vehicle file cleanup outbox

Revision ID: 20260807_0022
Revises: 20260806_0021
Create Date: 2026-08-07
"""

from alembic import op
import sqlalchemy as sa


revision = "20260807_0022"
down_revision = "20260806_0021"
branch_labels = None
depends_on = None

SCHEMA = "wechat_ai_customer_service"


def upgrade() -> None:
    op.alter_column("operation_logs", "target_id", type_=sa.String(length=128), existing_type=sa.String(length=36))
    op.create_table(
        "vehicle_file_cleanups",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("vehicle_id", sa.String(length=128), nullable=False),
        sa.Column("storage_key", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("storage_key", name="uq_vehicle_file_cleanups_storage_key"),
        schema=SCHEMA,
    )
    op.create_index(
        "idx_vehicle_file_cleanups_status_created",
        "vehicle_file_cleanups",
        ["status", "created_at"],
        schema=SCHEMA,
    )


def downgrade() -> None:
    raise RuntimeError(
        "IRREVERSIBLE_MIGRATION_20260807_0022: vehicle file cleanup records may be the only durable "
        "reference to files awaiting deletion. Create a verified backup and use a reviewed forward migration."
    )
