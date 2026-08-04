"""converge C2 image persistence and C3 batch lifecycle

Revision ID: 20260722_0012
Revises: 20260715_0011
Create Date: 2026-07-22
"""

from alembic import op
import sqlalchemy as sa


revision = "20260722_0012"
down_revision = "20260715_0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("message_events") as batch_op:
        batch_op.drop_column("image_local_path")
    with op.batch_alter_table("conversations") as batch_op:
        batch_op.add_column(sa.Column("friend_state", sa.String(length=32), nullable=False, server_default="friend_active"))
        batch_op.add_column(sa.Column("recall_count", sa.Integer(), nullable=False, server_default="0"))
        batch_op.add_column(sa.Column("recall_daily_count", sa.Integer(), nullable=False, server_default="0"))
        batch_op.add_column(sa.Column("recall_daily_date", sa.Date(), nullable=True))
        batch_op.add_column(sa.Column("recall_cycle_id", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("next_recall_at", sa.DateTime(timezone=True), nullable=True))
    with op.batch_alter_table("message_batches") as batch_op:
        batch_op.add_column(sa.Column("trigger_type", sa.String(length=32), nullable=False, server_default="customer_message"))
        batch_op.add_column(sa.Column("trigger_key", sa.String(length=128), nullable=True))
        batch_op.add_column(sa.Column("recall_cycle_id", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("retryable", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.create_index(
        "uq_message_batches_conversation_trigger",
        "message_batches",
        ["conversation_id", "trigger_type", "trigger_key"],
        unique=True,
        postgresql_where=sa.text("trigger_key IS NOT NULL AND deleted_at IS NULL"),
        sqlite_where=sa.text("trigger_key IS NOT NULL AND deleted_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_message_batches_conversation_trigger", table_name="message_batches")
    with op.batch_alter_table("message_batches") as batch_op:
        batch_op.drop_column("retryable")
        batch_op.drop_column("recall_cycle_id")
        batch_op.drop_column("trigger_key")
        batch_op.drop_column("trigger_type")
    with op.batch_alter_table("conversations") as batch_op:
        batch_op.drop_column("next_recall_at")
        batch_op.drop_column("recall_cycle_id")
        batch_op.drop_column("recall_daily_date")
        batch_op.drop_column("recall_daily_count")
        batch_op.drop_column("recall_count")
        batch_op.drop_column("friend_state")
    with op.batch_alter_table("message_events") as batch_op:
        batch_op.add_column(sa.Column("image_local_path", sa.Text(), nullable=True))
