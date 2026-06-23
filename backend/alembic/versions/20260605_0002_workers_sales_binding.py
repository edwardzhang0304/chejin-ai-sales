"""add workers and sales binding

Revision ID: 20260605_0002
Revises: 20260603_0001
Create Date: 2026-06-05
"""

from alembic import op
import sqlalchemy as sa


revision = "20260605_0002"
down_revision = "20260603_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "workers",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("worker_name", sa.String(length=64), nullable=False),
        sa.Column("device_name", sa.String(length=128), nullable=True),
        sa.Column("platform", sa.String(length=32), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("online_status", sa.String(length=32), nullable=False),
        sa.Column("running_status", sa.String(length=32), nullable=False),
        sa.Column("current_task", sa.String(length=255), nullable=True),
        sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("worker_token_hash", sa.String(length=128), nullable=False),
        sa.Column("worker_token_encrypted", sa.Text(), nullable=False),
        sa.Column("client_binding_state", sa.String(length=64), nullable=True),
        sa.Column("remark", sa.Text(), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_workers_enabled_status", "workers", ["enabled", "online_status", "running_status"])
    op.create_index("idx_workers_last_heartbeat_at", "workers", ["last_heartbeat_at"])

    with op.batch_alter_table("sales") as batch_op:
        batch_op.add_column(sa.Column("worker_id", sa.String(length=36), nullable=True))
        batch_op.create_foreign_key("fk_sales_worker_id_workers", "workers", ["worker_id"], ["id"])

    op.create_index("uq_sales_worker_id", "sales", ["worker_id"], unique=True)


def downgrade() -> None:
    op.drop_index("uq_sales_worker_id", table_name="sales")
    with op.batch_alter_table("sales") as batch_op:
        batch_op.drop_constraint("fk_sales_worker_id_workers", type_="foreignkey")
        batch_op.drop_column("worker_id")

    op.drop_index("idx_workers_last_heartbeat_at", table_name="workers")
    op.drop_index("idx_workers_enabled_status", table_name="workers")
    op.drop_table("workers")
