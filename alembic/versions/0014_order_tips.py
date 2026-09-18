"""Store the selected tip percentage on orders.

Revision ID: 0014
Revises: 0013
"""

import sqlalchemy as sa

from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "orders",
        sa.Column("tip_percent_snapshot", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade():
    op.drop_column("orders", "tip_percent_snapshot")
