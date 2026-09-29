"""Per-archetype NPC decision trees, ported per-tick from
`simulation/harness.py`'s `_run_agent_day`.

Differences from the harness, all deliberate:

- NO `claim_daily` anywhere (C6/permadeath): agents are stake-funded and
  P&L-sustained; a daily faucet claim would be a refill path.
- The harness's per-day `chance` rolls are replaced by the runner's
  `npc.action_prob_per_tick` gate; the inner decision forks (cover vs
  open, re-enter odds) copy near-verbatim.
- Venue awareness (C10): ticker picks go through `_random_open_ticker`
  so a closed-venue pick can't dead-letter inside the error catch.
- `wash_trader` and `farmer` are not ported: one exists only to exercise
  the detector, the other only claims.
- `stop_loss` reuses `npc_agents.quote_ticker` as its pinned name (NULL
  picks fresh each action -- same decision tree).

Each action returns the executed notional in minor units for the
round's `npc.max_tick_notional` budget; resting orders return 0
(placements aren't flow -- fills are bounded by the book's own caps).
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from decimal import Decimal

from psycopg import AsyncConnection
from psycopg.rows import dict_row

from stockbot.ledger.service import get_balance, get_user_account_id
from stockbot.margin.service import compute_health
from stockbot.market.data import (
    half_spread_for,
    instrument_session_cfg,
    spread_config,
)
from stockbot.orders.service import cancel_order, list_open_orders, place_order

TICKS_PER_DAY = 1440


@dataclass
class RoundContext:
    conn: AsyncConnection
    rng: random.Random
    tick_index: int
    open_mids: set[int]
    budget_minor: int  # remaining npc.max_tick_notional this round


@dataclass(frozen=True)
class NpcAgent:
    user_id: int
    archetype: str
    label: str
    quote_ticker: str | None


async def _config_float(conn: AsyncConnection, key: str, default: float) -> float:
    async with conn.cursor() as cur:
        await cur.execute("SELECT value FROM config WHERE key = %s", (key,))
        row = await cur.fetchone()
    return float(row[0]) if row else default


async def _random_open_ticker(
    conn: AsyncConnection, rng: random.Random, open_mids: set[int]
) -> str | None:
    """Venue-scoped pick (C10): a closed-venue ticker would only raise
    MarketClosedError into the broad catch -- the same trap that once
    made the harness silently place zero orders."""
    if not open_mids:
        return None
    async with conn.cursor() as cur:
        # ORDER BY: rng.choice() must be deterministic for a given seed.
        await cur.execute(
            """
            SELECT ticker FROM instruments
            WHERE is_active AND circuit_halted_until_tick IS NULL
              AND market_id = ANY(%s)
            ORDER BY ticker
            """,
            (sorted(open_mids),),
        )
        tickers = [row[0] for row in await cur.fetchall()]
    return rng.choice(tickers) if tickers else None


async def _position_qty(conn: AsyncConnection, user_id: int, ticker: str) -> int:
    async with conn.cursor() as cur:
        await cur.execute(
            """
            SELECT p.quantity FROM positions p
            JOIN instruments i ON i.id = p.instrument_id
            WHERE p.user_id = %s AND i.ticker = %s AND p.season_id IS NULL
            """,
            (user_id, ticker),
        )
        row = await cur.fetchone()
    return int(row[0]) if row else 0


async def _quote_minor(conn: AsyncConnection, ticker: str) -> int | None:
    """Mark in minor units; None when the instrument is gone or halted
    (a halted name can't be traded, same as the harness's NULL check)."""
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT quoted_price FROM instruments WHERE ticker = %s "
            "AND circuit_halted_until_tick IS NULL",
            (ticker,),
        )
        row = await cur.fetchone()
    return int(float(row[0]) * 100) if row else None


def _quantity_for_spend(spend_minor: int, price_minor: int) -> int:
    return max(1, spend_minor // max(price_minor, 1))


async def _buy(ctx: RoundContext, agent: NpcAgent, lo: float, hi: float) -> int:
    """Shared buy-the-mark body for the long-side archetypes; they differ
    only in the balance fraction they spend."""
    ticker = await _random_open_ticker(ctx.conn, ctx.rng, ctx.open_mids)
    if ticker is None:
        return 0
    account_id = await get_user_account_id(ctx.conn, agent.user_id)
    balance = await get_balance(ctx.conn, account_id)
    spend = int(balance * ctx.rng.uniform(lo, hi))
    if spend < 100:
        return 0
    price_minor = await _quote_minor(ctx.conn, ticker)
    if price_minor is None:
        return 0
    quantity = _quantity_for_spend(spend, price_minor)
    from stockbot.trading.service import execute_trade  # lazy: avoids cycles

    await execute_trade(
        ctx.conn, user_id=agent.user_id, ticker=ticker, side="BUY", quantity=quantity
    )
    return min(spend, quantity * price_minor)


async def _act_whale(ctx: RoundContext, agent: NpcAgent) -> int:
    return await _buy(ctx, agent, 0.05, 0.20)


async def _act_grinder(ctx: RoundContext, agent: NpcAgent) -> int:
    return await _buy(ctx, agent, 0.05, 0.15)


async def _act_yolo(ctx: RoundContext, agent: NpcAgent) -> int:
    return await _buy(ctx, agent, 0.50, 0.95)


async def _act_shorter(ctx: RoundContext, agent: NpcAgent) -> int:
    """Real-margin shorts (spawn grants the tier): cover an open short
    half the time, otherwise open one sized 30-80% of equity. NPC
    liquidations are the product."""
    conn = ctx.conn
    async with conn.cursor() as cur:
        await cur.execute(
            """
            SELECT i.ticker, p.quantity FROM positions p
            JOIN instruments i ON i.id = p.instrument_id
            WHERE p.user_id = %s AND p.quantity < 0 AND p.season_id IS NULL
            ORDER BY i.ticker
            """,
            (agent.user_id,),
        )
        shorts_held = [(str(r[0]), int(r[1])) for r in await cur.fetchall()]
    from stockbot.trading.service import execute_trade  # lazy: avoids cycles

    if shorts_held:
        if ctx.rng.random() >= 0.5:
            return 0
        cover_ticker, qty = ctx.rng.choice(shorts_held)
        cover_price = await _quote_minor(conn, cover_ticker) or 0
        await execute_trade(
            conn,
            user_id=agent.user_id,
            ticker=cover_ticker,
            side="BUY",
            quantity=-qty,
        )
        return -qty * cover_price

    health = await compute_health(conn, agent.user_id, None)
    ticker = await _random_open_ticker(conn, ctx.rng, ctx.open_mids)
    if ticker is None:
        return 0
    price_minor = await _quote_minor(conn, ticker)
    if price_minor is None:
        return 0
    target_notional = int(health.equity_minor * ctx.rng.uniform(0.3, 0.8))
    quantity = target_notional // max(price_minor, 1)
    if quantity < 1:
        return 0
    await execute_trade(conn, user_id=agent.user_id, ticker=ticker, side="SELL", quantity=quantity)
    return quantity * price_minor


async def _act_liquidity_provider(ctx: RoundContext, agent: NpcAgent) -> int:
    """Two-sided quote on its assigned name (or a fresh open-venue pick).

    Min-spread floor (P3): the quote offset is at least the MM's own
    dynamic half-spread plus `npc.lp_min_spread` -- quoting inside the
    MM's spread gets the agent scalped the moment the mark drifts; this
    is tuning, not safety (C6)."""
    conn = ctx.conn
    ticker = agent.quote_ticker or await _random_open_ticker(conn, ctx.rng, ctx.open_mids)
    if ticker is None:
        return 0
    # Prune order-count growth the way the harness does: list is
    # ORDER BY id (oldest first), so [6:] cancels the NEWEST -- the six
    # oldest stay resting. Stale paper is deliberate exposure here: far
    # quotes still cross fresh flow as marks drift, and the 2-day expiry
    # bounds how long they linger.
    open_orders = await list_open_orders(conn, agent.user_id)
    for open_order in open_orders[6:]:
        await cancel_order(conn, user_id=agent.user_id, order_id=int(open_order["id"]))
    # dict shape for half_spread_for: needs the named columns.
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            SELECT id, quoted_price, sigma, liquidity,
                   last_halt_end_tick, next_event_tick
            FROM instruments
            WHERE ticker = %s AND circuit_halted_until_tick IS NULL
            """,
            (ticker,),
        )
        row = await cur.fetchone()
    if row is None:
        return 0
    mark = Decimal(str(row["quoted_price"]))
    account_id = await get_user_account_id(conn, agent.user_id)
    balance = await get_balance(conn, account_id)
    spread_cfg = {
        **await spread_config(conn),
        **await instrument_session_cfg(conn, int(row["id"])),
    }
    half_spread = half_spread_for(row, ctx.tick_index, spread_cfg)
    floor = Decimal(str(half_spread)) + Decimal(
        str(await _config_float(conn, "npc.lp_min_spread", 0.002))
    )
    offset = max(Decimal(str(round(ctx.rng.uniform(0.005, 0.015), 4))), floor)
    bid_qty = max(1, int(balance / 100 * 0.15 / float(mark)))
    await place_order(
        conn,
        user_id=agent.user_id,
        ticker=ticker,
        side="BUY",
        quantity=bid_qty,
        limit_price=(mark * (1 - offset)).quantize(Decimal("0.000001")),
        expires_in_ticks=2 * TICKS_PER_DAY,
    )
    held = await _position_qty(conn, agent.user_id, ticker)
    if held > 0:
        await place_order(
            conn,
            user_id=agent.user_id,
            ticker=ticker,
            side="SELL",
            quantity=min(held, bid_qty),
            limit_price=(mark * (1 + offset)).quantize(Decimal("0.000001")),
            expires_in_ticks=2 * TICKS_PER_DAY,
        )
    return 0  # resting quotes are depth, not flow


async def _act_stop_loss(ctx: RoundContext, agent: NpcAgent) -> int:
    """Holds one name with a protective SELL stop trailing ~4-9% under
    the mark; re-enters at market after being stopped out."""
    conn = ctx.conn
    ticker = agent.quote_ticker or await _random_open_ticker(conn, ctx.rng, ctx.open_mids)
    if ticker is None:
        return 0
    held = await _position_qty(conn, agent.user_id, ticker)
    price_minor = await _quote_minor(conn, ticker)
    if price_minor is None:
        return 0  # halted or gone: leave the stop to rest
    mark = Decimal(str(price_minor)) / 100
    open_orders = await list_open_orders(conn, agent.user_id)
    has_stop = any(o["order_type"] != "LIMIT" and o["ticker"] == ticker for o in open_orders)
    notional = 0
    if held <= 0:
        # Flat (fresh start or just stopped out): re-enter at market
        # most actions, then re-arm the stop below.
        if ctx.rng.random() >= 0.6:
            return 0
        account_id = await get_user_account_id(conn, agent.user_id)
        balance = await get_balance(conn, account_id)
        spend = int(balance * ctx.rng.uniform(0.4, 0.7))
        qty = _quantity_for_spend(spend, price_minor)
        if qty < 1:
            return 0
        from stockbot.trading.service import execute_trade

        await execute_trade(conn, user_id=agent.user_id, ticker=ticker, side="BUY", quantity=qty)
        held = qty
        notional = min(spend, qty * price_minor)
    if held > 0 and not has_stop:
        stop_pct = Decimal(str(round(ctx.rng.uniform(0.04, 0.09), 4)))
        await place_order(
            conn,
            user_id=agent.user_id,
            ticker=ticker,
            side="SELL",
            quantity=held,
            limit_price=None,
            stop_price=(mark * (1 - stop_pct)).quantize(Decimal("0.000001")),
            expires_in_ticks=3 * TICKS_PER_DAY,
        )
    return notional


ACTIONS = {
    "whale": _act_whale,
    "grinder": _act_grinder,
    "yolo": _act_yolo,
    "shorter": _act_shorter,
    "liquidity_provider": _act_liquidity_provider,
    "stop_loss": _act_stop_loss,
}
