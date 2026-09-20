from __future__ import annotations

import numpy as np
from psycopg import AsyncConnection

from stockbot.market import events
from stockbot.market.tick import apply_tick


async def test_schedule_initial_earnings_seeds_every_instrument(conn: AsyncConnection) -> None:
    rng = np.random.default_rng(0)
    await events.schedule_initial_earnings(conn, rng, current_tick=0)

    async with conn.cursor() as cur:
        await cur.execute("SELECT COUNT(*) FROM events WHERE kind = 'EARNINGS' AND NOT resolved")
        (count,) = await cur.fetchone()
    async with conn.cursor() as cur:
        await cur.execute("SELECT COUNT(*) FROM instruments WHERE is_active")
        (instrument_count,) = await cur.fetchone()
    assert count == instrument_count


async def test_schedule_initial_earnings_is_idempotent(conn: AsyncConnection) -> None:
    rng = np.random.default_rng(0)
    await events.schedule_initial_earnings(conn, rng, current_tick=0)
    await events.schedule_initial_earnings(conn, rng, current_tick=1)

    async with conn.cursor() as cur:
        await cur.execute("SELECT COUNT(*) FROM events WHERE kind = 'EARNINGS'")
        (count,) = await cur.fetchone()
    async with conn.cursor() as cur:
        await cur.execute("SELECT COUNT(*) FROM instruments WHERE is_active")
        (instrument_count,) = await cur.fetchone()
    assert count == instrument_count


async def test_resolved_earnings_reschedules_the_next_one(conn: AsyncConnection) -> None:
    async with conn.cursor() as cur:
        await cur.execute("SELECT id FROM instruments ORDER BY id LIMIT 1")
        (instrument_id,) = await cur.fetchone()
        await cur.execute(
            """
            INSERT INTO events (instrument_id, kind, scheduled_tick, resolve_tick)
            VALUES (%s, 'EARNINGS', 0, 5)
            """,
            (instrument_id,),
        )

    rng = np.random.default_rng(1)
    fundamentals = {instrument_id: 100.0}
    await events.resolve_due_events(conn, rng, current_tick=5, fundamentals=fundamentals)

    assert fundamentals[instrument_id] != 100.0  # a jump was applied

    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT resolved FROM events WHERE instrument_id = %s AND resolve_tick = 5",
            (instrument_id,),
        )
        (resolved,) = await cur.fetchone()
    assert resolved is True

    async with conn.cursor() as cur:
        await cur.execute(
            """
            SELECT COUNT(*) FROM events
            WHERE instrument_id = %s AND kind = 'EARNINGS' AND NOT resolved
            """,
            (instrument_id,),
        )
        (pending_count,) = await cur.fetchone()
    assert pending_count == 1


async def test_news_hint_resolves_and_reveals_magnitude(conn: AsyncConnection) -> None:
    async with conn.cursor() as cur:
        await cur.execute("SELECT id FROM instruments ORDER BY id LIMIT 1")
        (instrument_id,) = await cur.fetchone()
        await cur.execute(
            """
            INSERT INTO events
                (instrument_id, kind, scheduled_tick, resolve_tick, headline, magnitude)
            VALUES (%s, 'NEWS', 0, 10, 'test headline', 0.03)
            """,
            (instrument_id,),
        )

    rng = np.random.default_rng(2)
    fundamentals = {instrument_id: 100.0}
    await events.resolve_due_events(conn, rng, current_tick=10, fundamentals=fundamentals)

    assert fundamentals[instrument_id] == 100.0 * float(np.exp(0.03))

    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT resolved, magnitude FROM events WHERE instrument_id = %s AND kind = 'NEWS'",
            (instrument_id,),
        )
        resolved, magnitude = await cur.fetchone()
    assert resolved is True
    assert float(magnitude) == 0.03


async def test_apply_tick_seeds_earnings_for_all_instruments(conn: AsyncConnection) -> None:
    await apply_tick(conn, "events-smoke-seed")
    async with conn.cursor() as cur:
        await cur.execute("SELECT COUNT(*) FROM events WHERE kind = 'EARNINGS' AND NOT resolved")
        (count,) = await cur.fetchone()
    async with conn.cursor() as cur:
        await cur.execute("SELECT COUNT(*) FROM instruments WHERE is_active")
        (instrument_count,) = await cur.fetchone()
    assert count == instrument_count
