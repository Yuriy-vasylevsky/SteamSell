"""Allow 100 percent promo codes and free orders.

Revision ID: 0018
Revises: 0017
"""

import sqlalchemy as sa

from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def _replace_check_constraint(table, column, name, expression):
    inspector = sa.inspect(op.get_bind())
    for constraint in inspector.get_check_constraints(table):
        if column in (constraint.get("sqltext") or ""):
            op.drop_constraint(constraint["name"], table, type_="check")
            break
    op.create_check_constraint(name, table, expression)


def upgrade():
    _replace_check_constraint(
        "promo_codes",
        "discount_percent",
        "ck_promo_codes_discount_percent",
        "discount_percent BETWEEN 1 AND 100",
    )
    _replace_check_constraint(
        "orders",
        "price_snapshot",
        "ck_orders_price_snapshot_nonnegative",
        "price_snapshot >= 0",
    )


def downgrade():
    op.drop_constraint("ck_orders_price_snapshot_nonnegative", "orders", type_="check")
    op.create_check_constraint("ck_orders_price_snapshot_positive", "orders", "price_snapshot > 0")
    op.drop_constraint("ck_promo_codes_discount_percent", "promo_codes", type_="check")
    op.create_check_constraint(
        "ck_promo_codes_discount_percent_90",
        "promo_codes",
        "discount_percent BETWEEN 1 AND 90",
    )
