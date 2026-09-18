"""Add product activation type and preserve it on orders.

Revision ID: 0020
Revises: 0019
"""

import sqlalchemy as sa

from alembic import op

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "products",
        sa.Column("activation_type", sa.String(16), nullable=False, server_default="standard"),
    )
    op.add_column(
        "orders",
        sa.Column("activation_type_snapshot", sa.String(16), nullable=False, server_default="standard"),
    )


def downgrade():
    op.drop_column("orders", "activation_type_snapshot")
    op.drop_column("products", "activation_type")
