"""Add Hook identity to the existing Worker binding; preserve OCR defaults."""
from alembic import op
import sqlalchemy as sa

revision = "20261009_0037"
down_revision = "20260918_0036"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("workers") as batch:
        batch.add_column(sa.Column("hook_self_wxid", sa.String(64), nullable=True))
        batch.add_column(sa.Column("hook_account_epoch", sa.String(36), nullable=True))
        batch.add_column(sa.Column("hook_sales_id", sa.String(36), nullable=True))
        batch.add_column(sa.Column("hook_runtime_context", sa.JSON(), nullable=False, server_default=sa.text("'{}'")))
    op.create_index("uq_workers_hook_account", "workers", ["hook_self_wxid"], unique=True,
        sqlite_where=sa.text("deleted_at IS NULL AND hook_self_wxid IS NOT NULL"),
        postgresql_where=sa.text("deleted_at IS NULL AND hook_self_wxid IS NOT NULL"))


def downgrade():
    if op.get_bind().execute(sa.text("SELECT 1 FROM workers WHERE hook_self_wxid IS NOT NULL OR hook_account_epoch IS NOT NULL OR hook_sales_id IS NOT NULL OR CAST(hook_runtime_context AS TEXT) <> '{}' LIMIT 1")).first():
        raise RuntimeError("Cannot discard Hook account identity or recovery evidence")
    op.drop_index("uq_workers_hook_account", table_name="workers")
    with op.batch_alter_table("workers") as batch:
        for field in ("hook_runtime_context", "hook_sales_id", "hook_account_epoch", "hook_self_wxid"):
            batch.drop_column(field)
