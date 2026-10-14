"""Global provider capacity and queue accounting separate from failure retries."""
from alembic import op
import sqlalchemy as sa
revision='20261012_0040'
down_revision='20261011_0039'
branch_labels=depends_on=None


def upgrade():
    op.add_column('message_batches',sa.Column('generation_capacity_wait_count',sa.Integer(),nullable=False,server_default='0'))
    op.create_table('ai_generation_gate',sa.Column('id',sa.String(32),primary_key=True),sa.Column('revision',sa.Integer(),nullable=False))
    op.create_table('ai_generation_slots',sa.Column('token',sa.String(36),primary_key=True),
        sa.Column('conversation_id',sa.String(36),sa.ForeignKey('conversations.conversation_id'),nullable=False),
        sa.Column('batch_id',sa.String(36),sa.ForeignKey('message_batches.id'),nullable=False),
        sa.Column('generation_attempt',sa.Integer(),nullable=False),sa.Column('owner_host',sa.String(64),nullable=False),
        sa.Column('owner_pid',sa.Integer(),nullable=False),sa.Column('owner_process_started',sa.String(64),nullable=False),
        sa.Column('acquired_at',sa.DateTime(timezone=True),nullable=False),
        sa.UniqueConstraint('conversation_id',name='uq_ai_generation_conversation'))


def downgrade():
    if op.get_bind().execute(sa.text('SELECT 1 FROM ai_generation_slots LIMIT 1')).first():
        raise RuntimeError('Cannot discard live generation ownership')
    op.drop_table('ai_generation_slots');op.drop_table('ai_generation_gate')
    op.drop_column('message_batches','generation_capacity_wait_count')
