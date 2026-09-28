"""Phase C: participation cap (InsufficientDepthError on oversized fills,
partial MM fills up to the cap per tick, forced-close bypass), sqrt
impact, and the trade-through epsilon for the MM fallback."""

from __future__ import annotations

from decimal import Decimal

import pytest
from psycopg import AsyncConnection
from psycopg.rows import dict_row

from stockbot.accounts.service import bootstrap_user
from stockbot.ledger.service import (
    get_balance,
    get_system_account_id,
    get_user_account_id,
    post_transfer,
)
from stockbot.margin.service import check_and_liquidate, compute_health
from stockbot.orders.service import match_orders, place_order
from stockbot.trading.errors import InsufficientDepthError
from stockbot.trading.service import execute_trade


async def _ticker_by_liquidity(conn: AsyncConnection, *, smallest: bool) -> tuple[str, float]:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT ticker, liquidity FROM instruments "
            "WHERE is_active AND kind != 'INDEX' "
            f"ORDER BY liquidity {'ASC' if smallest else 'DESC'} LIMIT 1"
        )
        row = await cur.fetchone()
        assert row is not None
        return str(row[0]), float(row[1])


async def _quoted(conn: AsyncConnection, ticker: str) -> Decimal:
    async with conn.cursor() as cur:
        await cur.execute("SELECT quoted_price FROM instruments WHERE ticker = %s", (ticker,))
        row = await cur.fetchone()
        assert row is not None
        return Decimal(row[0])


async def _fund(conn: AsyncConnection, user_id: int, amount_minor: int) -> None:
    faucet = await get_system_account_id(conn, "FAUCET")
    account = await get_user_account_id(conn, user_id)
    await post_transfer(
        conn,
        from_account_id=faucet,
        to_account_id=account,
        amount=amount_minor,
        reason="TEST_FUND",
    )


async def _grant_tier(conn: AsyncConnection, user_id: int, tier: int = 3) -> None:
    async with conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO entitlements (user_id, item_key, quantity)
            VALUES (%s, 'margin_tier', %s)
            ON CONFLICT (user_id, item_key)
            DO UPDATE SET quantity = EXCLUDED.quantity
            """,
            (user_id, tier),
        )


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


async def _order_row(conn: AsyncConnection, order_id: int) -> dict:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT status, filled_quantity FROM orders WHERE id = %s",
            (order_id,),
        )
        row = await cur.fetchone()
        assert row is not None
        return dict(row)


async def test_market_order_over_cap_rejected(conn: AsyncConnection) -> None:
    """A single marketable fill bigger than cap*liquidity raises
    InsufficientDepthError and leaves no trace."""
    account_id = (await bootstrap_user(conn, 5001)).account_id
    await _fund(conn, 5001, 100_000_000)  # $1M -- funds aren't the blocker
    ticker, liquidity = await _ticker_by_liquidity(conn, smallest=True)
    mark = await _quoted(conn, ticker)
    cap_dollars = Decimal(str(liquidity)) * Decimal("0.10")
    # Just over the cap in notional.
    qty = int(cap_dollars / mark) + 1

    balance_before = await get_balance(conn, account_id)
    with pytest.raises(InsufficientDepthError):
        await execute_trade(conn, user_id=5001, ticker=ticker, side="BUY", quantity=qty)

    assert await get_balance(conn, account_id) == balance_before
    assert await _position_qty(conn, 5001, ticker) == 0


async def test_resting_order_works_over_multiple_ticks(conn: AsyncConnection) -> None:
    """An oversized resting order partially fills up to the cap each tick
    instead of failing outright -- the 'work the order' behavior."""
    await bootstrap_user(conn, 5002)
    await _fund(conn, 5002, 100_000_000)
    ticker, liquidity = await _ticker_by_liquidity(conn, smallest=True)
    mark = await _quoted(conn, ticker)
    cap_dollars = Decimal(str(liquidity)) * Decimal("0.10")
    # ~2.5x the cap: needs at least 3 ticks to fill fully.
    qty = int(cap_dollars * Decimal("2.5") / mark)

    order = await place_order(
        conn,
        user_id=5002,
        ticker=ticker,
        side="BUY",
        quantity=qty,
        limit_price=(mark * Decimal("1.05")).quantize(Decimal("0.000001")),
    )

    fills1 = await match_orders(conn, 1)
    row1 = await _order_row(conn, order.order_id)
    assert fills1 == 1
    assert row1["status"] == "OPEN"
    filled1 = int(row1["filled_quantity"])
    assert 0 < filled1 < qty
    # The partial fill respected the cap (within a share of rounding).
    assert filled1 * mark <= cap_dollars * Decimal("1.05")

    fills2 = await match_orders(conn, 2)
    row2 = await _order_row(conn, order.order_id)
    assert fills2 == 1
    assert int(row2["filled_quantity"]) > filled1
    assert await _position_qty(conn, 5002, ticker) == int(row2["filled_quantity"])


async def test_trade_through_epsilon_gates_mm_fills(conn: AsyncConnection) -> None:
    """With the epsilon set wide, a limit the mark merely touches stays
    open while a properly crossed one fills."""
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE config SET value = 0.01 WHERE key = 'cross.trade_through_epsilon'"
        )
    await bootstrap_user(conn, 5003)
    await _fund(conn, 5003, 10_000_000)
    ticker, _ = await _ticker_by_liquidity(conn, smallest=False)
    mark = await _quoted(conn, ticker)
    # Seed a position to sell.
    await execute_trade(conn, user_id=5003, ticker=ticker, side="BUY", quantity=10)
    mark = await _quoted(conn, ticker)

    touching = await place_order(
        conn,
        user_id=5003,
        ticker=ticker,
        side="SELL",
        quantity=1,
        # 0.5% below the mark -- marketable, but within the 1% epsilon.
        limit_price=(mark * Decimal("0.995")).quantize(Decimal("0.000001")),
    )
    crossed = await place_order(
        conn,
        user_id=5003,
        ticker=ticker,
        side="SELL",
        quantity=1,
        # 2% below the mark -- traded through by more than the epsilon.
        limit_price=(mark * Decimal("0.98")).quantize(Decimal("0.000001")),
    )
    fills = await match_orders(conn, 1)

    assert fills == 1
    assert (await _order_row(conn, touching.order_id))["status"] == "OPEN"
    assert (await _order_row(conn, crossed.order_id))["status"] == "FILLED"


async def test_forced_close_bypasses_participation_cap(conn: AsyncConnection) -> None:
    """A short bigger than cap*liquidity (built across several under-cap
    fills) still liquidates completely -- a capped leg would wedge the
    account below maintenance forever."""
    await bootstrap_user(conn, 5004)
    await _grant_tier(conn, 5004, tier=3)
    ticker, liquidity = await _ticker_by_liquidity(conn, smallest=True)
    async with conn.cursor() as cur:
        # adv at the reference ratio pins adv_mult at 1, and vol_state = 1
        # clears committed vol-regime drift, so the effective liquidity the
        # cap now binds on equals static liquidity -- the slices below keep
        # their ~0.8x-cap sizing.
        await cur.execute(
            "UPDATE instruments SET init_margin_pct = 0.5, "
            "maint_margin_pct = 0.3, adv = liquidity * 2.5e-8, "
            "vol_state = 1.0 "
            "WHERE ticker = %s RETURNING quoted_price",
            (ticker,),
        )
        (price,) = await cur.fetchone()
    mark = Decimal(str(price))
    cap_dollars = Decimal(str(liquidity)) * Decimal("0.10")
    # Fund so the user can margin a short ~1.8x the cap (3x leverage cap ok).
    equity = cap_dollars * Decimal("1.2")
    await _fund(conn, 5004, _minor(equity))
    total_qty = int(cap_dollars * Decimal("1.8") / mark)
    slice_qty = int(cap_dollars * Decimal("0.8") / mark)
    opened = 0
    while opened < total_qty:
        step = min(slice_qty, total_qty - opened)
        await execute_trade(conn, user_id=5004, ticker=ticker, side="SELL", quantity=step)
        opened += step
    assert await _position_qty(conn, 5004, ticker) == -total_qty

    # Gap 3x against the short: the cover legs each exceed the cap but the
    # forced close must still complete.
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE instruments SET base_price = base_price * 3, "
            "quoted_price = quoted_price * 3 WHERE ticker = %s",
            (ticker,),
        )
    health = await compute_health(conn, 5004)
    assert health.undermargined

    legs = await check_and_liquidate(conn, 5004)
    assert legs >= 1
    pos = await _position_qty(conn, 5004, ticker)
    assert pos == 0
    health = await compute_health(conn, 5004)
    assert health.equity_minor >= 0


def _minor(dollars: Decimal) -> int:
    return int((dollars * 100).quantize(Decimal("1")))
