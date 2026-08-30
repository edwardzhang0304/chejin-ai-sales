"""Bind bounded read results to their unread generation.

Revision ID: 20260830_0030
Revises: 20260815_0029
"""

from alembic import op
import sqlalchemy as sa


revision = "20260830_0030"
down_revision = "20260815_0029"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "wechat_session_bindings",
        sa.Column(
            "last_read_result_unread_generation",
            sa.Integer(),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column(
        "wechat_session_bindings",
        "last_read_result_unread_generation",
    )
