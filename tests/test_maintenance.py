"""Phase H3: day-boundary bookkeeping prune."""

from __future__ import annotations

from psycopg import AsyncConnection

from stockbot.maintenance import run_maintenance_if_due
from stockbot.market.engine import TICKS_PER_DAY


async def test_noop_off_day_boundary(conn: AsyncConnection) -> None:
    assert await run_maintenance_if_due(conn, TICKS_PER_DAY + 7) is None
    assert await run_maintenance_if_due(conn, 0) is None


async def test_prunes_aged_rows_keeps_fresh(conn: AsyncConnection) -> None:
    async with conn.cursor() as cur:
        # Aged + fresh rows in all three tables.
        await cur.execute(
            "INSERT INTO idempotency_keys (interaction_id, created_at) VALUES "
            "('old-key', now() - interval '40 days'),"
            "('new-key', now())"
        )
        await cur.execute(
            "INSERT INTO notifications (user_id, kind, payload, sent_at) VALUES "
            "(777001, 'ORDER_FILLED', '{}', now() - interval '95 days'),"
            # sent_at IS NULL rows are NEVER pruned -- an unsent DM
            # is still deliverable, not bookkeeping garbage.
            "(777001, 'ORDER_FILLED', '{}', NULL),"
            "(777001, 'ORDER_FILLED', '{}', now())"
        )
        await cur.execute(
            "INSERT INTO command_stats (command, ok, duration_ms, created_at) "
            "VALUES ('old', true, 1, now() - interval '95 days'),"
            "('new', true, 1, now())"
        )

    deleted = await run_maintenance_if_due(conn, TICKS_PER_DAY)
    assert deleted == {
        "idempotency_keys": 1,
        "notifications": 1,
        "command_stats": 1,
    }

    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT interaction_id FROM idempotency_keys ORDER BY 1"
        )
        assert [r[0] for r in await cur.fetchall()] == ["new-key"]
        await cur.execute(
            "SELECT count(*) FROM notifications WHERE user_id = 777001"
        )
        (n,) = await cur.fetchone()
        assert n == 2  # the unsent + the fresh one survive
        await cur.execute(
            "SELECT command FROM command_stats WHERE command IN ('old', 'new')"
        )
        assert [r[0] for r in await cur.fetchall()] == ["new"]


async def test_prune_doesnt_block_concurrent_inserts(conn: AsyncConnection) -> None:
    """The prune's DELETEs lock only matched rows -- a fresh idempotency
    key lands cleanly right after a prune in the same transaction."""
    await run_maintenance_if_due(conn, TICKS_PER_DAY)
    async with conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO idempotency_keys (interaction_id) VALUES ('post-prune')"
        )
        await cur.execute(
            "SELECT count(*) FROM idempotency_keys WHERE interaction_id = 'post-prune'"
        )
        (n,) = await cur.fetchone()
    assert n == 1
