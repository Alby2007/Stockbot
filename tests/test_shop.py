from __future__ import annotations

import pytest
from psycopg import AsyncConnection

from stockbot.accounts.service import bootstrap_user
from stockbot.ledger.errors import InsufficientFundsError
from stockbot.ledger.service import get_balance
from stockbot.shop.errors import AlreadyOwnedError, UnknownItemError
from stockbot.shop.service import BASE_SLOTS, buy_item, get_slot_count, get_user_entitlements
from stockbot.trading.errors import TooManyPositionsError
from stockbot.trading.service import execute_trade


async def _give_cash(conn: AsyncConnection, user_id: int, amount: int) -> None:
    from stockbot.ledger.service import get_system_account_id, get_user_account_id, post_transfer

    account_id = await get_user_account_id(conn, user_id)
    faucet_id = await get_system_account_id(conn, "FAUCET")
    await post_transfer(
        conn, from_account_id=faucet_id, to_account_id=account_id, amount=amount, reason="TEST"
    )


async def test_default_slot_count_is_base(conn: AsyncConnection) -> None:
    await bootstrap_user(conn, 3001)
    assert await get_slot_count(conn, 3001) == BASE_SLOTS


async def test_buying_a_slot_increases_slot_count_and_costs_money(conn: AsyncConnection) -> None:
    account_id = await bootstrap_user(conn, 3002)
    await _give_cash(conn, 3002, 100_000)
    balance_before = await get_balance(conn, account_id)

    price = await buy_item(conn, 3002, "slot")

    assert await get_slot_count(conn, 3002) == BASE_SLOTS + 1
    assert await get_balance(conn, account_id) == balance_before - price


async def test_slot_price_escalates(conn: AsyncConnection) -> None:
    await bootstrap_user(conn, 3003)
    await _give_cash(conn, 3003, 1_000_000)
    first = await buy_item(conn, 3003, "slot")
    second = await buy_item(conn, 3003, "slot")
    assert second > first


async def test_permanent_item_cannot_be_bought_twice(conn: AsyncConnection) -> None:
    await bootstrap_user(conn, 3004)
    await _give_cash(conn, 3004, 1_000_000)
    await buy_item(conn, 3004, "theme_sunrise")
    with pytest.raises(AlreadyOwnedError):
        await buy_item(conn, 3004, "theme_sunrise")


async def test_recurring_item_can_be_renewed(conn: AsyncConnection) -> None:
    await bootstrap_user(conn, 3005)
    await _give_cash(conn, 3005, 1_000_000)
    await buy_item(conn, 3005, "analyst_tools")
    await buy_item(conn, 3005, "analyst_tools")  # should not raise

    entitlements = await get_user_entitlements(conn, 3005)
    assert any(e["item_key"] == "analyst_tools" for e in entitlements)


async def test_unknown_item_raises(conn: AsyncConnection) -> None:
    await bootstrap_user(conn, 3006)
    with pytest.raises(UnknownItemError):
        await buy_item(conn, 3006, "does_not_exist")


async def test_insufficient_funds_for_shop_purchase(conn: AsyncConnection) -> None:
    await bootstrap_user(conn, 3007)  # only the starting grant
    with pytest.raises(InsufficientFundsError):
        await buy_item(conn, 3007, "slot")
        await buy_item(conn, 3007, "slot")
        await buy_item(conn, 3007, "slot")
        await buy_item(conn, 3007, "slot")
        await buy_item(conn, 3007, "slot")
        await buy_item(conn, 3007, "slot")
        await buy_item(conn, 3007, "slot")
        await buy_item(conn, 3007, "slot")


async def test_buying_beyond_slot_count_is_rejected(conn: AsyncConnection) -> None:
    account_id = await bootstrap_user(conn, 3008)
    await _give_cash(conn, 3008, 10_000_000)

    async with conn.cursor() as cur:
        await cur.execute("SELECT ticker FROM instruments ORDER BY id LIMIT %s", (BASE_SLOTS + 1,))
        tickers = [row[0] for row in await cur.fetchall()]

    for ticker in tickers[:BASE_SLOTS]:
        await execute_trade(conn, user_id=3008, ticker=ticker, side="BUY", quantity=1)

    with pytest.raises(TooManyPositionsError):
        await execute_trade(conn, user_id=3008, ticker=tickers[BASE_SLOTS], side="BUY", quantity=1)

    # Buying a slot should unblock the next position.
    await buy_item(conn, 3008, "slot")
    await execute_trade(conn, user_id=3008, ticker=tickers[BASE_SLOTS], side="BUY", quantity=1)
    assert account_id is not None
