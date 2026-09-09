import asyncio
import hashlib
import json
import logging
from contextlib import asynccontextmanager, suppress

import httpx
from aiogram import Bot
from aiogram.fsm.storage.redis import RedisEventIsolation, RedisStorage
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from redis.asyncio import Redis
from sqlalchemy import text

from app.access import is_admin
from app.bot import create_dispatcher
from app.config import config
from app.db import database
from app.gmail import Gmail
from app.monobank import Monobank
from app.security import Vault
from app.services import Shop, setting
from app.worker import worker

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app):
    cfg = config()
    engine, sessions = database(cfg.database_url)
    redis = Redis.from_url(cfg.redis_url, decode_responses=True)
    await redis.ping()
    vault = Vault(cfg.encryption_key.get_secret_value())
    client = httpx.AsyncClient(timeout=httpx.Timeout(20), follow_redirects=False)
    mono = Monobank(cfg.mono_token.get_secret_value(), client)
    async with sessions() as session:
        await session.execute(text("SELECT 1"))
        saved = await setting(session, "mono_token")
        if saved:
            mono.token = vault.decrypt(saved)
    shop = Shop(cfg, sessions, vault, mono, Gmail(cfg, client), redis)
    bot = Bot(cfg.bot_token.get_secret_value())
    storage = RedisStorage(redis, state_ttl=1800, data_ttl=1800)
    dp = create_dispatcher(shop, storage)
    dp.fsm.events_isolation = RedisEventIsolation(redis, lock_kwargs={"timeout": 120})
    app.state.shop, app.state.engine = shop, engine
    tasks = [
        asyncio.create_task(dp.start_polling(bot, handle_signals=False, close_bot_session=False)),
        asyncio.create_task(worker(shop, bot)),
    ]
    app.state.tasks = tasks
    log.info("shop_started")
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        for task in tasks:
            with suppress(asyncio.CancelledError):
                await task
        await bot.session.close()
        await client.aclose()
        await redis.aclose()
        await engine.dispose()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.get("/health")
async def health(request: Request):
    try:
        await request.app.state.shop.redis.ping()
        async with request.app.state.engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
        if any(task.done() for task in request.app.state.tasks):
            raise RuntimeError()
        return {"status": "ok"}
    except Exception:
        return JSONResponse({"status": "unhealthy"}, status_code=503)


@app.post("/webhooks/monobank")
async def monobank_webhook(request: Request):
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > 65536:
            return JSONResponse({"error": "too_large"}, status_code=413)
    shop = request.app.state.shop
    try:
        if not await shop.mono.verify(bytes(body), request.headers.get("X-Sign", "")):
            return JSONResponse({"error": "invalid_signature"}, status_code=401)
        payload = json.loads(body)
        if not isinstance(payload, dict):
            raise ValueError()
        await shop.payment(payload, hashlib.sha256(body).hexdigest())
        return {"status": "ok"}
    except (ValueError, TypeError):
        return JSONResponse({"error": "invalid_payload"}, status_code=400)
    except LookupError:
        return JSONResponse({"error": "retry_later"}, status_code=503)
    except Exception:
        log.warning("payment_webhook_failed")
        return JSONResponse({"error": "temporary_failure"}, status_code=503)


@app.get("/oauth/gmail/callback", response_class=HTMLResponse)
async def gmail_callback(request: Request, state: str = "", code: str = "", error: str = ""):
    shop = request.app.state.shop
    headers = {
        "Cache-Control": "no-store",
        "Referrer-Policy": "no-referrer",
        "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
    }
    if len(state) > 100 or not state:
        return HTMLResponse("Invalid state", status_code=400, headers=headers)
    admin_id = await shop.redis.getdel("oauth:" + state)
    if not admin_id or not is_admin(shop.cfg, int(admin_id)):
        return HTMLResponse(
            "Authorization expired. Start again in Telegram.", status_code=400, headers=headers
        )
    if error or not code:
        return HTMLResponse("Authorization cancelled. Return to Telegram.", status_code=400, headers=headers)
    try:
        credentials = await shop.gmail.exchange(code)
        await shop.redis.set("gmail:draft:" + state, shop.vault.pack(credentials), ex=1800)
    except Exception:
        log.warning("gmail_oauth_failed")
        return HTMLResponse("Connection failed. Start again in Telegram.", status_code=400, headers=headers)
    return HTMLResponse("Gmail connected. Return to Telegram and confirm the connection.", headers=headers)
