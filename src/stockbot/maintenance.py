"""Daily bookkeeping prune (H3). Runs from market's `_post_tick` on the
day boundary -- the same `tick_index % TICKS_PER_DAY` cadence season
snapshots and net-worth snapshots use, so it inherits the market
service's singleton guarantee.

All three pruned tables are bookkeeping, not ledger -- DELETE is safe
and none are append-only-triggered like ledger_entries.
"""

from __future__ import annotations

import logging

from psycopg import AsyncConnection

from stockbot.market.engine import TICKS_PER_DAY

log = logging.getLogger("stockbot.maintenance")

# Discord's interaction retry window is seconds -- 30d is generous.
IDEMPOTENCY_RETENTION_DAYS = 30
NOTIFICATIONS_RETENTION_DAYS = 90
COMMAND_STATS_RETENTION_DAYS = 90


async def run_maintenance_if_due(
    conn: AsyncConnection, tick_index: int, *, interval_ticks: int = TICKS_PER_DAY
) -> dict[str, int] | None:
    """Prune on the day boundary; None on every other tick. Returns the
    per-table delete counts when it runs (tests + the tick log line)."""
    if tick_index == 0 or tick_index % interval_ticks != 0:
        return None
    deleted: dict[str, int] = {}
    async with conn.cursor() as cur:
        await cur.execute(
            "DELETE FROM idempotency_keys "
            "WHERE created_at < now() - make_interval(days => %s)",
            (IDEMPOTENCY_RETENTION_DAYS,),
        )
        deleted["idempotency_keys"] = cur.rowcount
        await cur.execute(
            "DELETE FROM notifications "
            "WHERE sent_at IS NOT NULL "
            "AND sent_at < now() - make_interval(days => %s)",
            (NOTIFICATIONS_RETENTION_DAYS,),
        )
        deleted["notifications"] = cur.rowcount
        await cur.execute(
            "DELETE FROM command_stats "
            "WHERE created_at < now() - make_interval(days => %s)",
            (COMMAND_STATS_RETENTION_DAYS,),
        )
        deleted["command_stats"] = cur.rowcount
    if any(deleted.values()):
        log.info("maintenance tick=%d pruned %s", tick_index, deleted)
    return deleted
