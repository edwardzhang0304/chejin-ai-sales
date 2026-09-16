"""Add ordered reply actions; existing single replies retain their identity."""
from alembic import op
import sqlalchemy as sa

revision = "20260916_0035"
down_revision = "20260907_0034"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("reply_actions", sa.Column("segment_index", sa.Integer(), nullable=False, server_default="1"))
    op.add_column("reply_actions", sa.Column("segment_count", sa.Integer(), nullable=False, server_default="1"))
    op.add_column("reply_actions", sa.Column("predecessor_reply_action_id", sa.String(36), nullable=True))
    op.add_column("reply_actions", sa.Column("pre_send_fact_checkpoint", sa.JSON(), nullable=True))
    op.create_check_constraint("ck_reply_actions_segment_range", "reply_actions",
                               "segment_count BETWEEN 1 AND 3 AND segment_index BETWEEN 1 AND segment_count")
    op.drop_index("uq_reply_actions_batch_generation", table_name="reply_actions")
    op.create_index("uq_reply_actions_batch_generation_segment", "reply_actions",
                    ["batch_id", "generation_no", "segment_index"], unique=True)


def downgrade():
    # Never discard issued per-segment receipts in order to downgrade a schema.
    if op.get_bind().execute(sa.text("SELECT 1 FROM reply_actions WHERE segment_count > 1 LIMIT 1")).first():
        raise RuntimeError("Cannot downgrade while reply-sequence history exists")
    op.drop_constraint("ck_reply_actions_segment_range", "reply_actions", type_="check")
    op.drop_index("uq_reply_actions_batch_generation_segment", table_name="reply_actions")
    op.create_index("uq_reply_actions_batch_generation", "reply_actions", ["batch_id", "generation_no"], unique=True)
    for name in ("pre_send_fact_checkpoint", "predecessor_reply_action_id", "segment_count", "segment_index"):
        op.drop_column("reply_actions", name)
