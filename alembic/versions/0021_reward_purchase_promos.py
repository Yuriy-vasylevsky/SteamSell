"""Add purchase reward promo configuration and source links.

Revision ID: 0021
Revises: 0020
"""

import sqlalchemy as sa

from alembic import op

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "products",
        sa.Column("reward_promo_enabled", sa.Boolean(), nullable=False, server_default="false"),
    )
    op.add_column(
        "promo_codes",
        sa.Column("generated_for_order_id", sa.String(32), nullable=True),
    )
    op.create_index(
        "ix_promo_codes_generated_for_order_id",
        "promo_codes",
        ["generated_for_order_id"],
        unique=True,
    )


def downgrade():
    op.drop_index("ix_promo_codes_generated_for_order_id", table_name="promo_codes")
    op.drop_column("promo_codes", "generated_for_order_id")
    op.drop_column("products", "reward_promo_enabled")
