"""Make Feiyu payload fingerprints compatible with legacy receipts.

Revision ID: 20261029_0031
Revises: 20261029_0030
"""

from alembic import op
import sqlalchemy as sa


revision = "20261029_0031"
down_revision = "20261029_0030"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"]: column for column in inspector.get_columns("feiyu_lead_receipts")}

    if "payload_fingerprint" not in columns:
        op.add_column(
            "feiyu_lead_receipts",
            sa.Column("payload_fingerprint", sa.String(length=64), nullable=True),
        )
    elif not columns["payload_fingerprint"]["nullable"]:
        if bind.dialect.name == "sqlite":
            with op.batch_alter_table("feiyu_lead_receipts") as batch_op:
                batch_op.alter_column(
                    "payload_fingerprint",
                    existing_type=sa.String(length=64),
                    nullable=True,
                )
        else:
            op.alter_column(
                "feiyu_lead_receipts",
                "payload_fingerprint",
                existing_type=sa.String(length=64),
                nullable=True,
            )


def downgrade() -> None:
    # Keep receipt rows and computed fingerprints intact. Revision 0030 was
    # applied with two different schemas, so dropping or tightening this column
    # cannot be reversed safely for every database.
    pass
