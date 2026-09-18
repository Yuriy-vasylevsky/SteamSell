from app.admin import pg_dump_url


def test_pg_dump_url_uses_standard_postgres_scheme():
    assert (
        pg_dump_url("postgresql+asyncpg://user:password@db:5432/steamsell")
        == "postgresql://user:password@db:5432/steamsell"
    )
