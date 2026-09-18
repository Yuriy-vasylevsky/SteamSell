"""Only authenticated statement responses can confirm a personal transfer."""

import asyncio
import hashlib
import logging
import time
from datetime import timedelta
from urllib.parse import quote

from sqlalchemy import select

from app.models import Order, PaymentEvent, now
from app.services import PAYMENT_AMOUNT_TOLERANCE_KOPECKS, aware, configured_manual_card

log = logging.getLogger(__name__)


async def resolve_account(client, token, card):
    response = await client.get("https://api.monobank.ua/personal/client-info", headers={"X-Token": token})
    response.raise_for_status()
    matches = [
        a
        for a in response.json().get("accounts", [])
        if a.get("currencyCode") == 980
        and any(
            len(mask) == len(card) and all(m == "*" or m == c for m, c in zip(mask, card))
            for mask in a.get("maskedPan", [])
        )
    ]
    if len(matches) != 1:
        raise ValueError("card_account_not_unique")
    return matches[0]["id"]


async def apply_statement(shop, account, entries, order_id=None):
    async with shop.sessions() as session, session.begin():
        orders = (
            await session.scalars(
                select(Order)
                .where(
                    Order.payment_method == "personal",
                    Order.status == "waiting_payment",
                    Order.created_at >= now() - timedelta(days=30),
                    Order.id == order_id if order_id else True,
                )
                .order_by(Order.created_at, Order.id)
                .with_for_update()
            )
        ).all()
        for item in entries:
            if (
                not isinstance(item.get("id"), str)
                or not item["id"]
                or type(item.get("amount")) is not int
                or item["amount"] <= 0
                or item.get("currencyCode") != 980
                or type(item.get("time")) is not int
            ):
                continue
            matches = [
                o
                for o in orders
                if o.status == "waiting_payment"
                and abs(item["amount"] - o.price_snapshot) <= PAYMENT_AMOUNT_TOLERANCE_KOPECKS
                and item["time"] >= int(aware(o.created_at).timestamp())
                and item["time"] <= int(now().timestamp())
            ]
            if not matches:
                continue
            digest = hashlib.sha256(("personal:" + account + ":" + item["id"]).encode()).hexdigest()
            if await session.scalar(select(PaymentEvent.id).where(PaymentEvent.digest == digest)):
                continue
            order = matches[0]
            session.add(PaymentEvent(digest=digest, invoice_id=item["id"], status="success", outcome="paid"))
            order.status, order.paid_at = "paid", now()
            log.info("personal_payment_confirmed order=%s", order.id)


async def reconcile_personal(shop, order_id):
    if not hasattr(shop, "personal_lock"):
        shop.personal_lock = asyncio.Lock()
    async with shop.personal_lock:
        return await _reconcile_personal(shop, order_id)


async def _reconcile_personal(shop, order_id):
    async with shop.sessions() as session:
        card = await configured_manual_card(session, shop)
    if not card or time.monotonic() < getattr(shop, "personal_next", 0):
        return "cooldown"
    shop.personal_next = time.monotonic() + 20
    try:
        async with shop.sessions() as session:
            pending = await session.scalar(
                select(Order)
                .where(
                    Order.payment_method == "personal",
                    Order.status == "waiting_payment",
                    Order.created_at >= now() - timedelta(days=30),
                    Order.id == order_id,
                )
                .order_by(Order.created_at)
                .limit(1)
            )
        if not pending:
            return "empty"
        token = shop.cfg.mono_token.get_secret_value()
        if not getattr(shop, "personal_account", None):
            shop.personal_account = await resolve_account(shop.mono.client, token, card)
        account = shop.personal_account
        # Page backwards across dense statements without skipping the oldest transfer.
        cursors = getattr(shop, "personal_cursors", {})
        end = cursors.get(order_id) or int(now().timestamp())
        start = int(aware(pending.created_at).timestamp())
        if end < start:
            end = int(now().timestamp())
        response = await shop.mono.client.get(
            f"https://api.monobank.ua/personal/statement/{quote(account, safe='')}/{start}/{end}",
            headers={"X-Token": token},
        )
        response.raise_for_status()
        entries = response.json()
        await apply_statement(shop, account, entries, order_id)
        cursors[order_id] = min(i["time"] for i in entries) if len(entries) >= 500 else None
        shop.personal_cursors = cursors
        return "checked"
    except Exception:
        log.warning("personal_statement_check_failed")
        return "error"
