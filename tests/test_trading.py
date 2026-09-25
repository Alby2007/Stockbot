from __future__ import annotations

from decimal import Decimal

import pytest
from psycopg import AsyncConnection

from stockbot.accounts.service import bootstrap_user
from stockbot.ledger.errors import InsufficientFundsError
from stockbot.ledger.service import get_balance, get_system_account_id, post_transfer
from stockbot.margin.errors import MarginNotUnlockedError
from stockbot.market.tick import apply_tick
from stockbot.trading.errors import (
    DuplicateInteractionError,
    InstrumentHaltedError,
    SlippageExceededError,
    UnknownInstrumentError,
)
from stockbot.trading.service import (
    execute_trade,
    max_affordable_shares,
    quote_trade,
    shares_for_dollars,
)


async def _first_ticker(conn: AsyncConnection) -> str:
    async with conn.cursor() as cur:
        await cur.execute("SELECT ticker FROM instruments WHERE is_active ORDER BY id LIMIT 1")
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


async def test_buy_moves_cash_creates_position_and_pays_a_fee(conn: AsyncConnection) -> None:
    account_id = (await bootstrap_user(conn, 1001)).account_id
    await _give_cash(conn, account_id, 1_000_000)  # $10,000
    ticker = await _first_ticker(conn)

    balance_before = await get_balance(conn, account_id)
    result = await execute_trade(conn, user_id=1001, ticker=ticker, side="BUY", quantity=10)

    assert result.quantity == 10
    assert result.fee_minor > 0

    balance_after = await get_balance(conn, account_id)
    assert balance_after == balance_before - result.notional_minor - result.fee_minor

    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT quantity, avg_cost FROM positions WHERE user_id = %s", (1001,)
        )
        quantity, avg_cost = await cur.fetchone()
    assert quantity == 10
    assert avg_cost == result.fill_price


async def test_buy_pushes_price_up_via_impact(conn: AsyncConnection) -> None:
    account_id = (await bootstrap_user(conn, 1002)).account_id
    await _give_cash(conn, account_id, 100_000_000)
    ticker = await _first_ticker(conn)

    async with conn.cursor() as cur:
        await cur.execute("SELECT base_price FROM instruments WHERE ticker = %s", (ticker,))
        (base_price,) = await cur.fetchone()

    # Under the participation cap but still large enough to move the mark.
    result = await execute_trade(conn, user_id=1002, ticker=ticker, side="BUY", quantity=100)
    assert result.fill_price > base_price

    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT impact, quoted_price FROM instruments WHERE ticker = %s", (ticker,)
        )
        impact, quoted_price = await cur.fetchone()
    assert impact > 0
    assert quoted_price > base_price


async def test_sell_without_position_requires_margin_tier(conn: AsyncConnection) -> None:
    """Phase 2: selling more than held opens a true margin short, which needs
    a margin tier entitlement. Without one it's rejected."""
    await bootstrap_user(conn, 1003)
    ticker = await _first_ticker(conn)
    with pytest.raises(MarginNotUnlockedError):
        await execute_trade(conn, user_id=1003, ticker=ticker, side="SELL", quantity=1)


async def test_buy_then_sell_all_returns_cash_minus_two_fees(conn: AsyncConnection) -> None:
    account_id = (await bootstrap_user(conn, 1004)).account_id
    await _give_cash(conn, account_id, 1_000_000)
    ticker = await _first_ticker(conn)

    balance_before = await get_balance(conn, account_id)
    buy = await execute_trade(conn, user_id=1004, ticker=ticker, side="BUY", quantity=10)
    sell = await execute_trade(conn, user_id=1004, ticker=ticker, side="SELL", quantity=10)
    balance_after = await get_balance(conn, account_id)

    # Selling a round trip must be loss-making once fees/spread are accounted
    # for (the wash-trade defense from the design doc), never a free profit.
    assert balance_after < balance_before
    assert buy.fee_minor > 0 and sell.fee_minor > 0

    async with conn.cursor() as cur:
        await cur.execute("SELECT quantity FROM positions WHERE user_id = %s", (1004,))
        (quantity,) = await cur.fetchone()
    assert quantity == 0


async def test_insufficient_funds_rejects_buy_atomically(conn: AsyncConnection) -> None:
    account_id = (await bootstrap_user(conn, 1005)).account_id  # only the starting grant, no top-up
    ticker = await _first_ticker(conn)
    balance_before = await get_balance(conn, account_id)

    # More than the grant can afford, under the participation cap.
    with pytest.raises(InsufficientFundsError):
        await execute_trade(conn, user_id=1005, ticker=ticker, side="BUY", quantity=5)

    assert await get_balance(conn, account_id) == balance_before
    async with conn.cursor() as cur:
        await cur.execute("SELECT COUNT(*) FROM trades WHERE user_id = %s", (1005,))
        (trade_count,) = await cur.fetchone()
    assert trade_count == 0


async def test_unknown_ticker_is_rejected(conn: AsyncConnection) -> None:
    await bootstrap_user(conn, 1006)
    with pytest.raises(UnknownInstrumentError):
        await execute_trade(conn, user_id=1006, ticker="NOPE", side="BUY", quantity=1)


async def test_halted_instrument_is_rejected(conn: AsyncConnection) -> None:
    account_id = (await bootstrap_user(conn, 1007)).account_id
    await _give_cash(conn, account_id, 1_000_000)
    ticker = await _first_ticker(conn)
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE instruments SET circuit_halted_until_tick = 999999 WHERE ticker = %s", (ticker,)
        )
    with pytest.raises(InstrumentHaltedError):
        await execute_trade(conn, user_id=1007, ticker=ticker, side="BUY", quantity=1)


async def test_duplicate_interaction_id_is_a_replay_not_a_double_trade(
    conn: AsyncConnection,
) -> None:
    account_id = (await bootstrap_user(conn, 1008)).account_id
    await _give_cash(conn, account_id, 1_000_000)
    ticker = await _first_ticker(conn)

    await execute_trade(
        conn,
        user_id=1008,
        ticker=ticker,
        side="BUY",
        quantity=1,
        interaction_id="interaction-1",
    )
    balance_after_first = await get_balance(conn, account_id)

    with pytest.raises(DuplicateInteractionError):
        await execute_trade(
            conn,
            user_id=1008,
            ticker=ticker,
            side="BUY",
            quantity=1,
            interaction_id="interaction-1",
        )

    assert await get_balance(conn, account_id) == balance_after_first
    async with conn.cursor() as cur:
        await cur.execute("SELECT COUNT(*) FROM trades WHERE user_id = %s", (1008,))
        (trade_count,) = await cur.fetchone()
    assert trade_count == 1


async def test_trade_prints_onto_the_latest_candle(conn: AsyncConnection) -> None:
    """Fills between ticks extend the latest candle's high/low and volume --
    otherwise intra-tick trades are invisible to /chart and volume stays 0."""
    account_id = (await bootstrap_user(conn, 1009)).account_id
    await _give_cash(conn, account_id, 1_000_000)
    ticker = await _first_ticker(conn)
    tick_index = await apply_tick(conn, "candle-print-seed")

    result = await execute_trade(conn, user_id=1009, ticker=ticker, side="BUY", quantity=10)

    async with conn.cursor() as cur:
        await cur.execute(
            """
            SELECT c.high, c.low, c.volume
            FROM candles c
            JOIN instruments i ON i.id = c.instrument_id
            WHERE i.ticker = %s AND c.tick_index = %s
            """,
            (ticker, tick_index),
        )
        row = await cur.fetchone()
    assert row is not None
    high, low, volume = row
    assert float(volume) == 10
    assert float(low) <= float(result.fill_price) <= float(high)


async def test_slippage_cap_rejects_and_reports_the_would_be_fill(
    conn: AsyncConnection,
) -> None:
    """B1: `max_slippage` is a marketable-limit collar -- a zero bound
    rejects every fill (spread+impact always deviate from the mark), and
    the error carries the would-be fill for the reply. A 100% bound is
    today's behavior: fills unconditionally."""
    account_id = (await bootstrap_user(conn, 1010)).account_id
    await _give_cash(conn, account_id, 1_000_000)
    ticker = await _first_ticker(conn)
    balance_before = await get_balance(conn, account_id)

    with pytest.raises(SlippageExceededError) as excinfo:
        await execute_trade(
            conn, user_id=1010, ticker=ticker, side="BUY", quantity=5,
            max_slippage=Decimal("0"),
        )
    assert excinfo.value.fill_price > 0
    assert excinfo.value.mark > 0
    assert float(excinfo.value.fill_price) != excinfo.value.mark

    # Atomic: nothing settled, no position, no trade row.
    assert await get_balance(conn, account_id) == balance_before
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT COUNT(*) FROM trades WHERE user_id = %s", (1010,)
        )
        (trade_count,) = await cur.fetchone()
        await cur.execute(
            "SELECT COUNT(*) FROM positions WHERE user_id = %s AND quantity <> 0",
            (1010,),
        )
        (pos_count,) = await cur.fetchone()
    assert trade_count == 0 and pos_count == 0

    # A permissive bound fills normally.
    result = await execute_trade(
        conn, user_id=1010, ticker=ticker, side="BUY", quantity=5,
        max_slippage=Decimal("1"),
    )
    assert result.quantity == 5


async def test_quote_trade_prices_above_mark_for_buys_below_for_sells(
    conn: AsyncConnection,
) -> None:
    """The estimate replicates the fill path: spread+impact push buys over
    the mark and sells under it; the fee lands on top of the notional."""
    await bootstrap_user(conn, 1011)
    ticker = await _first_ticker(conn)

    buy = await quote_trade(conn, user_id=1011, ticker=ticker, side="BUY", quantity=10)
    sell = await quote_trade(conn, user_id=1011, ticker=ticker, side="SELL", quantity=10)

    assert buy.fill_price > buy.mark_price
    assert sell.fill_price < sell.mark_price
    assert buy.notional_minor == int(buy.fill_price * 10 * 100)
    assert buy.fee_minor > 0
    # BUY unit is the all-in per-share cost; SELL unit is net proceeds.
    assert buy.unit_minor >= int(buy.notional_minor / 10)
    assert sell.unit_minor <= int(sell.notional_minor / 10)


async def test_shares_for_dollars_floors_and_the_trade_executes(
    conn: AsyncConnection,
) -> None:
    """dollar sizing -> whole shares whose estimated all-in cost fits the
    amount, and the resolved quantity fills for real."""
    account_id = (await bootstrap_user(conn, 1012)).account_id
    await _give_cash(conn, account_id, 1_000_000)  # $10,000
    ticker = await _first_ticker(conn)

    dollars = 200_000  # $2,000
    qty = await shares_for_dollars(
        conn, user_id=1012, ticker=ticker, side="BUY", dollars_minor=dollars
    )
    assert qty >= 1
    quote = await quote_trade(
        conn, user_id=1012, ticker=ticker, side="BUY", quantity=qty
    )
    assert quote.notional_minor + quote.fee_minor <= dollars

    balance_before = await get_balance(conn, account_id)
    result = await execute_trade(
        conn, user_id=1012, ticker=ticker, side="BUY", quantity=qty
    )
    assert result.quantity == qty
    spent = balance_before - await get_balance(conn, account_id)
    assert spent <= dollars


async def test_shares_for_dollars_too_small_returns_zero(conn: AsyncConnection) -> None:
    await bootstrap_user(conn, 1013)
    ticker = await _first_ticker(conn)
    qty = await shares_for_dollars(
        conn, user_id=1013, ticker=ticker, side="BUY", dollars_minor=50
    )
    assert qty == 0


async def test_shares_for_dollars_anchor_prices_off_the_limit(
    conn: AsyncConnection,
) -> None:
    """Resting orders size at their anchor (limit/stop), not the mark:
    $500 at a $50 limit buys floor(500/50.05) = 9 shares at the taker
    fee tier."""
    await bootstrap_user(conn, 1014)
    ticker = await _first_ticker(conn)
    qty = await shares_for_dollars(
        conn,
        user_id=1014,
        ticker=ticker,
        side="BUY",
        dollars_minor=50_000,  # $500
        anchor_price=Decimal("50"),
    )
    # 50 * (1 + fee) > 50 -> qty < 10; 50*(1+0.001)=50.05 -> floor(500/50.05)=9
    assert qty == 9


async def test_max_affordable_shares_fits_the_balance(conn: AsyncConnection) -> None:
    """all_in resolves to a size whose quoted all-in cost fits the cash
    balance and then actually executes without tripping funds."""
    account_id = (await bootstrap_user(conn, 1015)).account_id
    await _give_cash(conn, account_id, 300_000)  # $3,000
    ticker = await _first_ticker(conn)

    cash = await get_balance(conn, account_id)
    qty = await max_affordable_shares(
        conn, user_id=1015, ticker=ticker, cash_minor=cash
    )
    assert qty >= 1
    quote = await quote_trade(
        conn, user_id=1015, ticker=ticker, side="BUY", quantity=qty
    )
    assert quote.notional_minor + quote.fee_minor <= cash

    result = await execute_trade(
        conn, user_id=1015, ticker=ticker, side="BUY", quantity=qty
    )
    assert result.notional_minor + result.fee_minor <= cash
