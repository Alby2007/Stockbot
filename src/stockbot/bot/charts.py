"""Candle chart rendering: matplotlib -> PNG in-process, no render service."""

from __future__ import annotations

import asyncio
import io
from typing import Any

import matplotlib

matplotlib.use("Agg")

from matplotlib.figure import Figure  # noqa: E402
from matplotlib.patches import Rectangle  # noqa: E402
from matplotlib.ticker import FuncFormatter  # noqa: E402
from psycopg import AsyncConnection  # noqa: E402

# TradingView-style dark terminal palette.
_BG = "#131722"
_UP = "#26a69a"
_DOWN = "#ef5350"
_GRID = "#ffffff"
_TEXT = "#b2b5be"
_SPINE = "#2a2e39"
_MUTED = "#5a5f6b"
_HALT_FLOW = "#ff9800"
_HALT_MODEL = "#ef5350"


async def render_candle_chart(
    conn: AsyncConnection, instrument_id: int, ticker: str, limit_ticks: int = 240
) -> io.BytesIO | None:
    """Candlestick chart of the last `limit_ticks` candles. Returns None
    if the instrument has no candle history yet.
    """
    async with conn.cursor() as cur:
        await cur.execute(
            """
            SELECT c.tick_index, c.open, c.high, c.low, c.close, c.volume,
                   c.halt_kind, COALESCE(mt.session_state, 'OPEN') AS session_state
            FROM candles c
            LEFT JOIN market_ticks mt ON mt.tick_index = c.tick_index
            WHERE c.instrument_id = %s
            ORDER BY c.tick_index DESC
            LIMIT %s
            """,
            (instrument_id, limit_ticks),
        )
        rows = await cur.fetchall()
    if not rows:
        return None
    # The render is ~100ms+ of synchronous CPU -- run it off the event loop
    # so gateway heartbeats aren't delayed. matplotlib.figure.Figure (not
    # pyplot) keeps it off pyplot's shared global state, which isn't
    # thread-safe.
    return await asyncio.to_thread(_render_png, rows, ticker)


def _render_png(rows: list[Any], ticker: str) -> io.BytesIO:
    rows = list(reversed(rows))

    ticks = [int(r[0]) for r in rows]
    opens = [float(r[1]) for r in rows]
    highs = [float(r[2]) for r in rows]
    lows = [float(r[3]) for r in rows]
    closes = [float(r[4]) for r in rows]
    volumes = [int(r[5]) for r in rows]
    halts = [r[6] for r in rows]
    sessions = [str(r[7]) for r in rows]

    up = [c >= o for o, c in zip(opens, closes, strict=True)]
    price_hi = max(highs)
    price_lo = min(lows)
    span = price_hi - price_lo
    # Autoscale on flat/near-flat data collapses ylim to a hair-thin
    # interval and eps-height doji bodies would fill the whole panel --
    # force the pad so the displayed range is always meaningful.
    ypad = max(span * 0.08, price_lo * 0.002, 1e-9)
    view = span + 2 * ypad
    doji_eps = view * 0.004  # minimum body height so doji stay visible

    fig = Figure(figsize=(10, 6), facecolor=_BG)
    gs = fig.add_gridspec(2, 1, height_ratios=(3, 1), hspace=0.05)
    ax = fig.add_subplot(gs[0])
    axv = fig.add_subplot(gs[1], sharex=ax)
    for a in (ax, axv):
        a.set_facecolor(_BG)
        a.tick_params(colors=_TEXT, labelsize=8)
        a.grid(color=_GRID, alpha=0.06)
        for spine in a.spines.values():
            spine.set_color(_SPINE)
    ax.tick_params(labelbottom=False)

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
        ax.axvspan(ticks[lo_i] - 0.5, ticks[hi_i] + 0.5, color=_GRID, alpha=0.03, lw=0)

    # Candlesticks: wick low->high, body over [min(o,c), |o-c|].
    vol_colors = []
    for i, x in enumerate(ticks):
        closed = sessions[i] == "CLOSED"
        color = _MUTED if closed else (_UP if up[i] else _DOWN)
        ax.vlines(x, lows[i], highs[i], color=color, linewidth=0.8)
        body_lo = min(opens[i], closes[i])
        body_h = max(abs(closes[i] - opens[i]), doji_eps)
        ax.add_patch(
            Rectangle(
                (x - 0.4, body_lo), 0.8, body_h,
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
                color=_HALT_FLOW if halts[i] == "FLOW" else _HALT_MODEL,
                zorder=5,
                linewidths=0,
            )

    # Last-price line, colored by the final candle's direction.
    last_color = _UP if up[-1] else _DOWN
    ax.axhline(closes[-1], color=last_color, linestyle="--", linewidth=0.8, alpha=0.9)
    ax.annotate(
        f"{closes[-1]:,.2f}",
        xy=(1.005, closes[-1]),
        xycoords=("axes fraction", "data"),
        color=last_color,
        fontsize=8,
        va="center",
        annotation_clip=False,
    )

    ax.set_ylim(price_lo - ypad, price_hi + ypad)
    for lo_i, hi_i in spans:
        ax.text(
            (ticks[lo_i] + ticks[hi_i]) / 2,
            price_hi + ypad,
            "CLOSED",
            color=_TEXT,
            fontsize=6,
            ha="center",
            va="top",
            alpha=0.7,
        )

    ax.set_title(f"{ticker} — last {len(ticks)} ticks", color=_TEXT, fontsize=11)
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:,.2f}"))
    ax.set_ylabel("price", color=_TEXT, fontsize=8)
    ax.margins(x=0.02)

    axv.bar(ticks, volumes, width=0.8, color=vol_colors, alpha=0.6)
    axv.set_ylim(bottom=0)
    axv.set_xlabel("tick", color=_TEXT, fontsize=8)
    axv.set_ylabel("volume", color=_TEXT, fontsize=8)
    axv.yaxis.set_major_formatter(
        FuncFormatter(
            lambda v, _: f"{v / 1e3:,.0f}k" if v >= 1000 else f"{max(v, 0):,.0f}"
        )
    )

    gs.tight_layout(fig)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150, facecolor=fig.get_facecolor())
    buf.seek(0)
    return buf
