"""Immutable transcript receipts link to the original native voice parent."""
from alembic import op
import sqlalchemy as sa
revision='20261012_0041'
down_revision='20261012_0040'
branch_labels=depends_on=None


def upgrade():
    op.create_table('hook_voice_receipts',sa.Column('id',sa.String(36),primary_key=True),
        sa.Column('worker_id',sa.String(36),sa.ForeignKey('workers.id'),nullable=False),
        sa.Column('message_event_id',sa.String(36),sa.ForeignKey('message_events.id'),nullable=False),
        sa.Column('payload_digest',sa.String(64),nullable=False),sa.Column('payload_json',sa.JSON(),nullable=False),
        sa.Column('result_json',sa.JSON(),nullable=False),sa.Column('created_at',sa.DateTime(timezone=True),nullable=False),
        sa.UniqueConstraint('worker_id','message_event_id',name='uq_hook_voice_parent'))


def downgrade():
    if op.get_bind().execute(sa.text('SELECT 1 FROM hook_voice_receipts LIMIT 1')).first():
        raise RuntimeError('Cannot discard original voice transcript receipts')
    op.drop_table('hook_voice_receipts')
