"""add C3 continuation scope and stable message observation order

Revision ID: 20260724_0014
Revises: 20260723_0013
Create Date: 2026-07-24
"""

from alembic import op
import sqlalchemy as sa


revision = "20260724_0014"
down_revision = "20260723_0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("message_batches") as batch_op:
        batch_op.add_column(
            sa.Column("continuation_authorization_revision", sa.String(128), nullable=True)
        )
        batch_op.add_column(
            sa.Column("continuation_read_reason", sa.String(64), nullable=True)
        )
        batch_op.add_column(
            sa.Column("origin_conversation_status", sa.String(32), nullable=True)
        )
    with op.batch_alter_table("message_events") as batch_op:
        batch_op.add_column(
            sa.Column("observed_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.add_column(
            sa.Column("observation_order", sa.Integer(), nullable=True)
        )
        batch_op.create_index(
            "idx_message_events_conversation_observed",
            ["conversation_id", "observed_at", "observation_order"],
            unique=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("message_events") as batch_op:
        batch_op.drop_index("idx_message_events_conversation_observed")
        batch_op.drop_column("observation_order")
        batch_op.drop_column("observed_at")
    with op.batch_alter_table("message_batches") as batch_op:
        batch_op.drop_column("origin_conversation_status")
        batch_op.drop_column("continuation_read_reason")
        batch_op.drop_column("continuation_authorization_revision")
