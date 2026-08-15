"""Add side-channel C0-C4 process stage timing records.

Revision ID: 20260815_0029
Revises: 20260814_0028
"""

from alembic import op
import sqlalchemy as sa


revision = "20260815_0029"
down_revision = "20260814_0028"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "process_stage_runs",
        sa.Column("stage_run_id", sa.String(length=36), nullable=False),
        sa.Column("process_run_id", sa.String(length=36), nullable=False),
        sa.Column("parent_stage_run_id", sa.String(length=36), nullable=True),
        sa.Column("conversation_id", sa.String(length=36), nullable=True),
        sa.Column("worker_id", sa.String(length=36), nullable=True),
        sa.Column("stage_name", sa.String(length=64), nullable=False),
        sa.Column("component", sa.String(length=32), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("queued_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("queue_duration_ms", sa.Integer(), nullable=True),
        sa.Column("execution_duration_ms", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("trace_id", sa.String(length=128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["worker_id"], ["workers.id"]),
        sa.PrimaryKeyConstraint("stage_run_id"),
    )
    op.create_index(
        "idx_process_stage_runs_process_started",
        "process_stage_runs",
        ["process_run_id", "started_at"],
    )
    op.create_index(
        "idx_process_stage_runs_conversation_created",
        "process_stage_runs",
        ["conversation_id", "created_at"],
    )
    op.create_index(
        "idx_process_stage_runs_worker_created",
        "process_stage_runs",
        ["worker_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "idx_process_stage_runs_worker_created", table_name="process_stage_runs"
    )
    op.drop_index(
        "idx_process_stage_runs_conversation_created",
        table_name="process_stage_runs",
    )
    op.drop_index(
        "idx_process_stage_runs_process_started",
        table_name="process_stage_runs",
    )
    op.drop_table("process_stage_runs")
