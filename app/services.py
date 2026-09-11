import hashlib
import json
import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from app.models import (
    AdminLog,
    LoyaltyLevel,
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
TIP_PERCENTS = (0, 5, 10, 20)


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


def apply_discount(price_kopecks: int, discount_percent: int) -> int:
    if discount_percent <= 0:
        return price_kopecks
    # Round the discounted amount to the nearest whole hryvnia (half up).
    discounted_hryvnias = (
        price_kopecks * (100 - discount_percent) + 5_000
    ) // 10_000
    return max(100, discounted_hryvnias * 100)


def tip_amount(price_kopecks: int, tip_percent: int) -> int:
    if tip_percent not in TIP_PERCENTS:
        raise ValueError("invalid_tip_percent")
    return (price_kopecks * tip_percent + 50) // 100


def price_with_tip(price_kopecks: int, tip_percent: int) -> int:
    total = price_kopecks + tip_amount(price_kopecks, tip_percent)
    return ((total + 50) // 100) * 100 if tip_percent else total


async def loyalty_status(session, user_id):
    enabled = await setting(session, "loyalty_enabled", "true") == "true"
    spent = await session.scalar(
        select(func.coalesce(func.sum(Order.price_snapshot), 0)).where(
            Order.user_id == user_id,
            Order.status.in_(SUCCESS),
        )
    )
    levels = (
        await session.scalars(select(LoyaltyLevel).order_by(LoyaltyLevel.threshold_kopecks))
    ).all()
    current = max(
        (level for level in levels if level.threshold_kopecks <= spent),
        key=lambda level: level.threshold_kopecks,
        default=None,
    )
    next_level = next((level for level in levels if level.threshold_kopecks > spent), None)
    discount = current.discount_percent if enabled and current else 0
    return {
        "enabled": enabled,
        "spent": spent,
        "levels": levels,
        "current": current,
        "next": next_level,
        "discount_percent": discount,
    }


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


async def release_stock(session, product_id):
    product = await session.get(Product, product_id, with_for_update=True)
    if product and product.stock_quantity is not None:
        product.stock_quantity += 1


class Shop:
    def __init__(self, cfg, sessions, vault, mono, gmail, redis):
        self.cfg, self.sessions, self.vault = cfg, sessions, vault
        self.mono, self.gmail, self.redis = mono, gmail, redis

    async def checkout(self, user_id, product_id, payment_choice=None, tip_percent=0):
        if tip_percent not in TIP_PERCENTS:
            raise ShopError("missing")
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
            if payment_mode == "hybrid":
                if payment_choice not in {"mono", "deepseek"}:
                    raise ShopError("missing")
                effective_mode = payment_choice
            else:
                if payment_choice is not None and payment_choice != payment_mode:
                    raise ShopError("missing")
                effective_mode = payment_mode
            target_method = (
                "receipt" if effective_mode == "deepseek" else ("personal" if card else "acquiring")
            )
            cards = []
            receipt_iban = ""
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
                encrypted_iban = await setting(session, "receipt_iban", "")
                if encrypted_iban:
                    try:
                        receipt_iban = self.vault.decrypt(encrypted_iban)
                    except Exception:
                        log.warning("receipt_iban_setting_invalid")
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
                    Order.tip_percent_snapshot == tip_percent,
                    (Order.payment_method.in_(("personal", "receipt"))) | Order.payment_url.is_not(None),
                )
                .order_by(Order.created_at.desc())
                .limit(1)
            )
            if existing:
                return existing
            product = await session.get(Product, product_id, with_for_update=True)
            if product.stock_quantity is not None:
                if product.stock_quantity <= 0:
                    raise ShopError("missing")
                product.stock_quantity -= 1
            loyalty = await loyalty_status(session, user_id)
            discount_percent = loyalty["discount_percent"]
            product_price = apply_discount(product.price, discount_percent)
            order = Order(
                user_id=user_id,
                product_id=product_id,
                product_name_snapshot=getattr(product, "name_" + (user.language or "ua")),
                price_snapshot=price_with_tip(product_price, tip_percent),
                original_price_snapshot=product.price,
                discount_percent_snapshot=discount_percent,
                tip_percent_snapshot=tip_percent,
                delivery_mode_snapshot=product.delivery_mode,
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
                            ],
                            "ibans": [receipt_iban] if receipt_iban else [],
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
                        await release_stock(session, order.product_id)
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
                await release_stock(session, order.product_id)
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
            await release_stock(session, order.product_id)
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
            if found_count >= product.code_limit:
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
