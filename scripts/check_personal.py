"""Check configured card access without printing credentials or account data."""
import asyncio

import httpx

from app.config import config
from app.personal import resolve_account


async def main():
    async with httpx.AsyncClient(timeout=20) as client:
        try:
            await resolve_account(client, config().mono_token.get_secret_value(), config().manual_card)
            print("ACCOUNT_MATCH_OK")
        except httpx.HTTPStatusError as error:
            print("BANK_HTTP_STATUS", error.response.status_code)
        except Exception as error:
            print(type(error).__name__)


asyncio.run(main())
