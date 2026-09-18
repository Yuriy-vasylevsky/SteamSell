import asyncio
import logging
from contextlib import suppress
from datetime import datetime
from html import escape
from io import BytesIO
from zoneinfo import ZoneInfo

from aiogram import BaseMiddleware, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message, ReplyKeyboardRemove
from sqlalchemy import func, or_, select

from app.access import all_admin_ids, is_admin
from app.i18n import money, tr
from app.models import (
    MailCodeRequest,
    Order,
    PaymentCard,
    PaymentReceipt,
    Product,
    PromoCode,
    Review,
    User,
    now,
)
from app.receipts import ReceiptError, analyze_receipt, evaluate_receipt, prepare_receipt
from app.services import (
    SUCCESS,
    TIP_PERCENTS,
    ShopError,
    active_discount_percent,
    apply_discount,
    aware,
    code_request_window_open,
    configured_manual_card,
    has_recent_purchase,
    loyalty_status,
    price_with_tip,
    setting,
    valid_promo,
)
from app.ui import (
    back,
    copy_card_rows,
    discounted_price_text,
    home_rows,
    keyboard,
    manual_delivery_text,
    pagination,
    payment_rows,
    payment_wait_text,
    persistent_menu,
    product_text,
    purchase_rows,
    purchase_text,
    render,
    user_reference,
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
    for frame in MONO_PAYMENT_FRAMES[1:]:
        try:
            await message.edit_text(frame)
        except Exception:
            pass
        await asyncio.sleep(1)


async def send_receipt_for_admin_review(bot, shop, receipt, order, user, reason):
    caption = (
        f"🧾 Ручна перевірка скріну\n{escape(order.product_name_snapshot)}\n"
        f"💰 До сплати: <b>{money(order.price_snapshot)}</b>\n"
        f"👤 {user_reference(user)}\nПричина: {escape(reason)}\nЗаявка #{receipt.id}"
    )
    async with shop.sessions() as admin_session:
        recipients = await all_admin_ids(admin_session, shop.cfg)
    for admin_id in recipients:
        try:
            await bot.send_photo(
                admin_id,
                receipt.telegram_file_id,
                caption=caption,
                parse_mode="HTML",
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


class ReviewForm(StatesGroup):
    text = State()


class PromoForm(StatesGroup):
    code = State()


class SearchForm(StatesGroup):
    query = State()


def receipt_cards(shop, order):
    if not order.payment_cards_encrypted:
        return ""
    return "\n".join(
        f"<b>{escape(str(card['label']))}:</b> <code>{escape(str(card['number']))}</code>"
        for card in shop.vault.unpack(order.payment_cards_encrypted)["cards"]
    )


def receipt_card_numbers(shop, order):
    if not order.payment_cards_encrypted:
        return []
    return [card["number"] for card in shop.vault.unpack(order.payment_cards_encrypted)["cards"]]


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
            if user.access_blocked and not is_admin(self.shop.cfg, user.id, user):
                await message.answer(
                    tr("access_blocked", user.language or "ua"),
                    reply_markup=ReplyKeyboardRemove(),
                )
                return
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
        if await setting(session, "order_request_enabled", "true") == "true":
            order_request_title = await setting(
                session,
                "order_request_title_" + lang,
                tr("order_request_title", lang),
            )
            rows.append([(order_request_title, "order_request")])
        products = (
            await session.scalars(
                select(Product)
                .where(
                    Product.visible.is_(True),
                    Product.on_home.is_(True),
                    Product.deleted_at.is_(None),
                    or_(Product.stock_quantity.is_(None), Product.stock_quantity > 0),
                )
                .order_by(Product.featured_position, Product.id)
                .limit(5)
            )
        ).all()
        rows += [
            [
                (
                    getattr(p, "name_" + lang),
                    f"product:{p.id}",
                )
            ]
            for p in products
        ]
        if await setting(session, "enabled", "true") != "true":
            text = tr("maintenance", lang)
        from html import escape

        await render(event, escape(text), rows)

    async def pin_menu(event, user, lang):
        message = event.message if isinstance(event, CallbackQuery) else event
        async with shop.sessions() as menu_session:
            loyalty_enabled = (
                await setting(menu_session, "loyalty_enabled", "true") == "true"
                and await setting(menu_session, "sale_enabled", "false") != "true"
            )
        await message.answer(
            "\u2063",
            reply_markup=persistent_menu(
                lang,
                is_admin(shop.cfg, user.id, user),
                user.broadcast_subscribed,
                loyalty_enabled,
            ),
        )

    async def language(event, **kwargs):
        await render(
            event,
            "Оберіть мову / Выберите язык",
            [
                [("🇺🇦 Українська", "lang:ua"), ("🇷🇺 Русский", "lang:ru")],
            ],
        )

    def account_rows(lang, subscribed, loyalty_enabled, reviews_enabled):
        newsletter = (
            "🔕 Відписатися від розсилки"
            if subscribed and lang == "ua"
            else "🔕 Отписаться от рассылки"
            if subscribed
            else "📨 Підписатися на розсилку"
            if lang == "ua"
            else "📨 Подписаться на рассылку"
        )
        rows = [
            [(newsletter, "account:newsletter")],
            [(tr("loyalty", lang), "account:loyalty")],
            [(tr("language", lang), "account:language")],
            [(tr("admin_thanks", lang), "account:admin_thanks")],
        ]
        if reviews_enabled:
            rows[2].append((tr("review", lang), "account:reviews:0"))
        rows.append(back(lang))
        return rows

    async def show_account(event, session, user, lang):
        loyalty = await loyalty_status(session, user.id)
        reviews_enabled = await setting(session, "reviews_enabled", "true") == "true"
        profile_name = user.first_name or ("@" + user.username if user.username else "—")
        title = "МІЙ КАБІНЕТ" if lang == "ua" else "МОЙ КАБИНЕТ"
        user_label = "Ім'я" if lang == "ua" else "Имя"
        spent_label = "Витрачена сума" if lang == "ua" else "Потраченная сумма"
        discount_label = "Постійна знижка" if lang == "ua" else "Постоянная скидка"
        text = (
            f"👤 <b>{title}</b>\n"
            "━━━━━━━━━━━━━━\n\n"
            f"👤 <b>{user_label}:</b> {escape(profile_name)}\n"
            f"🆔 <b>ID:</b> <code>{user.id}</code>\n\n"
            f"💰 <b>{spent_label}:</b> {money(loyalty['spent'])}\n"
            f"🏷 <b>{discount_label}:</b> {loyalty['discount_percent']}%"
        )
        await render(
            event,
            text,
            account_rows(lang, user.broadcast_subscribed, loyalty["enabled"], reviews_enabled),
        )

    async def show_loyalty(event, session, user, lang):
        loyalty = await loyalty_status(session, user.id)
        if not loyalty["enabled"]:
            await show_account(event, session, user, lang)
            return
        current = loyalty["current"]
        next_level = loyalty["next"]
        current_name = (
            getattr(current, "name_" + lang)
            if current
            else ("Без рівня" if lang == "ua" else "Без уровня")
        )
        text = (
            f"🎁 <b>{tr('loyalty', lang).removeprefix('🎁 ')}</b>\n"
            "━━━━━━━━━━━━━━\n\n"
            f"🏅 {'Ваш рівень' if lang == 'ua' else 'Ваш уровень'}: "
            f"<b>{escape(current_name)}</b>\n"
            f"💰 {'Витрачено' if lang == 'ua' else 'Потрачено'}: "
            f"<b>{money(loyalty['spent'])}</b>\n"
            f"🏷 {'Постійна знижка' if lang == 'ua' else 'Постоянная скидка'}: "
            f"<b>{loyalty['discount_percent']}%</b>\n\n"
            f"📊 <b>{'Усі рівні' if lang == 'ua' else 'Все уровни'}:</b>\n"
        )
        level_icons = ("🥉", "🥈", "🥇", "💠", "💎")
        for index, level in enumerate(loyalty["levels"]):
            marker = "✅ " if current and level.level_number == current.level_number else ""
            name = escape(getattr(level, "name_" + lang))
            threshold = money(level.threshold_kopecks)
            text += (
                f"{marker}{level_icons[index] if index < len(level_icons) else '🏅'} "
                f"<b>{name}</b> — {'від' if lang == 'ua' else 'от'} {threshold} · "
                f"<b>{level.discount_percent}%</b>\n"
            )
        if next_level:
            needed = max(0, next_level.threshold_kopecks - loyalty["spent"])
            text += (
                f"\n🎯 {'До наступного рівня залишилось' if lang == 'ua' else 'До следующего уровня осталось'}: "
                f"<b>{money(needed)}</b>"
            )
        else:
            text += (
                "\n💎 <b>Ви досягли максимального рівня!</b>"
                if lang == "ua"
                else "\n💎 <b>Вы достигли максимального уровня!</b>"
            )
        text += (
            "\n\n💡 Знижка застосовується автоматично до кожної покупки."
            if lang == "ua"
            else "\n\n💡 Скидка применяется автоматически к каждой покупке."
        )
        await render(event, text, [back(lang, "account")])

    @router.message(CommandStart())
    async def start(message, session, user, lang, state):
        await state.clear()
        if user.language:
            await show_home(message, session, lang)
            await pin_menu(message, user, lang)
        else:
            await language(message)

    @router.message(Command("menu"))
    async def command_menu(message, session, user, lang, state):
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

    @router.callback_query(F.data == "account")
    async def account(callback, session, user, lang, state):
        await state.clear()
        await show_account(callback, session, user, lang)

    @router.callback_query(F.data == "account:language")
    async def account_language(callback, lang):
        await render(
            callback,
            "Оберіть мову / Выберите язык",
            [
                [("🇺🇦 Українська", "account_lang:ua"), ("🇷🇺 Русский", "account_lang:ru")],
                back(lang, "account"),
            ],
        )

    @router.callback_query(F.data.startswith("account_lang:"))
    async def save_account_language(callback, session, user):
        lang = callback.data.split(":", 1)[1]
        if lang not in ("ua", "ru"):
            return
        user.language = lang
        await session.commit()
        await show_account(callback, session, user, lang)
        await pin_menu(callback, user, lang)

    @router.callback_query(F.data == "account:newsletter")
    async def account_newsletter(callback, session, user, lang):
        user.broadcast_subscribed = not user.broadcast_subscribed
        await session.commit()
        await show_account(callback, session, user, lang)

    async def review_slots(session, user_id):
        purchases = await session.scalar(
            select(func.count()).select_from(Order).where(
                Order.user_id == user_id,
                Order.status.in_(SUCCESS),
            )
        )
        reviews = await session.scalar(
            select(func.count()).select_from(Review).where(Review.user_id == user_id)
        )
        return max(0, purchases - reviews)

    @router.callback_query(F.data == "account:review")
    async def account_review(callback, state, session, user, lang):
        if await setting(session, "reviews_enabled", "true") != "true":
            await show_account(callback, session, user, lang)
            return
        if not await review_slots(session, user.id):
            await render(
                callback,
                "Ви вже використали всі відгуки за свої покупки."
                if lang == "ua"
                else "Вы уже использовали все отзывы за свои покупки.",
                [back(lang, "account:reviews:0")],
            )
            return
        await state.set_state(ReviewForm.text)
        await render(callback, tr("review_prompt", lang), [back(lang, "account:reviews:0")])

    async def show_reviews(event, session, lang, page=0, user=None):
        if await setting(session, "reviews_enabled", "true") != "true":
            if user:
                await show_account(event, session, user, lang)
            return
        total = await session.scalar(select(func.count()).select_from(Review))
        page = min(page, max(0, (total - 1) // 7))
        items = (
            await session.scalars(
                select(Review).order_by(Review.created_at.desc()).offset(page * 7).limit(7)
            )
        ).all()
        rows = []
        for review in items:
            preview = " ".join(review.text.split())
            if len(preview) > 38:
                preview = preview[:35] + "…"
            created_at = review.created_at.astimezone(ZoneInfo("Europe/Kyiv"))
            rows.append(
                [(f"💬 {preview} · {created_at:%d.%m %H:%M}", f"account:review_detail:{review.id}:{page}")]
            )
        if items:
            rows.append(pagination("account:reviews", page, total, 7))
        if user and await review_slots(session, user.id):
            rows.append([(tr("leave_review", lang), "account:review")])
        rows.append(back(lang, "account"))
        text = tr("reviews_title", lang) + "\n\n"
        text += (
            f"{tr('reviews_total', lang)}: <b>{total}</b>"
            if items
            else tr("reviews_empty", lang)
        )
        await render(event, text, rows)

    @router.callback_query(F.data.regexp(r"^account:reviews:\d+$"))
    async def account_reviews(callback, session, user, lang, state):
        await state.clear()
        await show_reviews(callback, session, lang, int(callback.data.rsplit(":", 1)[1]), user)

    @router.callback_query(F.data.regexp(r"^account:review_detail:\d+:\d+$"))
    async def account_review_detail(callback, session, user, lang):
        _, _, review_id, page = callback.data.split(":")
        review = await session.get(Review, int(review_id))
        if not review:
            await show_reviews(callback, session, lang, int(page), user)
            return
        if await setting(session, "reviews_enabled", "true") != "true":
            await show_account(callback, session, user, lang)
            return
        created_at = review.created_at.astimezone(ZoneInfo("Europe/Kyiv"))
        await render(
            callback,
            f"💬 <b>{'Відгук' if lang == 'ua' else 'Отзыв'}</b>\n\n{escape(review.text)}"
            f"\n\n🕒 {created_at:%d.%m.%Y · %H:%M}",
            [[(tr("back_to_reviews", lang), f"account:reviews:{page}")]],
        )

    @router.callback_query(F.data == "account:loyalty")
    async def account_loyalty(callback, session, user, lang):
        await show_loyalty(callback, session, user, lang)

    @router.callback_query(F.data == "account:admin_thanks")
    async def account_admin_thanks(callback, session, shop, lang):
        cards = (
            await session.scalars(
                select(PaymentCard)
                .where(PaymentCard.active.is_(True))
                .order_by(PaymentCard.id)
                .limit(2)
            )
        ).all()
        if not cards:
            await render(callback, tr("admin_thanks_unavailable", lang), [back(lang, "account")])
            return
        card_text = "\n".join(
            f"<b>{escape(card.label)}:</b> <code>{escape(shop.vault.decrypt(card.number_encrypted))}</code>"
            for card in cards
        )
        await render(
            callback,
            tr("admin_thanks_text", lang) + "\n\n" + card_text,
            copy_card_rows(
                [shop.vault.decrypt(card.number_encrypted) for card in cards], lang
            )
            + [back(lang, "account")],
        )

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

    @router.callback_query(F.data == "order_request")
    async def order_request(callback, session, shop, lang):
        if await setting(session, "order_request_enabled", "true") != "true":
            await show_home(callback, session, lang)
            return
        title = await setting(
            session,
            "order_request_title_" + lang,
            tr("order_request_title", lang),
        )
        description = await setting(
            session,
            "order_request_description_" + lang,
            tr("order_request_description", lang),
        )
        photo = await setting(session, "order_request_photo", "") or None
        support = await setting(session, "support_username", shop.cfg.support_username)
        support_target = (
            "https://t.me/" + support.lstrip("@")
            if support
            else f"tg://user?id={shop.cfg.admin_id}"
        )
        await render(
            callback,
            f"📦 <b>{escape(title)}</b>\n\n{escape(description)}",
            [
                [(tr("contact_admin", lang), support_target)],
                back(lang),
            ],
            photo=photo,
        )

    @router.callback_query(F.data == "noop")
    async def noop(callback):
        pass

    menu_actions = {
        tr(key, language_code): key
        for language_code in ("ua", "ru")
        for key in (
            "featured",
            "catalog",
            "purchases",
            "info",
            "support",
            "account",
            "language",
            "review",
            "loyalty",
        )
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
    async def persistent_menu_action(message, state, session, lang, user, shop):
        action = menu_actions[message.text]
        await state.clear()
        if action == "account":
            await state.clear()
            await show_account(message, session, user, lang)
            return
        if action == "newsletter":
            user.broadcast_subscribed = not user.broadcast_subscribed
            await session.commit()
            await show_account(message, session, user, lang)
            return
        if action == "language":
            await language(message)
            return
        if action == "review":
            await state.clear()
            await show_reviews(message, session, lang, user=user)
            return
        if action == "loyalty":
            await show_loyalty(message, session, user, lang)
            return
        if action in ("info", "support"):
            support = await setting(session, "support_username", shop.cfg.support_username)
            text = (
                await setting(session, "info_" + lang, tr("info_default", lang))
                if action == "info"
                else tr("support", lang)
            )
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
            if is_admin(shop.cfg, user.id, user):
                raw_hidden_before = await setting(
                    session, f"admin_purchases_reset_at:{user.id}", ""
                )
                if raw_hidden_before:
                    try:
                        query = query.where(
                            Order.created_at >= aware(datetime.fromisoformat(raw_hidden_before))
                        )
                    except ValueError:
                        pass
            sorting = (Order.created_at.desc(),)
        else:
            query = select(Product).where(
                Product.visible.is_(True),
                Product.deleted_at.is_(None),
                or_(Product.stock_quantity.is_(None), Product.stock_quantity > 0),
            )
            if action == "featured":
                query = query.where(Product.featured.is_(True))
            sorting = (
                (Product.featured_position, Product.id)
                if action == "featured"
                else (getattr(Product, "name_" + lang), Product.id)
            )
        total = await session.scalar(select(func.count()).select_from(query.subquery()))
        items = (await session.scalars(query.order_by(*sorting).limit(size))).all()
        rows = []
        for item in items:
            title = item.product_name_snapshot if action == "purchases" else getattr(item, "name_" + lang)
            title_with_price = (
                f"{title} — {money(item.price_snapshot)}"
                if action == "purchases"
                else title
            )
            rows.append(
                [
                    (
                        title_with_price,
                        f"purchase:{item.id}:0"
                        if action == "purchases"
                        else f"product:{item.id}:{action}:0",
                    )
                ]
            )
        if action in ("catalog", "featured"):
            rows.append([("🔎 Пошук" if lang == "ua" else "🔎 Поиск", f"search:{action}")])
        rows.extend([pagination(action, 0, total, size), back(lang)])
        await render(message, tr(action, lang) if items else tr("empty", lang), rows)

    @router.callback_query(F.data.in_({"info", "support"}))
    async def info(callback, session, lang, shop):
        support = await setting(session, "support_username", shop.cfg.support_username)
        text = (
            await setting(session, "info_" + lang, tr("info_default", lang))
            if callback.data == "info"
            else tr("support", lang)
        )
        rows = [[(tr("support", lang), "https://t.me/" + support.lstrip("@"))], back(lang)]
        if callback.data == "info":
            rows = [
                [(tr("activation_guide", lang), "activation_guide")],
                [(tr("faq", lang), "faq")],
                back(lang),
            ]
        await render(callback, escape(text), rows)

    @router.callback_query(
        F.data.regexp(r"^(activation_guide|alternative_activation_guide|usage_rules)(?::[a-f0-9]{32})?$")
    )
    @router.callback_query(F.data == "faq")
    async def information_page(callback, session, lang):
        action, _, order_id = callback.data.partition(":")
        text = (
            await setting(session, "activation_guide_" + lang, ACTIVATION_GUIDE)
            if action == "activation_guide"
            else await setting(session, "alternative_activation_guide_" + lang, "")
            if action == "alternative_activation_guide"
            else await setting(session, "info_" + lang, tr("info_default", lang))
            if action == "usage_rules"
            else await setting(session, "faq_" + lang, FAQ_TEXT)
        )
        target = f"purchase:{order_id}" if order_id else "info"
        await render(callback, escape(text), [back(lang, target)])

    @router.callback_query(F.data.regexp(r"^(catalog|featured|purchases):\d+$"))
    async def listing(callback, state, session, lang, user, shop):
        await state.clear()
        kind, page = callback.data.split(":")
        page = min(int(page), 100000)
        size = int(await setting(session, "page_size", str(shop.cfg.page_size)))
        if kind == "purchases":
            query = select(Order).where(Order.user_id == user.id, Order.status.in_(SUCCESS))
            if is_admin(shop.cfg, user.id, user):
                raw_hidden_before = await setting(
                    session, f"admin_purchases_reset_at:{user.id}", ""
                )
                if raw_hidden_before:
                    try:
                        query = query.where(
                            Order.created_at >= aware(datetime.fromisoformat(raw_hidden_before))
                        )
                    except ValueError:
                        pass
            sorting = (Order.created_at.desc(),)
        else:
            query = select(Product).where(
                Product.visible.is_(True),
                Product.deleted_at.is_(None),
                or_(Product.stock_quantity.is_(None), Product.stock_quantity > 0),
            )
            if kind == "featured":
                query = query.where(Product.featured.is_(True))
            sorting = (
                (Product.featured_position, Product.id)
                if kind == "featured"
                else (getattr(Product, "name_" + lang), Product.id)
            )
        total = await session.scalar(select(func.count()).select_from(query.subquery()))
        page = min(page, max(0, (total - 1) // size))
        items = (await session.scalars(query.order_by(*sorting).offset(page * size).limit(size))).all()
        rows = []
        for item in items:
            title = item.product_name_snapshot if kind == "purchases" else getattr(item, "name_" + lang)
            title_with_price = (
                f"{title} — {money(item.price_snapshot)}"
                if kind == "purchases"
                else title
            )
            rows.append(
                [
                    (
                        title_with_price,
                        f"purchase:{item.id}:{page}"
                        if kind == "purchases"
                        else f"product:{item.id}:{kind}:{page}",
                    )
                ]
            )
        if kind in ("catalog", "featured"):
            rows.append([("🔎 Пошук" if lang == "ua" else "🔎 Поиск", f"search:{kind}")])
        rows.extend([pagination(kind, page, total, size), back(lang)])
        await render(callback, tr(kind, lang) if items else tr("empty", lang), rows)

    async def show_search_results(event, state, session, lang, user, shop, page=0):
        data = await state.get_data()
        kind = data.get("search_kind")
        search_query = data.get("search_query", "")
        if kind not in ("catalog", "featured") or not search_query:
            await state.clear()
            await show_home(event, session, lang)
            return
        escaped_query = (
            search_query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        )
        pattern = f"%{escaped_query}%"
        query = select(Product).where(
            Product.visible.is_(True),
            Product.deleted_at.is_(None),
            or_(Product.stock_quantity.is_(None), Product.stock_quantity > 0),
            or_(
                Product.name_ua.ilike(pattern, escape="\\"),
                Product.name_ru.ilike(pattern, escape="\\"),
            ),
        )
        if kind == "featured":
            query = query.where(Product.featured.is_(True))
        size = int(await setting(session, "page_size", str(shop.cfg.page_size)))
        total = await session.scalar(select(func.count()).select_from(query.subquery()))
        page = min(max(0, page), max(0, (total - 1) // size))
        sorting = (
            (Product.featured_position, Product.id)
            if kind == "featured"
            else (getattr(Product, "name_" + lang), Product.id)
        )
        items = (
            await session.scalars(
                query.order_by(*sorting).offset(page * size).limit(size)
            )
        ).all()
        rows = [
            [
                (
                    getattr(item, "name_" + lang),
                    f"product:{item.id}:search:{kind}:{page}",
                )
            ]
            for item in items
        ]
        if items:
            rows.append(pagination(f"search_results:{kind}", page, total, size))
        rows.extend(
            [
                [("🔎 Новий пошук" if lang == "ua" else "🔎 Новый поиск", f"search:{kind}")],
                back(lang, f"{kind}:0"),
            ]
        )
        title = "Результати пошуку" if lang == "ua" else "Результаты поиска"
        empty = "Нічого не знайдено." if lang == "ua" else "Ничего не найдено."
        await render(
            event,
            f"🔎 <b>{title}</b>\nЗапит: <code>{escape(search_query)}</code>\n\n"
            + (f"Знайдено: <b>{total}</b>" if items else empty),
            rows,
        )

    @router.callback_query(F.data.regexp(r"^search:(catalog|featured)$"))
    async def search_start(callback, state, lang):
        kind = callback.data.split(":", 1)[1]
        await state.set_state(SearchForm.query)
        await state.set_data({"search_kind": kind})
        await render(
            callback,
            "🔎 Введіть частину назви гри:" if lang == "ua" else "🔎 Введите часть названия игры:",
            [back(lang, f"{kind}:0")],
        )

    @router.message(SearchForm.query)
    async def search_submit(message, state, session, lang, user, shop):
        search_query = " ".join((message.text or "").split())
        if not 2 <= len(search_query) <= 80:
            await message.answer(
                "Введіть від 2 до 80 символів." if lang == "ua" else "Введите от 2 до 80 символов."
            )
            return
        await state.update_data(search_query=search_query)
        await show_search_results(message, state, session, lang, user, shop)

    @router.callback_query(F.data.regexp(r"^search_results:(catalog|featured):\d+$"))
    async def search_results(callback, state, session, lang, user, shop):
        await show_search_results(
            callback,
            state,
            session,
            lang,
            user,
            shop,
            int(callback.data.rsplit(":", 1)[1]),
        )

    @router.callback_query(
        F.data.regexp(
            r"^product:\d+(?::(?:catalog|featured):\d+|:search:(?:catalog|featured):\d+)?$"
        )
    )
    async def product(callback, state, session, lang, user):
        parts = callback.data.split(":")
        p = await session.get(Product, int(parts[1]))
        if not p or p.deleted_at or not p.visible or p.stock_quantity == 0:
            raise ShopError("missing")
        if len(parts) == 5 and parts[2] == "search":
            return_target = f"search_results:{parts[3]}:{parts[4]}"
        elif len(parts) == 4:
            await state.clear()
            return_target = f"{parts[2]}:{parts[3]}"
        else:
            await state.clear()
            return_target = "catalog:0"
        discount = await active_discount_percent(session, user.id)
        await render(
            callback,
            product_text(p, lang, discount),
            [
                [
                    (
                        tr("buy", lang)
                        + " — "
                        + discounted_price_text(p.price, discount, html=False),
                        f"buy:{p.id}",
                    )
                ],
                back(lang, return_target),
            ],
            photo=p.image_file_id,
        )

    async def show_checkout_options(event, session, user, lang, product_id, tip_percent=0, promo=None):
        product = await session.get(Product, int(product_id))
        if not product or product.deleted_at or not product.visible or product.stock_quantity == 0:
            raise ShopError("missing")
        sale_enabled = await setting(session, "sale_enabled", "false") == "true"
        discount = min(
            100,
            await active_discount_percent(session, user.id) + (promo.discount_percent if promo else 0),
        )
        price = apply_discount(product.price, discount)
        total_price = price_with_tip(price, tip_percent)
        delivery_text = (
            "👤 Тип видачі: <b>Вручну</b>\n"
            "<i>Товар буде видано продавцем вручну після оплати.</i>"
            if lang == "ua"
            else "👤 Тип выдачи: <b>Вручную</b>\n"
            "<i>Данный товар будет выдан вручную продавцом после оплаты.</i>"
        ) if product.delivery_mode == "manual" else (
            "🤖 Тип видачі: <b>Автоматично</b>"
            if lang == "ua"
            else "🤖 Тип выдачи: <b>Автоматически</b>"
        )
        tip_labels = {
            percent: money(price_with_tip(price, percent) - price)
            for percent in (15, 35, 50, 100)
        }
        promo_id = str(promo.id) if promo else "-"
        tip_name = f"{tip_percent}% · +{tip_labels[tip_percent]}" if tip_percent else (
            "Без чайових" if lang == "ua" else "Без чаевых"
        )
        promo_name = (
            f"<code>{promo.code}</code> −{promo.discount_percent}%"
            if promo
            else ("не обрано" if lang == "ua" else "не выбран")
        )
        text = (
            f"{'🛒 <b>Оформлення покупки</b>' if lang == 'ua' else '🛒 <b>Оформление покупки</b>'}\n\n"
            f"🎮 <b>{escape(getattr(product, 'name_' + lang))}</b>\n\n"
            f"💰 {'До сплати' if lang == 'ua' else 'К оплате'}: <b>{money(total_price)}</b>\n\n"
            f"{delivery_text}\n"
            f"💛 {'Чайові' if lang == 'ua' else 'Чаевые'}: <b>{tip_name}</b>"
        )
        if not sale_enabled:
            text += f"\n🎟 {'Промокод' if lang == 'ua' else 'Промокод'}: {promo_name}"
        rows = [
            [
                (f"15% · +{tip_labels[15]}", f"checkout_options:{product.id}:15:{promo_id}"),
                (f"35% · +{tip_labels[35]}", f"checkout_options:{product.id}:35:{promo_id}"),
            ],
            [
                (f"50% · +{tip_labels[50]}", f"checkout_options:{product.id}:50:{promo_id}"),
                (f"100% · +{tip_labels[100]}", f"checkout_options:{product.id}:100:{promo_id}"),
            ],
        ]
        if not sale_enabled:
            rows.append(
                [
                    (
                        "✏️ Змінити промокод" if promo and lang == "ua" else "✏️ Изменить промокод"
                        if promo
                        else "🎟 Ввести промокод",
                        f"promo_enter_options:{product.id}:{tip_percent}",
                    )
                ]
            )
            if promo:
                rows.append(
                    [
                        (
                            "✖️ Прибрати промокод" if lang == "ua" else "✖️ Убрать промокод",
                            f"checkout_options:{product.id}:{tip_percent}:-",
                        )
                    ]
                )
        rows += [
            [
                (
                    "➡️ Продовжити" if lang == "ua" else "➡️ Продолжить",
                    f"checkout_finish:{product.id}:{tip_percent}:{promo_id}",
                )
            ],
            back(lang, f"product:{product.id}"),
        ]
        await render(event, text, rows)

    @router.callback_query(F.data.regexp(r"^buy:\d+$"))
    async def buy(callback, session, user, lang):
        await show_checkout_options(callback, session, user, lang, int(callback.data.split(":")[1]))

    async def finish_checkout(event, session, shop, user, lang, product_id, tip_percent, promo_code=None):
        product = await session.get(Product, int(product_id))
        if not product or product.deleted_at or not product.visible or product.stock_quantity == 0:
            raise ShopError("missing")
        if promo_code:
            promo, _ = await valid_promo(session, promo_code, product.price)
            promo_code = promo.code
        else:
            promo = None
        if await setting(session, "payment_mode", "mono") == "hybrid" and (
            not promo or promo.discount_percent < 100
        ):
            callback_promo = str(promo.id) if promo_code else "-"
            await render(
                event,
                tr("choose_payment", lang),
                [
                    [(tr("pay_mono_api", lang), f"buy_method:mono:{product.id}:{tip_percent}:{callback_promo}")],
                    [(tr("pay_card", lang), f"buy_method:deepseek:{product.id}:{tip_percent}:{callback_promo}")],
                    back(lang, f"product:{product.id}"),
                ],
            )
            return
        async with shop.redis.lock(f"checkout:{user.id}", timeout=45, blocking_timeout=1):
            order = await shop.checkout(
                user.id,
                int(product_id),
                tip_percent=int(tip_percent),
                promo_code=promo_code,
            )
        if order.payment_method == "free":
            support = await setting(session, "support_username", shop.cfg.support_username)
            if order.delivery_mode_snapshot == "manual":
                await render(
                    event,
                    manual_delivery_text(order, lang),
                    [[(tr("support", lang), "https://t.me/" + support.lstrip("@"))], back(lang)],
                )
                return
            gmail_connected = bool(product.gmail_credentials_encrypted and code_request_window_open(order))
            await render(
                event,
                purchase_text(order, product, lang, shop.vault),
                purchase_rows(
                    order,
                    lang,
                    support,
                    gmail_connected,
                    product.code_limit,
                    return_target=f"purchase:{order.id}",
                ),
            )
            return
        mono_card = await configured_manual_card(session, shop)
        cards = receipt_card_numbers(shop, order) if order.payment_method == "receipt" else [mono_card]
        payment_message = await render(
            event,
            payment_wait_text(
                order,
                lang,
                card=(receipt_cards(shop, order) if order.payment_method == "receipt" else mono_card),
            ),
            payment_rows(order, lang, cards),
        )
        await shop.attach_payment_message(order.id, user.id, payment_message.message_id)

    @router.callback_query(F.data.regexp(r"^checkout_options:\d+:(0|15|35|50|100):(?:\d+|-)$"))
    async def checkout_options(callback, session, user, lang):
        _, product_id, tip_percent, promo_id = callback.data.split(":")
        promo = await session.get(PromoCode, int(promo_id)) if promo_id != "-" else None
        if promo_id != "-" and not promo:
            raise ShopError("promo_invalid")
        await show_checkout_options(callback, session, user, lang, product_id, int(tip_percent), promo)

    @router.callback_query(F.data.regexp(r"^promo_enter_options:\d+:(0|15|35|50|100)$"))
    async def promo_enter(callback, state, lang):
        _, product_id, tip_percent = callback.data.split(":")
        await state.set_state(PromoForm.code)
        await state.set_data({"promo_product_id": int(product_id), "promo_tip_percent": int(tip_percent)})
        await render(
            callback,
            "Введіть промокод одним повідомленням:" if lang == "ua" else "Введите промокод одним сообщением:",
            [[("⬅️ Назад" if lang == "ua" else "⬅️ Назад", f"checkout_options:{product_id}:{tip_percent}:-")]],
        )

    @router.callback_query(F.data.regexp(r"^checkout_finish:\d+:(0|15|35|50|100):(?:\d+|-)$"))
    async def checkout_finish(callback, session, shop, user, lang):
        _, product_id, tip_percent, promo_id = callback.data.split(":")
        promo = await session.get(PromoCode, int(promo_id)) if promo_id != "-" else None
        if promo_id != "-" and not promo:
            raise ShopError("promo_invalid")
        await finish_checkout(
            callback,
            session,
            shop,
            user,
            lang,
            product_id,
            tip_percent,
            promo.code if promo else None,
        )

    @router.message(PromoForm.code)
    async def promo_submit(message, state, session, shop, user, lang):
        data = await state.get_data()
        product_id = data.get("promo_product_id")
        tip_percent = data.get("promo_tip_percent")
        product = await session.get(Product, product_id) if product_id else None
        if not product or tip_percent not in TIP_PERCENTS:
            await state.clear()
            raise ShopError("missing")
        try:
            promo, _ = await valid_promo(session, message.text, product.price)
        except ShopError as error:
            await message.answer(tr(str(error), lang))
            return
        await state.clear()
        await show_checkout_options(
            message,
            session,
            user,
            lang,
            product_id,
            tip_percent,
            promo,
        )

    @router.callback_query(
        F.data.regexp(r"^buy_method:(mono|deepseek):\d+:(0|15|35|50|100)(?::(?:\d+|-))?$")
    )
    async def buy_method(callback, session, shop, user, lang):
        parts = callback.data.split(":")
        _, payment_choice, product_id, tip_percent = parts[:4]
        promo_id = parts[4] if len(parts) == 5 else "-"
        promo = await session.get(PromoCode, int(promo_id)) if promo_id != "-" else None
        if promo_id != "-" and not promo:
            raise ShopError("promo_invalid")
        async with shop.redis.lock(f"checkout:{user.id}", timeout=45, blocking_timeout=1):
            order = await shop.checkout(
                user.id,
                int(product_id),
                payment_choice,
                tip_percent=int(tip_percent),
                promo_code=promo.code if promo else None,
            )
        mono_card = await configured_manual_card(session, shop)
        cards = receipt_card_numbers(shop, order) if order.payment_method == "receipt" else [mono_card]
        payment_message = await render(
            callback,
            payment_wait_text(
                order,
                lang,
                card=(receipt_cards(shop, order) if order.payment_method == "receipt" else mono_card),
            ),
            payment_rows(order, lang, cards),
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
        instruction = (
            "📎 Надішліть скриншот успішної оплати саме як фото. На ньому мають бути "
            "чітко видні сума, останні 4 цифри картки отримувача, статус і час."
        )
        example_photo = await setting(session, "receipt_example_photo", "")
        if example_photo:
            await callback.message.answer_photo(
                example_photo,
                caption=f"✅ <b>Приклад вдалої квитанції</b>\n\n{instruction}",
                parse_mode="HTML",
            )
        else:
            await callback.message.answer(instruction)

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
            prepared = await asyncio.to_thread(prepare_receipt, destination.getvalue())
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
                payment_details = shop.vault.unpack(order.payment_cards_encrypted)
                cards = payment_details["cards"]
                allowed_ibans = set(payment_details.get("ibans", []))
            progress = await message.answer(RECEIPT_ANALYSIS_FRAMES[0])
            animation = asyncio.create_task(animate_receipt_analysis(progress))
            technical_failure = False
            try:
                analysis = await analyze_receipt(
                    shop.mono.client,
                    shop.cfg.deepseek_api_key.get_secret_value(),
                    shop.cfg.deepseek_vision_model,
                    prepared,
                    shop.cfg.deepseek_timeout,
                )
                approved, reason = evaluate_receipt(
                    analysis,
                    expected,
                    {c["last4"] for c in cards},
                    created,
                    allowed_ibans,
                )
            except ReceiptError as error:
                log.warning("deepseek_receipt_invalid order=%s reason=%s", order_id, error)
                technical_failure = True
                analysis, approved, reason = None, False, str(error)
            except Exception as error:
                log.warning(
                    "deepseek_receipt_failed order=%s error=%s detail=%s",
                    order_id,
                    type(error).__name__,
                    str(error)[:200],
                )
                technical_failure = True
                analysis, approved, reason = (
                    None,
                    False,
                    "Сервіс DeepSeek тимчасово недоступний. Потрібна ручна перевірка скриншота",
                )
            finally:
                animation.cancel()
                with suppress(asyncio.CancelledError):
                    await animation
            if technical_failure:
                async with shop.sessions() as receipt_session, receipt_session.begin():
                    receipt = await receipt_session.get(
                        PaymentReceipt, receipt_id, with_for_update=True
                    )
                    order = await receipt_session.get(Order, order_id)
                    if receipt and order:
                        receipt.status = "retry_allowed_review"
                        receipt.reason = reason
                await state.clear()
                await finish_receipt_analysis(
                    progress,
                    "⚠️ Аналіз не завершився — скрін передано адміністратору.",
                )
                await send_receipt_for_admin_review(message.bot, shop, receipt, order, user, reason)
                await message.answer(
                    f"⚠️ Не вдалося отримати відповідь від DeepSeek.\n\nПричина: {reason}\n\n"
                    "Скрін передано адміністратору на перевірку. У вас ще є одна спроба — надішліть квитанцію ще раз.",
                    reply_markup=keyboard(
                        [[("🔄 Спробувати ще раз", f"receipt:{order_id}")]]
                    ),
                )
                return
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
                info_notifications_enabled = (
                    await setting(receipt_session, "admin_info_notifications", "true") == "true"
                )
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
                    f"🤖 DeepSeek підтвердив оплату\n{escape(order.product_name_snapshot)}\n"
                    f"💰 До сплати: <b>{money(expected)}</b>\n"
                    f"👤 {user_reference(user)}\n"
                    + (
                        f"IBAN: {escape(str(analysis.get('recipient_iban'))[:4])}••••{escape(str(analysis.get('recipient_iban'))[-6:])}\n"
                        if analysis.get("recipient_iban") and not analysis.get("recipient_card_last4")
                        else f"Картка: •••• {escape(str(analysis.get('recipient_card_last4') or '—'))}\n"
                    )
                    + f"Час: {escape(str(analysis.get('payment_datetime') or '—'))}\nЗаявка #{receipt_id}"
                )
                if info_notifications_enabled:
                    for admin_id in await all_admin_ids(receipt_session, shop.cfg):
                        try:
                            await message.bot.send_photo(
                                admin_id,
                                photo.file_id,
                                caption=approved_caption,
                                parse_mode="HTML",
                            )
                        except Exception:
                            log.warning(
                                "receipt_admin_notify_failed admin=%s receipt=%s",
                                admin_id,
                                receipt_id,
                            )
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
            card = await configured_manual_card(check_session, shop)
        progress = await callback.message.answer(MONO_PAYMENT_FRAMES[0])
        if paid:
            result = "checked"
        else:
            await animate_mono_payment(progress)
            result = await reconcile_personal(shop, order_id)
        async with shop.sessions() as check_session:
            order = await check_session.get(Order, order_id)
            if order.status in SUCCESS:
                await progress.edit_text(
                    "✅ Оплату підтверджено. Покупка доступна нижче; бот також надішле дані в чат.",
                    reply_markup=keyboard(
                        [
                            [("📦 Відкрити покупку", f"purchase:{order.id}")],
                            back(lang, f"purchase:{order.id}"),
                        ]
                    ),
                    parse_mode="HTML",
                )
                return
        messages = {
            "cooldown": "⏳ Повторна перевірка доступна через 30 секунд.",
            "error": "Не вдалося отримати виписку Monobank. Спробуйте ще раз через 20 секунд.",
        }
        await progress.edit_text(
            messages.get(
                result,
                "Платіж на цю суму поки не знайдено. Повторіть перевірку через 20 секунд. Не сплачуйте повторно.",
            ),
            reply_markup=keyboard(payment_rows(order, lang, [card])),
            parse_mode="HTML",
        )

    @router.callback_query(F.data.regexp(r"^purchase:[a-f0-9]{32}(?::\d+)?$"))
    async def purchase(callback, session, shop, user, lang):
        _, order_id, *source = callback.data.split(":")
        return_target = f"purchases:{source[0]}" if source else "home"
        order = await session.get(Order, order_id)
        if not order or order.user_id != user.id or order.status not in SUCCESS:
            raise ShopError("missing")
        if is_admin(shop.cfg, user.id, user):
            raw_hidden_before = await setting(
                session, f"admin_purchases_reset_at:{user.id}", ""
            )
            if raw_hidden_before:
                try:
                    if aware(order.created_at) < aware(datetime.fromisoformat(raw_hidden_before)):
                        raise ShopError("missing")
                except ValueError:
                    pass
        product = await session.get(Product, order.product_id)
        reward_promo = await session.scalar(
            select(PromoCode).where(
                PromoCode.generated_for_order_id == order.id,
                PromoCode.expires_at > now(),
            )
        )
        support = await setting(session, "support_username", shop.cfg.support_username)
        if not product:
            text = (
                f"✅ <b>Оплата успішна!</b>\n\n🎮 <b>{escape(order.product_name_snapshot)}</b>\n"
                "⚠️ Дані товару потребують ручної перевірки. Зверніться до підтримки."
                if lang == "ua"
                else f"✅ <b>Оплата успешна!</b>\n\n🎮 <b>{escape(order.product_name_snapshot)}</b>\n"
                "⚠️ Данные товара требуют ручной проверки. Обратитесь в поддержку."
            )
            await render(
                callback,
                text,
                [[(tr("support", lang), "https://t.me/" + support.lstrip("@"))], back(lang, return_target)],
            )
            return
        if order.delivery_mode_snapshot == "manual":
            await render(
                callback,
                manual_delivery_text(order, lang, reward_promo),
                [
                    [(tr("support", lang), "https://t.me/" + support.lstrip("@"))],
                    back(lang, return_target),
                ],
            )
            return
        gmail_connected = bool(
            product.gmail_credentials_encrypted and code_request_window_open(order)
        )
        found_codes = await session.scalar(
            select(func.count())
            .select_from(MailCodeRequest)
            .where(MailCodeRequest.order_id == order.id, MailCodeRequest.outcome == "found")
        )
        remaining_codes = max(0, product.code_limit - found_codes)
        await render(
            callback,
            purchase_text(order, product, lang, shop.vault, reward_promo),
            purchase_rows(
                order,
                lang,
                support,
                gmail_connected,
                remaining_codes,
                code_request_available=remaining_codes > 0,
                return_target=return_target,
            ),
        )

    @router.callback_query(F.data.regexp(r"^code:[a-f0-9]{32}(?::\d+)?$"))
    async def code(callback, shop, user, lang):
        _, order_id, *source = callback.data.split(":")
        purchase_target = f"purchase:{order_id}:{source[0]}" if source else f"purchase:{order_id}"
        try:
            code, reused = await shop.code(user.id, order_id)
        except ShopError as error:
            if str(error) not in {"no_code", "cooldown", "code_limit", "error"}:
                raise
            await render(callback, tr(str(error), lang), [back(lang, purchase_target)])
            return
        await callback.message.bot.send_message(
            chat_id=callback.message.chat.id,
            text=(
                f"{tr('code_result', lang)}:\n<code>{code}</code>"
                + (f"\n\n{tr('code_reused', lang)}" if reused else "")
                + f"\n\nℹ️ {tr('code_login_retry', lang)}"
            ),
            reply_markup=keyboard([[("📋 Копіювати код" if lang == "ua" else "📋 Копировать код", "copy:" + code)]]),
            parse_mode="HTML",
            protect_content=True,
        )
        await render(
            callback,
            "✅ Код надіслано окремим повідомленням і він залишиться в чаті."
            if lang == "ua"
            else "✅ Код отправлен отдельным сообщением и останется в чате.",
            [back(lang, purchase_target)],
        )

    @router.message(ReviewForm.text)
    async def save_review(message, state, session, user, lang):
        if await setting(session, "reviews_enabled", "true") != "true":
            await state.clear()
            await show_account(message, session, user, lang)
            return
        review_text = (message.text or "").strip()
        if len(review_text) < 3:
            await message.answer(
                "Напишіть відгук довжиною щонайменше 3 символи."
                if lang == "ua"
                else "Напишите отзыв длиной не менее 3 символов."
            )
            return
        if len(review_text) > 1500:
            await message.answer(
                "Відгук задовгий. Максимум 1500 символів."
                if lang == "ua"
                else "Отзыв слишком длинный. Максимум 1500 символов."
            )
            return
        if not await review_slots(session, user.id):
            await state.clear()
            await render(
                message,
                "Ви вже використали всі відгуки за свої покупки."
                if lang == "ua"
                else "Вы уже использовали все отзывы за свои покупки.",
                [[(tr("back_to_reviews", lang), "account:reviews:0")]],
            )
            return
        session.add(Review(user_id=user.id, text=review_text))
        await session.commit()
        await state.clear()
        await render(
            message,
            tr("review_saved", lang),
            [[(tr("back_to_reviews", lang), "account:reviews:0")]],
        )

    @router.message()
    async def fallback(message, session, lang):
        await show_home(message, session, lang)

    dp.include_router(router)
    return dp
