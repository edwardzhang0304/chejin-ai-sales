"""add fair read-target dispatch cursor

Revision ID: 20260724_0017
Revises: 20260724_0016
Create Date: 2026-07-24
"""

from alembic import op
import sqlalchemy as sa


revision = "20260724_0017"
down_revision = "20260724_0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("wechat_session_bindings") as batch_op:
        batch_op.add_column(
            sa.Column(
                "last_read_dispatched_at",
                sa.DateTime(timezone=True),
                nullable=True,
            )
        )
        batch_op.create_index(
            "idx_wechat_bindings_read_dispatch",
            ["worker_id", "last_read_dispatched_at"],
            unique=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("wechat_session_bindings") as batch_op:
        batch_op.drop_index("idx_wechat_bindings_read_dispatch")
        batch_op.drop_column("last_read_dispatched_at")
