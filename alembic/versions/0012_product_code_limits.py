"""Store Steam Guard code limits on products.

Revision ID: 0012
Revises: 0011
"""

import sqlalchemy as sa

from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "products",
        sa.Column("code_limit", sa.Integer(), nullable=False, server_default="3"),
    )
    op.create_check_constraint("ck_products_code_limit", "products", "code_limit BETWEEN 0 AND 5")


def downgrade():
    op.drop_constraint("ck_products_code_limit", "products", type_="check")
    op.drop_column("products", "code_limit")
