from __future__ import annotations

from psycopg import AsyncConnection

from stockbot.market.data import all_instrument_snapshots, get_instrument_snapshot
from stockbot.market.tick import apply_tick


async def test_snapshots_have_no_day_change_before_any_ticks(conn: AsyncConnection) -> None:
    snapshots = await all_instrument_snapshots(conn)
    assert len(snapshots) == 41  # 40 stocks + SBX40 index
    assert all(s.day_change_pct is None for s in snapshots)


async def test_snapshot_reflects_a_tick(conn: AsyncConnection) -> None:
    await apply_tick(conn, "data-test-seed")
    snapshot = await get_instrument_snapshot(conn, "nort")  # lowercase should work
    assert snapshot is not None
    assert snapshot.ticker == "NORT"
    # With only one tick of history, "1 day ago" falls back to the earliest
    # candle, i.e. the same tick -- so change should be exactly zero.
    assert snapshot.day_change_pct == 0.0


async def test_unknown_ticker_returns_none(conn: AsyncConnection) -> None:
    assert await get_instrument_snapshot(conn, "NOPE") is None
