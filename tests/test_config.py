from unittest.mock import patch

import pytest
from cryptography.fernet import Fernet
from pydantic import ValidationError

from app.config import Config, DatabaseConfig
from app.run import main


def values(**overrides):
    return dict(
        bot_token="test",
        admin_id=1,
        encryption_key=Fernet.generate_key().decode(),
        database_url="postgresql://user:pass@postgres.railway.internal:5432/railway",
        mono_token="test",
        google_client_id="test",
        google_client_secret="test",
        support_username="support",
        **overrides,
    )


def test_railway_domain_and_database_url():
    with patch.dict("os.environ", {}, clear=True):
        cfg = Config(_env_file=None, **values(railway_public_domain="shop-production.up.railway.app"))
    assert cfg.public_base_url == "https://shop-production.up.railway.app"
    assert cfg.database_url == "postgresql+asyncpg://user:pass@postgres.railway.internal:5432/railway"


def test_explicit_public_url_takes_precedence():
    cfg = Config(
        _env_file=None,
        **values(public_base_url="https://custom.example/", railway_public_domain="shop.up.railway.app"),
    )
    assert cfg.public_base_url == "https://custom.example"


def test_personal_card_mode_does_not_need_public_url():
    with patch.dict("os.environ", {}, clear=True):
        cfg = Config(_env_file=None, **values(public_base_url="", manual_card="4441111043425077"))
    assert cfg.public_base_url == ""


@pytest.mark.parametrize(
    "url",
    [
        "",
        "http://host.test",
        "https://",
        "https://host.test/path",
        "https://user:password@host.test",
        "https://host.test?query=1",
    ],
)
def test_invalid_public_origin(url):
    with patch.dict("os.environ", {}, clear=True), pytest.raises(ValidationError):
        Config(_env_file=None, **values(public_base_url=url))


def test_migrations_need_only_database_configuration():
    with patch.dict("os.environ", {"DATABASE_URL": "postgres://user:pass@db:5432/shop"}, clear=True):
        cfg = DatabaseConfig(_env_file=None)
    assert cfg.database_url == "postgresql+asyncpg://user:pass@db:5432/shop"


def test_railway_port_is_used():
    with patch.dict("os.environ", {"PORT": "9010"}), patch("app.run.uvicorn.run") as run:
        main()
    assert run.call_args.kwargs["port"] == 9010
    assert run.call_args.kwargs["workers"] == 1
