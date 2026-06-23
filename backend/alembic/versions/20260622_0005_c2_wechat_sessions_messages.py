"""add c2 wechat session bindings and message events

Revision ID: 20260622_0005
Revises: 20260611_0004
Create Date: 2026-06-22
"""

from alembic import op
import sqlalchemy as sa


revision = "20260622_0005"
down_revision = "20260611_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("workers") as batch_op:
        batch_op.add_column(sa.Column("current_step", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("local_lock_summary", sa.JSON(), nullable=False, server_default="{}"))

    with op.batch_alter_table("worker_heartbeats") as batch_op:
        batch_op.add_column(sa.Column("current_step", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("local_lock_summary", sa.JSON(), nullable=False, server_default="{}"))

    op.create_table(
        "wechat_session_bindings",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("conversation_id", sa.String(length=36), nullable=False),
        sa.Column("lead_id", sa.String(length=36), nullable=True),
        sa.Column("sales_id", sa.String(length=36), nullable=True),
        sa.Column("worker_id", sa.String(length=36), nullable=False),
        sa.Column("remark_code", sa.String(length=64), nullable=True),
        sa.Column("display_name", sa.String(length=255), nullable=False),
        sa.Column("rpa_session_key", sa.String(length=255), nullable=False),
        sa.Column("row_fingerprint", sa.String(length=255), nullable=False),
        sa.Column("bind_status", sa.String(length=32), nullable=False),
        sa.Column("listen_status", sa.String(length=32), nullable=False),
        sa.Column("allow_listening", sa.Boolean(), nullable=False),
        sa.Column("reason_code", sa.String(length=64), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("unread_hint", sa.Boolean(), nullable=False),
        sa.Column("last_message_preview", sa.Text(), nullable=True),
        sa.Column("ocr_confidence", sa.Float(), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_ingested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_scan_snapshot", sa.JSON(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["lead_id"], ["leads.id"]),
        sa.ForeignKeyConstraint(["sales_id"], ["sales.id"]),
        sa.ForeignKeyConstraint(["worker_id"], ["workers.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("conversation_id", name="uq_wechat_session_bindings_conversation_id"),
        sa.UniqueConstraint("worker_id", "rpa_session_key", name="uq_wechat_session_bindings_worker_session"),
    )
    op.create_index("idx_wechat_bindings_lead_status", "wechat_session_bindings", ["lead_id", "bind_status"])
    op.create_index("idx_wechat_bindings_worker_status", "wechat_session_bindings", ["worker_id", "bind_status", "listen_status"])
    op.create_index("idx_wechat_bindings_remark_code", "wechat_session_bindings", ["remark_code"])

    op.create_table(
        "message_events",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("conversation_id", sa.String(length=36), nullable=False),
        sa.Column("binding_id", sa.String(length=36), nullable=True),
        sa.Column("lead_id", sa.String(length=36), nullable=True),
        sa.Column("sales_id", sa.String(length=36), nullable=True),
        sa.Column("worker_id", sa.String(length=36), nullable=False),
        sa.Column("rpa_session_key", sa.String(length=255), nullable=False),
        sa.Column("read_run_id", sa.String(length=128), nullable=False),
        sa.Column("dedupe_key", sa.String(length=255), nullable=False),
        sa.Column("sender_role", sa.String(length=32), nullable=False),
        sa.Column("message_type", sa.String(length=32), nullable=False),
        sa.Column("content", sa.Text(), nullable=True),
        sa.Column("image_local_path", sa.Text(), nullable=True),
        sa.Column("raw_payload", sa.JSON(), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column("ocr_confidence", sa.Float(), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ingested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ingest_status", sa.String(length=32), nullable=False),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.ForeignKeyConstraint(["binding_id"], ["wechat_session_bindings.id"]),
        sa.ForeignKeyConstraint(["lead_id"], ["leads.id"]),
        sa.ForeignKeyConstraint(["sales_id"], ["sales.id"]),
        sa.ForeignKeyConstraint(["worker_id"], ["workers.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("worker_id", "conversation_id", "dedupe_key", name="uq_message_events_worker_conversation_dedupe"),
    )
    op.create_index("idx_message_events_conversation_ingested", "message_events", ["conversation_id", sa.text("ingested_at DESC")])
    op.create_index("idx_message_events_worker_ingested", "message_events", ["worker_id", sa.text("ingested_at DESC")])
    op.create_index("idx_message_events_lead_ingested", "message_events", ["lead_id", sa.text("ingested_at DESC")])


def downgrade() -> None:
    op.drop_index("idx_message_events_lead_ingested", table_name="message_events")
    op.drop_index("idx_message_events_worker_ingested", table_name="message_events")
    op.drop_index("idx_message_events_conversation_ingested", table_name="message_events")
    op.drop_table("message_events")

    op.drop_index("idx_wechat_bindings_remark_code", table_name="wechat_session_bindings")
    op.drop_index("idx_wechat_bindings_worker_status", table_name="wechat_session_bindings")
    op.drop_index("idx_wechat_bindings_lead_status", table_name="wechat_session_bindings")
    op.drop_table("wechat_session_bindings")

    with op.batch_alter_table("worker_heartbeats") as batch_op:
        batch_op.drop_column("local_lock_summary")
        batch_op.drop_column("current_step")

    with op.batch_alter_table("workers") as batch_op:
        batch_op.drop_column("local_lock_summary")
        batch_op.drop_column("current_step")
