import asyncio
import logging
from datetime import timedelta
from html import escape
from zoneinfo import ZoneInfo

from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import MessageEntity
from sqlalchemy import select

from app.access import all_admin_ids, is_admin
from app.i18n import money, tr
from app.models import Broadcast, Order, Product, User, now
from app.services import (
    aware,
    cleanup_expired_promo_codes,
    code_request_window_open,
    ensure_reward_promo,
    setting,
)
from app.ui import (
    back,
    keyboard,
    manual_delivery_text,
    persistent_menu,
    purchase_rows,
    purchase_text,
    user_reference,
)

log = logging.getLogger(__name__)


async def deliver_one(shop, bot):
    async with shop.sessions() as session, session.begin():
        # Durable claim before external I/O; an ambiguous send is never blindly retried.
        order = await session.scalar(
            select(Order)
            .where(
                Order.status == "paid",
                Order.delivery_claimed_at.is_(None),
                Order.delivery_uncertain.is_(False),
            )
            .order_by(Order.paid_at)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if not order:
            return False
        order.delivery_claimed_at = now()
        order_id = order.id
        user = await session.get(User, order.user_id)
        product = await session.get(Product, order.product_id)
        reward_promo = await ensure_reward_promo(session, order, product)
        lang = user.language or "ua"
        support = await setting(session, "support_username", shop.cfg.support_username)
        if order.delivery_mode_snapshot == "manual":
            text = manual_delivery_text(order, lang, reward_promo)
            markup = keyboard([[(tr("support", lang), "https://t.me/" + support.lstrip("@"))]])
        else:
            gmail_connected = bool(
                product.gmail_credentials_encrypted and code_request_window_open(order)
            )
            text = purchase_text(order, product, lang, shop.vault, reward_promo)
            markup = keyboard(
                purchase_rows(
                    order,
                    lang,
                    support,
                    gmail_connected,
                    product.code_limit,
                    code_request_available=code_request_window_open(order),
                    return_target=f"purchase:{order.id}",
                )
            )
    try:
        message = await bot.send_message(
            user.id, text, parse_mode="HTML", reply_markup=markup
        )
    except TelegramRetryAfter as error:
        await asyncio.sleep(error.retry_after)
        async with shop.sessions() as session, session.begin():
            order = await session.get(Order, order_id)
            order.delivery_claimed_at = None
        return True
    except Exception:
        async with shop.sessions() as session, session.begin():
            order = await session.get(Order, order_id)
            order.delivery_uncertain = True
        log.warning("delivery_needs_review order=%s", order_id)
        return True
    async with shop.sessions() as session, session.begin():
        order = await session.get(Order, order_id)
        order.status, order.delivered_at = "delivered", now()
        order.delivery_message_id = message.message_id
        payment_message_id = order.payment_message_id
    if payment_message_id:
        try:
            await bot.edit_message_text(
                tr("manual_delivery", lang)
                if order.delivery_mode_snapshot == "manual"
                else tr("payment_confirmed", lang),
                chat_id=user.id,
                message_id=payment_message_id,
                reply_markup=keyboard([[back(lang, f"purchase:{order_id}")[0]]]),
            )
        except Exception:
            log.warning("payment_confirmation_edit_failed order=%s", order_id)
    log.info("order_delivered order=%s", order_id)
    return True


async def notify_admin(shop, bot):
    async with shop.sessions() as session:
        info_notifications_enabled = (
            await setting(session, "admin_info_notifications", "true") == "true"
        )
        items = (
            await session.scalars(
                select(Order)
                .where(
                    Order.status.in_(("paid", "delivered")),
                    Order.admin_notified.is_(False),
                )
                .limit(20)
            )
        ).all()
        for order in items:
            if order.status == "paid" and not order.delivery_uncertain:
                if order.delivery_claimed_at and now() - aware(order.delivery_claimed_at) > timedelta(
                    minutes=5
                ):
                    order.delivery_uncertain = True
                else:
                    continue
            requires_admin_action = (
                order.delivery_mode_snapshot == "manual"
                or order.status != "delivered"
                or order.delivery_uncertain
            )
            if not info_notifications_enabled and not requires_admin_action:
                order.admin_notified = True
                await session.commit()
                continue
            user = await session.get(User, order.user_id)
            status = (
                "👤 Очікується ручна видача адміністратором."
                if order.delivery_mode_snapshot == "manual"
                else
                "📦 Дані видано"
                if order.status == "delivered"
                else "⚠️ Видача потребує перевірки; покупка доступна у «Мої покупки»."
            )
            try:
                paid_at = aware(order.paid_at).astimezone(ZoneInfo("Europe/Kyiv"))
                text = (
                    "💰 <b>Нова покупка</b>\n\n"
                    f"🎮 {escape(order.product_name_snapshot)}\n"
                    f"💰 До сплати: <b>{money(order.price_snapshot)}</b>\n"
                    f"🎟 Промокод: {escape(order.promo_code_snapshot or '—')}\n"
                    f"👤 {user_reference(user)}\n\n"
                    f"{status}\n"
                    f"🕒 {paid_at:%d.%m.%Y · %H:%M}"
                )
                for admin_id in await all_admin_ids(session, shop.cfg):
                    await bot.send_message(admin_id, text, parse_mode="HTML")
                order.admin_notified = True
                await session.commit()
            except Exception:
                log.warning("admin_notification_failed order=%s", order.id)


async def broadcast_batch(shop, bot):
    async with shop.sessions() as session:
        job = await session.scalar(
            select(Broadcast).where(Broadcast.status == "queued").order_by(Broadcast.id).limit(1)
        )
        if not job:
            return
        payload = job.payload
        menu_refresh = payload.get("type") == "menu_refresh"
        user_query = select(User).where(
            User.id > job.last_user_id,
            User.blocked.is_(False),
            User.created_at <= job.created_at,
        )
        if not menu_refresh:
            user_query = user_query.where(User.broadcast_subscribed.is_(True))
        users = (await session.scalars(user_query.order_by(User.id).limit(20))).all()
        loyalty_enabled = (
            await setting(session, "loyalty_enabled", "true") == "true"
            and await setting(session, "sale_enabled", "false") != "true"
        )
        for user in users:
            try:
                text = payload.get("texts", {}).get(user.language or "ua", payload.get("text", ""))
                if menu_refresh:
                    await bot.send_message(
                        user.id,
                        "\u2063",
                        reply_markup=persistent_menu(
                            user.language or "ua",
                            is_admin(shop.cfg, user.id, user),
                            user.broadcast_subscribed,
                            loyalty_enabled,
                        ),
                    )
                elif payload["photo"]:
                    entities = [MessageEntity.model_validate(e) for e in payload["entities"]]
                    await bot.send_photo(user.id, payload["photo"], caption=text, caption_entities=entities)
                else:
                    entities = [MessageEntity.model_validate(e) for e in payload["entities"]]
                    await bot.send_message(user.id, text, entities=entities)
                job.delivered += 1
            except TelegramForbiddenError:
                job.blocked += 1
                user.blocked = True
            except TelegramRetryAfter as error:
                await session.commit()
                await asyncio.sleep(error.retry_after)
                return
            except Exception:
                job.errors += 1
            job.last_user_id = user.id
            await session.commit()
            await asyncio.sleep(0.1)
        if not users:
            job.status = "completed"
            await session.commit()
            result_title = "Оновлення меню" if menu_refresh else "Розсилка"
            await bot.send_message(
                shop.cfg.admin_id,
                f"{result_title} #{job.id} завершено.\n"
                f"Доставлено: {job.delivered}\nЗаблокували: {job.blocked}\nПомилки: {job.errors}",
            )


async def worker(shop, bot):
    iteration = 0
    while True:
        try:
            for _ in range(20):
                if not await deliver_one(shop, bot):
                    break
            await notify_admin(shop, bot)
            await broadcast_batch(shop, bot)
            if iteration % 30 == 0:
                await shop.reconcile()
                async with shop.sessions() as session, session.begin():
                    deleted_promos = await cleanup_expired_promo_codes(session)
                if deleted_promos:
                    log.info("expired_promo_codes_deleted count=%s", deleted_promos)
            iteration += 1
        except asyncio.CancelledError:
            raise
        except Exception:
            log.error("worker_iteration_failed")
        await asyncio.sleep(2)
