"""Track durable unread generations for WeChat session bindings.

Revision ID: 20260814_0027
Revises: 20260811_0026
"""

from alembic import op
import sqlalchemy as sa


revision = "20260814_0027"
down_revision = "20260811_0026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "wechat_session_bindings",
        sa.Column("last_message_preview_time", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "wechat_session_bindings",
        sa.Column("last_message_observation_id", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "wechat_session_bindings",
        sa.Column(
            "last_observed_unread_hint",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "wechat_session_bindings",
        sa.Column("unread_evidence_key", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "wechat_session_bindings",
        sa.Column(
            "unread_generation",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "wechat_session_bindings",
        sa.Column(
            "consumed_unread_generation",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.execute(
        """
        UPDATE wechat_session_bindings
           SET last_observed_unread_hint = unread_hint,
               unread_generation = CASE WHEN unread_hint THEN 1 ELSE 0 END,
               consumed_unread_generation = 0
        """
    )


def downgrade() -> None:
    op.drop_column("wechat_session_bindings", "consumed_unread_generation")
    op.drop_column("wechat_session_bindings", "unread_generation")
    op.drop_column("wechat_session_bindings", "unread_evidence_key")
    op.drop_column("wechat_session_bindings", "last_observed_unread_hint")
    op.drop_column("wechat_session_bindings", "last_message_observation_id")
    op.drop_column("wechat_session_bindings", "last_message_preview_time")
