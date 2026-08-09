"""move unsupported disabled bindings to safe manual review

Revision ID: 20260809_0024
Revises: 20260809_0023
"""

from alembic import op
import sqlalchemy as sa


revision = "20260809_0024"
down_revision = "20260809_0023"
branch_labels = None
depends_on = None


INCONSISTENT_DISABLED_REPAIR_SQL = sa.text(
    """
    UPDATE wechat_session_bindings
       SET bind_status = CASE
               WHEN listen_status = 'paused'
                AND disable_reason IS NULL
                AND disabled_at IS NULL
                AND (disabled_by IS NULL OR disabled_by = '')
               THEN 'bound'
               ELSE 'needs_review'
           END,
           listen_status = 'paused',
           allow_listening = false,
           authorization_revision = authorization_revision + 1,
           error_code = CASE
               WHEN listen_status = 'paused'
                AND disable_reason IS NULL
                AND disabled_at IS NULL
                AND (disabled_by IS NULL OR disabled_by = '')
               THEN 'SESSION_BINDING_MIGRATED_TO_PAUSED'
               ELSE 'SESSION_BINDING_STATE_INCONSISTENT'
           END,
           updated_at = CURRENT_TIMESTAMP
     WHERE bind_status = 'disabled'
       AND deleted_at IS NULL
       AND replacement_binding_id IS NULL
       AND NOT (
           disable_reason IN (
               'customer_hard_opt_out',
               'conversation_closed',
               'remark_code_removed_confirmed',
               'admin_disabled',
               'replaced_binding'
           )
           AND disabled_at IS NOT NULL
           AND disabled_by IS NOT NULL
           AND disabled_by <> ''
       )
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


def repair_inconsistent_disabled_bindings(connection) -> int:
    result = connection.execute(INCONSISTENT_DISABLED_REPAIR_SQL)
    return max(int(result.rowcount or 0), 0)


def upgrade() -> None:
    repair_inconsistent_disabled_bindings(op.get_bind())


def downgrade() -> None:
    raise RuntimeError(
        "IRREVERSIBLE_MIGRATION_20260809_0024: disabled binding states were classified for manual review; restore from backup or use a forward migration"
    )
