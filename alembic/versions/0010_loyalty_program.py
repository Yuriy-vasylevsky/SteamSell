"""Add loyalty program.

Revision ID: 0010
Revises: 0009
"""

import sqlalchemy as sa

from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "orders",
        sa.Column("original_price_snapshot", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "orders",
        sa.Column("discount_percent_snapshot", sa.Integer(), nullable=False, server_default="0"),
    )
    op.execute("UPDATE orders SET original_price_snapshot = price_snapshot")
    op.create_table(
        "loyalty_levels",
        sa.Column("level_number", sa.Integer(), nullable=False),
        sa.Column("name_ua", sa.String(64), nullable=False),
        sa.Column("name_ru", sa.String(64), nullable=False),
        sa.Column("threshold_kopecks", sa.Integer(), nullable=False),
        sa.Column("discount_percent", sa.Integer(), nullable=False),
        sa.CheckConstraint("level_number BETWEEN 1 AND 5"),
        sa.CheckConstraint("threshold_kopecks >= 0"),
        sa.CheckConstraint("discount_percent BETWEEN 0 AND 50"),
        sa.PrimaryKeyConstraint("level_number"),
    )
    levels = sa.table(
        "loyalty_levels",
        sa.column("level_number", sa.Integer()),
        sa.column("name_ua", sa.String()),
        sa.column("name_ru", sa.String()),
        sa.column("threshold_kopecks", sa.Integer()),
        sa.column("discount_percent", sa.Integer()),
    )
    op.bulk_insert(
        levels,
        [
            {"level_number": 1, "name_ua": "Бронзовий", "name_ru": "Бронзовый", "threshold_kopecks": 100000, "discount_percent": 2},
            {"level_number": 2, "name_ua": "Срібний", "name_ru": "Серебряный", "threshold_kopecks": 200000, "discount_percent": 4},
            {"level_number": 3, "name_ua": "Золотий", "name_ru": "Золотой", "threshold_kopecks": 300000, "discount_percent": 6},
            {"level_number": 4, "name_ua": "Платиновий", "name_ru": "Платиновый", "threshold_kopecks": 400000, "discount_percent": 8},
            {"level_number": 5, "name_ua": "Діамантовий", "name_ru": "Бриллиантовый", "threshold_kopecks": 500000, "discount_percent": 10},
        ],
    )
    op.execute("INSERT INTO settings (key, value) VALUES ('loyalty_enabled', 'true')")


def downgrade():
    op.execute("DELETE FROM settings WHERE key = 'loyalty_enabled'")
    op.drop_table("loyalty_levels")
    op.drop_column("orders", "discount_percent_snapshot")
    op.drop_column("orders", "original_price_snapshot")
