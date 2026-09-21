"""Resting limit orders, matched once per tick inside apply_tick.

Matching rule: an order fills when the impact-adjusted executable price
satisfies its limit -- a BUY fills only if the buy-side fill is at or below
its limit, a SELL only if the sell-side fill is at or above it. The fill
executes through `execute_trade` (same fees, impact, margin gates as a
market order) inside a savepoint, so a failing order rolls back cleanly and
stays OPEN for a later tick. Orders don't reserve cash or shares at
placement; fill-time validation is authoritative.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal

from psycopg import AsyncConnection
from psycopg.rows import dict_row

from stockbot.ledger.errors import LedgerError
from stockbot.ledger.service import record_idempotency_key
from stockbot.margin.errors import MarginError
from stockbot.market import engine
from stockbot.trading.errors import TradingError, UnknownInstrumentError
from stockbot.trading.service import HALF_SPREAD_BPS, execute_trade

Side = Literal["BUY", "SELL"]


@dataclass(frozen=True)
class OrderResult:
    order_id: int
    ticker: str
    side: Side
    quantity: int
    limit_price: Decimal
    expires_tick: int | None


async def _current_tick(conn: AsyncConnection) -> int | None:
    async with conn.cursor() as cur:
        await cur.execute("SELECT MAX(tick_index) FROM market_ticks")
        row = await cur.fetchone()
    return int(row[0]) if row and row[0] is not None else None


async def place_order(
    conn: AsyncConnection,
    *,
    user_id: int,
    ticker: str,
    side: Side,
    quantity: int,
    limit_price: Decimal,
    season_id: int | None = None,
    expires_in_ticks: int | None = None,
    interaction_id: str | None = None,
) -> OrderResult:
    """Validate and rest a limit order. Marketability/margin are re-checked at
    fill time; placement only rejects structurally bad orders."""
    if quantity <= 0:
        raise ValueError("quantity must be positive")
    if limit_price <= 0:
        raise ValueError("limit price must be positive")
    ticker = ticker.upper()

    async with conn.transaction(), conn.cursor() as cur:
        if interaction_id is not None:
            await record_idempotency_key(conn, interaction_id)
        await cur.execute(
            "SELECT id, is_active FROM instruments WHERE ticker = %s", (ticker,)
        )
        row = await cur.fetchone()
        if row is None or not row[1]:
            raise UnknownInstrumentError(ticker)
        instrument_id = int(row[0])
        tick = await _current_tick(conn)
        expires_tick = (
            None if expires_in_ticks is None else (tick or 0) + expires_in_ticks
        )
        await cur.execute(
            """
            INSERT INTO orders
                (user_id, instrument_id, season_id, side, quantity,
                 limit_price, opened_tick, expires_tick)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                user_id,
                instrument_id,
                season_id,
                side,
                quantity,
                limit_price,
                tick,
                expires_tick,
            ),
        )
        order_row = await cur.fetchone()
        assert order_row is not None
        order_id = int(order_row[0])

    return OrderResult(
        order_id=order_id,
        ticker=ticker,
        side=side,
        quantity=quantity,
        limit_price=limit_price,
        expires_tick=expires_tick,
    )


async def cancel_order(conn: AsyncConnection, *, user_id: int, order_id: int) -> bool:
    """Cancel an open order owned by `user_id`. Returns False if not found."""
    async with conn.transaction(), conn.cursor() as cur:
        await cur.execute(
            """
            UPDATE orders SET status = 'CANCELLED'
            WHERE id = %s AND user_id = %s AND status = 'OPEN'
            """,
            (order_id, user_id),
        )
        return cur.rowcount > 0


async def list_open_orders(
    conn: AsyncConnection, user_id: int, season_id: int | None = None
) -> list[dict[str, Any]]:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            SELECT o.id, i.ticker, o.side, o.quantity, o.limit_price,
                   i.quoted_price, o.expires_tick, o.created_at
            FROM orders o
            JOIN instruments i ON i.id = o.instrument_id
            WHERE o.user_id = %s AND o.status = 'OPEN'
              AND o.season_id IS NOT DISTINCT FROM %s
            ORDER BY o.id
            """,
            (user_id, season_id),
        )
        return await cur.fetchall()


async def match_orders(conn: AsyncConnection, tick_index: int) -> int:
    """Fill every open order whose limit is satisfied at this tick's marks.

    Called inside apply_tick's transaction: all instruments are already
    locked, accounts lock afterwards inside execute_trade -- consistent with
    the project's lock ordering. Returns the number of fills.
    """
    fills = 0
    async with conn.cursor(row_factory=dict_row) as cur:
        # Expire first, then scan orders whose limit is within reach of the
        # mark: a BUY needs quoted <= limit, a SELL quoted >= limit (impact
        # and half-spread only move the executable price against the taker,
        # so a mark on the wrong side can never produce a valid fill).
        await cur.execute(
            """
            UPDATE orders SET status = 'EXPIRED'
            WHERE status = 'OPEN' AND expires_tick IS NOT NULL
              AND expires_tick <= %s
            """,
            (tick_index,),
        )
        await cur.execute(
            """
            SELECT o.id, o.user_id, o.season_id, o.side, o.quantity,
                   o.limit_price, i.ticker, i.base_price, i.impact,
                   i.liquidity, i.lambda_impact, i.max_impact
            FROM orders o
            JOIN instruments i ON i.id = o.instrument_id
            WHERE o.status = 'OPEN'
              AND ((o.side = 'BUY'  AND i.quoted_price <= o.limit_price)
                OR (o.side = 'SELL' AND i.quoted_price >= o.limit_price))
            ORDER BY o.id
            """,
        )
        candidates = await cur.fetchall()

    for order in candidates:
        # Executable price check: the fill path includes half-spread and
        # impact; verify the worst-case fill respects the limit before
        # committing to the trade.
        fill_f, _ = engine.apply_trade_impact(
            base_price=float(order["base_price"]),
            impact_before=float(order["impact"]),
            signed_notional=float(order["base_price"]) * int(order["quantity"])
            * (1 if order["side"] == "BUY" else -1),
            liquidity=float(order["liquidity"]),
            lambda_impact=float(order["lambda_impact"]),
            max_impact=float(order["max_impact"]),
            half_spread=float(HALF_SPREAD_BPS) / 10_000,
        )
        limit = Decimal(order["limit_price"])
        if order["side"] == "BUY" and Decimal(str(fill_f)) > limit:
            continue
        if order["side"] == "SELL" and Decimal(str(fill_f)) < limit:
            continue

        try:
            async with conn.transaction():
                result = await execute_trade(
                    conn,
                    user_id=int(order["user_id"]),
                    ticker=str(order["ticker"]),
                    side=order["side"],
                    quantity=int(order["quantity"]),
                    season_id=(
                        int(order["season_id"])
                        if order["season_id"] is not None
                        else None
                    ),
                )
                async with conn.cursor() as cur:
                    await cur.execute(
                        """
                        UPDATE orders
                        SET status = 'FILLED', filled_tick = %s, fill_price = %s
                        WHERE id = %s
                        """,
                        (tick_index, result.fill_price, order["id"]),
                    )
                fills += 1
        except (TradingError, MarginError, LedgerError, ValueError):
            # Not fillable right now (funds, margin gates, slot limits) --
            # the savepoint rolls the attempt back and the order stays OPEN.
            continue

    return fills
