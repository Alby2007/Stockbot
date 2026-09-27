"""Persistent leaderboard boards: channels bound via /leaderboard-setup
get one bot-owned message edited in place by the leaderboard loop.

`sync_leaderboard_boards` keeps Discord calls and DB writes separated:
one pass reads the bindings + ranking up front, then edits/posts/pins
each board with per-channel error isolation (a deleted or locked-down
channel skips instead of killing the pass).
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import discord
from psycopg import AsyncConnection

from stockbot import db
from stockbot.bot.format import format_money
from stockbot.status import service as status_svc
from stockbot.status.service import LeaderboardRow

log = logging.getLogger("stockbot.bot.leaderboard")

BOARD_LIMIT = 15

# Per-guild content hashes -- unchanged boards skip the REST edit entirely
# (overnight the ranking is flat and edit spam just burns rate limit).
_last_hashes: dict[int, str] = {}


@dataclass(frozen=True)
class BoardBinding:
    guild_id: int
    channel_id: int
    message_id: int | None


def leaderboard_embed(
    rows: list[LeaderboardRow],
    *,
    limit: int = BOARD_LIMIT,
    caller: LeaderboardRow | None = None,
    flair: dict[int, str] | None = None,
) -> discord.Embed:
    """Shared renderer for `/leaderboard` and the persistent boards --
    `rank. <@uid> · title  $equity` lines plus an `Updated HH:MM UTC`
    footer so the board's freshness is legible. `caller` appends a
    "You: rank N" note for the command surface (boards pass None);
    `flair` is {user_id: equipped-title string}."""
    embed = discord.Embed(title="Leaderboard — net worth")
    if rows:
        embed.description = "\n".join(
            f"{r.rank:>3}. <@{r.user_id}>"
            + (f" · {flair[r.user_id]}" if flair and r.user_id in flair else "")
            + f"  {format_money(r.equity_minor):>14}"
            for r in rows[:limit]
        )
    else:
        embed.description = "No accounts yet — `/start` to join the market."
    stamp = datetime.now(UTC).strftime("%H:%M")
    footer = f"Updated {stamp} UTC"
    if caller is not None and caller.rank > limit:
        footer = (
            f"You: rank {caller.rank} of {caller.total} — "
            f"{format_money(caller.equity_minor)} · {footer}"
        )
    embed.set_footer(text=footer)
    return embed


async def bind_leaderboard_channel(
    conn: AsyncConnection, guild_id: int, channel_id: int
) -> None:
    """Upsert the binding and clear `message_id` so the next sync posts a
    fresh board rather than editing whatever used to be there."""
    async with conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO leaderboard_channels (guild_id, channel_id) "
            "VALUES (%s, %s) "
            "ON CONFLICT (guild_id) DO UPDATE SET "
            "channel_id = EXCLUDED.channel_id, "
            "message_id = NULL, "
            "updated_at = now()",
            (guild_id, channel_id),
        )


async def _fetch_bindings(conn: AsyncConnection) -> list[BoardBinding]:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT guild_id, channel_id, message_id FROM leaderboard_channels"
        )
        return [
            BoardBinding(
                guild_id=int(r[0]),
                channel_id=int(r[1]),
                message_id=None if r[2] is None else int(r[2]),
            )
            for r in await cur.fetchall()
        ]


async def _store_message_id(
    conn: AsyncConnection, guild_id: int, message_id: int
) -> None:
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE leaderboard_channels "
            "SET message_id = %s, updated_at = now() WHERE guild_id = %s",
            (message_id, guild_id),
        )


async def post_leaderboard_board(
    conn: AsyncConnection, guild_id: int, channel: Any
) -> Any:
    """Post a fresh board to `channel`, record its message id, and try to
    pin it (pinning is best-effort). Used by /leaderboard-setup so the
    board lands the moment the channel is bound."""
    rows = await status_svc.leaderboard(conn)
    flair = await status_svc.equipped_flair_map(
        conn, [r.user_id for r in rows[:BOARD_LIMIT]]
    )
    message = await channel.send(embed=leaderboard_embed(rows, flair=flair))
    await _store_message_id(conn, guild_id, message.id)
    try:
        await message.pin()
    except discord.HTTPException:
        log.info("could not pin leaderboard board in %s", channel.id)
    return message


async def _fetch_channel(client: Any, channel_id: int) -> Any | None:
    channel = client.get_channel(channel_id)
    if channel is None:
        channel = await client.fetch_channel(channel_id)
    return channel


async def sync_leaderboard_boards(client: Any) -> None:
    """One refresh pass over every bound channel. Discord calls are
    per-channel try/excepted: a deleted channel/message or a perms
    regression logs and skips, never kills the pass. `message_id` writes
    commit per binding -- a later channel's failure must not roll back an
    earlier board's stored id (that would double-post next pass)."""
    async with db.connection() as conn, conn.transaction():
        bindings = await _fetch_bindings(conn)
        if not bindings:
            return
        rows = await status_svc.leaderboard(conn)
        flair = await status_svc.equipped_flair_map(
            conn, [r.user_id for r in rows[:BOARD_LIMIT]]
        )
    embed = leaderboard_embed(rows, flair=flair)
    # Hash the ranking + flair, not the embed -- the `Updated HH:MM`
    # footer would change every minute and make the skip worthless. Flair
    # belongs in the digest: equipping a title must repaint the board.
    digest = hashlib.sha256(
        json.dumps(
            [
                (r.user_id, r.equity_minor, flair.get(r.user_id))
                for r in rows[:BOARD_LIMIT]
            ]
        ).encode()
    ).hexdigest()

    for binding in bindings:
        try:
            async with db.connection() as conn, conn.transaction():
                await _sync_one(client, conn, binding, embed, digest)
        except Exception:
            log.exception(
                "leaderboard board sync failed for channel %s",
                binding.channel_id,
            )


async def _sync_one(
    client: Any,
    conn: AsyncConnection,
    binding: BoardBinding,
    embed: discord.Embed,
    digest: str,
) -> None:
    channel = await _fetch_channel(client, binding.channel_id)
    if channel is None:
        log.warning("leaderboard channel %s not found", binding.channel_id)
        return

    if binding.message_id is not None:
        if _last_hashes.get(binding.guild_id) == digest:
            return  # unchanged -- don't burn a REST call
        try:
            message = await channel.fetch_message(binding.message_id)
        except discord.NotFound:
            message = None  # board deleted -- repost below
        if message is not None:
            await message.edit(embed=embed)
            _last_hashes[binding.guild_id] = digest
            return

    message = await channel.send(embed=embed)
    await _store_message_id(conn, binding.guild_id, message.id)
    _last_hashes[binding.guild_id] = digest
    try:
        await message.pin()
    except discord.HTTPException:
        log.info("could not pin leaderboard board in %s", binding.channel_id)
