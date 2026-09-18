"""Minimal vision API connectivity check; prints no credentials."""

import asyncio
import base64

import httpx

from app.config import config
from app.receipts import analyze_receipt, prepare_receipt


async def main():
    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
    )
    cfg = config()
    async with httpx.AsyncClient(timeout=cfg.deepseek_timeout) as client:
        try:
            result = await analyze_receipt(
                client,
                cfg.deepseek_api_key.get_secret_value(),
                cfg.deepseek_vision_model,
                prepare_receipt(png),
            )
            print("DEEPSEEK_VISION_OK", isinstance(result, dict))
        except httpx.HTTPStatusError as error:
            print("DEEPSEEK_HTTP", error.response.status_code)
        except Exception as error:
            print("DEEPSEEK_ERROR", type(error).__name__)


asyncio.run(main())
