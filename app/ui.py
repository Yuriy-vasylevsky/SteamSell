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
        [(tr("purchases", lang), "purchases:0")],
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


def product_text(product, lang):
    return (
        f"<b>{escape(getattr(product, 'name_' + lang))}</b>\n\n"
        f"{escape(getattr(product, 'description_' + lang))}\n\n{money(product.price)}"
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
    return (
        f"✅ <b>{paid_title}</b>\n\n"
        f"🎮 <b>{escape(order.product_name_snapshot)}</b>\n"
        f"💰 {money(order.price_snapshot)}\n"
        f"🗓 {paid_at:%d.%m.%Y · %H:%M}\n\n"
        f"🔐 <b>{credentials_title}</b>\n"
        f"👤 {login_label}: <code>{login}</code>\n"
        f"🔑 Пароль: <code>{password}</code>\n\n"
        f"⚠️ {tr('notice', lang)}"
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


def persistent_menu(lang, admin=False, subscribed=False):
    newsletter = KeyboardButton(
        text=(
            "🔕 Відписатися від розсилки"
            if subscribed and lang == "ua"
            else "🔕 Отписаться от рассылки"
            if subscribed
            else "📨 Підписатися на розсилку"
            if lang == "ua"
            else "📨 Подписаться на рассылку"
        )
    )
    rows = [
        [KeyboardButton(text=tr("featured", lang)), KeyboardButton(text=tr("catalog", lang))],
        [KeyboardButton(text=tr("purchases", lang))],
        [KeyboardButton(text=tr("info", lang)), KeyboardButton(text=tr("support", lang))],
        [KeyboardButton(text=tr("language", lang)), newsletter],
    ]
    if admin:
        rows.append([KeyboardButton(text="⚙️ Адмін-панель")])
    return ReplyKeyboardMarkup(
        keyboard=rows,
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Оберіть розділ" if lang == "ua" else "Выберите раздел",
    )


def payment_wait_text(order, lang, step=0, card=""):
    text = (
        f"{tr('payment_waiting', lang)}\n\n"
        f"<b>{escape(order.product_name_snapshot)}</b>\n{money(order.price_snapshot)}"
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


def purchase_rows(order, lang, support, gmail_connected=True, code_requests_remaining=2):
    rows = []
    if gmail_connected:
        label = f"{tr('code', lang)} · {'ще' if lang == 'ua' else 'ещё'} {code_requests_remaining}"
        rows.append([(label, f"code:{order.id}" if code_requests_remaining else "noop")])
    rows.extend(
        [
            [(tr("activation_guide", lang), f"activation_guide:{order.id}")],
            [(tr("usage_rules", lang), f"usage_rules:{order.id}")],
            [(tr("home", lang), "home")],
        ]
    )
    return rows
