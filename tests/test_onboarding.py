"""Phase N3: first-use onboarding — welcome suffix, /start, guild join."""

from __future__ import annotations

import random
from unittest.mock import AsyncMock, MagicMock

import discord

from stockbot import db
from stockbot.accounts.service import STARTING_GRANT, bootstrap_user
from stockbot.bot.commands import (
    _welcome_suffix,
    guild_welcome_message,
)
from stockbot.bot.format import format_money
from stockbot.config import get_settings
from stockbot.ledger.service import get_balance


def _fresh_user() -> int:
    """/start tests bootstrap through the real pool, which commits for
    real -- a fixed snowflake would only be 'new' on the first run ever."""
    return random.SystemRandom().randrange(10**15, 2**62)


def _forbidden_channel() -> MagicMock:
    channel = MagicMock()
    channel.send = AsyncMock(
        side_effect=discord.Forbidden(
            MagicMock(status=403), {"code": 50001, "message": "Missing Access"}
        )
    )
    return channel


def test_welcome_suffix_fires_only_on_creation() -> None:
    assert _welcome_suffix(False) == ""
    msg = _welcome_suffix(True)
    assert "Welcome" in msg
    assert format_money(STARTING_GRANT) in msg
    assert "/start" in msg


def test_guild_welcome_points_at_start() -> None:
    msg = guild_welcome_message()
    assert "/start" in msg
    assert "/market" in msg
    assert format_money(STARTING_GRANT) in msg


async def test_on_guild_join_posts_to_first_writable_channel() -> None:
    from stockbot.bot.main import StockBotClient

    client = StockBotClient()
    denied = _forbidden_channel()
    writable = MagicMock()
    writable.send = AsyncMock()
    guild = MagicMock(spec=discord.Guild)
    guild.id = 4242
    guild.system_channel = denied
    guild.text_channels = [denied, writable]

    await client.on_guild_join(guild)

    writable.send.assert_awaited_once()
    assert "/start" in writable.send.call_args.args[0]


async def test_on_guild_join_silently_skips_locked_down_guild() -> None:
    from stockbot.bot.main import StockBotClient

    client = StockBotClient()
    denied = _forbidden_channel()
    guild = MagicMock(spec=discord.Guild)
    guild.id = 4343
    guild.system_channel = denied
    guild.text_channels = [denied]

    await client.on_guild_join(guild)  # must not raise
    denied.send.assert_awaited()


async def test_start_command_bootstraps_and_sends_tour() -> None:
    """Invoke the real /start callback end-to-end: account created, grant
    committed, one embed response. Uses the global pool pointed at the
    test DB -- the row commits for real (fixed test snowflake)."""
    from stockbot.bot.main import StockBotClient

    await db.init_pool(get_settings().test_database_url, min_size=1, max_size=2)
    try:
        client = StockBotClient()
        cmd = client.tree.get_command("start")
        assert cmd is not None

        user_id = _fresh_user()
        interaction = MagicMock(spec=discord.Interaction)
        interaction.id = 1
        interaction.user = MagicMock()
        interaction.user.id = user_id
        interaction.response = MagicMock()
        interaction.response.send_message = AsyncMock()

        await cmd.callback(interaction)

        interaction.response.send_message.assert_awaited_once()
        embed = interaction.response.send_message.call_args.kwargs["embed"]
        assert isinstance(embed, discord.Embed)
        assert "Welcome" in (embed.title or "")

        async with db.connection() as conn:
            bootstrap = await bootstrap_user(conn, user_id)
            assert not bootstrap.created  # the command created it
            assert (
                await get_balance(conn, bootstrap.account_id) == STARTING_GRANT
            )
    finally:
        await db.close_pool()


async def test_start_command_repeat_shows_quick_start() -> None:
    """Second /start for the same user: no new grant, 'Quick start' title."""
    from stockbot.bot.main import StockBotClient

    await db.init_pool(get_settings().test_database_url, min_size=1, max_size=2)
    try:
        # Ensure the account already exists before /start runs.
        user_id = _fresh_user()
        async with db.connection() as conn, conn.transaction():
            await bootstrap_user(conn, user_id)

        client = StockBotClient()
        cmd = client.tree.get_command("start")
        assert cmd is not None

        interaction = MagicMock(spec=discord.Interaction)
        interaction.id = 2
        interaction.user = MagicMock()
        interaction.user.id = user_id
        interaction.response = MagicMock()
        interaction.response.send_message = AsyncMock()

        await cmd.callback(interaction)

        embed = interaction.response.send_message.call_args.kwargs["embed"]
        assert embed.title == "Quick start"
    finally:
        await db.close_pool()
