"""add durable C3 batch recovery markers

Revision ID: 20260723_0013
Revises: 20260722_0012
Create Date: 2026-07-23
"""

from alembic import op
import sqlalchemy as sa


revision = "20260723_0013"
down_revision = "20260722_0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("message_batches") as batch_op:
        batch_op.add_column(
            sa.Column(
                "generation_attempt_count",
                sa.Integer(),
                nullable=False,
                server_default="0",
            )
        )
        batch_op.add_column(
            sa.Column(
                "generation_started_at",
                sa.DateTime(timezone=True),
                nullable=True,
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("message_batches") as batch_op:
        batch_op.drop_column("generation_started_at")
        batch_op.drop_column("generation_attempt_count")
