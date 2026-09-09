from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from app.models import Order, PaymentEvent, Product
from app.services import ShopError, set_setting
from app.worker import animate_waiting_payments, deliver_one


async def test_repeated_webhook_delivers_once(shop, payment):
    await shop.payment(payment, "digest1")
    await shop.payment(payment, "digest1")
    await shop.payment(payment, "digest2")
    bot = AsyncMock()
    bot.send_message.return_value.message_id = 123
    assert await deliver_one(shop, bot)
    assert not await deliver_one(shop, bot)
    assert bot.send_message.await_count == 1
    assert "password&lt;&amp;&gt;" in bot.send_message.call_args.args[1]
    async with shop.sessions() as session:
        order = await session.get(Order, "a" * 32)
        assert order.status == "delivered"
        assert order.delivered_at and order.delivery_message_id == 123
        assert await session.scalar(select(func.count()).select_from(PaymentEvent)) == 2


@pytest.mark.parametrize(
    "field,value", [("amount", 1), ("amount", "49900"), ("ccy", 840), ("reference", "wrong")]
)
async def test_mismatched_payment_never_grants_access(shop, payment, field, value):
    payment[field] = value
    await shop.payment(payment, "mismatch")
    async with shop.sessions() as session:
        assert (await session.get(Order, "a" * 32)).status == "waiting_payment"
        assert (await session.scalar(select(PaymentEvent))).outcome == "mismatch"


async def test_out_of_order_events_do_not_undo_payment(shop, payment):
    await shop.payment(payment, "success")
    payment.update(status="failure", modifiedDate="2026-09-09T11:59:00Z")
    await shop.payment(payment, "old")
    payment["modifiedDate"] = "2026-09-09T12:01:00Z"
    await shop.payment(payment, "later_failure")
    async with shop.sessions() as session:
        assert (await session.get(Order, "a" * 32)).status == "paid"


async def test_unknown_invoice(shop, payment):
    payment.update(invoiceId="unknown", reference="unknown")
    with pytest.raises(LookupError):
        await shop.payment(payment, "unknown")


async def test_callback_recovers_invoice_creation_timeout(shop, payment):
    async with shop.sessions() as session, session.begin():
        order = await session.get(Order, "a" * 32)
        order.mono_invoice_id, order.status = None, "payment_failed"
    await shop.payment(payment, "recovered")
    async with shop.sessions() as session:
        order = await session.get(Order, "a" * 32)
        assert order.status == "paid" and order.mono_invoice_id == "invoice1"


async def test_ambiguous_delivery_is_not_retried(shop, payment):
    await shop.payment(payment, "success")
    bot = AsyncMock()
    bot.send_message.side_effect = TimeoutError()
    assert await deliver_one(shop, bot)
    assert not await deliver_one(shop, bot)
    async with shop.sessions() as session:
        order = await session.get(Order, "a" * 32)
        assert order.status == "paid" and order.delivery_uncertain


async def test_checkout_snapshot_and_repeat_click(shop):
    shop.mono.create.return_value = {"invoiceId": "new", "pageUrl": "https://pay.test/new"}
    order = await shop.checkout(1, 1)
    async with shop.sessions() as session, session.begin():
        product = await session.get(Product, 1)
        product.price, product.name_ua = 59900, "Changed"
    repeated = await shop.checkout(1, 1)
    assert repeated.id == order.id
    assert repeated.price_snapshot == 49900 and repeated.product_name_snapshot == "Гра"
    assert shop.mono.create.await_count == 1


async def test_maintenance_and_hidden_product(shop):
    async with shop.sessions() as session, session.begin():
        await set_setting(session, "enabled", "false")
    with pytest.raises(ShopError, match="maintenance"):
        await shop.checkout(1, 1)
    async with shop.sessions() as session, session.begin():
        await set_setting(session, "enabled", "true")
        (await session.get(Product, 1)).visible = False
    with pytest.raises(ShopError, match="missing"):
        await shop.checkout(1, 1)
    shop.mono.create.assert_not_awaited()


async def test_checkout_allows_product_without_gmail(shop):
    async with shop.sessions() as session, session.begin():
        (await session.get(Product, 1)).gmail_credentials_encrypted = None
    shop.mono.create.return_value = {"invoiceId": "without-mail", "pageUrl": "https://pay.test/no-mail"}
    order = await shop.checkout(1, 1)
    assert order.mono_invoice_id == "without-mail"


async def test_payment_message_is_saved_and_animated(shop):
    async with shop.sessions() as session, session.begin():
        order = await session.get(Order, "a" * 32)
        order.payment_url = "https://pay.test/invoice1"
    await shop.attach_payment_message("a" * 32, 1, 321)
    async with shop.sessions() as session, session.begin():
        (await session.get(Order, "a" * 32)).payment_animation_at = None
    bot = AsyncMock()
    await animate_waiting_payments(shop, bot)
    bot.edit_message_text.assert_awaited_once()
    call = bot.edit_message_text.call_args
    assert call.kwargs["chat_id"] == 1
    assert call.kwargs["message_id"] == 321
    assert "Очікуємо підтвердження оплати" in call.args[0]
    async with shop.sessions() as session:
        order = await session.get(Order, "a" * 32)
        assert order.payment_animation_step == 1
        assert order.payment_animation_at is not None
