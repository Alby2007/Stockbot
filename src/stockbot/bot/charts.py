"""Candle chart rendering: matplotlib -> PNG in-process, no render service."""

from __future__ import annotations

import asyncio
import io
import math
from collections import OrderedDict
from datetime import UTC
from typing import Any

import matplotlib

matplotlib.use("Agg")

from matplotlib.figure import Figure  # noqa: E402
from matplotlib.patches import Rectangle  # noqa: E402
from matplotlib.ticker import FuncFormatter, MaxNLocator  # noqa: E402
from psycopg import AsyncConnection  # noqa: E402
from psycopg.rows import dict_row  # noqa: E402

from stockbot.shop.service import palette_from_metadata  # noqa: E402

# TradingView-style dark terminal palette -- the default every themed
# palette merges over, so a theme only needs to set the keys it changes.
_DEFAULT_PALETTE = {
    "bg": "#131722",
    "up": "#26a69a",
    "down": "#ef5350",
    "grid": "#ffffff",
    "text": "#b2b5be",
    "accent": "#26a69a",
    "spine": "#2a2e39",
    "muted": "#5a5f6b",
    "halt_flow": "#ff9800",
    "halt_model": "#ef5350",
    "pill_text": "#ffffff",
}

# Theme shop rows are static (a palette, once loaded, never changes), so
# resolution caches forever: {item_key: palette|None} -- None = not a
# theme row, render falls back to defaults.
_THEME_CACHE: dict[str, dict[str, str] | None] = {}


async def _palette_for(
    conn: AsyncConnection, theme: str | None
) -> dict[str, str]:
    if theme is None:
        return _DEFAULT_PALETTE
    if theme not in _THEME_CACHE:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT metadata FROM shop_items WHERE key = %s AND kind = 'COSMETIC'",
                (theme,),
            )
            row = await cur.fetchone()
        _THEME_CACHE[theme] = (
            palette_from_metadata(row["metadata"]) if row else None
        )
    pal = _THEME_CACHE[theme]
    return _DEFAULT_PALETTE if pal is None else {**_DEFAULT_PALETTE, **pal}

# Render cache: a chart PNG is a pure function of its window's candle rows,
# so a repeat request (button spam, two users on the same ticker) can reuse
# the bytes instead of paying the matplotlib render again. The key
# fingerprints every row's tick_index + close + volume rather than just
# (end, span, axis) -- intra-tick fills amend the live candle, and the
# fingerprint catches it so a mid-tick trade still re-renders.
_RENDER_CACHE: OrderedDict[tuple[Any, ...], bytes] = OrderedDict()
_RENDER_CACHE_MAX = 64


def bucket_for_span(span: int) -> int:
    """Ticks aggregated per rendered candle; keeps any span at <=240 bars."""
    return max(1, math.ceil(span / 240))


def _span_label(span: int) -> str:
    """Human span for the title (trading-day-aware: 960 open ticks = one
    market day, 4800 = a week) -- replaces "N open ticks" insider speak.
    Odd zoom levels from the buttons fall back to tick counts."""
    return {60: "1h", 240: "4h", 960: "1d", 4800: "1w"}.get(
        span, f"~{span // 960}d" if span >= 960 else f"{span}t"
    )


def _session_boundaries(
    ticks: list[int], times: list[Any], bucket: int
) -> list[int]:
    """Candle indexes where an open-phase discontinuity was compressed by
    the ordinal axis. Two kinds: the tick gap exceeds the bucket
    (overnight, halt, missing rows), or the wall-clock gap exceeds 6x
    the window's median candle spacing (service downtime between ticks
    -- contiguous tick_indexes can still span days). Rendered at
    `i - 0.5` with a `_gap_tag` duration label: a same-UTC-day close
    teleports the axis (07:17 -> 15:44) and the `Mon DD` day labels only
    fire across UTC dates, so without a legible boundary + duration the
    compressed gap reads as a glitch. The closed tail has shading."""
    bounds = {
        i
        for i in range(1, len(ticks))
        if ticks[i] - ticks[i - 1] > bucket
    }
    # 10-minute floor: brief tick-cadence jitter isn't a "break"; real
    # downtime and session closes are hours.
    deltas = [
        (times[i] - times[i - 1]).total_seconds()
        for i in range(1, len(times))
        if times[i] is not None and times[i - 1] is not None
    ]
    spacing = sorted(d for d in deltas if d > 0)
    if spacing:
        threshold = max(spacing[len(spacing) // 2] * 6, 600.0)
        bounds |= {
            i
            for i in range(1, len(times))
            if times[i] is not None
            and times[i - 1] is not None
            and (times[i] - times[i - 1]).total_seconds() > threshold
        }
    return sorted(bounds)


def _gap_tag(t_prev: Any, t_next: Any, tick_prev: int, tick_next: int) -> str:
    """Label for a compressed boundary: the wall-clock gap when both
    edges carry a market_ticks.ts ("45m"/"8.5h"/"2d"), else the tick
    span. ts is NULL on candles with no market_ticks row."""
    if t_prev is not None and t_next is not None:
        secs = (t_next - t_prev).total_seconds()
        if secs > 0:
            if secs < 5400:
                return f"{secs / 60:,.0f}m"
            if secs < 129_600:
                h = secs / 3600
                return f"{h:.1f}h" if h % 1 else f"{int(h)}h"
            d = secs / 86_400
            return f"{d:.1f}d" if d % 1 else f"{int(d)}d"
    return f"{tick_next - tick_prev}t"


async def _select_window_rows(
    conn: AsyncConnection,
    instrument_id: int,
    end_tick: int | None,
    span: int,
) -> tuple[list[Any], int | None]:
    """Window selection: the last `span` OPEN-phase ticks ending at
    `end_tick` (None = last open tick), bucketed `bucket_for_span(span)`
    ticks per row -- OHLCV aggregated in SQL so any span renders <=240
    candles. A short tail of the current closed run is appended only when
    the window ends at last_open (a panned-back view doesn't want it).
    Returns (rows newest-first, resolved_end) -- resolved_end is the
    clamped end the buttons encode."""
    bucket = bucket_for_span(span)
    async with conn.cursor() as cur:
        await cur.execute(
            """
            WITH last_open AS (
                SELECT MAX(tick_index) AS t
                FROM market_ticks
                WHERE session_state = 'OPEN'
            ),
            eff AS (
                -- LEAST ignores NULL: end=None -> last_open; a panned end
                -- past it clamps down. No open ticks at all -> NULL ->
                -- empty window.
                SELECT LEAST(%s, (SELECT t FROM last_open)) AS end_tick
            ),
            picked AS (
                -- Open-phase candles only (missing market_ticks default
                -- OPEN); rn numbers newest-first so grp buckets anchor at
                -- the window's right edge and the OLDEST bucket may be
                -- partial.
                (SELECT c.tick_index,
                        (ROW_NUMBER() OVER (ORDER BY c.tick_index DESC) - 1)
                            / %s AS grp
                 FROM candles c
                 LEFT JOIN market_ticks mt ON mt.tick_index = c.tick_index
                 WHERE c.instrument_id = %s
                   AND c.tick_index <= (SELECT end_tick FROM eff)
                   AND COALESCE(mt.session_state, 'OPEN') = 'OPEN'
                 ORDER BY c.tick_index DESC
                 LIMIT %s)
                UNION ALL
                -- Closed-run tail: 1-min rows (grp offset keeps them
                -- unaggregated and disjoint from open bucket indexes).
                (SELECT c.tick_index, 1000000 + c.tick_index
                 FROM candles c
                 WHERE c.instrument_id = %s
                   AND (SELECT end_tick FROM eff) = (SELECT t FROM last_open)
                   AND c.tick_index > (SELECT end_tick FROM eff)
                 ORDER BY c.tick_index
                 LIMIT %s)
            )
            SELECT MAX(c.tick_index) AS tick_index,
                   (array_agg(c.open ORDER BY c.tick_index))[1] AS open,
                   MAX(c.high) AS high,
                   MIN(c.low) AS low,
                   (array_agg(c.close ORDER BY c.tick_index DESC))[1] AS close,
                   SUM(c.volume) AS volume,
                   MAX(c.halt_kind) AS halt_kind,
                   -- 'OPEN' > 'CLOSED': a bucket mixing the session
                   -- boundary renders as the open-phase candle it mostly is.
                   MAX(COALESCE(mt.session_state, 'OPEN')) AS session_state,
                   -- Buckets label by their right edge's timestamp, like
                   -- real charts label a bar by its close.
                   MAX(mt.ts) AS ts
            FROM candles c
            JOIN picked p ON p.tick_index = c.tick_index
            LEFT JOIN market_ticks mt ON mt.tick_index = c.tick_index
            WHERE c.instrument_id = %s
            GROUP BY p.grp
            ORDER BY tick_index DESC
            """,
            (
                end_tick,
                bucket,
                instrument_id,
                span,
                instrument_id,
                min(24, max(8, span // 40)),
                instrument_id,
            ),
        )
        rows = await cur.fetchall()

        await cur.execute(
            """
            SELECT LEAST(%s, (SELECT MAX(tick_index) FROM market_ticks
                              WHERE session_state = 'OPEN'))
            """,
            (end_tick,),
        )
        end_row = await cur.fetchone()
    resolved_end = end_row[0] if end_row else None
    return rows, int(resolved_end) if resolved_end is not None else None


async def render_candle_chart(
    conn: AsyncConnection,
    instrument_id: int,
    ticker: str,
    *,
    end: int | None = None,
    span: int = 240,
    axis: str = "time",
    theme: str | None = None,
) -> tuple[io.BytesIO, int, Any] | None:
    """Candlestick chart of the `span` open ticks ending at `end`.
    Returns (png, resolved_end, end_ts) -- the clamped end tick for
    button state and the newest candle's timestamp for footers -- or
    None if the window holds no candles."""
    if axis not in ("time", "ticks"):
        axis = "time"
    palette = await _palette_for(conn, theme)
    rows, resolved_end = await _select_window_rows(
        conn, instrument_id, end, span
    )
    if not rows or resolved_end is None:
        return None
    bucket = bucket_for_span(span)
    # `theme` rides the fingerprint: without it the cache would serve one
    # user's palette to everyone on the same window.
    fp = (
        instrument_id,
        span,
        axis,
        theme or "",
        tuple((int(r[0]), float(r[4]), int(r[5])) for r in rows),
    )
    cached = _RENDER_CACHE.get(fp)
    if cached is not None:
        _RENDER_CACHE.move_to_end(fp)
        buf = io.BytesIO(cached)
    else:
        # The render is ~100ms+ of synchronous CPU -- run it off the event
        # loop so gateway heartbeats aren't delayed. matplotlib.figure.Figure
        # (not pyplot) keeps it off pyplot's shared global state, which isn't
        # thread-safe.
        buf = await asyncio.to_thread(
            _render_png, rows, ticker, span, bucket, axis, palette
        )
        _RENDER_CACHE[fp] = buf.getvalue()
        while len(_RENDER_CACHE) > _RENDER_CACHE_MAX:
            _RENDER_CACHE.popitem(last=False)
    end_ts = rows[0][8]  # newest-first: the window's true right edge
    return buf, resolved_end, end_ts


def _axis_formatter(
    times: list[Any], ticks: list[int], axis: str
) -> Any:
    """X-label formatter for either axis mode. `time`: UTC HH:MM, with
    "Sep 25" on the first candle of each new UTC day in multi-day
    windows -- the compressed closed run then reads as a real overnight
    gap (16:00 -> Sep 25) instead of a cryptic tick jump. Candles whose
    market_ticks row is missing (synthetic data) fall back to the tick
    index."""
    if axis == "ticks":

        def _tick_fmt(v: float, _pos: int) -> str:
            i = int(round(v))
            if abs(v - i) > 1e-6 or not (0 <= i < len(ticks)):
                return ""
            return str(ticks[i])

        return _tick_fmt

    days = {t.astimezone(UTC).date() for t in times if t is not None}
    multi_day = len(days) > 1

    def _fmt(v: float, _pos: int) -> str:
        i = int(round(v))
        if abs(v - i) > 1e-6 or not (0 <= i < len(times)):
            return ""
        t = times[i]
        if t is None:
            return str(ticks[i])
        t = t.astimezone(UTC)
        prev = next(
            (times[j] for j in range(i - 1, -1, -1) if times[j] is not None),
            None,
        )
        if multi_day and (
            prev is None or t.date() != prev.astimezone(UTC).date()
        ):
            return str(t.strftime("%b %d"))
        return str(t.strftime("%H:%M"))

    return _fmt


def _render_png(
    rows: list[Any], ticker: str, span: int = 240, bucket: int = 1,
    axis: str = "time", palette: dict[str, str] | None = None,
) -> io.BytesIO:
    p = palette or _DEFAULT_PALETTE
    rows = list(reversed(rows))

    ticks = [int(r[0]) for r in rows]
    opens = [float(r[1]) for r in rows]
    highs = [float(r[2]) for r in rows]
    lows = [float(r[3]) for r in rows]
    closes = [float(r[4]) for r in rows]
    volumes = [int(r[5]) for r in rows]
    halts = [r[6] for r in rows]
    sessions = [str(r[7]) for r in rows]
    times = [r[8] for r in rows]

    up = [c >= o for o, c in zip(opens, closes, strict=True)]
    price_hi = max(highs)
    price_lo = min(lows)
    price_span = price_hi - price_lo
    # Autoscale on flat/near-flat data collapses ylim to a hair-thin
    # interval and eps-height doji bodies would fill the whole panel --
    # force the pad so the displayed range is always meaningful.
    ypad = max(price_span * 0.08, price_lo * 0.002, 1e-9)
    view = price_span + 2 * ypad
    doji_eps = view * 0.004  # minimum body height so doji stay visible

    fig = Figure(figsize=(10, 6), facecolor=p["bg"])
    gs = fig.add_gridspec(2, 1, height_ratios=(3, 1), hspace=0.05)
    ax = fig.add_subplot(gs[0])
    axv = fig.add_subplot(gs[1], sharex=ax)
    for a in (ax, axv):
        a.set_facecolor(p["bg"])
        a.tick_params(colors=p["text"], labelsize=8)
        # Horizontal gridlines only -- the session separators below carry
        # the vertical reference, without the crosshatch noise.
        a.grid(axis="y", color=p["grid"], alpha=0.06)
        for spine in a.spines.values():
            spine.set_color(p["spine"])
    ax.tick_params(labelbottom=False)

    # Ticker watermark behind everything -- cheap terminal flavor.
    ax.text(
        0.5,
        0.45,
        ticker,
        transform=ax.transAxes,
        fontsize=64,
        color=p["text"],
        alpha=0.035,
        ha="center",
        va="center",
        zorder=0,
    )

    # Closed-session shading: contiguous CLOSED runs get a muted overlay,
    # and their flat candles draw in grey rather than up/down colors.
    run_start: int | None = None
    spans: list[tuple[int, int]] = []
    for i, s in enumerate(sessions + ["OPEN"]):
        if s == "CLOSED" and run_start is None:
            run_start = i
        elif s != "CLOSED" and run_start is not None:
            spans.append((run_start, i - 1))
            run_start = None
    for lo_i, hi_i in spans:
        ax.axvspan(lo_i - 0.5, hi_i + 0.5, color=p["grid"], alpha=0.03, lw=0)

    # X positions are ordinal (0..N-1): the window holds OPEN candles only
    # (plus a short CLOSED tail), so a skipped closed run shows as a price
    # discontinuity between neighbours -- like real charts compress the
    # overnight -- instead of a hundreds-wide empty band on a tick axis.
    pos = list(range(len(ticks)))
    n_bars = len(pos)
    # Dense windows thin the bars so 240-candle spans don't blur to mush.
    bar_w, wick_lw = (0.8, 0.8) if n_bars <= 120 else (0.6, 0.6)

    # Session-boundary separators: open-open tick discontinuities the
    # ordinal axis hides (overnights, halts, missing rows) get a visible
    # dashed separator plus the compressed gap's duration. Without both,
    # a same-day close teleports the axis (07:17 -> 15:44) with no cue
    # and the reopen candle reads as a bad tick.
    for i in _session_boundaries(ticks, times, bucket):
        ax.axvline(i - 0.5, color=p["accent"], linestyle="--", linewidth=0.9)
        ax.text(
            i - 0.5,
            price_hi + ypad,
            _gap_tag(times[i - 1], times[i], ticks[i - 1], ticks[i]),
            color=p["text"],
            fontsize=6,
            ha="center",
            va="top",
            alpha=0.85,
            bbox=dict(facecolor=p["bg"], edgecolor="none", alpha=0.8, pad=0.4),
        )

    # Candlesticks: wick low->high, body over [min(o,c), |o-c|].
    vol_colors = []
    for i, x in enumerate(pos):
        closed = sessions[i] == "CLOSED"
        color = p["muted"] if closed else (p["up"] if up[i] else p["down"])
        ax.vlines(x, lows[i], highs[i], color=color, linewidth=wick_lw)
        body_lo = min(opens[i], closes[i])
        body_h = max(abs(closes[i] - opens[i]), doji_eps)
        ax.add_patch(
            Rectangle(
                (x - bar_w / 2, body_lo), bar_w, body_h,
                facecolor=color, edgecolor=color, linewidth=0.5,
            )
        )
        vol_colors.append(color)

        if halts[i] is not None:
            ax.scatter(
                x,
                highs[i] + view * 0.02,
                marker="v",
                s=18,
                color=p["halt_flow"] if halts[i] == "FLOW" else p["halt_model"],
                zorder=5,
                linewidths=0,
            )

    # Window OHLC + net-change legend, top-left -- the "how much did it
    # move" answer in one glance, colored by the window's direction.
    window_delta = closes[-1] / opens[0] - 1 if opens[0] else 0.0
    window_color = p["up"] if closes[-1] >= opens[0] else p["down"]
    ax.text(
        0.01,
        0.98,
        f"O {opens[0]:,.2f}   H {price_hi:,.2f}   L {price_lo:,.2f}   "
        f"C {closes[-1]:,.2f}   Δ {window_delta:+.2%}",
        transform=ax.transAxes,
        color=window_color,
        fontsize=9,
        va="top",
        ha="left",
    )

    # Last-price line, colored by the final candle's direction, with a
    # TradingView-style pill pinned in the reserved right gutter.
    last_color = p["up"] if up[-1] else p["down"]
    ax.axhline(closes[-1], color=last_color, linestyle="--", linewidth=0.8, alpha=0.9)
    ax.annotate(
        f"{closes[-1]:,.2f}",
        xy=(1.005, closes[-1]),
        xycoords=("axes fraction", "data"),
        color=p["pill_text"],
        fontweight="bold",
        fontsize=8,
        va="center",
        annotation_clip=False,
        bbox=dict(
            boxstyle="round,pad=0.25", facecolor=last_color, edgecolor="none"
        ),
    )

    ax.set_ylim(price_lo - ypad, price_hi + ypad)
    for lo_i, hi_i in spans:
        ax.text(
            (lo_i + hi_i) / 2,
            price_hi + ypad,
            "CLOSED",
            color=p["text"],
            fontsize=6,
            ha="center",
            va="top",
            alpha=0.7,
        )

    closed_now = sessions[-1] == "CLOSED"
    bucket_note = f" · {bucket}t/candle" if bucket > 1 else ""
    ax.set_title(
        f"{ticker} · {_span_label(span)}{bucket_note}"
        + (" · MARKET CLOSED" if closed_now else ""),
        color=p["text"],
        fontsize=11,
    )
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:,.2f}"))
    ax.set_ylabel("price", color=p["text"], fontsize=8)
    ax.margins(x=0.02)

    axv.bar(pos, volumes, width=bar_w, color=vol_colors, alpha=0.6)
    axv.set_ylim(bottom=0)
    # Ordinal positions hold OPEN candles only (+ a closed tail): a skipped
    # closed run reads as a jump in the axis labels, not a hole in the plot.
    axv.xaxis.set_major_locator(MaxNLocator(integer=True))
    axv.xaxis.set_major_formatter(
        FuncFormatter(_axis_formatter(times, ticks, axis))
    )
    axv.set_xlabel(
        "tick" if axis == "ticks" else "time (UTC)", color=p["text"], fontsize=8
    )
    axv.set_ylabel("volume", color=p["text"], fontsize=8)
    axv.yaxis.set_major_formatter(
        FuncFormatter(
            lambda v, _: f"{v / 1e3:,.0f}k" if v >= 1000 else f"{max(v, 0):,.0f}"
        )
    )

    # Right gutter reserved for the price pill's annotate (x=1.005 lands
    # inside the rect boundary instead of clipping at the figure edge).
    gs.tight_layout(fig, rect=(0, 0, 0.94, 1))
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=120, facecolor=fig.get_facecolor())
    buf.seek(0)
    return buf
