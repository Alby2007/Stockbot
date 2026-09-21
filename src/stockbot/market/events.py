"""Earnings calendar + delayed news.

Both are jumps applied to an instrument's fundamental value `F_i` (which
`market/engine.py`'s mean reversion pulls price toward), just with different
timing and transparency:

- **Earnings**: recurring every ~30 days per instrument, staggered. The date
  is public (`/calendar`); the size and direction of the jump is not, until
  it resolves.
- **News**: a hint is posted (`/news`) at tick `t`, naming the instrument and
  a vague direction; the actual price effect lands at `t + lag`. The exact
  magnitude is decided at creation time but withheld from `/news` until
  resolution -- the tradeable edge is the direction and the window, not
  certainty.

All randomness here comes from the caller's `rng` (see `market/tick.py`,
which derives a separate deterministic stream for events so this doesn't
perturb the price engine's own draw sequence).
"""

from __future__ import annotations

from typing import Any

import numpy as np
from psycopg import AsyncConnection
from psycopg.rows import dict_row

from stockbot.market.engine import TICKS_PER_DAY

EARNINGS_INTERVAL_TICKS = 30 * TICKS_PER_DAY
EARNINGS_JITTER_TICKS = 5 * TICKS_PER_DAY
EARNINGS_SHOCK_SIGMA = 0.05

NEWS_CHANCE_PER_INSTRUMENT_PER_TICK = 0.0005
NEWS_MIN_LAG_TICKS = 30
NEWS_MAX_LAG_TICKS = 240
NEWS_SHOCK_SIGMA = 0.02

NEWS_HEADLINES_POSITIVE = (
    "Analysts flag unusual accumulation in {ticker}",
    "Sources hint at a favorable announcement from {ticker}",
    "Rumor: {ticker} in talks that could move the stock",
)
NEWS_HEADLINES_NEGATIVE = (
    "Analysts flag unusual distribution in {ticker}",
    "Sources hint at headwinds for {ticker}",
    "Rumor: {ticker} facing scrutiny that could move the stock",
)


async def schedule_initial_earnings(
    conn: AsyncConnection, rng: np.random.Generator, current_tick: int
) -> None:
    """Seed one upcoming earnings date for any active instrument that
    doesn't already have one pending. Idempotent and cheap (a few dozen
    rows), safe to call every tick.
    """
    async with conn.cursor() as cur:
        await cur.execute(
            """
            SELECT i.id FROM instruments i
            WHERE i.is_active AND NOT EXISTS (
                SELECT 1 FROM events e
                WHERE e.instrument_id = i.id AND e.kind = 'EARNINGS' AND NOT e.resolved
            )
            ORDER BY i.id
            """
        )
        instrument_ids = [row[0] for row in await cur.fetchall()]

    for instrument_id in instrument_ids:
        # Offset >= 1 so a freshly scheduled earnings date can never resolve
        # on the same tick it was created.
        offset = int(rng.integers(1, EARNINGS_INTERVAL_TICKS))
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO events (instrument_id, kind, scheduled_tick, resolve_tick)
                VALUES (%s, 'EARNINGS', %s, %s)
                """,
                (instrument_id, current_tick, current_tick + offset),
            )


async def maybe_create_news(
    conn: AsyncConnection,
    rng: np.random.Generator,
    current_tick: int,
    instruments: list[dict[str, Any]],
) -> None:
    """Roll the dice once per instrument for a fresh news hint this tick."""
    for inst in instruments:
        if rng.random() >= NEWS_CHANCE_PER_INSTRUMENT_PER_TICK:
            continue
        lag = int(rng.integers(NEWS_MIN_LAG_TICKS, NEWS_MAX_LAG_TICKS + 1))
        magnitude = float(rng.normal(0, NEWS_SHOCK_SIGMA))
        pool = NEWS_HEADLINES_POSITIVE if magnitude >= 0 else NEWS_HEADLINES_NEGATIVE
        headline = str(rng.choice(np.array(pool, dtype=object))).format(ticker=inst["ticker"])
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO events
                    (instrument_id, kind, scheduled_tick, resolve_tick, headline, magnitude)
                VALUES (%s, 'NEWS', %s, %s, %s, %s)
                """,
                (inst["id"], current_tick, current_tick + lag, headline, magnitude),
            )


async def resolve_due_events(
    conn: AsyncConnection,
    rng: np.random.Generator,
    current_tick: int,
    fundamentals: dict[int, float],
) -> dict[int, float]:
    """Apply any events due this tick as a log-return jump to `fundamentals`
    (instrument_id -> fundamental value), mutating and returning it.
    Earnings events are rescheduled ~30 days out once resolved.
    """
    async with conn.cursor(row_factory=dict_row) as cur:
        # ORDER BY matters: earnings magnitudes and reschedule jitter are
        # drawn from `rng` in row order, so an unordered scan would assign
        # different draws to the same events across replays.
        await cur.execute(
            """
            SELECT id, instrument_id, kind, magnitude FROM events
            WHERE resolve_tick <= %s AND NOT resolved
            ORDER BY id
            FOR UPDATE
            """,
            (current_tick,),
        )
        due = await cur.fetchall()

    for event in due:
        instrument_id = event["instrument_id"]
        if event["kind"] == "EARNINGS":
            magnitude = float(rng.normal(0, EARNINGS_SHOCK_SIGMA))
        else:
            magnitude = float(event["magnitude"])

        if instrument_id in fundamentals:
            fundamentals[instrument_id] *= float(np.exp(magnitude))

        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE events SET resolved = TRUE, magnitude = %s WHERE id = %s",
                (magnitude, event["id"]),
            )

        if event["kind"] == "EARNINGS":
            jitter = int(rng.integers(-EARNINGS_JITTER_TICKS, EARNINGS_JITTER_TICKS + 1))
            next_resolve = current_tick + EARNINGS_INTERVAL_TICKS + jitter
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    INSERT INTO events (instrument_id, kind, scheduled_tick, resolve_tick)
                    VALUES (%s, 'EARNINGS', %s, %s)
                    """,
                    (instrument_id, current_tick, next_resolve),
                )

    return fundamentals


async def refresh_next_event_ticks(conn: AsyncConnection) -> None:
    """Recompute instruments.next_event_tick = earliest unresolved resolve_tick.

    One aggregate UPDATE per tick, called after all event creation/resolution
    in apply_tick, so it's self-healing: no per-insert bookkeeping and it can
    never drift. Feeds the spread's event-proximity term.
    """
    async with conn.cursor() as cur:
        await cur.execute(
            """
            UPDATE instruments i SET next_event_tick = (
                SELECT MIN(e.resolve_tick) FROM events e
                WHERE e.instrument_id = i.id AND NOT e.resolved
            )
            """
        )
