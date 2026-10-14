"""Native history coverage retains the exact accepted range and read evidence."""
from alembic import op
import sqlalchemy as sa
revision='20261011_0039'
down_revision='20261009_0038'
branch_labels=depends_on=None

def upgrade():
    op.create_table('hook_history_receipts',
        sa.Column('id',sa.String(36),primary_key=True),
        sa.Column('worker_id',sa.String(36),sa.ForeignKey('workers.id'),nullable=False),
        sa.Column('binding_id',sa.String(36),sa.ForeignKey('wechat_session_bindings.id'),nullable=False),
        sa.Column('capture_id',sa.String(36),nullable=False),
        sa.Column('payload_digest',sa.String(64),nullable=False),
        sa.Column('payload_json',sa.JSON(),nullable=False),
        sa.Column('result_json',sa.JSON(),nullable=False),
        sa.Column('created_at',sa.DateTime(timezone=True),nullable=False),
        sa.UniqueConstraint('worker_id','capture_id',name='uq_hook_history_capture'))

def downgrade():
    if op.get_bind().execute(sa.text('SELECT 1 FROM hook_history_receipts LIMIT 1')).first():
        raise RuntimeError('Cannot discard history coverage evidence')
    op.drop_table('hook_history_receipts')
