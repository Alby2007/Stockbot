"""Limit orders: placement, tick-time matching, expiry, cancellation."""

from __future__ import annotations

from decimal import Decimal

import pytest
from psycopg import AsyncConnection

from stockbot.accounts.service import bootstrap_user
from stockbot.ledger.service import (
    get_balance,
    get_system_account_id,
    get_user_account_id,
    post_transfer,
)
from stockbot.market.tick import apply_tick
from stockbot.orders.service import (
    cancel_order,
    list_open_orders,
    match_orders,
    place_order,
)
from stockbot.trading.errors import UnknownInstrumentError
from stockbot.trading.service import execute_trade

SEED = "orders-test-seed"


async def _quoted(conn: AsyncConnection, ticker: str) -> Decimal:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT quoted_price FROM instruments WHERE ticker = %s", (ticker,)
        )
        row = await cur.fetchone()
        assert row is not None
        return Decimal(row[0])


async def _first_ticker(conn: AsyncConnection) -> str:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT ticker FROM instruments WHERE is_active AND kind != 'INDEX' "
            "ORDER BY ticker LIMIT 1"
        )
        row = await cur.fetchone()
        assert row is not None
        return str(row[0])


async def _order_status(conn: AsyncConnection, order_id: int) -> str:
    async with conn.cursor() as cur:
        await cur.execute("SELECT status FROM orders WHERE id = %s", (order_id,))
        row = await cur.fetchone()
        assert row is not None
        return str(row[0])


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


async def test_place_order_rejects_unknown_ticker(conn: AsyncConnection) -> None:
    await bootstrap_user(conn, 3001)
    with pytest.raises(UnknownInstrumentError):
        await place_order(
            conn,
            user_id=3001,
            ticker="NOPE",
            side="BUY",
            quantity=1,
            limit_price=Decimal("10"),
        )


async def test_limit_buy_fills_when_mark_dips(conn: AsyncConnection) -> None:
    await bootstrap_user(conn, 3002)
    ticker = await _first_ticker(conn)
    mark = await _quoted(conn, ticker)
    # Limit far above the mark fills next tick; limit far below stays open.
    high = await place_order(
        conn, user_id=3002, ticker=ticker, side="BUY", quantity=1,
        limit_price=mark * 2,
    )
    low = await place_order(
        conn, user_id=3002, ticker=ticker, side="BUY", quantity=1,
        limit_price=Decimal("0.01"),
    )
    await apply_tick(conn, SEED)
    assert await _order_status(conn, high.order_id) == "FILLED"
    assert await _order_status(conn, low.order_id) == "OPEN"
    assert await _position_qty(conn, 3002, ticker) == 1


async def test_limit_sell_fills_when_mark_rises(conn: AsyncConnection) -> None:
    await bootstrap_user(conn, 3003)
    ticker = await _first_ticker(conn)
    await execute_trade(conn, user_id=3003, ticker=ticker, side="BUY", quantity=1)
    order = await place_order(
        conn, user_id=3003, ticker=ticker, side="SELL", quantity=1,
        limit_price=Decimal("0.01"),  # absurdly low -> fills next tick
    )
    await apply_tick(conn, SEED)
    assert await _order_status(conn, order.order_id) == "FILLED"
    assert await _position_qty(conn, 3003, ticker) == 0


async def test_order_expires(conn: AsyncConnection) -> None:
    await bootstrap_user(conn, 3004)
    ticker = await _first_ticker(conn)
    order = await place_order(
        conn, user_id=3004, ticker=ticker, side="BUY", quantity=1,
        limit_price=Decimal("0.01"), expires_in_ticks=1,
    )
    await apply_tick(conn, SEED)
    await apply_tick(conn, SEED)
    assert await _order_status(conn, order.order_id) == "EXPIRED"


async def test_cancel_order(conn: AsyncConnection) -> None:
    await bootstrap_user(conn, 3005)
    ticker = await _first_ticker(conn)
    order = await place_order(
        conn, user_id=3005, ticker=ticker, side="BUY", quantity=1,
        limit_price=Decimal("0.01"),
    )
    assert await cancel_order(conn, user_id=3005, order_id=order.order_id)
    assert await _order_status(conn, order.order_id) == "CANCELLED"
    # can't cancel someone else's or a non-open order
    assert not await cancel_order(conn, user_id=3005, order_id=order.order_id)
    assert not await cancel_order(conn, user_id=9999, order_id=order.order_id)


async def test_order_unfillable_stays_open(conn: AsyncConnection) -> None:
    """A limit buy that crosses the mark but exceeds cash stays open."""
    await bootstrap_user(conn, 3006)
    ticker = await _first_ticker(conn)
    mark = await _quoted(conn, ticker)
    # Drain the account so the fill can't afford itself.
    account_id = await get_user_account_id(conn, 3006)
    balance = await get_balance(conn, account_id)
    sink = await get_system_account_id(conn, "SINK")
    await post_transfer(
        conn,
        from_account_id=account_id,
        to_account_id=sink,
        amount=balance - 1,  # leave $0.01 -- can't afford anything
        reason="TEST",
    )
    order = await place_order(
        conn, user_id=3006, ticker=ticker, side="BUY", quantity=10,
        limit_price=mark * 2,
    )
    fills = await match_orders(conn, 999999)
    assert fills == 0
    assert await _order_status(conn, order.order_id) == "OPEN"


async def test_list_open_orders_scopes_by_season(conn: AsyncConnection) -> None:
    from stockbot.seasons.service import create_season

    await bootstrap_user(conn, 3007)
    ticker = await _first_ticker(conn)
    season_id = await create_season(
        conn, name="orders-test", start_tick=0, end_tick=10_000
    )
    await place_order(
        conn, user_id=3007, ticker=ticker, side="BUY", quantity=1,
        limit_price=Decimal("0.01"),
    )
    await place_order(
        conn, user_id=3007, ticker=ticker, side="BUY", quantity=1,
        limit_price=Decimal("0.02"), season_id=season_id,
    )
    main = await list_open_orders(conn, 3007)
    league = await list_open_orders(conn, 3007, season_id=season_id)
    assert len(main) == 1 and len(league) == 1
    assert league[0]["limit_price"] == Decimal("0.02")
