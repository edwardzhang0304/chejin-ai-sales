"""Manual history review preserves original evidence and reply ownership."""
from alembic import op
import sqlalchemy as sa
revision = '20261028_0043'
down_revision = '20261022_0042'
branch_labels = depends_on = None


def upgrade():
    op.create_table('history_reviews',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('lead_id', sa.String(36), sa.ForeignKey('leads.id'), nullable=False),
        sa.Column('binding_id', sa.String(36), sa.ForeignKey('wechat_session_bindings.id'), nullable=False),
        sa.Column('boundary_key', sa.String(64), nullable=False),
        sa.Column('reason', sa.String(64), nullable=False),
        sa.Column('status', sa.String(24), nullable=False),
        sa.Column('revision', sa.Integer(), nullable=False),
        sa.Column('evidence', sa.JSON(), nullable=False),
        sa.Column('message_ids', sa.JSON(), nullable=False),
        sa.Column('resolutions', sa.JSON(), nullable=False),
        sa.Column('note', sa.Text()), sa.Column('resolved_by', sa.String(36)),
        sa.Column('resolved_at', sa.DateTime(timezone=True)),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint('binding_id', 'boundary_key', name='uq_history_review_boundary'))
    op.create_index('ix_history_reviews_lead_id', 'history_reviews', ['lead_id'])


def downgrade():
    if op.get_bind().execute(sa.text('SELECT 1 FROM history_reviews LIMIT 1')).first():
        raise RuntimeError('Cannot discard original history review evidence')
    op.drop_table('history_reviews')
