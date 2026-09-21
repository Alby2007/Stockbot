"""Discord bot entrypoint: the gateway + slash command surface."""

from __future__ import annotations

import asyncio
import logging
import sys

import discord

from stockbot import db
from stockbot.bot.commands import register_commands
from stockbot.config import get_settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s bot %(levelname)s %(message)s")
log = logging.getLogger("stockbot.bot")


class StockBotTree(discord.app_commands.CommandTree["StockBotClient"]):
    """Last-resort error path: without it, an unexpected exception in a
    command handler leaves the user staring at "the application did not
    respond" with only a log line."""

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: discord.app_commands.AppCommandError,
    ) -> None:
        command_name = interaction.command.name if interaction.command else "unknown"
        log.error("unhandled error in /%s", command_name, exc_info=error)
        try:
            if interaction.response.is_done():
                await interaction.followup.send(
                    "Something went wrong running that command.", ephemeral=True
                )
            else:
                await interaction.response.send_message(
                    "Something went wrong running that command.", ephemeral=True
                )
        except discord.HTTPException:
            pass  # interaction already expired/ack'd -- nothing more to do


class StockBotClient(discord.Client):
    def __init__(self) -> None:
        # Slash-command-only surface: no privileged intents required.
        super().__init__(intents=discord.Intents.none())
        self.tree = StockBotTree(self)
        register_commands(self.tree)

    async def setup_hook(self) -> None:
        await self.tree.sync()


async def run() -> None:
    settings = get_settings()
    await db.init_pool(settings.database_url, min_size=1, max_size=10)
    try:
        if not settings.discord_token:
            log.warning("DISCORD_TOKEN not set; idling instead of connecting to Discord")
            while True:
                await asyncio.sleep(3600)
        client = StockBotClient()
        await client.start(settings.discord_token)
    finally:
        await db.close_pool()


def main() -> None:
    # psycopg's async mode needs a selector-based loop; Windows defaults to
    # ProactorEventLoop. The production image runs on Linux, where this is a
    # no-op, but it keeps `python -m stockbot.bot.main` usable on Windows too.
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(run())


if __name__ == "__main__":
    main()
