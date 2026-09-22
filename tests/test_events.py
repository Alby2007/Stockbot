from __future__ import annotations

import numpy as np
import pytest
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
    fundamentals, _ = await events.resolve_due_events(
        conn, rng, current_tick=5, fundamentals=fundamentals
    )

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
    fundamentals, _ = await events.resolve_due_events(
        conn, rng, current_tick=10, fundamentals=fundamentals
    )

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


# --- Plan D: consensus estimates + news fizzle -------------------------------


async def test_earnings_scheduled_with_street_estimate(
    conn: AsyncConnection,
) -> None:
    """Every pending EARNINGS event carries a public estimate after
    scheduling (and after each resolution reschedules the next one)."""
    rng = np.random.default_rng(0)
    await events.schedule_initial_earnings(conn, rng, current_tick=0)
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT COUNT(*) FROM events "
            "WHERE kind = 'EARNINGS' AND NOT resolved AND estimate IS NULL"
        )
        (missing,) = await cur.fetchone()
        await cur.execute(
            "SELECT COUNT(*) FROM events "
            "WHERE kind = 'EARNINGS' AND NOT resolved AND estimate <> 0"
        )
        (nonzero,) = await cur.fetchone()
    assert missing == 0
    assert nonzero > 0  # N(0, sigma) draws -- not all exactly zero


async def test_resolution_is_surplus_over_consensus(conn: AsyncConnection) -> None:
    """The realized jump = estimate + surprise: with a known estimate and
    a seeded rng the stored magnitude is estimate + the exact first
    normal draw (resolution adds no other draws before it)."""
    async with conn.cursor() as cur:
        await cur.execute("SELECT id FROM instruments ORDER BY id LIMIT 1")
        (instrument_id,) = await cur.fetchone()
        await cur.execute(
            """
            INSERT INTO events (instrument_id, kind, scheduled_tick, resolve_tick,
                                estimate)
            VALUES (%s, 'EARNINGS', 0, 5, 0.04)
            """,
            (instrument_id,),
        )

    rng = np.random.default_rng(7)
    expected_surprise = float(np.random.default_rng(7).normal(0, events.EARNINGS_SHOCK_SIGMA))
    fundamentals = {instrument_id: 100.0}
    fundamentals, _ = await events.resolve_due_events(
        conn, rng, current_tick=5, fundamentals=fundamentals
    )

    expected_magnitude = 0.04 + expected_surprise
    assert fundamentals[instrument_id] == 100.0 * float(np.exp(expected_magnitude))

    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT magnitude FROM events WHERE instrument_id = %s AND resolved "
            "AND kind = 'EARNINGS'",
            (instrument_id,),
        )
        (stored,) = await cur.fetchone()
    # magnitude is NUMERIC(10,6) -- quantized to 6dp on write.
    assert float(stored) == pytest.approx(expected_magnitude, abs=1e-6)


async def test_news_fizzle_shrinks_magnitude_and_marks_flag(
    conn: AsyncConnection, monkeypatch
) -> None:
    """fizzle_pct=1: every created rumor is fizzled -- headline still
    posts, magnitude shrunk ~10x, fizzled flag set for post-resolution
    labeling."""
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE config SET value = 1.0 WHERE key = 'news.fizzle_pct'"
        )
        await cur.execute(
            "SELECT id, ticker FROM instruments WHERE is_active AND kind != 'INDEX' "
            "ORDER BY id LIMIT 3"
        )
        insts = [{"id": r[0], "ticker": r[1]} for r in await cur.fetchall()]

    monkeypatch.setattr(events, "NEWS_CHANCE_PER_INSTRUMENT_PER_TICK", 1.0)
    rng = np.random.default_rng(3)
    await events.maybe_create_news(conn, rng, current_tick=0, instruments=insts)

    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT magnitude, fizzled FROM events WHERE kind = 'NEWS'"
        )
        rows = await cur.fetchall()
    assert len(rows) == len(insts)
    for magnitude, fizzled in rows:
        assert fizzled is True
        assert abs(float(magnitude)) < 0.006  # ~0.1x a typical 2% draw

    # Control: fizzle_pct=0 creates no fizzled rows.
    async with conn.cursor() as cur:
        await cur.execute("DELETE FROM events WHERE kind = 'NEWS'")
        await cur.execute(
            "UPDATE config SET value = 0.0 WHERE key = 'news.fizzle_pct'"
        )
    await events.maybe_create_news(conn, rng, current_tick=1, instruments=insts)
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT COUNT(*) FROM events WHERE kind = 'NEWS' AND fizzled"
        )
        (fizzled_count,) = await cur.fetchone()
    assert fizzled_count == 0


async def test_fizzled_news_still_resolves(conn: AsyncConnection) -> None:
    """A doomed rumor resolves like any news -- the flag rides through to
    the /news 'rumor died' label."""
    async with conn.cursor() as cur:
        await cur.execute("SELECT id FROM instruments ORDER BY id LIMIT 1")
        (instrument_id,) = await cur.fetchone()
        await cur.execute(
            """
            INSERT INTO events
                (instrument_id, kind, scheduled_tick, resolve_tick, headline,
                 magnitude, fizzled)
            VALUES (%s, 'NEWS', 0, 10, 'test rumor', 0.001, TRUE)
            """,
            (instrument_id,),
        )
    rng = np.random.default_rng(4)
    fundamentals = {instrument_id: 100.0}
    fundamentals, _ = await events.resolve_due_events(
        conn, rng, current_tick=10, fundamentals=fundamentals
    )
    assert fundamentals[instrument_id] == 100.0 * float(np.exp(0.001))
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT resolved, fizzled FROM events WHERE instrument_id = %s "
            "AND kind = 'NEWS'",
            (instrument_id,),
        )
        resolved, fizzled = await cur.fetchone()
    assert resolved is True and fizzled is True
