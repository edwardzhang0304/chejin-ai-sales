"""Preserve listening configuration across lead invalidation and restoration."""
from alembic import op
import sqlalchemy as sa

revision = "20260907_0034"
down_revision = "20260906_0033"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("wechat_session_bindings", sa.Column("followup_invalidated_revision", sa.Integer(), nullable=True))
    op.add_column("wechat_session_bindings", sa.Column("followup_restore_pending", sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade():
    op.drop_column("wechat_session_bindings", "followup_restore_pending")
    op.drop_column("wechat_session_bindings", "followup_invalidated_revision")
