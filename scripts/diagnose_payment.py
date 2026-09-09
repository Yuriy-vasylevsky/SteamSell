"""Read-only payment diagnostics; never print tokens or full statements."""
import asyncio
from datetime import datetime, UTC, timedelta
import httpx
from app.config import config
from app.personal import resolve_account


async def main():
    cfg = config()
    async with httpx.AsyncClient(timeout=20) as client:
        try:
            account = await resolve_account(client, cfg.mono_token.get_secret_value(), cfg.manual_card)
            start = int((datetime.now(UTC) - timedelta(hours=3)).timestamp())
            r = await client.get(f'https://api.monobank.ua/personal/statement/{account}/{start}',
                                 headers={'X-Token': cfg.mono_token.get_secret_value()})
            print('statement_http', r.status_code)
            r.raise_for_status()
            rows = r.json()
            print('entries', len(rows))
            for row in rows:
                if abs(row.get('amount', 0)) in (100, 500):
                    print({k: row.get(k) for k in ('time', 'amount', 'operationAmount', 'currencyCode', 'hold')})
        except Exception as e:
            print('error_type', type(e).__name__)


asyncio.run(main())
