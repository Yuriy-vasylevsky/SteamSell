import importlib.util
from pathlib import Path

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect

from app.models import Base


def test_initial_migration_roundtrip_matches_models():
    root = Path(__file__).parents[1] / "alembic/versions"
    migrations = []
    for name in ("0001_initial.py", "0002_payment_animation.py", "0003_manual_payment.py", "0004_personal.py", "0005_receipt_payments.py"):
        spec = importlib.util.spec_from_file_location(name, root / name)
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        migrations.append(migration)
    with create_engine("sqlite://").begin() as connection:
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            for migration in migrations:
                migration.upgrade()
            assert compare_metadata(context, Base.metadata) == []
            for migration in reversed(migrations):
                migration.downgrade()
        assert inspect(connection).get_table_names() == []
