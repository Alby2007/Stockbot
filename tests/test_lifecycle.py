"""Plan F lifecycle: admin listings and the fixed-mark delisting sweep."""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

import pytest
from psycopg import AsyncConnection
from psycopg.rows import dict_row

from stockbot.accounts.service import bootstrap_user
from stockbot.admin.service import (
    add_instrument,
    delist_instrument,
    ledger_audit,
)
from stockbot.ledger.service import (
    get_balance,
    get_system_account_id,
    post_transfer,
)
from stockbot.market.tick import apply_tick
from stockbot.orders.service import place_order
from stockbot.seasons.service import create_season, join_season, on_tick
from stockbot.shorts.service import open_bounded_short
from stockbot.trading.errors import UnknownInstrumentError
from stockbot.trading.service import execute_trade


async def _first_ticker(conn: AsyncConnection) -> str:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT ticker FROM instruments WHERE is_active AND kind = 'STOCK'"
            " ORDER BY id LIMIT 1"
        )
        (ticker,) = await cur.fetchone()
    return str(ticker)


async def _give_cash(conn: AsyncConnection, account_id: int, amount: int) -> None:
    faucet_id = await get_system_account_id(conn, "FAUCET")
    await post_transfer(
        conn,
        from_account_id=faucet_id,
        to_account_id=account_id,
        amount=amount,
        reason="TEST_TOPUP",
    )


async def _grant_tier(conn: AsyncConnection, user_id: int, tier: int = 1) -> None:
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


async def _quoted(conn: AsyncConnection, ticker: str) -> Decimal:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT quoted_price FROM instruments WHERE ticker = %s", (ticker,)
        )
        row = await cur.fetchone()
    assert row is not None
    return Decimal(row[0])


def _minor(amount: Decimal) -> int:
    return int(amount.quantize(Decimal("1"), ROUND_HALF_UP))


async def _index_row(conn: AsyncConnection) -> dict:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT id, index_divisor, base_price, quoted_price "
            "FROM instruments WHERE kind = 'INDEX'"
        )
        row = await cur.fetchone()
    assert row is not None
    return row


# --- listings ---------------------------------------------------------------


async def test_listing_creates_active_instrument_with_overrides(
    conn: AsyncConnection,
) -> None:
    iid = await add_instrument(
        conn,
        ticker="newco",
        name="New Company",
        sector_key="TECH",
        base_price=42.5,
        sigma=0.001,
        beta=1.5,
        gamma=0.8,
        liquidity=2_000_000.0,
    )
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute("SELECT * FROM instruments WHERE id = %s", (iid,))
        row = await cur.fetchone()
    assert row is not None
    assert row["ticker"] == "NEWCO"
    assert row["is_active"] and row["kind"] == "STOCK"
    assert row["index_member"] is False
    assert Decimal(row["quoted_price"]) == Decimal("42.5")
    assert Decimal(row["base_price"]) == Decimal("42.5")
    assert Decimal(row["fundamental_value"]) == Decimal("42.5")
    assert float(row["sigma"]) == pytest.approx(0.001)
    assert float(row["beta"]) == pytest.approx(1.5)
    assert float(row["gamma"]) == pytest.approx(0.8)
    assert float(row["liquidity"]) == pytest.approx(2_000_000.0)
    # ADV warm-start: seeded at liquidity * flow.adv_ref_frac so the
    # liquidity multiplier opens at 1.0, not the adv_mult_min floor.
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT value FROM config WHERE key = 'flow.adv_ref_frac'"
        )
        ref = float((await cur.fetchone())[0])
    assert float(row["adv"]) == pytest.approx(2_000_000.0 * ref)


async def test_listing_defaults_pull_sector_medians(conn: AsyncConnection) -> None:
    iid = await add_instrument(
        conn,
        ticker="MEDCO",
        name="Median Co",
        sector_key="FIN",
        base_price=10.0,
    )
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT sigma, beta, liquidity FROM instruments WHERE id = %s", (iid,)
        )
        row = await cur.fetchone()
        await cur.execute(
            """
            SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY sigma) AS sigma,
                   percentile_cont(0.5) WITHIN GROUP (ORDER BY beta) AS beta,
                   percentile_cont(0.5) WITHIN GROUP (ORDER BY liquidity) AS liquidity
            FROM instruments i JOIN sectors s ON s.id = i.sector_id
            WHERE i.kind = 'STOCK' AND i.is_active AND s.key = 'FIN'
              AND i.ticker <> 'MEDCO'
            """
        )
        med = await cur.fetchone()
    assert row is not None and med is not None
    assert float(row["sigma"]) == pytest.approx(float(med["sigma"]))
    assert float(row["beta"]) == pytest.approx(float(med["beta"]))
    assert float(row["liquidity"]) == pytest.approx(float(med["liquidity"]))


async def test_listing_validation(conn: AsyncConnection) -> None:
    with pytest.raises(ValueError, match="1-10 chars"):
        await add_instrument(
            conn, ticker="bad ticker!", name="X", sector_key="TECH", base_price=10
        )
    with pytest.raises(ValueError, match="unknown sector"):
        await add_instrument(
            conn, ticker="OKCO", name="X", sector_key="NOPE", base_price=10
        )
    with pytest.raises(ValueError, match="positive"):
        await add_instrument(
            conn, ticker="OKCO", name="X", sector_key="TECH", base_price=-1
        )
    with pytest.raises(ValueError, match="within"):
        await add_instrument(
            conn, ticker="OKCO", name="X", sector_key="TECH", base_price=10, sigma=9.0
        )
    with pytest.raises(ValueError, match="index sector"):
        await add_instrument(
            conn, ticker="OKCO", name="X", sector_key="index", base_price=10
        )
    # Duplicate ticker is rejected.
    ticker = await _first_ticker(conn)
    with pytest.raises(ValueError, match="already listed"):
        await add_instrument(
            conn, ticker=ticker, name="X", sector_key="TECH", base_price=10
        )


async def test_listing_stays_out_of_index_and_trades_next_tick(
    conn: AsyncConnection,
) -> None:
    index_before = await _index_row(conn)
    iid = await add_instrument(
        conn, ticker="FRESH", name="Fresh IPO", sector_key="ENERGY", base_price=25.0
    )
    index_after = await _index_row(conn)
    # Divisor untouched: the listing never entered the basket.
    assert Decimal(index_after["index_divisor"]) == Decimal(index_before["index_divisor"])

    await apply_tick(conn, "lifecycle-seed-1")
    async with conn.cursor() as cur:
        # A candle printed: the name is live.
        await cur.execute(
            "SELECT close FROM candles WHERE instrument_id = %s ORDER BY tick_index DESC",
            (iid,),
        )
        candle = await cur.fetchone()
        assert candle is not None
        # And the basket still excludes it.
        await cur.execute(
            "SELECT index_member FROM instruments WHERE id = %s", (iid,)
        )
        assert (await cur.fetchone())[0] is False


# --- delistings -------------------------------------------------------------


async def test_delist_pays_longs_at_final_mark(conn: AsyncConnection) -> None:
    account_id = (await bootstrap_user(conn, 8001)).account_id
    await _give_cash(conn, account_id, 1_000_000)
    ticker = await _first_ticker(conn)
    await execute_trade(conn, user_id=8001, ticker=ticker, side="BUY", quantity=10)
    mark = await _quoted(conn, ticker)
    cash_before = await get_balance(conn, account_id)

    report = await delist_instrument(conn, ticker)

    assert report.mark_price == mark
    assert report.positions_settled == 1
    expected = cash_before + _minor(Decimal(10) * mark * 100)
    assert await get_balance(conn, account_id) == expected
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT quantity FROM positions p JOIN instruments i ON i.id = p.instrument_id"
            " WHERE p.user_id = 8001 AND i.ticker = %s",
            (ticker,),
        )
        assert (await cur.fetchone())[0] == 0
        await cur.execute(
            "SELECT is_active, delisted_tick FROM instruments WHERE ticker = %s",
            (ticker,),
        )
        row = await cur.fetchone()
    assert row[0] is False


async def test_delist_covers_short_and_settles_carry_debts_first(
    conn: AsyncConnection,
) -> None:
    account_id = (await bootstrap_user(conn, 8002)).account_id
    await _give_cash(conn, account_id, 10_000_000)
    await _grant_tier(conn, 8002)
    ticker = await _first_ticker(conn)
    await execute_trade(conn, user_id=8002, ticker=ticker, side="SELL", quantity=5)
    mark = await _quoted(conn, ticker)

    # Simulate accrued carry debts deterministically.
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE positions p SET borrow_fees_accrued = 1000, dividends_accrued = 500"
            " FROM instruments i WHERE p.instrument_id = i.id"
            " AND i.ticker = %s AND p.user_id = 8002",
            (ticker,),
        )
    cash_before = await get_balance(conn, account_id)

    report = await delist_instrument(conn, ticker)

    cover_cost = _minor(Decimal(5) * mark * 100)
    assert report.shorts_covered == 1
    # Cash leaves in plan order: fee (1000) -> SINK, dividend (500) -> MM,
    # then the cover leg -> MM.
    assert await get_balance(conn, account_id) == cash_before - 1000 - 500 - cover_cost
    async with conn.cursor() as cur:
        await cur.execute(
            """
            SELECT l.reason, l.amount FROM ledger_entries l
            JOIN accounts a ON a.id = l.account_id
            WHERE a.id = %s AND l.reason IN ('BORROW_FEE', 'DIVIDEND', 'DELIST_COVER')
            ORDER BY l.id
            """,
            (account_id,),
        )
        legs = await cur.fetchall()
    assert [leg[0] for leg in legs] == ["BORROW_FEE", "DIVIDEND", "DELIST_COVER"]
    assert [int(leg[1]) for leg in legs] == [-1000, -500, -cover_cost]


async def test_delist_short_shortfall_uses_fund_then_mm(conn: AsyncConnection) -> None:
    account_id = (await bootstrap_user(conn, 8003)).account_id
    await _give_cash(conn, account_id, 40_000_000)  # $400k equity
    await _grant_tier(conn, 8003, tier=3)
    ticker = await _first_ticker(conn)
    mark = await _quoted(conn, ticker)
    # Short ~$320k total -- cover cost exceeds the $50k insurance fund seed.
    # Split across fills: the participation cap is per-fill.
    qty = 0
    per_fill = int(80_000 / float(mark))
    for _ in range(4):
        await execute_trade(
            conn, user_id=8003, ticker=ticker, side="SELL", quantity=per_fill
        )
        qty += per_fill
    mark = await _quoted(conn, ticker)
    sink_id = await get_system_account_id(conn, "SINK")
    # Drain cash to nearly zero so the cover must be backstopped.
    cash = await get_balance(conn, account_id)
    await post_transfer(
        conn,
        from_account_id=account_id,
        to_account_id=sink_id,
        amount=cash - 10_000,
        reason="TEST_DRAIN",
    )

    fund_id = await get_system_account_id(conn, "INSURANCE_FUND")
    fund_before = await get_balance(conn, fund_id)
    report = await delist_instrument(conn, ticker)

    assert await get_balance(conn, account_id) == 0
    assert report.fund_paid_minor == fund_before
    cost = _minor(Decimal(qty) * mark * 100)
    assert report.mm_absorbed_minor == cost - 10_000 - fund_before
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT reason, amount_minor, mm_absorbed_minor FROM insurance_fund_flows"
            " ORDER BY id DESC LIMIT 2"
        )
        flows = await cur.fetchall()
    assert {f[0] for f in flows} == {"COVER_SHORTFALL", "ADL"}


async def test_delist_bounded_short_settles_at_intrinsic(conn: AsyncConnection) -> None:
    account_id = (await bootstrap_user(conn, 8004)).account_id
    await _give_cash(conn, account_id, 10_000_000)
    ticker = await _first_ticker(conn)
    entry_mark = await _quoted(conn, ticker)
    short = await open_bounded_short(conn, user_id=8004, ticker=ticker, quantity=5)

    # Move the mark down ~10%: the short is in the money.
    new_mark = (entry_mark * Decimal("0.9")).quantize(Decimal("0.000001"))
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE instruments SET quoted_price = %s WHERE ticker = %s",
            (new_mark, ticker),
        )
    cash_before = await get_balance(conn, account_id)

    report = await delist_instrument(conn, ticker)

    intrinsic = _minor(
        Decimal(short.collateral_minor)
        + Decimal(5) * (short.entry_price - new_mark) * 100
    )
    assert report.bounded_shorts_settled == 1
    assert intrinsic > short.collateral_minor  # sanity: profitable
    assert await get_balance(conn, account_id) == cash_before + intrinsic
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT status, close_price, payout_minor FROM bounded_shorts WHERE id = %s",
            (short.short_id,),
        )
        row = await cur.fetchone()
    assert row[0] == "DELISTED"
    assert Decimal(row[1]) == new_mark
    assert int(row[2]) == intrinsic


async def test_delist_bounded_short_past_knockout_pays_zero(
    conn: AsyncConnection,
) -> None:
    account_id = (await bootstrap_user(conn, 8005)).account_id
    await _give_cash(conn, account_id, 10_000_000)
    ticker = await _first_ticker(conn)
    short = await open_bounded_short(conn, user_id=8005, ticker=ticker, quantity=5)

    # Mark beyond the knockout price: intrinsic is fully negative.
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE instruments SET quoted_price = %s WHERE ticker = %s",
            (short.knockout_price * Decimal("1.05"), ticker),
        )
    cash_before = await get_balance(conn, account_id)

    await delist_instrument(conn, ticker)

    assert await get_balance(conn, account_id) == cash_before
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT status, payout_minor FROM bounded_shorts WHERE id = %s",
            (short.short_id,),
        )
        row = await cur.fetchone()
    assert row[0] == "DELISTED"
    assert int(row[1]) == 0


async def test_delist_cancels_orders_and_resolves_events(
    conn: AsyncConnection,
) -> None:
    account_id = (await bootstrap_user(conn, 8006)).account_id
    await _give_cash(conn, account_id, 10_000_000)
    ticker = await _first_ticker(conn)
    mark = await _quoted(conn, ticker)
    # A resting bid far below mark -- would never fill anyway.
    await place_order(
        conn,
        user_id=8006,
        ticker=ticker,
        side="BUY",
        quantity=1,
        limit_price=mark * Decimal("0.5"),
    )
    # League order on the same instrument.
    season_id = await create_season(
        conn, name="Delist League", start_tick=0, end_tick=10_000,
        entry_fee_minor=0, stake_minor=100_000,
    )
    await on_tick(conn, 0)
    await join_season(conn, 8006, season_id)
    await place_order(
        conn,
        user_id=8006,
        ticker=ticker,
        side="BUY",
        quantity=1,
        limit_price=mark * Decimal("0.5"),
        season_id=season_id,
    )
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT id FROM instruments WHERE ticker = %s", (ticker,)
        )
        iid = int((await cur.fetchone())[0])
        await cur.execute(
            """
            INSERT INTO events (instrument_id, kind, scheduled_tick, resolve_tick,
                                headline, magnitude)
            VALUES (%s, 'NEWS', 0, 5, 'rumor', 0.05),
                   (%s, 'EARNINGS', 0, 5, NULL, NULL)
            """,
            (iid, iid),
        )

    report = await delist_instrument(conn, ticker)

    assert report.orders_cancelled == 2
    assert report.events_resolved >= 2  # seeded pending events may also exist
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT COUNT(*) FROM orders WHERE instrument_id = %s AND status = 'OPEN'",
            (iid,),
        )
        assert (await cur.fetchone())[0] == 0
        await cur.execute(
            "SELECT COUNT(*) FROM events WHERE instrument_id = %s AND NOT resolved",
            (iid,),
        )
        assert (await cur.fetchone())[0] == 0


async def test_delisted_instrument_never_steps_again(conn: AsyncConnection) -> None:
    ticker = await _first_ticker(conn)
    await apply_tick(conn, "lifecycle-seed-2")
    await delist_instrument(conn, ticker)
    mark = await _quoted(conn, ticker)
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT id FROM instruments WHERE ticker = %s", (ticker,)
        )
        iid = int((await cur.fetchone())[0])
        await cur.execute("SELECT MAX(tick_index) FROM market_ticks")
        last_tick = int((await cur.fetchone())[0])

    await apply_tick(conn, "lifecycle-seed-2")

    assert await _quoted(conn, ticker) == mark
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT COUNT(*) FROM candles WHERE instrument_id = %s AND tick_index > %s",
            (iid, last_tick),
        )
        assert (await cur.fetchone())[0] == 0
    # And every trading surface rejects it now.
    with pytest.raises(UnknownInstrumentError):
        await execute_trade(conn, user_id=8007, ticker=ticker, side="BUY", quantity=1)


async def test_delist_while_halted_settles_immediately(conn: AsyncConnection) -> None:
    account_id = (await bootstrap_user(conn, 8008)).account_id
    await _give_cash(conn, account_id, 1_000_000)
    ticker = await _first_ticker(conn)
    await execute_trade(conn, user_id=8008, ticker=ticker, side="BUY", quantity=3)
    # Circuit-halt it, then delist -- settlement must not wait for the halt.
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE instruments SET circuit_halted_until_tick = 999999 "
            "WHERE ticker = %s",
            (ticker,),
        )
    mark = await _quoted(conn, ticker)
    cash_before = await get_balance(conn, account_id)

    report = await delist_instrument(conn, ticker)

    assert report.positions_settled == 1
    assert await get_balance(conn, account_id) == cash_before + _minor(
        Decimal(3) * mark * 100
    )


async def test_delist_index_member_rebases_divisor(conn: AsyncConnection) -> None:
    """Delisting a basket member is a fast-track deletion: the divisor is
    re-based so the index level is continuous across the removal."""
    index = await _index_row(conn)
    level_before = float(index["base_price"])
    divisor_before = float(index["index_divisor"])
    ticker = await _first_ticker(conn)
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT index_member FROM instruments WHERE ticker = %s", (ticker,)
        )
        assert (await cur.fetchone())[0] is True

    await delist_instrument(conn, ticker)

    index_after = await _index_row(conn)
    divisor_after = float(index_after["index_divisor"])
    assert divisor_after != divisor_before
    # The divisor change keeps basket/divisor == the pre-delist level:
    # level continuity is exact at the delist instant.
    async with conn.cursor() as cur:
        await cur.execute(
            """
            SELECT COALESCE(SUM(i.float_shares * i.quoted_price), 0)
            FROM instruments i
            WHERE i.kind = 'STOCK' AND i.index_member AND i.is_active
            """
        )
        basket = float((await cur.fetchone())[0])
    assert basket / divisor_after == pytest.approx(level_before, rel=1e-9)


async def test_delist_last_index_constituent_refused(conn: AsyncConnection) -> None:
    ticker = await _first_ticker(conn)
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE instruments SET index_member = FALSE "
            "WHERE kind = 'STOCK' AND ticker <> %s",
            (ticker,),
        )
    with pytest.raises(ValueError, match="last index constituent"):
        await delist_instrument(conn, ticker)
    # The refusal rolled back: still active.
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT is_active FROM instruments WHERE ticker = %s", (ticker,)
        )
        assert (await cur.fetchone())[0] is True


async def test_delist_unknown_and_inactive_tickers_rejected(
    conn: AsyncConnection,
) -> None:
    with pytest.raises(UnknownInstrumentError):
        await delist_instrument(conn, "NOSUCH")
    ticker = await _first_ticker(conn)
    await delist_instrument(conn, ticker)
    with pytest.raises(ValueError, match="already delisted"):
        await delist_instrument(conn, ticker)


async def test_ledger_audit_clean_after_delist(conn: AsyncConnection) -> None:
    """The whole settlement path posts through the ledger: sum-zero holds."""
    account_id = (await bootstrap_user(conn, 8009)).account_id
    await _give_cash(conn, account_id, 10_000_000)
    await _grant_tier(conn, 8009)
    ticker = await _first_ticker(conn)
    await execute_trade(conn, user_id=8009, ticker=ticker, side="BUY", quantity=4)
    await execute_trade(conn, user_id=8009, ticker=ticker, side="SELL", quantity=2)
    await open_bounded_short(conn, user_id=8009, ticker=ticker, quantity=3)
    await delist_instrument(conn, ticker)

    report = await ledger_audit(conn)
    assert report.healthy, report
