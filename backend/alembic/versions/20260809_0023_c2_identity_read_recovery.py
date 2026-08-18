"""add C2 identity, read backoff, and binding recovery state

Revision ID: 20260809_0023
Revises: 20260807_0022
"""

from alembic import op
import sqlalchemy as sa


revision = "20260809_0023"
down_revision = "20260807_0022"
branch_labels = None
depends_on = None


LEGACY_BINDING_REPAIR_SQL = sa.text(
    """
    UPDATE wechat_session_bindings
       SET bind_status = 'bound',
           allow_listening = false,
           authorization_revision = authorization_revision + 1,
           error_code = 'SESSION_BINDING_MIGRATED_TO_PAUSED',
           updated_at = CURRENT_TIMESTAMP
     WHERE bind_status = 'disabled'
       AND listen_status = 'paused'
       AND deleted_at IS NULL
       AND disable_reason IS NULL
       AND replacement_binding_id IS NULL
       AND EXISTS (
           SELECT 1
             FROM conversations
            WHERE conversations.conversation_id = wechat_session_bindings.conversation_id
              AND conversations.deleted_at IS NULL
              AND conversations.ai_enabled = true
              AND conversations.status NOT IN ('closed', 'rejected')
              AND (conversations.close_reason IS NULL OR conversations.close_reason = '')
       )
    """
)


def repair_legacy_bindings(connection) -> int:
    result = connection.execute(LEGACY_BINDING_REPAIR_SQL)
    return max(int(result.rowcount or 0), 0)


def upgrade() -> None:
    op.add_column(
        "wechat_session_bindings",
        sa.Column("disable_reason", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "wechat_session_bindings",
        sa.Column("disabled_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "wechat_session_bindings",
        sa.Column("disabled_by", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "wechat_session_bindings",
        sa.Column("replacement_binding_id", sa.String(length=36), nullable=True),
    )
    op.create_foreign_key(
        "fk_wechat_bindings_replacement_binding_id",
        "wechat_session_bindings",
        "wechat_session_bindings",
        ["replacement_binding_id"],
        ["id"],
    )
    op.add_column(
        "wechat_session_bindings",
        sa.Column("last_read_completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "wechat_session_bindings",
        sa.Column("last_read_result", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "wechat_session_bindings",
        sa.Column("last_read_run_id", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "wechat_session_bindings",
        sa.Column(
            "no_change_read_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "wechat_session_bindings",
        sa.Column("next_read_due_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "wechat_session_bindings",
        sa.Column(
            "last_read_conversation_status",
            sa.String(length=32),
            nullable=True,
        ),
    )
    op.create_index(
        "idx_wechat_bindings_worker_read_due",
        "wechat_session_bindings",
        ["worker_id", "bind_status", "listen_status", "next_read_due_at"],
        unique=False,
    )
    repair_legacy_bindings(op.get_bind())


def downgrade() -> None:
    raise RuntimeError(
        "IRREVERSIBLE_MIGRATION_20260809_0023: binding state data may have been repaired; restore from backup or use a forward migration"
    )
