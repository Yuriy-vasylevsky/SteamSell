from datetime import UTC
from html import escape
from zoneinfo import ZoneInfo

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

from app.i18n import money, tr


def keyboard(rows):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=label,
                    **(
                        {"url": target}
                        if target.startswith(("https://", "tg://"))
                        else {"callback_data": target}
                    ),
                )
                for label, target in row
            ]
            for row in rows
        ]
    )


def home_rows(lang):
    return [
        [(tr("featured", lang), "featured:0"), (tr("catalog", lang), "catalog:0")],
    ]


def back(lang, target="home"):
    return [(tr("back", lang), target)]


def pagination(prefix, page, total, size):
    pages = max(1, (total + size - 1) // size)
    row = []
    if page > 0:
        row.append(("⬅️", f"{prefix}:{page - 1}"))
    row.append((f"{page + 1} / {pages}", "noop"))
    if page + 1 < pages:
        row.append(("➡️", f"{prefix}:{page + 1}"))
    return row


async def render(event, text, rows, photo=None, protect=False):
    message = event.message if hasattr(event, "message") else event
    markup = keyboard(rows)
    if hasattr(event, "message") and not photo and not message.photo and not protect:
        try:
            return await message.edit_text(text, reply_markup=markup, parse_mode="HTML")
        except TelegramBadRequest as error:
            if "message is not modified" in str(error):
                return message
    if photo:
        return await message.answer_photo(
            photo, caption=text, reply_markup=markup, parse_mode="HTML", protect_content=protect
        )
    return await message.answer(text, reply_markup=markup, parse_mode="HTML", protect_content=protect)


def discounted_price_text(price, discount_percent, html=True):
    from app.services import apply_discount

    discounted = apply_discount(price, discount_percent)
    if not discount_percent or discounted == price:
        return money(price)
    if html:
        return f"<s>{money(price)}</s> → <b>{money(discounted)}</b> (-{discount_percent}%)"
    return f"{money(price)} → {money(discounted)} (-{discount_percent}%)"


def priced_button_text(title, price, discount_percent=0):
    price_text = discounted_price_text(price, discount_percent, html=False)
    available = max(12, 61 - len(price_text))
    short_title = title if len(title) <= available else title[: available - 1] + "…"
    return f"{short_title} — {price_text}"


def product_text(product, lang, discount_percent=0):
    stock = tr("unlimited", lang) if product.stock_quantity is None else f"{product.stock_quantity} шт."
    return (
        f"<b>{escape(getattr(product, 'name_' + lang))}</b>\n\n"
        f"{escape(getattr(product, 'description_' + lang))}\n\n"
        f"{discounted_price_text(product.price, discount_percent)}\n"
        f"{tr('stock_available', lang)}: <b>{stock}</b>"
    )


def purchase_text(order, product, lang, vault):
    paid_at = order.paid_at
    if paid_at.tzinfo is None:
        paid_at = paid_at.replace(tzinfo=UTC)
    paid_at = paid_at.astimezone(ZoneInfo("Europe/Kyiv"))
    login = escape(vault.decrypt(product.steam_login_encrypted))
    password = escape(vault.decrypt(product.steam_password_encrypted))
    paid_title = "Оплата успішна!" if lang == "ua" else "Оплата успешна!"
    credentials_title = "Дані Steam" if lang == "ua" else "Данные Steam"
    login_label = "Логін" if lang == "ua" else "Логин"
    base_price = discounted_price_text(
        order.original_price_snapshot or order.price_snapshot,
        order.discount_percent_snapshot,
    )
    tip_text = (
        f"\n💛 {'Чайові' if lang == 'ua' else 'Чаевые'}: {order.tip_percent_snapshot}%"
        f"\n💰 {'Разом' if lang == 'ua' else 'Итого'}: <b>{money(order.price_snapshot)}</b>"
        if order.tip_percent_snapshot
        else ""
    )
    return (
        f"✅ <b>{paid_title}</b>\n\n"
        f"🎮 <b>{escape(order.product_name_snapshot)}</b>\n"
        f"💰 {base_price}{tip_text}\n"
        f"🗓 {paid_at:%d.%m.%Y · %H:%M}\n\n"
        f"🔐 <b>{credentials_title}</b>\n"
        f"👤 {login_label}: <code>{login}</code>\n"
        f"🔑 Пароль: <code>{password}</code>\n\n"
        f"⚠️ {tr('notice', lang)}\n\n"
        f"ℹ️ {tr('code_retry_hint', lang)}"
    )


def manual_delivery_text(order, lang):
    return (
        f"✅ <b>{'Оплату підтверджено' if lang == 'ua' else 'Оплата подтверждена'}</b>\n\n"
        f"🎮 <b>{escape(order.product_name_snapshot)}</b>\n"
        f"💰 <b>{money(order.price_snapshot)}</b>\n\n"
        f"📦 {'Видача товару через адміністратора.' if lang == 'ua' else 'Выдача товара через администратора.'}"
    )


def payment_rows(order, lang):
    cancel = [
        (
            "❌ Скасувати платіж" if lang == "ua" else "❌ Отменить платёж",
            f"cancel_payment:{order.id}",
        )
    ]
    if order.payment_method == "personal":
        return [
            [
                (
                    "🔎 Перевірити платіж" if lang == "ua" else "🔎 Проверить платёж",
                    f"check_payment:{order.id}",
                )
            ],
            cancel,
        ]
    if order.payment_method == "receipt":
        return [
            [
                (
                    "📎 Надіслати скрін оплати" if lang == "ua" else "📎 Отправить скрин оплаты",
                    f"receipt:{order.id}",
                )
            ],
            cancel,
        ]
    return ([[(tr("pay", lang), order.payment_url)]] if order.payment_url else []) + [cancel]


def persistent_menu(lang, admin=False, subscribed=False, loyalty_enabled=False):
    rows = [
        [KeyboardButton(text=tr("featured", lang)), KeyboardButton(text=tr("catalog", lang))],
        [KeyboardButton(text=tr("purchases", lang)), KeyboardButton(text=tr("account", lang))],
        [KeyboardButton(text=tr("info", lang)), KeyboardButton(text=tr("support", lang))],
    ]
    if admin:
        rows.append([KeyboardButton(text="⚙️ Адмін-панель")])
    return ReplyKeyboardMarkup(
        keyboard=rows,
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Оберіть розділ" if lang == "ua" else "Выберите раздел",
    )


def admin_menu():
    rows = [
        [KeyboardButton(text="➕ Додати товар"), KeyboardButton(text="📦 Товари")],
        [KeyboardButton(text="🔑 Отримати код"), KeyboardButton(text="🔥 Головна сторінка")],
        [KeyboardButton(text="📢 Розсилка"), KeyboardButton(text="💬 Відгуки")],
        [KeyboardButton(text="⚙️ Загальні налаштування")],
        [KeyboardButton(text="⬅️ Вийти з адмінки")],
    ]
    return ReplyKeyboardMarkup(
        keyboard=rows,
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Оберіть дію адміністратора",
    )


def payment_wait_text(order, lang, step=0, card=""):
    tip_text = (
        f"\n💛 {'Чайові' if lang == 'ua' else 'Чаевые'}: {order.tip_percent_snapshot}%"
        f"\n💰 {'До сплати' if lang == 'ua' else 'К оплате'}: <b>{money(order.price_snapshot)}</b>"
        if order.tip_percent_snapshot
        else ""
    )
    text = (
        f"{tr('payment_waiting', lang)}\n\n"
        f"<b>{escape(order.product_name_snapshot)}</b>\n"
        f"{discounted_price_text(order.original_price_snapshot or order.price_snapshot, order.discount_percent_snapshot)}{tip_text}"
    )
    if order.payment_method == "personal":
        text += (
            f"\n\nКартка / Карта:\n<code>{escape(card)}</code>"
            "\n\nПереказуйте точну суму після створення замовлення. "
            "Після переказу натисніть «Перевірити платіж». Коментар не потрібен."
        )
    elif order.payment_method == "receipt":
        text += (
            f"\n\nКартки для оплати / Карты для оплаты:\n<code>{escape(card)}</code>"
            "\n\nПереказуйте точну суму, потім натисніть «Надіслати скрін оплати»."
        )
    return text


def purchase_rows(
    order,
    lang,
    support,
    gmail_connected=True,
    code_requests_remaining=2,
    code_request_available=None,
):
    rows = []
    if gmail_connected:
        if code_request_available is None:
            code_request_available = code_requests_remaining > 0
        label = f"{tr('code', lang)} · {'ще' if lang == 'ua' else 'ещё'} {code_requests_remaining}"
        rows.append([(label, f"code:{order.id}" if code_request_available else "noop")])
    rows.extend(
        [
            [(tr("activation_guide", lang), f"activation_guide:{order.id}")],
            [(tr("usage_rules", lang), f"usage_rules:{order.id}")],
            [(tr("home", lang), "home")],
        ]
    )
    return rows
