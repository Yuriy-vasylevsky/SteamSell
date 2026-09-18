"""Link anonymous reviews to purchases internally.

Revision ID: 0016
Revises: 0015
"""

import sqlalchemy as sa

from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("reviews", sa.Column("user_id", sa.BigInteger(), nullable=True))
    op.create_index("ix_reviews_user_id", "reviews", ["user_id"])
    op.create_foreign_key("fk_reviews_user_id", "reviews", "users", ["user_id"], ["id"])


def downgrade():
    op.drop_constraint("fk_reviews_user_id", "reviews", type_="foreignkey")
    op.drop_index("ix_reviews_user_id", table_name="reviews")
    op.drop_column("reviews", "user_id")
