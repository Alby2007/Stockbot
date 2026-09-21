"""Phase 2 margin: signed positions, margin gates, borrow fees, liquidation,
short interest, squeeze mechanic, and the SBX-40 index."""

from __future__ import annotations

from decimal import Decimal

import pytest
from psycopg import AsyncConnection

from stockbot.accounts.service import bootstrap_user
from stockbot.ledger.service import get_balance, get_system_account_id, post_transfer
from stockbot.margin.errors import (
    InsufficientMarginError,
    MarginNotUnlockedError,
    MarginSpendBlockedError,
    PositionLimitError,
    ShortInterestLimitError,
)
from stockbot.margin.service import (
    accrue_borrow_fees,
    check_and_liquidate,
    compute_health,
    margin_config,
    margin_tier,
    refresh_short_interest,
)
from stockbot.market.tick import apply_tick
from stockbot.shop.service import buy_item
from stockbot.trading.service import execute_trade


async def _first_ticker(conn: AsyncConnection) -> str:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT ticker FROM instruments WHERE is_active AND kind = 'STOCK'"
            " ORDER BY id LIMIT 1"
        )
        (ticker,) = await cur.fetchone()
    return ticker


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


async def _position(conn: AsyncConnection, user_id: int, ticker: str):
    async with conn.cursor() as cur:
        await cur.execute(
            """
            SELECT p.quantity, p.avg_cost, p.borrow_fees_accrued
            FROM positions p JOIN instruments i ON i.id = p.instrument_id
            WHERE p.user_id = %s AND i.ticker = %s AND p.season_id IS NULL
            """,
            (user_id, ticker),
        )
        return await cur.fetchone()


async def test_shorting_requires_margin_tier(conn: AsyncConnection) -> None:
    await bootstrap_user(conn, 2001)
    ticker = await _first_ticker(conn)
    with pytest.raises(MarginNotUnlockedError):
        await execute_trade(conn, user_id=2001, ticker=ticker, side="SELL", quantity=1)


async def test_short_open_credits_proceeds_and_marks_negative(
    conn: AsyncConnection,
) -> None:
    account_id = await bootstrap_user(conn, 2002)
    await _give_cash(conn, account_id, 1_000_000)
    await _grant_tier(conn, 2002)
    ticker = await _first_ticker(conn)
    before = await get_balance(conn, account_id)

    result = await execute_trade(conn, user_id=2002, ticker=ticker, side="SELL", quantity=2)
    pos = await _position(conn, 2002, ticker)
    assert pos is not None and pos[0] == -2
    assert pos[1] == result.fill_price  # avg_cost = entry for a fresh short
    after = await get_balance(conn, account_id)
    # proceeds minus the trading fee
    assert after == before + result.notional_minor - result.fee_minor

    health = await compute_health(conn, 2002)
    assert health.margined
    assert health.maint_req_minor > 0
    # opening is equity-neutral modulo fees/impact
    assert health.equity_minor < before + 100  # sanity bound


async def test_initial_margin_blocks_oversized_short(conn: AsyncConnection) -> None:
    account_id = await bootstrap_user(conn, 2003)
    await _grant_tier(conn, 2003)
    ticker = await _first_ticker(conn)
    cash = await get_balance(conn, account_id)
    # try to short more than 2x equity allows (kept small enough that the
    # participation cap doesn't reject it before the margin gate can)
    with pytest.raises((InsufficientMarginError, PositionLimitError)):
        await execute_trade(
            conn, user_id=2003, ticker=ticker, side="SELL", quantity=5
        )
    assert cash == await get_balance(conn, account_id)  # rolled back cleanly


async def test_cover_short_via_buy(conn: AsyncConnection) -> None:
    account_id = await bootstrap_user(conn, 2004)
    await _give_cash(conn, account_id, 1_000_000)
    await _grant_tier(conn, 2004)
    ticker = await _first_ticker(conn)
    await execute_trade(conn, user_id=2004, ticker=ticker, side="SELL", quantity=2)
    await execute_trade(conn, user_id=2004, ticker=ticker, side="BUY", quantity=2)
    pos = await _position(conn, 2004, ticker)
    assert pos is None or pos[0] == 0
    health = await compute_health(conn, 2004)
    assert not health.margined


async def test_borrow_fees_accrue_and_settle_on_cover(
    conn: AsyncConnection,
) -> None:
    account_id = await bootstrap_user(conn, 2005)
    await _give_cash(conn, account_id, 1_000_000)
    await _grant_tier(conn, 2005)
    ticker = await _first_ticker(conn)
    await execute_trade(conn, user_id=2005, ticker=ticker, side="SELL", quantity=5)

    # crank the borrow rate so one tick accrues >= 1 minor unit
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE config SET value = 500 WHERE key = 'margin.borrow_fee_bps_per_tick'"
        )
    await apply_tick(conn, "borrow-test")
    pos = await _position(conn, 2005, ticker)
    assert pos is not None and Decimal(pos[2]) > 0  # accrued something

    await execute_trade(conn, user_id=2005, ticker=ticker, side="BUY", quantity=5)
    pos = await _position(conn, 2005, ticker)
    assert pos is None or Decimal(pos[2]) == 0
    # a BORROW_FEE ledger posting exists
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT 1 FROM ledger_entries WHERE reason = 'BORROW_FEE' LIMIT 1"
        )
        assert await cur.fetchone() is not None


async def _levered_short(
    conn: AsyncConnection, user_id: int, ticker: str, leverage: float = 1.8
) -> None:
    """Short ~leverage*equity of `ticker` with known margin params (init 50%,
    maint 30%) so a moderate adverse move breaches maintenance."""
    async with conn.cursor() as cur:
        await cur.execute(
            """
            UPDATE instruments
            SET init_margin_pct = 0.5, maint_margin_pct = 0.3
            WHERE ticker = %s
            RETURNING quoted_price
            """,
            (ticker,),
        )
        (price,) = await cur.fetchone()
        await cur.execute(
            "SELECT balance FROM accounts WHERE user_id = %s AND kind = 'USER'",
            (user_id,),
        )
        (cash,) = await cur.fetchone()
    qty = max(1, int(Decimal(cash) * Decimal(leverage) / (Decimal(price) * 100)))
    await execute_trade(conn, user_id=user_id, ticker=ticker, side="SELL", quantity=qty)


async def test_liquidation_fires_when_price_gaps_up(conn: AsyncConnection) -> None:
    await bootstrap_user(conn, 2006)
    await _grant_tier(conn, 2006)
    ticker = await _first_ticker(conn)
    await _levered_short(conn, 2006, ticker)

    # gap the instrument up far beyond maintenance
    async with conn.cursor() as cur:
        await cur.execute(
            """
            UPDATE instruments
            SET base_price = base_price * 3, quoted_price = quoted_price * 3
            WHERE ticker = %s
            """,
            (ticker,),
        )
    health = await compute_health(conn, 2006)
    assert health.undermargined

    legs = await check_and_liquidate(conn, 2006)
    assert legs >= 1
    pos = await _position(conn, 2006, ticker)
    assert pos is None or pos[0] == 0

    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT COUNT(*) FROM liquidations WHERE user_id = 2006"
        )
        (count,) = await cur.fetchone()
    assert count >= 1

    health = await compute_health(conn, 2006)
    assert health.equity_minor >= 0


async def test_never_negative_equity_and_fund_reconciles(
    conn: AsyncConnection,
) -> None:
    """Property: whatever the gap, user equity never lands below zero, and
    insurance_fund_flows + the fund balance always reconcile."""
    await bootstrap_user(conn, 2007)
    await _grant_tier(conn, 2007, tier=3)
    ticker = await _first_ticker(conn)
    await _levered_short(conn, 2007, ticker)

    # catastrophic gap: 10x against the short
    async with conn.cursor() as cur:
        await cur.execute(
            """
            UPDATE instruments
            SET base_price = base_price * 10, quoted_price = quoted_price * 10
            WHERE ticker = %s
            """,
            (ticker,),
        )
    await check_and_liquidate(conn, 2007)

    health = await compute_health(conn, 2007)
    assert health.equity_minor >= 0

    # fund reconciliation: seed + flows == balance
    fund_id = await get_system_account_id(conn, "INSURANCE_FUND")
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT COALESCE(SUM(amount_minor), 0) FROM insurance_fund_flows"
        )
        (flows,) = await cur.fetchone()
    fund_balance = await get_balance(conn, fund_id)
    assert fund_balance == int(flows)


async def test_liquidation_sweep_inside_tick(conn: AsyncConnection) -> None:
    await bootstrap_user(conn, 2008)
    await _grant_tier(conn, 2008)
    ticker = await _first_ticker(conn)
    await _levered_short(conn, 2008, ticker)

    async with conn.cursor() as cur:
        await cur.execute(
            """
            UPDATE instruments
            SET base_price = base_price * 4, quoted_price = quoted_price * 4,
                impact = 0
            WHERE ticker = %s
            """,
            (ticker,),
        )
    await apply_tick(conn, "liq-tick-test")
    pos = await _position(conn, 2008, ticker)
    assert pos is None or pos[0] == 0


async def test_short_interest_cap(conn: AsyncConnection) -> None:
    await bootstrap_user(conn, 2009)
    await _grant_tier(conn, 2009)
    ticker = await _first_ticker(conn)
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE instruments SET float_shares = 3 WHERE ticker = %s", (ticker,)
        )
    with pytest.raises(ShortInterestLimitError):
        await execute_trade(conn, user_id=2009, ticker=ticker, side="SELL", quantity=4)


async def test_squeeze_boosts_buy_impact(conn: AsyncConnection) -> None:
    account_id = await bootstrap_user(conn, 2010)
    await _give_cash(conn, account_id, 1_000_000)
    ticker = await _first_ticker(conn)
    # Tick rounding quantizes the impact delta to ~tick/price, which at a
    # 1-share fill swamps the boost ratio. Flatten the grid -- this test
    # targets the squeeze boost, not the grid.
    async with conn.cursor() as cur:
        await cur.execute("UPDATE config SET value = 0 WHERE key = 'spread.tick_pct'")
        await cur.execute(
            "UPDATE config SET value = 0.0000001 WHERE key = 'spread.tick_min'"
        )

    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT impact FROM instruments WHERE ticker = %s", (ticker,)
        )
        (impact0,) = await cur.fetchone()
    await execute_trade(conn, user_id=2010, ticker=ticker, side="BUY", quantity=1)
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT impact FROM instruments WHERE ticker = %s", (ticker,)
        )
        (impact1,) = await cur.fetchone()
    delta_normal = float(impact1) - float(impact0)

    # crank SI above the squeeze threshold and repeat
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE instruments SET short_interest_pct = 0.9, impact = %s"
            " WHERE ticker = %s",
            (impact0, ticker),
        )
    await execute_trade(conn, user_id=2010, ticker=ticker, side="BUY", quantity=1)
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT impact FROM instruments WHERE ticker = %s", (ticker,)
        )
        (impact2,) = await cur.fetchone()
    delta_squeezed = float(impact2) - float(impact0)

    assert delta_squeezed > delta_normal * 2


async def test_spend_gate_blocks_shop_when_margined(conn: AsyncConnection) -> None:
    account_id = await bootstrap_user(conn, 2011)
    await _grant_tier(conn, 2011)
    ticker = await _first_ticker(conn)
    await _levered_short(conn, 2011, ticker)
    health = await compute_health(conn, 2011)
    # spend everything but a hair over maintenance
    drain = health.equity_minor - health.maint_req_minor + 1
    faucet = await get_system_account_id(conn, "FAUCET")
    await post_transfer(
        conn,
        from_account_id=account_id,
        to_account_id=faucet,
        amount=drain,
        reason="TEST_DRAIN",
    )
    with pytest.raises(MarginSpendBlockedError):
        await buy_item(conn, 2011, "theme_sunrise")


async def test_sbx40_index_tracks_components(conn: AsyncConnection) -> None:
    await apply_tick(conn, "index-test")
    async with conn.cursor() as cur:
        await cur.execute(
            """
            SELECT i.quoted_price, i.index_divisor,
                   (SELECT SUM(s.float_shares * s.quoted_price)
                    FROM instruments s WHERE s.kind = 'STOCK' AND s.is_active)
            FROM instruments i WHERE i.ticker = 'SBX40'
            """
        )
        row = await cur.fetchone()
    assert row is not None
    quoted, divisor, basket = row
    expected = float(basket) / float(divisor)
    assert abs(float(quoted) - expected) / expected < 0.01  # within ~1% (impact only)

    # and it trades like a normal instrument
    account_id = await bootstrap_user(conn, 2012)
    await _give_cash(conn, account_id, 1_000_000)
    await execute_trade(
        conn, user_id=2012, ticker="SBX40", side="BUY", quantity=1
    )
    pos = await _position(conn, 2012, "SBX40")
    assert pos is not None and pos[0] == 1


async def test_margin_tier_purchase_unlocks_and_scales(conn: AsyncConnection) -> None:
    await bootstrap_user(conn, 2013)
    assert await margin_tier(conn, 2013, None) == 0
    account_id = await bootstrap_user(conn, 2013)
    await _give_cash(conn, account_id, 10_000_000)
    await buy_item(conn, 2013, "margin_tier")
    assert await margin_tier(conn, 2013, None) == 1
    await buy_item(conn, 2013, "margin_tier")
    assert await margin_tier(conn, 2013, None) == 2
    cfg = await margin_config(conn)
    from stockbot.margin.service import leverage_cap

    assert leverage_cap(2, cfg) == Decimal(3)


async def test_refresh_short_interest(conn: AsyncConnection) -> None:
    account_id = await bootstrap_user(conn, 2014)
    await _give_cash(conn, account_id, 1_000_000)
    await _grant_tier(conn, 2014)
    ticker = await _first_ticker(conn)
    await execute_trade(conn, user_id=2014, ticker=ticker, side="SELL", quantity=2)
    await refresh_short_interest(conn)
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT short_interest_pct, float_shares FROM instruments WHERE ticker = %s",
            (ticker,),
        )
        si, float_shares = await cur.fetchone()
    assert float(si) == pytest.approx(2 / float(float_shares), abs=1e-6)


async def test_adl_absorption_is_not_a_fund_outflow(conn: AsyncConnection) -> None:
    """ADL is MARKET_MAKER's loss: flows must keep
    `fund_balance == SUM(amount_minor)` with the absorbed amount on
    `mm_absorbed_minor`, not as a phantom fund payment."""
    await bootstrap_user(conn, 2015)
    await _grant_tier(conn, 2015)
    ticker = await _first_ticker(conn)
    await _levered_short(conn, 2015, ticker)

    async with conn.cursor() as cur:
        # Bankrupt the fund so the entire shortfall lands on MARKET_MAKER.
        await cur.execute(
            "UPDATE accounts SET balance = 0 WHERE system_name = 'INSURANCE_FUND'"
        )
        await cur.execute(
            "UPDATE instruments SET base_price = base_price * 10, "
            "quoted_price = quoted_price * 10 WHERE ticker = %s",
            (ticker,),
        )
    await check_and_liquidate(conn, 2015)

    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT COALESCE(SUM(amount_minor), 0), "
            "COALESCE(SUM(mm_absorbed_minor), 0) FROM insurance_fund_flows"
        )
        row = await cur.fetchone()
        assert row is not None
        flows, absorbed = int(row[0]), int(row[1])
    assert absorbed > 0

    fund_id = await get_system_account_id(conn, "INSURANCE_FUND")
    # flows - seed == everything the fund actually moved since seeding;
    # the drained balance proves the ADL amount never left the fund.
    assert int(flows) - 50_000_00 == await get_balance(conn, fund_id)


async def test_borrow_fees_accrue_with_zero_si_cap(conn: AsyncConnection) -> None:
    """margin.max_short_interest_pct = 0 must not NULL the utilization
    divisor -- fees still accrue instead of crashing on Decimal(None)
    at the next cover."""
    account_id = await bootstrap_user(conn, 2016)
    await _give_cash(conn, account_id, 1_000_000)
    await _grant_tier(conn, 2016)
    ticker = await _first_ticker(conn)
    await execute_trade(conn, user_id=2016, ticker=ticker, side="SELL", quantity=2)

    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE config SET value = 0 WHERE key = 'margin.max_short_interest_pct'"
        )
    await refresh_short_interest(conn)
    await accrue_borrow_fees(conn)

    pos = await _position(conn, 2016, ticker)
    assert pos is not None and pos[2] > 0
