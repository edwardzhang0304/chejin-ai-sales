"""converge fields and move c3 state to conversations

Revision ID: 20260623_0007
Revises: 20260623_0006
Create Date: 2026-06-23
"""

from alembic import op
import sqlalchemy as sa


revision = "20260623_0007"
down_revision = "20260623_0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "conversations",
        sa.Column("conversation_id", sa.String(length=36), nullable=False),
        sa.Column("lead_id", sa.String(length=36), nullable=True),
        sa.Column("sales_id", sa.String(length=36), nullable=True),
        sa.Column("worker_id", sa.String(length=36), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="ai_active"),
        sa.Column("ai_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("reply_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("handoff_reason_code", sa.String(length=64), nullable=True),
        sa.Column("handoff_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_inbound_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_outbound_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_ai_reply_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_sales_reply_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sales_first_reply_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("close_reason", sa.String(length=255), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("conversation_id"),
    )
    op.create_index("idx_conversations_lead_status", "conversations", ["lead_id", "status"])
    op.create_index("idx_conversations_sales_status", "conversations", ["sales_id", "status"])
    op.create_index("idx_conversations_worker_status", "conversations", ["worker_id", "status"])
    op.create_index("idx_conversations_last_inbound", "conversations", [sa.text("last_inbound_at DESC")])

    op.execute(
        """
        INSERT INTO conversations (
            conversation_id, lead_id, sales_id, worker_id, status, ai_enabled, reply_count,
            handoff_reason_code, handoff_at, created_at, updated_at
        )
        SELECT
            conversation_id,
            lead_id,
            sales_id,
            worker_id,
            COALESCE(conversation_status, 'ai_active'),
            COALESCE(ai_enabled, TRUE),
            COALESCE(reply_count, 0),
            handoff_reason,
            handoff_at,
            created_at,
            updated_at
        FROM wechat_session_bindings
        WHERE deleted_at IS NULL
        ON CONFLICT (conversation_id) DO NOTHING
        """
    )

    op.execute("UPDATE workers SET platform = 'windows' WHERE platform IS NULL OR platform = '' OR platform = 'mac'")
    op.execute("UPDATE workers SET running_status = 'running' WHERE running_status IN ('busy', 'executing')")

    with op.batch_alter_table("workers") as batch_op:
        batch_op.alter_column("platform", server_default="windows")

    with op.batch_alter_table("worker_heartbeats") as batch_op:
        batch_op.add_column(sa.Column("current_task", sa.String(length=255), nullable=True))
    op.execute("UPDATE worker_heartbeats SET current_task = current_task_id")
    with op.batch_alter_table("worker_heartbeats") as batch_op:
        batch_op.drop_column("current_task_id")
        batch_op.drop_column("runtime_status")

    with op.batch_alter_table("message_events") as batch_op:
        batch_op.drop_column("ingest_status")

    with op.batch_alter_table("wechat_session_bindings") as batch_op:
        batch_op.drop_column("handoff_at")
        batch_op.drop_column("handoff_reason")
        batch_op.drop_column("reply_count")
        batch_op.drop_column("conversation_status")
        batch_op.drop_column("ai_enabled")
        batch_op.drop_column("reason_code")

    with op.batch_alter_table("reply_actions") as batch_op:
        batch_op.add_column(sa.Column("handoff_reason_code", sa.String(length=64), nullable=True))
        batch_op.drop_column("handoff_reason")

    with op.batch_alter_table("handoff_events") as batch_op:
        batch_op.add_column(sa.Column("handoff_reason_code", sa.String(length=64), nullable=True))
    op.execute("UPDATE handoff_events SET handoff_reason_code = reason_code")
    with op.batch_alter_table("handoff_events") as batch_op:
        batch_op.alter_column("handoff_reason_code", nullable=False)
        batch_op.drop_column("reason_code")


def downgrade() -> None:
    with op.batch_alter_table("handoff_events") as batch_op:
        batch_op.add_column(sa.Column("reason_code", sa.String(length=64), nullable=True))
    op.execute("UPDATE handoff_events SET reason_code = handoff_reason_code")
    with op.batch_alter_table("handoff_events") as batch_op:
        batch_op.alter_column("reason_code", nullable=False)
        batch_op.drop_column("handoff_reason_code")

    with op.batch_alter_table("reply_actions") as batch_op:
        batch_op.add_column(sa.Column("handoff_reason", sa.String(length=255), nullable=True))
        batch_op.drop_column("handoff_reason_code")

    with op.batch_alter_table("wechat_session_bindings") as batch_op:
        batch_op.add_column(sa.Column("reason_code", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("ai_enabled", sa.Boolean(), nullable=False, server_default=sa.true()))
        batch_op.add_column(sa.Column("conversation_status", sa.String(length=32), nullable=False, server_default="ai_active"))
        batch_op.add_column(sa.Column("reply_count", sa.Integer(), nullable=False, server_default="0"))
        batch_op.add_column(sa.Column("handoff_reason", sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column("handoff_at", sa.DateTime(timezone=True), nullable=True))

    with op.batch_alter_table("message_events") as batch_op:
        batch_op.add_column(sa.Column("ingest_status", sa.String(length=32), nullable=False, server_default="ingested"))

    with op.batch_alter_table("worker_heartbeats") as batch_op:
        batch_op.add_column(sa.Column("runtime_status", sa.String(length=32), nullable=True))
        batch_op.add_column(sa.Column("current_task_id", sa.String(length=36), nullable=True))
    op.execute("UPDATE worker_heartbeats SET current_task_id = current_task")
    with op.batch_alter_table("worker_heartbeats") as batch_op:
        batch_op.drop_column("current_task")

    with op.batch_alter_table("workers") as batch_op:
        batch_op.alter_column("platform", server_default=None)

    op.drop_index("idx_conversations_last_inbound", table_name="conversations")
    op.drop_index("idx_conversations_worker_status", table_name="conversations")
    op.drop_index("idx_conversations_sales_status", table_name="conversations")
    op.drop_index("idx_conversations_lead_status", table_name="conversations")
    op.drop_table("conversations")
