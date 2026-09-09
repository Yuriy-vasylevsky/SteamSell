import asyncio
import logging
from datetime import timedelta
from html import escape

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import MessageEntity
from sqlalchemy import select

from app.i18n import money, tr
from app.models import Broadcast, Order, Product, User, now
from app.services import aware, setting
from app.ui import back, keyboard, payment_rows, payment_wait_text, purchase_rows, purchase_text

log = logging.getLogger(__name__)


async def animate_waiting_payments(shop, bot):
    cutoff = now() - timedelta(seconds=4)
    async with shop.sessions() as session:
        orders = (
            await session.scalars(
                select(Order)
                .where(
                    Order.status == "waiting_payment",
                    Order.payment_message_id.is_not(None),
                    (Order.payment_animation_at.is_(None) | (Order.payment_animation_at <= cutoff)),
                )
                .order_by(Order.payment_animation_at)
                .limit(20)
            )
        ).all()
        card = getattr(shop.cfg, "manual_card", "")
        for order in orders:
            user = await session.get(User, order.user_id)
            lang = user.language or "ua"
            order.payment_animation_step = (order.payment_animation_step + 1) % 5
            order.payment_animation_at = now()
            await session.commit()
            try:
                display_card = card
                if order.payment_method == "receipt" and order.payment_cards_encrypted:
                    display_card = "\n".join(
                        f"{item['label']}: {item['number']}"
                        for item in shop.vault.unpack(order.payment_cards_encrypted)["cards"]
                    )
                await bot.edit_message_text(
                    payment_wait_text(order, lang, order.payment_animation_step, display_card),
                    chat_id=order.user_id,
                    message_id=order.payment_message_id,
                    parse_mode="HTML",
                    reply_markup=keyboard(payment_rows(order, lang)),
                )
            except TelegramBadRequest:
                # The user may have deleted the payment message; stop editing it.
                order.payment_message_id = None
                await session.commit()
            except TelegramRetryAfter as error:
                await asyncio.sleep(error.retry_after)
                return
            except Exception:
                log.warning("payment_animation_failed order=%s", order.id)


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
        lang = user.language or "ua"
        support = await setting(session, "support_username", shop.cfg.support_username)
        text = purchase_text(order, product, lang, shop.vault)
        markup = keyboard(purchase_rows(order, lang, support, bool(product.gmail_credentials_encrypted)))
    try:
        message = await bot.send_message(
            user.id, text, parse_mode="HTML", reply_markup=markup, protect_content=True
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
                tr("payment_confirmed", lang),
                chat_id=user.id,
                message_id=payment_message_id,
                reply_markup=keyboard([[back(lang)[0]]]),
            )
        except Exception:
            log.warning("payment_confirmation_edit_failed order=%s", order_id)
    log.info("order_delivered order=%s", order_id)
    return True


async def notify_admin(shop, bot):
    async with shop.sessions() as session:
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
            user = await session.get(User, order.user_id)
            status = (
                "📦 Дані видано"
                if order.status == "delivered"
                else "⚠️ Видача потребує перевірки; покупка доступна у «Мої покупки»."
            )
            try:
                await bot.send_message(
                    shop.cfg.admin_id,
                    f"💰 Нова покупка\n{escape(order.product_name_snapshot)}\n{money(order.price_snapshot)}\n"
                    f"@{escape(user.username or '—')} / {user.id}\n✅ Оплату підтверджено\n{status}\n"
                    f"#{order.id}\n{order.paid_at} UTC",
                    parse_mode="HTML",
                )
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
        users = (
            await session.scalars(
                select(User)
                .where(User.id > job.last_user_id, User.blocked.is_(False), User.created_at <= job.created_at)
                .order_by(User.id)
                .limit(20)
            )
        ).all()
        for user in users:
            payload = job.payload
            entities = [MessageEntity.model_validate(e) for e in payload["entities"]]
            try:
                if payload["photo"]:
                    await bot.send_photo(
                        user.id, payload["photo"], caption=payload["text"], caption_entities=entities
                    )
                else:
                    await bot.send_message(user.id, payload["text"], entities=entities)
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
            await bot.send_message(
                shop.cfg.admin_id,
                f"Розсилка #{job.id} завершена.\n"
                f"Доставлено: {job.delivered}\nЗаблокували: {job.blocked}\nПомилки: {job.errors}",
            )


async def worker(shop, bot):
    iteration = 0
    while True:
        try:
            await animate_waiting_payments(shop, bot)
            for _ in range(20):
                if not await deliver_one(shop, bot):
                    break
            await notify_admin(shop, bot)
            await broadcast_batch(shop, bot)
            if iteration % 30 == 0:
                await shop.reconcile()
            iteration += 1
        except asyncio.CancelledError:
            raise
        except Exception:
            log.error("worker_iteration_failed")
        await asyncio.sleep(2)
