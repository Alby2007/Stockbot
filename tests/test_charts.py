from __future__ import annotations

from psycopg import AsyncConnection

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
