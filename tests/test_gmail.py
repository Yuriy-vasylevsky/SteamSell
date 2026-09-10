import base64
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.gmail import Gmail, parse_latest_steam_code, parse_steam_code
from app.models import MailCodeRequest, Order, now
from app.services import ShopError


def mail(
    text="Dear test_account,\nYour Steam Guard code is:\nABCDE\n",
    age=10,
    sender="Steam <noreply@steampowered.com>",
    subject="Your Steam account: Access from new computer",
):
    return {
        "id": "message1",
        "internalDate": str(int((now() - timedelta(seconds=age)).timestamp() * 1000)),
        "payload": {
            "mimeType": "multipart/alternative",
            "headers": [{"name": "From", "value": sender}, {"name": "Subject", "value": subject}],
            "parts": [
                {"mimeType": "text/plain", "body": {"data": base64.urlsafe_b64encode(text.encode()).decode()}}
            ],
        },
    }


def test_parse_fresh_login_email():
    assert parse_steam_code(mail(), "test_account", now() - timedelta(minutes=3)) == "ABCDE"


def test_admin_parser_returns_code_when_login_is_missing():
    message = mail(
        text="Your Steam Guard code: Z9X8C",
        subject="Your Steam account: New sign-in",
    )
    result = parse_latest_steam_code(
        message,
        [("another_account", "Інша гра")],
        now() - timedelta(minutes=3),
    )
    assert result[:3] == ("Z9X8C", "—", "Гру не вдалося визначити")


def test_parse_russian_new_computer_email():
    message = mail(
        text=(
            "yuriy_vasylevsky, Похоже,те войти с нового устройства.\n"
            "Для входа понадобится код Steam Guard:\nRGXJ5\n"
        ),
        subject="Ваш аккаунт Steam: доступ с нового компьютера",
    )
    result = parse_latest_steam_code(
        message,
        [("yuriy_vasylevsky", "Onimusha")],
        now() - timedelta(hours=1),
    )
    assert result[:3] == ("RGXJ5", "yuriy_vasylevsky", "Onimusha")


def test_parse_other_language_by_steam_guard_context():
    message = mail(
        text="Hola test_account. Tu código de Steam Guard es:\nQ7W9E\n",
        subject="Acceso a tu cuenta desde un dispositivo nuevo",
    )
    assert parse_steam_code(message, "test_account", now() - timedelta(minutes=3)) == "Q7W9E"


def test_does_not_return_unrelated_five_character_value():
    message = mail(
        text="Dear test_account, transaction reference:\nA1B2C\n",
        subject="Access from new computer",
    )
    assert parse_steam_code(message, "test_account", now() - timedelta(minutes=3)) is None


async def test_latest_admin_code_maps_login_to_game():
    gmail = Gmail(SimpleNamespace(), AsyncMock())
    gmail.token = AsyncMock(return_value={"access_token": "token"})
    message = mail()
    gmail.get = AsyncMock(
        side_effect=lambda path, token, **params: (
            {"messages": [{"id": "message1"}]} if path == "messages" else message
        )
    )
    result = await gmail.latest_code_for_accounts(
        {"refresh_token": "refresh"},
        [("test_account", "Тестова гра")],
        now() - timedelta(minutes=3),
    )
    assert result[:3] == ("ABCDE", "test_account", "Тестова гра")


@pytest.mark.parametrize(
    "changes",
    [
        {"age": 400},
        {"age": -30},
        {"sender": "noreply@steampowered.com.evil.test"},
        {"subject": "Reset your Steam password"},
        {"text": "Dear another_account,\nABCDE\n"},
        {"text": "Dear test_account,\nABCDE\nFGHIJ\n"},
    ],
)
def test_rejects_unrelated_or_old_email(changes):
    assert parse_steam_code(mail(**changes), "test_account", now() - timedelta(minutes=3)) is None


async def test_only_owner_of_paid_order_can_request_code(shop):
    for user in (1, 2):
        with pytest.raises(ShopError, match="missing"):
            await shop.code(user, "a" * 32)
    async with shop.sessions() as session, session.begin():
        order = await session.get(Order, "a" * 32)
        order.status, order.paid_at = "paid", now() - timedelta(minutes=1)
    with pytest.raises(ShopError, match="missing"):
        await shop.code(2, "a" * 32)
    shop.gmail.latest_code.assert_not_awaited()
    shop.gmail.latest_code.return_value = ("ABCDE", "mail1")
    assert await shop.code(1, "a" * 32) == "ABCDE"
    with pytest.raises(ShopError, match="no_code"):
        await shop.code(1, "a" * 32)


async def test_throttle_prevents_gmail_call(shop):
    async with shop.sessions() as session, session.begin():
        order = await session.get(Order, "a" * 32)
        order.status, order.paid_at = "paid", now()
    shop.redis.eval.return_value = 0
    with pytest.raises(ShopError, match="cooldown"):
        await shop.code(1, "a" * 32)
    shop.gmail.latest_code.assert_not_awaited()


async def test_purchase_allows_primary_code_and_two_reissues(shop):
    async with shop.sessions() as session, session.begin():
        order = await session.get(Order, "a" * 32)
        order.status, order.paid_at = "paid", now()
        for index in range(3):
            session.add(
                MailCodeRequest(
                    user_id=1,
                    product_id=order.product_id,
                    order_id=order.id,
                    outcome="found",
                    message_id=f"mail{index}",
                )
            )
    with pytest.raises(ShopError, match="code_limit"):
        await shop.code(1, "a" * 32)
    shop.gmail.latest_code.assert_not_awaited()
