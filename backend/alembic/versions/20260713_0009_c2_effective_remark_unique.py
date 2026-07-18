"""enforce one effective C2 binding per remark code

Revision ID: 20260713_0009
Revises: 20260623_0008
Create Date: 2026-07-13
"""

from alembic import op
import sqlalchemy as sa


revision = "20260713_0009"
down_revision = "20260623_0008"
branch_labels = None
depends_on = None


EFFECTIVE_PREDICATE = (
    "deleted_at IS NULL AND remark_code IS NOT NULL AND remark_code <> '' "
    "AND bind_status = 'bound'"
)


def upgrade() -> None:
    # Keep the most trustworthy historical row before adding the constraint.
    # Rows with message history win, followed by bound/listening and recency.
    op.execute(
        """
        WITH ranked AS (
            SELECT
                id,
                ROW_NUMBER() OVER (
                    PARTITION BY remark_code
                    ORDER BY
                        CASE WHEN last_ingested_at IS NOT NULL THEN 1 ELSE 0 END DESC,
                        CASE WHEN bind_status = 'bound' THEN 1 ELSE 0 END DESC,
                        CASE WHEN allow_listening THEN 1 ELSE 0 END DESC,
                        last_seen_at DESC,
                        created_at DESC,
                        id DESC
                ) AS row_no
            FROM wechat_session_bindings
            WHERE deleted_at IS NULL
              AND remark_code IS NOT NULL
              AND remark_code <> ''
              AND bind_status = 'bound'
        )
        UPDATE wechat_session_bindings
        SET bind_status = 'disabled',
            listen_status = 'not_started',
            allow_listening = FALSE,
            error_code = 'SESSION_BINDING_DUPLICATE_RETIRED_BY_MIGRATION',
            updated_at = CURRENT_TIMESTAMP
        WHERE id IN (SELECT id FROM ranked WHERE row_no > 1)
        """
    )
    op.create_index(
        "uq_wechat_bindings_effective_remark_code",
        "wechat_session_bindings",
        ["remark_code"],
        unique=True,
        postgresql_where=sa.text(EFFECTIVE_PREDICATE),
        sqlite_where=sa.text(EFFECTIVE_PREDICATE),
    )


def downgrade() -> None:
    op.drop_index("uq_wechat_bindings_effective_remark_code", table_name="wechat_session_bindings")
