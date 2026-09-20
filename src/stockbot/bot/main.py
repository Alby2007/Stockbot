"""Discord bot entrypoint.

Phase 0 placeholder: no slash commands yet (Phase 1). This just wires up the
DB pool and a bare discord.py client so the process topology (two services,
one database) is real from the start.
"""

from __future__ import annotations

import asyncio
import logging
import sys

import discord

from stockbot import db
from stockbot.config import get_settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s bot %(levelname)s %(message)s")
log = logging.getLogger("stockbot.bot")


class StockBotClient(discord.Client):
    def __init__(self) -> None:
        # Slash-command-only surface: no privileged intents required.
        super().__init__(intents=discord.Intents.none())
        self.tree = discord.app_commands.CommandTree(self)

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
