import logging
from io import BytesIO

from aiogram import BaseMiddleware, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy import func, select

from app.access import admin_ids, is_admin
from app.i18n import money, tr
from app.models import Order, PaymentReceipt, Product, User, now
from app.receipts import ReceiptError, analyze_receipt, evaluate_receipt, prepare_receipt
from app.services import SUCCESS, ShopError, setting
from app.ui import (
    back,
    home_rows,
    pagination,
    payment_rows,
    payment_wait_text,
    product_text,
    purchase_rows,
    purchase_text,
    render,
    keyboard,
)

log = logging.getLogger(__name__)


class ReceiptForm(StatesGroup):
    photo = State()


def receipt_cards(shop, order):
    if not order.payment_cards_encrypted:
        return ""
    return "\n".join(f"{c['label']}: {c['number']}" for c in shop.vault.unpack(order.payment_cards_encrypted)["cards"])


class ContextMiddleware(BaseMiddleware):
    def __init__(self, shop):
        self.shop = shop

    async def __call__(self, handler, event, data):
        actor = event.from_user
        message = event.message if isinstance(event, CallbackQuery) else event
        if not actor or not isinstance(message, Message) or message.chat.type != "private":
            return
        async with self.shop.sessions() as session:
            user = await session.get(User, actor.id)
            if not user:
                user = User(id=actor.id, first_name=actor.first_name)
                session.add(user)
            user.username = actor.username
            user.first_name = actor.first_name
            user.last_activity_at = now()
            user.blocked = False
            await session.commit()
            data.update(shop=self.shop, session=session, user=user, lang=user.language or "ua")
            if isinstance(event, CallbackQuery):
                await event.answer()
            try:
                return await handler(event, data)
            except ShopError as error:
                await render(event, tr(str(error), data["lang"]), [back(data["lang"])])
            except Exception:
                # Do not log Telegram updates, FSM content, HTTP errors or traceback locals.
                log.error("bot_operation_failed user=%s", actor.id)
                await session.rollback()
                await render(event, tr("error", data["lang"]), [back(data["lang"])])


def create_dispatcher(shop, storage):
    from app.admin import admin_router

    dp = Dispatcher(storage=storage)
    dp.message.outer_middleware(ContextMiddleware(shop))
    dp.callback_query.outer_middleware(ContextMiddleware(shop))
    dp.include_router(admin_router())
    router = Router()

    async def show_home(event, session, lang, **kwargs):
        text = await setting(session, "welcome_" + lang, tr("welcome", lang))
        rows = home_rows(lang)
        if is_admin(shop.cfg, event.from_user.id):
            rows.append([("⚙️ Адмін-панель", "a:home")])
        products = (
            await session.scalars(
                select(Product)
                .where(
                    Product.visible.is_(True),
                    Product.featured.is_(True),
                    Product.deleted_at.is_(None),
                )
                .order_by(Product.featured_position, Product.id)
                .limit(5)
            )
        ).all()
        rows += [[(getattr(p, "name_" + lang), f"product:{p.id}")] for p in products]
        if await setting(session, "enabled", "true") != "true":
            text = tr("maintenance", lang)
        from html import escape

        await render(event, escape(text), rows)

    async def language(event, **kwargs):
        await render(
            event,
            "Оберіть мову / Выберите язык",
            [
                [("🇺🇦 Українська", "lang:ua"), ("🇷🇺 Русский", "lang:ru")],
            ],
        )

    @router.message(CommandStart())
    async def start(message, session, user, lang, state):
        await state.clear()
        if user.language:
            await show_home(message, session, lang)
        else:
            await language(message)

    @router.message(Command("cancel"))
    async def cancel(message, state, session, lang):
        await state.clear()
        await show_home(message, session, lang)

    @router.callback_query(F.data == "language")
    async def choose_language(callback):
        await language(callback)

    @router.callback_query(F.data.startswith("lang:"))
    async def save_language(callback, session, user):
        lang = callback.data.split(":")[1]
        if lang not in ("ua", "ru"):
            return
        user.language = lang
        await session.commit()
        await show_home(callback, session, lang)

    @router.callback_query(F.data == "home")
    async def home(callback, session, lang, state):
        await state.clear()
        await show_home(callback, session, lang)

    @router.callback_query(F.data == "noop")
    async def noop(callback):
        pass

    @router.callback_query(F.data.in_({"info", "support"}))
    async def info(callback, session, lang, shop):
        from html import escape

        support = await setting(session, "support_username", shop.cfg.support_username)
        text = (
            await setting(session, "info_" + lang, tr("info_default", lang))
            if callback.data == "info"
            else tr("support", lang)
        )
        await render(
            callback,
            escape(text),
            [[(tr("support", lang), "https://t.me/" + support.lstrip("@"))], back(lang)],
        )

    @router.callback_query(F.data.regexp(r"^(catalog|featured|purchases):\d+$"))
    async def listing(callback, session, lang, user, shop):
        kind, page = callback.data.split(":")
        page = min(int(page), 100000)
        size = int(await setting(session, "page_size", str(shop.cfg.page_size)))
        if kind == "purchases":
            query = select(Order).where(Order.user_id == user.id, Order.status.in_(SUCCESS))
            sorting = (Order.created_at.desc(),)
        else:
            query = select(Product).where(Product.visible.is_(True), Product.deleted_at.is_(None))
            if kind == "featured":
                query = query.where(Product.featured.is_(True))
            sorting = (Product.featured_position, Product.id)
        total = await session.scalar(select(func.count()).select_from(query.subquery()))
        page = min(page, max(0, (total - 1) // size))
        items = (await session.scalars(query.order_by(*sorting).offset(page * size).limit(size))).all()
        rows = []
        for item in items:
            title = item.product_name_snapshot if kind == "purchases" else getattr(item, "name_" + lang)
            price = item.price_snapshot if kind == "purchases" else item.price
            rows.append(
                [
                    (
                        title + " — " + money(price),
                        f"{'purchase' if kind == 'purchases' else 'product'}:{item.id}",
                    )
                ]
            )
        rows.extend([pagination(kind, page, total, size), back(lang)])
        await render(callback, tr(kind, lang) if items else tr("empty", lang), rows)

    @router.callback_query(F.data.regexp(r"^product:\d+$"))
    async def product(callback, session, lang):
        p = await session.get(Product, int(callback.data.split(":")[1]))
        if not p or p.deleted_at or not p.visible:
            raise ShopError("missing")
        await render(
            callback,
            product_text(p, lang),
            [
                [(tr("buy", lang) + " — " + money(p.price), f"buy:{p.id}")],
                back(lang, "catalog:0"),
            ],
            photo=p.image_file_id,
        )

    @router.callback_query(F.data.regexp(r"^buy:\d+$"))
    async def buy(callback, shop, user, lang):
        async with shop.redis.lock(f"checkout:{user.id}", timeout=45, blocking_timeout=1):
            order = await shop.checkout(user.id, int(callback.data.split(":")[1]))
        payment_message = await render(
            callback,
            payment_wait_text(
                order, lang,
                card=(receipt_cards(shop, order) if order.payment_method == "receipt" else getattr(shop.cfg, "manual_card", "")),
            ),
            payment_rows(order, lang),
        )
        await shop.attach_payment_message(order.id, user.id, payment_message.message_id)

    @router.callback_query(F.data.regexp(r"^receipt:[a-f0-9]{32}$"))
    async def receipt_start(callback, state, session, user):
        order = await session.get(Order, callback.data.split(":")[1])
        if not order or order.user_id != user.id or order.status != "waiting_payment" or order.payment_method != "receipt":
            raise ShopError("missing")
        await state.set_state(ReceiptForm.photo)
        await state.set_data({"receipt_order_id": order.id})
        await callback.message.answer(
            "📎 Надішліть скриншот успішної оплати саме як фото. На ньому мають бути чітко видні сума, останні 4 цифри картки отримувача, статус і час."
        )

    @router.message(ReceiptForm.photo)
    async def receipt_photo(message, state, shop, user, lang):
        order_id = (await state.get_data()).get("receipt_order_id")
        if not message.photo:
            await message.answer("Надішліть скриншот саме як фото або /cancel.")
            return
        photo = message.photo[-1]
        if photo.file_size and photo.file_size > 8 * 1024 * 1024:
            await message.answer("Фото більше 8 MB. Надішліть менший файл.")
            return
        destination = BytesIO()
        telegram_file = await message.bot.get_file(photo.file_id)
        await message.bot.download_file(telegram_file.file_path, destination)
        try:
            prepared = prepare_receipt(destination.getvalue())
        except ReceiptError as error:
            await message.answer(str(error))
            return
        async with shop.redis.lock(f"receipt:{prepared.sha256}", timeout=120, blocking_timeout=2):
            async with shop.sessions() as receipt_session, receipt_session.begin():
                if await receipt_session.scalar(select(PaymentReceipt.id).where(PaymentReceipt.file_sha256 == prepared.sha256)):
                    await message.answer("Цей скриншот уже використовувався. Надішліть іншу квитанцію.")
                    return
                order = await receipt_session.get(Order, order_id, with_for_update=True)
                if not order or order.user_id != user.id or order.status != "waiting_payment" or order.payment_method != "receipt":
                    raise ShopError("missing")
                receipt = PaymentReceipt(order_id=order.id, user_id=user.id, telegram_file_id=photo.file_id,
                                         file_sha256=prepared.sha256)
                receipt_session.add(receipt)
                await receipt_session.flush()
                receipt_id = receipt.id
                expected, created = order.price_snapshot, order.created_at
                cards = shop.vault.unpack(order.payment_cards_encrypted)["cards"]
            try:
                analysis = await analyze_receipt(
                    shop.mono.client, shop.cfg.deepseek_api_key.get_secret_value(),
                    shop.cfg.deepseek_vision_model, prepared,
                )
                approved, reason = evaluate_receipt(analysis, expected, {c["last4"] for c in cards}, created)
            except Exception as error:
                log.warning("deepseek_receipt_failed order=%s error=%s", order_id, type(error).__name__)
                analysis, approved, reason = None, False, f"Помилка DeepSeek: {type(error).__name__}"
            async with shop.sessions() as receipt_session, receipt_session.begin():
                receipt = await receipt_session.get(PaymentReceipt, receipt_id, with_for_update=True)
                order = await receipt_session.get(Order, order_id, with_for_update=True)
                receipt.analysis, receipt.reason = analysis, reason
                if approved and order.status == "waiting_payment":
                    receipt.status, receipt.reviewed_at = "approved", now()
                    order.status, order.paid_at = "paid", now()
                else:
                    receipt.status = "manual_review"
            await state.clear()
            if approved:
                await message.answer("✅ Скрін підтверджено. Дані покупки зараз надійдуть у чат.")
                return
            caption = f"🧾 Ручна перевірка скріну\n{order.product_name_snapshot}\n{money(expected)}\n@{user.username or '—'} / {user.id}\nПричина: {reason}\nЗаявка #{receipt_id}"
            for admin_id in admin_ids(shop.cfg):
                try:
                    await message.bot.send_photo(admin_id, photo.file_id, caption=caption,
                        reply_markup=keyboard([[('✅ Підтвердити', f'a:receipt:approve:{receipt_id}'), ('❌ Відхилити', f'a:receipt:reject:{receipt_id}')]]))
                except Exception:
                    log.warning("receipt_admin_notify_failed admin=%s receipt=%s", admin_id, receipt_id)
            await message.answer("Скрін передано адміністратору на перевірку.")

    @router.callback_query(F.data.regexp(r"^check_payment:[a-f0-9]{32}$"))
    async def check_payment(callback, shop, user, lang):
        from app.personal import reconcile_personal

        order_id = callback.data.split(":")[1]
        async with shop.sessions() as check_session:
            order = await check_session.get(Order, order_id)
            if not order or order.user_id != user.id or order.payment_method != "personal":
                raise ShopError("missing")
            if order.status not in SUCCESS and order.status != "waiting_payment":
                raise ShopError("missing")
            paid = order.status in SUCCESS
        result = "checked" if paid else await reconcile_personal(shop, order_id)
        async with shop.sessions() as check_session:
            order = await check_session.get(Order, order_id)
            if order.status in SUCCESS:
                await render(callback, "✅ Оплату підтверджено. Покупка доступна нижче; бот також надішле дані в чат.",
                             [[("📦 Відкрити покупку", f"purchase:{order.id}")], back(lang)])
                return
        messages = {
            "cooldown": "⏳ Зачекайте приблизно хвилину після попередньої перевірки та натисніть ще раз.",
            "error": "Не вдалося отримати виписку Monobank. Спробуйте через хвилину або зверніться до підтримки.",
        }
        await callback.message.answer(messages.get(result,
            "Невикористаний платіж на цю суму після створення замовлення поки не знайдено. Повторіть перевірку через хвилину. Не сплачуйте повторно."))

    @router.callback_query(F.data.regexp(r"^purchase:[a-f0-9]{32}$"))
    async def purchase(callback, session, shop, user, lang):
        order = await session.get(Order, callback.data.split(":")[1])
        if not order or order.user_id != user.id or order.status not in SUCCESS:
            raise ShopError("missing")
        product = await session.get(Product, order.product_id)
        support = await setting(session, "support_username", shop.cfg.support_username)
        await render(
            callback,
            purchase_text(order, product, lang, shop.vault),
            purchase_rows(order, lang, support, bool(product.gmail_credentials_encrypted)),
            protect=True,
        )

    @router.callback_query(F.data.regexp(r"^code:[a-f0-9]{32}$"))
    async def code(callback, shop, user, lang):
        code = await shop.code(user.id, callback.data.split(":")[1])
        await render(
            callback,
            f"{tr('code_result', lang)}:\n<code>{code}</code>",
            [back(lang, "purchase:" + callback.data.split(":")[1])],
            protect=True,
        )

    @router.message()
    async def fallback(message, session, lang):
        await show_home(message, session, lang)

    dp.include_router(router)
    return dp
