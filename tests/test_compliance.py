from __future__ import annotations

from psycopg import AsyncConnection

from stockbot.accounts.service import bootstrap_user
from stockbot.compliance.wash_trade import scan_for_wash_trades
from stockbot.ledger.service import get_system_account_id, get_user_account_id, post_transfer
from stockbot.market.tick import apply_tick
from stockbot.trading.service import execute_trade


async def _give_cash(conn: AsyncConnection, user_id: int, amount: int) -> None:
    account_id = await get_user_account_id(conn, user_id)
    faucet_id = await get_system_account_id(conn, "FAUCET")
    await post_transfer(
        conn, from_account_id=faucet_id, to_account_id=account_id, amount=amount, reason="TEST"
    )


async def _lowest_liquidity_ticker(conn: AsyncConnection) -> str:
    async with conn.cursor() as cur:
        await cur.execute("SELECT ticker FROM instruments ORDER BY liquidity ASC LIMIT 1")
        (ticker,) = await cur.fetchone()
    return ticker


async def test_same_tick_opposite_trades_on_illiquid_stock_are_flagged(
    conn: AsyncConnection,
) -> None:
    await apply_tick(conn, "wash-trade-test-seed")
    ticker = await _lowest_liquidity_ticker(conn)

    await bootstrap_user(conn, 5001)
    await _give_cash(conn, 5001, 1_000_000)
    await execute_trade(conn, user_id=5001, ticker=ticker, side="BUY", quantity=10)
    await execute_trade(conn, user_id=5001, ticker=ticker, side="SELL", quantity=10)

    await bootstrap_user(conn, 5002)
    await _give_cash(conn, 5002, 1_000_000)
    await execute_trade(conn, user_id=5002, ticker=ticker, side="BUY", quantity=10)

    flags = await scan_for_wash_trades(conn)
    assert any(f.buyer_id == 5002 and f.seller_id == 5001 for f in flags)


async def test_scan_is_idempotent(conn: AsyncConnection) -> None:
    await apply_tick(conn, "wash-trade-test-seed-2")
    ticker = await _lowest_liquidity_ticker(conn)

    await bootstrap_user(conn, 5003)
    await _give_cash(conn, 5003, 1_000_000)
    await execute_trade(conn, user_id=5003, ticker=ticker, side="BUY", quantity=5)
    await execute_trade(conn, user_id=5003, ticker=ticker, side="SELL", quantity=5)

    await bootstrap_user(conn, 5004)
    await _give_cash(conn, 5004, 1_000_000)
    await execute_trade(conn, user_id=5004, ticker=ticker, side="BUY", quantity=5)

    first_pass = await scan_for_wash_trades(conn)
    second_pass = await scan_for_wash_trades(conn)
    assert len(first_pass) > 0
    assert second_pass == []  # already flagged, not re-reported


async def test_same_account_round_trip_is_not_flagged_as_a_pair(conn: AsyncConnection) -> None:
    await apply_tick(conn, "wash-trade-test-seed-3")
    ticker = await _lowest_liquidity_ticker(conn)

    await bootstrap_user(conn, 5005)
    await _give_cash(conn, 5005, 1_000_000)
    await execute_trade(conn, user_id=5005, ticker=ticker, side="BUY", quantity=5)
    await execute_trade(conn, user_id=5005, ticker=ticker, side="SELL", quantity=5)

    flags = await scan_for_wash_trades(conn)
    assert flags == []
