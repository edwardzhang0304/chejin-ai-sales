"""Native binding identity and durable intake receipt; OCR identities retained."""
from alembic import op
import sqlalchemy as sa

revision = '20261009_0038'
down_revision = '20261009_0037'
branch_labels = depends_on = None


def upgrade():
    with op.batch_alter_table('wechat_session_bindings') as batch:
        batch.alter_column('rpa_session_key', existing_type=sa.String(255), nullable=True)
        batch.alter_column('row_fingerprint', existing_type=sa.String(255), nullable=True)
        batch.add_column(sa.Column('native_self_wxid', sa.String(64), nullable=True))
        batch.add_column(sa.Column('native_peer_wxid', sa.String(64), nullable=True))
        batch.add_column(sa.Column('native_context', sa.JSON(), nullable=False, server_default=sa.text("'{}'")))
    clause = sa.text("deleted_at IS NULL AND bind_status='bound' AND native_self_wxid IS NOT NULL")
    op.create_index('uq_wechat_binding_native_peer', 'wechat_session_bindings', ['native_self_wxid','native_peer_wxid'], unique=True,
        sqlite_where=clause, postgresql_where=clause)
    op.create_table('hook_native_receipts',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('worker_id', sa.String(36), sa.ForeignKey('workers.id'), nullable=False),
        sa.Column('client_instance_id', sa.String(128), nullable=False),
        sa.Column('self_wxid', sa.String(64), nullable=False),
        sa.Column('account_epoch', sa.String(36), nullable=False),
        sa.Column('binding_id', sa.String(36), sa.ForeignKey('wechat_session_bindings.id'), nullable=False),
        sa.Column('binding_revision', sa.String(32), nullable=False),
        sa.Column('event_uuid', sa.String(36), nullable=False),
        sa.Column('payload_digest', sa.String(64), nullable=False),
        sa.Column('payload_json', sa.JSON(), nullable=False),
        sa.Column('receipt_json', sa.JSON(), nullable=False),
        sa.Column('dispatch_status', sa.String(32), nullable=False, server_default='pending'),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint('worker_id','event_uuid',name='uq_hook_native_receipt_event'))
    op.create_index('idx_hook_native_receipt_dispatch', 'hook_native_receipts', ['dispatch_status','created_at'])


def downgrade():
    db = op.get_bind()
    if (db.execute(sa.text('SELECT 1 FROM hook_native_receipts LIMIT 1')).first()
            or db.execute(sa.text("SELECT 1 FROM wechat_session_bindings WHERE native_self_wxid IS NOT NULL OR native_peer_wxid IS NOT NULL OR CAST(native_context AS TEXT)<>'{}' OR rpa_session_key IS NULL OR row_fingerprint IS NULL LIMIT 1")).first()):
        raise RuntimeError('Cannot discard native binding identity or delivery evidence')
    op.drop_table('hook_native_receipts')
    op.drop_index('uq_wechat_binding_native_peer', table_name='wechat_session_bindings')
    with op.batch_alter_table('wechat_session_bindings') as batch:
        for name in ('native_context','native_peer_wxid','native_self_wxid'):
            batch.drop_column(name)
        batch.alter_column('rpa_session_key', existing_type=sa.String(255), nullable=False)
        batch.alter_column('row_fingerprint', existing_type=sa.String(255), nullable=False)
