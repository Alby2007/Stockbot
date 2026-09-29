"""Paginated `/collection` binder -- stateless cids like shop_view.

`cards:pg:{owner_id}:{page}` carries the whole navigation state, so
page buttons keep working after a restart with no server-side session.
The double-dispatch guard mirrors `chart_view`/`shop_view`: a live
View's callback and `on_interaction` both fire on one Interaction, and
`_INFLIGHT` turns the loser into a no-op.

Each page is one embed of card lines grouped by sector/set; buttons are
the only components (◀ ▶), so `response.edit_message` is always valid.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import discord

from stockbot import db
from stockbot.bot.format import EMBED_GOLD

log = logging.getLogger("stockbot.bot.collection_view")

CID_PREFIX = "cards:"
_PAGE_LINES = 18  # card lines per embed page (under the 6000-char cap)

# Frame ladder for the binder's compact glyphs: a colored circle reads
# at a glance even at one char.
FRAME_GLYPH = {
    "STANDARD": "⚪",
    "SILVER": "🔘",
    "GOLD": "🟡",
    "PLATINUM": "💠",
    "EPIC": "🟣",
    "LEGENDARY": "🌟",
    "PART": "🧩",
}

# Embed stripe per frame -- a legendary pull announces itself on the
# reveal card before the eye reaches the name.
FRAME_COLOR = {
    "STANDARD": 0xBDC3C7,
    "SILVER": 0x95A5A6,
    "GOLD": 0xF1C40F,
    "PLATINUM": 0x5DADEC,
    "EPIC": 0x9B59B6,
    "LEGENDARY": 0xE67E22,
    "PART": 0x3498DB,
}

# Print-context stamps: one glyph per provenance mark.
STAMP_GLYPH = {
    "HALT_PRINT": "🧊",
    "PEAK_PRINT": "🏔",
    "DAY_ONE": "🌅",
    "OFF_HOURS": "🌙",
    "MOON": "🚀",
    "CRATER": "☄",
    "PITY_BREAK": "🎲",
}

STAMP_LABEL = {
    "HALT_PRINT": "printed during a halt",
    "PEAK_PRINT": "printed at the all-time high",
    "DAY_ONE": "printed on listing day",
    "OFF_HOURS": "printed after hours",
    "MOON": "printed on a moon tick",
    "CRATER": "printed on a crash tick",
    "PITY_BREAK": "broke the pity streak",
}


def stamp_glyphs(stamps: list[str] | tuple[str, ...] | None) -> str:
    """Compact ' 🧊🚀' suffix for a pull's stamp list."""
    if not stamps:
        return ""
    return "".join(STAMP_GLYPH.get(s, "") for s in stamps)


def serial_tag(row: dict[str, Any]) -> str:
    """' #7' suffix; 1st Ed. when the held copy predates set rotation."""
    serial = row.get("best_serial")
    if serial is None:
        return ""
    tag = f" #{serial}"
    if row.get("first_edition"):
        tag += " 1st Ed."
    return tag




def parse_cid(custom_id: str) -> tuple[str, int, int] | None:
    parts = custom_id.split(":")
    if parts[0] != "cards" or len(parts) != 4 or parts[1] != "pg":
        return None
    try:
        return "pg", int(parts[2]), int(parts[3])
    except ValueError:
        return None


def frame_glyph(frame: str) -> str:
    return FRAME_GLYPH.get(frame, "❔")


def frame_color(frame: str) -> int | None:
    return FRAME_COLOR.get(frame)


@dataclass
class CollectionView:
    stats: dict[str, int]
    lines: list[str]  # pre-grouped display lines
    page_count: int


def _group_lines(rows: list[dict[str, Any]]) -> list[str]:
    """Binder order: instruments by sector, then lore, then
    commemoratives -- each with a small header row."""
    instruments = [r for r in rows if r["kind"] == "INSTRUMENT"]
    lore = [r for r in rows if r["kind"] == "LORE"]
    comm = [r for r in rows if r["kind"] == "COMMEMORATIVE"]
    parts = [r for r in rows if r["kind"] == "PART"]
    assembled = [r for r in rows if r["kind"] == "ASSEMBLED"]

    lines: list[str] = []
    by_sector: dict[Any, list[dict[str, Any]]] = {}
    for r in instruments:
        by_sector.setdefault(r["sector_id"], []).append(r)
    for sid in sorted(by_sector, key=lambda s: (s is None, s)):
        header = by_sector[sid][0].get("sector_name") if sid is not None else None
        lines.append(f"**— {header or 'Index'} —**")
        for r in by_sector[sid]:
            # copies counts lifetime pulls, including burned duplicates;
            # a held row is always a single copy.
            copies = f" (pulled ×{r['copies']})" if r["copies"] > 1 else ""
            lines.append(
                f"{frame_glyph(r['best_frame'])} {r['name']}"
                f"{serial_tag(r)}{stamp_glyphs(r.get('best_stamps'))}{copies}"
            )
    if lore:
        lines.append("**— Lore —**")
        for r in lore:
            copies = f" (pulled ×{r['copies']})" if r["copies"] > 1 else ""
            lines.append(
                f"{frame_glyph(r['best_frame'])} {r['name']}"
                f"{serial_tag(r)}{stamp_glyphs(r.get('best_stamps'))}{copies}"
            )
    if comm:
        lines.append("**— Commemoratives —**")
        for r in comm:
            lines.append(f"🏅 {r['name']}{serial_tag(r)}")
    if assembled:
        lines.append("**— Assembled —**")
        for r in assembled:
            lines.append(f"🏗 {r['name']}{serial_tag(r)}")
    if parts:
        lines.append("**— Parts —**")
        for r in parts:
            # PART copies are the held stack (consumed on assemble).
            lines.append(f"🧩 {r['name']} ×{r['copies']}")
    return lines


def build_page(
    display_name: str,
    stats: dict[str, int],
    rows: list[dict[str, Any]],
    owner_id: int,
    page: int,
) -> tuple[discord.Embed, discord.ui.View]:
    lines = _group_lines(rows)
    page_count = max(1, (len(lines) + _PAGE_LINES - 1) // _PAGE_LINES)
    page = max(0, min(page, page_count - 1))
    window = lines[page * _PAGE_LINES : (page + 1) * _PAGE_LINES]

    held = stats.get("held", 0)
    inst_total = stats.get("instrument_total", 0)
    lore_held = stats.get("lore_held", 0)
    lore_total = stats.get("lore_total", 0)
    frame_bits = " · ".join(
        f"{n} {FRAME_GLYPH[f]}"
        for f in ("PLATINUM", "GOLD", "SILVER", "STANDARD")
        if (n := stats.get(f"frame_{f.lower()}", 0))
    )
    summary = (
        f"**{held}/{inst_total}** instrument cards"
        + (f" · {frame_bits}" if frame_bits else "")
        + f" · Lore **{lore_held}/{lore_total}**"
        + f" · **{stats.get('shards', 0)}** shards"
        + f" · score **{stats.get('score', 0)}**"
    )

    embed = discord.Embed(
        title=f"{display_name}'s collection",
        description=summary
        + "\n\n"
        + ("\n".join(window) if window else "*No cards yet — `/open` a pack.*"),
        color=EMBED_GOLD,
    )
    embed.set_footer(text=f"Page {page + 1}/{page_count}")

    view = discord.ui.View(timeout=None)
    if page_count > 1:
        view.add_item(
            _PageButton("◀", f"{CID_PREFIX}pg:{owner_id}:{page - 1}", disabled=page == 0)
        )
        view.add_item(
            _PageButton(
                "▶",
                f"{CID_PREFIX}pg:{owner_id}:{page + 1}",
                disabled=page >= page_count - 1,
            )
        )
    return embed, view


class _PageButton(discord.ui.Button[discord.ui.View]):
    def __init__(self, label: str, cid: str, *, disabled: bool) -> None:
        super().__init__(
            style=discord.ButtonStyle.secondary, label=label, custom_id=cid, disabled=disabled
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await handle_collection_component(interaction)


_INFLIGHT: set[int] = set()


async def handle_collection_component(interaction: discord.Interaction) -> None:
    if interaction.response.is_done() or interaction.id in _INFLIGHT:
        return
    _INFLIGHT.add(interaction.id)
    try:
        await _handle(interaction)
    finally:
        _INFLIGHT.discard(interaction.id)


async def _handle(interaction: discord.Interaction) -> None:
    from stockbot.collectibles.service import collection_stats, list_collection

    data: Any = interaction.data or {}
    parsed = parse_cid(str(data.get("custom_id", "")))
    if parsed is None:
        await interaction.response.send_message(
            "That binder page is stale — run `/collection` again.", ephemeral=True
        )
        return
    _action, owner_id, page = parsed
    async with db.connection() as conn:
        stats = await collection_stats(conn, owner_id)
        rows = await list_collection(conn, owner_id)
    # No privileged members intent: resolve the owner via the user cache,
    # falling back to fetch (then a bare id) for cross-server binders.
    user = interaction.client.get_user(owner_id)
    if user is None:
        try:
            user = await interaction.client.fetch_user(owner_id)
        except discord.HTTPException:
            user = None
    display = user.display_name if user else f"User {owner_id}"
    embed, view = build_page(display, stats, rows, owner_id, page)
    await interaction.response.edit_message(embed=embed, view=view)
