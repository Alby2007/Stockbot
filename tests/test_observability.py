"""Observability: tick telemetry, heartbeats, periodic audit, kill
switches, and fill provenance."""

from __future__ import annotations

import pytest
from psycopg import AsyncConnection

from stockbot.accounts.service import bootstrap_user
from stockbot.market.tick import apply_tick
from stockbot.orders.service import place_order
from stockbot.shorts.service import cover_bounded_short, open_bounded_short
from stockbot.trading.errors import FeatureDisabledError
from stockbot.trading.service import execute_trade

SEED = "observability-test-seed"


async def _first_ticker(conn: AsyncConnection) -> str:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT ticker FROM instruments WHERE is_active AND kind != 'INDEX' "
            "ORDER BY ticker LIMIT 1"
        )
        row = await cur.fetchone()
        assert row is not None
        return str(row[0])


async def test_tick_writes_telemetry_and_heartbeat(conn: AsyncConnection) -> None:
    tick = await apply_tick(conn, SEED)
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT duration_ms, fills, crosses, stops_triggered, knockouts, "
            "       liquidations, events_resolved, session_state "
            "FROM market_ticks WHERE tick_index = %s",
            (tick,),
        )
        row = await cur.fetchone()
    assert row is not None
    duration_ms, fills, crosses, stops, kos, liqs, events, phase = row
    assert duration_ms is not None and float(duration_ms) >= 0
    assert fills == 0 and crosses == 0 and stops == 0
    assert kos == 0 and liqs == 0 and int(events) >= 0
    assert phase == "OPEN"

    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT detail->>'tick' FROM service_heartbeats WHERE service = 'market'"
        )
        hb = await cur.fetchone()
    assert hb is not None and int(hb[0]) == tick


async def test_periodic_audit_writes_results(conn: AsyncConnection) -> None:
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE config SET value = 2 WHERE key = 'audit.every_n_ticks'"
        )
    await apply_tick(conn, SEED)
    await apply_tick(conn, SEED)  # tick 2 % 2 == 0 -> audit runs

    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT check_name, ok FROM audit_results ORDER BY check_name"
        )
        rows = await cur.fetchall()
    names = {r[0] for r in rows}
    assert {
        "ledger_sum_zero",
        "no_negative_balances",
        "balance_matches_ledger",
        "fund_reconciles",
    } <= names
    assert all(r[1] for r in rows)


async def test_kill_switches_block_user_paths(conn: AsyncConnection) -> None:
    await bootstrap_user(conn, 8001)
    ticker = await _first_ticker(conn)
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE config SET value = 0 WHERE key IN "
            "('trading.enabled', 'orders.enabled', 'shorts.enabled')"
        )

    with pytest.raises(FeatureDisabledError):
        await execute_trade(conn, user_id=8001, ticker=ticker, side="BUY", quantity=1)
    with pytest.raises(FeatureDisabledError):
        await place_order(
            conn, user_id=8001, ticker=ticker, side="BUY", quantity=1,
            limit_price=None, stop_price=1,
        )
    with pytest.raises(FeatureDisabledError):
        await open_bounded_short(conn, user_id=8001, ticker=ticker, quantity=1)

    # match_orders silently no-ops while orders.enabled is off: re-enable
    # it long enough to rest a marketable order, disable, and confirm the
    # tick skips it
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE config SET value = 1 WHERE key = 'orders.enabled'"
        )
        await cur.execute(
            "SELECT quoted_price FROM instruments WHERE ticker = %s", (ticker,)
        )
        mark = float((await cur.fetchone())[0])
    await place_order(
        conn, user_id=8001, ticker=ticker, side="BUY", quantity=1,
        limit_price=mark * 2, stop_price=None,
    )
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE config SET value = 0 WHERE key = 'orders.enabled'"
        )
    await apply_tick(conn, SEED)
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT status FROM orders WHERE user_id = 8001", ()
        )
        assert (await cur.fetchone())[0] == "OPEN"


async def test_trading_halt_does_not_burn_fill_failures(
    conn: AsyncConnection,
) -> None:
    """FeatureDisabledError is a global transient, not an
    order-deterministic failure: with trading.enabled off, resting
    marketable orders just wait -- they must not accumulate strikes
    toward auto-cancel."""
    await bootstrap_user(conn, 8003)
    ticker = await _first_ticker(conn)
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT quoted_price FROM instruments WHERE ticker = %s", (ticker,)
        )
        mark = float((await cur.fetchone())[0])
    result = await place_order(
        conn, user_id=8003, ticker=ticker, side="BUY", quantity=1,
        limit_price=mark * 2, stop_price=None,
    )
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE config SET value = 0 WHERE key = 'trading.enabled'"
        )
    for _ in range(6):  # one more than order.max_fill_failures (5)
        await apply_tick(conn, SEED)
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT status, fill_failures FROM orders WHERE id = %s",
            (result.order_id,),
        )
        status, strikes = await cur.fetchone()
    assert status == "OPEN" and strikes == 0


async def test_cover_bounded_short_survives_shorts_kill_switch(
    conn: AsyncConnection,
) -> None:
    """Closes reduce risk, so a shorts halt must not trap users in open
    positions -- cover stays available while opens are gated."""
    await bootstrap_user(conn, 8004)
    ticker = await _first_ticker(conn)
    result = await open_bounded_short(conn, user_id=8004, ticker=ticker, quantity=1)
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE config SET value = 0 WHERE key = 'shorts.enabled'"
        )
    # Must NOT raise FeatureDisabledError.
    covered = await cover_bounded_short(conn, user_id=8004, short_id=result.short_id)
    assert covered is not None


async def test_fill_provenance_on_trade_row(conn: AsyncConnection) -> None:
    await bootstrap_user(conn, 8002)
    ticker = await _first_ticker(conn)
    await execute_trade(conn, user_id=8002, ticker=ticker, side="BUY", quantity=1)
    async with conn.cursor() as cur:
        await cur.execute(
            """
            SELECT t.half_spread, t.impact_delta
            FROM trades t JOIN instruments i ON i.id = t.instrument_id
            WHERE i.ticker = %s AND t.user_id = 8002
            """,
            (ticker,),
        )
        row = await cur.fetchone()
    assert row is not None
    half_spread, impact_delta = row
    assert half_spread is not None and float(half_spread) > 0
    assert impact_delta is not None and float(impact_delta) > 0  # buy-side impact
