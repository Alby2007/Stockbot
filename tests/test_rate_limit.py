"""Phase H4: /chart cooldown mapping + the wrapper's global throttle."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import discord
from discord import app_commands

from stockbot import db
from stockbot.bot.commands import _THROTTLE_RATE, _instrument_one, _throttle
from stockbot.config import get_settings


def _interaction(user_id: int) -> MagicMock:
    interaction = MagicMock(spec=discord.Interaction)
    interaction.id = 1
    interaction.user = MagicMock()
    interaction.user.id = user_id
    interaction.response = MagicMock()
    interaction.response.is_done = MagicMock(return_value=False)
    interaction.response.send_message = AsyncMock()
    interaction.followup = MagicMock()
    interaction.followup.send = AsyncMock()
    return interaction


async def test_on_error_maps_cooldown_to_friendly_reply() -> None:
    from stockbot.bot.main import StockBotClient

    client = StockBotClient()
    interaction = _interaction(777)
    interaction.command = MagicMock()
    interaction.command.name = "chart"
    error = MagicMock(spec=app_commands.CommandOnCooldown)
    error.retry_after = 7.0

    await client.tree.on_error(interaction, error)

    interaction.response.send_message.assert_awaited_once()
    msg = interaction.response.send_message.call_args.args[0]
    assert "Slow down" in msg and "7s" in msg
    assert interaction.response.send_message.call_args.kwargs["ephemeral"]


async def test_global_throttle_trips_at_burst_limit() -> None:
    """_instrument_one's per-user throttle: N commands per window, then
    an ephemeral 'slow down' -- cheap spam defense for every command."""
    await db.init_pool(get_settings().test_database_url, min_size=1, max_size=2)
    try:
        user_id = 424_242_424
        _throttle.pop(user_id, None)
        calls = 0

        async def real_handler(interaction: discord.Interaction) -> None:
            nonlocal calls
            calls += 1
            await interaction.response.send_message("ok")

        cmd = app_commands.Command(
            name="ping", description="x", callback=real_handler
        )
        _instrument_one(cmd)
        interaction = _interaction(user_id)

        for _ in range(_THROTTLE_RATE):
            await cmd.callback(interaction)
        assert calls == _THROTTLE_RATE
        assert interaction.response.send_message.call_count == _THROTTLE_RATE

        # The next one is throttled: handler NOT invoked.
        await cmd.callback(interaction)
        assert calls == _THROTTLE_RATE
        last = interaction.response.send_message.call_args
        assert "Slow down" in last.args[0]

        # An emptied window frees the user (equivalent to expiry).
        _throttle[user_id].clear()
        await cmd.callback(interaction)
        assert calls == _THROTTLE_RATE + 1
    finally:
        _throttle.pop(user_id, None)
        await db.close_pool()
