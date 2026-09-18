"""Add manual product delivery mode.

Revision ID: 0015
Revises: 0014
"""

import sqlalchemy as sa

from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("products", sa.Column("delivery_mode", sa.String(16), nullable=False, server_default="auto"))
    op.alter_column("products", "steam_login_encrypted", nullable=True)
    op.alter_column("products", "steam_password_encrypted", nullable=True)
    op.add_column("orders", sa.Column("delivery_mode_snapshot", sa.String(16), nullable=False, server_default="auto"))


def downgrade():
    op.drop_column("orders", "delivery_mode_snapshot")
    op.alter_column("products", "steam_password_encrypted", nullable=False)
    op.alter_column("products", "steam_login_encrypted", nullable=False)
    op.drop_column("products", "delivery_mode")
