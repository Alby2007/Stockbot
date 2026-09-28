"""NPC service entrypoint: the singleton synthetic-trader process.

Cloned from `market/main.py`: its own `pg_try_advisory_lock` (distinct
key) plus the `conn.commit()` right after -- without it every later
`conn.transaction()` becomes a savepoint of a never-committed
transaction. The lock also makes `--scale npc=2` a clean exit for the
second instance rather than a doubled synthetic population.

The loop polls `MAX(tick_index)` and runs `run_round` when it advances.
`npc.enabled` gates every round; dead agents (`died_at_tick`) never act
again -- permadeath. Nothing here advances the market: the market
service owns ticks; NPCs just trade inside them like humans do.
"""

from __future__ import annotations

import asyncio
import logging
import sys

import psycopg
from psycopg import AsyncConnection

from stockbot import db
from stockbot.config import get_settings
from stockbot.logging import setup_logging
from stockbot.npc.service import npc_enabled, run_round
from stockbot.observability import write_heartbeat

setup_logging("npc")
log = logging.getLogger("stockbot.npc")

# Distinct from market/main.py's 0x53504B54 -- same lock-name
# space, different singleton.
ADVISORY_LOCK_KEY = 0x4E50_4354  # "NPCT"

POLL_INTERVAL_SECONDS = 2
TICK_INTERVAL_SECONDS = 60  # pacing reference for the action stagger
RECONNECT_DELAY_SECONDS = 5


async def _round_loop(conn: AsyncConnection, master_seed: str) -> None:
    """Watch for new ticks; on each, fire one staggered action round.

    The advisory lock is session-scoped: a broken conn means a second
    instance may already hold it, so a dead conn exits the loop entirely
    (same contract as _tick_loop in market/main.py).
    """
    last_tick: int | None = None
    last_beat = 0.0
    while True:
        try:
            async with conn.transaction():
                async with conn.cursor() as cur:
                    await cur.execute("SELECT MAX(tick_index) FROM market_ticks")
                    row = await cur.fetchone()
                enabled = await npc_enabled(conn)
            current = int(row[0]) if row and row[0] is not None else None
            stats: dict[str, int] = {"acted": 0, "agents": 0, "dead": 0}
            if current is not None and current != last_tick:
                last_tick = current
                if enabled:
                    stats = await run_round(
                        conn,
                        master_seed,
                        current,
                        interval_seconds=TICK_INTERVAL_SECONDS,
                    )
            # Heartbeat per new tick plus a slow liveness beat while idle
            # (a 2s poll writing every pass would be 43k beats/day of
            # "still disabled").
            now = asyncio.get_running_loop().time()
            if stats["acted"] or stats["dead"] or now - last_beat >= 60:
                last_beat = now
                async with conn.transaction():
                    await write_heartbeat(
                        conn,
                        "npc",
                        {"last_tick": last_tick, "enabled": enabled, **stats},
                    )
        except psycopg.OperationalError:
            log.exception("database connection lost; re-acquiring the advisory lock")
            return
        except Exception:
            log.exception("npc round failed; retrying next poll")
            if conn.closed or conn.broken:
                return
        await asyncio.sleep(POLL_INTERVAL_SECONDS)


async def run() -> None:
    settings = get_settings()
    await db.init_pool(settings.database_url, min_size=1, max_size=2)
    try:
        while True:
            try:
                async with db.connection() as conn, conn.cursor() as cur:
                    await cur.execute("SELECT pg_try_advisory_lock(%s)", (ADVISORY_LOCK_KEY,))
                    row = await cur.fetchone()
                    assert row is not None
                    (acquired,) = row
                    if not acquired:
                        log.error("another npc instance already holds the singleton lock; exiting")
                        return
                    # See market/main.py: session lock survives commit, and
                    # without this commit every later transaction() is a
                    # savepoint of a never-committed transaction.
                    await conn.commit()
                    log.info("acquired singleton advisory lock; npc service starting")
                    await _round_loop(conn, settings.master_seed)
            except psycopg.OperationalError:
                log.exception("database unreachable; retrying shortly")
            await asyncio.sleep(RECONNECT_DELAY_SECONDS)
    finally:
        await db.close_pool()


def main() -> None:
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(run())


if __name__ == "__main__":
    main()
