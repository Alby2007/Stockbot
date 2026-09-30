"""Trade-scope picker -- stateless select like shop_view's.

`scope:pick` carries no state; the chosen season id is in values[0].
Every selection re-validates that the user holds an ACTIVE entry in
that season before pinning, so replayed cids are safe. The select is
attached to an ephemeral /scope reply; `on_interaction` is the only
dispatch path needed (ephemeral Views also double-fire, hence _INFLIGHT).
"""

from __future__ import annotations

import logging
from typing import Any

import discord

from stockbot import db
from stockbot.bot.format import EMBED_INFO

log = logging.getLogger("stockbot.bot.scope_view")

CID_PREFIX = "scope:"
PICK_CID = f"{CID_PREFIX}pick"


def scope_view(options: list[discord.SelectOption]) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(
        discord.ui.Select(
            custom_id=PICK_CID,
            placeholder="Pin your league-flag trades to…",
            options=options,
        )
    )
    return view


_INFLIGHT: set[int] = set()


async def handle_scope_component(interaction: discord.Interaction) -> None:
    if interaction.response.is_done() or interaction.id in _INFLIGHT:
        return
    _INFLIGHT.add(interaction.id)
    try:
        await _handle(interaction)
    finally:
        _INFLIGHT.discard(interaction.id)


async def _handle(interaction: discord.Interaction) -> None:
    from stockbot.seasons.service import (
        get_active_entry,
        get_season,
        pin_scope,
        resolve_trade_entry,
    )

    data: Any = interaction.data or {}
    if str(data.get("custom_id", "")) != PICK_CID:
        return
    values = list(data.get("values") or [])
    try:
        season_id = int(values[0]) if values else -1
    except ValueError:
        season_id = -1

    async with db.connection() as conn:
        uid = interaction.user.id
        if season_id == 0:
            # "auto" option: clear the pin, recency takes over.
            from stockbot.seasons.service import clear_scope

            await clear_scope(conn, uid)
            await interaction.response.send_message(
                "Scope cleared — `league:` trades now follow your most recent "
                "active season.",
                ephemeral=True,
            )
            return
        entry = await get_active_entry(conn, uid, season_id)
        season = await get_season(conn, season_id) if entry else None
        if entry is None or season is None:
            await interaction.response.send_message(
                "That season is no longer active for you — run /scope again.",
                ephemeral=True,
            )
            return
        await pin_scope(conn, uid, season_id)
        pinned = await resolve_trade_entry(conn, uid)
    label = season.name
    if season.duel_id is not None:
        label = f"Duel — {season.name}"
    elif season.division_tier is not None:
        from stockbot.divisions.service import tier_name

        label = f"{tier_name(season.division_tier)} Division"
    await interaction.response.send_message(
        f"Scope pinned to **{label}** — `league:` commands route there "
        f"(resolved entry {pinned[0] if pinned else '—'}).",
        ephemeral=True,
    )


def describe_entry(season: Any, duel: Any | None) -> str:
    """One option label for an active competitive entry."""
    if season.duel_id is not None and duel is not None:
        return f"Duel vs <@{duel.challenger_id}>·<@{duel.opponent_id}> — {season.name}"[:100]
    if season.duel_id is not None:
        return f"Duel — {season.name}"[:100]
    if season.division_tier is not None:
        from stockbot.divisions.service import tier_name

        return f"{tier_name(season.division_tier)} Division — {season.name}"[:100]
    return f"League — {season.name}"[:100]


def scope_embed() -> discord.Embed:
    return discord.Embed(
        title="Trade scope",
        description=(
            "`league:`-flag commands (buy, sell, orders, options, portfolio…) "
            "route to the pinned season below, or your most recent active "
            "season if unpinned."
        ),
        color=EMBED_INFO,
    )
