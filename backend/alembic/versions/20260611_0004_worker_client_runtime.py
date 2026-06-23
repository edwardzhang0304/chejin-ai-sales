"""add worker client runtime and task evidence tables

Revision ID: 20260611_0004
Revises: 20260608_0003
Create Date: 2026-06-11
"""

from alembic import op
import sqlalchemy as sa


revision = "20260611_0004"
down_revision = "20260608_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("workers") as batch_op:
        batch_op.add_column(sa.Column("run_status", sa.String(length=32), nullable=False, server_default="paused"))
        batch_op.add_column(sa.Column("rpa_component_status", sa.String(length=32), nullable=False, server_default="unavailable"))
        batch_op.add_column(sa.Column("wechat_status", sa.String(length=32), nullable=True))
        batch_op.add_column(sa.Column("client_instance_id", sa.String(length=128), nullable=True))
        batch_op.add_column(sa.Column("bound_at", sa.DateTime(timezone=True), nullable=True))

    op.create_index("idx_workers_client_instance_id", "workers", ["client_instance_id"])
    op.create_index("idx_workers_run_status", "workers", ["run_status", "rpa_component_status"])

    op.create_table(
        "worker_heartbeats",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("worker_id", sa.String(length=36), nullable=False),
        sa.Column("client_instance_id", sa.String(length=128), nullable=True),
        sa.Column("online_status", sa.String(length=32), nullable=False),
        sa.Column("run_status", sa.String(length=32), nullable=True),
        sa.Column("runtime_status", sa.String(length=32), nullable=True),
        sa.Column("rpa_component_status", sa.String(length=32), nullable=True),
        sa.Column("wechat_status", sa.String(length=32), nullable=True),
        sa.Column("current_task_id", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["worker_id"], ["workers.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_worker_heartbeats_worker_created_at", "worker_heartbeats", ["worker_id", sa.text("created_at DESC")])

    op.create_table(
        "task_evidences",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("task_id", sa.String(length=36), nullable=False),
        sa.Column("worker_id", sa.String(length=36), nullable=True),
        sa.Column("evidence_type", sa.String(length=32), nullable=False),
        sa.Column("file_name", sa.String(length=255), nullable=True),
        sa.Column("storage_url", sa.Text(), nullable=True),
        sa.Column("content", sa.Text(), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("remark", sa.Text(), nullable=True),
        sa.Column("extra_metadata", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"]),
        sa.ForeignKeyConstraint(["worker_id"], ["workers.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_task_evidences_task_created_at", "task_evidences", ["task_id", sa.text("created_at DESC")])
    op.create_index("idx_task_evidences_worker_created_at", "task_evidences", ["worker_id", sa.text("created_at DESC")])


def downgrade() -> None:
    op.drop_index("idx_task_evidences_worker_created_at", table_name="task_evidences")
    op.drop_index("idx_task_evidences_task_created_at", table_name="task_evidences")
    op.drop_table("task_evidences")

    op.drop_index("idx_worker_heartbeats_worker_created_at", table_name="worker_heartbeats")
    op.drop_table("worker_heartbeats")

    op.drop_index("idx_workers_run_status", table_name="workers")
    op.drop_index("idx_workers_client_instance_id", table_name="workers")
    with op.batch_alter_table("workers") as batch_op:
        batch_op.drop_column("bound_at")
        batch_op.drop_column("client_instance_id")
        batch_op.drop_column("wechat_status")
        batch_op.drop_column("rpa_component_status")
        batch_op.drop_column("run_status")
