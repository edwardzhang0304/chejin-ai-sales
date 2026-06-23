"""add unified task center tables

Revision ID: 20260608_0003
Revises: 20260605_0002
Create Date: 2026-06-08
"""

from alembic import op
import sqlalchemy as sa


revision = "20260608_0003"
down_revision = "20260605_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tasks",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("task_type", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("result_code", sa.String(length=64), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("block_code", sa.String(length=64), nullable=True),
        sa.Column("lead_id", sa.String(length=36), nullable=True),
        sa.Column("sales_id", sa.String(length=36), nullable=True),
        sa.Column("worker_id", sa.String(length=36), nullable=True),
        sa.Column("original_task_id", sa.String(length=36), nullable=True),
        sa.Column("current_step", sa.String(length=64), nullable=True),
        sa.Column("failure_step", sa.String(length=64), nullable=True),
        sa.Column("failure_remark", sa.Text(), nullable=True),
        sa.Column("cancel_reason", sa.Text(), nullable=True),
        sa.Column("remark", sa.Text(), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.String(length=36), nullable=True),
        sa.Column("updated_by", sa.String(length=36), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["lead_id"], ["leads.id"]),
        sa.ForeignKeyConstraint(["sales_id"], ["sales.id"]),
        sa.ForeignKeyConstraint(["worker_id"], ["workers.id"]),
        sa.ForeignKeyConstraint(["original_task_id"], ["tasks.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_tasks_type_status_created_at", "tasks", ["task_type", "status", sa.text("created_at DESC")])
    op.create_index("idx_tasks_status_created_at", "tasks", ["status", sa.text("created_at DESC")])
    op.create_index("idx_tasks_sales_status", "tasks", ["sales_id", "status"])
    op.create_index("idx_tasks_worker_status", "tasks", ["worker_id", "status"])
    op.create_index("idx_tasks_lead_type_status", "tasks", ["lead_id", "task_type", "status"])
    op.create_index("idx_tasks_result_code", "tasks", ["result_code"])
    op.create_index("idx_tasks_error_code", "tasks", ["error_code"])
    op.create_index("idx_tasks_block_code", "tasks", ["block_code"])
    op.create_index("idx_tasks_original_task_id", "tasks", ["original_task_id"])

    op.create_table(
        "task_events",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("task_id", sa.String(length=36), nullable=False),
        sa.Column("event_type", sa.String(length=32), nullable=False),
        sa.Column("from_status", sa.String(length=32), nullable=True),
        sa.Column("to_status", sa.String(length=32), nullable=True),
        sa.Column("result_code", sa.String(length=64), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("block_code", sa.String(length=64), nullable=True),
        sa.Column("current_step", sa.String(length=64), nullable=True),
        sa.Column("operator_id", sa.String(length=36), nullable=True),
        sa.Column("operator_name", sa.String(length=64), nullable=True),
        sa.Column("worker_id", sa.String(length=36), nullable=True),
        sa.Column("remark", sa.Text(), nullable=True),
        sa.Column("extra_metadata", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"]),
        sa.ForeignKeyConstraint(["worker_id"], ["workers.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_task_events_task_created_at", "task_events", ["task_id", sa.text("created_at DESC")])
    op.create_index("idx_task_events_type_created_at", "task_events", ["event_type", sa.text("created_at DESC")])

    op.create_table(
        "task_notes",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("task_id", sa.String(length=36), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("operator_id", sa.String(length=36), nullable=False),
        sa.Column("operator_name", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_task_notes_task_created_at", "task_notes", ["task_id", sa.text("created_at DESC")])


def downgrade() -> None:
    op.drop_index("idx_task_notes_task_created_at", table_name="task_notes")
    op.drop_table("task_notes")

    op.drop_index("idx_task_events_type_created_at", table_name="task_events")
    op.drop_index("idx_task_events_task_created_at", table_name="task_events")
    op.drop_table("task_events")

    op.drop_index("idx_tasks_original_task_id", table_name="tasks")
    op.drop_index("idx_tasks_block_code", table_name="tasks")
    op.drop_index("idx_tasks_error_code", table_name="tasks")
    op.drop_index("idx_tasks_result_code", table_name="tasks")
    op.drop_index("idx_tasks_lead_type_status", table_name="tasks")
    op.drop_index("idx_tasks_worker_status", table_name="tasks")
    op.drop_index("idx_tasks_sales_status", table_name="tasks")
    op.drop_index("idx_tasks_status_created_at", table_name="tasks")
    op.drop_index("idx_tasks_type_status_created_at", table_name="tasks")
    op.drop_table("tasks")
