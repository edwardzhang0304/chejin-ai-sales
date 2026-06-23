"""initial P0 ops admin schema

Revision ID: 20260603_0001
Revises:
Create Date: 2026-06-03
"""

from alembic import op
import sqlalchemy as sa


revision = "20260603_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "sales",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("sales_name", sa.String(length=50), nullable=False),
        sa.Column("phone", sa.String(length=64), nullable=True),
        sa.Column("wechat", sa.String(length=64), nullable=True),
        sa.Column("feishu_user_id", sa.String(length=128), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=True),
        sa.Column("remark", sa.Text(), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_sales_round_robin", "sales", ["enabled", "sort_order", "id"])

    op.create_table(
        "leads",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("customer_name", sa.String(length=50), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("source_type", sa.String(length=32), nullable=False),
        sa.Column("source_name_snapshot", sa.String(length=64), nullable=False),
        sa.Column("sales_id", sa.String(length=36), nullable=True),
        sa.Column("assigned_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("assign_status", sa.String(length=32), nullable=False),
        sa.Column("assign_failure_reason", sa.String(length=255), nullable=True),
        sa.Column("remark", sa.Text(), nullable=True),
        sa.Column("invalid_reason", sa.String(length=64), nullable=True),
        sa.Column("invalid_remark", sa.Text(), nullable=True),
        sa.Column("invalid_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("invalid_by", sa.String(length=36), nullable=True),
        sa.Column("duplicate_count", sa.Integer(), nullable=False),
        sa.Column("last_duplicate_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("custom_fields", sa.JSON(), nullable=False),
        sa.Column("created_by", sa.String(length=36), nullable=False),
        sa.Column("updated_by", sa.String(length=36), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["sales_id"], ["sales.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_leads_created_by_created_at", "leads", ["created_by", "created_at"])
    op.create_index("idx_leads_last_duplicate_at", "leads", ["last_duplicate_at"])
    op.create_index("idx_leads_sales_id_status", "leads", ["sales_id", "status"])
    op.create_index("idx_leads_status_created_at", "leads", ["status", "created_at"])

    op.create_table(
        "assignment_round_robin_state",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("current_sales_id", sa.String(length=36), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["current_sales_id"], ["sales.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", name="uq_assignment_round_robin_state_id"),
    )

    op.create_table(
        "lead_contacts",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("lead_id", sa.String(length=36), nullable=False),
        sa.Column("contact_type", sa.String(length=16), nullable=False),
        sa.Column("contact_value_encrypted", sa.Text(), nullable=False),
        sa.Column("contact_value_normalized", sa.String(length=128), nullable=False),
        sa.Column("contact_hash", sa.String(length=128), nullable=False),
        sa.Column("masked_value", sa.String(length=128), nullable=False),
        sa.Column("is_primary", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["lead_id"], ["leads.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_lead_contacts_lead_id", "lead_contacts", ["lead_id"])
    op.create_index("idx_lead_contacts_type_hash", "lead_contacts", ["contact_type", "contact_hash"])

    op.create_table(
        "lead_notes",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("lead_id", sa.String(length=36), nullable=False),
        sa.Column("note_type", sa.String(length=32), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("operator_id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["lead_id"], ["leads.id"]),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "lead_duplicate_events",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("lead_id", sa.String(length=36), nullable=False),
        sa.Column("matched_contact_hash", sa.String(length=128), nullable=False),
        sa.Column("submitted_customer_name", sa.String(length=50), nullable=False),
        sa.Column("submitted_phone_masked", sa.String(length=64), nullable=False),
        sa.Column("submitted_wechat_masked", sa.Text(), nullable=True),
        sa.Column("submitted_email_masked", sa.Text(), nullable=True),
        sa.Column("submitted_remark", sa.Text(), nullable=True),
        sa.Column("submitted_payload", sa.JSON(), nullable=False),
        sa.Column("operator_id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["lead_id"], ["leads.id"]),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "lead_assignments",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("lead_id", sa.String(length=36), nullable=False),
        sa.Column("from_sales_id", sa.String(length=36), nullable=True),
        sa.Column("to_sales_id", sa.String(length=36), nullable=True),
        sa.Column("assignment_type", sa.String(length=32), nullable=False),
        sa.Column("assignment_status", sa.String(length=32), nullable=False),
        sa.Column("failure_reason", sa.String(length=255), nullable=True),
        sa.Column("round_robin_cursor_before", sa.String(length=36), nullable=True),
        sa.Column("round_robin_cursor_after", sa.String(length=36), nullable=True),
        sa.Column("operator_id", sa.String(length=36), nullable=True),
        sa.Column("remark", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["from_sales_id"], ["sales.id"]),
        sa.ForeignKeyConstraint(["lead_id"], ["leads.id"]),
        sa.ForeignKeyConstraint(["to_sales_id"], ["sales.id"]),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "operation_logs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("module", sa.String(length=32), nullable=False),
        sa.Column("target_type", sa.String(length=32), nullable=False),
        sa.Column("target_id", sa.String(length=36), nullable=True),
        sa.Column("lead_id", sa.String(length=36), nullable=True),
        sa.Column("operator_id", sa.String(length=36), nullable=True),
        sa.Column("operator_name_snapshot", sa.String(length=64), nullable=True),
        sa.Column("ip_address", sa.String(length=64), nullable=True),
        sa.Column("user_agent", sa.Text(), nullable=True),
        sa.Column("request_id", sa.String(length=64), nullable=True),
        sa.Column("before_data", sa.JSON(), nullable=True),
        sa.Column("after_data", sa.JSON(), nullable=True),
        sa.Column("metadata", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_operation_logs_created_at", "operation_logs", ["created_at"])
    op.create_index("idx_operation_logs_event_type", "operation_logs", ["event_type"])
    op.create_index("idx_operation_logs_lead_id", "operation_logs", ["lead_id"])
    op.create_index("idx_operation_logs_operator_id", "operation_logs", ["operator_id"])

    op.create_table(
        "export_tasks",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("export_type", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("selected_count", sa.Integer(), nullable=False),
        sa.Column("file_name", sa.String(length=255), nullable=False),
        sa.Column("file_path", sa.Text(), nullable=True),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("operator_id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("export_tasks")
    op.drop_index("idx_operation_logs_operator_id", table_name="operation_logs")
    op.drop_index("idx_operation_logs_lead_id", table_name="operation_logs")
    op.drop_index("idx_operation_logs_event_type", table_name="operation_logs")
    op.drop_index("idx_operation_logs_created_at", table_name="operation_logs")
    op.drop_table("operation_logs")
    op.drop_table("lead_assignments")
    op.drop_table("lead_duplicate_events")
    op.drop_table("lead_notes")
    op.drop_index("idx_lead_contacts_type_hash", table_name="lead_contacts")
    op.drop_index("idx_lead_contacts_lead_id", table_name="lead_contacts")
    op.drop_table("lead_contacts")
    op.drop_table("assignment_round_robin_state")
    op.drop_index("idx_leads_status_created_at", table_name="leads")
    op.drop_index("idx_leads_sales_id_status", table_name="leads")
    op.drop_index("idx_leads_last_duplicate_at", table_name="leads")
    op.drop_index("idx_leads_created_by_created_at", table_name="leads")
    op.drop_table("leads")
    op.drop_index("idx_sales_round_robin", table_name="sales")
    op.drop_table("sales")
