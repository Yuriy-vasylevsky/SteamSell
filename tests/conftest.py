from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from cryptography.fernet import Fernet

from app.db import database
from app.models import Base, Order, Product, User
from app.security import Vault
from app.services import Shop


@pytest.fixture
async def shop(tmp_path):
    engine, sessions = database("sqlite+aiosqlite:///" + (tmp_path / "test.db").as_posix())
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    vault = Vault(Fernet.generate_key().decode())
    cfg = SimpleNamespace(
        public_base_url="https://example.test",
        code_cooldown=30,
        code_max_age=180,
        support_username="support_test",
        admin_id=99,
        page_size=7,
        timezone="Europe/Kyiv",
    )
    mono, gmail, redis = AsyncMock(), AsyncMock(), AsyncMock()
    redis.eval.return_value = 1
    service = Shop(cfg, sessions, vault, mono, gmail, redis)
    async with sessions() as session, session.begin():
        session.add_all(
            [User(id=1, language="ua", first_name="Buyer"), User(id=2, language="ru", first_name="Other")]
        )
        session.add(
            Product(
                id=1,
                name_ua="Гра",
                name_ru="Игра",
                price=49900,
                steam_login_encrypted=vault.encrypt("test_account"),
                steam_password_encrypted=vault.encrypt("password<&>"),
                gmail_credentials_encrypted=vault.pack(
                    {"refresh_token": "secret", "email": "test@gmail.com"}
                ),
            )
        )
        await session.flush()
        session.add(
            Order(
                id="a" * 32,
                user_id=1,
                product_id=1,
                price_snapshot=49900,
                product_name_snapshot="Original",
                mono_invoice_id="invoice1",
            )
        )
    yield service
    await engine.dispose()


@pytest.fixture
def payment():
    return {
        "invoiceId": "invoice1",
        "reference": "a" * 32,
        "amount": 49900,
        "ccy": 980,
        "status": "success",
        "modifiedDate": "2026-09-09T12:00:00Z",
    }
