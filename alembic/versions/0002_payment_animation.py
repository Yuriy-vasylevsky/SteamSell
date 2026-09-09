"""Store the Telegram message used for payment progress animation."""

import sqlalchemy as sa

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("orders", sa.Column("payment_message_id", sa.BigInteger(), nullable=True))
    op.add_column(
        "orders",
        sa.Column("payment_animation_step", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column("orders", sa.Column("payment_animation_at", sa.DateTime(timezone=True), nullable=True))


def downgrade():
    op.drop_column("orders", "payment_animation_at")
    op.drop_column("orders", "payment_animation_step")
    op.drop_column("orders", "payment_message_id")
