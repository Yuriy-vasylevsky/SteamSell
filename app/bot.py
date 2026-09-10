import asyncio
import logging
from contextlib import suppress
from html import escape
from io import BytesIO

from aiogram import BaseMiddleware, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy import func, select

from app.access import admin_ids, is_admin
from app.i18n import money, tr
from app.models import MailCodeRequest, Order, PaymentReceipt, Product, User, now
from app.receipts import ReceiptError, analyze_receipt, evaluate_receipt, prepare_receipt
from app.services import (
    SUCCESS,
    ShopError,
    configured_manual_card,
    has_recent_purchase,
    setting,
    shared_gmail_credentials,
)
from app.ui import (
    back,
    home_rows,
    keyboard,
    pagination,
    payment_rows,
    payment_wait_text,
    persistent_menu,
    product_text,
    purchase_rows,
    purchase_text,
    render,
)

log = logging.getLogger(__name__)

ACTIVATION_GUIDE = """🎟 Інструкція з активації гри в STEAM:

1. Зайдіть у Steam з логіном і паролем, які отримали.
2. Отримайте код Steam Guard (на запит у чаті).
   У верхньому лівому куті Steam відкрийте Steam → Налаштування → Remote Play та вимкніть повзунок.
3. Завантажте та встановіть гру з бібліотеки Steam.
4. Натисніть правою кнопкою на гру в бібліотеці, відкрийте «Властивості» та вимкніть хмарні збереження Steam Cloud.
5. Запустіть гру в онлайн-режимі й одразу вийдіть (ALT+F4). Якщо у гри немає Denuvo, пропустіть цей пункт.
6. Налаштуйте офлайн-режим:

• У верхньому лівому куті Steam натисніть Steam → «Перейти в автономний режим».
• Потім натисніть Steam → «Увійти в інший акаунт» та вимкніть пункт «При кожному запуску Steam запитувати, який акаунт використовувати».

P.S. Ви можете вільно перемикатися між акаунтами без втрати автономного режиму!"""

FAQ_TEXT = """❓ Як працює офлайн-активація?
❗️У популярних лаунчерах, зокрема Steam, немає обмежень за кількістю офлайн-гравців. Після покупки ви можете грати в придбану гру на нашому акаунті офлайн без обмежень у часі.
———————————————————
❓ Який термін обслуговування активації після покупки?
❗️Технічне обслуговування діє 6 місяців із моменту купівлі. Через 180 днів потрібно знову придбати активацію, якщо ви захочете ще раз пройти гру.
———————————————————
❓ Чому така низька ціна? Схоже на обман 👺
❗️Ніякого обману немає. Магазин нічого не втрачає, якщо ви граєте на нашому акаунті необмежений час. На акаунті може перебувати необмежена кількість людей, і вони не заважають одне одному. Тому активація коштує від 5% до 10% вартості гри.
———————————————————
❓ Якщо активація злетіла, як бути?
❗️Є дві категорії причин:

1. Можна безкоштовно реактивувати:
• оновлення гри;
• випадковий збій через оновлення лаунчера;
• вимкнення світла.

2. Потрібно купувати активацію знову:
• перевстановлення Windows;
• заміна комплектуючих ПК;
• завершення терміну обслуговування (180 днів).
———————————————————
❓ Якщо активація злетить, а на акаунті не буде вільних місць для реактивації?
❗️Достатньо почекати до 24 годин або ми надамо заміну. Під час заміни є ризик втратити збереження гри. Такий випадок можливий лише після оновлення гри та протягом перших 30 днів після її виходу.
———————————————————
❓ Що означає активація, як вона відбувається і що таке Denuvo?
❗️Denuvo Anti-Tamper — технологія захисту від несанкціонованого доступу. Через цей захист ігри можуть роками не з’являтися на «веселокачай». Denuvo дозволяє до 5 нових ПК протягом 24 годин. Ви використовуєте одну із цих активацій, після чого можете ввімкнути автономний режим і грати.
———————————————————
❓ Я купив гру, але на акаунті немає вільного місця. Як бути?
❗️Ви не залишитеся без активації та не мусите чекати. Якщо на виданому акаунті немає місць, ми надамо інший без повторного завантаження гри. У крайньому разі створимо новий акаунт, щоб ви могли грати без очікування.
———————————————————
❓ Минув час, а дані акаунта недійсні. Що робити?
❗️Іноді акаунти блокують і доступ до них зникає. Зазвичай це трапляється наприкінці першого місяця після виходу гри. У такому разі ми надамо інший акаунт з тією самою грою та виданням.
———————————————————
❓ Гра мені не сподобалася або мій ПК занадто слабкий. Чи можна повернути гроші?
❗️Повернення грошей не передбачено, оскільки активацію неможливо повернути. Для постійних клієнтів та в окремих випадках ми можемо додатково надати іншу гру.
———————————————————
❓ Що з цінами?
❗️Ціна залежить від популярності гри та попиту. У перші тижні після виходу вона вища, згодом знижується, але не нижче 49 грн."""

RECEIPT_ANALYSIS_FRAMES = (
    "🔍✨ Аналізуємо квитанцію\n\n▰▱▱▱  Перевіряємо зображення",
    "🧾🔎 Аналізуємо квитанцію\n\n▰▰▱▱  Читаємо суму та картку",
    "💳✨ Аналізуємо квитанцію\n\n▰▰▰▱  Звіряємо платіж",
    "🛡️🤖 Аналізуємо квитанцію\n\n▰▰▰▰  Завершуємо перевірку",
)

MONO_PAYMENT_FRAMES = (
    "💳✨ Перевіряємо оплату\n\n▰▱▱  Підключаємося до Monobank",
    "🔎🏦 Перевіряємо оплату\n\n▰▰▱  Шукаємо переказ",
    "🛡️✅ Перевіряємо оплату\n\n▰▰▰  Звіряємо суму та час",
)


async def animate_receipt_analysis(progress):
    step = 1
    while True:
        await asyncio.sleep(1.4)
        try:
            await progress.edit_text(RECEIPT_ANALYSIS_FRAMES[step % len(RECEIPT_ANALYSIS_FRAMES)])
        except Exception:
            return
        step += 1


async def finish_receipt_analysis(progress, text):
    try:
        await progress.edit_text(text)
    except Exception:
        await progress.answer(text)


async def animate_mono_payment(message):
    for frame in MONO_PAYMENT_FRAMES:
        try:
            await message.edit_text(frame)
        except Exception:
            pass
        await asyncio.sleep(1)


async def send_receipt_for_admin_review(bot, shop, receipt, order, user, reason):
    caption = (
        f"🧾 Ручна перевірка скріну\n{order.product_name_snapshot}\n{money(order.price_snapshot)}\n"
        f"@{user.username or '—'} / {user.id}\nПричина: {reason}\nЗаявка #{receipt.id}"
    )
    for admin_id in admin_ids(shop.cfg):
        try:
            await bot.send_photo(
                admin_id,
                receipt.telegram_file_id,
                caption=caption,
                reply_markup=keyboard(
                    [
                        [
                            ("✅ Підтвердити", f"a:receipt:approve:{receipt.id}"),
                            ("❌ Відхилити", f"a:receipt:reject:{receipt.id}"),
                        ]
                    ]
                ),
            )
        except Exception:
            log.warning("receipt_admin_notify_failed admin=%s receipt=%s", admin_id, receipt.id)


class ReceiptForm(StatesGroup):
    photo = State()


def receipt_cards(shop, order):
    if not order.payment_cards_encrypted:
        return ""
    return "\n".join(
        f"{c['label']}: {c['number']}" for c in shop.vault.unpack(order.payment_cards_encrypted)["cards"]
    )


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

    async def show_home(event, session, lang, first_launch=False, **kwargs):
        text = (
            await setting(session, "welcome_" + lang, tr("welcome", lang))
            if first_launch
            else tr("menu", lang)
        )
        rows = home_rows(lang)
        if is_admin(shop.cfg, event.from_user.id):
            rows.append([("⚙️ Адмін-панель", "a:home")])
        products = (
            await session.scalars(
                select(Product)
                .where(
                    Product.visible.is_(True),
                    Product.on_home.is_(True),
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

    async def pin_menu(event, user, lang):
        message = event.message if isinstance(event, CallbackQuery) else event
        await message.answer(
            "\u2063",
            reply_markup=persistent_menu(lang, is_admin(shop.cfg, user.id), user.broadcast_subscribed),
        )

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
            await pin_menu(message, user, lang)
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
        first_launch = user.language is None
        user.language = lang
        await session.commit()
        await show_home(callback, session, lang, first_launch=first_launch)
        await pin_menu(callback, user, lang)

    @router.callback_query(F.data == "home")
    async def home(callback, session, lang, state):
        await state.clear()
        await show_home(callback, session, lang)

    @router.callback_query(F.data == "noop")
    async def noop(callback):
        pass

    menu_actions = {
        tr(key, language_code): key
        for language_code in ("ua", "ru")
        for key in ("featured", "catalog", "purchases", "info", "support", "language")
    }
    menu_actions.update(
        {
            "📨 Підписатися на розсилку": "newsletter",
            "📨 Подписаться на рассылку": "newsletter",
            "🔕 Відписатися від розсилки": "newsletter",
            "🔕 Отписаться от рассылки": "newsletter",
        }
    )

    @router.message(F.text.in_(set(menu_actions)))
    async def persistent_menu_action(message, session, lang, user, shop):
        action = menu_actions[message.text]
        if action == "newsletter":
            user.broadcast_subscribed = not user.broadcast_subscribed
            await session.commit()
            text = (
                "✅ Ви підписалися на розсилку."
                if user.broadcast_subscribed and lang == "ua"
                else "✅ Вы подписались на рассылку."
                if user.broadcast_subscribed
                else "🔕 Ви відписалися від розсилки."
                if lang == "ua"
                else "🔕 Вы отписались от рассылки."
            )
            await message.answer(
                text,
                reply_markup=persistent_menu(lang, is_admin(shop.cfg, user.id), user.broadcast_subscribed),
            )
            return
        if action == "language":
            await language(message)
            return
        if action in ("info", "support"):
            support = await setting(session, "support_username", shop.cfg.support_username)
            text = tr("info_default", lang) if action == "info" else tr("support", lang)
            rows = [[(tr("support", lang), "https://t.me/" + support.lstrip("@"))], back(lang)]
            if action == "info":
                rows = [
                    [(tr("activation_guide", lang), "activation_guide")],
                    [(tr("faq", lang), "faq")],
                    back(lang),
                ]
            await render(message, escape(text), rows)
            return
        size = int(await setting(session, "page_size", str(shop.cfg.page_size)))
        if action == "purchases":
            query = select(Order).where(Order.user_id == user.id, Order.status.in_(SUCCESS))
            sorting = (Order.created_at.desc(),)
        else:
            query = select(Product).where(Product.visible.is_(True), Product.deleted_at.is_(None))
            if action == "featured":
                query = query.where(Product.featured.is_(True))
            sorting = (Product.featured_position, Product.id)
        total = await session.scalar(select(func.count()).select_from(query.subquery()))
        items = (await session.scalars(query.order_by(*sorting).limit(size))).all()
        rows = []
        for item in items:
            title = item.product_name_snapshot if action == "purchases" else getattr(item, "name_" + lang)
            price = item.price_snapshot if action == "purchases" else item.price
            rows.append(
                [
                    (
                        title + " — " + money(price),
                        f"{'purchase' if action == 'purchases' else 'product'}:{item.id}",
                    )
                ]
            )
        rows.extend([pagination(action, 0, total, size), back(lang)])
        await render(message, tr(action, lang) if items else tr("empty", lang), rows)

    @router.callback_query(F.data.in_({"info", "support"}))
    async def info(callback, session, lang, shop):
        support = await setting(session, "support_username", shop.cfg.support_username)
        text = tr("info_default", lang) if callback.data == "info" else tr("support", lang)
        rows = [[(tr("support", lang), "https://t.me/" + support.lstrip("@"))], back(lang)]
        if callback.data == "info":
            rows = [
                [(tr("activation_guide", lang), "activation_guide")],
                [(tr("faq", lang), "faq")],
                back(lang),
            ]
        await render(callback, escape(text), rows)

    @router.callback_query(F.data.regexp(r"^(activation_guide|usage_rules)(?::[a-f0-9]{32})?$"))
    @router.callback_query(F.data == "faq")
    async def information_page(callback, lang):
        action, _, order_id = callback.data.partition(":")
        text = (
            ACTIVATION_GUIDE
            if action == "activation_guide"
            else tr("info_default", lang)
            if action == "usage_rules"
            else FAQ_TEXT
        )
        target = f"purchase:{order_id}" if order_id else "info"
        await render(callback, escape(text), [back(lang, target)])

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
    async def buy(callback, session, shop, user, lang):
        async with shop.redis.lock(f"checkout:{user.id}", timeout=45, blocking_timeout=1):
            order = await shop.checkout(user.id, int(callback.data.split(":")[1]))
        mono_card = await configured_manual_card(session, shop)
        payment_message = await render(
            callback,
            payment_wait_text(
                order,
                lang,
                card=(receipt_cards(shop, order) if order.payment_method == "receipt" else mono_card),
            ),
            payment_rows(order, lang),
        )
        await shop.attach_payment_message(order.id, user.id, payment_message.message_id)

    @router.callback_query(F.data.regexp(r"^receipt:[a-f0-9]{32}$"))
    async def receipt_start(callback, state, session, user):
        order = await session.get(Order, callback.data.split(":")[1])
        if (
            not order
            or order.user_id != user.id
            or order.status != "waiting_payment"
            or order.payment_method != "receipt"
        ):
            raise ShopError("missing")
        attempts = await session.scalar(
            select(func.count()).select_from(PaymentReceipt).where(PaymentReceipt.order_id == order.id)
        )
        pending = await session.scalar(
            select(PaymentReceipt.id)
            .where(
                PaymentReceipt.order_id == order.id,
                PaymentReceipt.status.in_(("analyzing", "manual_review")),
            )
            .limit(1)
        )
        if pending:
            await callback.message.answer("Квитанція вже перевіряється або очікує рішення адміністратора.")
            return
        if attempts >= 2:
            await callback.message.answer(
                "Ліміт квитанцій для цієї оплати вичерпано. Очікуйте рішення адміністратора."
            )
            return
        await state.set_state(ReceiptForm.photo)
        await state.set_data({"receipt_order_id": order.id})
        await callback.message.answer(
            "📎 Надішліть скриншот успішної оплати саме як фото. На ньому мають бути чітко видні сума, останні 4 цифри картки отримувача, статус і час."
        )

    @router.callback_query(F.data.regexp(r"^receipt_manual:\d+$"))
    async def receipt_manual_review(callback, state, shop, user, lang):
        receipt_id = int(callback.data.rsplit(":", 1)[1])
        async with shop.sessions() as review_session, review_session.begin():
            receipt = await review_session.get(PaymentReceipt, receipt_id, with_for_update=True)
            if not receipt or receipt.user_id != user.id or receipt.status != "rejected":
                raise ShopError("missing")
            order = await review_session.get(Order, receipt.order_id, with_for_update=True)
            if not order or order.status != "waiting_payment":
                raise ShopError("missing")
            receipt.status = "manual_review"
            reason = (receipt.reason or "Причину не вдалося визначити").strip()
        await state.clear()
        await send_receipt_for_admin_review(callback.bot, shop, receipt, order, user, reason)
        await render(
            callback,
            "🧑‍💻 Квитанцію та причину відмови передано адміністратору. Очікуйте рішення.",
            [back(lang)],
        )

    @router.callback_query(F.data.regexp(r"^cancel_payment:[a-f0-9]{32}$"))
    async def cancel_payment(callback, state, shop, user, lang):
        order_id = callback.data.split(":", 1)[1]
        await shop.cancel_payment(order_id, user.id)
        await state.clear()
        text = "❌ Платіж скасовано." if lang == "ua" else "❌ Платёж отменён."
        await render(callback, text, [back(lang)])

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
                if await receipt_session.scalar(
                    select(PaymentReceipt.id).where(PaymentReceipt.file_sha256 == prepared.sha256)
                ):
                    await message.answer("Цей скриншот уже використовувався. Надішліть іншу квитанцію.")
                    return
                order = await receipt_session.get(Order, order_id, with_for_update=True)
                if (
                    not order
                    or order.user_id != user.id
                    or order.status != "waiting_payment"
                    or order.payment_method != "receipt"
                ):
                    raise ShopError("missing")
                attempts = await receipt_session.scalar(
                    select(func.count())
                    .select_from(PaymentReceipt)
                    .where(PaymentReceipt.order_id == order.id)
                )
                pending = await receipt_session.scalar(
                    select(PaymentReceipt.id)
                    .where(
                        PaymentReceipt.order_id == order.id,
                        PaymentReceipt.status.in_(("analyzing", "manual_review")),
                    )
                    .limit(1)
                )
                if pending:
                    await message.answer("Попередня квитанція ще перевіряється. Дочекайтеся результату.")
                    return
                if attempts >= 2:
                    await message.answer("Ліміт: дві квитанції на одну оплату.")
                    return
                attempt_number = attempts + 1
                receipt = PaymentReceipt(
                    order_id=order.id,
                    user_id=user.id,
                    telegram_file_id=photo.file_id,
                    file_sha256=prepared.sha256,
                )
                receipt_session.add(receipt)
                await receipt_session.flush()
                receipt_id = receipt.id
                expected, created = order.price_snapshot, receipt.created_at
                cards = shop.vault.unpack(order.payment_cards_encrypted)["cards"]
            progress = await message.answer(RECEIPT_ANALYSIS_FRAMES[0])
            animation = asyncio.create_task(animate_receipt_analysis(progress))
            try:
                analysis = await analyze_receipt(
                    shop.mono.client,
                    shop.cfg.deepseek_api_key.get_secret_value(),
                    shop.cfg.deepseek_vision_model,
                    prepared,
                )
                approved, reason = evaluate_receipt(analysis, expected, {c["last4"] for c in cards}, created)
            except ReceiptError as error:
                log.warning("deepseek_receipt_invalid order=%s", order_id)
                analysis, approved, reason = None, False, str(error)
            except Exception as error:
                log.warning("deepseek_receipt_failed order=%s error=%s", order_id, type(error).__name__)
                analysis, approved, reason = (
                    None,
                    False,
                    "Сервіс DeepSeek тимчасово недоступний. Потрібна ручна перевірка скриншота",
                )
            finally:
                animation.cancel()
                with suppress(asyncio.CancelledError):
                    await animation
            async with shop.sessions() as receipt_session, receipt_session.begin():
                receipt = await receipt_session.get(PaymentReceipt, receipt_id, with_for_update=True)
                order = await receipt_session.get(Order, order_id, with_for_update=True)
                manual_required = False
                if approved and await has_recent_purchase(
                    receipt_session, user.id, order.id, receipt.created_at
                ):
                    approved = False
                    manual_required = True
                    reason = (
                        "Повторна покупка цього користувача протягом 12 хвилин. "
                        "Потрібна ручна перевірка, щоб виключити повторне використання скриншота"
                    )
                receipt.analysis, receipt.reason = analysis, reason
                if order.status != "waiting_payment":
                    receipt.status, approved = "cancelled", False
                    receipt.reason = "Платіж скасовано покупцем"
                elif approved:
                    receipt.status, receipt.reviewed_at = "approved", now()
                    order.status, order.paid_at = "paid", now()
                elif attempt_number == 1 and not manual_required:
                    receipt.status = "rejected"
                else:
                    receipt.status = "manual_review"
                cancelled = receipt.status == "cancelled"
                first_rejected = receipt.status == "rejected" and attempt_number == 1
            await state.clear()
            progress_text = (
                "❌ Платіж скасовано. Скриншот не зараховано."
                if cancelled
                else (
                    "✅✨ Аналіз завершено — оплату підтверджено!"
                    if approved
                    else "❌ Аналіз завершено — квитанцію відхилено."
                    if first_rejected
                    else "🧑‍💻🛡️ Аналіз завершено — скрин передано адміністратору."
                )
            )
            await finish_receipt_analysis(progress, progress_text)
            if cancelled:
                await message.answer("❌ Платіж уже скасовано. Скриншот не зараховано.")
                return
            if approved:
                approved_caption = (
                    f"🤖 DeepSeek підтвердив оплату\n{order.product_name_snapshot}\n{money(expected)}\n"
                    f"@{user.username or '—'} / {user.id}\nКартка: •••• {analysis.get('recipient_card_last4')}\n"
                    f"Час: {analysis.get('payment_datetime')}\nЗаявка #{receipt_id}"
                )
                for admin_id in admin_ids(shop.cfg):
                    try:
                        await message.bot.send_photo(admin_id, photo.file_id, caption=approved_caption)
                    except Exception:
                        log.warning("receipt_admin_notify_failed admin=%s receipt=%s", admin_id, receipt_id)
                await message.answer("✅ Скрін підтверджено. Дані покупки зараз надійдуть у чат.")
                return
            if first_rejected:
                await message.answer(
                    f"❌ Квитанцію не прийнято.\n\nПричина: {reason}\n\n"
                    "У вас залишилася одна спроба надіслати іншу квитанцію.",
                    reply_markup=keyboard(
                        [
                            [("📎 Надіслати іншу квитанцію", f"receipt:{order.id}")],
                            [("🧑‍💻 Передати на перевірку адміну", f"receipt_manual:{receipt_id}")],
                        ]
                    ),
                )
                return
            await send_receipt_for_admin_review(message.bot, shop, receipt, order, user, reason)
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
        if paid:
            result = "checked"
        else:
            await animate_mono_payment(callback.message)
            result = await reconcile_personal(shop, order_id)
        async with shop.sessions() as check_session:
            order = await check_session.get(Order, order_id)
            if order.status in SUCCESS:
                await render(
                    callback,
                    "✅ Оплату підтверджено. Покупка доступна нижче; бот також надішле дані в чат.",
                    [[("📦 Відкрити покупку", f"purchase:{order.id}")], back(lang)],
                )
                return
        messages = {
            "cooldown": "⏳ Повторна перевірка доступна через 20 секунд.",
            "error": "Не вдалося отримати виписку Monobank. Спробуйте ще раз через 20 секунд.",
        }
        await render(
            callback,
            messages.get(
                result,
                "Платіж на цю суму поки не знайдено. Повторіть перевірку через 20 секунд. Не сплачуйте повторно.",
            ),
            payment_rows(order, lang),
        )

    @router.callback_query(F.data.regexp(r"^purchase:[a-f0-9]{32}$"))
    async def purchase(callback, session, shop, user, lang):
        order = await session.get(Order, callback.data.split(":")[1])
        if not order or order.user_id != user.id or order.status not in SUCCESS:
            raise ShopError("missing")
        product = await session.get(Product, order.product_id)
        support = await setting(session, "support_username", shop.cfg.support_username)
        gmail_connected = bool(await shared_gmail_credentials(session, shop))
        found_codes = await session.scalar(
            select(func.count())
            .select_from(MailCodeRequest)
            .where(MailCodeRequest.order_id == order.id, MailCodeRequest.outcome == "found")
        )
        await render(
            callback,
            purchase_text(order, product, lang, shop.vault),
            purchase_rows(
                order,
                lang,
                support,
                gmail_connected,
                max(0, 2 - max(0, found_codes - 1)),
            ),
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
