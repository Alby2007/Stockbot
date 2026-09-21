"""Read-only market data queries shared by several slash commands.

Kept separate from `market/tick.py` (which only the tick engine writes
through) so command handlers have one obvious place to read consistent,
already-joined instrument state.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from psycopg import AsyncConnection

from stockbot.market import engine
from stockbot.market.engine import TICKS_PER_DAY


@dataclass(frozen=True)
class InstrumentSnapshot:
    id: int
    ticker: str
    name: str
    sector_key: str
    sector_name: str
    quoted_price: Decimal
    impact: Decimal
    halted_until_tick: int | None
    day_ago_close: Decimal | None
    short_interest_pct: Decimal = Decimal(0)
    day_volume: int = 0
    bid: Decimal | None = None
    ask: Decimal | None = None

    @property
    def day_change_pct(self) -> float | None:
        if not self.day_ago_close:
            return None
        return float(self.quoted_price) / float(self.day_ago_close) - 1

    @property
    def is_halted(self) -> bool:
        return self.halted_until_tick is not None


async def current_tick_index(conn: AsyncConnection) -> int | None:
    async with conn.cursor() as cur:
        await cur.execute("SELECT MAX(tick_index) FROM market_ticks")
        row = await cur.fetchone()
        return row[0] if row is not None else None


async def _fetch_snapshots(
    conn: AsyncConnection, ticker: str | None
) -> list[InstrumentSnapshot]:
    """One row per active instrument (or just `ticker`, when given), plus
    its close price from ~1 day ago (or the earliest candle available, if
    the market hasn't run a full day yet) for computing a 24h % change.
    """
    current_tick = await current_tick_index(conn)
    day_ago_tick = max((current_tick or 0) - TICKS_PER_DAY, 0)
    spread_cfg = {**await spread_config(conn), **await session_config(conn)}

    async with conn.cursor() as cur:
        await cur.execute(
            """
            SELECT i.id, i.ticker, i.name, s.key, s.name, i.quoted_price, i.impact,
                   i.circuit_halted_until_tick, c.close, i.short_interest_pct,
                   COALESCE(v.volume, 0),
                   COALESCE(i.sigma_eff, i.sigma) AS sigma, i.liquidity,
                   i.next_event_tick, i.last_halt_end_tick
            FROM instruments i
            JOIN sectors s ON s.id = i.sector_id
            LEFT JOIN LATERAL (
                SELECT close FROM candles
                WHERE candles.instrument_id = i.id AND candles.tick_index <= %s
                ORDER BY candles.tick_index DESC
                LIMIT 1
            ) c ON TRUE
            LEFT JOIN LATERAL (
                SELECT SUM(volume) AS volume FROM candles
                WHERE candles.instrument_id = i.id AND candles.tick_index > %s
            ) v ON TRUE
            WHERE i.is_active AND (%s::text IS NULL OR i.ticker = %s)
            ORDER BY i.ticker
            """,
            (day_ago_tick, day_ago_tick, ticker, ticker),
        )
        rows = await cur.fetchall()

    snapshots: list[InstrumentSnapshot] = []
    for r in rows:
        # Bid/ask on the tick grid, outward-rounded so the display is the
        # worst case a market fill can land at (G1/G3).
        mark = float(r[5])
        row = {
            "sigma": float(r[11]),
            "liquidity": float(r[12]),
            "next_event_tick": r[13],
            "last_halt_end_tick": r[14],
        }
        half = half_spread_for(row, current_tick, spread_cfg)
        tick = engine.tick_size(mark, spread_cfg)
        bid_f, ask_f = engine.quote_ticks(mark, half, tick)
        snapshots.append(
            InstrumentSnapshot(
                id=r[0],
                ticker=r[1],
                name=r[2],
                sector_key=r[3],
                sector_name=r[4],
                quoted_price=r[5],
                impact=r[6],
                halted_until_tick=r[7],
                day_ago_close=r[8],
                short_interest_pct=r[9],
                day_volume=int(r[10]),
                bid=Decimal(str(round(bid_f, 6))),
                ask=Decimal(str(round(ask_f, 6))),
            )
        )
    return snapshots


async def spread_config(conn: AsyncConnection) -> dict[str, float]:
    """The `spread.*` config namespace, read fresh so tuning takes effect
    without a deploy (same pattern as `margin.margin_config`)."""
    async with conn.cursor() as cur:
        await cur.execute("SELECT key, value FROM config WHERE key LIKE 'spread.%'")
        return {str(k): float(v) for k, v in await cur.fetchall()}


async def _config_float(conn: AsyncConnection, key: str, default: float) -> float:
    async with conn.cursor() as cur:
        await cur.execute("SELECT value FROM config WHERE key = %s", (key,))
        row = await cur.fetchone()
    return float(row[0]) if row else default


async def participation_cap(conn: AsyncConnection) -> float:
    """Max single-fill notional as a fraction of instrument liquidity."""
    return await _config_float(conn, "impact.participation_cap", 0.10)


async def trade_through_epsilon(conn: AsyncConnection) -> float:
    """Fraction by which the mark must cross a resting limit for an MM fill."""
    return await _config_float(conn, "cross.trade_through_epsilon", 0.0005)


async def vol_config(conn: AsyncConnection) -> dict[str, float]:
    """The `vol.*` config namespace (EWMA rho, regime clip bounds, market
    weight, and the F5 flow caps), read fresh like `spread_config`."""
    async with conn.cursor() as cur:
        await cur.execute("SELECT key, value FROM config WHERE key LIKE 'vol.%'")
        return {str(k): float(v) for k, v in await cur.fetchall()}


async def record_flow(
    conn: AsyncConnection,
    *,
    instrument_id: int,
    user_id: int,
    delta_impact: float,
    signed_notional_minor: int,
) -> None:
    """Accumulate one fill's mark move in `pending_flow` for the next
    tick's vol-state update (and Phase H flow effects).

    `user_id` 0 is reserved for system flow (forced liquidation legs);
    real users carry their Discord id so the per-account contribution cap
    (vol.account_flow_cap) can bind. The caller MUST already hold the
    instrument row lock -- apply_tick holds every instrument lock while it
    consumes the table, which is what makes the accumulation race-free
    without any locking of its own.
    """
    async with conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO pending_flow
                (instrument_id, user_id, tick_index,
                 delta_impact, signed_notional_minor)
            VALUES (%s, %s,
                    (SELECT COALESCE(MAX(tick_index), 0) FROM market_ticks),
                    %s, %s)
            ON CONFLICT (instrument_id, user_id) DO UPDATE SET
                tick_index = EXCLUDED.tick_index,
                delta_impact = pending_flow.delta_impact + EXCLUDED.delta_impact,
                signed_notional_minor = pending_flow.signed_notional_minor
                                   + EXCLUDED.signed_notional_minor
            """,
            (instrument_id, user_id, delta_impact, signed_notional_minor),
        )


def bound_flow(
    deltas_by_user: dict[int, float],
    account_cap: float,
    total_cap: float,
) -> tuple[float, float]:
    """Apply the F5 flow bounds to one instrument's tick-interval deltas.

    Returns (bounded_flow, raw_flow): each real account's contribution is
    clipped to +/-account_cap, system flow (user_id 0) passes through, and
    the sum is clipped to +/-total_cap. `raw_flow` is the unbounded sum --
    kept for breaker attribution, never fed to the EWMA uncapped.
    """
    bounded = 0.0
    raw = 0.0
    for user_id, delta in deltas_by_user.items():
        raw += delta
        bounded += delta if user_id == 0 else max(-account_cap, min(account_cap, delta))
    return max(-total_cap, min(total_cap, bounded)), raw


async def flow_config(conn: AsyncConnection) -> dict[str, float]:
    """The `flow.*` config namespace (cross-impact coefficient, permanent-
    impact fraction + cap, ADV window/multiplier bounds), read fresh like
    `spread_config`."""
    async with conn.cursor() as cur:
        await cur.execute("SELECT key, value FROM config WHERE key LIKE 'flow.%'")
        return {str(k): float(v) for k, v in await cur.fetchall()}


async def session_config(conn: AsyncConnection) -> dict[str, float]:
    """The `session.*` config namespace (open/closed ticks, phase offset,
    open impact reset), read fresh like `spread_config`."""
    async with conn.cursor() as cur:
        await cur.execute("SELECT key, value FROM config WHERE key LIKE 'session.%'")
        return {str(k): float(v) for k, v in await cur.fetchall()}


def session_parts(cfg: dict[str, float]) -> tuple[int, int, int]:
    """(open_ticks, closed_ticks, offset_ticks) from a `session.*` cfg dict,
    defaulting to a market that never closes."""
    open_ticks = int(cfg.get("session.open_ticks", TICKS_PER_DAY))
    closed_ticks = int(cfg.get("session.closed_ticks", 0))
    offset = int(cfg.get("session.phase_offset_ticks", 0))
    return open_ticks, closed_ticks, offset


def session_open_fraction(cfg: dict[str, float]) -> float:
    """Fraction of ticks that are open -- used to scale the dividend drift
    offset so payouts stay funded when only open ticks step the model."""
    open_ticks, closed_ticks, _ = session_parts(cfg)
    return open_ticks / max(open_ticks + closed_ticks, 1)


async def current_session(conn: AsyncConnection) -> tuple[str, int | None, dict[str, float]]:
    """('OPEN'|'CLOSED', last applied tick, session cfg). No ticks applied
    yet counts as OPEN so bootstrap/trading-before-first-tick still works."""
    cfg = await session_config(conn)
    tick = await current_tick_index(conn)
    if tick is None:
        return "OPEN", None, cfg
    open_ticks, closed_ticks, offset = session_parts(cfg)
    return engine.session_phase(tick, open_ticks, closed_ticks, offset), tick, cfg


async def feature_enabled_flag(conn: AsyncConnection, key: str) -> bool:
    """Non-raising kill-switch read for tick-internal call sites
    (match_orders) that silently skip rather than error to a user."""
    return await _config_float(conn, key, 1.0) != 0.0


async def assert_feature_enabled(conn: AsyncConnection, key: str, label: str) -> None:
    """Kill switch: raise FeatureDisabledError when a `*.enabled` config
    row is 0. Missing rows default to enabled."""
    if await _config_float(conn, key, 1.0) == 0.0:
        # Lazy import for the same trading.errors cycle as below.
        from stockbot.trading.errors import FeatureDisabledError

        raise FeatureDisabledError(label)


async def assert_market_open(conn: AsyncConnection) -> None:
    """Raise MarketClosedError when the last applied tick is a closed tick.

    The session of "now" is the phase of the most recent committed tick:
    a trade between tick T and T+1 executes against tick-T marks, so it
    belongs to T's session."""
    # Lazy: trading.errors -> trading/__init__ -> trading.service imports
    # this module back, so a top-level import here would be circular.
    from stockbot.trading.errors import MarketClosedError

    phase, tick, cfg = await current_session(conn)
    if phase == "CLOSED" and tick is not None:
        open_ticks, closed_ticks, offset = session_parts(cfg)
        raise MarketClosedError(
            engine.ticks_until_open(tick, open_ticks, closed_ticks, offset)
        )


def half_spread_for(
    row: dict[str, Any], current_tick: int | None, cfg: dict[str, float]
) -> float:
    """Dynamic half-spread fraction for an instrument row carrying `sigma`,
    `liquidity`, `last_halt_end_tick`, and `next_event_tick`. Shared by all
    fill paths (trades, bounded shorts, liquidation legs, order fills).
    When `cfg` also carries the `session.*` keys (merged in by the
    caller), the U-shaped intraday open/close terms engage."""
    since_halt = (
        None
        if row["last_halt_end_tick"] is None or current_tick is None
        else float(current_tick - int(row["last_halt_end_tick"]))
    )
    to_event = (
        None
        if row["next_event_tick"] is None or current_tick is None
        else float(int(row["next_event_tick"]) - current_tick)
    )
    since_open = to_close = None
    open_ticks = int(cfg.get("session.open_ticks", 0))
    closed_ticks = int(cfg.get("session.closed_ticks", 0))
    cycle = open_ticks + closed_ticks
    if cycle > 0 and current_tick is not None:
        offset = int(cfg.get("session.phase_offset_ticks", 0))
        cycle_pos = (current_tick - offset) % cycle
        if cycle_pos < open_ticks:
            since_open = float(cycle_pos)
            to_close = float(open_ticks - cycle_pos)
    return engine.half_spread_fraction(
        cfg=cfg,
        sigma=float(row["sigma"]),
        liquidity=float(row["liquidity"]),
        ticks_since_halt=since_halt,
        ticks_to_event=to_event,
        ticks_since_open=since_open,
        ticks_to_close=to_close,
    )


async def all_instrument_snapshots(conn: AsyncConnection) -> list[InstrumentSnapshot]:
    return await _fetch_snapshots(conn, None)


async def get_instrument_snapshot(conn: AsyncConnection, ticker: str) -> InstrumentSnapshot | None:
    rows = await _fetch_snapshots(conn, ticker.upper())
    return rows[0] if rows else None
