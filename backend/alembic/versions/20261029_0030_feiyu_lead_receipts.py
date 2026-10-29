"""Add durable account-scoped Feiyu clue receipts.

Revision ID: 20261029_0030
Revises: 20260815_0029
"""

from alembic import op
import sqlalchemy as sa


revision = "20261029_0030"
down_revision = "20260815_0029"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "feiyu_lead_receipts",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("account_id", sa.String(length=128), nullable=False),
        sa.Column("clue_id", sa.String(length=128), nullable=False),
        sa.Column("lead_id", sa.String(length=36), nullable=False),
        sa.Column("contact_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("payload_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("response_status", sa.Integer(), nullable=False),
        sa.Column("response_body", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["lead_id"], ["leads.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "account_id", "clue_id", name="uq_feiyu_lead_receipts_account_clue"
        ),
    )
    op.create_index(
        "idx_feiyu_lead_receipts_lead", "feiyu_lead_receipts", ["lead_id"]
    )


def downgrade() -> None:
    op.drop_index("idx_feiyu_lead_receipts_lead", table_name="feiyu_lead_receipts")
    op.drop_table("feiyu_lead_receipts")
