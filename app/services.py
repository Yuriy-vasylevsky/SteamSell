import hashlib
import json
import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from app.models import (
    AdminLog,
    MailCodeRequest,
    Order,
    PaymentCard,
    PaymentEvent,
    PaymentReceipt,
    Product,
    Setting,
    User,
    now,
)

log = logging.getLogger(__name__)
SUCCESS = ("paid", "delivered")


class ShopError(Exception):
    pass


def aware(value):
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


async def setting(session, key, default=""):
    row = await session.get(Setting, key)
    return row.value if row else default


async def configured_manual_card(session, shop):
    encrypted = await setting(session, "manual_card", "")
    if encrypted:
        try:
            return shop.vault.decrypt(encrypted)
        except Exception:
            log.warning("manual_card_setting_invalid")
    return getattr(shop.cfg, "manual_card", "")


async def shared_gmail_credentials(session, shop):
    encrypted = await session.scalar(
        select(Product.gmail_credentials_encrypted)
        .where(
            Product.gmail_credentials_encrypted.is_not(None),
            Product.deleted_at.is_(None),
        )
        .order_by(Product.id.desc())
        .limit(1)
    )
    return shop.vault.unpack(encrypted) if encrypted else None


async def set_setting(session, key, value):
    row = await session.get(Setting, key)
    if row:
        row.value = value
    else:
        session.add(Setting(key=key, value=value))


async def has_recent_purchase(session, user_id, order_id, checked_at, minutes=12):
    checked_at = aware(checked_at)
    return bool(
        await session.scalar(
            select(Order.id)
            .where(
                Order.user_id == user_id,
                Order.id != order_id,
                Order.status.in_(SUCCESS),
                Order.paid_at.is_not(None),
                Order.paid_at >= checked_at - timedelta(minutes=minutes),
                Order.paid_at <= checked_at,
            )
            .limit(1)
        )
    )


def audit(session, admin_id, action, target):
    session.add(AdminLog(admin_id=admin_id, action=action, target=str(target)))


class Shop:
    def __init__(self, cfg, sessions, vault, mono, gmail, redis):
        self.cfg, self.sessions, self.vault = cfg, sessions, vault
        self.mono, self.gmail, self.redis = mono, gmail, redis

    async def checkout(self, user_id, product_id):
        async with self.sessions() as session:
            if await setting(session, "enabled", "true") != "true":
                raise ShopError("maintenance")
            pending_receipt = await session.scalar(
                select(PaymentReceipt.id)
                .join(Order, Order.id == PaymentReceipt.order_id)
                .where(
                    Order.user_id == user_id,
                    PaymentReceipt.status.in_(("analyzing", "manual_review")),
                )
                .limit(1)
            )
            if pending_receipt:
                raise ShopError("receipt_pending")
            product = await session.get(Product, product_id)
            if not product or not product.visible or product.deleted_at:
                raise ShopError("missing")
            card = await configured_manual_card(session, self)
            payment_mode = await setting(session, "payment_mode", "mono")
            target_method = "receipt" if payment_mode == "deepseek" else ("personal" if card else "acquiring")
            cards = []
            if target_method == "receipt":
                cards = (
                    await session.scalars(
                        select(PaymentCard)
                        .where(PaymentCard.active.is_(True))
                        .order_by(PaymentCard.id)
                        .limit(2)
                    )
                ).all()
                if not cards:
                    raise ShopError("payment_unavailable")
            user = await session.get(User, user_id)
            # Repeated clicks return the same active invoice.
            existing = await session.scalar(
                select(Order)
                .where(
                    Order.user_id == user_id,
                    Order.product_id == product_id,
                    Order.status == "waiting_payment",
                    Order.created_at > now() - timedelta(minutes=55),
                    Order.payment_method == target_method,
                    (Order.payment_method.in_(("personal", "receipt"))) | Order.payment_url.is_not(None),
                )
                .order_by(Order.created_at.desc())
                .limit(1)
            )
            if existing:
                return existing
            order = Order(
                user_id=user_id,
                product_id=product_id,
                product_name_snapshot=getattr(product, "name_" + (user.language or "ua")),
                price_snapshot=product.price,
                payment_method=target_method,
                payment_cards_encrypted=(
                    self.vault.pack(
                        {
                            "cards": [
                                {
                                    "label": c.label,
                                    "number": self.vault.decrypt(c.number_encrypted),
                                    "last4": c.last4,
                                }
                                for c in cards
                            ]
                        }
                    )
                    if cards
                    else None
                ),
            )
            session.add(order)
            await session.commit()
            if target_method == "acquiring":
                try:
                    invoice = await self.mono.create(order, self.cfg.public_base_url + "/webhooks/monobank")
                    order.mono_invoice_id = invoice["invoiceId"]
                    order.payment_url = invoice["pageUrl"]
                    if not order.payment_url.startswith("https://"):
                        raise ValueError("invalid_payment_url")
                    await session.commit()
                except Exception:
                    await session.refresh(order)
                    if order.status not in SUCCESS:
                        order.status = "payment_failed"
                    await session.commit()
                    raise ShopError("error") from None
            return order

    async def payment(self, payload, digest):
        invoice = payload.get("invoiceId")
        if not isinstance(invoice, str) or len(invoice) > 128:
            raise ValueError("invalid_invoice")
        async with self.sessions() as session, session.begin():
            order = await session.scalar(
                select(Order).where(Order.mono_invoice_id == invoice).with_for_update()
            )
            if not order:
                reference = payload.get("reference")
                if isinstance(reference, str):
                    order = await session.scalar(
                        select(Order)
                        .where(
                            Order.id == reference,
                            Order.mono_invoice_id.is_(None),
                        )
                        .with_for_update()
                    )
                if not order:
                    raise LookupError("unknown_invoice")
            if order.payment_method != "acquiring":
                return
            if await session.scalar(select(PaymentEvent.id).where(PaymentEvent.digest == digest)):
                return
            status = payload.get("status", "")
            event = PaymentEvent(
                digest=digest, invoice_id=invoice, status=str(status)[:32], outcome="ignored"
            )
            session.add(event)
            if (
                type(payload.get("amount")) is not int
                or payload["amount"] != order.price_snapshot
                or payload.get("ccy") != 980
                or payload.get("reference") != order.id
            ):
                event.outcome = "mismatch"
                log.warning("payment_mismatch order=%s", order.id)
                return
            # Also recovers a signed callback after invoice/create timed out locally.
            order.mono_invoice_id = invoice
            try:
                modified = datetime.fromisoformat(payload["modifiedDate"].replace("Z", "+00:00"))
                if modified.tzinfo is None:
                    raise ValueError()
            except (KeyError, ValueError, TypeError):
                event.outcome = "invalid_date"
                return
            if order.provider_modified_at and modified < aware(order.provider_modified_at):
                event.outcome = "stale"
                return
            order.provider_modified_at = modified
            if status == "success" and order.status not in SUCCESS:
                order.status = "paid"
                order.paid_at = now()
                event.outcome = "paid"
                log.info("payment_confirmed order=%s", order.id)
            elif status in ("failure", "expired", "reversed") and order.status == "waiting_payment":
                order.status = "payment_failed"
                event.outcome = "failed"

    async def attach_payment_message(self, order_id, user_id, message_id):
        if not isinstance(message_id, int):
            return
        async with self.sessions() as session, session.begin():
            order = await session.get(Order, order_id)
            if order and order.user_id == user_id and order.status == "waiting_payment":
                order.payment_message_id = message_id

    async def cancel_payment(self, order_id, user_id):
        async with self.sessions() as session, session.begin():
            order = await session.get(Order, order_id, with_for_update=True)
            if not order or order.user_id != user_id or order.status != "waiting_payment":
                raise ShopError("missing")
            if await session.scalar(
                select(PaymentReceipt.id)
                .where(
                    PaymentReceipt.order_id == order.id,
                )
                .limit(1)
            ):
                raise ShopError("receipt_locked")
            order.status = "cancelled"
            receipts = (
                await session.scalars(
                    select(PaymentReceipt)
                    .where(
                        PaymentReceipt.order_id == order.id,
                        PaymentReceipt.status.in_(("analyzing", "manual_review")),
                    )
                    .with_for_update()
                )
            ).all()
            for receipt in receipts:
                receipt.status = "cancelled"
                receipt.reason = "Платіж скасовано покупцем"
            return order

    async def code(self, user_id, order_id):
        async with self.sessions() as session:
            order = await session.get(Order, order_id)
            if not order or order.user_id != user_id or order.status not in SUCCESS:
                raise ShopError("missing")
            product = await session.get(Product, order.product_id)
            found_count = await session.scalar(
                select(func.count())
                .select_from(MailCodeRequest)
                .where(
                    MailCodeRequest.order_id == order.id,
                    MailCodeRequest.outcome == "found",
                )
            )
            if found_count >= 3:
                raise ShopError("code_limit")
            request = MailCodeRequest(
                user_id=user_id,
                product_id=product.id,
                order_id=order.id,
                outcome="requested",
            )
            session.add(request)
            # Atomic cross-process throttle, both per user and per shared Steam account.
            allowed = await self.redis.eval(
                "if redis.call('EXISTS',KEYS[1])==1 or redis.call('EXISTS',KEYS[2])==1 then return 0 end "
                "redis.call('SET',KEYS[1],'1','EX',ARGV[1]); redis.call('SET',KEYS[2],'1','EX',ARGV[1]); return 1",
                2,
                f"code:user:{user_id}",
                f"code:product:{product.id}",
                self.cfg.code_cooldown,
            )
            if not allowed:
                request.outcome = "throttled"
                await session.commit()
                raise ShopError("cooldown")
            count = await session.scalar(
                select(func.count())
                .select_from(MailCodeRequest)
                .where(
                    MailCodeRequest.user_id == user_id,
                    MailCodeRequest.created_at > now() - timedelta(minutes=10),
                    MailCodeRequest.outcome != "throttled",
                )
            )
            if count > 10:
                request.outcome = "throttled"
                await session.commit()
                raise ShopError("cooldown")
            try:
                credentials = await shared_gmail_credentials(session, self)
                if not credentials:
                    raise ValueError("gmail_not_connected")
                # A buyer may return through "My purchases" after the original
                # three-minute polling window. Keep the login match strict, but
                # allow the same one-hour mailbox window as the admin lookup.
                earliest = max(now() - timedelta(hours=1), aware(order.paid_at))
                result = await self.gmail.latest_code(
                    credentials,
                    self.vault.decrypt(product.steam_login_encrypted),
                    earliest,
                )
                if result:
                    code, message_id = result
                    seen = await session.scalar(
                        select(MailCodeRequest.id).where(
                            MailCodeRequest.user_id == user_id,
                            MailCodeRequest.order_id == order.id,
                            MailCodeRequest.message_id == message_id,
                            MailCodeRequest.outcome == "found",
                        )
                    )
                    if not seen:
                        request.outcome, request.message_id = "found", message_id
                        await session.commit()
                        return code
                request.outcome = "not_found"
                await session.commit()
                raise ShopError("no_code")
            except ShopError:
                raise
            except Exception:
                request.outcome = "error"
                await session.commit()
                log.warning("gmail_request_failed product=%s", product.id)
                raise ShopError("error") from None

    async def reconcile(self):
        async with self.sessions() as session:
            orders = (
                await session.scalars(
                    select(Order)
                    .where(
                        Order.status == "waiting_payment",
                        Order.mono_invoice_id.is_not(None),
                        Order.created_at < now() - timedelta(minutes=2),
                    )
                    .order_by(Order.created_at)
                    .limit(100)
                )
            ).all()
        for order in orders:
            try:
                payload = await self.mono.status(order.mono_invoice_id)
                digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
                await self.payment(payload, digest)
            except Exception:
                log.warning("reconciliation_failed order=%s", order.id)
