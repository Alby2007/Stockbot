"""Paid order types (0041): entitlement gates on iceberg/trailing/OCO,
the per-tick trailing ratchet, and one-cancels-other sibling semantics."""

from __future__ import annotations

from decimal import Decimal

import pytest
from psycopg import AsyncConnection

from stockbot.accounts.service import bootstrap_user
from stockbot.ledger.service import (
    get_system_account_id,
    get_user_account_id,
    post_transfer,
)
from stockbot.orders.service import (
    cancel_order,
    list_open_orders,
    match_orders,
    place_oco,
    place_order,
)
from stockbot.trading.errors import TradingError
from stockbot.trading.service import execute_trade


async def _fund(conn: AsyncConnection, user_id: int, amount: int = 10_000_000) -> None:
    await bootstrap_user(conn, user_id)
    await post_transfer(
        conn,
        from_account_id=await get_system_account_id(conn, "FAUCET"),
        to_account_id=await get_user_account_id(conn, user_id),
        amount=amount,
        reason="TEST_TOPUP",
    )


async def _unlock(conn: AsyncConnection, user_id: int, item_key: str) -> None:
    async with conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO entitlements (user_id, item_key) VALUES (%s, %s)",
            (user_id, item_key),
        )


async def _first_ticker(conn: AsyncConnection) -> str:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT ticker FROM instruments WHERE is_active AND kind = 'STOCK'"
            " ORDER BY liquidity DESC, id LIMIT 1"
        )
        (ticker,) = await cur.fetchone()
    return ticker


async def _quoted(conn: AsyncConnection, ticker: str) -> Decimal:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT quoted_price FROM instruments WHERE ticker = %s", (ticker,)
        )
        (price,) = await cur.fetchone()
    return Decimal(price)


async def _set_mark(conn: AsyncConnection, ticker: str, price: float) -> None:
    """Direct mark poke -- tests the ratchet/trigger against controlled
    prices without driving fills to move the quote."""
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE instruments SET quoted_price = %s WHERE ticker = %s",
            (price, ticker),
        )


async def _order_row(conn: AsyncConnection, order_id: int) -> dict:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT status, stop_price, triggered_tick, oco_group, trail_amount "
            "FROM orders WHERE id = %s",
            (order_id,),
        )
        r = await cur.fetchone()
    return {
        "status": r[0],
        "stop_price": Decimal(r[1]) if r[1] is not None else None,
        "triggered_tick": r[2],
        "oco_group": r[3],
        "trail_amount": Decimal(r[4]) if r[4] is not None else None,
    }


# --- Entitlement gates ------------------------------------------------------


async def test_trailing_requires_unlock(conn: AsyncConnection) -> None:
    await _fund(conn, 9101)
    ticker = await _first_ticker(conn)
    with pytest.raises(TradingError, match="order_trailing"):
        await place_order(
            conn, user_id=9101, ticker=ticker, side="SELL", quantity=1,
            limit_price=None, trail_amount=Decimal("1.00"),
        )


async def test_iceberg_requires_unlock(conn: AsyncConnection) -> None:
    await _fund(conn, 9102)
    ticker = await _first_ticker(conn)
    mark = await _quoted(conn, ticker)
    with pytest.raises(TradingError, match="order_iceberg"):
        await place_order(
            conn, user_id=9102, ticker=ticker, side="BUY", quantity=1,
            limit_price=mark, display_qty=1,
        )


async def test_oco_requires_unlock(conn: AsyncConnection) -> None:
    await _fund(conn, 9103)
    ticker = await _first_ticker(conn)
    mark = await _quoted(conn, ticker)
    with pytest.raises(TradingError, match="order_oco"):
        await place_oco(
            conn, user_id=9103, ticker=ticker, side="SELL", quantity=1,
            take_profit=mark * 2, stop_price=mark / 2,
        )


# --- Trailing stops ---------------------------------------------------------


async def test_trail_only_stop_anchors_off_mark_and_ratchets(
    conn: AsyncConnection,
) -> None:
    await _fund(conn, 9110)
    await _unlock(conn, 9110, "order_trailing")
    ticker = await _first_ticker(conn)
    mark = await _quoted(conn, ticker)

    order = await place_order(
        conn, user_id=9110, ticker=ticker, side="SELL", quantity=1,
        limit_price=None, trail_amount=Decimal("1.00"),
    )
    assert order.order_type == "STOP"
    initial = (await _order_row(conn, order.order_id))["stop_price"]
    assert initial is not None and initial < mark  # anchored mark - $1

    # Favorable move (mark up $10): the SELL trail ratchets up with it.
    new_mark = float(mark) + 10.0
    await _set_mark(conn, ticker, new_mark)
    await match_orders(conn, 999_900)
    ratcheted = (await _order_row(conn, order.order_id))["stop_price"]
    assert ratcheted > initial
    assert ratcheted <= Decimal(str(new_mark))
    assert ratcheted >= Decimal(str(new_mark - 1.0 - 1.0))  # within ~trail+grid

    # Adverse move that stays above the stop (mark dips half a dollar off
    # the high but doesn't reach it): the trail does NOT follow down.
    await _set_mark(conn, ticker, new_mark - 0.5)
    await match_orders(conn, 999_901)
    assert (await _order_row(conn, order.order_id))["stop_price"] == ratcheted


async def test_trailing_stop_still_triggers_on_fall(conn: AsyncConnection) -> None:
    await _fund(conn, 9111)
    await _unlock(conn, 9111, "order_trailing")
    ticker = await _first_ticker(conn)
    order = await place_order(
        conn, user_id=9111, ticker=ticker, side="SELL", quantity=1,
        limit_price=None, trail_amount=Decimal("1.00"),
    )
    stop = (await _order_row(conn, order.order_id))["stop_price"]
    # Drop the mark through the stop: the trail can't save it.
    await _set_mark(conn, ticker, float(stop) * 0.99)
    await match_orders(conn, 999_910)
    row = await _order_row(conn, order.order_id)
    assert row["triggered_tick"] == 999_910


async def test_trail_rejects_limit(conn: AsyncConnection) -> None:
    await _fund(conn, 9112)
    await _unlock(conn, 9112, "order_trailing")
    ticker = await _first_ticker(conn)
    mark = await _quoted(conn, ticker)
    with pytest.raises(ValueError, match="pure stop"):
        await place_order(
            conn, user_id=9112, ticker=ticker, side="SELL", quantity=1,
            limit_price=mark, trail_amount=Decimal("1.00"),
        )


# --- OCO brackets -----------------------------------------------------------


async def _sell_bracket(
    conn: AsyncConnection, user_id: int, ticker: str, qty: int,
    tp: Decimal, sl: Decimal,
):
    await _unlock(conn, user_id, "order_oco")
    return await place_oco(
        conn, user_id=user_id, ticker=ticker, side="SELL", quantity=qty,
        take_profit=tp, stop_price=sl,
    )


async def test_oco_fill_cancels_sibling(conn: AsyncConnection) -> None:
    await _fund(conn, 9120)
    ticker = await _first_ticker(conn)
    mark = await _quoted(conn, ticker)
    await execute_trade(conn, user_id=9120, ticker=ticker, side="BUY", quantity=10)
    mark = await _quoted(conn, ticker)

    tp_leg, sl_leg = await _sell_bracket(
        conn, 9120, ticker, 10, tp=mark * Decimal("0.99"), sl=mark / 2
    )
    tp_row = await _order_row(conn, tp_leg.order_id)
    sl_row = await _order_row(conn, sl_leg.order_id)
    assert tp_row["oco_group"] == tp_leg.order_id == sl_row["oco_group"]

    await match_orders(conn, 999_920)
    assert (await _order_row(conn, tp_leg.order_id))["status"] == "FILLED"
    assert (await _order_row(conn, sl_leg.order_id))["status"] == "CANCELLED"


async def test_oco_manual_cancel_cancels_sibling(conn: AsyncConnection) -> None:
    await _fund(conn, 9121)
    ticker = await _first_ticker(conn)
    mark = await _quoted(conn, ticker)
    # tp far above the mark so nothing fills before the cancel.
    tp_leg, sl_leg = await _sell_bracket(
        conn, 9121, ticker, 5, tp=mark * 2, sl=mark / 2
    )
    assert await cancel_order(conn, user_id=9121, order_id=sl_leg.order_id)
    assert (await _order_row(conn, tp_leg.order_id))["status"] == "CANCELLED"


async def test_oco_rejects_crossed_bracket(conn: AsyncConnection) -> None:
    await _fund(conn, 9122)
    await _unlock(conn, 9122, "order_oco")
    ticker = await _first_ticker(conn)
    mark = await _quoted(conn, ticker)
    with pytest.raises(ValueError, match="take_profit above stop_loss"):
        await place_oco(
            conn, user_id=9122, ticker=ticker, side="SELL", quantity=1,
            take_profit=mark / 2, stop_price=mark * 2,
        )


async def test_list_orders_shows_trail_and_oco(conn: AsyncConnection) -> None:
    await _fund(conn, 9123)
    ticker = await _first_ticker(conn)
    mark = await _quoted(conn, ticker)
    tp_leg, _sl = await _sell_bracket(
        conn, 9123, ticker, 1, tp=mark * 2, sl=mark / 2
    )
    await _unlock(conn, 9123, "order_trailing")
    trail_order = await place_order(
        conn, user_id=9123, ticker=ticker, side="SELL", quantity=1,
        limit_price=None, trail_amount=Decimal("1.00"),
    )
    rows = {r["id"]: r for r in await list_open_orders(conn, 9123)}
    assert rows[tp_leg.order_id]["oco_group"] == tp_leg.order_id
    assert rows[trail_order.order_id]["trail_amount"] == Decimal("1.00")
