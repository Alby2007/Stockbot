"""Phase D: stop orders (trigger, cascade bound, stop-limit conversion,
short covers) and dividends (ex-date drop, long payout, short accrual,
drift-offset funding, ledger invariant)."""

from __future__ import annotations

from decimal import Decimal

from psycopg import AsyncConnection
from psycopg.rows import dict_row

from stockbot.accounts.service import bootstrap_user
from stockbot.ledger.service import (
    get_balance,
    get_system_account_id,
    get_user_account_id,
    post_transfer,
)
from stockbot.market.tick import apply_tick
from stockbot.orders.service import match_orders, place_order
from stockbot.trading.service import execute_trade


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


async def _liquid_ticker(conn: AsyncConnection) -> str:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT ticker FROM instruments "
            "WHERE is_active AND kind != 'INDEX' "
            "ORDER BY liquidity DESC, ticker LIMIT 1"
        )
        row = await cur.fetchone()
        assert row is not None
        return str(row[0])


async def _illiquid_ticker(conn: AsyncConnection) -> str:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT ticker FROM instruments "
            "WHERE is_active AND kind != 'INDEX' "
            "ORDER BY liquidity ASC, ticker LIMIT 1"
        )
        row = await cur.fetchone()
        assert row is not None
        return str(row[0])


async def _quoted(conn: AsyncConnection, ticker: str) -> Decimal:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT quoted_price FROM instruments WHERE ticker = %s", (ticker,)
        )
        row = await cur.fetchone()
        assert row is not None
        return Decimal(row[0])


async def _set_quoted(conn: AsyncConnection, ticker: str, factor: float) -> Decimal:
    """Force the mark for trigger testing: scales quoted AND base so the
    impact term stays coherent (fill math uses base*exp(impact))."""
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE instruments SET quoted_price = quoted_price * %s, "
            "base_price = base_price * %s WHERE ticker = %s "
            "RETURNING quoted_price",
            (factor, factor, ticker),
        )
        row = await cur.fetchone()
        assert row is not None
        return Decimal(row[0])


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
            "SELECT status, filled_quantity, order_type, triggered_tick, fill_price "
            "FROM orders WHERE id = %s",
            (order_id,),
        )
        row = await cur.fetchone()
        assert row is not None
        return dict(row)


# ---------------------------------------------------------------- stops


async def test_sell_stop_triggers_and_fills(conn: AsyncConnection) -> None:
    """A SELL stop at/above the mark triggers on the next matching pass
    and sells at market."""
    await bootstrap_user(conn, 6001)
    await _fund(conn, 6001, 10_000_000)
    ticker = await _liquid_ticker(conn)
    await execute_trade(conn, user_id=6001, ticker=ticker, side="BUY", quantity=10)
    mark = await _quoted(conn, ticker)

    order = await place_order(
        conn,
        user_id=6001,
        ticker=ticker,
        side="SELL",
        quantity=10,
        limit_price=None,
        stop_price=(mark * Decimal("1.5")).quantize(Decimal("0.000001")),
    )
    fills = await match_orders(conn, 1)

    assert fills >= 1
    row = await _order_row(conn, order.order_id)
    assert row["status"] == "FILLED"
    assert row["triggered_tick"] == 1
    assert await _position_qty(conn, 6001, ticker) == 0


async def test_untriggered_stop_rest_quietly(conn: AsyncConnection) -> None:
    """A SELL stop below the mark doesn't match -- it's not a live order
    until the mark falls through it."""
    await bootstrap_user(conn, 6002)
    await _fund(conn, 6002, 10_000_000)
    ticker = await _liquid_ticker(conn)
    await execute_trade(conn, user_id=6002, ticker=ticker, side="BUY", quantity=10)
    mark = await _quoted(conn, ticker)

    order = await place_order(
        conn,
        user_id=6002,
        ticker=ticker,
        side="SELL",
        quantity=10,
        limit_price=None,
        stop_price=(mark * Decimal("0.9")).quantize(Decimal("0.000001")),
    )
    fills = await match_orders(conn, 1)

    assert fills == 0
    row = await _order_row(conn, order.order_id)
    assert row["status"] == "OPEN"
    assert row["triggered_tick"] is None
    assert await _position_qty(conn, 6002, ticker) == 10

    # Now the mark falls through the stop: trigger + marketable fill.
    await _set_quoted(conn, ticker, 0.85)
    fills = await match_orders(conn, 2)
    assert fills >= 1
    row = await _order_row(conn, order.order_id)
    assert row["status"] == "FILLED"
    assert row["triggered_tick"] == 2


async def test_buy_stop_covers_a_short(conn: AsyncConnection) -> None:
    """Stops work for shorts too: a BUY stop above the mark fires on an
    uptick and covers."""
    await bootstrap_user(conn, 6003)
    await _grant_tier(conn, 6003, tier=3)
    await _fund(conn, 6003, 50_000_000)
    ticker = await _liquid_ticker(conn)
    mark = await _quoted(conn, ticker)
    # Short sized well under margin + depth limits.
    await execute_trade(conn, user_id=6003, ticker=ticker, side="SELL", quantity=5)
    assert await _position_qty(conn, 6003, ticker) == -5

    order = await place_order(
        conn,
        user_id=6003,
        ticker=ticker,
        side="BUY",
        quantity=5,
        limit_price=None,
        stop_price=(mark * Decimal("1.05")).quantize(Decimal("0.000001")),
    )
    assert (await _order_row(conn, order.order_id))["triggered_tick"] is None

    await _set_quoted(conn, ticker, 1.10)
    fills = await match_orders(conn, 3)
    assert fills >= 1
    row = await _order_row(conn, order.order_id)
    assert row["status"] == "FILLED"
    assert await _position_qty(conn, 6003, ticker) == 0


async def test_stop_limit_converts_to_limit_order(conn: AsyncConnection) -> None:
    """A STOP_LIMIT triggers into a limit order: it must still respect its
    limit after triggering -- a gap straight through leaves it open."""
    await bootstrap_user(conn, 6004)
    await _fund(conn, 6004, 10_000_000)
    ticker = await _liquid_ticker(conn)
    await execute_trade(conn, user_id=6004, ticker=ticker, side="BUY", quantity=10)
    mark = await _quoted(conn, ticker)

    order = await place_order(
        conn,
        user_id=6004,
        ticker=ticker,
        side="SELL",
        quantity=10,
        limit_price=(mark * Decimal("0.93")).quantize(Decimal("0.000001")),
        stop_price=(mark * Decimal("0.97")).quantize(Decimal("0.000001")),
    )
    # Gap to 0.90*mark: stop (0.97) triggers but the mark gapped straight
    # through the limit (0.93) -- triggered, unfilled.
    await _set_quoted(conn, ticker, 0.90)
    await match_orders(conn, 4)
    row = await _order_row(conn, order.order_id)
    assert row["status"] == "OPEN"
    assert row["triggered_tick"] == 4

    # Mark recovers above the limit: the now-limit order fills.
    await _set_quoted(conn, ticker, 1.10)
    await match_orders(conn, 5)
    row = await _order_row(conn, order.order_id)
    assert row["status"] == "FILLED"
    assert Decimal(row["fill_price"]) >= mark * Decimal("0.93")


async def test_stop_cascade_is_bounded(conn: AsyncConnection) -> None:
    """A stop fill's mark move triggers the next stop -- the cascade loop
    catches it, bounded by order.stop_cascade_max_iters."""
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE config SET value = 1 WHERE key = 'order.stop_cascade_max_iters'"
        )
        await cur.execute(
            "SELECT liquidity, lambda_impact FROM instruments "
            "WHERE is_active AND kind != 'INDEX' "
            "ORDER BY liquidity ASC, ticker LIMIT 1"
        )
        liquidity, lambda_impact = await cur.fetchone()
    await bootstrap_user(conn, 6005)
    await _grant_tier(conn, 6005, tier=3)
    await _fund(conn, 6005, 100_000_000)
    ticker = await _illiquid_ticker(conn)
    mark = await _quoted(conn, ticker)
    # A sells the full participation cap (~10% of liquidity): the biggest
    # single-fill mark move the engine allows, ~lambda*sqrt(0.1). B's stop
    # sits just under, so only a post-A mark crosses it.
    qty_a = max(1, int(Decimal(str(liquidity)) * Decimal("0.10") / mark))
    stop_a = await place_order(
        conn,
        user_id=6005,
        ticker=ticker,
        side="SELL",
        quantity=qty_a,
        limit_price=None,
        stop_price=(mark * Decimal("1.5")).quantize(Decimal("0.000001")),
    )
    # B's stop sits halfway through the mark move A's fill should cause
    # (lambda*sqrt(participation)), so it only triggers after A prints.
    expected_drop = float(lambda_impact) * (0.10**0.5)
    stop_b = await place_order(
        conn,
        user_id=6005,
        ticker=ticker,
        side="SELL",
        quantity=10,
        limit_price=None,
        stop_price=(mark * Decimal(str(1 - expected_drop * 0.5))).quantize(
            Decimal("0.000001")
        ),
    )

    await match_orders(conn, 6)
    row_a = await _order_row(conn, stop_a.order_id)
    row_b = await _order_row(conn, stop_b.order_id)
    assert row_a["status"] == "FILLED"
    assert row_a["triggered_tick"] == 6
    # The cascade cap of 1 allowed one match pass: A's fill dropped the
    # mark through B's stop, so B triggered but didn't get a second pass.
    assert row_b["status"] == "OPEN"
    assert row_b["triggered_tick"] == 6

    # Next tick's match finishes the job: B is already triggered, so it
    # fills as a marketable order.
    await match_orders(conn, 7)
    row_b = await _order_row(conn, stop_b.order_id)
    assert row_b["status"] == "FILLED"


# ------------------------------------------------------------ dividends


async def _insert_dividend(
    conn: AsyncConnection, ticker: str, div_per_share: float, resolve_tick: int = 0
) -> None:
    async with conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO events (instrument_id, kind, scheduled_tick, resolve_tick,
                                dividend_per_share)
            SELECT id, 'DIVIDEND', 0, %s, %s
            FROM instruments WHERE ticker = %s
            """,
            (resolve_tick, div_per_share, ticker),
        )


async def _dividends_accrued(conn: AsyncConnection, user_id: int, ticker: str) -> Decimal:
    async with conn.cursor() as cur:
        await cur.execute(
            """
            SELECT p.dividends_accrued FROM positions p
            JOIN instruments i ON i.id = p.instrument_id
            WHERE p.user_id = %s AND i.ticker = %s AND p.season_id IS NULL
            """,
            (user_id, ticker),
        )
        row = await cur.fetchone()
        return Decimal(row[0]) if row else Decimal(0)


async def _ledger_sum(conn: AsyncConnection) -> Decimal:
    async with conn.cursor() as cur:
        await cur.execute("SELECT COALESCE(SUM(amount), 0) FROM ledger_entries")
        row = await cur.fetchone()
        assert row is not None
        return Decimal(row[0])


async def test_dividend_pays_longs_drops_price_and_reschedules(
    conn: AsyncConnection,
) -> None:
    """Ex-date: the mark drops by ~the dividend, longs get cash from
    MARKET_MAKER with a DIVIDEND reason, and the next ex-date lands on the
    calendar. The ledger stays sum-zero throughout."""
    await bootstrap_user(conn, 6101)
    await _fund(conn, 6101, 10_000_000)
    ticker = await _liquid_ticker(conn)
    await execute_trade(conn, user_id=6101, ticker=ticker, side="BUY", quantity=20)
    mark = await _quoted(conn, ticker)
    div = float(mark) * 0.05  # 5% dividend: drop dominates per-tick noise
    await _insert_dividend(conn, ticker, div)

    account_id = await get_user_account_id(conn, 6101)
    cash_before = await get_balance(conn, account_id)
    await apply_tick(conn, "div-test-seed")

    cash_after = await get_balance(conn, account_id)
    expected_minor = int((Decimal(20) * Decimal(str(div)) * 100).quantize(Decimal("1")))
    assert cash_after - cash_before == expected_minor

    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT COUNT(*) FROM ledger_entries WHERE reason = 'DIVIDEND' "
            "AND account_id = %s AND amount > 0",
            (account_id,),
        )
        (count,) = await cur.fetchone()
        assert count == 1
        # The next ex-date is on the calendar already.
        await cur.execute(
            """
            SELECT COUNT(*) FROM events e JOIN instruments i ON i.id = e.instrument_id
            WHERE i.ticker = %s AND e.kind = 'DIVIDEND' AND NOT e.resolved
            """,
            (ticker,),
        )
        (pending,) = await cur.fetchone()
        assert pending >= 1

    # Ex-date drop: quoted is ~div lower than it would have been. The step
    # adds ordinary noise, so allow generous bounds -- but well under 2x:
    # a pre-bleed drift offset would double-suppress the drop (the old
    # dividend double-drag bug, a per-cycle short arb).
    new_mark = await _quoted(conn, ticker)
    drop = mark - new_mark
    assert Decimal(str(div)) * Decimal("0.4") < drop < Decimal(str(div)) * Decimal("1.5")

    assert await _ledger_sum(conn) == 0


async def test_dividend_accrues_on_short_then_settles_on_cover(
    conn: AsyncConnection,
) -> None:
    """Shorts owe the dividend -- accrued on the position (cash can't go
    negative), settled to MARKET_MAKER when the short covers."""
    await bootstrap_user(conn, 6102)
    await _grant_tier(conn, 6102, tier=3)
    await _fund(conn, 6102, 50_000_000)
    ticker = await _liquid_ticker(conn)
    mark = await _quoted(conn, ticker)
    await execute_trade(conn, user_id=6102, ticker=ticker, side="SELL", quantity=5)
    assert await _position_qty(conn, 6102, ticker) == -5

    div = float(mark) * 0.05
    await _insert_dividend(conn, ticker, div)
    account_id = await get_user_account_id(conn, 6102)
    cash_before = await get_balance(conn, account_id)
    await apply_tick(conn, "div-short-seed")

    # No cash moved: the obligation accrued on the position instead.
    assert await get_balance(conn, account_id) == cash_before
    accrued = await _dividends_accrued(conn, 6102, ticker)
    expected = Decimal(5) * Decimal(str(div)) * 100
    assert abs(accrued - expected) < Decimal("0.5")

    # Covering settles the accrual to MARKET_MAKER.
    await execute_trade(conn, user_id=6102, ticker=ticker, side="BUY", quantity=5)
    assert await _dividends_accrued(conn, 6102, ticker) == 0
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT COALESCE(SUM(-amount), 0) FROM ledger_entries "
            "WHERE reason = 'DIVIDEND' AND account_id = %s AND amount < 0",
            (account_id,),
        )
        row = await cur.fetchone()
        paid_minor = Decimal(row[0])
    assert paid_minor >= accrued - Decimal("1")
    assert await _ledger_sum(conn) == 0


async def test_dividend_drift_offset_stays_zero(conn: AsyncConnection) -> None:
    """Regression guard for the retired drift-offset model: pending and
    resolved dividends must leave instruments.dividend_drift_offset at 0
    and new events must not schedule an offset -- the ex-date drop alone
    funds the payout (a pre-bleed would double-suppress ~2x/cycle)."""
    ticker = await _liquid_ticker(conn)
    mark = await _quoted(conn, ticker)
    await _insert_dividend(conn, ticker, float(mark) * 0.05, resolve_tick=10**9)
    await apply_tick(conn, "div-offset-seed")

    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT dividend_drift_offset FROM instruments WHERE ticker = %s",
            (ticker,),
        )
        assert Decimal((await cur.fetchone())[0]) == 0

    # Force the ex-date: resolution must also keep the aggregate at zero
    # and the rescheduled event carries no offset.
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE events SET resolve_tick = 0 WHERE kind = 'DIVIDEND' "
            "AND NOT resolved AND instrument_id = "
            "(SELECT id FROM instruments WHERE ticker = %s)",
            (ticker,),
        )
    await apply_tick(conn, "div-offset-seed-2")
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT dividend_drift_offset FROM instruments WHERE ticker = %s",
            (ticker,),
        )
        assert Decimal((await cur.fetchone())[0]) == 0
        await cur.execute(
            "SELECT e.drift_offset FROM events e "
            "JOIN instruments i ON i.id = e.instrument_id "
            "WHERE i.ticker = %s AND e.kind = 'DIVIDEND' AND NOT e.resolved",
            (ticker,),
        )
        assert (await cur.fetchone())[0] in (None, Decimal(0))


async def test_bounded_shorts_ignore_dividends(conn: AsyncConnection) -> None:
    """Bounded shorts are a collateralized derivative vs MARKET_MAKER, not
    borrowed stock: no accrual, no cash effect on the ex-date."""
    from stockbot.shorts.service import open_bounded_short

    await bootstrap_user(conn, 6103)
    await _fund(conn, 6103, 10_000_000)
    ticker = await _liquid_ticker(conn)
    mark = await _quoted(conn, ticker)
    await open_bounded_short(conn, user_id=6103, ticker=ticker, quantity=5)

    div = float(mark) * 0.05
    await _insert_dividend(conn, ticker, div)
    account_id = await get_user_account_id(conn, 6103)
    cash_before = await get_balance(conn, account_id)
    await apply_tick(conn, "div-bshort-seed")

    assert await get_balance(conn, account_id) == cash_before
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT COUNT(*) FROM ledger_entries WHERE reason = 'DIVIDEND' "
            "AND account_id = %s",
            (account_id,),
        )
        (count,) = await cur.fetchone()
    assert count == 0
