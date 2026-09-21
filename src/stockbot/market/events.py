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

from decimal import ROUND_HALF_UP, Decimal
from typing import Any

import numpy as np
from psycopg import AsyncConnection
from psycopg.rows import dict_row

from stockbot.ledger.service import get_system_account_id, post_transfer
from stockbot.market.engine import TICKS_PER_DAY

EARNINGS_INTERVAL_TICKS = 30 * TICKS_PER_DAY
EARNINGS_JITTER_TICKS = 5 * TICKS_PER_DAY
EARNINGS_SHOCK_SIGMA = 0.05

# Lower bound for fundamental/base values after an ex-date drop, so a
# dividend can never push the factor model into nonpositive prices.
MIN_POST_DIV_PRICE = 0.01

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


async def _dividend_config(conn: AsyncConnection) -> dict[str, float]:
    async with conn.cursor() as cur:
        await cur.execute("SELECT key, value FROM config WHERE key LIKE 'dividend.%'")
        return {str(k): float(v) for k, v in await cur.fetchall()}


def _pays_dividends(instrument_id: int, payer_pct: float) -> bool:
    """Deterministic payer flag: a stable hash of the instrument id puts a
    fixed subset of names on a dividend schedule (like real indices where
    only some constituents pay)."""
    return (instrument_id * 2654435761) % 100 < payer_pct


def _dividend_per_share(
    rng: np.random.Generator,
    cfg: dict[str, float],
    quoted: float,
) -> Decimal:
    """Per-share dividend for a new ex-date.

    No drift pre-bleed: the ex-date drop is self-funding under the
    MM-as-counterparty model (MARKET_MAKER pays the cash and is short the
    aggregate book, so its mark-to-market on the drop offsets the payout).
    A drift offset would double-suppress ~2x the dividend per cycle --
    shorts would pocket the second drop while owing only one.
    """
    yield_frac = float(rng.uniform(cfg["dividend.min_yield"], cfg["dividend.max_yield"]))
    return Decimal(str(round(quoted * yield_frac, 6)))


async def schedule_initial_dividends(
    conn: AsyncConnection, rng: np.random.Generator, current_tick: int
) -> None:
    """Seed one upcoming ex-date for each dividend-paying instrument that
    doesn't have one pending. Mirrors schedule_initial_earnings."""
    cfg = await _dividend_config(conn)
    payer_pct = cfg.get("dividend.payer_pct", 35.0)
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            SELECT i.id, i.quoted_price FROM instruments i
            WHERE i.is_active AND i.kind <> 'INDEX' AND NOT EXISTS (
                SELECT 1 FROM events e
                WHERE e.instrument_id = i.id AND e.kind = 'DIVIDEND' AND NOT e.resolved
            )
            ORDER BY i.id
            """
        )
        rows = await cur.fetchall()

    interval = int(cfg.get("dividend.interval_ticks", 86400))
    jitter = int(cfg.get("dividend.jitter_ticks", 20160))
    for row in rows:
        if not _pays_dividends(int(row["id"]), payer_pct):
            continue
        window = max(1, interval + int(rng.integers(-jitter, jitter + 1)))
        div = _dividend_per_share(rng, cfg, float(row["quoted_price"]))
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO events
                    (instrument_id, kind, scheduled_tick, resolve_tick,
                     dividend_per_share)
                VALUES (%s, 'DIVIDEND', %s, %s, %s)
                """,
                (row["id"], current_tick, current_tick + window, div),
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


async def _resolve_dividend(
    conn: AsyncConnection,
    rng: np.random.Generator,
    event: dict[str, Any],
    current_tick: int,
) -> Decimal:
    """Ex-date processing for one DIVIDEND event: pay longs from
    MARKET_MAKER, accrue the obligation on shorts (settled on cover, like
    borrow fees), and reschedule the next ex-date. Returns the per-share
    amount so the caller can drop the instrument's base/fundamental by it.

    Bounded shorts are NOT charged: they're a collateralized derivative
    against MARKET_MAKER, not borrowed stock.
    """
    instrument_id = int(event["instrument_id"])
    div = Decimal(event["dividend_per_share"])
    mm_id = await get_system_account_id(conn, "MARKET_MAKER")

    async with conn.cursor(row_factory=dict_row) as cur:
        # Accounts join straight on (user_id, season_id) -- league
        # positions pay their league account. ORDER BY for determinism.
        await cur.execute(
            """
            SELECT p.user_id, p.quantity, a.id AS account_id, i.ticker
            FROM positions p
            JOIN accounts a ON a.user_id = p.user_id
              AND a.season_id IS NOT DISTINCT FROM p.season_id
              AND a.kind <> 'SYSTEM'
            JOIN instruments i ON i.id = p.instrument_id
            WHERE p.instrument_id = %s AND p.quantity > 0
            ORDER BY p.user_id, p.season_id
            """,
            (instrument_id,),
        )
        longs = await cur.fetchall()
    for pos in longs:
        amount_minor = int(
            (Decimal(int(pos["quantity"])) * div * 100).quantize(
                Decimal("1"), ROUND_HALF_UP
            )
        )
        if amount_minor <= 0:
            continue
        await post_transfer(
            conn,
            from_account_id=mm_id,
            to_account_id=int(pos["account_id"]),
            amount=amount_minor,
            reason="DIVIDEND",
            memo=str(pos["ticker"]),
        )

    async with conn.cursor() as cur:
        # Shorts owe the dividend but can't be debited (cash CHECK >= 0) --
        # accrue it on the position, settled to MARKET_MAKER on cover.
        await cur.execute(
            """
            UPDATE positions
            SET dividends_accrued = dividends_accrued + %s * (-quantity) * 100
            WHERE instrument_id = %s AND quantity < 0
            """,
            (div, instrument_id),
        )

    # Reschedule the next ex-date (like earnings' rolling calendar).
    cfg = await _dividend_config(conn)
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT quoted_price FROM instruments WHERE id = %s", (instrument_id,)
        )
        row = await cur.fetchone()
    quoted = float(row[0]) if row else float(div)
    interval = int(cfg.get("dividend.interval_ticks", 86400))
    jitter = int(cfg.get("dividend.jitter_ticks", 20160))
    window = max(1, interval + int(rng.integers(-jitter, jitter + 1)))
    next_div = _dividend_per_share(rng, cfg, quoted)
    async with conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO events
                (instrument_id, kind, scheduled_tick, resolve_tick,
                 dividend_per_share)
            VALUES (%s, 'DIVIDEND', %s, %s, %s)
            """,
            (instrument_id, current_tick, current_tick + window, next_div),
        )
    return div


async def resolve_due_events(
    conn: AsyncConnection,
    rng: np.random.Generator,
    current_tick: int,
    fundamentals: dict[int, float],
    stats: dict[str, int] | None = None,
) -> tuple[dict[int, float], dict[int, Decimal]]:
    """Apply any events due this tick, mutating and returning
    `fundamentals` (instrument_id -> fundamental value). Earnings/news are
    log-return jumps; earnings are rescheduled ~30 days out once resolved.

    The second return value maps instrument_id -> dividend per share for
    DIVIDEND events resolved this tick: the caller subtracts it from the
    instrument's base price (the immediate ex-date drop) while this
    function subtracts it from the fundamental (permanent value loss).
    `stats["events_resolved"]` accumulates the count when given.
    """
    resolved_count = 0
    async with conn.cursor(row_factory=dict_row) as cur:
        # ORDER BY matters: earnings magnitudes and reschedule jitter are
        # drawn from `rng` in row order, so an unordered scan would assign
        # different draws to the same events across replays.
        await cur.execute(
            """
            SELECT e.id, e.instrument_id, e.kind, e.magnitude,
                   e.dividend_per_share, i.kind AS instrument_kind
            FROM events e
            JOIN instruments i ON i.id = e.instrument_id
            WHERE e.resolve_tick <= %s AND NOT e.resolved
            ORDER BY e.id
            FOR UPDATE OF e
            """,
            (current_tick,),
        )
        due = await cur.fetchall()

    dividend_drops: dict[int, Decimal] = {}
    for event in due:
        instrument_id = event["instrument_id"]

        if event["instrument_kind"] == "INDEX":
            # Scheduling never creates events on the index, but a
            # hand-inserted row would resolve here -- a DIVIDEND on SBX40
            # would pay index longs cash with no real price drop. Swallow
            # it: mark resolved, apply nothing.
            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE events SET resolved = TRUE WHERE id = %s",
                    (event["id"],),
                )
            resolved_count += 1
            continue

        if event["kind"] == "DIVIDEND":
            div = await _resolve_dividend(conn, rng, event, current_tick)
            dividend_drops[instrument_id] = div
            if instrument_id in fundamentals:
                fundamentals[instrument_id] = max(
                    MIN_POST_DIV_PRICE, fundamentals[instrument_id] - float(div)
                )
            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE events SET resolved = TRUE WHERE id = %s", (event["id"],)
                )
            resolved_count += 1
            continue

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
        resolved_count += 1

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

    if stats is not None:
        stats["events_resolved"] = stats.get("events_resolved", 0) + resolved_count
    return fundamentals, dividend_drops


async def refresh_next_event_ticks(conn: AsyncConnection) -> None:
    """Recompute per-instrument event aggregates.

    One aggregate UPDATE per tick, called after all event creation/resolution
    in apply_tick, so it's self-healing: no per-insert bookkeeping and it can
    never drift. `next_event_tick` feeds the spread's event-proximity term.
    `dividend_drift_offset` is vestigial -- the ex-date drop alone funds the
    payout now (a pre-bleed would double-suppress it), so new DIVIDEND
    events carry a NULL offset and this aggregate stays 0.
    """
    async with conn.cursor() as cur:
        await cur.execute(
            """
            UPDATE instruments i SET
                next_event_tick = (
                    SELECT MIN(e.resolve_tick) FROM events e
                    WHERE e.instrument_id = i.id AND NOT e.resolved
                ),
                dividend_drift_offset = COALESCE((
                    SELECT SUM(e.drift_offset) FROM events e
                    WHERE e.instrument_id = i.id AND e.kind = 'DIVIDEND'
                      AND NOT e.resolved
                ), 0)
            """
        )
