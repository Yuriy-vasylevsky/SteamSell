from datetime import UTC, datetime
from html import escape
from zoneinfo import ZoneInfo

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (
    CopyTextButton,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)
from cryptography.fernet import InvalidToken

from app.i18n import money, tr


def keyboard(rows):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=label,
                    **(
                        {"copy_text": CopyTextButton(text=target.removeprefix("copy:"))}
                        if target.startswith("copy:")
                        else {"url": target}
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


def user_reference(user):
    """Return an admin-friendly Telegram reference for a user."""
    if user.username:
        return f"@{escape(user.username)}"
    return f'<a href="tg://user?id={user.id}">ID: {user.id}</a>'


def product_text(product, lang, discount_percent=0):
    stock = tr("unlimited", lang) if product.stock_quantity is None else f"{product.stock_quantity} шт."
    delivery = (
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
    return (
        f"<b>{escape(getattr(product, 'name_' + lang))}</b>\n\n"
        f"{escape(getattr(product, 'description_' + lang))}\n\n"
        f"💰 {'Ціна' if lang == 'ua' else 'Цена'}: {discounted_price_text(product.price, discount_percent)}\n\n"
        f"{tr('stock_available', lang)}: <b>{stock}</b>\n"
        f"{delivery}"
    )


def reward_promo_text(promo, lang):
    if not promo or aware_datetime(promo.expires_at) <= datetime.now(UTC):
        return ""
    expires = aware_datetime(promo.expires_at).astimezone(ZoneInfo("Europe/Kyiv"))
    if lang == "ua":
        return (
            f"\n\n🎁 <b>Ваш промокод:</b> <code>{escape(promo.code)}</code>\n"
            f"Знижка: <b>100%</b> · 1 використання\n"
            f"Для товарів до: <b>{money(promo.max_product_price)}</b>\n"
            f"Дійсний до: <b>{expires:%d.%m.%Y · %H:%M}</b>"
        )
    return (
        f"\n\n🎁 <b>Ваш промокод:</b> <code>{escape(promo.code)}</code>\n"
        f"Скидка: <b>100%</b> · 1 использование\n"
        f"Для товаров до: <b>{money(promo.max_product_price)}</b>\n"
        f"Действует до: <b>{expires:%d.%m.%Y · %H:%M}</b>"
    )


def aware_datetime(value):
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def purchase_text(order, product, lang, vault, reward_promo=None):
    # Older orders may not have a recorded payment timestamp.  They must
    # remain accessible from the purchase history nevertheless.
    paid_at = order.paid_at or order.delivered_at or order.created_at
    if paid_at.tzinfo is None:
        paid_at = paid_at.replace(tzinfo=UTC)
    paid_at = paid_at.astimezone(ZoneInfo("Europe/Kyiv"))
    try:
        login = escape(vault.decrypt(product.steam_login_encrypted))
        password = escape(vault.decrypt(product.steam_password_encrypted))
        credentials = (
            f"🔐 <b>{'Дані Steam' if lang == 'ua' else 'Данные Steam'}</b>\n"
            f"👤 {'Логін' if lang == 'ua' else 'Логин'}: <code>{login}</code>\n"
            f"🔑 Пароль: <code>{password}</code>"
        )
    except (AttributeError, TypeError, ValueError, InvalidToken):
        credentials = (
            "⚠️ <b>Дані доступу потребують ручної перевірки.</b>\n"
            "Зверніться до підтримки."
            if lang == "ua"
            else "⚠️ <b>Данные доступа требуют ручной проверки.</b>\n"
            "Обратитесь в поддержку."
        )
    paid_title = "Оплата успішна!" if lang == "ua" else "Оплата успешна!"
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
    promo_text = (
        f"\n🎟 Промокод: <code>{escape(order.promo_code_snapshot)}</code> "
        f"(−{order.promo_discount_percent_snapshot}%)"
        if order.promo_code_snapshot
        else ""
    )
    return (
        f"✅ <b>{paid_title}</b>\n\n"
        f"🎮 <b>{escape(order.product_name_snapshot)}</b>\n"
        f"💰 {base_price}{promo_text}{tip_text}\n"
        f"🗓 {paid_at:%d.%m.%Y · %H:%M}\n\n"
        f"{credentials}\n\n"
        f"ℹ️ {tr('code_retry_hint', lang)}"
        f"{reward_promo_text(reward_promo, lang)}"
    )


def manual_delivery_text(order, lang, reward_promo=None):
    return (
        f"✅ <b>{'Оплату підтверджено' if lang == 'ua' else 'Оплата подтверждена'}</b>\n\n"
        f"🎮 <b>{escape(order.product_name_snapshot)}</b>\n"
        f"💰 <b>{money(order.price_snapshot)}</b>\n\n"
        f"📦 {'Видача товару через адміністратора.' if lang == 'ua' else 'Выдача товара через администратора.'}"
        f"{reward_promo_text(reward_promo, lang)}"
    )


def copy_card_rows(cards, lang):
    """Buttons copy only the card number, without its label or any payment text."""
    rows = []
    seen = set()
    for card in cards:
        number = "".join(char for char in str(card) if char.isdigit())
        if not number or number in seen:
            continue
        seen.add(number)
        label = (
            f"📋 Скопіювати картку •••• {number[-4:]}"
            if lang == "ua"
            else f"📋 Скопировать карту •••• {number[-4:]}"
        )
        rows.append([(label, f"copy:{number}")])
    return rows


def payment_rows(order, lang, cards=()):
    cancel = [
        (
            "❌ Скасувати платіж" if lang == "ua" else "❌ Отменить платёж",
            f"cancel_payment:{order.id}",
        )
    ]
    if order.payment_method == "personal":
        return copy_card_rows(cards, lang) + [
            [
                (
                    "🔎 Перевірити платіж" if lang == "ua" else "🔎 Проверить платёж",
                    f"check_payment:{order.id}",
                )
            ],
            cancel,
        ]
    if order.payment_method == "receipt":
        return copy_card_rows(cards, lang) + [
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
    copy_hint = (
        "Натисніть «Скопіювати картку», щоб скопіювати лише номер картки."
        if lang == "ua"
        else "Нажмите «Скопировать карту», чтобы скопировать только номер карты."
    )
    promo_text = (
        f"\n🎟 {'Промокод' if lang == 'ua' else 'Промокод'}: "
        f"<code>{escape(order.promo_code_snapshot)}</code> (−{order.promo_discount_percent_snapshot}%)"
        if order.promo_code_snapshot
        else ""
    )
    tip_text = (
        f"\n💛 {'Чайові' if lang == 'ua' else 'Чаевые'}: {order.tip_percent_snapshot}%"
        if order.tip_percent_snapshot
        else ""
    )
    text = (
        f"{tr('payment_waiting', lang)}\n\n"
        f"<b>{escape(order.product_name_snapshot)}</b>\n\n"
        f"{discounted_price_text(order.original_price_snapshot or order.price_snapshot, order.discount_percent_snapshot)}"
        f"{promo_text}{tip_text}\n\n"
        f"💰 {'До сплати' if lang == 'ua' else 'К оплате'}: <b>{money(order.price_snapshot)}</b>"
    )
    if order.payment_method == "personal":
        text += (
            f"\n\nКартка / Карта:\n<code>{escape(card)}</code>"
            f"\n<i>{copy_hint}</i>"
            "\n\nПереказуйте точну суму після створення замовлення. "
            "Після переказу натисніть «Перевірити платіж». Коментар не потрібен."
        )
    elif order.payment_method == "receipt":
        text += (
            f"\n\nКартки для оплати / Карты для оплаты:\n{card}"
            f"\n<i>{copy_hint}</i>"
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
    return_target="home",
):
    rows = []
    guide_action = (
        "alternative_activation_guide"
        if getattr(order, "activation_type_snapshot", "standard") == "alternative"
        else "activation_guide"
    )
    code_target = (
        f"code:{order.id}:{return_target.rsplit(':', 1)[1]}"
        if return_target.startswith("purchases:")
        else f"code:{order.id}"
    )
    if gmail_connected:
        if code_request_available is None:
            code_request_available = code_requests_remaining > 0
        label = f"{tr('code', lang)} · {'ще' if lang == 'ua' else 'ещё'} {code_requests_remaining}"
        rows.append([(label, code_target if code_request_available else "noop")])
    rows.extend(
        [
            [
                (
                    tr("activation_guide", lang),
                    f"{guide_action}:{order.id}",
                )
            ],
            [(tr("usage_rules", lang), f"usage_rules:{order.id}")],
            [back(lang, return_target)[0]],
        ]
    )
    return rows
