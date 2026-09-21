"""Trading sessions: closed ticks reject trading, flat candles, the
overnight gap, post-open liquidation/knockout sweeps, and calendar-day
accruals (borrow fees, season lifecycle) still running while closed."""

from __future__ import annotations

import math
from decimal import Decimal

import pytest
from psycopg import AsyncConnection

from stockbot.accounts.service import bootstrap_user
from stockbot.ledger.service import get_balance, get_system_account_id, post_transfer
from stockbot.market import engine
from stockbot.market.data import current_session
from stockbot.market.tick import apply_tick
from stockbot.orders.service import place_order
from stockbot.seasons.service import create_season
from stockbot.shorts.service import open_bounded_short
from stockbot.trading.errors import MarketClosedError
from stockbot.trading.service import execute_trade

SEED = "sessions-test-seed"


async def _set_session(conn: AsyncConnection, open_t: int, closed_t: int) -> None:
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE config SET value = %s WHERE key = 'session.open_ticks'", (open_t,)
        )
        await cur.execute(
            "UPDATE config SET value = %s WHERE key = 'session.closed_ticks'",
            (closed_t,),
        )


async def _last_tick(conn: AsyncConnection) -> int:
    async with conn.cursor() as cur:
        await cur.execute("SELECT MAX(tick_index) FROM market_ticks")
        row = await cur.fetchone()
    assert row is not None and row[0] is not None
    return int(row[0])


async def _run_to_phase(conn: AsyncConnection, want: str, max_ticks: int = 30) -> int:
    """Apply ticks until the last applied tick's session phase == `want`."""
    for _ in range(max_ticks):
        tick = await apply_tick(conn, SEED)
        phase, _, _ = await current_session(conn)
        if phase == want:
            return tick
    raise AssertionError(f"never reached phase {want}")


async def _first_ticker(conn: AsyncConnection) -> str:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT ticker FROM instruments WHERE is_active AND kind != 'INDEX' "
            "ORDER BY ticker LIMIT 1"
        )
        row = await cur.fetchone()
        assert row is not None
        return str(row[0])


async def _quoted(conn: AsyncConnection, ticker: str) -> float:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT quoted_price FROM instruments WHERE ticker = %s", (ticker,)
        )
        row = await cur.fetchone()
        assert row is not None
        return float(row[0])


async def test_closed_ticks_reject_all_trading(conn: AsyncConnection) -> None:
    await bootstrap_user(conn, 7001)
    ticker = await _first_ticker(conn)
    await _set_session(conn, 2, 2)
    await _run_to_phase(conn, "CLOSED")

    with pytest.raises(MarketClosedError):
        await execute_trade(conn, user_id=7001, ticker=ticker, side="BUY", quantity=1)
    with pytest.raises(MarketClosedError):
        await place_order(
            conn, user_id=7001, ticker=ticker, side="BUY", quantity=1,
            limit_price=Decimal("1"),
        )
    with pytest.raises(MarketClosedError):
        await open_bounded_short(conn, user_id=7001, ticker=ticker, quantity=1)


async def test_closed_ticks_write_flat_candles(conn: AsyncConnection) -> None:
    ticker = await _first_ticker(conn)
    await _set_session(conn, 2, 2)
    tick = await _run_to_phase(conn, "CLOSED")

    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT session_state FROM market_ticks WHERE tick_index = %s", (tick,)
        )
        (state,) = await cur.fetchone()
        assert state == "CLOSED"
        await cur.execute(
            """
            SELECT c.open, c.high, c.low, c.close, c.volume
            FROM candles c JOIN instruments i ON i.id = c.instrument_id
            WHERE i.ticker = %s AND c.tick_index = %s
            """,
            (ticker, tick),
        )
        row = await cur.fetchone()
    assert row is not None
    o, h, lo, c, vol = row
    assert o == h == lo == c and int(vol) == 0


async def test_open_gap_respects_circuit_breaker(conn: AsyncConnection) -> None:
    """The reopen step is one step with dt=closed_ticks -- big, but still
    bounded by the same circuit-breaker cap as any tick."""
    ticker = await _first_ticker(conn)
    await _set_session(conn, 2, 20)  # dt=20 reopen steps
    await _run_to_phase(conn, "CLOSED")
    pre_close = await _quoted(conn, ticker)
    tick = await _run_to_phase(conn, "OPEN")  # first open tick: the gap
    post_gap = await _quoted(conn, ticker)
    move = abs(math.log(post_gap / pre_close))
    assert move <= engine.CIRCUIT_BREAKER_CAP + 1e-9, f"gap {move} exceeded breaker"
    # and the tick was actually recorded as open
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT session_state FROM market_ticks WHERE tick_index = %s", (tick,)
        )
        assert (await cur.fetchone())[0] == "OPEN"


async def test_overnight_earnings_resolves_into_gap(conn: AsyncConnection) -> None:
    """An event due during the close doesn't resolve until the open tick --
    it lands inside the gap step."""
    ticker = await _first_ticker(conn)
    await _set_session(conn, 2, 4)
    await _run_to_phase(conn, "CLOSED")
    closed_tick = await _last_tick(conn)

    async with conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO events (instrument_id, kind, scheduled_tick, resolve_tick,
                                magnitude)
            SELECT id, 'EARNINGS', %s, %s, 0.05
            FROM instruments WHERE ticker = %s
            RETURNING id
            """,
            (closed_tick, closed_tick, ticker),
        )
        event_id = (await cur.fetchone())[0]
        await cur.execute(
            "SELECT fundamental_value FROM instruments WHERE ticker = %s", (ticker,)
        )
        before = float((await cur.fetchone())[0])

    # Still closed: another closed tick must not resolve it.
    await apply_tick(conn, SEED)
    async with conn.cursor() as cur:
        await cur.execute("SELECT resolved FROM events WHERE id = %s", (event_id,))
        assert (await cur.fetchone())[0] is False

    # The first open tick resolves it into the gap.
    await _run_to_phase(conn, "OPEN")
    async with conn.cursor() as cur:
        await cur.execute("SELECT resolved FROM events WHERE id = %s", (event_id,))
        assert (await cur.fetchone())[0] is True
        await cur.execute(
            "SELECT fundamental_value FROM instruments WHERE ticker = %s", (ticker,)
        )
        after = float((await cur.fetchone())[0])
    assert after != before


async def test_no_liquidation_sweep_while_closed(conn: AsyncConnection) -> None:
    """A gap that puts an account underwater sits untouched through the
    close; the sweep fires on the first open tick."""
    await bootstrap_user(conn, 7002)
    account_id = await _account(conn, 7002)
    faucet = await get_system_account_id(conn, "FAUCET")
    await post_transfer(
        conn, from_account_id=faucet, to_account_id=account_id,
        amount=2_000_000, reason="TEST_TOPUP",
    )
    ticker = await _first_ticker(conn)
    async with conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO entitlements (user_id, item_key, quantity) "
            "VALUES (7002, 'margin_tier', 1)"
        )
        await cur.execute(
            "UPDATE instruments SET init_margin_pct = 0.5, maint_margin_pct = 0.3 "
            "WHERE ticker = %s",
            (ticker,),
        )
    price = await _quoted(conn, ticker)
    cash = await get_balance(conn, account_id)
    qty = max(1, int(Decimal(cash) * Decimal("1.8") / (Decimal(str(price)) * 100)))
    await execute_trade(conn, user_id=7002, ticker=ticker, side="SELL", quantity=qty)

    await _set_session(conn, 2, 4)
    await _run_to_phase(conn, "CLOSED")
    # Gap the instrument up while closed -- nothing may act on it yet.
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE instruments SET base_price = base_price * 3, "
            "quoted_price = quoted_price * 3 WHERE ticker = %s",
            (ticker,),
        )
    await apply_tick(conn, SEED)  # still closed
    async with conn.cursor() as cur:
        await cur.execute("SELECT COUNT(*) FROM liquidations WHERE user_id = 7002")
        assert (await cur.fetchone())[0] == 0

    await _run_to_phase(conn, "OPEN")
    async with conn.cursor() as cur:
        await cur.execute("SELECT COUNT(*) FROM liquidations WHERE user_id = 7002")
        assert (await cur.fetchone())[0] >= 1


async def test_gap_through_knockout_settles_at_open(conn: AsyncConnection) -> None:
    """A bounded short whose instrument gaps through its knockout while
    closed is swept on the open tick with payout floored at 0."""
    await bootstrap_user(conn, 7003)
    ticker = await _first_ticker(conn)
    short = await open_bounded_short(conn, user_id=7003, ticker=ticker, quantity=1)

    await _set_session(conn, 2, 4)
    await _run_to_phase(conn, "CLOSED")
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE instruments SET base_price = base_price * 5, "
            "quoted_price = quoted_price * 5 WHERE ticker = %s",
            (ticker,),
        )
    await apply_tick(conn, SEED)  # closed: no KO sweep
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT status FROM bounded_shorts WHERE id = %s", (short.short_id,)
        )
        assert (await cur.fetchone())[0] == "OPEN"

    await _run_to_phase(conn, "OPEN")
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT status, payout_minor FROM bounded_shorts WHERE id = %s",
            (short.short_id,),
        )
        status, payout = await cur.fetchone()
    assert status == "KNOCKED_OUT" and int(payout) == 0


async def test_borrow_fees_accrue_across_closed_ticks(conn: AsyncConnection) -> None:
    """Borrow is a calendar-day charge -- it accrues on closed ticks too."""
    await bootstrap_user(conn, 7004)
    account_id = await _account(conn, 7004)
    faucet = await get_system_account_id(conn, "FAUCET")
    await post_transfer(
        conn, from_account_id=faucet, to_account_id=account_id,
        amount=5_000_000, reason="TEST_TOPUP",
    )
    async with conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO entitlements (user_id, item_key, quantity) "
            "VALUES (7004, 'margin_tier', 1)"
        )
    ticker = await _first_ticker(conn)
    await execute_trade(conn, user_id=7004, ticker=ticker, side="SELL", quantity=1)

    await _set_session(conn, 2, 4)
    await _run_to_phase(conn, "CLOSED")
    before = await _borrow_fees(conn, 7004)
    await apply_tick(conn, SEED)  # closed tick
    after = await _borrow_fees(conn, 7004)
    assert after > before


async def test_season_lifecycle_runs_while_closed(conn: AsyncConnection) -> None:
    """seasons.on_tick is invoked on closed ticks: a season whose start
    tick falls in the closed stretch still activates on schedule."""
    await _set_session(conn, 2, 4)
    await _run_to_phase(conn, "CLOSED")
    closed_tick = await _last_tick(conn)
    season_id = await create_season(
        conn,
        name="Closed-start season",
        start_tick=closed_tick,
        end_tick=closed_tick + 5000,
        entry_fee_minor=0,
        stake_minor=100_000,
    )
    await apply_tick(conn, SEED)  # still closed
    async with conn.cursor() as cur:
        await cur.execute("SELECT status FROM seasons WHERE id = %s", (season_id,))
        assert (await cur.fetchone())[0] == "ACTIVE"


async def _account(conn: AsyncConnection, user_id: int) -> int:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT id FROM accounts WHERE user_id = %s AND kind = 'USER'", (user_id,)
        )
        row = await cur.fetchone()
        assert row is not None
        return int(row[0])


async def _borrow_fees(conn: AsyncConnection, user_id: int) -> Decimal:
    async with conn.cursor() as cur:
        await cur.execute(
            """
            SELECT p.borrow_fees_accrued FROM positions p
            JOIN instruments i ON i.id = p.instrument_id
            WHERE p.user_id = %s AND p.quantity < 0 AND p.season_id IS NULL
            """,
            (user_id,),
        )
        row = await cur.fetchone()
        assert row is not None
        return Decimal(row[0])
