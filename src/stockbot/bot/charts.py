"""Candle chart rendering: matplotlib -> PNG in-process, no render service."""

from __future__ import annotations

import asyncio
import io
from typing import Any

import matplotlib

matplotlib.use("Agg")

from matplotlib.figure import Figure  # noqa: E402
from psycopg import AsyncConnection  # noqa: E402


async def render_candle_chart(
    conn: AsyncConnection, instrument_id: int, ticker: str, limit_ticks: int = 240
) -> io.BytesIO | None:
    """Line-plus-range chart of the last `limit_ticks` candles. Returns None
    if the instrument has no candle history yet.
    """
    async with conn.cursor() as cur:
        await cur.execute(
            """
            SELECT tick_index, high, low, close FROM candles
            WHERE instrument_id = %s
            ORDER BY tick_index DESC
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

    ticks = [r[0] for r in rows]
    highs = [float(r[1]) for r in rows]
    lows = [float(r[2]) for r in rows]
    closes = [float(r[3]) for r in rows]

    fig = Figure(figsize=(8, 4))
    ax = fig.add_subplot(111)
    ax.plot(ticks, closes, color="#4C9AFF", linewidth=1.5, label="close")
    ax.fill_between(ticks, lows, highs, color="#4C9AFF", alpha=0.15, label="range")
    ax.set_title(f"{ticker} — last {len(ticks)} ticks")
    ax.set_xlabel("tick")
    ax.set_ylabel("price")
    ax.grid(alpha=0.3)
    fig.tight_layout()

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=120)
    buf.seek(0)
    return buf
