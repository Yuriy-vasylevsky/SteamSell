"""Distinguish personal transfers from acquiring invoices."""
import sqlalchemy as sa

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("orders", sa.Column("payment_method", sa.String(16), nullable=False, server_default="acquiring"))


def downgrade():
    op.drop_column("orders", "payment_method")
