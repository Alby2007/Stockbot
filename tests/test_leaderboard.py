"""Persistent leaderboard boards: channel bindings, the shared board
embed, and the sync pass's edit/repost/error paths (duck-typed fakes --
no Discord needed)."""

from __future__ import annotations

from unittest.mock import MagicMock

import discord
import pytest
from psycopg import AsyncConnection

from stockbot import db
from stockbot.bot import leaderboard as lb
from stockbot.bot.leaderboard import (
    bind_leaderboard_channel,
    fetch_binding,
    leaderboard_embed,
    sync_leaderboard_boards,
)
from stockbot.config import get_settings
from stockbot.status.service import LeaderboardRow

_G = 9_000_000_000  # test guild ids -- far from any real snowflake


def _row(user_id: int, equity_minor: int, rank: int, total: int) -> LeaderboardRow:
    return LeaderboardRow(
        user_id=user_id, equity_minor=equity_minor, rank=rank, total=total
    )


async def test_bind_upserts_and_resets_message_id(conn: AsyncConnection) -> None:
    await bind_leaderboard_channel(conn, _G + 1, 111)
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE leaderboard_channels SET message_id = 42 "
            "WHERE guild_id = %s",
            (_G + 1,),
        )
    await bind_leaderboard_channel(conn, _G + 1, 222)  # rebind elsewhere
    binding = await fetch_binding(conn, _G + 1)
    assert binding is not None
    assert binding.channel_id == 222
    assert binding.message_id is None  # forces a fresh board post
    assert await fetch_binding(conn, _G + 999) is None  # unbound guild


def test_leaderboard_embed_ranks_and_footer() -> None:
    rows = [_row(101 + i, 10_000_000 - i * 500, i + 1, 20) for i in range(3)]
    embed = leaderboard_embed(rows)
    assert embed.title == "Leaderboard — net worth"
    desc = embed.description or ""
    assert "<@101>" in desc and "<@103>" in desc
    lines = desc.splitlines()
    assert lines[0].startswith("🥇")
    assert "$100,000.00" in lines[0]
    assert "Updated" in (embed.footer.text or "")


def test_leaderboard_embed_medals_and_gold() -> None:
    rows = [_row(101 + i, 10_000_000 - i * 500, i + 1, 20) for i in range(5)]
    embed = leaderboard_embed(rows)
    assert embed.color is not None and embed.color.value == 0xF1C40F
    lines = (embed.description or "").splitlines()
    assert lines[0].startswith("🥇")
    assert lines[1].startswith("🥈")
    assert lines[2].startswith("🥉")
    # Rank 4+ stays numeric -- medals are podium-only.
    assert lines[3].startswith("  4.")


def test_leaderboard_embed_empty_board() -> None:
    embed = leaderboard_embed([])
    assert "No accounts yet" in (embed.description or "")


def test_leaderboard_embed_caller_rank_footer() -> None:
    rows = [_row(101 + i, 100_000 - i, i + 1, 40) for i in range(40)]
    caller = rows[39]  # rank 40 > 15 -- off the board
    embed = leaderboard_embed(rows, caller=caller)
    footer = embed.footer.text or ""
    assert "You: rank 40 of 40" in footer
    assert "Updated" in footer
    # On-board callers get no extra footer line.
    embed2 = leaderboard_embed(rows, caller=rows[0])
    assert "You:" not in (embed2.footer.text or "")


# --- sync pass against fakes -------------------------------------------


class _FakeMessage:
    def __init__(self, message_id: int) -> None:
        self.id = message_id
        self.edits: list[object] = []
        self.pinned = False
        self.deleted = False

    async def edit(self, *, embed: object = None) -> None:
        self.edits.append(embed)

    async def pin(self) -> None:
        self.pinned = True

    async def delete(self) -> None:
        self.deleted = True


class _FakeChannel:
    def __init__(
        self,
        channel_id: int,
        existing: dict[int, _FakeMessage] | None = None,
        send_error: Exception | None = None,
    ) -> None:
        self.id = channel_id
        self.sent: list[_FakeMessage] = []
        self._existing = existing or {}
        self._send_error = send_error
        self._next_id = 7000

    async def fetch_message(self, message_id: int) -> _FakeMessage:
        if message_id in self._existing:
            return self._existing[message_id]
        raise discord.NotFound(MagicMock(), "unknown message")

    async def send(self, *, embed: object = None) -> _FakeMessage:
        if self._send_error is not None:
            raise self._send_error
        msg = _FakeMessage(self._next_id)
        self._next_id += 1
        self.sent.append(msg)
        self._existing[msg.id] = msg
        return msg


class _FakeClient:
    def __init__(self, channels: dict[int, _FakeChannel]) -> None:
        self._channels = channels

    def get_channel(self, channel_id: int) -> _FakeChannel | None:
        return self._channels.get(channel_id)

    async def fetch_channel(self, channel_id: int) -> _FakeChannel:
        channel = self._channels.get(channel_id)
        if channel is None:
            raise discord.NotFound(MagicMock(), "unknown channel")
        return channel


@pytest.fixture(autouse=True)
def _reset_hashes() -> None:
    lb._last_hashes.clear()


async def _bind_committed(guild_id: int, channel_id: int) -> None:
    """sync_leaderboard_boards reads via its own pooled connections --
    bindings under test must be committed, then cleaned up."""
    async with db.connection() as conn, conn.transaction():
        await bind_leaderboard_channel(conn, guild_id, channel_id)


async def _drop_binding(guild_id: int) -> None:
    async with db.connection() as conn, conn.transaction():
        async with conn.cursor() as cur:
            await cur.execute(
                "DELETE FROM leaderboard_channels WHERE guild_id = %s",
                (guild_id,),
            )


async def _binding_message_id(guild_id: int) -> int | None:
    async with db.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT message_id FROM leaderboard_channels WHERE guild_id = %s",
                (guild_id,),
            )
            row = await cur.fetchone()
    return None if row is None or row[0] is None else int(row[0])


async def test_sync_posts_and_pins_new_board() -> None:
    await db.init_pool(get_settings().test_database_url, min_size=1, max_size=2)
    guild_id = _G + 11
    try:
        channel = _FakeChannel(555)
        await _bind_committed(guild_id, 555)
        await sync_leaderboard_boards(_FakeClient({555: channel}))
        assert len(channel.sent) == 1
        assert channel.sent[0].pinned is True
        assert await _binding_message_id(guild_id) == channel.sent[0].id
    finally:
        await _drop_binding(guild_id)
        await db.close_pool()


async def test_sync_edits_existing_board() -> None:
    await db.init_pool(get_settings().test_database_url, min_size=1, max_size=2)
    guild_id = _G + 12
    try:
        existing = _FakeMessage(4242)
        channel = _FakeChannel(556, existing={4242: existing})
        await _bind_committed(guild_id, 556)
        async with db.connection() as conn, conn.transaction():
            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE leaderboard_channels SET message_id = 4242 "
                    "WHERE guild_id = %s",
                    (guild_id,),
                )
        await sync_leaderboard_boards(_FakeClient({556: channel}))
        assert len(existing.edits) == 1
        assert not channel.sent  # edited, no repost
    finally:
        await _drop_binding(guild_id)
        await db.close_pool()


async def test_sync_skips_unchanged_board() -> None:
    await db.init_pool(get_settings().test_database_url, min_size=1, max_size=2)
    guild_id = _G + 13
    try:
        existing = _FakeMessage(4343)
        channel = _FakeChannel(557, existing={4343: existing})
        await _bind_committed(guild_id, 557)
        async with db.connection() as conn, conn.transaction():
            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE leaderboard_channels SET message_id = 4343 "
                    "WHERE guild_id = %s",
                    (guild_id,),
                )
        await sync_leaderboard_boards(_FakeClient({557: channel}))
        assert len(existing.edits) == 1
        await sync_leaderboard_boards(_FakeClient({557: channel}))
        assert len(existing.edits) == 1  # same ranking -> no second edit
    finally:
        await _drop_binding(guild_id)
        await db.close_pool()


async def test_sync_reposts_when_board_message_deleted() -> None:
    await db.init_pool(get_settings().test_database_url, min_size=1, max_size=2)
    guild_id = _G + 14
    try:
        channel = _FakeChannel(558)  # no message 9876 -> NotFound
        await _bind_committed(guild_id, 558)
        async with db.connection() as conn, conn.transaction():
            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE leaderboard_channels SET message_id = 9876, "
                    "updated_at = now() - interval '10 minutes' "
                    "WHERE guild_id = %s",
                    (guild_id,),
                )
        await sync_leaderboard_boards(_FakeClient({558: channel}))
        assert len(channel.sent) == 1
        assert await _binding_message_id(guild_id) == channel.sent[0].id
    finally:
        await _drop_binding(guild_id)
        await db.close_pool()


async def test_sync_no_repost_inside_grace_window() -> None:
    """A board 404ing right after being posted is propagation lag, not a
    deletion -- sync must wait rather than double-post."""
    await db.init_pool(get_settings().test_database_url, min_size=1, max_size=2)
    guild_id = _G + 16
    try:
        channel = _FakeChannel(560)  # no message 9876 -> NotFound
        await _bind_committed(guild_id, 560)
        async with db.connection() as conn, conn.transaction():
            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE leaderboard_channels SET message_id = 9876 "
                    "WHERE guild_id = %s",
                    (guild_id,),
                )  # updated_at stays fresh -> inside _REPOST_GRACE
        await sync_leaderboard_boards(_FakeClient({560: channel}))
        assert not channel.sent
        assert await _binding_message_id(guild_id) == 9876  # kept for retry
    finally:
        await _drop_binding(guild_id)
        await db.close_pool()


async def test_delete_board_message() -> None:
    old = _FakeMessage(3131)
    channel = _FakeChannel(561, existing={3131: old})
    client = _FakeClient({561: channel})
    assert await lb.delete_board_message(client, 561, 3131) is True
    assert old.deleted is True
    # Gone message / gone channel -> False, never raises.
    assert await lb.delete_board_message(client, 561, 9999) is False
    assert await lb.delete_board_message(client, 999, 3131) is False


async def test_sync_survives_forbidden_channel() -> None:
    await db.init_pool(get_settings().test_database_url, min_size=1, max_size=2)
    guild_id = _G + 15
    try:
        channel = _FakeChannel(
            559, send_error=discord.Forbidden(MagicMock(), "missing access")
        )
        await _bind_committed(guild_id, 559)
        await sync_leaderboard_boards(_FakeClient({559: channel}))
        assert not channel.sent
        assert await _binding_message_id(guild_id) is None  # kept for retry
    finally:
        await _drop_binding(guild_id)
        await db.close_pool()
