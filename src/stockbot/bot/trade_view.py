"""Trade offer components -- stateless cids like collection_view.

`trade:{action}:{trade_id}` carries the whole state: the card_trades
row is the source of truth, so buttons keep working after a restart.
The double-dispatch guard mirrors the other views: a live View's
callback and `on_interaction` both fire on one Interaction, and
_INFLIGHT turns the loser into a no-op.

Accept/decline gate on interaction.user.id == counterparty; cancel
gates on the proposer. Everyone else gets an ephemeral refusal.
"""

from __future__ import annotations

import logging
from typing import Any

import discord

from stockbot import db

log = logging.getLogger("stockbot.bot.trade_view")

CID_PREFIX = "trade:"


def parse_cid(custom_id: str) -> tuple[str, int] | None:
    parts = custom_id.split(":")
    if parts[0] != "trade" or len(parts) != 3 or parts[1] not in (
        "accept",
        "decline",
        "cancel",
    ):
        return None
    try:
        return parts[1], int(parts[2])
    except ValueError:
        return None


def trade_view(trade_id: int) -> discord.ui.View:
    """Accept/Decline/Cancel button row for one open offer."""
    view = discord.ui.View(timeout=None)
    view.add_item(
        discord.ui.Button(
            style=discord.ButtonStyle.success,
            label="Accept",
            custom_id=f"{CID_PREFIX}accept:{trade_id}",
        )
    )
    view.add_item(
        discord.ui.Button(
            style=discord.ButtonStyle.danger,
            label="Decline",
            custom_id=f"{CID_PREFIX}decline:{trade_id}",
        )
    )
    view.add_item(
        discord.ui.Button(
            style=discord.ButtonStyle.secondary,
            label="Cancel offer",
            custom_id=f"{CID_PREFIX}cancel:{trade_id}",
        )
    )
    return view


async def render_trade(
    conn: Any, trade: Any, *, resolved: str | None = None
) -> discord.Embed:
    """Offer card: both sides' contents spelled out -- the accept click
    must show exactly what moves, no hidden terms."""
    from stockbot.collectibles.service import get_card

    async def names(keys: list[str]) -> str:
        out = []
        for k in keys:
            c = await get_card(conn, k)
            out.append(c.name if c else k)
        return ", ".join(f"**{n}**" for n in out) or "—"

    gave = await names(trade.give_cards)
    want = await names(trade.want_cards)
    if trade.give_shards:
        gave += f" + **{trade.give_shards}** shards"
    if trade.want_shards:
        want += f" + **{trade.want_shards}** shards"
    embed = discord.Embed(
        title=f"Trade offer #{trade.id}" + (f" — {resolved}" if resolved else ""),
        description=(
            f"<@{trade.proposer}> gives: {gave}\n"
            f"<@{trade.counterparty}> gives: {want}"
        ),
    )
    if resolved is None:
        embed.set_footer(
            text="Only the counterparty can accept or decline; "
            "either side sees exact contents first."
        )
    return embed


_INFLIGHT: set[int] = set()


async def handle_trade_component(interaction: discord.Interaction) -> None:
    if interaction.response.is_done() or interaction.id in _INFLIGHT:
        return
    _INFLIGHT.add(interaction.id)
    try:
        await _handle(interaction)
    finally:
        _INFLIGHT.discard(interaction.id)


async def _handle(interaction: discord.Interaction) -> None:
    from stockbot.collectibles import trades
    from stockbot.market.data import current_tick_index

    data: Any = interaction.data or {}
    parsed = parse_cid(str(data.get("custom_id", "")))
    if parsed is None:
        await interaction.response.send_message(
            "That trade offer is stale.", ephemeral=True
        )
        return
    action, trade_id = parsed
    async with db.connection() as conn:
        trade = await trades.get_trade(conn, trade_id)
        if trade is None:
            await interaction.response.send_message(
                "Unknown trade.", ephemeral=True
            )
            return
        if trade.status != "OPEN":
            await interaction.response.send_message(
                f"That trade is already {trade.status.lower()}.", ephemeral=True
            )
            return
        uid = interaction.user.id
        if action in ("accept", "decline") and uid != trade.counterparty:
            await interaction.response.send_message(
                "Only the counterparty can answer this offer.", ephemeral=True
            )
            return
        if action == "cancel" and uid != trade.proposer:
            await interaction.response.send_message(
                "Only the proposer can cancel — the counterparty can decline.",
                ephemeral=True,
            )
            return
        tick = await current_tick_index(conn)
        try:
            if action == "accept":
                await trades.accept(conn, trade_id, uid, tick)
            elif action == "decline":
                await trades.decline(conn, trade_id, uid)
            else:
                await trades.cancel(conn, trade_id, uid)
            trade = await trades.get_trade(conn, trade_id)
        except trades.TradeError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        assert trade is not None
        label = trade.status.lower().capitalize()
        embed = await render_trade(conn, trade, resolved=label)
    await interaction.response.edit_message(embed=embed, view=None)
