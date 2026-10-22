"""Persist reply responsibility and unanswered rounds without replaying old work."""
from datetime import datetime, timezone
from alembic import op
import sqlalchemy as sa
revision='20261022_0042'
down_revision='20261012_0041'
branch_labels=depends_on=None


def upgrade():
    op.add_column('workers',sa.Column('reply_history_required_after',sa.DateTime(timezone=True),nullable=True))
    op.add_column('conversations',sa.Column('reply_ownership',sa.JSON(),nullable=True))
    op.add_column('message_batches',sa.Column('ownership_revision',sa.Integer(),nullable=True))
    op.add_column('message_batches',sa.Column('silence_revision',sa.Integer(),nullable=True))
    # User excluded historical backfill. New verified post-upgrade messages
    # start new cycles; original settings, handoffs and receipts stay intact.
    table=sa.table('conversations',sa.column('reply_ownership',sa.JSON()))
    op.get_bind().execute(table.update().values(reply_ownership={
        'owner':'legacy','revision':0,'recall_streak':0,'since':datetime.now(timezone.utc).isoformat()}))
    op.create_table('human_reply_reminders',
        sa.Column('id',sa.String(36),primary_key=True),sa.Column('conversation_id',sa.String(36),nullable=False),
        sa.Column('first_message_id',sa.String(36),nullable=False),
        sa.Column('started_at',sa.DateTime(timezone=True),nullable=False),
        sa.Column('next_attempt_at',sa.DateTime(timezone=True),nullable=False),
        sa.Column('last_checked_at',sa.DateTime(timezone=True)),
        sa.Column('status',sa.String(24),nullable=False),sa.Column('attempt_count',sa.Integer(),nullable=False),
        sa.Column('attempted_at',sa.DateTime(timezone=True)),sa.Column('closed_at',sa.DateTime(timezone=True)),
        sa.Column('error_code',sa.String(100)),sa.Column('attempts',sa.JSON(),nullable=False),
        sa.Column('created_at',sa.DateTime(timezone=True),nullable=False),sa.Column('updated_at',sa.DateTime(timezone=True),nullable=False))
    op.create_index('ix_human_reply_reminders_conversation_id','human_reply_reminders',['conversation_id'])
    op.create_index('uq_human_reply_reminder_open','human_reply_reminders',['conversation_id'],unique=True,
        sqlite_where=sa.text('closed_at IS NULL'),postgresql_where=sa.text('closed_at IS NULL'))


def downgrade():
    if op.get_bind().execute(sa.text('SELECT 1 FROM human_reply_reminders LIMIT 1')).first():
        raise RuntimeError('Cannot discard original reminder delivery evidence')
    op.drop_table('human_reply_reminders')
    op.drop_column('workers','reply_history_required_after')
    op.drop_column('message_batches','ownership_revision')
    op.drop_column('message_batches','silence_revision')
    op.drop_column('conversations','reply_ownership')
