"""Interactive chart controls: pan / zoom / timeframe buttons.

Stateless by construction -- the whole window state rides inside each
button's custom_id (`cbt:{action}:{iid}:{end}:{span}`), so a click needs
no server-side session. Two entry paths reach `handle_chart_component`:
the View's item callbacks while the sending process lives, and
`StockBotClient.on_interaction` as the post-restart fallback (discord.py
dispatches a component interaction to both; the handler's is_done()
guard makes the second call a no-op).

Actions: panl/panr (shift the window half a span), zin/zout (halve/double
the span), home (re-anchor to the last open tick), s<span> (timeframe
presets: set span, re-anchor).
"""

from __future__ import annotations

import logging
from typing import Any

import discord
from psycopg import AsyncConnection

from stockbot import db
from stockbot.bot.charts import bucket_for_span, render_candle_chart

log = logging.getLogger("stockbot.bot.chart_view")

CHART_CID_PREFIX = "cbt:"

MIN_SPAN = 30
MAX_SPAN = 6720  # ~1 week of open ticks at session.open_ticks=960
TIMEFRAME_SPANS = {"1h": 60, "4h": 240, "1d": 960, "1w": 4800}

_BUTTONS = [
    ("\u25c0 Older", "panl", 0),
    ("Newer \u25b6", "panr", 0),
    ("Zoom in", "zin", 0),
    ("Zoom out", "zout", 0),
    ("Latest", "home", 0),
    ("1H", "s60", 1),
    ("4H", "s240", 1),
    ("1D", "s960", 1),
    ("1W", "s4800", 1),
]


def encode_cid(action: str, iid: int, end: int, span: int) -> str:
    return f"cbt:{action}:{iid}:{end}:{span}"


def parse_cid(custom_id: str) -> tuple[str, int, int, int] | None:
    parts = custom_id.split(":")
    if len(parts) != 5 or parts[0] != "cbt":
        return None
    try:
        return parts[1], int(parts[2]), int(parts[3]), int(parts[4])
    except ValueError:
        return None


async def _last_open_tick(conn: AsyncConnection) -> int | None:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT MAX(tick_index) FROM market_ticks WHERE session_state = 'OPEN'"
        )
        row = await cur.fetchone()
    return int(row[0]) if row and row[0] is not None else None


async def _open_tick_offset(
    conn: AsyncConnection, instrument_id: int, end: int, offset: int
) -> int | None:
    """The open tick `offset` open-candles before `end` (offset 0 = the
    open tick at-or-before `end` itself)."""
    async with conn.cursor() as cur:
        await cur.execute(
            """
            SELECT c.tick_index FROM candles c
            LEFT JOIN market_ticks mt ON mt.tick_index = c.tick_index
            WHERE c.instrument_id = %s AND c.tick_index <= %s
              AND COALESCE(mt.session_state, 'OPEN') = 'OPEN'
            ORDER BY c.tick_index DESC OFFSET %s LIMIT 1
            """,
            (instrument_id, end, offset),
        )
        row = await cur.fetchone()
    return int(row[0]) if row else None


async def _earliest_open_tick(conn: AsyncConnection, instrument_id: int) -> int | None:
    async with conn.cursor() as cur:
        await cur.execute(
            """
            SELECT c.tick_index FROM candles c
            LEFT JOIN market_ticks mt ON mt.tick_index = c.tick_index
            WHERE c.instrument_id = %s
              AND COALESCE(mt.session_state, 'OPEN') = 'OPEN'
            ORDER BY c.tick_index LIMIT 1
            """,
            (instrument_id,),
        )
        row = await cur.fetchone()
    return int(row[0]) if row else None


async def _distance_from_last_open(
    conn: AsyncConnection, instrument_id: int, end: int, last_open: int
) -> int:
    """Open candles strictly between `end` and `last_open` -- how far the
    window's right edge sits from 'now'."""
    async with conn.cursor() as cur:
        await cur.execute(
            """
            SELECT COUNT(*) FROM candles c
            LEFT JOIN market_ticks mt ON mt.tick_index = c.tick_index
            WHERE c.instrument_id = %s
              AND c.tick_index > %s AND c.tick_index <= %s
              AND COALESCE(mt.session_state, 'OPEN') = 'OPEN'
            """,
            (instrument_id, end, last_open),
        )
        row = await cur.fetchone()
    return int(row[0]) if row else 0


async def next_window(
    conn: AsyncConnection, action: str, iid: int, end: int, span: int
) -> tuple[int, int]:
    """Resolve a button action to the new (end, span). All movement is in
    open-candle units so session boundaries fall out of the open-only
    filter -- a pan across a close just skips it."""
    last_open = await _last_open_tick(conn)
    anchor = last_open if last_open is not None else end
    if action == "panl":
        new_end = await _open_tick_offset(conn, iid, end, max(1, span // 2))
        if new_end is None:
            # Past the start of history: clamp to the first candle.
            new_end = await _earliest_open_tick(conn, iid)
        return (new_end if new_end is not None else end), span
    if action == "panr":
        shift = max(1, span // 2)
        d = await _distance_from_last_open(conn, iid, end, anchor)
        new_end = await _open_tick_offset(conn, iid, anchor, max(0, d - shift))
        return (new_end if new_end is not None else anchor), span
    if action == "zin":
        return end, max(MIN_SPAN, span // 2)
    if action == "zout":
        return end, min(MAX_SPAN, span * 2)
    if action == "home":
        return anchor, span
    if action.startswith("s") and action[1:].isdigit():
        return anchor, min(MAX_SPAN, max(MIN_SPAN, int(action[1:])))
    return end, span


class _ChartButton(discord.ui.Button[discord.ui.View]):
    async def callback(self, interaction: discord.Interaction) -> None:
        await handle_chart_component(interaction)


def build_chart_view(iid: int, end: int, span: int) -> discord.ui.View:
    """Button rows encoding the current window -- every button's custom_id
    carries (action, iid, end, span) so no state lives process-side."""
    view = discord.ui.View(timeout=None)
    for label, action, row in _BUTTONS:
        view.add_item(
            _ChartButton(
                style=discord.ButtonStyle.secondary,
                label=label,
                custom_id=encode_cid(action, iid, end, span),
                row=row,
            )
        )
    return view


_INFLIGHT: set[int] = set()


async def handle_chart_component(interaction: discord.Interaction) -> None:
    # A live View AND on_interaction both fire for one click (discord.py
    # dispatches both, on the SAME Interaction object). is_done() alone
    # doesn't dedup -- both tasks can enter before either responds -- so
    # the in-flight set claims the interaction id atomically; the loser
    # of the race returns here as a no-op.
    if interaction.response.is_done() or interaction.id in _INFLIGHT:
        log.info(
            "chart component skipped iid=%s done=%s inflight=%s",
            interaction.id,
            interaction.response.is_done(),
            interaction.id in _INFLIGHT,
        )
        return
    _INFLIGHT.add(interaction.id)
    log.info("chart component handling iid=%s", interaction.id)
    try:
        await _handle(interaction)
    finally:
        _INFLIGHT.discard(interaction.id)


async def _handle(interaction: discord.Interaction) -> None:
    data: Any = interaction.data or {}
    parsed = parse_cid(str(data.get("custom_id", "")))
    if parsed is None:
        await interaction.response.send_message(
            "That chart is stale — run /chart for a fresh one.", ephemeral=True
        )
        return
    action, iid, end, span = parsed

    # Payload-free type-6 ACK up front. New file attachments cannot ride
    # the type-7 edit_message interaction callback (Discord rejects it --
    # observed as 10062 Unknown interaction); the PNG goes up on the
    # follow-up message PATCH, which supports uploads.
    try:
        await interaction.response.defer()
    except Exception:
        log.exception("chart component defer failed iid=%s", interaction.id)
        raise
    log.info("chart component deferred iid=%s", interaction.id)

    async with db.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT ticker, name FROM instruments WHERE id = %s", (iid,)
            )
            info = await cur.fetchone()
        if info is None:
            await interaction.followup.send(
                "That instrument no longer exists — run /chart.", ephemeral=True
            )
            return
        ticker, name = str(info[0]), str(info[1])
        new_end, new_span = await next_window(conn, action, iid, end, span)
        result = await render_candle_chart(
            conn, iid, ticker, end=new_end, span=new_span
        )
    if result is None:
        await interaction.followup.send(
            "No candles in that window — zoom out or pan forward.",
            ephemeral=True,
        )
        return
    buf, resolved_end = result

    filename = f"{ticker}.png"
    embed = discord.Embed(title=f"{ticker} \u2014 {name}")
    embed.set_image(url=f"attachment://{filename}")
    embed.set_footer(
        text=(
            f"{new_span} open ticks ending {resolved_end}"
            f" · {bucket_for_span(new_span)}t/candle"
        )
    )
    view = build_chart_view(iid, resolved_end, new_span)
    file = discord.File(buf, filename=filename)
    msg = interaction.message
    try:
        if msg is not None:
            await msg.edit(embed=embed, attachments=[file], view=view)
        else:
            await interaction.edit_original_response(
                embed=embed, attachments=[file], view=view
            )
    except Exception:
        log.exception("chart component message edit failed iid=%s", interaction.id)
        raise
    log.info("chart component edited iid=%s span=%s end=%s", interaction.id, new_span, resolved_end)
