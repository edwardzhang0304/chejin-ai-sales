"""Add server-owned Feishu handoff notifications.

Revision ID: 20260811_0026
Revises: 20260809_0025
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import text


revision = "20260811_0026"
down_revision = "20260809_0025"
branch_labels = None
depends_on = None


def _normalize_and_validate_sales_phones(connection) -> None:
    dialect = connection.dialect.name
    if dialect == "postgresql":
        connection.execute(
            text(
                """
                UPDATE sales
                   SET phone = regexp_replace(phone, '[^0-9]', '', 'g')
                 WHERE phone IS NOT NULL
                """
            )
        )
        invalid_count = connection.execute(
            text(
                """
                SELECT count(*)
                  FROM sales
                 WHERE phone IS NULL OR phone !~ '^1[3-9][0-9]{9}$'
                """
            )
        ).scalar_one()
    else:
        invalid_count = connection.execute(
            text(
                """
                SELECT count(*)
                  FROM sales
                 WHERE phone IS NULL OR length(phone) != 11
                """
            )
        ).scalar_one()
    if int(invalid_count or 0) > 0:
        raise RuntimeError(
            "SALES_PHONE_BACKFILL_REQUIRED: sales contain missing or invalid phones; "
            "repair them before applying 20260811_0026"
        )


def _close_duplicate_open_handoffs(connection) -> None:
    connection.execute(
        text(
            """
            WITH ranked AS (
                SELECT id,
                       row_number() OVER (
                           PARTITION BY conversation_id
                           ORDER BY
                               CASE
                                   WHEN handoff_reason_code IN (
                                       'MESSAGE_CROSS_ROUND_IDENTITY_AMBIGUOUS',
                                       'C2_MESSAGE_HISTORY_GAP'
                                   ) THEN 1
                                   ELSE 0
                               END,
                               created_at ASC,
                               id ASC
                       ) AS position
                  FROM handoff_events
                 WHERE closed_at IS NULL
                   AND deleted_at IS NULL
            )
            UPDATE handoff_events
               SET status = 'closed_duplicate_migration',
                   closed_at = CURRENT_TIMESTAMP,
                   updated_at = CURRENT_TIMESTAMP
             WHERE id IN (SELECT id FROM ranked WHERE position > 1)
            """
        )
    )


def upgrade() -> None:
    connection = op.get_bind()
    _normalize_and_validate_sales_phones(connection)

    # Existing values have no proof that they came from the current Chejin app.
    connection.execute(text("UPDATE sales SET feishu_user_id = NULL"))
    op.alter_column(
        "sales",
        "phone",
        existing_type=sa.String(length=64),
        type_=sa.String(length=32),
        nullable=False,
    )

    op.add_column("handoff_events", sa.Column("notify_status", sa.String(length=16), nullable=True))
    op.add_column(
        "handoff_events",
        sa.Column("notify_attempted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "handoff_events",
        sa.Column("notify_completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "handoff_events",
        sa.Column("notify_error_summary", sa.String(length=512), nullable=True),
    )
    op.create_check_constraint(
        "ck_handoff_events_notify_status",
        "handoff_events",
        "notify_status IS NULL OR notify_status IN ('pending','sending','succeeded','failed')",
    )
    op.create_index(
        "idx_handoff_events_notify_status",
        "handoff_events",
        ["notify_status", "created_at"],
        unique=False,
    )

    _close_duplicate_open_handoffs(connection)
    op.create_index(
        "uq_handoff_events_open_conversation",
        "handoff_events",
        ["conversation_id"],
        unique=True,
        postgresql_where=sa.text("closed_at IS NULL AND deleted_at IS NULL"),
        sqlite_where=sa.text("closed_at IS NULL AND deleted_at IS NULL"),
    )


def downgrade() -> None:
    raise RuntimeError(
        "IRREVERSIBLE_MIGRATION_20260811_0026: HandoffEvent notification audit and "
        "server-managed Feishu identity state may contain production data; restore from "
        "a verified backup and use a forward migration instead"
    )
