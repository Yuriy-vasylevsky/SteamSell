"""Add promo codes.

Revision ID: 0017
Revises: 0016
"""

import sqlalchemy as sa

from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "promo_codes",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("code", sa.String(32), nullable=False),
        sa.Column("discount_percent", sa.Integer(), nullable=False),
        sa.Column("usage_limit", sa.Integer(), nullable=False),
        sa.Column("max_product_price", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("discount_percent BETWEEN 1 AND 90"),
        sa.CheckConstraint("usage_limit > 0"),
        sa.CheckConstraint("max_product_price > 0"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_promo_codes_code", "promo_codes", ["code"], unique=True)
    op.create_index("ix_promo_codes_expires_at", "promo_codes", ["expires_at"])
    op.create_index("ix_promo_codes_created_by", "promo_codes", ["created_by"])
    op.add_column("orders", sa.Column("promo_code_id", sa.Integer(), nullable=True))
    op.add_column("orders", sa.Column("promo_code_snapshot", sa.String(32), nullable=True))
    op.add_column(
        "orders",
        sa.Column("promo_discount_percent_snapshot", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_index("ix_orders_promo_code_id", "orders", ["promo_code_id"])
    op.create_foreign_key("fk_orders_promo_code_id", "orders", "promo_codes", ["promo_code_id"], ["id"])


def downgrade():
    op.drop_constraint("fk_orders_promo_code_id", "orders", type_="foreignkey")
    op.drop_index("ix_orders_promo_code_id", table_name="orders")
    op.drop_column("orders", "promo_discount_percent_snapshot")
    op.drop_column("orders", "promo_code_snapshot")
    op.drop_column("orders", "promo_code_id")
    op.drop_index("ix_promo_codes_created_by", table_name="promo_codes")
    op.drop_index("ix_promo_codes_expires_at", table_name="promo_codes")
    op.drop_index("ix_promo_codes_code", table_name="promo_codes")
    op.drop_table("promo_codes")
