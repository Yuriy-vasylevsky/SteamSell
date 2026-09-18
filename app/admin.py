import asyncio
import logging
import os
import re
import secrets
import tempfile
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from html import escape
from pathlib import Path
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.filters import Command, Filter
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import FSInputFile
from sqlalchemy import delete as sa_delete
from sqlalchemy import func, or_, select

from app.access import is_admin, is_primary_admin
from app.i18n import money, tr
from app.models import (
    Broadcast,
    LoyaltyLevel,
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
from app.services import (
    SUCCESS,
    audit,
    aware,
    configured_manual_card,
    delete_promo_code,
    loyalty_status,
    release_stock,
    set_setting,
    setting,
)
from app.ui import (
    admin_menu,
    discounted_price_text,
    keyboard,
    pagination,
    persistent_menu,
    product_text,
    render,
    user_reference,
)

log = logging.getLogger(__name__)


class AdminOnly(Filter):
    async def __call__(self, event, shop, user=None):
        return is_admin(shop.cfg, event.from_user.id, user)


def pg_dump_url(database_url):
    """Convert SQLAlchemy's async PostgreSQL URL to a pg_dump-compatible URL."""
    return database_url.replace("postgresql+asyncpg://", "postgresql://", 1)


class Form(StatesGroup):
    product = State()
    edit = State()
    setting = State()
    broadcast = State()
    card_label = State()
    card_number = State()
    iban = State()
    loyalty = State()
    promo = State()
    product_search = State()
    user_search = State()
    order_request_photo = State()
    receipt_example_photo = State()


FIELDS = [
    "name_ua",
    "description_ua",
    "image_file_id",
    "price",
    "code_limit",
    "stock_quantity",
    "delivery_mode",
    "activation_type",
    "reward_promo_enabled",
    "steam_login_encrypted",
    "steam_password_encrypted",
    "gmail_credentials_encrypted",
    "featured",
    "on_home",
]
LABELS = {
    "name_ua": "Назва товару",
    "price": "Ціна у грн",
    "code_limit": "Кількість кодів",
    "stock_quantity": "Кількість",
    "image_file_id": "Фото",
    "description_ua": "Опис",
    "delivery_mode": "Спосіб видачі товару",
    "activation_type": "Тип активації",
    "reward_promo_enabled": "Додати промокод?",
    "steam_login_encrypted": "Steam Login",
    "steam_password_encrypted": "Steam Password",
    "gmail_credentials_encrypted": "Gmail OAuth",
    "featured": "Додати до новинок?",
    "on_home": "Показувати на головній?",
}
OPTIONAL = {"image_file_id", "description_ua", "gmail_credentials_encrypted"}
BACK = [("⬅️ Адмінка", "a:home")]
GENERAL_BACK = [("⬅️ Загальні налаштування", "a:general_settings")]
SETTINGS = {
    "support_username": "Telegram підтримки",
    "page_size": "Товарів на сторінці",
    "welcome_ua": "Привітання UA",
    "welcome_ru": "Привітання RU",
    "info_ua": "Інформація UA",
    "info_ru": "Інформація RU",
    "activation_guide_ua": "Як активувати гру UA",
    "activation_guide_ru": "Як активувати гру RU",
    "alternative_activation_guide_ua": "Альтернативна інструкція UA",
    "alternative_activation_guide_ru": "Альтернативна інструкція RU",
    "faq_ua": "Часті запитання UA",
    "faq_ru": "Часті запитання RU",
    "order_request_title_ua": "Назва картки замовлення UA",
    "order_request_title_ru": "Назва картки замовлення RU",
    "order_request_description_ua": "Опис картки замовлення UA",
    "order_request_description_ru": "Опис картки замовлення RU",
    "mono_token": "Токен Monobank",
}


def valid_ukrainian_iban(value: str) -> bool:
    if not re.fullmatch(r"UA\d{27}", value):
        return False
    rearranged = value[4:] + "3010" + value[2:4]
    return int(rearranged) % 97 == 1


def parse_field(field, message, shop):
    text = (message.text or "").strip()
    if field == "image_file_id":
        if not message.photo:
            raise ValueError("Надішліть фото або натисніть «Пропустити».")
        return message.photo[-1].file_id
    if not text:
        raise ValueError("Введіть текст.")
    if field == "price":
        try:
            amount = Decimal(text.replace(",", "."))
            if (
                not amount.is_finite()
                or amount <= 0
                or amount > 1000000
                or amount * 100 != (amount * 100).to_integral_value()
            ):
                raise ValueError()
            return int(amount * 100)
        except (InvalidOperation, ValueError):
            raise ValueError("Введіть додатну ціну до 1 000 000 ₴, максимум 2 знаки після коми.") from None
    if field == "code_limit":
        if not text.isdigit() or not 0 <= int(text) <= 5:
            raise ValueError("Введіть ціле число від 0 до 5.")
        return int(text)
    if field == "stock_quantity":
        if not text.isdigit() or int(text) > 100000:
            raise ValueError("Введіть ціле число від 0 до 100 000.")
        return int(text)
    limit = 150 if field.startswith("name") else 500 if field.startswith("description") else 256
    if len(text) > limit:
        raise ValueError(f"Максимум {limit} символів.")
    return shop.vault.encrypt(text) if field.endswith("_encrypted") else text


async def prompt(event, state):
    data = await state.get_data()
    field = data["field"]
    rows = []
    if field in OPTIONAL:
        rows.append([("Пропустити / очистити", "a:skip")])
    if field in ("featured", "on_home", "reward_promo_enabled"):
        rows.append([("✅ Так", "a:feature:1"), ("❌ Ні", "a:feature:0")])
    if field == "stock_quantity":
        rows.append([("♾ Необмежено", "a:stock:unlimited"), ("📦 Вказати кількість", "a:stock:limited")])
    if field == "delivery_mode":
        rows.append([("🤖 Автовидача ботом", "a:delivery:auto")])
        rows.append([("👤 Ручна видача адміністратором", "a:delivery:manual")])
    if field == "activation_type":
        rows.append([("Звичайна", "a:activation_type:standard")])
        rows.append([("Альтернативна", "a:activation_type:alternative")])
    if field == "gmail_credentials_encrypted":
        rows += [
            [("📧 Підключити Gmail", "a:oauth")],
            [("✅ Перевірити підключення", "a:gmail_done")],
        ]
    rows.append([("❌ Скасувати", "a:home")])
    await render(event, LABELS[field], rows)


async def preview(event, state, shop):
    data = await state.get_data()
    p = Product(**data["draft"])
    delivery_text = (
        "👤 Видача: вручну адміністратором. Steam-дані та Gmail не потрібні."
        if p.delivery_mode == "manual"
        else "🤖 Видача: автоматично ботом.\n\nSteam-дані: збережено без показу.\nGmail: "
        + ("підключено." if p.gmail_credentials_encrypted else "не підключено.")
    )
    await render(
        event,
        product_text(p, "ua")
        + f"\n\n🔑 Доступно кодів на покупку: <b>{p.code_limit}</b>"
        + "\n🎟 Тип активації: <b>"
        + ("альтернативна" if p.activation_type == "alternative" else "звичайна")
        + "</b>"
        + "\n🎁 Подарунковий промокод: <b>"
        + ("так" if p.reward_promo_enabled else "ні")
        + "</b>"
        + f"\n\n{delivery_text}",
        [[("✅ Зберегти", "a:save"), ("✏️ Редагувати", "a:review")], [("❌ Скасувати", "a:home")]],
        photo=p.image_file_id,
    )


async def accept(event, state, shop, value):
    data = await state.get_data()
    draft = data.get("draft", {})
    draft[data["field"]] = value
    if data["field"] == "delivery_mode" and value == "manual":
        for secret_field in ("steam_login_encrypted", "steam_password_encrypted", "gmail_credentials_encrypted"):
            draft[secret_field] = None
    # The storefront keeps both DB columns for compatibility, while the admin
    # enters one shared title and description for both interface languages.
    if data["field"] == "name_ua":
        draft["name_ru"] = value
    elif data["field"] == "description_ua":
        draft["description_ru"] = value
    await state.update_data(draft=draft)
    if data.get("edit_id") or data.get("reviewing"):
        if data.get("reviewing"):
            await preview(event, state, shop)
        else:
            await render(
                event,
                "Підтвердити зміну поля «" + LABELS[data["field"]] + "»?",
                [[("✅ Зберегти", "a:save_edit")], [("❌ Скасувати", "a:home")]],
            )
        return
    index = FIELDS.index(data["field"]) + 1
    while (
        draft.get("delivery_mode") == "manual"
        and index < len(FIELDS)
        and FIELDS[index] in {"steam_login_encrypted", "steam_password_encrypted", "gmail_credentials_encrypted"}
    ):
        index += 1
    if index == len(FIELDS):
        await preview(event, state, shop)
    else:
        await state.update_data(field=FIELDS[index])
        await prompt(event, state)


def admin_router():
    router = Router(name="admin")
    router.message.filter(AdminOnly())
    router.callback_query.filter(AdminOnly())

    @router.message(Command("admin", "cancel"))
    @router.message(F.text == "⚙️ Адмін-панель")
    @router.callback_query(F.data == "a:home")
    async def home(event, state, shop):
        await state.clear()
        await render(event, "Керування магазином", [])
        message = event.message if hasattr(event, "message") else event
        await message.answer("\u2063", reply_markup=admin_menu())

    @router.message(F.text == "⬅️ Вийти з адмінки")
    async def exit_admin(message, session, user, shop, state):
        await state.clear()
        loyalty = await loyalty_status(session, user.id)
        await message.answer(
            "Ви вийшли з адмінки.",
            reply_markup=persistent_menu(
                user.language or "ua",
                True,
                user.broadcast_subscribed,
                loyalty["enabled"],
            ),
        )

    @router.callback_query(F.data == "a:general_settings")
    @router.message(F.text == "⚙️ Загальні налаштування")
    async def general_settings(event, session):
        sale_enabled = await setting(session, "sale_enabled", "false") == "true"
        await render(
            event,
            "⚙️ <b>Загальні налаштування</b>",
            [
                [("👥 Користувачі", "a:users:0"), ("📊 Статистика", "a:stats")],
                [("💳 Режим оплати", "a:payment_mode"), ("💳 Керування картками", "a:cards")],
                [("🎁 Програма лояльності", "a:loyalty")],
                [("🎟 Промокоди", "a:promos:0")],
                [
                    (
                        "🔥 Розпродаж −50%: ВКЛ" if sale_enabled else "🏷 Розпродаж −50%: ВИКЛ",
                        "a:sale",
                    )
                ],
                [
                    ("🧾 Історія замовлень", "a:recent_orders:today:0"),
                    ("⚙️ Налаштування", "a:settings"),
                ],
                BACK,
            ],
        )

    @router.callback_query(F.data.regexp(r"^a:recent_orders:(?:(?:today|yesterday):)?\d+$"))
    async def recent_orders(callback, session, shop):
        page_size = 7
        parts = callback.data.split(":")
        day = parts[2] if len(parts) == 4 else "today"
        page = int(parts[-1])
        timezone = ZoneInfo(getattr(shop.cfg, "timezone", "Europe/Kyiv"))
        today = now().astimezone(timezone).date()
        selected_date = today if day == "today" else today - timedelta(days=1)
        period_start = datetime.combine(selected_date, datetime.min.time(), timezone)
        period_end = period_start + timedelta(days=1)

        query = (
            select(Order, User)
            .join(User, User.id == Order.user_id)
            .where(Order.created_at >= period_start, Order.created_at < period_end)
        )
        total = await session.scalar(select(func.count()).select_from(query.subquery()))
        page = min(page, max(0, (total - 1) // page_size))
        items = (
            await session.execute(
                query.order_by(Order.created_at.desc(), Order.id.desc())
                .offset(page * page_size)
                .limit(page_size)
            )
        ).all()

        lines = [
            "🧾 <b>Історія замовлень</b>",
            f"📅 <b>{'Сьогодні' if day == 'today' else 'Вчора'}</b> · {selected_date.strftime('%d.%m')}",
        ]
        if items:
            for index, (order, user) in enumerate(items, start=page * page_size + 1):
                created = aware(order.created_at).astimezone(timezone)
                lines += [
                    "",
                    f"<b>{index}. {escape(order.product_name_snapshot)}</b> <i>· {created.strftime('%H:%M')}</i>",
                    f"👤 {user_reference(user)}  ·  💰 <b>{money(order.price_snapshot)}</b>",
                ]
        else:
            lines += ["", "Замовлень за цей період ще немає."]

        await render(
            callback,
            "\n".join(lines),
            [
                pagination(f"a:recent_orders:{day}", page, total, page_size),
                [
                    ("🟢 Сьогодні" if day == "today" else "Сьогодні", "a:recent_orders:today:0"),
                    ("🟢 Вчора" if day == "yesterday" else "Вчора", "a:recent_orders:yesterday:0"),
                ],
                GENERAL_BACK,
            ],
        )

    @router.callback_query(F.data == "a:admin_notifications_toggle")
    async def admin_notifications_toggle(callback, session):
        enabled = await setting(session, "admin_info_notifications", "true") == "true"
        new_enabled = not enabled
        await set_setting(
            session,
            "admin_info_notifications",
            "true" if new_enabled else "false",
        )
        audit(session, callback.from_user.id, "admin_notifications_toggle", new_enabled)
        await session.commit()
        await settings(callback, session)

    @router.callback_query(F.data == "a:order_request_settings")
    async def order_request_settings(callback, state, session):
        await state.clear()
        enabled = await setting(session, "order_request_enabled", "true") == "true"
        photo = await setting(session, "order_request_photo", "")
        await render(
            callback,
            "📌 <b>Картка «Замовити товар»</b>\n\n"
            f"Статус: <b>{'увімкнено' if enabled else 'вимкнено'}</b>\n"
            f"Фото: <b>{'додано' if photo else 'не додано'}</b>",
            [
                [
                    (
                        "🔴 Вимкнути" if enabled else "🟢 Увімкнути",
                        "a:order_request_toggle",
                    )
                ],
                [
                    ("🇺🇦 Назва", "a:setting:order_request_title_ua"),
                    ("🇺🇦 Опис", "a:setting:order_request_description_ua"),
                ],
                [
                    ("🇷🇺 Назва", "a:setting:order_request_title_ru"),
                    ("🇷🇺 Опис", "a:setting:order_request_description_ru"),
                ],
                [("🖼 Додати / змінити фото", "a:order_request_photo")],
                *([[("🗑 Прибрати фото", "a:order_request_photo_clear")]] if photo else []),
                GENERAL_BACK,
            ],
        )

    @router.callback_query(F.data == "a:order_request_toggle")
    async def order_request_toggle(callback, state, session):
        enabled = await setting(session, "order_request_enabled", "true") == "true"
        await set_setting(session, "order_request_enabled", "false" if enabled else "true")
        audit(session, callback.from_user.id, "order_request_toggle", not enabled)
        await session.commit()
        await order_request_settings(callback, state, session)

    @router.callback_query(F.data == "a:order_request_photo")
    async def order_request_photo_start(callback, state):
        await state.set_state(Form.order_request_photo)
        await render(
            callback,
            "🖼 Надішліть фото для картки «Замовити товар».",
            [[("❌ Скасувати", "a:order_request_settings")], GENERAL_BACK],
        )

    @router.message(Form.order_request_photo)
    async def order_request_photo_save(message, state, session):
        if not message.photo:
            await message.answer("Надішліть зображення саме як фото.")
            return
        await set_setting(session, "order_request_photo", message.photo[-1].file_id)
        audit(session, message.from_user.id, "order_request_photo", "updated")
        await session.commit()
        await state.clear()
        await render(
            message,
            "✅ Фото картки збережено.",
            [[("⬅️ До картки", "a:order_request_settings")], GENERAL_BACK],
        )

    @router.callback_query(F.data == "a:order_request_photo_clear")
    async def order_request_photo_clear(callback, state, session):
        await set_setting(session, "order_request_photo", "")
        audit(session, callback.from_user.id, "order_request_photo", "cleared")
        await session.commit()
        await state.clear()
        await order_request_settings(callback, state, session)

    @router.callback_query(F.data.regexp(r"^a:promos:\d+$"))
    async def promos(callback, state, session):
        await state.clear()
        page = min(int(callback.data.rsplit(":", 1)[1]), 100000)
        query = select(PromoCode).where(
            PromoCode.created_by == callback.from_user.id,
            PromoCode.generated_for_order_id.is_(None),
        )
        total = await session.scalar(select(func.count()).select_from(query.subquery()))
        page = min(page, max(0, (total - 1) // 7))
        items = (
            await session.scalars(
                query.order_by(PromoCode.created_at.desc()).offset(page * 7).limit(7)
            )
        ).all()
        rows = []
        for promo in items:
            used = await session.scalar(
                select(func.count()).select_from(Order).where(
                    Order.promo_code_id == promo.id,
                    Order.status.notin_(("payment_failed", "cancelled")),
                )
            )
            remaining = max(0, promo.usage_limit - used)
            status = "⏳" if aware(promo.expires_at) > now() and remaining else "⛔"
            rows.append(
                [
                    (
                        f"{status} {promo.code} · −{promo.discount_percent}% · залишилось {remaining}",
                        f"a:promo:{promo.id}",
                    )
                ]
            )
        rows.extend(
            [
                [("➕ Новий промокод", "a:promo_new")],
                [("🎁 Промокоди з покупок", "a:reward_promos:0")],
                pagination("a:promos", page, total, 7),
                GENERAL_BACK,
            ]
        )
        await render(
            callback,
            "🎟 <b>Мої промокоди</b>\n\n"
            + (f"Усього: <b>{total}</b>" if items else "Промокодів ще немає."),
            rows,
        )

    @router.callback_query(F.data.regexp(r"^a:reward_promos:\d+$"))
    async def reward_promos(callback, state, session):
        await state.clear()
        page = min(int(callback.data.rsplit(":", 1)[1]), 100000)
        used_reward_promo = select(Order.id).where(
            Order.promo_code_id == PromoCode.id,
            Order.status.notin_(("payment_failed", "cancelled")),
        ).exists()
        query = select(PromoCode).where(
            PromoCode.generated_for_order_id.is_not(None),
            PromoCode.expires_at > now(),
            ~used_reward_promo,
        )
        total = await session.scalar(select(func.count()).select_from(query.subquery()))
        page = min(page, max(0, (total - 1) // 7))
        items = (
            await session.scalars(
                query.order_by(PromoCode.created_at.desc()).offset(page * 7).limit(7)
            )
        ).all()
        rows = []
        for promo in items:
            used = await session.scalar(
                select(func.count()).select_from(Order).where(
                    Order.promo_code_id == promo.id,
                    Order.status.notin_(("payment_failed", "cancelled")),
                )
            )
            status = "⏳" if aware(promo.expires_at) > now() and not used else "⛔"
            rows.append(
                [(f"{status} {promo.code} · покупка {promo.generated_for_order_id[:8]}", f"a:reward_promo:{promo.id}")]
            )
        rows.extend(
            [pagination("a:reward_promos", page, total, 7), [("⬅️ До промокодів", "a:promos:0")], GENERAL_BACK]
        )
        await render(
            callback,
            "🎁 <b>Промокоди з покупок</b>\n\n"
            + (f"Усього: <b>{total}</b>" if items else "Промокодів ще немає."),
            rows,
        )

    @router.callback_query(F.data.regexp(r"^a:reward_promo:\d+$"))
    async def reward_promo_detail(callback, session, shop):
        promo = await session.get(PromoCode, int(callback.data.rsplit(":", 1)[1]))
        if (
            not promo
            or not promo.generated_for_order_id
            or aware(promo.expires_at) <= now()
        ):
            return
        order = await session.get(Order, promo.generated_for_order_id)
        used = await session.scalar(
            select(func.count()).select_from(Order).where(
                Order.promo_code_id == promo.id,
                Order.status.notin_(("payment_failed", "cancelled")),
            )
        )
        if used:
            await render(
                callback,
                "Цей промокод уже використано.",
                [[("⬅️ До промокодів з покупок", "a:reward_promos:0")], GENERAL_BACK],
            )
            return
        expires = aware(promo.expires_at).astimezone(ZoneInfo(shop.cfg.timezone))
        await render(
            callback,
            f"🎁 <b>{escape(promo.code)}</b>\n\n"
            f"Покупка: <code>{escape(promo.generated_for_order_id)}</code>\n"
            f"Товар: <b>{escape(order.product_name_snapshot if order else '—')}</b>\n"
            f"Покупець: <code>{promo.created_by}</code>\n"
            f"Знижка: <b>{promo.discount_percent}%</b>\n"
            f"Використання: <b>{used}/{promo.usage_limit}</b>\n"
            f"Максимальна ціна товару: <b>{money(promo.max_product_price)}</b>\n"
            f"Активний до: <b>{expires:%d.%m.%Y · %H:%M}</b>",
            [[("⬅️ До промокодів з покупок", "a:reward_promos:0")], GENERAL_BACK],
        )

    @router.callback_query(F.data.regexp(r"^a:promo:\d+$"))
    async def promo_detail(callback, session, shop):
        promo = await session.get(PromoCode, int(callback.data.rsplit(":", 1)[1]))
        if (
            not promo
            or promo.created_by != callback.from_user.id
            or promo.generated_for_order_id is not None
        ):
            return
        used = await session.scalar(
            select(func.count()).select_from(Order).where(
                Order.promo_code_id == promo.id,
                Order.status.notin_(("payment_failed", "cancelled")),
            )
        )
        expires = aware(promo.expires_at).astimezone(ZoneInfo(shop.cfg.timezone))
        await render(
            callback,
            f"🎟 <b>{escape(promo.code)}</b>\n\n"
            f"Знижка: <b>{promo.discount_percent}%</b>\n"
            f"Використання: <b>{used}/{promo.usage_limit}</b>\n"
            f"Залишилось: <b>{max(0, promo.usage_limit - used)}</b>\n"
            f"Максимальна ціна товару: <b>{money(promo.max_product_price)}</b>\n"
            f"Активний до: <b>{expires:%d.%m.%Y · %H:%M}</b>",
            [
                [("🗑 Видалити промокод", f"a:promo_delete:{promo.id}")],
                [("⬅️ До промокодів", "a:promos:0")],
                GENERAL_BACK,
            ],
        )

    @router.callback_query(F.data.regexp(r"^a:promo_delete:\d+$"))
    async def promo_delete_prompt(callback, session):
        promo = await session.get(PromoCode, int(callback.data.rsplit(":", 1)[1]))
        if (
            not promo
            or promo.created_by != callback.from_user.id
            or promo.generated_for_order_id is not None
        ):
            return
        await render(
            callback,
            f"🗑 <b>Видалити промокод?</b>\n\n<code>{escape(promo.code)}</code> більше не можна буде використати. "
            "Історія вже оформлених замовлень залишиться без змін.",
            [
                [("✅ Так, видалити", f"a:promo_delete_confirm:{promo.id}")],
                [("❌ Скасувати", f"a:promo:{promo.id}")],
            ],
        )

    @router.callback_query(F.data.regexp(r"^a:promo_delete_confirm:\d+$"))
    async def promo_delete_confirm(callback, session):
        promo_id = int(callback.data.rsplit(":", 1)[1])
        promo = await session.get(PromoCode, promo_id)
        if (
            not promo
            or promo.created_by != callback.from_user.id
            or promo.generated_for_order_id is not None
        ):
            return
        code = promo.code
        await delete_promo_code(session, promo_id)
        audit(session, callback.from_user.id, "promo_delete", code)
        await session.commit()
        await render(
            callback,
            f"✅ Промокод <code>{escape(code)}</code> видалено.",
            [[("🎟 До промокодів", "a:promos:0")], GENERAL_BACK],
        )

    @router.callback_query(F.data == "a:promo_new")
    async def promo_new(callback, state):
        await state.set_state(Form.promo)
        await state.set_data({"promo_step": "discount"})
        await render(
            callback,
            "Введіть відсоток знижки від 1 до 100:",
            [[("❌ Скасувати", "a:promos:0")]],
        )

    @router.message(Form.promo)
    async def promo_input(message, state):
        data = await state.get_data()
        step = data.get("promo_step")
        value = (message.text or "").strip()
        error = None
        if step == "discount":
            if not value.isdigit() or not 1 <= int(value) <= 100:
                error = "Введіть ціле число від 1 до 100."
            else:
                await state.update_data(promo_discount=int(value), promo_step="uses")
                await message.answer("Введіть загальну кількість використань:")
        elif step == "uses":
            if not value.isdigit() or not 1 <= int(value) <= 100000:
                error = "Введіть ціле число від 1 до 100 000."
            else:
                await state.update_data(promo_uses=int(value), promo_step="max_price")
                await message.answer("Введіть максимальну ціну товару в гривнях:")
        elif step == "max_price":
            try:
                amount = Decimal(value.replace(",", "."))
                if (
                    not amount.is_finite()
                    or amount <= 0
                    or amount > 1000000
                    or amount * 100 != (amount * 100).to_integral_value()
                ):
                    raise ValueError()
            except (InvalidOperation, ValueError):
                error = "Введіть суму до 1 000 000 ₴, максимум 2 знаки після коми."
            else:
                await state.update_data(promo_max_price=int(amount * 100), promo_step="code")
                await message.answer("Введіть назву промокоду: 3–32 латинські літери, цифри, _ або -")
        elif step == "code":
            code = value.upper()
            if not re.fullmatch(r"[A-Z0-9_-]{3,32}", code):
                error = "Назва: 3–32 латинські літери, цифри, _ або -."
            else:
                await state.update_data(promo_code=code, promo_step="hours")
                await message.answer("Введіть, скільки годин промокод буде активний:")
        elif step == "hours":
            if not value.isdigit() or not 1 <= int(value) <= 8760:
                error = "Введіть ціле число годин від 1 до 8760."
            else:
                await state.update_data(promo_hours=int(value), promo_step="ready")
                data = await state.get_data()
                await message.answer(
                    "🎟 <b>Новий промокод</b>\n\n"
                    f"Назва: <code>{escape(data['promo_code'])}</code>\n"
                    f"Знижка: <b>{data['promo_discount']}%</b>\n"
                    f"Використань: <b>{data['promo_uses']}</b>\n"
                    f"Максимальна ціна: <b>{money(data['promo_max_price'])}</b>\n"
                    f"Термін: <b>{data['promo_hours']} год.</b>",
                    parse_mode="HTML",
                    reply_markup=keyboard(
                        [[("✅ Зберегти", "a:promo_save")], [("❌ Скасувати", "a:promos:0")]]
                    ),
                )
        if error:
            await message.answer(error)

    @router.callback_query(F.data == "a:promo_save")
    async def promo_save(callback, state, session):
        data = await state.get_data()
        required = ("promo_code", "promo_discount", "promo_uses", "promo_max_price", "promo_hours")
        if data.get("promo_step") != "ready" or any(key not in data for key in required):
            await render(callback, "Створення промокоду не завершено.", [GENERAL_BACK])
            return
        code = data["promo_code"]
        if await session.scalar(select(PromoCode.id).where(PromoCode.code == code)):
            await render(callback, "Промокод із такою назвою вже існує.", [[("⬅️ До промокодів", "a:promos:0")]])
            return
        promo = PromoCode(
            code=code,
            discount_percent=data["promo_discount"],
            usage_limit=data["promo_uses"],
            max_product_price=data["promo_max_price"],
            expires_at=now() + timedelta(hours=data["promo_hours"]),
            created_by=callback.from_user.id,
        )
        session.add(promo)
        await session.commit()
        audit(session, callback.from_user.id, "promo_create", code)
        await session.commit()
        await state.clear()
        await render(
            callback,
            f"✅ Промокод <code>{escape(code)}</code> створено.",
            [[("🎟 Мої промокоди", "a:promos:0")], GENERAL_BACK],
        )

    @router.callback_query(F.data == "a:sale")
    async def sale_settings(callback, session):
        enabled = await setting(session, "sale_enabled", "false") == "true"
        await render(
            callback,
            "🔥 <b>Розпродаж −50%</b>\n\n"
            f"Статус: {'🟢 увімкнено' if enabled else '🔴 вимкнено'}\n\n"
            "Під час розпродажу всі нові замовлення отримують знижку 50%, "
            "а програма лояльності тимчасово не застосовується. Після вимкнення "
            "акції лояльність автоматично відновиться.",
            [
                [
                    (
                        "🔴 Вимкнути розпродаж" if enabled else "🟢 Увімкнути розпродаж",
                        "a:sale_toggle",
                    )
                ],
                GENERAL_BACK,
            ],
        )

    @router.callback_query(F.data == "a:sale_toggle")
    async def sale_toggle(callback, session):
        enabled = await setting(session, "sale_enabled", "false") == "true"
        new_enabled = not enabled
        await set_setting(session, "sale_enabled", "true" if new_enabled else "false")
        session.add(
            Broadcast(
                payload={
                    "text": "",
                    "photo": None,
                    "entities": [],
                    "texts": (
                        {
                            "ua": (
                                "🔥 РОЗПРОДАЖ РОЗПОЧАВСЯ!\n\n"
                                "🎮 Знижка 50% на всі ігри!\n"
                                "✨ Обирайте гру та встигніть скористатися вигідною ціною."
                            ),
                            "ru": (
                                "🔥 РАСПРОДАЖА НАЧАЛАСЬ!\n\n"
                                "🎮 Скидка 50% на все игры!\n"
                                "✨ Выбирайте игру и успейте воспользоваться выгодной ценой."
                            ),
                        }
                        if new_enabled
                        else {
                            "ua": (
                                "🏁 РОЗПРОДАЖ ЗАВЕРШЕНО\n\n"
                                "🎮 Акційні ціни більше не діють.\n"
                                "💙 Дякуємо, що обираєте наш магазин!"
                            ),
                            "ru": (
                                "🏁 РАСПРОДАЖА ЗАВЕРШЕНА\n\n"
                                "🎮 Акционные цены больше не действуют.\n"
                                "💙 Спасибо, что выбираете наш магазин!"
                            ),
                        }
                    ),
                }
            )
        )
        audit(session, callback.from_user.id, "sale_toggle", new_enabled)
        await session.commit()
        await sale_settings(callback, session)

    @router.callback_query(F.data == "a:payment_mode")
    @router.message(F.text == "💳 Режим оплати")
    async def payment_mode(callback, session):
        mode = await setting(session, "payment_mode", "mono")
        cards = await session.scalar(
            select(func.count()).select_from(PaymentCard).where(PaymentCard.active.is_(True))
        )
        await render(
            callback,
            "Режим оплати для нових замовлень: "
            f"<b>{'Вибір покупця' if mode == 'hybrid' else 'DeepSeek — перевірка скріну' if mode == 'deepseek' else 'Monobank API'}</b>\n"
            f"Активних карток для DeepSeek: {cards}/2",
            [
                [("✅ Monobank API" if mode == "mono" else "Monobank API", "a:paymode:mono")],
                [("✅ DeepSeek-скрін" if mode == "deepseek" else "DeepSeek-скрін", "a:paymode:deepseek")],
                [("✅ Вибір покупця" if mode == "hybrid" else "Вибір покупця", "a:paymode:hybrid")],
                GENERAL_BACK,
            ],
        )

    @router.callback_query(F.data.regexp(r"^a:latest_code(?::\d+)?$"))
    @router.message(F.text == "🔑 Отримати код")
    async def latest_steam_code(callback, session, shop):
        products = (
            await session.scalars(
                select(Product)
                .where(
                    Product.deleted_at.is_(None),
                    Product.gmail_credentials_encrypted.is_not(None),
                )
                .order_by(Product.id)
            )
        ).all()
        mailboxes = {}
        for product in products:
            try:
                credentials = shop.vault.unpack(product.gmail_credentials_encrypted)
                email = credentials["email"]
                login = shop.vault.decrypt(product.steam_login_encrypted)
            except (KeyError, TypeError, ValueError):
                continue
            mailbox = mailboxes.setdefault(
                email,
                {"product_id": product.id, "credentials": credentials, "accounts": []},
            )
            # Prefer the newest OAuth connection when several products use the
            # same address, but keep all Steam accounts assigned to that mailbox.
            mailbox["product_id"] = product.id
            mailbox["credentials"] = credentials
            mailbox["accounts"].append((login, product.name_ua))
        if not mailboxes:
            await render(callback, "Немає товарів із підключеною Gmail-поштою.", [BACK])
            return

        selected_product_id = None
        data = getattr(callback, "data", None)
        if data and data.startswith("a:latest_code:"):
            selected_product_id = int(data.rsplit(":", 1)[1])
        selected_email, mailbox = next(
            (
                (email, item)
                for email, item in mailboxes.items()
                if item["product_id"] == selected_product_id
            ),
            (None, None),
        )
        if mailbox is None:
            await render(
                callback,
                "📧 <b>Оберіть пошту, з якої потрібно отримати код:</b>",
                [
                    [(f"📧 {email}", f"a:latest_code:{item['product_id']}")]
                    for email, item in mailboxes.items()
                ]
                + [BACK],
            )
            return

        retry_target = f"a:latest_code:{mailbox['product_id']}"
        allowed = await shop.redis.eval(
            "if redis.call('EXISTS',KEYS[1])==1 then return 0 end "
            "redis.call('SET',KEYS[1],'1','EX',ARGV[1]); return 1",
            1,
            f"code:admin:mailbox:{selected_email}",
            30,
        )
        if not allowed:
            await render(
                callback,
                "⏳ Зачекайте 30 секунд перед наступним пошуком коду.",
                [[("🔄 Оновити", retry_target)], BACK],
            )
            return
        try:
            result = await shop.gmail.latest_code_for_accounts(
                mailbox["credentials"],
                mailbox["accounts"],
                now() - timedelta(hours=1),
            )
        except Exception:
            await render(
                callback,
                "Не вдалося прочитати Gmail. Перевірте підключення пошти та спробуйте ще раз.",
                [BACK],
            )
            return
        if not result:
            await render(
                callback,
                "За останню годину відповідний код Steam Guard не знайдено.",
                [[("🔄 Перевірити ще раз", retry_target)], BACK],
            )
            return
        code, login, game, received_at = result
        code_text = (
            "🔑 <b>Останній код Steam Guard</b>\n\n"
            f"📧 {escape(selected_email)}\n"
            f"🎮 {escape(game)}\n"
            f"👤 {escape(login)}\n"
            f"🔐 <code>{escape(code)}</code>\n"
            f"🕒 {received_at.astimezone(ZoneInfo('Europe/Kyiv')):%d.%m.%Y · %H:%M}"
        )
        await callback.message.bot.send_message(
            chat_id=callback.message.chat.id,
            text=code_text,
            reply_markup=keyboard([[("📋 Копіювати код", "copy:" + code)]]),
            parse_mode="HTML",
            protect_content=True,
        )
        await render(
            callback,
            "✅ Код надіслано окремим повідомленням і він залишиться в чаті.",
            [[("🔄 Оновити", retry_target)], BACK],
        )

    @router.callback_query(F.data.regexp(r"^a:paymode:(mono|deepseek|hybrid)$"))
    async def payment_mode_set(callback, session):
        mode = callback.data.rsplit(":", 1)[1]
        if mode in {"deepseek", "hybrid"} and not await session.scalar(
            select(PaymentCard.id).where(PaymentCard.active.is_(True)).limit(1)
        ):
            await callback.message.answer("Спочатку додайте хоча б одну активну картку.")
            return
        await set_setting(session, "payment_mode", mode)
        audit(session, callback.from_user.id, "payment_mode", mode)
        await session.commit()
        await render(
            callback,
            "Режим оплати змінено. Він діятиме для нових замовлень.",
            [GENERAL_BACK],
        )

    @router.callback_query(F.data == "a:cards")
    @router.message(F.text == "💳 Керування картками")
    async def cards(callback, session, shop):
        items = (await session.scalars(select(PaymentCard).order_by(PaymentCard.id))).all()
        active = sum(card.active for card in items)
        mono_card = await configured_manual_card(session, shop)
        iban_encrypted = await setting(session, "receipt_iban", "")
        try:
            iban = shop.vault.decrypt(iban_encrypted) if iban_encrypted else ""
        except Exception:
            iban = ""
        rows = [[(f"🏦 Monobank •••• {mono_card[-4:] if mono_card else 'не задано'}", "a:mono_card")]]
        rows.append([(f"🏧 IBAN •••••• {iban[-6:] if iban else 'не задано'}", "a:iban")])
        rows += [
            [(f"{'🟢' if c.active else '⚫'} {c.label} •••• {c.last4}", f"a:card:{c.id}")] for c in items
        ]
        if active < 2:
            rows.append([("➕ Додати картку", "a:card_add")])
        rows.append(GENERAL_BACK)
        await render(
            callback,
            "💳 <b>Керування реквізитами</b>\n\n"
            "Картка Monobank використовується для оплати через API.\n"
            "Картки DeepSeek показуються покупцю.\n"
            "IBAN покупцю не показується — він лише перевіряється на квитанції.\n\n"
            f"Активних карток DeepSeek: <b>{active}/2</b>",
            rows,
        )

    @router.callback_query(F.data.regexp(r"^a:reviews:\d+$"))
    @router.message(F.text == "💬 Відгуки")
    async def reviews(callback, session, shop):
        total = await session.scalar(select(func.count()).select_from(Review))
        enabled = await setting(session, "reviews_enabled", "true") == "true"
        page = min(
            int(callback.data.rsplit(":", 1)[1])
            if hasattr(callback, "data") and callback.data.rsplit(":", 1)[1].isdigit()
            else 0,
            max(0, (total - 1) // 7),
        )
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
            created_at = aware(review.created_at).astimezone(ZoneInfo(shop.cfg.timezone))
            rows.append([(f"💬 {preview} · {created_at:%d.%m %H:%M}", f"a:review:{review.id}")])
        rows.extend(
            [
                [
                    (
                        "🔴 Вимкнути відгуки" if enabled else "🟢 Увімкнути відгуки",
                        "a:reviews_toggle",
                    )
                ],
                pagination("a:reviews", page, total, 7),
                BACK,
            ]
        )
        await render(
            callback,
            f"💬 <b>Анонімні відгуки</b>\n\nСтатус: <b>{'увімкнено' if enabled else 'вимкнено'}</b>\n"
            f"Всього: <b>{total}</b>"
            if items
            else "💬 <b>Анонімні відгуки</b>\n\n"
            f"Статус: <b>{'увімкнено' if enabled else 'вимкнено'}</b>\nПоки що відгуків немає.",
            rows,
        )

    @router.callback_query(F.data == "a:reviews_toggle")
    async def reviews_toggle(callback, session, shop):
        enabled = await setting(session, "reviews_enabled", "true") == "true"
        await set_setting(session, "reviews_enabled", "false" if enabled else "true")
        audit(session, callback.from_user.id, "reviews_toggle", "disabled" if enabled else "enabled")
        await session.commit()
        await reviews(callback, session, shop)

    @router.callback_query(F.data.regexp(r"^a:review:\d+$"))
    async def review_detail(callback, session, shop):
        review = await session.get(Review, int(callback.data.rsplit(":", 1)[1]))
        if not review:
            return
        created_at = aware(review.created_at).astimezone(ZoneInfo(shop.cfg.timezone))
        await render(
            callback,
            "💬 <b>Анонімний відгук</b>\n\n"
            f"{escape(review.text)}\n\n"
            f"🕒 {created_at:%d.%m.%Y · %H:%M}",
            [
                [("🗑 Видалити", f"a:review_delete:{review.id}")],
                [("⬅️ До відгуків", "a:reviews:0")],
                BACK,
            ],
        )

    @router.callback_query(F.data.regexp(r"^a:review_delete:\d+$"))
    async def review_delete_confirm(callback, session):
        review_id = int(callback.data.rsplit(":", 1)[1])
        if not await session.get(Review, review_id):
            return
        await render(
            callback,
            "⚠️ <b>Видалити цей відгук?</b>\n\nЦю дію неможливо скасувати.",
            [
                [("🗑 Так, видалити", f"a:review_delete_confirm:{review_id}")],
                [("⬅️ Назад", f"a:review:{review_id}")],
            ],
        )

    @router.callback_query(F.data.regexp(r"^a:review_delete_confirm:\d+$"))
    async def review_delete(callback, session):
        review_id = int(callback.data.rsplit(":", 1)[1])
        review = await session.get(Review, review_id)
        if review:
            await session.delete(review)
            audit(session, callback.from_user.id, "review_delete", review_id)
            await session.commit()
        await render(
            callback,
            "✅ Відгук видалено.",
            [[("⬅️ До відгуків", "a:reviews:0")], BACK],
        )

    @router.callback_query(F.data == "a:loyalty")
    @router.message(F.text == "🎁 Програма лояльності")
    async def loyalty_settings(callback, session):
        enabled = await setting(session, "loyalty_enabled", "true") == "true"
        levels = (
            await session.scalars(select(LoyaltyLevel).order_by(LoyaltyLevel.level_number))
        ).all()
        icons = ("🥉", "🥈", "🥇", "🏆", "💎")
        rows = [
            [
                (
                    f"{icons[level.level_number - 1]} {level.name_ua}: "
                    f"{money(level.threshold_kopecks)} · {level.discount_percent}%",
                    f"a:loyalty_level:{level.level_number}",
                )
            ]
            for level in levels
        ]
        rows.extend(
            [
                [
                    (
                        "🔴 Вимкнути програму" if enabled else "🟢 Увімкнути програму",
                        "a:loyalty_toggle",
                    )
                ],
                GENERAL_BACK,
            ]
        )
        await render(
            callback,
            "🎁 <b>Програма лояльності</b>\n\n"
            f"Статус: {'🟢 увімкнена' if enabled else '🔴 вимкнена'}\n\n"
            "Рівень визначається за загальною фактично сплаченою сумою. "
            "Знижка автоматично застосовується до нових замовлень.",
            rows,
        )

    @router.callback_query(F.data == "a:loyalty_toggle")
    async def loyalty_toggle(callback, session):
        enabled = await setting(session, "loyalty_enabled", "true") == "true"
        new_enabled = not enabled
        await set_setting(session, "loyalty_enabled", "true" if new_enabled else "false")
        audit(session, callback.from_user.id, "loyalty_toggle", new_enabled)
        await session.commit()
        await render(
            callback,
            (
                "🟢 <b>Програму лояльності увімкнено.</b>"
                if new_enabled
                else "🔴 <b>Програму лояльності вимкнено.</b>"
            )
            + "\n\nКнопка програми лояльності залишиться в меню користувачів.",
            [[("🎁 До програми лояльності", "a:loyalty")], GENERAL_BACK],
        )

    @router.callback_query(F.data.regexp(r"^a:loyalty_level:[1-5]$"))
    async def loyalty_level(callback, session):
        level = await session.get(LoyaltyLevel, int(callback.data.rsplit(":", 1)[1]))
        if not level:
            return
        await render(
            callback,
            f"🏅 <b>{escape(level.name_ua)}</b>\n\n"
            f"💰 Поріг витрат: <b>{money(level.threshold_kopecks)}</b>\n"
            f"🏷 Постійна знижка: <b>{level.discount_percent}%</b>",
            [
                [("💰 Змінити суму", f"a:loyalty_edit:{level.level_number}:threshold")],
                [("🏷 Змінити відсоток", f"a:loyalty_edit:{level.level_number}:discount")],
                [("⬅️ До рівнів", "a:loyalty")],
                BACK,
            ],
        )

    @router.callback_query(
        F.data.regexp(r"^a:loyalty_edit:[1-5]:(threshold|discount)$")
    )
    async def loyalty_edit(callback, state, session):
        _, _, level_number, field = callback.data.split(":")
        level = await session.get(LoyaltyLevel, int(level_number))
        if not level:
            return
        await state.set_state(Form.loyalty)
        await state.set_data({"loyalty_level": level.level_number, "loyalty_field": field})
        prompt_text = (
            "Введіть нову суму витрат у гривнях. Пороги рівнів мають іти за зростанням."
            if field == "threshold"
            else "Введіть нову знижку цілим числом від 0 до 50%."
        )
        await render(callback, f"🏅 {escape(level.name_ua)}\n\n{prompt_text}", [BACK])

    @router.message(Form.loyalty)
    async def loyalty_input(message, state, session):
        data = await state.get_data()
        level = await session.get(LoyaltyLevel, data.get("loyalty_level"))
        if not level:
            await state.clear()
            return
        raw = (message.text or "").strip().replace(",", ".")
        if data.get("loyalty_field") == "discount":
            if not raw.isdigit() or not 0 <= int(raw) <= 50:
                await message.answer("Введіть цілий відсоток від 0 до 50.")
                return
            level.discount_percent = int(raw)
            value = f"{level.discount_percent}%"
        else:
            try:
                amount = Decimal(raw)
                if (
                    not amount.is_finite()
                    or amount <= 0
                    or amount > 1000000
                    or amount * 100 != (amount * 100).to_integral_value()
                ):
                    raise ValueError
                threshold = int(amount * 100)
            except (InvalidOperation, ValueError):
                await message.answer("Введіть суму від 0,01 до 1 000 000 грн, максимум 2 знаки після коми.")
                return
            previous = await session.get(LoyaltyLevel, level.level_number - 1)
            following = await session.get(LoyaltyLevel, level.level_number + 1)
            if previous and threshold <= previous.threshold_kopecks:
                await message.answer(f"Сума має бути більшою за поріг попереднього рівня: {money(previous.threshold_kopecks)}.")
                return
            if following and threshold >= following.threshold_kopecks:
                await message.answer(f"Сума має бути меншою за поріг наступного рівня: {money(following.threshold_kopecks)}.")
                return
            level.threshold_kopecks = threshold
            value = money(threshold)
        audit(
            session,
            message.from_user.id,
            "loyalty_level_edit",
            f"{level.level_number}:{data.get('loyalty_field')}:{value}",
        )
        await session.commit()
        await state.clear()
        await render(
            message,
            f"✅ Рівень <b>{escape(level.name_ua)}</b> оновлено: <b>{value}</b>",
            [[("🎁 До рівня", f"a:loyalty_level:{level.level_number}")], BACK],
        )

    @router.callback_query(F.data == "a:iban")
    async def iban_replace(callback, state):
        await state.set_state(Form.iban)
        await render(
            callback,
            "Введіть український IBAN у форматі <code>UA</code> + 27 цифр. "
            "Він не показуватиметься покупцям і використовуватиметься лише для перевірки скринів.",
            [[("🗑 Видалити IBAN", "a:iban_delete")], BACK],
        )

    @router.callback_query(F.data == "a:iban_delete")
    async def iban_delete(callback, state, session):
        await set_setting(session, "receipt_iban", "")
        audit(session, callback.from_user.id, "receipt_iban_delete", "receipt_iban")
        await session.commit()
        await state.clear()
        await render(callback, "IBAN видалено.", [[("💳 До реквізитів", "a:cards")], BACK])

    @router.message(Form.iban)
    async def iban_input(message, state, session, shop):
        iban = re.sub(r"\s+", "", message.text or "").upper()
        if not valid_ukrainian_iban(iban):
            await message.answer("Некоректний український IBAN. Перевірте номер і контрольні цифри.")
            return
        try:
            await message.delete()
        except Exception:
            pass
        await set_setting(session, "receipt_iban", shop.vault.encrypt(iban))
        audit(session, message.from_user.id, "receipt_iban_saved", iban[-6:])
        await session.commit()
        await state.clear()
        await render(
            message,
            f"✅ IBAN збережено: <code>UA•••••••••••••••••••••{iban[-6:]}</code>",
            [[("💳 До реквізитів", "a:cards")], BACK],
        )

    @router.callback_query(F.data == "a:mono_card")
    async def mono_card_replace(callback, state):
        await state.set_state(Form.card_number)
        await state.set_data({"card_action": "mono"})
        await render(callback, "Введіть новий номер картки Monobank (16–19 цифр).", [BACK])

    @router.callback_query(F.data == "a:card_add")
    async def card_add(callback, state):
        await state.set_state(Form.card_label)
        await state.set_data({"card_action": "add"})
        await render(callback, "Введіть назву картки, наприклад «mono». Максимум 64 символи.", [BACK])

    @router.message(Form.card_label)
    async def card_label(message, state):
        label = (message.text or "").strip()
        if not label or len(label) > 64:
            await message.answer("Введіть назву від 1 до 64 символів.")
            return
        await state.update_data(card_label=label)
        await state.set_state(Form.card_number)
        await message.answer("Введіть номер картки (16–19 цифр). Повідомлення буде видалено.")

    @router.message(Form.card_number)
    async def card_number(message, state, session, shop):
        digits = re.sub(r"\D", "", message.text or "")
        if not 16 <= len(digits) <= 19:
            await message.answer("Номер має містити 16–19 цифр.")
            return
        data = await state.get_data()
        try:
            await message.delete()
        except Exception:
            pass
        if data.get("card_action") == "mono":
            await set_setting(session, "manual_card", shop.vault.encrypt(digits))
            shop.cfg.manual_card = digits
            shop.personal_account = None
            shop.personal_cursors = {}
            shop.personal_next = 0
            target = "mono"
        elif data.get("card_action") == "replace":
            card = await session.get(PaymentCard, data.get("card_id"))
            if not card:
                return
            card.number_encrypted, card.last4 = shop.vault.encrypt(digits), digits[-4:]
            target = card.id
        else:
            active = await session.scalar(
                select(func.count()).select_from(PaymentCard).where(PaymentCard.active.is_(True))
            )
            if active >= 2:
                await message.answer("Уже є дві активні картки. Вимкніть одну перед додаванням.")
                return
            card = PaymentCard(
                label=data["card_label"], number_encrypted=shop.vault.encrypt(digits), last4=digits[-4:]
            )
            session.add(card)
            await session.flush()
            target = card.id
        audit(session, message.from_user.id, "payment_card_saved", target)
        await session.commit()
        await state.clear()
        await render(message, "Картку збережено.", [[("💳 До карток", "a:cards")], BACK])

    @router.callback_query(F.data.regexp(r"^a:card:\d+$"))
    async def card_detail(callback, session):
        card = await session.get(PaymentCard, int(callback.data.rsplit(":", 1)[1]))
        if not card:
            return
        await render(
            callback,
            f"{escape(card.label)}\n•••• {card.last4}\nСтатус: {'активна' if card.active else 'вимкнена'}",
            [
                [("🔄 Замінити номер", f"a:card_replace:{card.id}")],
                [("⏸ Вимкнути" if card.active else "▶️ Увімкнути", f"a:card_toggle:{card.id}")],
                [("🗑 Видалити", f"a:card_delete:{card.id}")],
                [("⬅️ Картки", "a:cards")],
                BACK,
            ],
        )

    @router.callback_query(F.data.regexp(r"^a:card_replace:\d+$"))
    async def card_replace(callback, state, session):
        card_id = int(callback.data.rsplit(":", 1)[1])
        if not await session.get(PaymentCard, card_id):
            return
        await state.set_state(Form.card_number)
        await state.set_data({"card_action": "replace", "card_id": card_id})
        await render(callback, "Введіть новий номер картки (16–19 цифр).", [BACK])

    @router.callback_query(F.data.regexp(r"^a:card_toggle:\d+$"))
    async def card_toggle(callback, session):
        card = await session.get(PaymentCard, int(callback.data.rsplit(":", 1)[1]))
        if not card:
            return
        if not card.active:
            active = await session.scalar(
                select(func.count()).select_from(PaymentCard).where(PaymentCard.active.is_(True))
            )
            if active >= 2:
                await callback.message.answer("Одночасно можуть бути активними максимум дві картки.")
                return
        card.active = not card.active
        audit(session, callback.from_user.id, "payment_card_toggle", card.id)
        await session.commit()
        await render(callback, "Статус картки змінено.", [[("💳 До карток", "a:cards")], BACK])

    @router.callback_query(F.data.regexp(r"^a:card_delete:\d+$"))
    async def card_delete(callback, session):
        card_id = int(callback.data.rsplit(":", 1)[1])
        await session.execute(sa_delete(PaymentCard).where(PaymentCard.id == card_id))
        audit(session, callback.from_user.id, "payment_card_delete", card_id)
        await session.commit()
        await render(
            callback,
            "Картку видалено. Старі замовлення зберегли свої реквізити.",
            [[("💳 До карток", "a:cards")], BACK],
        )

    @router.callback_query(F.data.regexp(r"^a:receipt:(approve|reject):\d+$"))
    async def receipt_review(callback, session):
        _, _, decision, receipt_id = callback.data.split(":")
        receipt = await session.get(PaymentReceipt, int(receipt_id), with_for_update=True)
        if not receipt:
            return
        if receipt.status not in {"manual_review", "retry_allowed_review"}:
            status = "✅ Підтверджено" if receipt.status == "approved" else "❌ Відхилено"
            caption = callback.message.caption or f"Заявка #{receipt.id}"
            if status not in caption:
                caption = caption[: 1024 - len(status) - 2] + "\n\n" + status
            try:
                await callback.message.edit_caption(caption=caption, reply_markup=None)
            except Exception:
                pass
            return
        order = await session.get(Order, receipt.order_id, with_for_update=True)
        receipt.status = "approved" if decision == "approve" else "rejected"
        receipt.reviewed_by, receipt.reviewed_at = callback.from_user.id, now()
        if decision == "approve" and order.status == "waiting_payment":
            order.status, order.paid_at = "paid", now()
        elif decision == "reject" and order.status == "waiting_payment":
            order.status = "cancelled"
            await release_stock(session, order.product_id)
        audit(session, callback.from_user.id, "receipt_" + decision, receipt.id)
        await session.commit()
        reason = (receipt.reason or "Причину не вдалося визначити").strip()
        if decision == "reject":
            await callback.bot.send_message(
                receipt.user_id,
                f"❌ Скрин оплати відхилено.\n\nПричина: {reason}\n\n"
                "Заявку закрито. Тепер ви можете створити нову покупку.",
            )
        status = (
            "✅ Підтверджено адміністратором — товар буде видано автоматично"
            if decision == "approve"
            else "❌ Відхилено адміністратором — заявку закрито"
        )
        caption = callback.message.caption or f"Заявка #{receipt.id}"
        caption = caption[: 1024 - len(status) - 2] + "\n\n" + status
        try:
            await callback.message.edit_caption(caption=caption, reply_markup=None)
        except Exception:
            await callback.message.answer(status)

    @router.callback_query(F.data == "a:add")
    @router.message(F.text == "➕ Додати товар")
    async def add(callback, state):
        await state.set_state(Form.product)
        await state.set_data({"field": FIELDS[0], "draft": {}})
        await prompt(callback, state)

    @router.callback_query(F.data == "a:review")
    async def review(callback, state):
        data = await state.get_data()
        if not data.get("draft"):
            return
        await render(
            callback,
            "Оберіть поле",
            [[(label, "a:reviewfield:" + field)] for field, label in LABELS.items()] + [BACK],
        )

    @router.callback_query(F.data.startswith("a:reviewfield:"))
    async def reviewfield(callback, state):
        field = callback.data.split(":")[2]
        if field not in FIELDS:
            return
        await state.update_data(field=field, reviewing=True)
        await prompt(callback, state)

    @router.message(Form.product)
    @router.message(Form.edit)
    async def input_product(message, state, shop):
        data = await state.get_data()
        field = data["field"]
        if field in ("featured", "on_home", "reward_promo_enabled", "gmail_credentials_encrypted"):
            await prompt(message, state)
            return
        try:
            value = parse_field(field, message, shop)
        except ValueError as error:
            await message.answer(str(error))
            return
        if field.endswith("_encrypted"):
            try:
                await message.delete()
            except Exception:
                pass
        await accept(message, state, shop, value)

    @router.callback_query(F.data == "a:skip")
    async def skip(callback, state, shop):
        data = await state.get_data()
        if data.get("field") in OPTIONAL:
            value = None if data["field"] in {"image_file_id", "gmail_credentials_encrypted"} else ""
            await accept(callback, state, shop, value)

    @router.callback_query(F.data.startswith("a:feature:"))
    async def feature(callback, state, shop):
        if (await state.get_data()).get("field") in ("featured", "on_home", "reward_promo_enabled"):
            await accept(callback, state, shop, callback.data.endswith(":1"))

    @router.callback_query(F.data == "a:stock:unlimited")
    async def stock_unlimited(callback, state, shop):
        if (await state.get_data()).get("field") == "stock_quantity":
            await accept(callback, state, shop, None)

    @router.callback_query(F.data == "a:stock:limited")
    async def stock_limited(callback, state):
        if (await state.get_data()).get("field") == "stock_quantity":
            await render(callback, "Введіть кількість товару від 0 до 100 000.", [[("❌ Скасувати", "a:home")]])

    @router.callback_query(F.data.regexp(r"^a:delivery:(auto|manual)$"))
    async def delivery_mode(callback, state, shop):
        if (await state.get_data()).get("field") == "delivery_mode":
            await accept(callback, state, shop, callback.data.rsplit(":", 1)[1])

    @router.callback_query(F.data.regexp(r"^a:activation_type:(standard|alternative)$"))
    async def activation_type(callback, state, shop):
        if (await state.get_data()).get("field") == "activation_type":
            await accept(callback, state, shop, callback.data.rsplit(":", 1)[1])

    @router.callback_query(F.data == "a:oauth")
    async def oauth(callback, state, shop):
        data = await state.get_data()
        if data.get("field") != "gmail_credentials_encrypted":
            return
        nonce = secrets.token_urlsafe(32)
        await state.update_data(oauth_nonce=nonce)
        # Connection is staged, never written to a product without admin confirmation.
        await shop.redis.set("oauth:" + nonce, str(callback.from_user.id), ex=600)
        await render(
            callback,
            "Авторизуйте потрібну Gmail-скриньку. Потім поверніться та перевірте підключення.",
            [
                [("Відкрити Google", shop.gmail.authorize_url(nonce))],
                [("✅ Перевірити підключення", "a:gmail_done")],
                [BACK[0]],
            ],
        )

    @router.callback_query(F.data == "a:gmail_done")
    async def gmail_done(callback, state, shop):
        data = await state.get_data()
        if data.get("field") != "gmail_credentials_encrypted":
            return
        credentials = await shop.redis.get("gmail:draft:" + data.get("oauth_nonce", "none"))
        if not credentials:
            await callback.message.answer("Підключення ще не завершено або термін дії минув.")
            return
        email = shop.vault.unpack(credentials)["email"]
        await callback.message.answer("Підключено: " + email)
        await accept(callback, state, shop, credentials)

    @router.callback_query(F.data == "a:save")
    async def save(callback, state, session, shop):
        data = await state.get_data()
        draft = data.get("draft", {})
        if not all(field in draft for field in FIELDS) or data.get("edit_id"):
            return
        p = Product(**draft)
        session.add(p)
        await session.flush()
        audit(session, callback.from_user.id, "product_create", p.id)
        await session.commit()
        await state.clear()
        await render(callback, "Товар збережено.", [[("Відкрити", f"a:product:{p.id}")], BACK])

    @router.callback_query(F.data.regexp(r"^a:(products|featured):\d+$"))
    @router.message(F.text.in_({"📦 Товари", "🔥 Головна сторінка"}))
    async def products(callback, session):
        if hasattr(callback, "data"):
            _, kind, page = callback.data.split(":")
        else:
            kind = "featured" if callback.text == "🔥 Головна сторінка" else "products"
            page = 0
        query = select(Product).where(Product.deleted_at.is_(None))
        if kind == "featured":
            query = query.where(Product.on_home.is_(True))
        total = await session.scalar(select(func.count()).select_from(query.subquery()))
        page = min(int(page), max(0, (total - 1) // 7))
        sorting = (
            (Product.featured_position, Product.id)
            if kind == "featured"
            else (Product.name_ua, Product.id)
        )
        items = (
            await session.scalars(
                query.order_by(*sorting).offset(page * 7).limit(7)
            )
        ).all()
        rows = [
            [
                (
                    ("👁 " if p.visible else "🙈 ") + p.name_ua,
                    f"a:product:{p.id}:{kind}:{page}",
                )
            ]
            for p in items
        ]
        rows += [pagination("a:" + kind, page, total, 7)]
        if kind == "products":
            rows += [[("🔎 Пошук товару", "a:product_search")]]
        if kind == "featured":
            rows += [[("➕ Додати гру на головну", "a:products:0")]]
        title = "Товари" if items else "Товарів немає"
        if kind == "products":
            sold = await session.scalar(
                select(func.count()).select_from(Order).where(Order.status.in_(SUCCESS))
            )
            title = f"{title}\n\n🎮 Всього продано за весь час: <b>{sold}</b>"
        await render(callback, title, rows + [BACK])

    async def show_product_search_results(callback, state, session, page=0):
        data = await state.get_data()
        search_query = data.get("product_search_query", "")
        if not search_query:
            await state.clear()
            await render(callback, "Введіть пошуковий запит ще раз.", [[("🔎 Пошук товару", "a:product_search")], BACK])
            return
        escaped_query = search_query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{escaped_query}%"
        query = select(Product).where(
            Product.deleted_at.is_(None),
            or_(
                Product.name_ua.ilike(pattern, escape="\\"),
                Product.name_ru.ilike(pattern, escape="\\"),
            ),
        )
        total = await session.scalar(select(func.count()).select_from(query.subquery()))
        page = min(max(0, page), max(0, (total - 1) // 7))
        items = (
            await session.scalars(
                query.order_by(Product.name_ua, Product.id).offset(page * 7).limit(7)
            )
        ).all()
        rows = [
            [
                (
                    ("👁 " if product.visible else "🙈 ")
                    + product.name_ua,
                    f"a:product:{product.id}:search:{page}",
                )
            ]
            for product in items
        ]
        if items:
            rows.append(pagination("a:product_search_results", page, total, 7))
        rows += [[("🔎 Новий пошук", "a:product_search")], [("⬅️ До товарів", "a:products:0")], BACK]
        await render(
            callback,
            "🔎 <b>Результати пошуку</b>\n"
            f"Запит: <code>{escape(search_query)}</code>\n\n"
            + (f"Знайдено: <b>{total}</b>" if items else "Нічого не знайдено."),
            rows,
        )

    @router.callback_query(F.data == "a:product_search")
    async def product_search_start(callback, state):
        await state.set_state(Form.product_search)
        await state.set_data({})
        await render(
            callback,
            "🔎 Введіть щонайменше 2 символи з назви товару:",
            [[("⬅️ До товарів", "a:products:0")], BACK],
        )

    @router.message(Form.product_search)
    async def product_search_submit(message, state, session):
        search_query = " ".join((message.text or "").split())
        if not 2 <= len(search_query) <= 80:
            await message.answer("Введіть від 2 до 80 символів.")
            return
        await state.update_data(product_search_query=search_query)
        await show_product_search_results(message, state, session)

    @router.callback_query(F.data.regexp(r"^a:product_search_results:\d+$"))
    async def product_search_results(callback, state, session):
        await show_product_search_results(callback, state, session, int(callback.data.rsplit(":", 1)[1]))

    @router.callback_query(F.data.regexp(r"^a:product:\d+(?::(products|search):\d+)?$"))
    async def product(callback, session):
        _, _, product_id, *source = callback.data.split(":")
        p = await session.get(Product, int(product_id))
        if not p:
            return
        if source and source[0] == "search":
            return_target = f"a:product_search_results:{source[1]}"
        else:
            return_target = f"a:{source[0] if source else 'products'}:{source[1] if source else 0}"
        sold, revenue = (
            await session.execute(
                select(
                    func.count(),
                    func.coalesce(func.sum(Order.price_snapshot), 0),
                ).where(Order.product_id == p.id, Order.status.in_(SUCCESS))
            )
        ).one()
        rows = [
            [(LABELS["name_ua"], f"a:edit:{p.id}:name_ua")],
            [
                (LABELS["price"], f"a:edit:{p.id}:price"),
                (LABELS["code_limit"], f"a:edit:{p.id}:code_limit"),
            ],
            [
                (LABELS["image_file_id"], f"a:edit:{p.id}:image_file_id"),
                (LABELS["description_ua"], f"a:edit:{p.id}:description_ua"),
            ],
            [
                (LABELS["steam_login_encrypted"], f"a:edit:{p.id}:steam_login_encrypted"),
                (LABELS["steam_password_encrypted"], f"a:edit:{p.id}:steam_password_encrypted"),
            ],
            [
                (LABELS["stock_quantity"], f"a:edit:{p.id}:stock_quantity"),
                (LABELS["gmail_credentials_encrypted"], f"a:edit:{p.id}:gmail_credentials_encrypted"),
            ],
            [(LABELS["delivery_mode"], f"a:edit:{p.id}:delivery_mode")],
            [(LABELS["activation_type"], f"a:edit:{p.id}:activation_type")],
            [(LABELS["reward_promo_enabled"], f"a:edit:{p.id}:reward_promo_enabled")],
        ]
        rows += [
            [
                ("🆕 Новинка: " + str(p.featured), f"a:toggle:{p.id}:featured"),
                ("🔥 Головна: " + str(p.on_home), f"a:toggle:{p.id}:on_home"),
            ],
            [
                ("👁 Видимість: " + str(p.visible), f"a:toggle:{p.id}:visible"),
            ],
            [("⬆️ Вище", f"a:move:{p.id}:-1"), ("⬇️ Нижче", f"a:move:{p.id}:1")],
            [("🗑 Видалити", f"a:delete:{p.id}")],
            [("⬅️ До товарів", return_target)],
        ]
        await render(
            callback,
            product_text(p, "ua")
            + f"\n\n🔑 Доступно кодів на покупку: <b>{p.code_limit}</b>"
            + "\n🎟 Тип активації: <b>"
            + ("альтернативна" if p.activation_type == "alternative" else "звичайна")
            + "</b>"
            + "\n🎁 Подарунковий промокод: <b>"
            + ("так" if p.reward_promo_enabled else "ні")
            + "</b>"
            + f"\n📊 Продано за весь час: <b>{sold}</b> на суму <b>{money(revenue)}</b>",
            rows,
            photo=p.image_file_id,
        )

    @router.callback_query(F.data.startswith("a:edit:"))
    async def edit(callback, state, session):
        _, _, pid, field = callback.data.split(":")
        if field not in FIELDS or not await session.get(Product, int(pid)):
            return
        await state.set_state(Form.edit)
        await state.set_data({"edit_id": int(pid), "field": field, "draft": {}})
        await prompt(callback, state)

    @router.callback_query(F.data == "a:save_edit")
    async def save_edit(callback, state, session):
        data = await state.get_data()
        if not data.get("edit_id") or not data.get("draft"):
            return
        p = await session.get(Product, data["edit_id"])
        for field, value in data["draft"].items():
            if field in FIELDS or field in {"name_ru", "description_ru"}:
                setattr(p, field, value)
                audit(session, callback.from_user.id, "product_edit:" + field, p.id)
        await session.commit()
        await state.clear()
        await render(callback, "Зміни збережено.", [[("⬅️ Товар", f"a:product:{p.id}")], BACK])

    @router.callback_query(F.data.startswith("a:toggle:"))
    async def toggle(callback, session):
        _, _, pid, field = callback.data.split(":")
        if field not in ("featured", "on_home", "visible"):
            return
        p = await session.get(Product, int(pid))
        if not p or p.deleted_at:
            return
        setattr(p, field, not getattr(p, field))
        audit(session, callback.from_user.id, "toggle:" + field, p.id)
        await session.commit()
        await render(callback, "Оновлено.", [[("⬅️ Товар", f"a:product:{p.id}")], BACK])

    @router.callback_query(F.data.startswith("a:move:"))
    async def move(callback, session):
        _, _, pid, direction = callback.data.split(":")
        items = (
            await session.scalars(
                select(Product)
                .where(Product.on_home.is_(True), Product.deleted_at.is_(None))
                .order_by(Product.featured_position, Product.id)
                .with_for_update()
            )
        ).all()
        for i, p in enumerate(items):
            if p.id == int(pid):
                target = i + (-1 if direction == "-1" else 1)
                if 0 <= target < len(items):
                    items[i], items[target] = items[target], items[i]
                break
        for i, p in enumerate(items):
            p.featured_position = i
        audit(session, callback.from_user.id, "featured_reorder", pid)
        await session.commit()
        await render(callback, "Порядок оновлено.", [[("🔥 Головна", "a:featured:0")], BACK])

    @router.callback_query(F.data.regexp(r"^a:delete:\d+$"))
    async def delete(callback, state, session):
        pid = int(callback.data.split(":")[2])
        p = await session.get(Product, pid)
        if not p:
            return
        await state.update_data(delete_id=pid)
        await render(
            callback,
            "Видалити «" + escape(p.name_ua) + "»? Історія покупок залишиться.",
            [[("✅ Так, видалити", "a:confirm_delete")], [("❌ Скасувати", "a:home")]],
        )

    @router.callback_query(F.data == "a:confirm_delete")
    async def confirm_delete(callback, state, session):
        pid = (await state.get_data()).get("delete_id")
        if not pid:
            return
        p = await session.get(Product, pid)
        p.deleted_at, p.visible, p.featured, p.on_home = now(), False, False, False
        audit(session, callback.from_user.id, "product_delete", pid)
        await session.commit()
        await state.clear()
        await render(callback, "Товар видалено з каталогу.", [BACK])

    @router.callback_query(F.data.startswith("a:orders:"))
    async def orders(callback, session):
        _, _, status, page = callback.data.split(":")
        query = select(Order)
        if status.isdigit():
            query = query.where(Order.user_id == int(status))
        elif status in ("paid", "delivered", "waiting_payment", "payment_failed"):
            query = query.where(Order.status == status)
        total = await session.scalar(select(func.count()).select_from(query.subquery()))
        page = min(int(page), max(0, (total - 1) // 7))
        items = (
            await session.scalars(query.order_by(Order.created_at.desc()).offset(page * 7).limit(7))
        ).all()
        rows = [
            [(o.product_name_snapshot + " — " + money(o.price_snapshot), "a:order:" + o.id)] for o in items
        ]
        rows += [
            pagination("a:orders:" + status, page, total, 7),
            [("Всі", "a:orders:all:0"), ("Оплачені", "a:orders:paid:0")],
            [("Неоплачені", "a:orders:waiting_payment:0"), ("Видано", "a:orders:delivered:0")],
            BACK,
        ]
        await render(callback, "Замовлення", rows)

    @router.callback_query(F.data.startswith("a:order:"))
    async def order(callback, session):
        o = await session.get(Order, callback.data.split(":")[2])
        if not o:
            return
        u = await session.get(User, o.user_id)
        await render(
            callback,
            f"#{o.id}\n{escape(o.product_name_snapshot)}\n"
            f"{discounted_price_text(o.original_price_snapshot or o.price_snapshot, o.discount_percent_snapshot)}\n"
            f"Промокод: {escape(o.promo_code_snapshot or '—')}\n"
            f"👤 {user_reference(u)}\n{o.status}\n{o.created_at} UTC\n"
            f"Invoice: {escape(o.mono_invoice_id or '—')}\n"
            f"Видача потребує перевірки: {o.delivery_uncertain}",
            [BACK],
        )

    @router.callback_query(F.data.regexp(r"^a:users:\d+$"))
    @router.message(F.text == "👥 Користувачі")
    async def users(callback, state, session):
        await state.clear()
        total = await session.scalar(select(func.count()).select_from(User))
        page = min(
            int(callback.data.split(":")[2]) if hasattr(callback, "data") else 0,
            max(0, (total - 1) // 7),
        )
        items = (
            await session.scalars(select(User).order_by(User.created_at.desc()).offset(page * 7).limit(7))
        ).all()
        rows = [
            [
                (
                    ("🔴 " if u.access_blocked else "") + (u.username or u.first_name or str(u.id)),
                    f"a:user:{u.id}",
                )
            ]
            for u in items
        ]
        await render(
            callback,
            "Користувачі",
            rows + [pagination("a:users", page, total, 7), [("🔎 Пошук користувача", "a:user_search")], GENERAL_BACK],
        )

    async def show_user_search_results(callback, state, session, page=0):
        data = await state.get_data()
        search_query = data.get("user_search_query", "")
        if not search_query:
            await state.clear()
            await render(callback, "Введіть пошуковий запит ще раз.", [[("🔎 Пошук користувача", "a:user_search")], GENERAL_BACK])
            return
        escaped_query = search_query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{escaped_query}%"
        conditions = [
            User.username.ilike(pattern, escape="\\"),
            User.first_name.ilike(pattern, escape="\\"),
        ]
        if search_query.isdigit():
            conditions.append(User.id == int(search_query))
        query = select(User).where(or_(*conditions))
        total = await session.scalar(select(func.count()).select_from(query.subquery()))
        page = min(max(0, page), max(0, (total - 1) // 7))
        items = (
            await session.scalars(
                query.order_by(User.created_at.desc()).offset(page * 7).limit(7)
            )
        ).all()
        rows = [
            [
                (
                    ("🔴 " if u.access_blocked else "") + (u.username or u.first_name or str(u.id)),
                    f"a:user:{u.id}",
                )
            ]
            for u in items
        ]
        if items:
            rows.append(pagination("a:user_search_results", page, total, 7))
        rows += [[("🔎 Новий пошук", "a:user_search")], [("⬅️ До користувачів", "a:users:0")], GENERAL_BACK]
        await render(
            callback,
            "🔎 <b>Результати пошуку</b>\n"
            f"Запит: <code>{escape(search_query)}</code>\n\n"
            + (f"Знайдено: <b>{total}</b>" if items else "Нічого не знайдено."),
            rows,
        )

    @router.callback_query(F.data == "a:user_search")
    async def user_search_start(callback, state):
        await state.set_state(Form.user_search)
        await state.set_data({})
        await render(
            callback,
            "🔎 Введіть ім’я, username або Telegram ID користувача:",
            [[("⬅️ До користувачів", "a:users:0")], GENERAL_BACK],
        )

    @router.message(Form.user_search)
    async def user_search_submit(message, state, session):
        search_query = " ".join((message.text or "").split()).removeprefix("@")
        if not 1 <= len(search_query) <= 80:
            await message.answer("Введіть від 1 до 80 символів.")
            return
        await state.update_data(user_search_query=search_query)
        await show_user_search_results(message, state, session)

    @router.callback_query(F.data.regexp(r"^a:user_search_results:\d+$"))
    async def user_search_results(callback, state, session):
        await show_user_search_results(callback, state, session, int(callback.data.rsplit(":", 1)[1]))

    @router.callback_query(F.data.regexp(r"^a:user:\d+$"))
    async def user_detail(callback, session, shop):
        u = await session.get(User, int(callback.data.split(":")[2]))
        if not u:
            return
        count, spent, last = (
            await session.execute(
                select(
                    func.count(), func.coalesce(func.sum(Order.price_snapshot), 0), func.max(Order.paid_at)
                ).where(Order.user_id == u.id, Order.status.in_(SUCCESS))
            )
        ).one()
        timezone = ZoneInfo(shop.cfg.timezone)
        created_at = aware(u.created_at).astimezone(timezone)
        active_at = aware(u.last_activity_at).astimezone(timezone)
        last_purchase = aware(last).astimezone(timezone) if last else None
        username = user_reference(u)
        loyalty = await loyalty_status(session, u.id)
        loyalty_level = loyalty["current"]
        loyalty_name = loyalty_level.name_ua if loyalty_level else "Початковий"
        access_status = "🔴 Заблокований" if u.access_blocked else "🟢 Активний"
        block_label = "🔓 Розблокувати" if u.access_blocked else "🔒 Заблокувати"
        role = (
            "Головний адміністратор"
            if is_primary_admin(shop.cfg, u.id)
            else "Адміністратор"
            if is_admin(shop.cfg, u.id, u)
            else "Користувач"
        )
        rows = [[("🛒 Покупки / список ігор", f"a:orders:{u.id}:0")]]
        if (
            is_primary_admin(shop.cfg, callback.from_user.id)
            and not is_primary_admin(shop.cfg, u.id)
            and (u.is_admin or not is_admin(shop.cfg, u.id, u))
        ):
            role_label = "➖ Прибрати з адміністраторів" if u.is_admin else "➕ Зробити адміністратором"
            rows.append([(role_label, f"a:user_admin:{u.id}")])
        rows += [
            [(block_label, f"a:user_block:{u.id}")],
            [("🗑 Видалити користувача", f"a:user_delete:{u.id}")],
            [("⬅️ До користувачів", "a:users:0")],
        ]
        await render(
            callback,
            "👤 <b>Користувач</b>\n\n"
            f"🧑 Ім’я: <b>{escape(u.first_name or '—')}</b>\n"
            f"🔗 Username: {username}\n"
            f"🆔 ID: <code>{u.id}</code>\n"
            f"🌐 Мова: <b>{escape(u.language or '—').upper()}</b>\n"
            f"🛡 Доступ: <b>{access_status}</b>\n"
            f"🔑 Роль: <b>{role}</b>\n\n"
            "🕒 <b>Активність</b>\n"
            f"🚀 Перший запуск: {created_at:%d.%m.%Y · %H:%M}\n"
            f"⚡ Остання активність: {active_at:%d.%m.%Y · %H:%M}\n\n"
            "🛍 <b>Покупки</b>\n"
            f"🧾 Кількість: <b>{count}</b>\n"
            f"💰 Витрачено: <b>{money(spent)}</b>\n"
            f"📅 Остання покупка: {last_purchase.strftime('%d.%m.%Y · %H:%M') if last_purchase else '—'}\n\n"
            "🎁 <b>Лояльність</b>\n"
            f"🏅 Рівень: <b>{escape(loyalty_name)}</b>\n"
            f"🏷 Знижка: <b>{loyalty['discount_percent']}%</b>",
            rows,
        )

    @router.callback_query(F.data.regexp(r"^a:user_admin:\d+$"))
    async def user_admin_toggle(callback, session, shop):
        user_id = int(callback.data.rsplit(":", 1)[1])
        if not is_primary_admin(shop.cfg, callback.from_user.id):
            await render(
                callback,
                "⛔ Керувати адміністраторами може лише головний адміністратор.",
                [BACK],
            )
            return
        if is_primary_admin(shop.cfg, user_id):
            await render(callback, "Головного адміністратора не можна позбавити прав.", [BACK])
            return
        user = await session.get(User, user_id, with_for_update=True)
        if not user:
            await render(callback, "Користувача не знайдено.", [BACK])
            return
        user.is_admin = not user.is_admin
        audit(
            session,
            callback.from_user.id,
            "admin_grant" if user.is_admin else "admin_revoke",
            user_id,
        )
        await session.commit()
        await render(
            callback,
            "✅ Адміністратора додано." if user.is_admin else "✅ Права адміністратора знято.",
            [[("⬅️ До користувача", f"a:user:{user_id}")], BACK],
        )

    @router.callback_query(F.data.regexp(r"^a:user_block:\d+$"))
    async def user_block(callback, session, shop):
        user_id = int(callback.data.rsplit(":", 1)[1])
        target = await session.get(User, user_id)
        if is_admin(shop.cfg, user_id, target):
            await render(
                callback,
                "Адміністратора не можна заблокувати.",
                [[("⬅️ Назад", f"a:user:{user_id}")], BACK],
            )
            return
        user = await session.get(User, user_id, with_for_update=True)
        if not user:
            await render(callback, "Користувача не знайдено.", [[("👥 До користувачів", "a:users:0")], BACK])
            return
        user.access_blocked = not user.access_blocked
        action = "user_unblock" if not user.access_blocked else "user_block"
        audit(session, callback.from_user.id, action, user_id)
        await session.commit()
        result = (
            "Користувача розблоковано."
            if not user.access_blocked
            else "Користувача заблоковано."
        )
        await render(
            callback,
            f"{'✅' if not user.access_blocked else '⛔️'} {result}",
            [[("⬅️ До користувача", f"a:user:{user_id}")], BACK],
        )

    @router.callback_query(F.data.regexp(r"^a:user_delete:\d+$"))
    async def user_delete(callback, session, shop):
        user_id = int(callback.data.rsplit(":", 1)[1])
        target = await session.get(User, user_id)
        if is_admin(shop.cfg, user_id, target):
            await render(
                callback,
                "Адміністратора не можна видалити.",
                [[("⬅️ Назад", f"a:user:{user_id}")], BACK],
            )
            return
        user = await session.get(User, user_id)
        if not user:
            await render(
                callback,
                "Користувача не знайдено.",
                [[("👥 До користувачів", "a:users:0")], BACK],
            )
            return
        name = f"@{user.username}" if user.username else user.first_name or str(user.id)
        await render(
            callback,
            f"🗑 <b>Видалити користувача?</b>\n\n"
            f"👤 {escape(name)}\n🆔 <code>{user.id}</code>\n\n"
            "Буде назавжди видалено профіль, усі покупки, квитанції та запити кодів.",
            [
                [("✅ Так, видалити повністю", f"a:user_delete_confirm:{user.id}")],
                [("❌ Скасувати", f"a:user:{user.id}")],
                BACK,
            ],
        )

    @router.callback_query(F.data.regexp(r"^a:user_delete_confirm:\d+$"))
    async def user_delete_confirm(callback, session, shop):
        user_id = int(callback.data.rsplit(":", 1)[1])
        target = await session.get(User, user_id)
        if is_admin(shop.cfg, user_id, target):
            await render(callback, "Адміністратора не можна видалити.", [BACK])
            return
        user = await session.get(User, user_id, with_for_update=True)
        if not user:
            await render(
                callback,
                "Користувача вже видалено.",
                [[("👥 До користувачів", "a:users:0")], BACK],
            )
            return
        await session.execute(sa_delete(PaymentReceipt).where(PaymentReceipt.user_id == user_id))
        await session.execute(sa_delete(MailCodeRequest).where(MailCodeRequest.user_id == user_id))
        await session.execute(
            sa_delete(PromoCode).where(
                PromoCode.generated_for_order_id.in_(select(Order.id).where(Order.user_id == user_id))
            )
        )
        await session.execute(sa_delete(Order).where(Order.user_id == user_id))
        await session.execute(sa_delete(User).where(User.id == user_id))
        audit(session, callback.from_user.id, "user_delete", user_id)
        await session.commit()
        await render(
            callback,
            f"✅ Користувача <code>{user_id}</code> та всі пов’язані дані видалено.",
            [[("👥 До користувачів", "a:users:0")], BACK],
        )

    @router.callback_query(F.data == "a:stats")
    @router.message(F.text == "📊 Статистика")
    async def stats(callback, session, shop):
        reset_at = None
        raw_reset_at = await setting(session, "stats_reset_at", "")
        admin_purchases_hidden = bool(
            await setting(session, f"admin_purchases_reset_at:{callback.from_user.id}", "")
        )
        if raw_reset_at:
            try:
                reset_at = aware(datetime.fromisoformat(raw_reset_at))
            except ValueError:
                reset_at = None

        users_query = select(func.count()).select_from(User)
        orders_filter = [Order.status.in_(SUCCESS)]
        products_query = (
            select(func.count())
            .select_from(Product)
            .where(Product.visible.is_(True), Product.deleted_at.is_(None))
        )
        if reset_at:
            users_query = users_query.where(User.created_at >= reset_at)
            orders_filter.append(Order.paid_at >= reset_at)
            products_query = products_query.where(Product.created_at >= reset_at)

        users = await session.scalar(users_query)
        buyers, sales, revenue = (
            await session.execute(
                select(
                    func.count(func.distinct(Order.user_id)),
                    func.count(),
                    func.coalesce(func.sum(Order.price_snapshot), 0),
                ).where(*orders_filter)
            )
        ).one()
        active = await session.scalar(products_query)
        text = (
            "📊 <b>Статистика магазину</b>\n\n"
            "👥 <b>Загальні показники</b>\n"
            f"👤 Користувачів: <b>{users}</b>\n"
            f"🛒 Покупців: <b>{buyers}</b>\n"
            f"🎮 Продажів: <b>{sales}</b>\n"
            f"💰 Загальна виручка: <b>{money(revenue)}</b>\n"
            f"📦 Активних товарів: <b>{active}</b>\n\n"
            "📈 <b>Продажі за період</b>"
        )
        local = now().astimezone(ZoneInfo(shop.cfg.timezone))
        for icon, label, start in [
            ("☀️", "Сьогодні", local.replace(hour=0, minute=0, second=0, microsecond=0)),
            ("📅", "За 7 днів", now() - timedelta(days=7)),
            ("🗓", "За 30 днів", now() - timedelta(days=30)),
        ]:
            count, amount = (
                await session.execute(
                    select(func.count(), func.coalesce(func.sum(Order.price_snapshot), 0)).where(
                        *orders_filter, Order.paid_at >= start
                    )
                )
            ).one()
            text += f"\n{icon} {label}: <b>{count}</b> · <b>{money(amount)}</b>"
        top = (
            await session.execute(
                select(Product.name_ua, func.count(Order.id))
                .join(Order, Order.product_id == Product.id)
                .where(*orders_filter)
                .group_by(Product.id, Product.name_ua)
                .order_by(func.count(Order.id).desc())
                .limit(5)
            )
        ).all()
        medals = ("🥇", "🥈", "🥉", "4️⃣", "5️⃣")
        if top:
            text += "\n\n🏆 <b>Найпопулярніші ігри</b>\n" + "\n".join(
                f"{medals[index]} {escape(name)} — <b>{count}</b>"
                for index, (name, count) in enumerate(top)
            )
        else:
            text += "\n\n🏆 <b>Найпопулярніші ігри</b>\nПоки немає продажів"
        if reset_at:
            text += f"\n\nℹ️ Статистика ведеться з <b>{reset_at.astimezone(ZoneInfo(shop.cfg.timezone)):%d.%m.%Y %H:%M}</b>"
        await render(
            callback,
            text,
            [
                [("🗑 Очистити статистику", "a:stats:reset")],
                [("🧾 Очистити список покупок", "a:stats:purchases_reset")],
                *([[('♻️ Відновити список покупок', 'a:stats:purchases_restore')]] if admin_purchases_hidden else []),
                GENERAL_BACK,
            ],
        )

    @router.callback_query(F.data == "a:stats:reset")
    async def stats_reset_prompt(callback):
        await render(
            callback,
            "🗑 <b>Очистити статистику?</b>\n\n"
            "Показники продажів, покупців і товарів буде відраховано заново від поточного моменту. "
            "Історія замовлень та дані магазину не будуть видалені.",
            [
                [("✅ Так, очистити", "a:stats:reset_confirm")],
                [("❌ Скасувати", "a:stats")],
            ],
        )

    @router.callback_query(F.data == "a:stats:reset_confirm")
    async def stats_reset_confirm(callback, session):
        reset_at = now()
        await set_setting(session, "stats_reset_at", reset_at.isoformat())
        audit(session, callback.from_user.id, "stats_reset", reset_at.isoformat())
        await session.commit()
        await render(
            callback,
            "✅ Статистику очищено. Нові показники рахуються від цього моменту.",
            [[("📊 Відкрити статистику", "a:stats")], GENERAL_BACK],
        )

    @router.callback_query(F.data == "a:stats:purchases_reset")
    async def purchases_reset_prompt(callback):
        await render(
            callback,
            "🧾 <b>Очистити список покупок?</b>\n\n"
            "Ваші особисті покупки буде приховано з розділу «Мої покупки». "
            "Адмінська історія замовлень, покупки інших користувачів та всі дані залишаться без змін — "
            "їх можна буде відновити кнопкою у статистиці.",
            [
                [("✅ Так, очистити список", "a:stats:purchases_reset_confirm")],
                [("❌ Скасувати", "a:stats")],
            ],
        )

    @router.callback_query(F.data == "a:stats:purchases_reset_confirm")
    async def purchases_reset_confirm(callback, session):
        hidden_orders = await session.scalar(
            select(func.count()).select_from(Order).where(
                Order.user_id == callback.from_user.id,
                Order.status.in_(SUCCESS),
            )
        )
        reset_at = now()
        await set_setting(
            session,
            f"admin_purchases_reset_at:{callback.from_user.id}",
            reset_at.isoformat(),
        )
        audit(session, callback.from_user.id, "admin_purchases_hide", hidden_orders)
        await session.commit()
        await render(
            callback,
            f"✅ З розділу «Мої покупки» приховано <b>{hidden_orders}</b> ваших покупок. Дані не видалено.",
            [[("📊 Відкрити статистику", "a:stats")], GENERAL_BACK],
        )

    @router.callback_query(F.data == "a:stats:purchases_restore")
    async def purchases_restore(callback, session):
        await set_setting(session, f"admin_purchases_reset_at:{callback.from_user.id}", "")
        audit(session, callback.from_user.id, "admin_purchases_restore", "all")
        await session.commit()
        await render(
            callback,
            "♻️ Усі покупки відновлено в адмінському списку.",
            [[("📊 Відкрити статистику", "a:stats")], GENERAL_BACK],
        )

    @router.callback_query(F.data == "a:settings")
    @router.message(F.text == "⚙️ Налаштування")
    async def settings(callback, session):
        enabled = await setting(session, "enabled", "true")
        notifications_enabled = (
            await setting(session, "admin_info_notifications", "true") == "true"
        )
        setting_buttons = [
            (label, "a:setting:" + key)
            for key, label in SETTINGS.items()
            if not key.startswith(
                (
                    "info_",
                    "welcome_",
                    "activation_guide_",
                    "alternative_activation_guide_",
                    "faq_",
                    "order_request_",
                )
            )
        ]
        compact_buttons = setting_buttons + [
            ("👋 Привітання", "a:welcome_settings"),
            ("ℹ️ Інформація", "a:info_settings"),
            ("🎟 Активація гри", "a:activation_guide_settings"),
            ("🔀 Альтернативна інструкція", "a:alternative_activation_guide_settings"),
            ("❓ FAQ", "a:faq_settings"),
            ("🧾 Приклад квитанції", "a:receipt_example_settings"),
            (
                "🔔 Сповіщення: ВКЛ" if notifications_enabled else "🔕 Сповіщення: ВИКЛ",
                "a:admin_notifications_toggle",
            ),
            ("📌 Замовити товар", "a:order_request_settings"),
            ("🗄️ Резервна копія", "a:download_database"),
            ("🟢 Магазин" if enabled == "true" else "🔴 Магазин", "a:enabled"),
        ]
        await render(
            callback,
            "Налаштування. Monobank використовує merchant acquiring token.",
            [compact_buttons[index : index + 2] for index in range(0, len(compact_buttons), 2)]
            + [GENERAL_BACK],
        )

    @router.callback_query(F.data == "a:receipt_example_settings")
    async def receipt_example_settings(callback, state, session):
        await state.clear()
        photo = await setting(session, "receipt_example_photo", "")
        await render(
            callback,
            "🧾 <b>Приклад вдалої квитанції</b>\n\n"
            + (
                "Фото додано. Воно показується покупцю перед надсиланням квитанції."
                if photo
                else "Фото ще не додано. Покупець бачить звичайну текстову інструкцію."
            ),
            [
                [("🖼 Додати / замінити фото", "a:receipt_example_photo")],
                *([[('🗑 Прибрати фото', 'a:receipt_example_photo_clear')]] if photo else []),
                [("⬅️ Налаштування", "a:settings")],
                GENERAL_BACK,
            ],
        )

    @router.callback_query(F.data == "a:receipt_example_photo")
    async def receipt_example_photo_start(callback, state):
        await state.set_state(Form.receipt_example_photo)
        await render(
            callback,
            "Надішліть фото прикладу вдалої квитанції. Воно замінить попереднє.",
            [[("❌ Скасувати", "a:receipt_example_settings")], GENERAL_BACK],
        )

    @router.message(Form.receipt_example_photo)
    async def receipt_example_photo_save(message, state, session):
        if not message.photo:
            await message.answer("Надішліть зображення саме як фото.")
            return
        await set_setting(session, "receipt_example_photo", message.photo[-1].file_id)
        audit(session, message.from_user.id, "receipt_example_photo", "updated")
        await session.commit()
        await state.clear()
        await render(
            message,
            "✅ Приклад квитанції збережено.",
            [[("⬅️ До прикладу квитанції", "a:receipt_example_settings")], GENERAL_BACK],
        )

    @router.callback_query(F.data == "a:receipt_example_photo_clear")
    async def receipt_example_photo_clear(callback, state, session):
        await set_setting(session, "receipt_example_photo", "")
        audit(session, callback.from_user.id, "receipt_example_photo", "cleared")
        await session.commit()
        await state.clear()
        await render(
            callback,
            "✅ Приклад квитанції прибрано. Покупці знову бачитимуть звичайний текст.",
            [[("⬅️ До прикладу квитанції", "a:receipt_example_settings")], GENERAL_BACK],
        )

    @router.callback_query(F.data == "a:download_database")
    async def download_database(callback, session, shop):
        """Create a consistent PostgreSQL dump and send it only to an administrator."""
        await callback.answer("Формую резервну копію…")
        fd, filename = tempfile.mkstemp(prefix="steamsell-", suffix=".dump")
        os.close(fd)
        dump_path = Path(filename)
        try:
            process = await asyncio.create_subprocess_exec(
                "pg_dump",
                "--format=custom",
                "--no-owner",
                "--no-privileges",
                f"--file={dump_path}",
                f"--dbname={pg_dump_url(shop.cfg.database_url)}",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await process.communicate()
            if process.returncode:
                raise RuntimeError(stderr.decode(errors="replace").strip())

            timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            await callback.message.answer_document(
                FSInputFile(dump_path, filename=f"steamsell-backup-{timestamp}.dump"),
                caption="🗄️ Резервна копія PostgreSQL. Відновлення: pg_restore --clean --dbname=… файл.dump",
                protect_content=True,
            )
            audit(session, callback.from_user.id, "database_backup_download", timestamp)
            await session.commit()
        except (OSError, RuntimeError):
            log.exception("database_backup_failed admin=%s", callback.from_user.id)
            await callback.message.answer(
                "Не вдалося сформувати резервну копію БД. Спробуйте ще раз або перевірте логи сервера."
            )
            # Diagnostics may include connection details, so do not send them to Telegram.
        finally:
            dump_path.unlink(missing_ok=True)

    @router.callback_query(F.data == "a:info_settings")
    async def info_settings(callback):
        await render(
            callback,
            "Оберіть мову тексту розділу «Інформація».",
            [
                [("🇺🇦 Українська", "a:setting:info_ua")],
                [("🇷🇺 Русский", "a:setting:info_ru")],
                [("⬅️ Налаштування", "a:settings")],
                GENERAL_BACK,
            ],
        )

    @router.callback_query(F.data == "a:welcome_settings")
    async def welcome_settings(callback):
        await render(
            callback,
            "Оберіть мову привітального повідомлення для першого запуску.",
            [
                [("🇺🇦 Українська", "a:setting:welcome_ua")],
                [("🇷🇺 Русский", "a:setting:welcome_ru")],
                [("⬅️ Налаштування", "a:settings")],
                GENERAL_BACK,
            ],
        )

    @router.callback_query(F.data == "a:activation_guide_settings")
    async def activation_guide_settings(callback):
        await render(
            callback,
            "Оберіть мову тексту розділу «Як активувати гру».",
            [
                [("🇺🇦 Українська", "a:setting:activation_guide_ua")],
                [("🇷🇺 Русский", "a:setting:activation_guide_ru")],
                [("⬅️ Налаштування", "a:settings")],
                GENERAL_BACK,
            ],
        )

    @router.callback_query(F.data == "a:alternative_activation_guide_settings")
    async def alternative_activation_guide_settings(callback):
        await render(
            callback,
            "Оберіть мову альтернативної інструкції.",
            [
                [("🇺🇦 Українська", "a:setting:alternative_activation_guide_ua")],
                [("🇷🇺 Русский", "a:setting:alternative_activation_guide_ru")],
                [("⬅️ Налаштування", "a:settings")],
                GENERAL_BACK,
            ],
        )

    @router.callback_query(F.data == "a:faq_settings")
    async def faq_settings(callback):
        await render(
            callback,
            "Оберіть мову тексту розділу «Часті запитання».",
            [
                [("🇺🇦 Українська", "a:setting:faq_ua")],
                [("🇷🇺 Русский", "a:setting:faq_ru")],
                [("⬅️ Налаштування", "a:settings")],
                GENERAL_BACK,
            ],
        )

    @router.callback_query(F.data == "a:enabled")
    async def enabled(callback, session):
        await set_setting(
            session, "enabled", "false" if await setting(session, "enabled", "true") == "true" else "true"
        )
        audit(session, callback.from_user.id, "shop_toggle", "enabled")
        await session.commit()
        await render(
            callback,
            "Режим магазину змінено.",
            [[("⚙️ Налаштування", "a:settings")], GENERAL_BACK],
        )

    @router.callback_query(F.data.startswith("a:setting:"))
    async def setting_start(callback, state, session):
        key = callback.data.split(":")[2]
        if key not in SETTINGS:
            return
        await state.set_state(Form.setting)
        await state.set_data({"key": key})
        if key.startswith(
            (
                "info_",
                "welcome_",
                "activation_guide_",
                "alternative_activation_guide_",
                "faq_",
                "order_request_",
            )
        ):
            language = key.rsplit("_", 1)[1]
            if key.startswith("info_"):
                fallback = tr("info_default", language)
            elif key.startswith("welcome_"):
                fallback = tr("welcome", language)
            elif key.startswith("order_request_title_"):
                fallback = tr("order_request_title", language)
            elif key.startswith("order_request_description_"):
                fallback = tr("order_request_description", language)
            else:
                from app.bot import ACTIVATION_GUIDE, FAQ_TEXT

                fallback = (
                    ""
                    if key.startswith("alternative_activation_guide_")
                    else ACTIVATION_GUIDE
                    if key.startswith("activation_guide_")
                    else FAQ_TEXT
                )
            current = await setting(session, key, "") or fallback
            await render(
                callback,
                f"<b>{SETTINGS[key]}</b>\n\n"
                f"Поточний текст:\n<blockquote>{escape(current)}</blockquote>\n\n"
                "Скопіюйте його, підправте та надішліть новою відповіддю.",
                [[("⬅️ Налаштування", "a:settings")], GENERAL_BACK],
            )
            return
        await render(callback, "Введіть: " + SETTINGS[key], [GENERAL_BACK])

    @router.message(Form.setting)
    async def setting_input(message, state, shop):
        key = (await state.get_data())["key"]
        value = (message.text or "").strip()
        text_setting = key.startswith(
            (
                "info_",
                "welcome_",
                "activation_guide_",
                "alternative_activation_guide_",
                "faq_",
                "order_request_",
            )
        )
        max_length = (
            60
            if key.startswith("order_request_title_")
            else 800
            if key.startswith("order_request_description_")
            else 3500
            if text_setting
            else 2000
        )
        if not value or len(value) > max_length:
            await message.answer(f"Введіть від 1 до {max_length} символів.")
            return
        if key == "page_size" and (not value.isdigit() or not 1 <= int(value) <= 20):
            await message.answer("Введіть число від 1 до 20.")
            return
        if key == "support_username":
            value = value.lstrip("@")
            if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{4,31}", value):
                await message.answer("Введіть Telegram username без посилання.")
                return
        if key == "mono_token":
            value = shop.vault.encrypt(value)
            try:
                await message.delete()
            except Exception:
                pass
        await state.update_data(value=value)
        await render(
            message,
            "Підтвердити зміну «" + SETTINGS[key] + "»?",
            [[("✅ Зберегти", "a:save_setting")], [("❌ Скасувати", "a:settings")]],
        )

    @router.callback_query(F.data == "a:save_setting")
    async def save_setting(callback, state, session, shop):
        data = await state.get_data()
        if data.get("key") not in SETTINGS or "value" not in data:
            return
        if data["key"] == "mono_token":
            pending = await session.scalar(
                select(func.count()).select_from(Order).where(Order.status == "waiting_payment")
            )
            if pending:
                await callback.message.answer(
                    "Спочатку дочекайтеся завершення неоплачених рахунків. Заміна токена зараз унеможливить їх перевірку."
                )
                return
        await set_setting(session, data["key"], data["value"])
        audit(session, callback.from_user.id, "setting_change", data["key"])
        await session.commit()
        if data["key"] == "mono_token":
            shop.mono.token = shop.vault.decrypt(data["value"])
            shop.mono.cached_key = None
        return_to_order_request = data["key"].startswith("order_request_")
        await state.clear()
        await render(
            callback,
            "Налаштування збережено.",
            [
                [("⬅️ До картки", "a:order_request_settings")],
                GENERAL_BACK,
            ]
            if return_to_order_request
            else [GENERAL_BACK],
        )

    @router.callback_query(F.data == "a:broadcast")
    @router.message(F.text == "📢 Розсилка")
    async def broadcast_start(callback, state):
        await state.set_state(Form.broadcast)
        await state.set_data({})
        await render(
            callback, "Надішліть текст або фото з підписом. Форматування Telegram буде збережено.", [BACK]
        )

    @router.message(Form.broadcast)
    async def broadcast_input(message, state):
        if not message.text and not message.photo:
            return
        payload = {
            "text": message.text or message.caption or "",
            "photo": message.photo[-1].file_id if message.photo else None,
            "entities": [
                e.model_dump(mode="json", exclude_none=True)
                for e in (message.entities or message.caption_entities or [])
            ],
        }
        await state.update_data(broadcast=payload, broadcast_confirmed=False)
        await message.copy_to(message.chat.id)
        await render(
            message,
            "Попередній перегляд розсилки. Продовжити?",
            [[("📤 Надіслати", "a:broadcast_confirm")], [("❌ Скасувати", "a:home")]],
        )

    @router.callback_query(F.data == "a:broadcast_confirm")
    async def broadcast_confirm(callback, state, session):
        if not (await state.get_data()).get("broadcast"):
            return
        total = await session.scalar(
            select(func.count())
            .select_from(User)
            .where(User.blocked.is_(False), User.broadcast_subscribed.is_(True))
        )
        await state.update_data(broadcast_confirmed=True)
        await render(
            callback,
            f"Остаточно підтвердити розсилку для {total} користувачів?",
            [[("✅ Так, розіслати", "a:broadcast_send")], [("❌ Скасувати", "a:home")]],
        )

    @router.callback_query(F.data == "a:broadcast_send")
    async def broadcast_send(callback, state, session):
        data = await state.get_data()
        if not data.get("broadcast_confirmed") or not data.get("broadcast"):
            return
        job = Broadcast(payload=data["broadcast"])
        session.add(job)
        await session.flush()
        audit(session, callback.from_user.id, "broadcast_queued", job.id)
        await session.commit()
        await state.clear()
        await render(
            callback, f"Розсилку #{job.id} додано в чергу. Підсумок надійде після завершення.", [BACK]
        )

    return router
