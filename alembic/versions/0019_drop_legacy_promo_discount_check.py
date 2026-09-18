"""Drop the legacy 90 percent promo discount constraint.

Revision ID: 0019
Revises: 0018
"""

import sqlalchemy as sa

from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade():
    constraints = {
        constraint["name"]
        for constraint in sa.inspect(op.get_bind()).get_check_constraints("promo_codes")
    }
    if "promo_codes_discount_percent_check" in constraints:
        op.drop_constraint(
            "promo_codes_discount_percent_check",
            "promo_codes",
            type_="check",
        )


def downgrade():
    constraints = {
        constraint["name"]
        for constraint in sa.inspect(op.get_bind()).get_check_constraints("promo_codes")
    }
    if "promo_codes_discount_percent_check" not in constraints:
        op.create_check_constraint(
            "promo_codes_discount_percent_check",
            "promo_codes",
            "discount_percent BETWEEN 1 AND 90",
        )
