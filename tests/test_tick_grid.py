"""Phase G: tick-size grid, U-shaped intraday spread, and bid/ask display.

Covers the 1-2-5 price grid (`tick_size`), tie-breaking (`round_to_tick`),
outward-rounded display quotes (`quote_ticks`), the open/close spread
terms in `half_spread_fraction`, placement-time price snapping in
`place_order`, and `InstrumentSnapshot.bid`/`ask`.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from psycopg import AsyncConnection

from stockbot.accounts.service import bootstrap_user
from stockbot.market import engine
from stockbot.market.data import all_instrument_snapshots
from stockbot.orders.service import place_order
from stockbot.trading.service import execute_trade

_CFG = {
    "spread.tick_pct": 0.001,
    "spread.tick_min": 0.01,
    "spread.base_bps": 5.0,
    "spread.sigma_coeff": 1.0,
    "spread.sigma_ref": 0.0003,
    "spread.inv_liquidity_coeff": 0.5,
    "spread.liquidity_ref": 3_000_000.0,
    "spread.halt_coeff": 3.0,
    "spread.halt_decay_ticks": 60.0,
    "spread.event_coeff": 1.0,
    "spread.event_window_ticks": 360.0,
    "spread.open_coeff": 1.0,
    "spread.open_decay_ticks": 90.0,
    "spread.close_coeff": 0.5,
    "spread.close_decay_ticks": 60.0,
}


def test_tick_size_is_a_125_grid() -> None:
    # raw = price * 0.001; tick = smallest 1/2/5 * 10^n >= raw.
    assert engine.tick_size(100.0, _CFG) == pytest.approx(0.1)
    assert engine.tick_size(10.0, _CFG) == pytest.approx(0.01)
    assert engine.tick_size(10.5, _CFG) == pytest.approx(0.02)
    assert engine.tick_size(30.0, _CFG) == pytest.approx(0.05)
    assert engine.tick_size(60.0, _CFG) == pytest.approx(0.1)
    # Floored at tick_min.
    assert engine.tick_size(1.0, _CFG) == pytest.approx(0.01)


def test_round_to_tick_ties_half_up() -> None:
    assert engine.round_to_tick(10.043, 0.01) == pytest.approx(10.04)
    assert engine.round_to_tick(10.045, 0.01) == pytest.approx(10.05)
    assert engine.round_to_tick(10.044, 0.01) == pytest.approx(10.04)


def test_quote_ticks_round_outward() -> None:
    mark, half, tick = 10.043, 0.01, 0.05
    bid, ask = engine.quote_ticks(mark, half, tick)
    # Bid rounds DOWN to the grid, ask rounds UP -- the display is the
    # worst case a market fill can land at, never better.
    assert bid <= mark * (1 - half) and bid == pytest.approx(9.90)
    assert ask >= mark * (1 + half) and ask == pytest.approx(10.15)
    assert (bid / tick).is_integer() and (ask / tick).is_integer()


def test_u_shape_widens_at_open_and_close() -> None:
    base = dict(
        cfg=_CFG,
        sigma=0.0003,
        liquidity=3_000_000.0,
        ticks_since_halt=None,
        ticks_to_event=None,
    )
    mid = engine.half_spread_fraction(
        **base, ticks_since_open=480.0, ticks_to_close=480.0
    )
    at_open = engine.half_spread_fraction(
        **base, ticks_since_open=0.0, ticks_to_close=960.0
    )
    at_close = engine.half_spread_fraction(
        **base, ticks_since_open=960.0, ticks_to_close=0.0
    )
    assert at_open > mid > 0
    assert at_close > mid
    # The open term dominates the close term (open_coeff > close_coeff).
    assert at_open > at_close


async def _first_ticker(conn: AsyncConnection) -> str:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT ticker FROM instruments WHERE is_active AND kind != 'INDEX'"
            " ORDER BY ticker LIMIT 1"
        )
        row = await cur.fetchone()
    assert row is not None
    return str(row[0])


async def test_place_order_snaps_to_grid(conn: AsyncConnection) -> None:
    await bootstrap_user(conn, 4101)
    ticker = await _first_ticker(conn)
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT quoted_price FROM instruments WHERE ticker = %s", (ticker,)
        )
        (mark,) = await cur.fetchone()
    off_grid = Decimal(mark) + Decimal("0.0013")
    order = await place_order(
        conn, user_id=4101, ticker=ticker, side="BUY", quantity=1,
        limit_price=off_grid,
    )
    tick = Decimal(str(engine.tick_size(float(mark), _CFG)))
    snapped = (off_grid / tick).to_integral_value() * tick
    assert order.limit_price == snapped


async def test_fill_prints_on_grid(conn: AsyncConnection) -> None:
    await bootstrap_user(conn, 4102)
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE accounts SET balance = 100000000 WHERE user_id = 4102"
        )
    ticker = await _first_ticker(conn)
    result = await execute_trade(
        conn, user_id=4102, ticker=ticker, side="BUY", quantity=1
    )
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT base_price FROM instruments WHERE ticker = %s", (ticker,)
        )
        (base,) = await cur.fetchone()
    tick = Decimal(str(engine.tick_size(float(base), _CFG)))
    # The fill price is a grid multiple (allowing float dust).
    ratio = float(result.fill_price) / float(tick)
    assert abs(ratio - round(ratio)) < 1e-6


async def test_snapshot_carries_bid_ask_around_mark(conn: AsyncConnection) -> None:
    snaps = await all_instrument_snapshots(conn)
    priced = [s for s in snaps if s.bid is not None and s.ask is not None]
    assert priced, "expected snapshots to carry bid/ask"
    for s in priced:
        mark = Decimal(s.quoted_price)
        assert s.bid < mark < s.ask, (s.ticker, s.bid, mark, s.ask)
