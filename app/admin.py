import re
import secrets
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from html import escape
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.filters import Command, Filter
from aiogram.fsm.state import State, StatesGroup
from sqlalchemy import delete as sa_delete
from sqlalchemy import func, select

from app.access import is_admin
from app.i18n import money
from app.models import Broadcast, Order, PaymentCard, PaymentReceipt, Product, User, now
from app.services import SUCCESS, audit, configured_manual_card, set_setting, setting
from app.ui import pagination, product_text, render


class AdminOnly(Filter):
    async def __call__(self, event, shop):
        return is_admin(shop.cfg, event.from_user.id)


class Form(StatesGroup):
    product = State()
    edit = State()
    setting = State()
    broadcast = State()
    card_label = State()
    card_number = State()


FIELDS = [
    "name_ua",
    "price",
    "image_file_id",
    "description_ua",
    "steam_login_encrypted",
    "steam_password_encrypted",
    "gmail_credentials_encrypted",
    "featured",
    "on_home",
]
LABELS = {
    "name_ua": "Назва товару",
    "price": "Ціна у гривнях (наприклад 599.50)",
    "image_file_id": "Фото",
    "description_ua": "Опис",
    "steam_login_encrypted": "Steam Login",
    "steam_password_encrypted": "Steam Password",
    "gmail_credentials_encrypted": "Gmail OAuth",
    "featured": "Додати до новинок?",
    "on_home": "Показувати на головній?",
}
OPTIONAL = {"image_file_id", "description_ua", "gmail_credentials_encrypted"}
MENU = [
    [("➕ Додати товар", "a:add"), ("📦 Товари", "a:products:0")],
    [("🔑 Отримати код", "a:latest_code"), ("👥 Користувачі", "a:users:0")],
    [("🔥 Головна сторінка", "a:featured:0"), ("📊 Статистика", "a:stats")],
    [("📢 Розсилка", "a:broadcast"), ("⚙️ Налаштування", "a:settings")],
    [("💳 Режим оплати", "a:payment_mode"), ("💳 Керування картками", "a:cards")],
    [("⬅️ Вийти з адмінки", "home")],
]
BACK = [("⬅️ Адмінка", "a:home")]
SETTINGS = {
    "support_username": "Telegram підтримки",
    "page_size": "Товарів на сторінці",
    "welcome_ua": "Привітання UA",
    "welcome_ru": "Привітання RU",
    "info_ua": "Інформація UA",
    "info_ru": "Інформація RU",
    "mono_token": "Токен Monobank",
}


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
    if field in ("featured", "on_home"):
        rows.append([("✅ Так", "a:feature:1"), ("❌ Ні", "a:feature:0")])
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
    await render(
        event,
        product_text(p, "ua")
        + "\n\nSteam-дані: збережено без показу.\nGmail: "
        + ("підключено." if p.gmail_credentials_encrypted else "не підключено."),
        [[("✅ Зберегти", "a:save"), ("✏️ Редагувати", "a:review")], [("❌ Скасувати", "a:home")]],
        photo=p.image_file_id,
    )


async def accept(event, state, shop, value):
    data = await state.get_data()
    draft = data.get("draft", {})
    draft[data["field"]] = value
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
        await render(event, "Керування магазином", MENU)

    @router.callback_query(F.data == "a:payment_mode")
    async def payment_mode(callback, session):
        mode = await setting(session, "payment_mode", "mono")
        cards = await session.scalar(
            select(func.count()).select_from(PaymentCard).where(PaymentCard.active.is_(True))
        )
        await render(
            callback,
            f"Режим оплати для нових замовлень: <b>{'DeepSeek — перевірка скріну' if mode == 'deepseek' else 'Monobank API'}</b>\nАктивних карток для DeepSeek: {cards}/2",
            [
                [("✅ Monobank API" if mode == "mono" else "Monobank API", "a:paymode:mono")],
                [("✅ DeepSeek-скрін" if mode == "deepseek" else "DeepSeek-скрін", "a:paymode:deepseek")],
                BACK,
            ],
        )

    @router.callback_query(F.data == "a:latest_code")
    async def latest_steam_code(callback, session, shop):
        products = (
            await session.scalars(select(Product).where(Product.deleted_at.is_(None)).order_by(Product.id))
        ).all()
        gmail_product = next(
            (product for product in reversed(products) if product.gmail_credentials_encrypted),
            None,
        )
        if not gmail_product:
            await render(callback, "Немає товарів із підключеною Gmail-поштою.", [BACK])
            return
        accounts = []
        for product in products:
            accounts.append(
                (
                    shop.vault.decrypt(product.steam_login_encrypted),
                    product.name_ua,
                )
            )
        try:
            result = await shop.gmail.latest_code_for_accounts(
                shop.vault.unpack(gmail_product.gmail_credentials_encrypted),
                accounts,
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
                [[("🔄 Перевірити ще раз", "a:latest_code")], BACK],
            )
            return
        code, login, game, received_at = result
        await render(
            callback,
            "🔑 <b>Останній код Steam Guard</b>\n\n"
            f"🎮 {escape(game)}\n"
            f"👤 <code>{escape(login)}</code>\n"
            f"🔐 <code>{escape(code)}</code>\n"
            f"🕒 {received_at.astimezone(ZoneInfo('Europe/Kyiv')):%d.%m.%Y · %H:%M}",
            [[("🔄 Оновити", "a:latest_code")], BACK],
        )

    @router.callback_query(F.data.regexp(r"^a:paymode:(mono|deepseek)$"))
    async def payment_mode_set(callback, session):
        mode = callback.data.rsplit(":", 1)[1]
        if mode == "deepseek" and not await session.scalar(
            select(PaymentCard.id).where(PaymentCard.active.is_(True)).limit(1)
        ):
            await callback.message.answer("Спочатку додайте хоча б одну активну картку.")
            return
        await set_setting(session, "payment_mode", mode)
        audit(session, callback.from_user.id, "payment_mode", mode)
        await session.commit()
        await render(callback, "Режим оплати змінено. Він діятиме для нових замовлень.", [BACK])

    @router.callback_query(F.data == "a:cards")
    async def cards(callback, session, shop):
        items = (await session.scalars(select(PaymentCard).order_by(PaymentCard.id))).all()
        active = sum(card.active for card in items)
        mono_card = await configured_manual_card(session, shop)
        rows = [[(f"🏦 Monobank •••• {mono_card[-4:] if mono_card else 'не задано'}", "a:mono_card")]]
        rows += [
            [(f"{'🟢' if c.active else '⚫'} {c.label} •••• {c.last4}", f"a:card:{c.id}")] for c in items
        ]
        if active < 2:
            rows.append([("➕ Додати картку", "a:card_add")])
        rows.append(BACK)
        await render(callback, f"Картка Monobank та картки для DeepSeek. Активних DeepSeek: {active}/2", rows)

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
        if receipt.status != "manual_review":
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
        if field in ("featured", "on_home", "gmail_credentials_encrypted"):
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
        if (await state.get_data()).get("field") in ("featured", "on_home"):
            await accept(callback, state, shop, callback.data.endswith(":1"))

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
    async def products(callback, session):
        _, kind, page = callback.data.split(":")
        query = select(Product).where(Product.deleted_at.is_(None))
        if kind == "featured":
            query = query.where(Product.on_home.is_(True))
        total = await session.scalar(select(func.count()).select_from(query.subquery()))
        page = min(int(page), max(0, (total - 1) // 7))
        items = (
            await session.scalars(
                query.order_by(Product.featured_position, Product.id).offset(page * 7).limit(7)
            )
        ).all()
        rows = [
            [(("👁 " if p.visible else "🙈 ") + p.name_ua + " — " + money(p.price), f"a:product:{p.id}")]
            for p in items
        ]
        rows += [pagination("a:" + kind, page, total, 7)]
        if kind == "featured":
            rows += [[("➕ Додати гру на головну", "a:products:0")]]
        await render(callback, "Товари" if items else "Товарів немає", rows + [BACK])

    @router.callback_query(F.data.regexp(r"^a:product:\d+$"))
    async def product(callback, session):
        p = await session.get(Product, int(callback.data.split(":")[2]))
        if not p:
            return
        rows = [
            [(label, f"a:edit:{p.id}:{field}")]
            for field, label in LABELS.items()
            if field not in ("featured", "on_home")
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
            BACK,
        ]
        await render(callback, product_text(p, "ua"), rows, photo=p.image_file_id)

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
            f"#{o.id}\n{escape(o.product_name_snapshot)}\n{money(o.price_snapshot)}\n"
            f"@{escape(u.username or '—')} / {u.id}\n{o.status}\n{o.created_at} UTC\n"
            f"Invoice: {escape(o.mono_invoice_id or '—')}\n"
            f"Видача потребує перевірки: {o.delivery_uncertain}",
            [BACK],
        )

    @router.callback_query(F.data.regexp(r"^a:users:\d+$"))
    async def users(callback, session):
        total = await session.scalar(select(func.count()).select_from(User))
        page = min(int(callback.data.split(":")[2]), max(0, (total - 1) // 7))
        items = (
            await session.scalars(select(User).order_by(User.created_at.desc()).offset(page * 7).limit(7))
        ).all()
        rows = [[(u.username or u.first_name or str(u.id), f"a:user:{u.id}")] for u in items]
        await render(callback, "Користувачі", rows + [pagination("a:users", page, total, 7), BACK])

    @router.callback_query(F.data.regexp(r"^a:user:\d+$"))
    async def user_detail(callback, session):
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
        await render(
            callback,
            f"@{escape(u.username or '—')} / {u.id}\n{escape(u.first_name)}\n"
            f"Мова: {u.language}\nПерший запуск: {u.created_at}\nАктивність: {u.last_activity_at}\n"
            f"Покупок: {count}\nВитрачено: {money(spent)}\nОстання покупка: {last or '—'}",
            [[("🛒 Покупки / список ігор", f"a:orders:{u.id}:0")], BACK],
        )

    @router.callback_query(F.data == "a:stats")
    async def stats(callback, session, shop):
        users = await session.scalar(select(func.count()).select_from(User))
        buyers, sales, revenue = (
            await session.execute(
                select(
                    func.count(func.distinct(Order.user_id)),
                    func.count(),
                    func.coalesce(func.sum(Order.price_snapshot), 0),
                ).where(Order.status.in_(SUCCESS))
            )
        ).one()
        active = await session.scalar(
            select(func.count())
            .select_from(Product)
            .where(Product.visible.is_(True), Product.deleted_at.is_(None))
        )
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
                        Order.status.in_(SUCCESS), Order.paid_at >= start
                    )
                )
            ).one()
            text += f"\n{icon} {label}: <b>{count}</b> · <b>{money(amount)}</b>"
        top = (
            await session.execute(
                select(Product.name_ua, func.count(Order.id))
                .join(Order, Order.product_id == Product.id)
                .where(Order.status.in_(SUCCESS))
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
        await render(callback, text, [BACK])

    @router.callback_query(F.data == "a:settings")
    async def settings(callback, session):
        enabled = await setting(session, "enabled", "true")
        await render(
            callback,
            "Налаштування. Monobank використовує merchant acquiring token.",
            [[(label, "a:setting:" + key)] for key, label in SETTINGS.items()]
            + [[("Магазин: " + ("🟢" if enabled == "true" else "🔴"), "a:enabled")], BACK],
        )

    @router.callback_query(F.data == "a:enabled")
    async def enabled(callback, session):
        await set_setting(
            session, "enabled", "false" if await setting(session, "enabled", "true") == "true" else "true"
        )
        audit(session, callback.from_user.id, "shop_toggle", "enabled")
        await session.commit()
        await render(callback, "Режим магазину змінено.", [[("⚙️ Налаштування", "a:settings")], BACK])

    @router.callback_query(F.data.startswith("a:setting:"))
    async def setting_start(callback, state):
        key = callback.data.split(":")[2]
        if key not in SETTINGS:
            return
        await state.set_state(Form.setting)
        await state.set_data({"key": key})
        await render(callback, "Введіть: " + SETTINGS[key], [BACK])

    @router.message(Form.setting)
    async def setting_input(message, state, shop):
        key = (await state.get_data())["key"]
        value = (message.text or "").strip()
        if not value or len(value) > 2000:
            await message.answer("Введіть від 1 до 2000 символів.")
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
            [[("✅ Зберегти", "a:save_setting")], [("❌ Скасувати", "a:home")]],
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
        await state.clear()
        await render(callback, "Налаштування збережено.", [BACK])

    @router.callback_query(F.data == "a:broadcast")
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
