"""track vehicle facts used by pending reply actions

Revision ID: 20260806_0021
Revises: 20260805_0020
Create Date: 2026-08-06
"""

from alembic import op
import sqlalchemy as sa


revision = "20260806_0021"
down_revision = "20260805_0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "reply_action_vehicle_facts",
        sa.Column("reply_action_id", sa.String(length=36), nullable=False),
        sa.Column("vehicle_id", sa.String(length=128), nullable=False),
        sa.Column("fact_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("vehicle_updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("reply_action_id", "vehicle_id"),
    )
    op.create_index(
        "idx_reply_action_vehicle_facts_vehicle_action",
        "reply_action_vehicle_facts",
        ["vehicle_id", "reply_action_id"],
    )


def downgrade() -> None:
    raise RuntimeError(
        "DOWNGRADE_CHAIN_BLOCKED_BY_DATA_SAFETY: automatic downgrade from 20260806_0021 is "
        "disabled so Alembic cannot partially drop reply-action vehicle facts before reaching "
        "the irreversible 20260805_0020 and 20260804_0019 migrations. Use a separately reviewed "
        "forward migration after a verified backup."
    )
