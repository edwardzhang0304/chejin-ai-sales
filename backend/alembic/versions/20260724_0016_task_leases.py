"""add fenced service task leases

Revision ID: 20260724_0016
Revises: 20260724_0015
Create Date: 2026-07-24
"""

from alembic import op
import sqlalchemy as sa


revision = "20260724_0016"
down_revision = "20260724_0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("tasks") as batch_op:
        batch_op.add_column(sa.Column("lease_owner_worker_id", sa.String(36), nullable=True))
        batch_op.add_column(sa.Column("lease_owner_client_instance_id", sa.String(128), nullable=True))
        batch_op.add_column(sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("lease_last_renewed_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("lease_fencing_token", sa.Integer(), nullable=False, server_default="0"))
        batch_op.create_foreign_key(
            "fk_tasks_lease_owner_worker_id_workers",
            "workers",
            ["lease_owner_worker_id"],
            ["id"],
        )
        batch_op.create_index(
            "idx_tasks_lease_expiry",
            ["status", "lease_expires_at"],
            unique=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("tasks") as batch_op:
        batch_op.drop_index("idx_tasks_lease_expiry")
        batch_op.drop_constraint(
            "fk_tasks_lease_owner_worker_id_workers",
            type_="foreignkey",
        )
        batch_op.drop_column("lease_fencing_token")
        batch_op.drop_column("lease_last_renewed_at")
        batch_op.drop_column("lease_expires_at")
        batch_op.drop_column("lease_owner_client_instance_id")
        batch_op.drop_column("lease_owner_worker_id")
