"""Discord bot entrypoint: the gateway + slash command surface."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import sys
from typing import Any

import discord

from stockbot import db
from stockbot.bot.chart_view import CHART_CID_PREFIX, handle_chart_component
from stockbot.bot.collection_view import (
    CID_PREFIX as COLLECTION_CID_PREFIX,
)
from stockbot.bot.collection_view import (
    handle_collection_component,
)
from stockbot.bot.commands import guild_welcome_message, register_commands
from stockbot.bot.feed import ChannelGone
from stockbot.bot.feed import poll_once as feed_poll_once
from stockbot.bot.leaderboard import sync_leaderboard_boards
from stockbot.bot.notify import DeliveryForbidden, poll_once
from stockbot.bot.shop_view import SHOP_CID_PREFIX, handle_shop_component
from stockbot.bot.trade_view import (
    CID_PREFIX as TRADE_CID_PREFIX,
)
from stockbot.bot.trade_view import (
    handle_trade_component,
)
from stockbot.config import get_settings
from stockbot.logging import setup_logging
from stockbot.observability import write_heartbeat

setup_logging("bot")
log = logging.getLogger("stockbot.bot")

HEARTBEAT_INTERVAL_SECONDS = 30
NOTIFY_POLL_INTERVAL_SECONDS = 5
FEED_POLL_INTERVAL_SECONDS = 30
LEADERBOARD_INTERVAL_SECONDS = 15


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
        # H4: cooldowns are expected flow, not errors -- say how long to
        # wait instead of the generic failure line.
        if isinstance(error, discord.app_commands.CommandOnCooldown):
            retry = getattr(error, "retry_after", 0.0)
            try:
                msg = (
                    f"Slow down — `/{command_name}` can be used again in "
                    f"{retry:.0f}s."
                )
                if interaction.response.is_done():
                    await interaction.followup.send(msg, ephemeral=True)
                else:
                    await interaction.response.send_message(msg, ephemeral=True)
            except discord.HTTPException:
                pass
            return
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
        # Slash-command-only surface: no privileged intents. `guilds` is
        # the one standard intent we take -- without it Discord never
        # sends GUILD_CREATE and on_guild_join can't fire (it also makes
        # the heartbeat's guild count real instead of always-zero).
        intents = discord.Intents.none()
        intents.guilds = True
        super().__init__(intents=intents)
        self.tree = StockBotTree(self)
        register_commands(self.tree)
        self._notify_stats: dict[str, int] = {}
        self._feed_stats: dict[str, int] = {}

    async def setup_hook(self) -> None:
        # tree.sync() is a bulk PUT that re-publishes the entire command
        # set: Discord bumps the global command `version` on EVERY call,
        # even a byte-identical one, and clients holding the old version
        # get "This command is outdated" -- the interaction never reaches
        # the gateway. Syncing on every boot therefore breaks slash
        # commands for minutes after each deploy/restart. Hash the
        # serialized command set and only re-publish on a real change.
        payload = [cmd.to_dict(self.tree) for cmd in self.tree.get_commands()]
        digest = int(
            hashlib.sha256(
                json.dumps(payload, sort_keys=True, default=str).encode()
            ).hexdigest()[:20],
            16,
        )
        async with db.connection() as conn, conn.transaction():
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT value FROM config WHERE key = 'bot.command_hash'"
                )
                row = await cur.fetchone()
            if row is not None and int(row[0]) == digest:
                log.info("command set unchanged -- skipping global sync")
                return
            synced = await self.tree.sync()
            async with conn.cursor() as cur:
                await cur.execute(
                    "INSERT INTO config (key, value) "
                    "VALUES ('bot.command_hash', %s) "
                    "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
                    (digest,),
                )
            log.info("synced %d global commands", len(synced))

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
                            "feed": self._feed_stats,
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
        transaction, so a mid-poll crash never loses a row (undelivered
        rows stay pending). Delivery is at-least-once: a crash after a
        successful send but before the commit re-delivers on the next
        poll -- duplicates are rare and harmless, so no dedup is kept."""
        while True:
            try:
                async with db.connection() as conn, conn.transaction():
                    self._notify_stats = await poll_once(conn, self._deliver_dm)
            except Exception:
                log.exception("notification poll failed")
            await asyncio.sleep(NOTIFY_POLL_INTERVAL_SECONDS)

    async def _deliver_feed(self, channel_id: int, message: str) -> None:
        try:
            channel = self.get_channel(channel_id) or await self.fetch_channel(
                channel_id
            )
            if not hasattr(channel, "send"):
                raise ChannelGone(f"channel type can't receive messages: {type(channel).__name__}")
            await channel.send(
                message, allowed_mentions=discord.AllowedMentions.none()
            )
        except discord.Forbidden as exc:
            raise ChannelGone(str(exc)) from exc
        except discord.NotFound as exc:
            raise ChannelGone("channel not found") from exc

    async def _feed_loop(self) -> None:
        """Public-tape poller: 30s is plenty of latency for drama and the
        batching is the point -- a 60s tick's liquidation cascade lands as
        one digest post. Same at-least-once transaction semantics as
        _notify_loop."""
        while True:
            try:
                async with db.connection() as conn, conn.transaction():
                    self._feed_stats = await feed_poll_once(
                        conn, self._deliver_feed
                    )
            except Exception:
                log.exception("feed poll failed")
            await asyncio.sleep(FEED_POLL_INTERVAL_SECONDS)

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
        elif str(data.get("custom_id", "")).startswith(SHOP_CID_PREFIX):
            # Same restart-survival fallback as chart buttons; the shop
            # handler's _INFLIGHT guard dedups the double dispatch.
            try:
                await handle_shop_component(interaction)
            except Exception:
                log.exception("shop component interaction failed")
                try:
                    if interaction.response.is_done():
                        await interaction.followup.send(
                            "Couldn't refresh that shop — run /shop again.",
                            ephemeral=True,
                        )
                    else:
                        await interaction.response.send_message(
                            "Couldn't refresh that shop — run /shop again.",
                            ephemeral=True,
                        )
                except discord.HTTPException:
                    pass
        elif str(data.get("custom_id", "")).startswith(COLLECTION_CID_PREFIX):
            try:
                await handle_collection_component(interaction)
            except Exception:
                log.exception("collection component interaction failed")
                try:
                    if interaction.response.is_done():
                        await interaction.followup.send(
                            "Couldn't turn that page — run /collection again.",
                            ephemeral=True,
                        )
                    else:
                        await interaction.response.send_message(
                            "Couldn't turn that page — run /collection again.",
                            ephemeral=True,
                        )
                except discord.HTTPException:
                    pass
        elif str(data.get("custom_id", "")).startswith(TRADE_CID_PREFIX):
            try:
                await handle_trade_component(interaction)
            except Exception:
                log.exception("trade component interaction failed")
                try:
                    if interaction.response.is_done():
                        await interaction.followup.send(
                            "Couldn't answer that trade — run /trade list.",
                            ephemeral=True,
                        )
                    else:
                        await interaction.response.send_message(
                            "Couldn't answer that trade — run /trade list.",
                            ephemeral=True,
                        )
                except discord.HTTPException:
                    pass

    async def on_guild_join(self, guild: discord.Guild) -> None:
        """Post the tour once wherever we're allowed to speak. Permissions
        are probed by trying (guild.me isn't reliably cached without the
        privileged members intent); cap attempts so a locked-down guild
        costs us a handful of 403s, not a channel sweep."""
        candidates = (
            [guild.system_channel] if guild.system_channel is not None else []
        ) + [c for c in guild.text_channels if c != guild.system_channel]
        for channel in candidates[:3]:
            try:
                await channel.send(guild_welcome_message())
                log.info("posted welcome in guild %s", guild.id)
                return
            except discord.Forbidden:
                continue
            except discord.HTTPException:
                log.warning("welcome post failed in guild %s", guild.id)
                return
        log.info("no writable channel for welcome in guild %s", guild.id)

    async def _leaderboard_loop(self) -> None:
        """Refresh bound leaderboard boards every 15s. Equity can move
        off-tick (trades, claims, ledger transfers), so the poll is
        faster than the 60s tick cadence -- unchanged embeds still skip
        the REST edit via the digest gate in sync_leaderboard_boards."""
        while True:
            try:
                await sync_leaderboard_boards(self)
            except Exception:
                log.exception("leaderboard board sync pass failed")
            await asyncio.sleep(LEADERBOARD_INTERVAL_SECONDS)

    async def on_ready(self) -> None:
        if getattr(self, "_heartbeat_task", None) is None:
            self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        if getattr(self, "_notify_task", None) is None:
            self._notify_task = asyncio.create_task(self._notify_loop())
        if getattr(self, "_feed_task", None) is None:
            self._feed_task = asyncio.create_task(self._feed_loop())
        if getattr(self, "_leaderboard_task", None) is None:
            self._leaderboard_task = asyncio.create_task(self._leaderboard_loop())


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
