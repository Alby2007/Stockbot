from __future__ import annotations

import io
from typing import Any

import pytest
from psycopg import AsyncConnection

from stockbot.bot import charts
from stockbot.bot.charts import render_candle_chart
from stockbot.market.tick import apply_tick


async def test_render_returns_none_without_history(conn: AsyncConnection) -> None:
    async with conn.cursor() as cur:
        await cur.execute("SELECT id FROM instruments ORDER BY id LIMIT 1")
        (instrument_id,) = await cur.fetchone()
    assert await render_candle_chart(conn, instrument_id, "TEST") is None


async def test_render_returns_a_png_after_a_tick(conn: AsyncConnection) -> None:
    await apply_tick(conn, "chart-test-seed")
    async with conn.cursor() as cur:
        await cur.execute("SELECT id FROM instruments ORDER BY id LIMIT 1")
        (instrument_id,) = await cur.fetchone()

    buf = await render_candle_chart(conn, instrument_id, "TEST")
    assert buf is not None
    data = buf.read()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"


async def test_render_covers_closed_session_rows(conn: AsyncConnection) -> None:
    """Flip to a short open/closed cycle so the render sees CLOSED candles
    (and the market_ticks join's session_state) -- exercises the shading
    and muted-candle paths."""
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE config SET value = %s WHERE key = 'session.open_ticks'", (2,)
        )
        await cur.execute(
            "UPDATE config SET value = %s WHERE key = 'session.closed_ticks'", (3,)
        )
        await cur.execute("SELECT id FROM instruments ORDER BY id LIMIT 1")
        (instrument_id,) = await cur.fetchone()

    for _ in range(8):
        await apply_tick(conn, "chart-test-seed")

    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT COUNT(*) FROM candles c "
            "JOIN market_ticks mt ON mt.tick_index = c.tick_index "
            "WHERE c.instrument_id = %s AND mt.session_state = 'CLOSED'",
            (instrument_id,),
        )
        (closed_count,) = await cur.fetchone()
    assert closed_count > 0

    buf = await render_candle_chart(conn, instrument_id, "TEST")
    assert buf is not None
    assert buf.read()[:8] == b"\x89PNG\r\n\x1a\n"


async def test_closed_window_anchors_to_last_open_tick(
    conn: AsyncConnection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Closed market: the window reaches back to the last open candles
    (instead of rendering an all-flat closed chart) and keeps only a
    short closed tail as the 'closed now' signal."""
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE config SET value = %s WHERE key = 'session.open_ticks'", (2,)
        )
        await cur.execute(
            "UPDATE config SET value = %s WHERE key = 'session.closed_ticks'", (3,)
        )
        await cur.execute("SELECT id FROM instruments ORDER BY id LIMIT 1")
        (instrument_id,) = await cur.fetchone()

    # Tick until the latest tick is CLOSED.
    for _ in range(10):
        await apply_tick(conn, "chart-test-seed")
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT session_state FROM market_ticks "
                "ORDER BY tick_index DESC LIMIT 1"
            )
            (state,) = await cur.fetchone()
        if state == "CLOSED":
            break
    assert state == "CLOSED"

    captured: list[list[Any]] = []

    def fake_render(rows: list[Any], ticker: str) -> io.BytesIO:
        captured.append(rows)
        return io.BytesIO(b"png")

    monkeypatch.setattr(charts, "_render_png", fake_render)
    buf = await render_candle_chart(conn, instrument_id, "TEST", limit_ticks=4)
    assert buf is not None

    rows = captured[0]
    sessions = [r[7] for r in rows]  # DESC by tick_index
    assert sessions[0] == "CLOSED"  # closed tail present
    assert sessions[-1] == "OPEN"  # window anchored back to open candles
    assert sessions.count("CLOSED") <= 3  # tail never exceeds the closed run


async def test_window_skips_intra_window_closed_run(
    conn: AsyncConnection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """0033: the window is the last N OPEN candles -- a closed run inside
    the window is skipped, not rendered as flat filler. open=3/closed=5
    (cycle 8): ticks 0-2 open, 3-7 closed, 8-9 open."""
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE config SET value = %s WHERE key = 'session.open_ticks'", (3,)
        )
        await cur.execute(
            "UPDATE config SET value = %s WHERE key = 'session.closed_ticks'", (5,)
        )
        await cur.execute(
            "SELECT id FROM instruments WHERE is_active AND kind != 'INDEX' "
            "ORDER BY id LIMIT 1"
        )
        (instrument_id,) = await cur.fetchone()

    for _ in range(10):  # ticks 0-9; last open tick = 9
        await apply_tick(conn, "chart-test-seed")

    captured: list[list[Any]] = []
    monkeypatch.setattr(
        charts,
        "_render_png",
        lambda rows, ticker: (captured.append(rows), io.BytesIO(b"png"))[1],
    )
    buf = await render_candle_chart(conn, instrument_id, "TEST", limit_ticks=4)
    assert buf is not None

    ticks = sorted(int(r[0]) for r in captured[0])
    # Last 4 OPEN candles <= 9 are {1, 2, 8, 9}: the closed run 3-7 must
    # not appear (the pre-0033 window would have returned {6,7,8,9}).
    assert ticks == [1, 2, 8, 9]
