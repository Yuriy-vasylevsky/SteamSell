"""Add a durable buyer payment claim for manual card transfers."""

import sqlalchemy as sa

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("orders", sa.Column("payment_claimed_at", sa.DateTime(timezone=True), nullable=True))


def downgrade():
    op.drop_column("orders", "payment_claimed_at")
