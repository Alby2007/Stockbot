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
    buf, _end, _end_ts = result
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
    buf, _end, _end_ts = result
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
        rows: list[Any], ticker: str, span: int, bucket: int, axis: str
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


async def test_window_rows_carry_tick_timestamps(conn: AsyncConnection) -> None:
    """Each bucket labels by its newest tick's market_ticks.ts."""
    iid = await _instrument_id(conn)
    async with conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO market_ticks (tick_index, market_factor, sector_factors, ts) "
            "SELECT g, 0, '{}'::jsonb, "
            "'2025-01-06 14:00+00'::timestamptz + g * interval '1 minute' "
            "FROM generate_series(0, 9) g"
        )
        await cur.execute(
            "INSERT INTO candles "
            "(instrument_id, tick_index, open, high, low, close, volume) "
            "SELECT %s, g, 100, 101, 99, 100, 0 FROM generate_series(0, 9) g",
            (iid,),
        )

    rows, end = await charts._select_window_rows(conn, iid, None, 10)
    assert end == 9
    # bucket=1: the row's ts is that tick's own market_ticks.ts.
    newest = rows[0]
    assert int(newest[0]) == 9
    assert newest[8].hour == 14 and newest[8].minute == 9
    assert int(rows[-1][0]) == 0
    assert rows[-1][8].minute == 0


async def test_bucketed_rows_label_by_newest_tick_ts(
    conn: AsyncConnection,
) -> None:
    """Aggregated candles label by the bucket's LAST tick's ts."""
    iid = await _instrument_id(conn)
    async with conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO market_ticks (tick_index, market_factor, sector_factors, ts) "
            "SELECT g, 0, '{}'::jsonb, "
            "'2025-01-06 14:00+00'::timestamptz + g * interval '1 minute' "
            "FROM generate_series(0, 299) g"
        )
        await cur.execute(
            "INSERT INTO candles "
            "(instrument_id, tick_index, open, high, low, close, volume) "
            "SELECT %s, g, 100, 101, 99, 100, 0 FROM generate_series(0, 299) g",
            (iid,),
        )

    rows, end = await charts._select_window_rows(conn, iid, None, 480)
    assert end == 299 and len(rows) == 150  # 2-tick buckets
    # Newest bucket {299,298}: labels by tick 299's ts = 14:00 + 299min.
    ts = rows[0][8]
    assert ts.hour * 60 + ts.minute == 14 * 60 + 299  # 18:59
    # The bucket's ts must be >= its members' -- newest tick's own row
    # (unbucketed query) carries the identical ts.
    solo, _ = await charts._select_window_rows(conn, iid, 299, 1)
    assert solo[0][8] == ts


async def test_render_returns_end_ts(conn: AsyncConnection) -> None:
    iid = await _instrument_id(conn)
    await _seed_synthetic_candles(conn, iid, 30)
    result = await render_candle_chart(conn, iid, "TEST", span=10)
    assert result is not None
    _buf, end, end_ts = result
    assert end == 29
    assert end_ts is not None  # newest candle's tick timestamp


async def test_render_axis_ticks_still_works(conn: AsyncConnection) -> None:
    iid = await _instrument_id(conn)
    await _seed_synthetic_candles(conn, iid, 30)
    result = await render_candle_chart(
        conn, iid, "TEST", span=10, axis="ticks"
    )
    assert result is not None
    buf, _end, _ts = result
    assert buf.read()[:8] == b"\x89PNG\r\n\x1a\n"


def test_axis_formatter_time_labels() -> None:
    from datetime import UTC, datetime

    fmt = charts._axis_formatter(
        [datetime(2025, 1, 6, 14, 30, tzinfo=UTC)],
        [1234],
        "time",
    )
    assert fmt(0, 0) == "14:30"
    assert fmt(0.5, 0) == ""  # non-integer positions unlabeled
    assert fmt(5, 0) == ""  # out of range


def test_axis_formatter_marks_day_boundaries() -> None:
    """A multi-day window labels each new UTC day with the date -- a
    compressed overnight reads 16:00 -> 'Jan 07' rather than 16:00->09:30."""
    from datetime import UTC, datetime

    times = [
        datetime(2025, 1, 6, 15, 0, tzinfo=UTC),
        datetime(2025, 1, 6, 16, 0, tzinfo=UTC),
        datetime(2025, 1, 7, 9, 30, tzinfo=UTC),
    ]
    fmt = charts._axis_formatter(times, [10, 11, 12], "time")
    assert fmt(0, 0) == "Jan 06"  # first candle of a multi-day window
    assert fmt(1, 0) == "16:00"
    assert fmt(2, 0) == "Jan 07"  # day boundary gets the date


def test_axis_formatter_single_day_window() -> None:
    """Intraday windows stay HH:MM -- a date label would be noise."""
    from datetime import UTC, datetime

    times = [
        datetime(2025, 1, 6, 14, 0, tzinfo=UTC),
        datetime(2025, 1, 6, 14, 1, tzinfo=UTC),
    ]
    fmt = charts._axis_formatter(times, [10, 11], "time")
    assert fmt(0, 0) == "14:00"
    assert fmt(1, 0) == "14:01"


def test_axis_formatter_falls_back_to_tick() -> None:
    """Candles without a market_ticks row (synthetic data) show the tick."""
    from datetime import UTC, datetime

    fmt = charts._axis_formatter(
        [datetime(2025, 1, 6, 14, 0, tzinfo=UTC), None],
        [10, 11],
        "time",
    )
    assert fmt(0, 0) == "14:00"
    assert fmt(1, 0) == "11"


def test_axis_formatter_ticks_mode() -> None:
    fmt = charts._axis_formatter([None, None], [4323, 4324], "ticks")
    assert fmt(0, 0) == "4323"
    assert fmt(1, 0) == "4324"


def test_session_boundaries_detects_compressed_gap() -> None:
    """An open-open tick jump (compressed overnight/halt) draws a
    separator at the gap's left edge."""
    assert charts._session_boundaries([100, 101, 102, 580, 581], 1) == [2.5]


def test_session_boundaries_none_when_contiguous() -> None:
    assert charts._session_boundaries([10, 11, 12, 13], 1) == []
    assert charts._session_boundaries([], 1) == []
    assert charts._session_boundaries([7], 1) == []


def test_session_boundaries_respect_bucket() -> None:
    """Bucketed rows differ by `bucket` normally -- only bigger gaps are
    real boundaries."""
    assert charts._session_boundaries([100, 104, 108], 4) == []
    assert charts._session_boundaries([100, 104, 120], 4) == [1.5]


def test_session_boundaries_closed_tail_is_not_a_boundary() -> None:
    """The open->closed-tail transition is tick-adjacent (gap=1), so no
    separator -- the shading already marks it."""
    ticks = [956, 957, 958, 959, 960, 961, 962]
    assert charts._session_boundaries(ticks, 1) == []


def test_span_label() -> None:
    assert charts._span_label(60) == "1h"
    assert charts._span_label(240) == "4h"
    assert charts._span_label(960) == "1d"
    assert charts._span_label(4800) == "1w"
    assert charts._span_label(6720) == "~7d"  # max zoom-out
    assert charts._span_label(30) == "30t"  # sub-hour zoom-in
