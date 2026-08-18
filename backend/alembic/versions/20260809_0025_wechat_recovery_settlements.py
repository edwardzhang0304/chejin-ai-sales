"""Add backend-owned C2 recovery settlements.

Revision ID: 20260809_0025
Revises: 20260809_0024
"""

from alembic import op
import sqlalchemy as sa


revision = "20260809_0025"
down_revision = "20260809_0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "wechat_recovery_settlements",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("worker_id", sa.String(length=36), nullable=False),
        sa.Column("conversation_id", sa.String(length=36), nullable=True),
        sa.Column("recovery_transaction_id", sa.String(length=128), nullable=False),
        sa.Column("action_kind", sa.String(length=16), nullable=False),
        sa.Column("source_message_key_digest", sa.String(length=64), nullable=False),
        sa.Column("settlement_mode", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("source_results_json", sa.JSON(), nullable=False),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("trace_id", sa.String(length=128), nullable=True),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["worker_id"], ["workers.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "worker_id",
            "recovery_transaction_id",
            name="uq_wechat_recovery_settlement_worker_transaction",
        ),
    )
    op.create_index(
        "idx_wechat_recovery_settlement_conversation",
        "wechat_recovery_settlements",
        ["conversation_id", "settled_at"],
        unique=False,
    )


def downgrade() -> None:
    # Recovery settlements are production audit evidence and must not be
    # silently destroyed by an automated downgrade.
    raise RuntimeError(
        "20260809_0025 downgrade is intentionally blocked: "
        "wechat_recovery_settlements contains audit evidence"
    )
