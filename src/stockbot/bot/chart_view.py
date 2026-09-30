"""Interactive chart controls: pan / zoom navigation plus a span Select.

Stateless by construction -- the whole window state rides inside each
component's custom_id (`cbt:{action}:{iid}:{end}:{span}:{axis}:{theme}`),
so a click needs no server-side session. Two entry paths reach
`handle_chart_component`: the View's item callbacks while the sending
process lives, and `StockBotClient.on_interaction` as the post-restart
fallback (discord.py dispatches a component interaction to both; the
handler's is_done() guard makes the second call a no-op).

Actions: panl/panr (shift the window half a span), zin/zout (halve/double
the span), home (re-anchor to the last open tick), s<span> (timeframe
presets: set span, re-anchor), ax (toggle the x-axis between real time
and raw ticks). The timeframe presets and the axis toggle ride one
Select whose cid action is `sel` -- its option values ARE the actions
("s960", "ax"); legacy `s<span>` buttons on old messages still resolve.
`axis` is "time" (UTC wall-clock labels) or "ticks"; `theme` is the
equipped theme item key ("-" = default palette) and rides the cid rather
than the clicker's pref -- a shared chart message can't repaint
per-clicker. Legacy 5/6-field cids parse as time/no-theme -- an in-place
upgrade.
"""

from __future__ import annotations

import logging
from datetime import UTC
from typing import Any

import discord
from psycopg import AsyncConnection

from stockbot import db
from stockbot.bot.charts import bucket_for_span, render_candle_chart
from stockbot.bot.format import stripe_for_change
from stockbot.shop.service import owns_item

log = logging.getLogger("stockbot.bot.chart_view")

CHART_CID_PREFIX = "cbt:"

MIN_SPAN = 30
MAX_SPAN = 6720  # ~1 week of open ticks at session.open_ticks=960
MAX_SPAN_PRO = 19200  # Pro Terminal bound: the 2w/1M spans
TIMEFRAME_SPANS = {
    "1h": 60, "4h": 240, "1d": 960, "1w": 4800,
    "2w": 9600, "1M": 19200,
}
PRO_SPANS = {"2w", "1M"}  # TIMEFRAME_SPANS keys gated on pro_terminal

_BUTTONS = [
    ("\u25c0 Older", "panl"),
    ("Newer \u25b6", "panr"),
    ("Zoom in", "zin"),
    ("Zoom out", "zout"),
    ("Latest", "home"),
]


AXES = ("time", "ticks")


def encode_cid(
    action: str, iid: int, end: int, span: int, axis: str = "time",
    theme: str | None = None, mine: bool = False,
) -> str:
    return (
        f"cbt:{action}:{iid}:{end}:{span}:{axis}:{theme or '-'}"
        f":{'m' if mine else '-'}"
    )


def parse_cid(
    custom_id: str,
) -> tuple[str, int, int, int, str, str | None, bool] | None:
    parts = custom_id.split(":")
    if parts[0] != "cbt" or len(parts) not in (5, 6, 7, 8):
        return None
    # Legacy 5/6/7-field ids predate axis/theme/mine -- parse the defaults.
    axis = parts[5] if len(parts) >= 6 else "time"
    if axis not in AXES:
        return None
    theme_raw = parts[6] if len(parts) >= 7 else "-"
    theme = theme_raw if theme_raw not in ("", "-") else None
    mine_raw = parts[7] if len(parts) == 8 else "-"
    if mine_raw not in ("m", "-"):
        return None
    try:
        return (
            parts[1], int(parts[2]), int(parts[3]), int(parts[4]),
            axis, theme, mine_raw == "m",
        )
    except ValueError:
        return None


async def _last_open_tick(conn: AsyncConnection, instrument_id: int) -> int | None:
    """Last open tick *on this instrument's venue* -- per-instrument so a
    chart never anchors on a tick where its own venue was closed."""
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT MAX(tick_index) FROM candles "
            "WHERE instrument_id = %s AND session_state = 'OPEN'",
            (instrument_id,),
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
            WHERE c.instrument_id = %s AND c.tick_index <= %s
              AND c.session_state = 'OPEN'
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
            WHERE c.instrument_id = %s
              AND c.session_state = 'OPEN'
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
            WHERE c.instrument_id = %s
              AND c.tick_index > %s AND c.tick_index <= %s
              AND c.session_state = 'OPEN'
            """,
            (instrument_id, end, last_open),
        )
        row = await cur.fetchone()
    return int(row[0]) if row else 0


async def load_chart_prefs(
    conn: AsyncConnection, user_id: int, *, max_span: int = MAX_SPAN
) -> tuple[int, str, str | None] | None:
    """The user's saved chart view: (span, axis, theme). span is
    re-clamped to `max_span` on read -- a bound change can't strand a
    stored value. `theme` is the equipped theme item key (or None for the
    default palette); callers verify ownership before rendering. None when
    the user has never customized a chart."""
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT span, axis, theme FROM chart_prefs WHERE user_id = %s",
            (user_id,),
        )
        row = await cur.fetchone()
    if row is None:
        return None
    span = min(max_span, max(MIN_SPAN, int(row[0])))
    theme = str(row[2]) if row[2] else None
    return span, str(row[1]), theme


async def save_chart_prefs(
    conn: AsyncConnection, user_id: int, span: int, axis: str
) -> None:
    """Remember the resolved view -- called on every chart interaction,
    so the last click IS the preference."""
    async with conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO chart_prefs (user_id, span, axis)
            VALUES (%s, %s, %s)
            ON CONFLICT (user_id) DO UPDATE
            SET span = EXCLUDED.span,
                axis = EXCLUDED.axis,
                updated_at = now()
            """,
            (user_id, span, axis),
        )


async def next_window(
    conn: AsyncConnection, action: str, iid: int, end: int, span: int,
    max_span: int = MAX_SPAN,
) -> tuple[int, int]:
    """Resolve a button action to the new (end, span). All movement is in
    open-candle units so session boundaries fall out of the open-only
    filter -- a pan across a close just skips it."""
    last_open = await _last_open_tick(conn, iid)
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
        return end, min(max_span, span * 2)
    if action == "home":
        return anchor, span
    if action.startswith("s") and action[1:].isdigit():
        return anchor, min(max_span, max(MIN_SPAN, int(action[1:])))
    return end, span


class _ChartButton(discord.ui.Button[discord.ui.View]):
    async def callback(self, interaction: discord.Interaction) -> None:
        await handle_chart_component(interaction)


class _ChartSelect(discord.ui.Select[discord.ui.View]):
    async def callback(self, interaction: discord.Interaction) -> None:
        await handle_chart_component(interaction)


def build_chart_view(
    iid: int, end: int, span: int, axis: str = "time",
    theme: str | None = None, mine: bool = False,
) -> discord.ui.View:
    """Component rows encoding the current window -- every component's
    custom_id carries (action, iid, end, span, axis, theme, mine) so no
    state lives process-side. The theme rides the cid rather than the
    clicker's pref: a shared chart message can't repaint per-clicker.
    `mine` marks ephemeral /chart messages: clicks keep drawing the
    clicker's own marks and reply with a fresh ephemeral message.

    Row 0 is navigation; row 1 is a single Select for the six span
    presets and the axis toggle -- its option values are the actions
    ("s960", "ax") resolved by the `sel` cid, which halves the chrome
    under every chart and lets gated Pro spans read as menu entries."""
    view = discord.ui.View(timeout=None)
    for label, action in _BUTTONS:
        view.add_item(
            _ChartButton(
                style=discord.ButtonStyle.secondary,
                label=label,
                custom_id=encode_cid(action, iid, end, span, axis, theme, mine),
                row=0,
            )
        )
    # The toggle labels the mode a pick switches TO, not the current one.
    other = "ticks" if axis == "time" else "time"
    options = [
        discord.SelectOption(
            label=label.upper(), value=f"s{t}", default=t == span
        )
        for label, t in TIMEFRAME_SPANS.items()
    ]
    options.append(discord.SelectOption(label=f"Axis: {other}", value="ax"))
    view.add_item(
        _ChartSelect(
            custom_id=encode_cid("sel", iid, end, span, axis, theme, mine),
            placeholder="Span…",
            options=options,
            row=1,
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
    action, iid, end, span, axis, theme, mine = parsed

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
        if action == "sel":
            # The span Select's option values ARE the actions ("s960",
            # "ax") -- normalize so legacy s<span> buttons and menu picks
            # share one path.
            values = data.get("values") or []
            action = str(values[0]) if values else ""
        if action == "ax":
            # Axis toggle: same window, flipped label mode.
            new_end, new_span = end, span
            axis = "ticks" if axis == "time" else "time"
        else:
            # Pro Terminal unlocks the 2w/1M presets and lifts the zoom
            # bound -- resolved per CLICKER, not from the message (a
            # shared chart can't repaint per-viewer, but span gating can).
            pro = await owns_item(conn, interaction.user.id, "pro_terminal")
            if (
                not pro
                and action.startswith("s")
                and action[1:].isdigit()
                and int(action[1:]) > MAX_SPAN
            ):
                await interaction.followup.send(
                    "The 2w and 1M spans need **Pro Terminal** — "
                    "`/shop item:pro_terminal`.",
                    ephemeral=True,
                )
                return
            new_end, new_span = await next_window(
                conn, action, iid, end, span,
                max_span=MAX_SPAN_PRO if pro else MAX_SPAN,
            )
        # The click teaches the clicker's default view -- the next bare
        # /chart opens at this span+axis. Pan position isn't saved:
        # charts always open anchored at the latest tick.
        await save_chart_prefs(conn, interaction.user.id, new_span, axis)
        result = await render_candle_chart(
            conn, iid, ticker, end=new_end, span=new_span, axis=axis,
            theme=theme,
            viewer_id=interaction.user.id if mine else None,
        )
    if result is None:
        await interaction.followup.send(
            "No candles in that window — zoom out or pan forward.",
            ephemeral=True,
        )
        return
    buf, resolved_end, end_ts, last_close, window_delta = result

    filename = f"{ticker}.png"
    # Price + delta in the title make the embed useful on a slow
    # connection; the direction-coloured stripe lands before the PNG.
    embed = discord.Embed(
        title=f"{ticker} \u2014 {name} · {last_close:,.2f} {window_delta:+.2%}",
        color=stripe_for_change(window_delta),
    )
    embed.set_image(url=f"attachment://{filename}")
    if axis == "time" and end_ts is not None:
        ending = end_ts.astimezone(UTC).strftime("%b %d %H:%M UTC")
    else:
        ending = str(resolved_end)
    embed.set_footer(
        text=(
            f"{new_span} open ticks ending {ending}"
            f" · {bucket_for_span(new_span)}t/candle"
        )
    )
    view = build_chart_view(iid, resolved_end, new_span, axis, theme, mine=mine)
    file = discord.File(buf, filename=filename)
    if mine:
        # Ephemeral messages are interaction-bound: there's no message
        # PATCH route for them, so a re-rendered PNG can't replace the
        # original in place. A fresh ephemeral message per click is the
        # private equivalent -- stale ones keep working (cids are
        # stateless), they just scroll up.
        await interaction.followup.send(
            embed=embed, file=file, view=view, ephemeral=True
        )
        return
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
