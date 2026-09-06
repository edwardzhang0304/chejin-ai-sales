"""Store optional Worker Vision credentials; existing Workers remain unconfigured."""

from alembic import op
import sqlalchemy as sa

revision = "20260906_0033"
down_revision = "20260903_0032"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("workers", sa.Column("vision_api_key_encrypted", sa.Text(), nullable=True))
    op.add_column("workers", sa.Column("vision_credential_updated_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("workers", sa.Column("vision_credential_updated_by", sa.String(36), nullable=True))


def downgrade() -> None:
    op.drop_column("workers", "vision_credential_updated_by")
    op.drop_column("workers", "vision_credential_updated_at")
    op.drop_column("workers", "vision_api_key_encrypted")
