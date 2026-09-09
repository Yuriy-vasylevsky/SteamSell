from datetime import UTC, datetime
from unittest.mock import AsyncMock, Mock

from aiogram import Bot
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Update
from sqlalchemy import select

from app.bot import create_dispatcher
from app.models import Broadcast, Product, User


def update(user_id=1, text=None, callback=None, group=False):
    actor = {"id": user_id, "is_bot": False, "first_name": "Tester"}
    message = {
        "message_id": 1,
        "date": datetime.now(UTC),
        "chat": {"id": user_id, "type": "group" if group else "private"},
        "from": actor,
        "text": text or "Menu",
    }
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
    assert "Все игры" in method.text
    assert "Игра" in method.reply_markup.inline_keyboard[0][0].text
    before = bot.session.call_count
    await dp.feed_update(bot, update(user_id=3, callback="a:settings"))
    assert bot.session.call_count == before + 1  # callback acknowledgement only
    await dp.feed_update(bot, update(user_id=99, text="/admin"))
    assert "Керування" in bot.session.call_args.args[1].text
    await storage.close()


async def test_admin_button_is_visible_only_to_admin(shop):
    dp = create_dispatcher(shop, MemoryStorage())
    bot = Bot("123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi")
    bot.session = AsyncMock()

    await dp.feed_update(bot, update(user_id=1, callback="home"))
    user_markup = bot.session.call_args.args[1].reply_markup
    assert all(button.callback_data != "a:home" for row in user_markup.inline_keyboard for button in row)

    await dp.feed_update(bot, update(user_id=99, callback="home"))
    admin_markup = bot.session.call_args.args[1].reply_markup
    assert any(button.callback_data == "a:home" for row in admin_markup.inline_keyboard for button in row)


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
    assert any(b.callback_data == "a:home" for row in markup.inline_keyboard for b in row)
    await dp.feed_update(bot, update(user_id=1042869230, text="/admin"))
    assert "Керування" in bot.session.call_args.args[1].text
    await dp.feed_update(bot, update(user_id=99, text="/admin"))
    assert "Керування" in bot.session.call_args.args[1].text


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
    async with shop.sessions() as session:
        assert await session.scalar(select(Product).where(Product.name_ua == "Нова гра")) is None
    await send(callback="a:save")
    async with shop.sessions() as session:
        p = await session.scalar(select(Product).where(Product.name_ua == "Нова гра"))
        assert p and p.price == 59950 and p.featured and not p.image_file_id
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
