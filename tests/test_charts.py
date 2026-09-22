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

    result = await render_candle_chart(conn, instrument_id, "TEST")
    assert result is not None
    buf, _end = result
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

    result = await render_candle_chart(conn, instrument_id, "TEST")
    assert result is not None
    buf, _end = result
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

    def fake_render(
        rows: list[Any], ticker: str, span: int, bucket: int
    ) -> io.BytesIO:
        captured.append(rows)
        return io.BytesIO(b"png")

    monkeypatch.setattr(charts, "_render_png", fake_render)
    result = await render_candle_chart(conn, instrument_id, "TEST", span=4)
    assert result is not None

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
        lambda rows, ticker, *a: (captured.append(rows), io.BytesIO(b"png"))[1],
    )
    result = await render_candle_chart(conn, instrument_id, "TEST", span=4)
    assert result is not None

    ticks = sorted(int(r[0]) for r in captured[0])
    # Last 4 OPEN candles <= 9 are {1, 2, 8, 9}: the closed run 3-7 must
    # not appear (the pre-0033 window would have returned {6,7,8,9}).
    assert ticks == [1, 2, 8, 9]


def test_bucket_for_span() -> None:
    assert charts.bucket_for_span(240) == 1
    assert charts.bucket_for_span(241) == 2
    assert charts.bucket_for_span(960) == 4
    assert charts.bucket_for_span(4800) == 20
    assert charts.bucket_for_span(1) == 1


async def _seed_synthetic_candles(
    conn: AsyncConnection, instrument_id: int, n: int = 300
) -> None:
    """Open-phase ticks 0..n-1 with a known OHLCV shape, no tick engine --
    the window query only needs candles + market_ticks rows."""
    async with conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO market_ticks (tick_index, market_factor, sector_factors) "
            "SELECT g, 0, '{}'::jsonb FROM generate_series(0, %s) g",
            (n - 1,),
        )
        await cur.execute(
            "INSERT INTO candles "
            "(instrument_id, tick_index, open, high, low, close, volume) "
            "SELECT %s, g, 100+g-0.5, 100+g+0.5, 100+g-1, 100+g, g "
            "FROM generate_series(0, %s) g",
            (instrument_id, n - 1),
        )


async def _instrument_id(conn: AsyncConnection) -> int:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT id FROM instruments WHERE is_active AND kind != 'INDEX' "
            "ORDER BY id LIMIT 1"
        )
        (iid,) = await cur.fetchone()
    return int(iid)


async def test_bucketing_aggregates_ohlcv(conn: AsyncConnection) -> None:
    """span=480 -> bucket=2: newest-first pairs, oldest bucket partial."""
    iid = await _instrument_id(conn)
    await _seed_synthetic_candles(conn, iid, 300)

    rows, end = await charts._select_window_rows(conn, iid, None, 480)
    assert end == 299
    assert len(rows) == 150

    newest = rows[0]  # bucket {299, 298}
    assert int(newest[0]) == 299
    assert float(newest[1]) == pytest.approx(397.5)  # open of tick 298
    assert float(newest[2]) == pytest.approx(399.5)  # max high
    assert float(newest[3]) == pytest.approx(397.0)  # min low
    assert float(newest[4]) == pytest.approx(399.0)  # close of tick 299
    assert float(newest[5]) == pytest.approx(597.0)  # 298 + 299

    oldest = rows[-1]  # bucket {1, 0}: tick_index is MAX, open is tick 0's
    assert int(oldest[0]) == 1
    assert float(oldest[1]) == pytest.approx(99.5)
    assert float(oldest[5]) == pytest.approx(1.0)  # 0 + 1


async def test_window_end_pins_and_clamps(conn: AsyncConnection) -> None:
    """`end` pins the window's right edge; an end past last_open clamps."""
    iid = await _instrument_id(conn)
    await _seed_synthetic_candles(conn, iid, 300)

    rows, end = await charts._select_window_rows(conn, iid, 200, 480)
    assert end == 200
    assert int(rows[0][0]) == 200  # newest candle is the pinned end
    assert len(rows) == 101  # 201 candles in 2-tick buckets

    rows, end = await charts._select_window_rows(conn, iid, 9999, 480)
    assert end == 299  # clamped to last open tick

    # span < history: newest 60 candles unbucketed.
    rows, _ = await charts._select_window_rows(conn, iid, None, 60)
    assert len(rows) == 60
    assert int(rows[0][0]) == 299 and int(rows[-1][0]) == 240
