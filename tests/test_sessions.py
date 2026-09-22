"""Trading sessions: closed ticks reject trading, flat candles, the
overnight gap, post-open liquidation/knockout sweeps, and calendar-day
accruals (borrow fees, season lifecycle) still running while closed."""

from __future__ import annotations

import math
from decimal import Decimal

import numpy as np
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
from stockbot.trading.errors import EventHaltedError, MarketClosedError
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


async def _order_status(conn: AsyncConnection, order_id: int) -> str:
    async with conn.cursor() as cur:
        await cur.execute("SELECT status FROM orders WHERE id = %s", (order_id,))
        row = await cur.fetchone()
        assert row is not None
        return str(row[0])


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


async def test_closing_auction_clears_uniform_and_skips_mm(
    conn: AsyncConnection,
) -> None:
    """C1: on the last open tick, crossing books print once at the
    volume-maximizing uniform price and the MM fallback doesn't run --
    an MM-fillable resting order waits through the close and fills on
    the next open tick instead."""
    for uid in (7010, 7011, 7012):
        account = await bootstrap_user(conn, uid)
        await post_transfer(
            conn,
            from_account_id=await get_system_account_id(conn, "FAUCET"),
            to_account_id=account,
            amount=10_000_000,
            reason="TEST_TOPUP",
        )
    ticker = await _first_ticker(conn)
    await _set_session(conn, 4, 2)
    # Pin the auction window to the boundary tick so pass 2 still runs on
    # the other open ticks (the scenario asserts the MM fallback resumes).
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE config SET value = 1 WHERE key = 'session.auction_ticks'"
        )
    await _run_to_phase(conn, "OPEN")
    # Determinism: clear any pending-earnings halt flag the seed draws.
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE instruments SET next_halting_event_tick = NULL WHERE ticker = %s",
            (ticker,),
        )

    # Advance to ticks_until_close == 2: orders placed now meet the book
    # for the first time on the last open tick -- the auction.
    from stockbot.market.data import session_parts

    for _ in range(10):
        last = await _last_tick(conn)
        _, _, cfg = await current_session(conn)
        open_t, closed_t, offset = session_parts(cfg)
        if engine.ticks_until_close(last, open_t, closed_t, offset) == 2:
            break
        await apply_tick(conn, SEED)
    else:
        raise AssertionError("never reached ticks_until_close == 2")

    mark = Decimal(str(await _quoted(conn, ticker)))
    # Seller needs inventory; the buy also gives 7011 a position to close.
    await execute_trade(conn, user_id=7011, ticker=ticker, side="BUY", quantity=10)
    mark = Decimal(str(await _quoted(conn, ticker)))
    bid = await place_order(
        conn, user_id=7010, ticker=ticker, side="BUY", quantity=10,
        limit_price=mark * Decimal("1.05"),
    )
    ask = await place_order(
        conn, user_id=7011, ticker=ticker, side="SELL", quantity=10,
        limit_price=mark * Decimal("0.98"),
    )
    # An MM-fillable bid on a SECOND instrument -- a book with no
    # counterparty: the auction finds zero clearing volume for it, and
    # with pass 2 skipped it can't MM-fill on the auction tick either.
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT ticker FROM instruments WHERE is_active AND kind != 'INDEX' "
            "AND ticker <> %s ORDER BY ticker LIMIT 1",
            (ticker,),
        )
        ticker2 = str((await cur.fetchone())[0])
        await cur.execute(
            "UPDATE instruments SET next_halting_event_tick = NULL WHERE ticker = %s",
            (ticker2,),
        )
    mark2 = await _quoted(conn, ticker2)
    late_bid = await place_order(
        conn, user_id=7012, ticker=ticker2, side="BUY", quantity=5,
        limit_price=Decimal(str(mark2)) * Decimal("1.01"),
    )

    auction_tick = await apply_tick(conn, SEED)

    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT fill_price FROM trades WHERE order_id IN (%s, %s) ORDER BY order_id",
            (bid.order_id, ask.order_id),
        )
        fills = [Decimal(r[0]) for r in await cur.fetchall()]
        await cur.execute(
            "SELECT crosses FROM market_ticks WHERE tick_index = %s",
            (auction_tick,),
        )
        (crosses,) = await cur.fetchone()
        await cur.execute(
            "SELECT quoted_price FROM instruments WHERE ticker = %s", (ticker,)
        )
        quoted = Decimal(str((await cur.fetchone())[0]))

    assert len(fills) == 2
    # Uniform clearing: both legs of the auction print at one price, and
    # the mark IS that price afterwards.
    assert fills[0] == fills[1] == quoted
    assert int(crosses) >= 1
    assert await _order_status(conn, bid.order_id) == "FILLED"
    assert await _order_status(conn, ask.order_id) == "FILLED"
    # Pass 2 was skipped: the extra bid rests through the close.
    assert await _order_status(conn, late_bid.order_id) == "OPEN"

    # MM fallback resumes after the auction: the leftover bid fills on
    # the next open tick.
    await _run_to_phase(conn, "OPEN")
    assert await _order_status(conn, late_bid.order_id) == "FILLED"


async def test_closing_auction_window_spans_n_ticks(
    conn: AsyncConnection,
) -> None:
    """session.auction_ticks is a real window, not a flag: with N=2 the
    second-to-last open tick auctions too -- a lone MM-fillable bid stays
    OPEN because pass 2 is skipped inside the window."""
    from stockbot.market.data import session_parts

    account = await bootstrap_user(conn, 7030)
    await post_transfer(
        conn,
        from_account_id=await get_system_account_id(conn, "FAUCET"),
        to_account_id=account,
        amount=10_000_000,
        reason="TEST_TOPUP",
    )
    ticker = await _first_ticker(conn)
    await _set_session(conn, 4, 2)
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE config SET value = 2 WHERE key = 'session.auction_ticks'"
        )
        await cur.execute(
            "UPDATE instruments SET next_halting_event_tick = NULL "
            "WHERE ticker = %s",
            (ticker,),
        )
    await _run_to_phase(conn, "OPEN")

    # Advance so the NEXT applied tick has ticks_until_close == 2 --
    # inside the window but not the final open tick.
    for _ in range(10):
        last = await _last_tick(conn)
        _, _, cfg = await current_session(conn)
        open_t, closed_t, offset = session_parts(cfg)
        if engine.ticks_until_close(last, open_t, closed_t, offset) == 3:
            break
        await apply_tick(conn, SEED)
    else:
        raise AssertionError("never reached ticks_until_close == 3")

    mark = Decimal(str(await _quoted(conn, ticker)))
    bid = await place_order(
        conn, user_id=7030, ticker=ticker, side="BUY", quantity=1,
        limit_price=mark * Decimal("1.01"),
    )
    await apply_tick(conn, SEED)  # until_close == 2: auction, no pass 2
    # A lone bid has no clearing volume and the MM fallback is skipped --
    # under the old flag-semantics this tick would have MM-filled it.
    # (Resumption of pass 2 outside the window is covered by C1 above.)
    assert await _order_status(conn, bid.order_id) == "OPEN"


async def test_event_halt_blocks_exposure_but_not_closing(
    conn: AsyncConnection,
) -> None:
    """C2: inside event.halt_lead_ticks of an earnings resolution, new
    exposure (trade, order, bounded short) is rejected, resting orders
    freeze without burning fill_failures, and closing stays legal."""
    from stockbot.orders.service import match_orders
    from stockbot.shorts.service import open_bounded_short

    account = await bootstrap_user(conn, 7020)
    await post_transfer(
        conn,
        from_account_id=await get_system_account_id(conn, "FAUCET"),
        to_account_id=account,
        amount=10_000_000,
        reason="TEST_TOPUP",
    )
    account2 = await bootstrap_user(conn, 7021)
    await post_transfer(
        conn,
        from_account_id=await get_system_account_id(conn, "FAUCET"),
        to_account_id=account2,
        amount=10_000_000,
        reason="TEST_TOPUP",
    )
    ticker = await _first_ticker(conn)
    await apply_tick(conn, SEED)
    current = await _last_tick(conn)
    # Isolate the event stream before the buys: a seed-scheduled earnings
    # inside the lead window would halt the name early.
    async with conn.cursor() as cur:
        await cur.execute(
            """
            DELETE FROM events WHERE kind = 'EARNINGS' AND NOT resolved
              AND instrument_id = (SELECT id FROM instruments WHERE ticker = %s)
            """,
            (ticker,),
        )
        await cur.execute(
            "UPDATE instruments SET next_halting_event_tick = NULL WHERE ticker = %s",
            (ticker,),
        )

    # Park a crossing pair on the name before the halt. Both legs are
    # exposure-INCREASING for their owners (the ask sits above the mark:
    # selling into it opens a short for the holder of 10 only after the
    # cross; as an MM candidate it's above the mark so pass 2 won't touch
    # it, and the bid's MM path is what the halt blocks).
    await execute_trade(conn, user_id=7020, ticker=ticker, side="BUY", quantity=10)
    mark = Decimal(str(await _quoted(conn, ticker)))
    ask = await place_order(
        conn, user_id=7020, ticker=ticker, side="SELL", quantity=15,
        limit_price=mark * Decimal("1.05"),
    )
    bid = await place_order(
        conn, user_id=7021, ticker=ticker, side="BUY", quantity=5,
        limit_price=mark * Decimal("1.05"),
    )

    # Enter the halt window: earnings resolves 2 ticks out, lead is 10.
    resolve_tick = current + 2
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE instruments SET next_halting_event_tick = %s WHERE ticker = %s",
            (resolve_tick, ticker),
        )

    with pytest.raises(EventHaltedError):
        await execute_trade(conn, user_id=7021, ticker=ticker, side="BUY", quantity=1)
    with pytest.raises(EventHaltedError):
        await place_order(
            conn, user_id=7021, ticker=ticker, side="BUY", quantity=1,
            limit_price=mark,
        )
    with pytest.raises(EventHaltedError):
        await open_bounded_short(conn, user_id=7021, ticker=ticker, quantity=1)

    # The book freezes: no cross, no MM fill, and no strikes recorded.
    fills = await match_orders(conn, current)
    assert fills == 0
    assert await _order_status(conn, bid.order_id) == "OPEN"
    assert await _order_status(conn, ask.order_id) == "OPEN"
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT fill_failures FROM orders WHERE id IN (%s, %s)",
            (bid.order_id, ask.order_id),
        )
        assert all(r[0] == 0 for r in await cur.fetchall())

    # Risk-reducing stays legal: 7020 holds 10 shares and can sell 5.
    result = await execute_trade(
        conn, user_id=7020, ticker=ticker, side="SELL", quantity=5
    )
    assert result.quantity == 5


async def test_event_halt_lifts_after_resolution(conn: AsyncConnection) -> None:
    """The gate reopens on its own: once the event resolves the aggregate
    rolls to the next earnings ~30d out and the halt window ends."""
    from stockbot.market import events as events_mod

    await bootstrap_user(conn, 7022)
    ticker = await _first_ticker(conn)
    await apply_tick(conn, SEED)
    current = await _last_tick(conn)
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT id FROM instruments WHERE ticker = %s", (ticker,)
        )
        (instrument_id,) = await cur.fetchone()
        # Isolate the event stream: the seed may already have scheduled an
        # earnings for this name inside the halt window.
        await cur.execute(
            "DELETE FROM events WHERE instrument_id = %s AND kind = 'EARNINGS'",
            (instrument_id,),
        )
        await cur.execute(
            """
            INSERT INTO events (instrument_id, kind, scheduled_tick, resolve_tick,
                                estimate)
            VALUES (%s, 'EARNINGS', %s, %s, 0.01)
            """,
            (instrument_id, current, current + 1),
        )
    await events_mod.refresh_next_event_ticks(conn)

    # Inside the window now (resolve is next tick, lead default 10).
    with pytest.raises(EventHaltedError):
        await execute_trade(conn, user_id=7022, ticker=ticker, side="BUY", quantity=1)

    await apply_tick(conn, SEED)  # resolves the event + reschedules
    # Gate lifted: the next earnings is ~30 days out.
    result = await execute_trade(
        conn, user_id=7022, ticker=ticker, side="BUY", quantity=1
    )
    assert result.quantity == 1
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT next_halting_event_tick FROM instruments WHERE id = %s",
            (instrument_id,),
        )
        (next_halt,) = await cur.fetchone()
    assert int(next_halt) - (current + 1) > 1000  # ~30d out, not imminently halted


async def test_auction_tick_keeps_event_halt_risk_reducing_path(
    conn: AsyncConnection,
) -> None:
    """Auction + T1-halt interaction: on the auction tick the MM fallback
    still runs for books that had no auction -- an event-halted name's
    resting orders keep the same position-aware gate as every other tick
    (a holder's resting SELL fills; an exposure-increasing BUY waits)."""
    from stockbot.market import events as events_mod
    from stockbot.market.data import session_parts

    account = await bootstrap_user(conn, 7040)
    await post_transfer(
        conn,
        from_account_id=await get_system_account_id(conn, "FAUCET"),
        to_account_id=account,
        amount=10_000_000,
        reason="TEST_TOPUP",
    )
    ticker = await _first_ticker(conn)
    await _set_session(conn, 4, 2)
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE config SET value = 1 WHERE key = 'session.auction_ticks'"
        )
    await _run_to_phase(conn, "OPEN")

    # A position to reduce, and a clean event stream so the seed can't
    # schedule an earnings inside the halt window first.
    await execute_trade(conn, user_id=7040, ticker=ticker, side="BUY", quantity=10)
    async with conn.cursor() as cur:
        await cur.execute(
            """
            DELETE FROM events WHERE kind = 'EARNINGS' AND NOT resolved
              AND instrument_id = (SELECT id FROM instruments WHERE ticker = %s)
            """,
            (ticker,),
        )
        await cur.execute(
            "SELECT id FROM instruments WHERE ticker = %s", (ticker,)
        )
        iid = int((await cur.fetchone())[0])

    # Advance until the NEXT applied tick is the auction (until_close == 1).
    for _ in range(10):
        last = await _last_tick(conn)
        _, _, cfg = await current_session(conn)
        open_t, closed_t, offset = session_parts(cfg)
        if engine.ticks_until_close(last, open_t, closed_t, offset) == 2:
            break
        await apply_tick(conn, SEED)
    else:
        raise AssertionError("never reached ticks_until_close == 2")
    current = await _last_tick(conn)

    # Resting orders placed BEFORE the halt: an MM-fillable sell against
    # the held long (risk-reducing) and an MM-fillable buy (increasing).
    mark = Decimal(str(await _quoted(conn, ticker)))
    sell = await place_order(
        conn, user_id=7040, ticker=ticker, side="SELL", quantity=5,
        limit_price=mark * Decimal("0.95"),
    )
    buy = await place_order(
        conn, user_id=7040, ticker=ticker, side="BUY", quantity=5,
        limit_price=mark * Decimal("1.05"),
    )
    # Earnings resolves one tick past the auction: the halt window covers
    # the auction tick and refresh_next_event_ticks keeps it that way.
    async with conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO events (instrument_id, kind, scheduled_tick, resolve_tick,
                                estimate)
            VALUES (%s, 'EARNINGS', %s, %s, 0.01)
            """,
            (iid, current, current + 2),
        )
    await events_mod.refresh_next_event_ticks(conn)

    await apply_tick(conn, SEED)  # the auction tick, inside the halt window

    # The holder's resting sell MM-filled mid-halt (same as any other
    # tick); the exposure-increasing buy waited with no strike.
    assert await _order_status(conn, sell.order_id) == "FILLED"
    assert await _order_status(conn, buy.order_id) == "OPEN"
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT fill_failures FROM orders WHERE id = %s", (buy.order_id,)
        )
        assert (await cur.fetchone())[0] == 0


async def test_first_tick_does_not_gap(conn: AsyncConnection) -> None:
    """Tick 0 must not 'reopen' with dt=closed_ticks -- the market never
    closed. With a large drift and all vol terms zeroed, the first tick's
    move is ~drift*1, not ~drift*closed_ticks. Drift must sit under the
    breaker cap (0.03) so the correct move doesn't itself halt."""
    await _set_session(conn, 1, 10)
    ticker = await _first_ticker(conn)
    async with conn.cursor() as cur:
        await cur.execute(
            """
            UPDATE instruments SET drift = 0.02, sigma = 0, fundamental_sigma = 0,
                fundamental_value = base_price, beta = 0, gamma = 0, kappa = 0
            WHERE ticker = %s
            RETURNING base_price
            """,
            (ticker,),
        )
        seed_base = float((await cur.fetchone())[0])

    tick = await apply_tick(conn, SEED)
    assert tick == 0

    quoted = await _quoted(conn, ticker)
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT circuit_halted_until_tick FROM instruments WHERE ticker = %s",
            (ticker,),
        )
        assert (await cur.fetchone())[0] is None
    move = math.log(quoted / seed_base)
    # dt=1 -> ~0.02; the buggy gap (dt=10) -> ~0.2, clipped + halted.
    assert 0.015 < move < 0.03


async def test_overnight_gap_scales_variance_not_clock(
    conn: AsyncConnection,
) -> None:
    """0033: the reopen's stochastic terms scale by sqrt(closed*frac), not
    sqrt(closed). open=2/closed=40, frac=0.125 -> clock dt=40, var_dt=5.
    Replicates the tick's RNG stream to pin the exact formula."""
    ticker = await _first_ticker(conn)
    await _set_session(conn, 2, 40)
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE config SET value = %s "
            "WHERE key = 'session.overnight_var_frac'",
            (0.125,),
        )

    # Ticks 0-1 open, 2-41 closed, 42 reopens. Stop one tick before it.
    for _ in range(42):
        await apply_tick(conn, SEED)
    assert await _last_tick(conn) == 41

    # The draw stream is seed-deterministic, so replicate it before the
    # tick runs and choose sigma so the expected move lands well clear of
    # both 0 and the breaker cap.
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT i.id FROM instruments i WHERE i.ticker = %s", (ticker,)
        )
        iid = int((await cur.fetchone())[0])
        await cur.execute(
            "SELECT id FROM instruments WHERE is_active AND kind != 'INDEX' "
            "ORDER BY id"
        )
        draw_order = [int(r[0]) for r in await cur.fetchall()]
        await cur.execute(
            "SELECT DISTINCT s.key FROM instruments i "
            "JOIN sectors s ON s.id = i.sector_id WHERE i.is_active"
        )
        n_sectors = len(await cur.fetchall())

    rng = np.random.default_rng(engine.tick_seed(SEED, 42))
    rng.standard_normal()  # market factor
    rng.standard_normal(n_sectors)  # sector factors
    for _ in range(draw_order.index(iid)):
        rng.standard_normal()  # drift_innov
        rng.standard_t(engine.STUDENT_T_DF)  # idiosyncratic
        rng.standard_normal()  # fundamental shock
    z_mom = rng.standard_normal()  # this instrument's drift_innov
    z = rng.standard_t(engine.STUDENT_T_DF) * engine.STUDENT_T_SCALE

    sigma = min(0.01, 0.02 / (math.sqrt(5.0) * max(abs(z), 1e-9)))
    async with conn.cursor() as cur:
        await cur.execute(
            """
            UPDATE instruments
            SET sigma = %s, sigma_eff = %s, beta = 0, gamma = 0,
                drift = 0.0001, drift_state = 0, impact = 0, kappa = 0,
                fundamental_value = base_price, dividend_drift_offset = 0,
                circuit_halted_until_tick = NULL
            WHERE id = %s
            """,
            (sigma, sigma, iid),
        )
        # An event resolving on the reopen tick would pollute model_ret.
        await cur.execute(
            "DELETE FROM events WHERE instrument_id = %s AND NOT resolved",
            (iid,),
        )

    tick = await apply_tick(conn, SEED)
    assert tick == 42

    clock_dt, var_dt = 40.0, 5.0
    async with conn.cursor() as cur:
        await cur.execute("SELECT key, value FROM config WHERE key LIKE 'mom.%'")
        mom_cfg = {k: float(v) for k, v in await cur.fetchall()}
    # Momentum regime is live (mom.innov_frac>0): the gap tick's
    # drift_state innovation scales by sqrt(var_dt) and the carried term
    # multiplies var_dt -- both part of what this test pins. Prior
    # drift_state was pinned to 0 above, so rho*state contributes nothing.
    mom_innov = mom_cfg.get("mom.innov_frac", 0.0)
    mom_max = mom_cfg.get("mom.max_frac", 0.0)
    drift_state = float(
        np.clip(
            mom_innov * sigma * math.sqrt(var_dt) * z_mom,
            -mom_max * sigma,
            mom_max * sigma,
        )
    )
    expected = (
        0.0001 * clock_dt
        + drift_state * var_dt
        + sigma * math.sqrt(var_dt) * z
    )
    assert abs(expected) < engine.CIRCUIT_BREAKER_CAP

    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT model_ret, halt_kind FROM candles "
            "WHERE instrument_id = %s AND tick_index = %s",
            (iid, tick),
        )
        model_ret, halt_kind = await cur.fetchone()
    assert math.isclose(float(model_ret), expected, abs_tol=1e-7)
    assert halt_kind is None
    # The pre-0033 formula multiplies the stochastic term by sqrt(40/5):
    old_style = sigma * math.sqrt(clock_dt) * z
    assert abs(old_style) > 2 * abs(sigma * math.sqrt(var_dt) * z)
