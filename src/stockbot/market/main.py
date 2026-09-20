"""Market service entrypoint.

Phase 0 placeholder: the tick engine itself lands in Phase 1. This just
proves out the process must be a singleton, guarded by a Postgres advisory
lock, so an accidental second instance can never double-tick.
"""

from __future__ import annotations

import asyncio
import logging
import sys

from stockbot import db
from stockbot.config import get_settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s market %(levelname)s %(message)s")
log = logging.getLogger("stockbot.market")

# Arbitrary fixed key identifying "the market tick engine" lock, shared by all
# instances so Postgres will only ever grant it to one of them at a time.
ADVISORY_LOCK_KEY = 0x5350_4B_54  # "stock" (truncated), just needs to be a stable constant


async def run() -> None:
    settings = get_settings()
    await db.init_pool(settings.database_url, min_size=1, max_size=2)
    try:
        async with db.connection() as conn, conn.cursor() as cur:
            await cur.execute("SELECT pg_try_advisory_lock(%s)", (ADVISORY_LOCK_KEY,))
            row = await cur.fetchone()
            assert row is not None
            (acquired,) = row
            if not acquired:
                log.error("another market instance already holds the singleton lock; exiting")
                return
            log.info("acquired singleton advisory lock; market service starting")

            # Phase 1 will replace this with the real 60s tick loop.
            while True:
                await asyncio.sleep(60)
    finally:
        await db.close_pool()


def main() -> None:
    # psycopg's async mode needs a selector-based loop; Windows defaults to
    # ProactorEventLoop. The production image runs on Linux, where this is a
    # no-op, but it keeps `python -m stockbot.market.main` usable on Windows too.
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(run())


if __name__ == "__main__":
    main()
