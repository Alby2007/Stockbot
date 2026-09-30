"""Duel challenge components -- stateless cids like trade_view.

`duel:{action}:{duel_id}` carries the whole state: the duels row is the
source of truth, so buttons keep working after a restart. The
double-dispatch guard mirrors the other views: a live View's callback
and `on_interaction` both fire on one Interaction, and _INFLIGHT turns
the loser into a no-op.

Accept/decline gate on interaction.user.id == opponent; cancel gates on
the challenger. Everyone else gets an ephemeral refusal.
"""

from __future__ import annotations

import logging
from typing import Any

import discord

from stockbot import db
from stockbot.bot.format import EMBED_FLAT, EMBED_INFO, EMBED_UP

log = logging.getLogger("stockbot.bot.duel_view")

CID_PREFIX = "duel:"


def parse_cid(custom_id: str) -> tuple[str, int] | None:
    parts = custom_id.split(":")
    if parts[0] != "duel" or len(parts) != 3 or parts[1] not in (
        "accept",
        "decline",
        "cancel",
    ):
        return None
    try:
        return parts[1], int(parts[2])
    except ValueError:
        return None


def duel_view(duel_id: int) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(
        discord.ui.Button(
            style=discord.ButtonStyle.success,
            label="Accept",
            custom_id=f"{CID_PREFIX}accept:{duel_id}",
        )
    )
    view.add_item(
        discord.ui.Button(
            style=discord.ButtonStyle.danger,
            label="Decline",
            custom_id=f"{CID_PREFIX}decline:{duel_id}",
        )
    )
    view.add_item(
        discord.ui.Button(
            style=discord.ButtonStyle.secondary,
            label="Cancel challenge",
            custom_id=f"{CID_PREFIX}cancel:{duel_id}",
        )
    )
    return view


def _window_label(window_ticks: int) -> str:
    from stockbot.market.engine import TICKS_PER_DAY

    if window_ticks % TICKS_PER_DAY == 0:
        days = window_ticks // TICKS_PER_DAY
        return f"{days}d" if days > 1 else "1 day"
    return f"{window_ticks // 60}h"


def render_duel(duel: Any, *, resolved: str | None = None) -> discord.Embed:
    resolved_colors = {
        "active": EMBED_UP,
        "declined": EMBED_FLAT,
        "cancelled": EMBED_FLAT,
        "expired": EMBED_FLAT,
    }
    stake = f"${duel.stake_minor / 100:,.2f}"
    embed = discord.Embed(
        title=f"⚔️ Duel #{duel.id}" + (f" — {resolved}" if resolved else ""),
        description=(
            f"<@{duel.challenger_id}> challenges <@{duel.opponent_id}>\n"
            f"Stake **{stake}** each · { _window_label(duel.window_ticks) } · "
            "equal league stakes, higher equity wins the pot"
        ),
        color=resolved_colors.get(resolved or "", EMBED_INFO),
    )
    if resolved is None:
        embed.set_footer(
            text="Only the challenged player can accept or decline; "
            "the challenger can cancel. Both stakes escrow on accept."
        )
    return embed


_INFLIGHT: set[int] = set()


async def handle_duel_component(interaction: discord.Interaction) -> None:
    if interaction.response.is_done() or interaction.id in _INFLIGHT:
        return
    _INFLIGHT.add(interaction.id)
    try:
        await _handle(interaction)
    finally:
        _INFLIGHT.discard(interaction.id)


async def _handle(interaction: discord.Interaction) -> None:
    from stockbot.duels import service as duels
    from stockbot.duels.errors import DuelError
    from stockbot.market.data import current_tick_index

    data: Any = interaction.data or {}
    parsed = parse_cid(str(data.get("custom_id", "")))
    if parsed is None:
        await interaction.response.send_message(
            "That duel challenge is stale.", ephemeral=True
        )
        return
    action, duel_id = parsed
    async with db.connection() as conn:
        duel = await duels.get_duel(conn, duel_id)
        if duel is None:
            await interaction.response.send_message("Unknown duel.", ephemeral=True)
            return
        if duel.status != "OPEN":
            await interaction.response.send_message(
                f"That duel is already {duel.status.lower()}.", ephemeral=True
            )
            return
        uid = interaction.user.id
        if action in ("accept", "decline") and uid != duel.opponent_id:
            await interaction.response.send_message(
                "Only the challenged player can answer this offer.",
                ephemeral=True,
            )
            return
        if action == "cancel" and uid != duel.challenger_id:
            await interaction.response.send_message(
                "Only the challenger can cancel — the opponent can decline.",
                ephemeral=True,
            )
            return
        tick = await current_tick_index(conn) or 0
        try:
            if action == "accept":
                duel = await duels.accept(conn, duel_id, uid, tick)
                label = "Active — good luck"
            elif action == "decline":
                await duels.decline(conn, duel_id, uid)
                duel = await duels.get_duel(conn, duel_id)
                label = "Declined"
            else:
                await duels.cancel(conn, duel_id, uid)
                duel = await duels.get_duel(conn, duel_id)
                label = "Cancelled"
        except DuelError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        assert duel is not None
        embed = render_duel(duel, resolved=label)
    await interaction.response.edit_message(embed=embed, view=None)
