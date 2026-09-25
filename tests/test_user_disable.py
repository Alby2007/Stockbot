"""Phase H2: per-user disable -- bootstrap is the chokepoint; market
mechanics (order fills, liquidation sweeps) deliberately bypass it."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from psycopg import AsyncConnection

from stockbot.accounts.errors import UserDisabledError
from stockbot.accounts.service import bootstrap_user
from stockbot.admin.service import disable_user, enable_user, user_info
from stockbot.margin.service import check_and_liquidate, compute_health
from stockbot.orders.service import place_order


def _snowflake() -> int:
    """A >30d-old snowflake so the account is grant-funded."""
    import random
    import time

    rng = random.SystemRandom()
    ts_ms = (
        int(time.time() * 1000)
        - 1_420_070_400_000
        - rng.randrange(31, 4000) * 86_400_000
    )
    return (ts_ms << 22) | rng.randrange(1, 1 << 22)


async def test_disabled_user_cannot_bootstrap(conn: AsyncConnection) -> None:
    user_id = _snowflake()
    await bootstrap_user(conn, user_id)
    assert await disable_user(conn, user_id, "test suspension", 99)
    with pytest.raises(UserDisabledError):
        await bootstrap_user(conn, user_id)
    # Idempotent.
    assert not await disable_user(conn, user_id, "again", 99)
    # Re-enable restores access.
    assert await enable_user(conn, user_id)
    result = await bootstrap_user(conn, user_id)
    assert not result.created  # account survived suspension


async def test_disable_cancels_orders_and_dms(conn: AsyncConnection) -> None:
    user_id = _snowflake()
    await bootstrap_user(conn, user_id)
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT ticker, quoted_price FROM instruments "
            "WHERE is_active AND kind != 'INDEX' ORDER BY ticker LIMIT 1"
        )
        ticker, mark = await cur.fetchone()
    order = await place_order(
        conn,
        user_id=user_id,
        ticker=ticker,
        side="BUY",
        quantity=1,
        limit_price=Decimal(str(mark)) * Decimal("0.5"),
    )

    await disable_user(conn, user_id, "wash trading", 42)

    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT status FROM orders WHERE id = %s", (order.order_id,)
        )
        (status,) = await cur.fetchone()
        assert status == "CANCELLED"
        await cur.execute(
            "SELECT kind, payload FROM notifications WHERE user_id = %s",
            (user_id,),
        )
        kind, payload = await cur.fetchone()
        assert kind == "ACCOUNT_SUSPENDED"
        assert payload["reason"] == "wash trading"
        assert payload["cancelled_orders"] == 1


async def test_disabled_positions_still_liquidate(conn: AsyncConnection) -> None:
    """Disabling blocks the user's commands, not the market's mechanics:
    an undermargined position still gets swept."""
    from stockbot.trading.service import execute_trade

    user_id = _snowflake()
    await bootstrap_user(conn, user_id)
    async with conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO entitlements (user_id, item_key, quantity) "
            "VALUES (%s, 'margin_tier', 1)",
            (user_id,),
        )
        await cur.execute(
            "SELECT ticker, quoted_price FROM instruments "
            "WHERE is_active AND kind = 'STOCK' ORDER BY id LIMIT 1"
        )
        ticker, price = await cur.fetchone()
        await cur.execute(
            "UPDATE instruments SET init_margin_pct = 0.5, "
            "maint_margin_pct = 0.3 WHERE ticker = %s",
            (ticker,),
        )
        await cur.execute(
            "SELECT balance FROM accounts WHERE user_id = %s AND kind = 'USER'",
            (user_id,),
        )
        (cash,) = await cur.fetchone()
    qty = max(1, int(Decimal(cash) * Decimal("1.8") / (Decimal(str(price)) * 100)))
    await execute_trade(conn, user_id=user_id, ticker=ticker, side="SELL", quantity=qty)

    await disable_user(conn, user_id, "suspended mid-position", 42)

    # A gap up against the short breaches maintenance; the sweep must
    # still fire -- bootstrap isn't consulted on this path.
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE instruments SET base_price = base_price * 3, "
            "quoted_price = quoted_price * 3 WHERE ticker = %s",
            (ticker,),
        )
    health = await compute_health(conn, user_id)
    assert health.undermargined
    legs = await check_and_liquidate(conn, user_id)
    assert legs >= 1


async def test_disabled_command_gets_suspended_ephemeral() -> None:
    """_instrument_one catches UserDisabledError for every command --
    one wrapper edit, no per-handler changes."""
    from stockbot import db
    from stockbot.bot.main import StockBotClient
    from stockbot.config import get_settings

    await db.init_pool(get_settings().test_database_url, min_size=1, max_size=2)
    try:
        user_id = _snowflake()
        async with db.connection() as conn, conn.transaction():
            await bootstrap_user(conn, user_id)
            await disable_user(conn, user_id, "audit-flag-7", 1)

        client = StockBotClient()
        cmd = client.tree.get_command("balance")
        interaction = MagicMock(spec=discord.Interaction)
        interaction.id = 1
        interaction.user = MagicMock()
        interaction.user.id = user_id
        interaction.response = MagicMock()
        interaction.response.is_done = MagicMock(return_value=False)
        interaction.response.send_message = AsyncMock()

        await cmd.callback(interaction)

        interaction.response.send_message.assert_awaited_once()
        message = interaction.response.send_message.call_args.args[0]
        assert "suspended" in message
        assert "audit-flag-7" in message  # the admin's reason surfaces
        assert interaction.response.send_message.call_args.kwargs["ephemeral"]
    finally:
        await db.close_pool()


async def test_user_info_triage(conn: AsyncConnection) -> None:
    user_id = _snowflake()
    missing = await user_info(conn, user_id)
    assert not missing.exists

    await bootstrap_user(conn, user_id)
    await disable_user(conn, user_id, "reason here", 77)
    info = await user_info(conn, user_id)
    assert info.exists and info.grant_issued
    assert info.balance_minor == 10_000
    assert info.disabled_at is not None
    assert info.disabled_reason == "reason here"
    assert info.disabled_by == 77
    assert info.discord_age_days > 30
