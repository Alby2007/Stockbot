"""Discord bot entrypoint: the gateway + slash command surface."""

from __future__ import annotations

import asyncio
import logging
import sys
from typing import Any

import discord

from stockbot import db
from stockbot.bot.chart_view import CHART_CID_PREFIX, handle_chart_component
from stockbot.bot.commands import register_commands
from stockbot.bot.notify import DeliveryForbidden, poll_once
from stockbot.config import get_settings
from stockbot.logging import setup_logging
from stockbot.observability import write_heartbeat

setup_logging("bot")
log = logging.getLogger("stockbot.bot")

HEARTBEAT_INTERVAL_SECONDS = 30
NOTIFY_POLL_INTERVAL_SECONDS = 5


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
        self._notify_stats: dict[str, int] = {}

    async def setup_hook(self) -> None:
        await self.tree.sync()

    async def _heartbeat_loop(self) -> None:
        """Liveness signal: 'bot is running' means 'heartbeat younger than
        ~2 intervals' -- including gateway latency so a dead gateway shows
        up even while the process looks fine in docker ps."""
        while True:
            try:
                async with db.connection() as conn, conn.transaction():
                    await write_heartbeat(
                        conn,
                        "bot",
                        {
                            "gateway_latency_s": round(self.latency, 3),
                            "guilds": len(self.guilds),
                            "notify": self._notify_stats,
                        },
                    )
            except Exception:
                log.exception("heartbeat write failed")
            await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)

    async def _deliver_dm(self, user_id: int, message: str) -> None:
        try:
            user = self.get_user(user_id) or await self.fetch_user(user_id)
            await user.send(message)
        except discord.Forbidden as exc:
            raise DeliveryForbidden(str(exc)) from exc
        except discord.NotFound:
            # Deleted/unreachable Discord account -- same treatment as
            # Forbidden, there's no one to ever deliver this to.
            raise DeliveryForbidden("user not found") from None

    async def _notify_loop(self) -> None:
        """Transactional-outbox poller: one poll = one committed
        transaction, so a mid-poll crash never double-sends (unsent rows
        just stay unsent) or double-marks (sent rows are already
        committed sent before the next poll can see them)."""
        while True:
            try:
                async with db.connection() as conn, conn.transaction():
                    self._notify_stats = await poll_once(conn, self._deliver_dm)
            except Exception:
                log.exception("notification poll failed")
            await asyncio.sleep(NOTIFY_POLL_INTERVAL_SECONDS)

    async def on_interaction(self, interaction: discord.Interaction) -> None:
        # Post-restart fallback for chart buttons: live Views get the click
        # first (their callback responds), so by the time this fires the
        # response is already consumed -- handle_chart_component's is_done()
        # guard turns the double dispatch into a no-op. After a restart the
        # View is gone and this is the only path.
        data: Any = interaction.data or {}
        if str(data.get("custom_id", "")).startswith(CHART_CID_PREFIX):
            try:
                await handle_chart_component(interaction)
            except Exception:
                log.exception("chart component interaction failed")
                try:
                    if interaction.response.is_done():
                        await interaction.followup.send(
                            "Couldn't refresh that chart — run /chart again.",
                            ephemeral=True,
                        )
                    else:
                        await interaction.response.send_message(
                            "Couldn't refresh that chart — run /chart again.",
                            ephemeral=True,
                        )
                except discord.HTTPException:
                    pass

    async def on_ready(self) -> None:
        if getattr(self, "_heartbeat_task", None) is None:
            self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        if getattr(self, "_notify_task", None) is None:
            self._notify_task = asyncio.create_task(self._notify_loop())


async def run() -> None:
    settings = get_settings()
    await db.init_pool(settings.database_url, min_size=1, max_size=10)
    try:
        if not settings.discord_token:
            log.warning("DISCORD_TOKEN not set; idling instead of connecting to Discord")
            while True:
                # Still heartbeat while idling: the service is up, just
                # unconfigured -- /admin health should show it alive.
                async with db.connection() as conn, conn.transaction():
                    await write_heartbeat(conn, "bot", {"status": "idle_no_token"})
                await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)
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
