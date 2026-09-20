"""Market service entrypoint: the singleton tick engine process.

Guarded by a Postgres advisory lock so an accidental second instance can
never double-tick (see `docker-compose.yml`'s `market` service and the
scale-to-2 check in AGENTS.md).
"""

from __future__ import annotations

import asyncio
import logging
import sys

from stockbot import db
from stockbot.compliance.wash_trade import scan_for_wash_trades
from stockbot.config import get_settings
from stockbot.market.tick import apply_tick

logging.basicConfig(level=logging.INFO, format="%(asctime)s market %(levelname)s %(message)s")
log = logging.getLogger("stockbot.market")

# Arbitrary fixed key identifying "the market tick engine" lock, shared by all
# instances so Postgres will only ever grant it to one of them at a time.
ADVISORY_LOCK_KEY = 0x5350_4B_54  # "stock" (truncated), just needs to be a stable constant

TICK_INTERVAL_SECONDS = 60
WASH_TRADE_SCAN_EVERY_N_TICKS = 10


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
            # pg_try_advisory_lock is session-scoped, not transaction-scoped, so
            # committing here does not release it -- but it's essential: without
            # this, the connection would sit in the implicit transaction this
            # SELECT opened for the rest of the process's life, and every
            # "async with conn.transaction()" inside apply_tick would silently
            # become a savepoint of that never-committed transaction instead of
            # a real commit.
            await conn.commit()
            log.info("acquired singleton advisory lock; market service starting")

            while True:
                start = asyncio.get_event_loop().time()
                try:
                    tick_index = await apply_tick(conn, settings.master_seed)
                    log.info("applied tick %d", tick_index)
                    if tick_index % WASH_TRADE_SCAN_EVERY_N_TICKS == 0:
                        flags = await scan_for_wash_trades(conn)
                        if flags:
                            log.warning("flagged %d possible wash trade(s)", len(flags))
                except Exception:
                    log.exception("tick failed; will retry next interval")
                elapsed = asyncio.get_event_loop().time() - start
                await asyncio.sleep(max(0.0, TICK_INTERVAL_SECONDS - elapsed))
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
