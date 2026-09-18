import json
from unittest.mock import AsyncMock

import httpx

from app.main import app


async def test_webhook_rejects_unsigned_body(shop):
    shop.mono.verify.return_value = False
    app.state.shop = shop
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://test") as client:
        response = await client.post("/webhooks/monobank", json={"status": "success"})
        assert response.status_code == 401


async def test_webhook_validated_then_processed(shop, payment):
    shop.mono.verify.return_value = True
    app.state.shop = shop
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://test") as client:
        response = await client.post(
            "/webhooks/monobank", content=json.dumps(payment), headers={"X-Sign": "test"}
        )
        assert response.status_code == 200
        response = await client.post("/webhooks/monobank", json=["wrong"])
        assert response.status_code == 400
        response = await client.post("/webhooks/monobank", content=b"a" * 65537)
        assert response.status_code == 413


async def test_oauth_state_is_single_use_and_secret_is_encrypted(shop):
    app.state.shop = shop
    shop.redis.getdel = AsyncMock(side_effect=["99", None])
    shop.gmail.exchange.return_value = {"email": "test@gmail.com", "refresh_token": "private-refresh"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://test") as client:
        response = await client.get("/oauth/gmail/callback", params={"state": "nonce", "code": "code"})
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        stored = shop.redis.set.call_args.args[1]
        assert shop.redis.set.call_args.args[0] == "gmail:draft:nonce"
        assert "private-refresh" not in stored
        assert shop.vault.unpack(stored)["refresh_token"] == "private-refresh"
        response = await client.get("/oauth/gmail/callback", params={"state": "nonce", "code": "code"})
        assert response.status_code == 400
        assert shop.gmail.exchange.await_count == 1
