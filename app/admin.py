import re
import secrets
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from html import escape
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.filters import Command, Filter
from aiogram.fsm.state import State, StatesGroup
from sqlalchemy import func, select

from app.access import is_admin
from app.i18n import money
from app.models import Broadcast, Order, Product, User, now
from app.services import SUCCESS, audit, set_setting, setting
from app.ui import pagination, product_text, render


class AdminOnly(Filter):
    async def __call__(self, event, shop):
        return is_admin(shop.cfg, event.from_user.id)


class Form(StatesGroup):
    product = State()
    edit = State()
    setting = State()
    broadcast = State()


FIELDS = [
    "name_ua",
    "price",
    "image_file_id",
    "description_ua",
    "steam_login_encrypted",
    "steam_password_encrypted",
    "gmail_credentials_encrypted",
    "featured",
]
LABELS = {
    "name_ua": "Назва товару",
    "price": "Ціна у гривнях (наприклад 599.50)",
    "image_file_id": "Фото",
    "description_ua": "Опис",
    "steam_login_encrypted": "Steam Login",
    "steam_password_encrypted": "Steam Password",
    "gmail_credentials_encrypted": "Gmail OAuth",
    "featured": "Показувати на головній?",
}
OPTIONAL = {"image_file_id", "description_ua", "gmail_credentials_encrypted"}
MENU = [
    [("➕ Додати товар", "a:add"), ("📦 Товари", "a:products:0")],
    [("🧾 Замовлення", "a:orders:all:0"), ("👥 Користувачі", "a:users:0")],
    [("🔥 Головна сторінка", "a:featured:0"), ("📊 Статистика", "a:stats")],
    [("📢 Розсилка", "a:broadcast"), ("⚙️ Налаштування", "a:settings")],
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
    if field == "featured":
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
    @router.callback_query(F.data == "a:home")
    async def home(event, state, shop):
        await state.clear()
        await render(event, "Керування магазином", MENU)

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
        if field in ("featured", "gmail_credentials_encrypted"):
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
        if (await state.get_data()).get("field") == "featured":
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
            query = query.where(Product.featured.is_(True))
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
        rows = [[(label, f"a:edit:{p.id}:{field}")] for field, label in LABELS.items() if field != "featured"]
        rows += [
            [
                ("🔥 Головна: " + str(p.featured), f"a:toggle:{p.id}:featured"),
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
        if field not in ("featured", "visible"):
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
                .where(Product.featured.is_(True), Product.deleted_at.is_(None))
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
        p.deleted_at, p.visible, p.featured = now(), False, False
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
        text = f"📊 Статистика\nКористувачів: {users}\nПокупців: {buyers}\nПродажів: {sales}\nВиручка: {money(revenue)}\nАктивних товарів: {active}\n"
        local = now().astimezone(ZoneInfo(shop.cfg.timezone))
        for label, start in [
            ("Сьогодні", local.replace(hour=0, minute=0, second=0, microsecond=0)),
            ("7 днів", now() - timedelta(days=7)),
            ("30 днів", now() - timedelta(days=30)),
        ]:
            count, amount = (
                await session.execute(
                    select(func.count(), func.coalesce(func.sum(Order.price_snapshot), 0)).where(
                        Order.status.in_(SUCCESS), Order.paid_at >= start
                    )
                )
            ).one()
            text += f"\n{label}: {count} / {money(amount)}"
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
        text += "\n\nНайпопулярніші:\n" + "\n".join(f"{escape(name)}: {count}" for name, count in top)
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
        total = await session.scalar(select(func.count()).select_from(User).where(User.blocked.is_(False)))
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
