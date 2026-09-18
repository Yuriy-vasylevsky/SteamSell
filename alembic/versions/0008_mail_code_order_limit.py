"""track code requests per purchase

Revision ID: 0008
Revises: 0007
"""

import sqlalchemy as sa

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("mail_code_requests", sa.Column("order_id", sa.String(32), nullable=True))
    op.create_foreign_key(
        "fk_mail_code_requests_order_id_orders",
        "mail_code_requests",
        "orders",
        ["order_id"],
        ["id"],
    )
    op.create_index("ix_mail_code_requests_order_id", "mail_code_requests", ["order_id"])


def downgrade():
    op.drop_index("ix_mail_code_requests_order_id", table_name="mail_code_requests")
    op.drop_constraint(
        "fk_mail_code_requests_order_id_orders",
        "mail_code_requests",
        type_="foreignkey",
    )
    op.drop_column("mail_code_requests", "order_id")
