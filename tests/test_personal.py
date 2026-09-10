from sqlalchemy import func, select

from app.models import Order, PaymentEvent, now
from app.personal import apply_statement


async def test_check_button_owner_and_unpaid_result(shop):
    from unittest.mock import AsyncMock, patch

    from aiogram import Bot
    from aiogram.fsm.storage.memory import MemoryStorage

    from app.bot import create_dispatcher
    from tests.test_bot import update

    shop.cfg.manual_card = "4441110000000000"
    order = await shop.checkout(1, 1)
    dp = create_dispatcher(shop, MemoryStorage())
    bot = Bot("123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi")
    bot.session = AsyncMock()
    with patch("app.personal.reconcile_personal", new_callable=AsyncMock) as check:
        check.return_value = "checked"
        await dp.feed_update(bot, update(user_id=2, callback="check_payment:" + order.id))
        check.assert_not_awaited()
        await dp.feed_update(bot, update(user_id=1, callback="check_payment:" + order.id))
        check.assert_awaited_once()
        assert "поки не знайдено" in bot.session.call_args.args[1].text
        check.assert_awaited_once_with(shop, order.id)
    async with shop.sessions() as session:
        assert (await session.get(Order, order.id)).status == "waiting_payment"


async def test_personal_requires_amount_time_and_accepts_positive_hold(shop):
    shop.cfg.manual_card = "4441110000000000"
    order = await shop.checkout(1, 1)
    shop.mono.create.assert_not_awaited()
    assert (await shop.checkout(1, 1)).id == order.id
    entry = dict(
        id="transfer1", amount=order.price_snapshot, currencyCode=980, hold=True, time=int(now().timestamp())
    )
    for changes in ({"amount": -49900}, {"amount": 1}, {"currencyCode": 840}, {"time": 1}):
        await apply_statement(shop, "account", [entry | changes])
        async with shop.sessions() as session:
            assert (await session.get(Order, order.id)).status == "waiting_payment"
    await apply_statement(shop, "account", [entry])
    await apply_statement(shop, "account", [entry])
    async with shop.sessions() as session:
        assert (await session.get(Order, order.id)).status == "paid"
        assert await session.scalar(select(func.count()).select_from(PaymentEvent)) == 1


async def test_acquiring_webhook_cannot_confirm_personal_order(shop, payment):
    shop.cfg.manual_card = "4441110000000000"
    order = await shop.checkout(1, 1)
    await shop.payment(payment | {"reference": order.id, "invoiceId": "foreign"}, "digest")
    async with shop.sessions() as session:
        assert (await session.get(Order, order.id)).status == "waiting_payment"


async def test_same_amount_assigned_only_to_clicked_order(shop):
    shop.cfg.manual_card = "4441110000000000"
    first = await shop.checkout(1, 1)
    second = await shop.checkout(2, 1)
    await apply_statement(
        shop,
        "account",
        [
            dict(
                id="ambiguous",
                amount=first.price_snapshot,
                currencyCode=980,
                hold=False,
                time=int(now().timestamp()),
            )
        ],
        second.id,
    )
    async with shop.sessions() as session:
        assert (await session.get(Order, first.id)).status == "waiting_payment"
        assert (await session.get(Order, second.id)).status == "paid"
        assert await session.scalar(select(func.count()).select_from(PaymentEvent)) == 1


async def test_used_transfer_cannot_pay_another_order(shop):
    shop.cfg.manual_card = "4441110000000000"
    first = await shop.checkout(1, 1)
    entry = dict(
        id="used", amount=first.price_snapshot, currencyCode=980, hold=False, time=int(now().timestamp())
    )
    await apply_statement(shop, "account", [entry])
    second = await shop.checkout(2, 1)
    async with shop.sessions() as session, session.begin():
        (await session.get(Order, second.id)).created_at = first.created_at
    await apply_statement(shop, "account", [entry])
    async with shop.sessions() as session:
        assert (await session.get(Order, second.id)).status == "waiting_payment"
        assert await session.scalar(select(func.count()).select_from(PaymentEvent)) == 1
