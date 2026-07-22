"""add stable C2 read authorization revision

Revision ID: 20260715_0011
Revises: 20260714_0010
Create Date: 2026-07-15
"""

from alembic import op
import sqlalchemy as sa


revision = "20260715_0011"
down_revision = "20260714_0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "wechat_session_bindings",
        sa.Column("authorization_revision", sa.Integer(), nullable=False, server_default="1"),
    )


def downgrade() -> None:
    op.drop_column("wechat_session_bindings", "authorization_revision")
