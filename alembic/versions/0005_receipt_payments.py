"""Add DeepSeek receipt payments and managed cards."""

import sqlalchemy as sa

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("orders", sa.Column("payment_cards_encrypted", sa.Text(), nullable=True))
    op.create_table(
        "payment_cards",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("label", sa.String(64), nullable=False),
        sa.Column("number_encrypted", sa.Text(), nullable=False),
        sa.Column("last4", sa.String(4), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "payment_receipts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("order_id", sa.String(32), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("telegram_file_id", sa.Text(), nullable=False),
        sa.Column("file_sha256", sa.String(64), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("analysis", sa.JSON(), nullable=True),
        sa.Column("reason", sa.String(256), nullable=True),
        sa.Column("reviewed_by", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("file_sha256"),
    )
    op.create_index("ix_payment_receipts_order_id", "payment_receipts", ["order_id"])
    op.create_index("ix_payment_receipts_user_id", "payment_receipts", ["user_id"])
    op.create_index("ix_payment_receipts_status", "payment_receipts", ["status"])


def downgrade():
    op.drop_index("ix_payment_receipts_status", table_name="payment_receipts")
    op.drop_index("ix_payment_receipts_user_id", table_name="payment_receipts")
    op.drop_index("ix_payment_receipts_order_id", table_name="payment_receipts")
    op.drop_table("payment_receipts")
    op.drop_table("payment_cards")
    op.drop_column("orders", "payment_cards_encrypted")
