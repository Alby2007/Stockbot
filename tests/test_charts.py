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
