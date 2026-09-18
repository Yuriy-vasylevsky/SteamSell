from datetime import UTC, datetime
from unittest.mock import AsyncMock, Mock

from aiogram import Bot
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Update
from sqlalchemy import select

from app.bot import create_dispatcher
from app.models import Broadcast, MailCodeRequest, Order, PaymentReceipt, Product, Setting, User


def update(user_id=1, text=None, callback=None, group=False, photo=None):
    actor = {"id": user_id, "is_bot": False, "first_name": "Tester"}
    message = {
        "message_id": 1,
        "date": datetime.now(UTC),
        "chat": {"id": user_id, "type": "group" if group else "private"},
        "from": actor,
    }
    if photo:
        message["photo"] = [
            {"file_id": photo, "file_unique_id": "unique-" + photo, "width": 800, "height": 600}
        ]
    else:
        message["text"] = text or "Menu"
    if callback:
        return Update.model_validate(
            {
                "update_id": 1,
                "callback_query": {
                    "id": "callback1",
                    "from": actor,
                    "chat_instance": "chat",
                    "data": callback,
                    "message": message,
                },
            }
        )
    return Update.model_validate({"update_id": 1, "message": message})


async def test_receipt_example_can_be_managed_and_is_shown_before_instructions(shop):
    storage = MemoryStorage()
    dp = create_dispatcher(shop, storage)
    bot = Bot("123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi")
    bot.session = AsyncMock()

    await dp.feed_update(bot, update(user_id=99, callback="a:settings"))
    menu_markup = bot.session.call_args.args[1].reply_markup
    assert all(len(row) == 2 for row in menu_markup.inline_keyboard[:-1])
    assert len(menu_markup.inline_keyboard[-1]) == 1

    await dp.feed_update(bot, update(user_id=99, callback="a:receipt_example_settings"))
    settings_markup = bot.session.call_args.args[1].reply_markup
    assert any(
        button.callback_data == "a:receipt_example_photo"
        for row in settings_markup.inline_keyboard
        for button in row
    )

    await dp.feed_update(bot, update(user_id=99, callback="a:receipt_example_photo"))
    await dp.feed_update(bot, update(user_id=99, photo="example-receipt"))
    async with shop.sessions() as session:
        assert (await session.get(Setting, "receipt_example_photo")).value == "example-receipt"
        order = await session.get(Order, "a" * 32)
        order.payment_method = "receipt"
        await session.commit()

    await dp.feed_update(bot, update(user_id=1, callback="receipt:" + "a" * 32))
    method = bot.session.call_args.args[1]
    assert method.photo == "example-receipt"
    assert "Приклад вдалої квитанції" in method.caption
    assert "Надішліть скриншот успішної оплати" in method.caption
    assert method.parse_mode == "HTML"

    await dp.feed_update(bot, update(user_id=99, callback="a:receipt_example_photo_clear"))
    await dp.feed_update(bot, update(user_id=1, callback="receipt:" + "a" * 32))
    method = bot.session.call_args.args[1]
    assert "Надішліть скриншот успішної оплати" in method.text
    assert not hasattr(method, "photo")


async def test_start_language_catalog_and_admin_denial(shop):
    storage = MemoryStorage()
    dp = create_dispatcher(shop, storage)
    bot = Bot("123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi")
    bot.session = AsyncMock()
    await dp.feed_update(bot, update(user_id=3, text="/start"))
    assert "Выберите язык" in bot.session.call_args.args[1].text
    await dp.feed_update(bot, update(user_id=3, callback="lang:ru"))
    async with shop.sessions() as session:
        assert (await session.get(User, 3)).language == "ru"
    await dp.feed_update(bot, update(user_id=3, callback="catalog:0"))
    method = bot.session.call_args.args[1]
    assert "Список товаров" in method.text
    assert "Игра" in method.reply_markup.inline_keyboard[0][0].text
    before = bot.session.call_count
    await dp.feed_update(bot, update(user_id=3, callback="a:settings"))
    assert bot.session.call_count == before + 1  # callback acknowledgement only
    await dp.feed_update(bot, update(user_id=99, text="/admin"))
    assert "Керування" in bot.session.call_args.args[1].text
    await storage.close()


async def test_admin_button_is_not_shown_in_home_inline_menu(shop):
    dp = create_dispatcher(shop, MemoryStorage())
    bot = Bot("123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi")
    bot.session = AsyncMock()

    await dp.feed_update(bot, update(user_id=1, callback="home"))
    user_markup = bot.session.call_args.args[1].reply_markup
    assert all(button.callback_data != "a:home" for row in user_markup.inline_keyboard for button in row)

    await dp.feed_update(bot, update(user_id=99, callback="home"))
    admin_markup = bot.session.call_args.args[1].reply_markup
    assert all(button.callback_data != "a:home" for row in admin_markup.inline_keyboard for button in row)


async def test_group_chats_do_not_receive_credentials(shop):
    dp = create_dispatcher(shop, MemoryStorage())
    bot = Bot("123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi")
    bot.session = AsyncMock()
    await dp.feed_update(bot, update(callback="purchase:" + "a" * 32, group=True))
    bot.session.assert_not_called()


async def test_additional_admin_can_open_panel(shop):
    shop.cfg.admin_ids = "1042869230"
    dp = create_dispatcher(shop, MemoryStorage())
    bot = Bot("123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi")
    bot.session = AsyncMock()
    await dp.feed_update(bot, update(user_id=1042869230, callback="home"))
    markup = bot.session.call_args.args[1].reply_markup
    assert all(b.callback_data != "a:home" for row in markup.inline_keyboard for b in row)
    await dp.feed_update(bot, update(user_id=1042869230, text="/admin"))
    assert "Керування" in bot.session.call_args.args[1].text
    await dp.feed_update(bot, update(user_id=99, text="/admin"))
    assert "Керування" in bot.session.call_args.args[1].text


async def test_admin_recent_orders_are_available_from_general_settings(shop):
    dp = create_dispatcher(shop, MemoryStorage())
    bot = Bot("123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi")
    bot.session = AsyncMock()

    await dp.feed_update(bot, update(user_id=99, callback="a:general_settings"))
    markup = bot.session.call_args.args[1].reply_markup
    recent_button = next(
        button
        for row in markup.inline_keyboard
        for button in row
        if button.callback_data == "a:recent_orders:today:0"
    )
    assert recent_button.text == "🧾 Історія замовлень"

    await dp.feed_update(bot, update(user_id=99, callback="a:recent_orders:today:0"))
    method = bot.session.call_args.args[1]
    assert "Історія замовлень" in method.text
    assert "Original" in method.text
    assert "#aaaaaaaa" not in method.text
    assert "ID: 1" in method.text
    assert "499" in method.text
    assert method.reply_markup.inline_keyboard[0][0].text == "1 / 1"
    assert [button.callback_data for button in method.reply_markup.inline_keyboard[1]] == [
        "a:recent_orders:today:0",
        "a:recent_orders:yesterday:0",
    ]


async def test_primary_admin_can_manage_database_admins(shop):
    dp = create_dispatcher(shop, MemoryStorage())
    bot = Bot("123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi")
    bot.session = AsyncMock()

    await dp.feed_update(bot, update(user_id=99, callback="a:user_admin:1"))
    async with shop.sessions() as session:
        assert (await session.get(User, 1)).is_admin

    await dp.feed_update(bot, update(user_id=1, text="/admin"))
    assert "Керування магазином" in bot.session.call_args.args[1].text

    await dp.feed_update(bot, update(user_id=1, callback="a:user_admin:2"))
    async with shop.sessions() as session:
        assert not (await session.get(User, 2)).is_admin
    assert "лише головний адміністратор" in bot.session.call_args.args[1].text

    await dp.feed_update(bot, update(user_id=99, callback="a:user_admin:1"))
    async with shop.sessions() as session:
        assert not (await session.get(User, 1)).is_admin


async def test_admin_can_block_and_unblock_user(shop):
    dp = create_dispatcher(shop, MemoryStorage())
    bot = Bot("123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi")
    bot.session = AsyncMock()

    await dp.feed_update(bot, update(user_id=99, callback="a:user_block:1"))
    async with shop.sessions() as session:
        assert (await session.get(User, 1)).access_blocked

    await dp.feed_update(bot, update(user_id=1, text="Menu"))
    assert "заблоковано за порушення правил" in bot.session.call_args.args[1].text

    await dp.feed_update(bot, update(user_id=99, callback="a:user_block:1"))
    async with shop.sessions() as session:
        assert not (await session.get(User, 1)).access_blocked


async def test_admin_user_delete_removes_related_data(shop):
    async with shop.sessions() as session, session.begin():
        session.add(
            PaymentReceipt(
                order_id="a" * 32,
                user_id=1,
                telegram_file_id="receipt-file",
                file_sha256="b" * 64,
            )
        )
        session.add(
            MailCodeRequest(
                user_id=1,
                product_id=1,
                order_id="a" * 32,
                outcome="found",
            )
        )

    dp = create_dispatcher(shop, MemoryStorage())
    bot = Bot("123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi")
    bot.session = AsyncMock()
    await dp.feed_update(bot, update(user_id=99, callback="a:user_delete_confirm:1"))

    async with shop.sessions() as session:
        assert await session.get(User, 1) is None
        assert await session.get(Order, "a" * 32) is None
        assert await session.scalar(select(PaymentReceipt).where(PaymentReceipt.user_id == 1)) is None
        assert await session.scalar(select(MailCodeRequest).where(MailCodeRequest.user_id == 1)) is None
async def test_admin_product_wizard_edit_and_confirmed_delete(shop):
    storage = MemoryStorage()
    dp = create_dispatcher(shop, storage)
    bot = Bot("123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi")
    bot.session = AsyncMock()
    shop.gmail.authorize_url = Mock(return_value="https://accounts.google.com/test")
    shop.redis.get.return_value = shop.vault.pack({"email": "new@gmail.com", "refresh_token": "new-refresh"})

    async def send(text=None, callback=None):
        await dp.feed_update(bot, update(user_id=99, text=text, callback=callback))

    await send(callback="a:add")
    for value in ("Нова гра", "599.50"):
        await send(text=value)
    for _ in range(2):  # optional photo and shared description
        await send(callback="a:skip")
    await send(text="new_login")
    await send(text="new_password")
    await send(callback="a:oauth")
    await send(callback="a:gmail_done")
    await send(callback="a:feature:1")
    await send(callback="a:feature:1")
    async with shop.sessions() as session:
        assert await session.scalar(select(Product).where(Product.name_ua == "Нова гра")) is None
    await send(callback="a:save")
    async with shop.sessions() as session:
        p = await session.scalar(select(Product).where(Product.name_ua == "Нова гра"))
        assert p and p.price == 59950 and p.featured and p.on_home and not p.image_file_id
        assert p.name_ua == p.name_ru == "Нова гра"
        assert p.description_ua == p.description_ru == ""
        assert shop.vault.decrypt(p.steam_password_encrypted) == "new_password"
        pid = p.id
    await send(callback=f"a:edit:{pid}:price")
    await send(text="699")
    await send(callback="a:save_edit")
    await send(callback=f"a:edit:{pid}:name_ua")
    await send(text="Оновлена назва")
    await send(callback="a:save_edit")
    await send(callback=f"a:delete:{pid}")
    async with shop.sessions() as session:
        p = await session.get(Product, pid)
        assert p.price == 69900 and p.deleted_at is None
        assert p.name_ua == p.name_ru == "Оновлена назва"
    await send(callback="a:confirm_delete")
    async with shop.sessions() as session:
        p = await session.get(Product, pid)
        assert p.deleted_at and not p.visible


async def test_admin_can_create_product_without_gmail(shop):
    storage = MemoryStorage()
    dp = create_dispatcher(shop, storage)
    bot = Bot("123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi")
    bot.session = AsyncMock()

    async def send(text=None, callback=None):
        await dp.feed_update(bot, update(user_id=99, text=text, callback=callback))

    await send(callback="a:add")
    await send(text="Товар без пошти")
    await send(text="100")
    await send(callback="a:skip")  # photo
    await send(text="Один опис для обох мов")
    await send(text="login")
    await send(text="password")
    await send(callback="a:skip")  # Gmail
    await send(callback="a:feature:0")
    await send(callback="a:feature:0")
    await send(callback="a:save")

    async with shop.sessions() as session:
        product = await session.scalar(select(Product).where(Product.name_ua == "Товар без пошти"))
        assert product is not None
        assert product.name_ru == product.name_ua
        assert product.description_ru == product.description_ua
        assert product.gmail_credentials_encrypted is None


async def test_broadcast_requires_two_confirmations(shop):
    dp = create_dispatcher(shop, MemoryStorage())
    bot = Bot("123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi")
    bot.session = AsyncMock()
    await dp.feed_update(bot, update(user_id=99, callback="a:broadcast"))
    await dp.feed_update(bot, update(user_id=99, text="Нові ігри"))
    await dp.feed_update(bot, update(user_id=99, callback="a:broadcast_send"))
    async with shop.sessions() as session:
        assert await session.scalar(select(Broadcast)) is None
    await dp.feed_update(bot, update(user_id=99, callback="a:broadcast_confirm"))
    await dp.feed_update(bot, update(user_id=99, callback="a:broadcast_send"))
    async with shop.sessions() as session:
        job = await session.scalar(select(Broadcast))
        assert job and job.payload["text"] == "Нові ігри" and job.status == "queued"
