"""add c3 message batches and reply actions

Revision ID: 20260623_0006
Revises: 20260622_0005
Create Date: 2026-06-23
"""

from alembic import op
import sqlalchemy as sa


revision = "20260623_0006"
down_revision = "20260622_0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("wechat_session_bindings") as batch_op:
        batch_op.add_column(sa.Column("ai_enabled", sa.Boolean(), nullable=False, server_default=sa.true()))
        batch_op.add_column(sa.Column("conversation_status", sa.String(length=32), nullable=False, server_default="ai_active"))
        batch_op.add_column(sa.Column("reply_count", sa.Integer(), nullable=False, server_default="0"))
        batch_op.add_column(sa.Column("handoff_reason", sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column("handoff_at", sa.DateTime(timezone=True), nullable=True))

    with op.batch_alter_table("tasks") as batch_op:
        batch_op.add_column(sa.Column("reply_action_id", sa.String(length=36), nullable=True))

    op.create_index(
        "uq_tasks_reply_action_id",
        "tasks",
        ["reply_action_id"],
        unique=True,
        sqlite_where=sa.text("reply_action_id IS NOT NULL"),
        postgresql_where=sa.text("reply_action_id IS NOT NULL"),
    )

    op.create_table(
        "message_batches",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("conversation_id", sa.String(length=36), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("trigger_message_event_id", sa.String(length=36), nullable=True),
        sa.Column("message_event_ids", sa.JSON(), nullable=False),
        sa.Column("message_count", sa.Integer(), nullable=False),
        sa.Column("generation_no", sa.Integer(), nullable=False),
        sa.Column("trace_id", sa.String(length=64), nullable=True),
        sa.Column("decision", sa.String(length=32), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("suggested_action", sa.String(length=64), nullable=True),
        sa.Column("superseded_by_batch_id", sa.String(length=36), nullable=True),
        sa.Column("ai_request_snapshot", sa.JSON(), nullable=False),
        sa.Column("ai_response_snapshot", sa.JSON(), nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_message_batches_conversation_created", "message_batches", ["conversation_id", sa.text("created_at DESC")])
    op.create_index("idx_message_batches_status_created", "message_batches", ["status", sa.text("created_at DESC")])
    op.create_index(
        "uq_message_batches_active_conversation",
        "message_batches",
        ["conversation_id"],
        unique=True,
        sqlite_where=sa.text("active = 1"),
        postgresql_where=sa.text("active IS TRUE"),
    )

    op.create_table(
        "reply_actions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("batch_id", sa.String(length=36), nullable=False),
        sa.Column("conversation_id", sa.String(length=36), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("current", sa.Boolean(), nullable=False),
        sa.Column("generation_no", sa.Integer(), nullable=False),
        sa.Column("decision", sa.String(length=32), nullable=False),
        sa.Column("reply_text", sa.Text(), nullable=True),
        sa.Column("reply_text_hash", sa.String(length=64), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("risk_flags", sa.JSON(), nullable=False),
        sa.Column("evidence_refs", sa.JSON(), nullable=False),
        sa.Column("guard_result", sa.String(length=32), nullable=True),
        sa.Column("handoff_reason", sa.String(length=255), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("suggested_action", sa.String(length=64), nullable=True),
        sa.Column("expire_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("send_token", sa.String(length=128), nullable=True),
        sa.Column("claimed_by_worker_id", sa.String(length=36), nullable=True),
        sa.Column("claimed_task_id", sa.String(length=36), nullable=True),
        sa.Column("sending_claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ai_payload", sa.JSON(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_reply_actions_batch_status", "reply_actions", ["batch_id", "status"])
    op.create_index("idx_reply_actions_conversation_created", "reply_actions", ["conversation_id", sa.text("created_at DESC")])
    op.create_index("idx_reply_actions_status_expire", "reply_actions", ["status", "expire_at"])
    op.create_index("uq_reply_actions_batch_generation", "reply_actions", ["batch_id", "generation_no"], unique=True)
    op.create_index(
        "uq_reply_actions_current_batch",
        "reply_actions",
        ["batch_id"],
        unique=True,
        sqlite_where=sa.text("current = 1"),
        postgresql_where=sa.text("current IS TRUE"),
    )

    op.create_table(
        "sent_acks",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("reply_action_id", sa.String(length=36), nullable=False),
        sa.Column("task_id", sa.String(length=36), nullable=False),
        sa.Column("worker_id", sa.String(length=36), nullable=False),
        sa.Column("client_instance_id", sa.String(length=128), nullable=True),
        sa.Column("send_token", sa.String(length=128), nullable=False),
        sa.Column("send_result", sa.String(length=32), nullable=False),
        sa.Column("reply_text_hash", sa.String(length=64), nullable=True),
        sa.Column("sidecar_run_id", sa.String(length=128), nullable=True),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("remark", sa.Text(), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("uq_sent_acks_reply_action", "sent_acks", ["reply_action_id"], unique=True)
    op.create_index("idx_sent_acks_task_created", "sent_acks", ["task_id", sa.text("created_at DESC")])

    op.create_table(
        "handoff_events",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("conversation_id", sa.String(length=36), nullable=False),
        sa.Column("batch_id", sa.String(length=36), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("reason_code", sa.String(length=64), nullable=False),
        sa.Column("reason_detail", sa.Text(), nullable=True),
        sa.Column("trigger_message_event_ids", sa.JSON(), nullable=False),
        sa.Column("risk_flags", sa.JSON(), nullable=False),
        sa.Column("evidence_refs", sa.JSON(), nullable=False),
        sa.Column("ai_payload", sa.JSON(), nullable=False),
        sa.Column("notify_error_code", sa.String(length=64), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_handoff_events_conversation_created", "handoff_events", ["conversation_id", sa.text("created_at DESC")])
    op.create_index("idx_handoff_events_status_created", "handoff_events", ["status", sa.text("created_at DESC")])


def downgrade() -> None:
    op.drop_index("idx_handoff_events_status_created", table_name="handoff_events")
    op.drop_index("idx_handoff_events_conversation_created", table_name="handoff_events")
    op.drop_table("handoff_events")

    op.drop_index("idx_sent_acks_task_created", table_name="sent_acks")
    op.drop_index("uq_sent_acks_reply_action", table_name="sent_acks")
    op.drop_table("sent_acks")

    op.drop_index("uq_reply_actions_current_batch", table_name="reply_actions")
    op.drop_index("uq_reply_actions_batch_generation", table_name="reply_actions")
    op.drop_index("idx_reply_actions_status_expire", table_name="reply_actions")
    op.drop_index("idx_reply_actions_conversation_created", table_name="reply_actions")
    op.drop_index("idx_reply_actions_batch_status", table_name="reply_actions")
    op.drop_table("reply_actions")

    op.drop_index("uq_message_batches_active_conversation", table_name="message_batches")
    op.drop_index("idx_message_batches_status_created", table_name="message_batches")
    op.drop_index("idx_message_batches_conversation_created", table_name="message_batches")
    op.drop_table("message_batches")

    op.drop_index("uq_tasks_reply_action_id", table_name="tasks")
    with op.batch_alter_table("tasks") as batch_op:
        batch_op.drop_column("reply_action_id")

    with op.batch_alter_table("wechat_session_bindings") as batch_op:
        batch_op.drop_column("handoff_at")
        batch_op.drop_column("handoff_reason")
        batch_op.drop_column("reply_count")
        batch_op.drop_column("conversation_status")
        batch_op.drop_column("ai_enabled")
