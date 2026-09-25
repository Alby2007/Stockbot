"""Phase N4: explicit oversell opt-in — orders.allow_short, /sell short
flag, dollar-formatted margin errors."""

from __future__ import annotations

import random
import time
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import discord
from psycopg import AsyncConnection

from stockbot import db
from stockbot.accounts.service import bootstrap_user
from stockbot.config import get_settings
from stockbot.ledger.service import get_system_account_id, post_transfer
from stockbot.margin.errors import (
    InsufficientMarginError,
    MarginSpendBlockedError,
    PositionLimitError,
)
from stockbot.market.tick import apply_tick
from stockbot.orders.service import place_order
from stockbot.trading.service import execute_trade

SEED = "oversell-test-seed"


def _fresh_user() -> int:
    """Command-level tests run through the real pool and commit; a fixed
    snowflake would only be 'new' on the first run ever. Anchored >30d
    in the past so the H1 grant gate doesn't leave the account unfunded."""
    rng = random.SystemRandom()
    age_ms = rng.randrange(31, 4000) * 86_400_000
    ts_ms = int(time.time() * 1000) - 1_420_070_400_000 - age_ms
    return (ts_ms << 22) | rng.randrange(1, 1 << 22)


async def _first_ticker(conn: AsyncConnection) -> str:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT ticker FROM instruments WHERE is_active AND kind != 'INDEX' "
            "ORDER BY ticker LIMIT 1"
        )
        row = await cur.fetchone()
        assert row is not None
        return str(row[0])


async def _give_cash(conn: AsyncConnection, account_id: int, amount: int) -> None:
    faucet = await get_system_account_id(conn, "FAUCET")
    await post_transfer(
        conn, from_account_id=faucet, to_account_id=account_id,
        amount=amount, reason="TEST_TOPUP",
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


async def _order_row(conn: AsyncConnection, order_id: int):
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT status, filled_quantity FROM orders WHERE id = %s",
            (order_id,),
        )
        row = await cur.fetchone()
        assert row is not None
        return row[0], int(row[1])


def test_margin_errors_show_dollars_not_minor_units() -> None:
    """N4.1: 'equity 123456 < required 200000' meant nothing to a user."""
    msg = str(InsufficientMarginError(123456, 200000))
    assert "$1,234.56" in msg and "$2,000.00" in msg
    assert "123456" not in msg
    assert "$1,234.56" in str(MarginSpendBlockedError(123456, 200000))
    assert "$1,234.56" in str(PositionLimitError(123456, 200000))


async def test_resting_oversell_stays_open_without_allow_short(
    conn: AsyncConnection,
) -> None:
    """A SELL resting past the position can't short by accident: the
    unbacked part just stays OPEN across ticks."""
    account_id = (await bootstrap_user(conn, 6101)).account_id
    await _give_cash(conn, account_id, 1_000_000)
    ticker = await _first_ticker(conn)
    await execute_trade(
        conn, user_id=6101, ticker=ticker, side="BUY", quantity=2
    )

    order = await place_order(
        conn,
        user_id=6101,
        ticker=ticker,
        side="SELL",
        quantity=5,
        limit_price=Decimal("0.01"),  # deep through the mark: always fills
    )
    await apply_tick(conn, SEED)

    status, filled = await _order_row(conn, order.order_id)
    # The backed 2 shares filled; the other 3 rest forever rather than
    # short. (No margin tier granted -- the fill path never got near
    # MarginNotUnlockedError.)
    assert status == "OPEN"
    assert filled == 2
    assert await _position_qty(conn, 6101, ticker) == 0


async def test_resting_oversell_with_allow_short_shorts(
    conn: AsyncConnection,
) -> None:
    """The same order with allow_short=True reaches the margin gates and
    fills through the position floor."""
    account_id = (await bootstrap_user(conn, 6102)).account_id
    await _give_cash(conn, account_id, 1_000_000)
    await _grant_tier(conn, 6102)
    ticker = await _first_ticker(conn)

    order = await place_order(
        conn,
        user_id=6102,
        ticker=ticker,
        side="SELL",
        quantity=3,
        limit_price=Decimal("0.01"),
        allow_short=True,
    )
    await apply_tick(conn, SEED)

    status, filled = await _order_row(conn, order.order_id)
    assert status == "FILLED" and filled == 3
    assert await _position_qty(conn, 6102, ticker) == -3


async def test_resting_oversell_allow_short_still_needs_tier(
    conn: AsyncConnection,
) -> None:
    """allow_short opens the gate, not the margin checks -- a tier-0
    user's order just can't fill (stays OPEN, not cancelled)."""
    account_id = (await bootstrap_user(conn, 6103)).account_id
    await _give_cash(conn, account_id, 1_000_000)
    ticker = await _first_ticker(conn)

    order = await place_order(
        conn,
        user_id=6103,
        ticker=ticker,
        side="SELL",
        quantity=2,
        limit_price=Decimal("0.01"),
        allow_short=True,
    )
    await apply_tick(conn, SEED)

    status, _ = await _order_row(conn, order.order_id)
    assert status == "OPEN"
    assert await _position_qty(conn, 6103, ticker) == 0


def _mock_interaction(user_id: int) -> MagicMock:
    interaction = MagicMock(spec=discord.Interaction)
    interaction.id = random.SystemRandom().randrange(1, 2**62)
    interaction.user = MagicMock()
    interaction.user.id = user_id
    interaction.user.display_name = f"u{user_id}"
    interaction.response = MagicMock()
    interaction.response.send_message = AsyncMock()
    return interaction


async def test_sell_oversell_rejected_without_short_flag() -> None:
    """N4.2 command side: /sell quantity>N without short:True stops at a
    holdings-aware error before execute_trade ever runs."""
    from stockbot.bot.main import StockBotClient

    await db.init_pool(get_settings().test_database_url, min_size=1, max_size=2)
    try:
        user_id = _fresh_user()
        async with db.connection() as conn, conn.transaction():
            bootstrap = await bootstrap_user(conn, user_id)
            await _give_cash(conn, bootstrap.account_id, 1_000_000)
            ticker = await _first_ticker(conn)
            await execute_trade(
                conn, user_id=user_id, ticker=ticker, side="BUY", quantity=2
            )

        client = StockBotClient()
        cmd = client.tree.get_command("sell")
        assert cmd is not None
        interaction = _mock_interaction(user_id)
        await cmd.callback(interaction, ticker=ticker, quantity=5)

        interaction.response.send_message.assert_awaited_once()
        msg = interaction.response.send_message.call_args.args[0]
        assert "2" in msg and "short: True" in msg
        assert interaction.response.send_message.call_args.kwargs["ephemeral"]
        async with db.connection() as conn:
            assert await _position_qty(conn, user_id, ticker) == 2
    finally:
        await db.close_pool()


async def test_sell_oversell_with_short_flag_hits_margin_gate() -> None:
    """With short:True a tier-0 user reaches the margin gate and gets the
    MarginNotUnlockedError message -- not a holdings complaint."""
    from stockbot.bot.main import StockBotClient

    await db.init_pool(get_settings().test_database_url, min_size=1, max_size=2)
    try:
        user_id = _fresh_user()
        async with db.connection() as conn, conn.transaction():
            bootstrap = await bootstrap_user(conn, user_id)
            await _give_cash(conn, bootstrap.account_id, 1_000_000)
            ticker = await _first_ticker(conn)

        client = StockBotClient()
        cmd = client.tree.get_command("sell")
        assert cmd is not None
        interaction = _mock_interaction(user_id)
        await cmd.callback(interaction, ticker=ticker, quantity=2, short=True)

        msg = interaction.response.send_message.call_args.args[0]
        assert "margin tier" in msg
        async with db.connection() as conn:
            assert await _position_qty(conn, user_id, ticker) == 0
    finally:
        await db.close_pool()
