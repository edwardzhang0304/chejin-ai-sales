"""Track Worker draining flow and recoverable C2 identity holds.

Revision ID: 20260814_0028
Revises: 20260814_0027
"""

from alembic import op
import sqlalchemy as sa


revision = "20260814_0028"
down_revision = "20260814_0027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "workers",
        sa.Column(
            "inflight_flow_state",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
    )
    op.add_column(
        "wechat_session_bindings",
        sa.Column(
            "recovery_hold",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
    )


def downgrade() -> None:
    op.drop_column("wechat_session_bindings", "recovery_hold")
    op.drop_column("workers", "inflight_flow_state")
