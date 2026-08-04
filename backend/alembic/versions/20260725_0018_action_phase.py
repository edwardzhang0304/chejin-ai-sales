"""add sent ack action phase

Revision ID: 20260725_0018
Revises: 20260724_0017
Create Date: 2026-07-25
"""

from alembic import op
import sqlalchemy as sa


revision = "20260725_0018"
down_revision = "20260724_0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("sent_acks") as batch_op:
        batch_op.add_column(
            sa.Column(
                "action_phase",
                sa.String(length=32),
                nullable=True,
            )
        )
    op.execute(
        """
        UPDATE sent_acks
        SET action_phase = CASE send_result
          WHEN 'sent' THEN 'confirmed'
          WHEN 'unknown' THEN 'trigger_attempted'
          ELSE 'not_attempted'
        END
        """
    )
    with op.batch_alter_table("sent_acks") as batch_op:
        batch_op.alter_column("action_phase", nullable=False)


def downgrade() -> None:
    with op.batch_alter_table("sent_acks") as batch_op:
        batch_op.drop_column("action_phase")
