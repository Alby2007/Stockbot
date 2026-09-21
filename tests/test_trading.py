from __future__ import annotations

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
    UnknownInstrumentError,
)
from stockbot.trading.service import execute_trade


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
    account_id = await bootstrap_user(conn, 1001)
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
    account_id = await bootstrap_user(conn, 1002)
    await _give_cash(conn, account_id, 100_000_000)
    ticker = await _first_ticker(conn)

    async with conn.cursor() as cur:
        await cur.execute("SELECT base_price FROM instruments WHERE ticker = %s", (ticker,))
        (base_price,) = await cur.fetchone()

    result = await execute_trade(conn, user_id=1002, ticker=ticker, side="BUY", quantity=5000)
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
    account_id = await bootstrap_user(conn, 1004)
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
    account_id = await bootstrap_user(conn, 1005)  # only the starting grant, no top-up
    ticker = await _first_ticker(conn)
    balance_before = await get_balance(conn, account_id)

    with pytest.raises(InsufficientFundsError):
        await execute_trade(conn, user_id=1005, ticker=ticker, side="BUY", quantity=1_000_000)

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
    account_id = await bootstrap_user(conn, 1007)
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
    account_id = await bootstrap_user(conn, 1008)
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
    account_id = await bootstrap_user(conn, 1009)
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
