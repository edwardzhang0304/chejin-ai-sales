"""add c2 scan idempotency and conversation message dedupe

Revision ID: 20260623_0008
Revises: 20260623_0007
Create Date: 2026-06-23
"""

from alembic import op
import sqlalchemy as sa


revision = "20260623_0008"
down_revision = "20260623_0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "wechat_scan_runs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("worker_id", sa.String(length=36), nullable=False),
        sa.Column("scan_id", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("response_snapshot", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["worker_id"], ["workers.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("scan_id", name="uq_wechat_scan_runs_scan_id"),
    )
    op.create_index("idx_wechat_scan_runs_worker_created", "wechat_scan_runs", ["worker_id", sa.text("created_at DESC")])
    op.create_unique_constraint("uq_message_events_conversation_dedupe", "message_events", ["conversation_id", "dedupe_key"])


def downgrade() -> None:
    op.drop_constraint("uq_message_events_conversation_dedupe", "message_events", type_="unique")
    op.drop_index("idx_wechat_scan_runs_worker_created", table_name="wechat_scan_runs")
    op.drop_table("wechat_scan_runs")
