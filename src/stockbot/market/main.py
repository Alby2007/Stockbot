"""Market service entrypoint: the singleton tick engine process.

Guarded by a Postgres advisory lock so an accidental second instance can
never double-tick (see `docker-compose.yml`'s `market` service and the
scale-to-2 check in AGENTS.md).
"""

from __future__ import annotations

import asyncio
import logging
import sys

import psycopg
from psycopg import AsyncConnection

from stockbot import db
from stockbot.compliance.wash_trade import scan_for_wash_trades
from stockbot.config import get_settings
from stockbot.logging import setup_logging
from stockbot.market.tick import apply_tick
from stockbot.observability import write_heartbeat

setup_logging("market")
log = logging.getLogger("stockbot.market")

# Arbitrary fixed key identifying "the market tick engine" lock, shared by all
# instances so Postgres will only ever grant it to one of them at a time.
ADVISORY_LOCK_KEY = 0x5350_4B_54  # "stock" (truncated), just needs to be a stable constant

TICK_INTERVAL_SECONDS = 60
WASH_TRADE_SCAN_EVERY_N_TICKS = 10
RECONNECT_DELAY_SECONDS = 5
# Consecutive tick failures before we escalate to log.critical and stamp
# the heartbeat with the streak (a poison-pill row wedging match_orders
# is invisible to `docker ps` otherwise).
TICK_FAILURE_ESCALATE_AFTER = 5


async def _tick_loop(conn: AsyncConnection, master_seed: str) -> None:
    """Tick every TICK_INTERVAL_SECONDS; returns when `conn` dies.

    The advisory lock is session-scoped: a broken connection means the lock
    is already gone and a second instance may have grabbed it. So a dead
    conn must stop the loop entirely -- the caller re-checks out a fresh
    connection and re-acquires the lock -- rather than letting `apply_tick`
    fail every interval forever on a conn the pool will never heal.
    """
    consecutive_failures = 0
    while True:
        start = asyncio.get_running_loop().time()
        try:
            tick_index = await apply_tick(conn, master_seed)
            consecutive_failures = 0
            if tick_index % WASH_TRADE_SCAN_EVERY_N_TICKS == 0:
                flags = await scan_for_wash_trades(conn)
                if flags:
                    log.warning("flagged %d possible wash trade(s)", len(flags))
        except psycopg.OperationalError:
            log.exception("database connection lost; re-acquiring the advisory lock")
            return
        except Exception:
            consecutive_failures += 1
            log.exception(
                "tick failed (%d consecutive); will retry next interval",
                consecutive_failures,
            )
            if conn.closed or conn.broken:
                # Driver didn't surface it as OperationalError, but the conn
                # (and the lock with it) is dead -- bail out the same way.
                return
            if consecutive_failures >= TICK_FAILURE_ESCALATE_AFTER:
                log.critical(
                    "tick has failed %d times in a row -- the market is "
                    "wedged on something deterministic; investigate NOW",
                    consecutive_failures,
                )
                try:
                    # Best-effort: stamps the failure streak where
                    # /admin health and external checks can see it. The
                    # failed tick tx rolled back, so this needs its own.
                    async with conn.transaction():
                        await write_heartbeat(
                            conn,
                            "market",
                            {"consecutive_failures": consecutive_failures},
                        )
                except Exception:
                    pass
        elapsed = asyncio.get_running_loop().time() - start
        await asyncio.sleep(max(0.0, TICK_INTERVAL_SECONDS - elapsed))


async def run() -> None:
    settings = get_settings()
    await db.init_pool(settings.database_url, min_size=1, max_size=2)
    try:
        # Outer reconnect loop: every iteration checks out a fresh pooled
        # connection and re-acquires the session-scoped advisory lock. When
        # Postgres itself is unreachable, pool checkout raises
        # OperationalError (PoolTimeout is a subclass) and we retry in
        # process instead of crash-looping on `restart: unless-stopped`.
        while True:
            try:
                async with db.connection() as conn, conn.cursor() as cur:
                    await cur.execute(
                        "SELECT pg_try_advisory_lock(%s)", (ADVISORY_LOCK_KEY,)
                    )
                    row = await cur.fetchone()
                    assert row is not None
                    (acquired,) = row
                    if not acquired:
                        log.error(
                            "another market instance already holds the singleton "
                            "lock; exiting"
                        )
                        return
                    # pg_try_advisory_lock is session-scoped, not
                    # transaction-scoped, so committing here does not release
                    # it -- but it's essential: without this, the connection
                    # would sit in the implicit transaction this SELECT opened
                    # for the rest of the process's life, and every
                    # "async with conn.transaction()" inside apply_tick would
                    # silently become a savepoint of that never-committed
                    # transaction instead of a real commit.
                    await conn.commit()
                    log.info("acquired singleton advisory lock; market service starting")
                    await _tick_loop(conn, settings.master_seed)
            except psycopg.OperationalError:
                log.exception("database unreachable; retrying shortly")
            await asyncio.sleep(RECONNECT_DELAY_SECONDS)
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
