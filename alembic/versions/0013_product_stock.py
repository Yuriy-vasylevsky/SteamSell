"""Add optional product stock quantities.

Revision ID: 0013
Revises: 0012
"""

import sqlalchemy as sa

from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("products", sa.Column("stock_quantity", sa.Integer(), nullable=True))
    op.create_check_constraint(
        "ck_products_stock_quantity",
        "products",
        "stock_quantity IS NULL OR stock_quantity >= 0",
    )


def downgrade():
    op.drop_constraint("ck_products_stock_quantity", "products", type_="check")
    op.drop_column("products", "stock_quantity")
