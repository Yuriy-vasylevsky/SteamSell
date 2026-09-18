import base64
import time

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec


def verify_signature(body: bytes, signature: str, public_key: str) -> bool:
    try:
        key = serialization.load_pem_public_key(base64.b64decode(public_key, validate=True))
        if not isinstance(key, ec.EllipticCurvePublicKey):
            return False
        key.verify(base64.b64decode(signature, validate=True), body, ec.ECDSA(hashes.SHA256()))
        return True
    except (ValueError, InvalidSignature, TypeError):
        return False


class Monobank:
    def __init__(self, token, client: httpx.AsyncClient):
        self.token = token
        self.client = client
        self.cached_key = None
        self.key_at = 0

    async def request(self, method, path, **kwargs):
        response = await self.client.request(
            method,
            "https://api.monobank.ua/api/merchant/" + path,
            headers={"X-Token": self.token, "X-Cms": "SteamSell"},
            **kwargs,
        )
        response.raise_for_status()
        return response.json()

    async def create(self, order, webhook):
        return await self.request(
            "POST",
            "invoice/create",
            json={
                "amount": order.price_snapshot,
                "ccy": 980,
                "validity": 3600,
                "paymentType": "debit",
                "webHookUrl": webhook,
                "merchantPaymInfo": {"reference": order.id, "destination": order.product_name_snapshot},
            },
        )

    async def status(self, invoice):
        return await self.request("GET", "invoice/status", params={"invoiceId": invoice})

    async def verify(self, body, signature):
        if not signature:
            return False
        if not self.cached_key or time.monotonic() - self.key_at > 3600:
            self.cached_key = (await self.request("GET", "pubkey"))["key"]
            self.key_at = time.monotonic()
        return verify_signature(body, signature, self.cached_key)
