"""Append proven historical OCR corrections without rewriting message facts."""
from alembic import op
import sqlalchemy as sa

revision = "20260918_0036"
down_revision = "20260916_0035"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "message_text_corrections",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("message_event_id", sa.String(36), sa.ForeignKey("message_events.id"), nullable=False),
        sa.Column("conversation_id", sa.String(36), nullable=False),
        sa.Column("worker_id", sa.String(36), sa.ForeignKey("workers.id"), nullable=False),
        sa.Column("effective_version", sa.Integer(), nullable=False),
        sa.Column("previous_version", sa.Integer(), nullable=False),
        sa.Column("original_text_sha256", sa.String(64), nullable=False),
        sa.Column("effective_text", sa.Text(), nullable=False),
        sa.Column("effective_text_sha256", sa.String(64), nullable=False),
        sa.Column("proof_sha256", sa.String(64), nullable=False),
        sa.Column("image_sha256", sa.String(64), nullable=False),
        sa.Column("image_bytes", sa.LargeBinary(), nullable=False),
        sa.Column("proof", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("message_event_id", "effective_version", name="uq_message_text_correction_version"),
        sa.UniqueConstraint("message_event_id", "proof_sha256", name="uq_message_text_correction_proof"),
    )
    op.create_index("ix_message_text_corrections_conversation_id", "message_text_corrections", ["conversation_id"])


def downgrade():
    if op.get_bind().execute(sa.text("SELECT 1 FROM message_text_corrections LIMIT 1")).first():
        raise RuntimeError("Cannot discard confirmed historical OCR corrections")
    op.drop_table("message_text_corrections")
