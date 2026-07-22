"""add C2 canonical message contract v2

Revision ID: 20260714_0010
Revises: 20260713_0009
Create Date: 2026-07-14
"""

from alembic import op
import sqlalchemy as sa


revision = "20260714_0010"
down_revision = "20260713_0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("message_events", sa.Column("contract_version", sa.Integer(), nullable=False, server_default="1"))
    op.add_column("message_events", sa.Column("source_message_key", sa.String(length=255), nullable=True))
    op.add_column("message_events", sa.Column("item_state", sa.String(length=32), nullable=True))
    op.add_column("message_events", sa.Column("flow_state", sa.String(length=32), nullable=True))
    op.create_unique_constraint(
        "uq_message_events_read_source",
        "message_events",
        ["conversation_id", "read_run_id", "source_message_key"],
    )


def downgrade() -> None:
    op.drop_constraint("uq_message_events_read_source", "message_events", type_="unique")
    op.drop_column("message_events", "flow_state")
    op.drop_column("message_events", "item_state")
    op.drop_column("message_events", "source_message_key")
    op.drop_column("message_events", "contract_version")
