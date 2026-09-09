from html import escape

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

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
        [(tr("info", lang), "info"), (tr("support", lang), "support")],
        [(tr("language", lang), "language")],
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
    return (
        f"{tr('paid', lang)}\n\n<b>{escape(order.product_name_snapshot)}</b>\n"
        f"{money(order.price_snapshot)}\n{tr('date', lang)}: {order.paid_at:%Y-%m-%d %H:%M} UTC\n\n"
        f"Steam Login:\n<code>{escape(vault.decrypt(product.steam_login_encrypted))}</code>\n\n"
        f"Steam Password:\n<code>{escape(vault.decrypt(product.steam_password_encrypted))}</code>\n\n"
        f"{tr('notice', lang)}"
    )


def payment_rows(order, lang):
    if order.payment_method == "personal":
        return [[("🔎 Перевірити платіж" if lang == "ua" else "🔎 Проверить платёж", f"check_payment:{order.id}")], back(lang)]
    if order.payment_method == "receipt":
        return [[("📎 Надіслати скрін оплати" if lang == "ua" else "📎 Отправить скрин оплаты", f"receipt:{order.id}")], back(lang)]
    return ([[(tr("pay", lang), order.payment_url)]] if order.payment_url else []) + [back(lang)]


def payment_wait_text(order, lang, step=0, card=""):
    frames = ("⏳", "⌛", "⏳·", "⏳··", "⏳···")
    text = (
        f"{frames[step % len(frames)]} {tr('payment_waiting', lang)}\n\n"
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


def purchase_rows(order, lang, support, gmail_connected=True):
    rows = []
    if gmail_connected:
        rows.append([(tr("code", lang), f"code:{order.id}")])
    rows.extend(
        [
            [(tr("support", lang), "https://t.me/" + support.lstrip("@"))],
            [(tr("home", lang), "home")],
        ]
    )
    return rows
