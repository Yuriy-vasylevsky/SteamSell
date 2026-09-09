import base64
from datetime import timedelta

import pytest

from app.gmail import parse_steam_code
from app.models import Order, now
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
