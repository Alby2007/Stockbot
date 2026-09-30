"""Chaos: scheduling, one-shot crash application, window multipliers,
Margin Call Monday, lifecycle idempotency."""

import pytest
from psycopg import AsyncConnection

from stockbot.chaos import service as chaos
from stockbot.margin.service import margin_config


async def _marks(conn: AsyncConnection) -> dict[int, float]:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT id, quoted_price FROM instruments "
            "WHERE is_active AND kind = 'STOCK' ORDER BY id"
        )
        return {int(r[0]): float(r[1]) for r in await cur.fetchall()}


async def test_flash_crash_moves_all_marks_once(conn: AsyncConnection) -> None:
    before = await _marks(conn)
    await chaos.schedule(
        conn, kind="FLASH_CRASH", start_tick=0, duration_ticks=10,
        payload={"pct": 0.05},
    )

    fired = await chaos.on_tick(conn, 0)

    assert fired == 1
    after = await _marks(conn)
    assert len(after) == len(before) > 0
    for iid, old in before.items():
        # NUMERIC(18,6) storage rounds the recomputed mark.
        assert after[iid] == pytest.approx(old * 0.95, rel=1e-5)
    # Idempotent: ACTIVE (not SCHEDULED) events don't re-apply; expiry
    # is the only remaining transition.
    again = await chaos.on_tick(conn, 1)
    assert again == 0
    still = await _marks(conn)
    assert still == after


async def test_vol_storm_window_multiplier(conn: AsyncConnection) -> None:
    await chaos.schedule(
        conn, kind="VOL_STORM", start_tick=10, duration_ticks=100,
        payload={"mult": 2.5},
    )
    assert await chaos.active_multiplier(conn, "VOL_STORM", 9) == 1.0
    await chaos.on_tick(conn, 10)
    assert await chaos.active_multiplier(conn, "VOL_STORM", 50) == pytest.approx(2.5)
    await chaos.on_tick(conn, 110)
    assert await chaos.active_multiplier(conn, "VOL_STORM", 110) == 1.0


async def test_fee_surge_folds_into_margin_config(conn: AsyncConnection) -> None:
    base = await margin_config(conn)
    base_rate = base["margin.borrow_fee_bps_per_tick"]
    await chaos.schedule(
        conn, kind="FEE_SURGE", start_tick=0, duration_ticks=1440,
        payload={"mult": 3.0},
    )
    await chaos.on_tick(conn, 0)
    # margin_config resolves "now" from market_ticks -- seed one inside
    # the event window (the test tx rolls it back).
    async with conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO market_ticks (tick_index, market_factor, sector_factors) "
            "VALUES (5, 0, '{}') ON CONFLICT (tick_index) DO NOTHING"
        )
    surged = await margin_config(conn)
    assert surged["margin.borrow_fee_bps_per_tick"] == base_rate * 3


async def test_margin_call_monday_autogen(conn: AsyncConnection) -> None:
    # chaos.mcm_day_mod default 4 -> day_index % 7 == 4.
    await chaos.autogen(conn, 4 * 1440)
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT kind, start_tick, end_tick, auto FROM chaos_events "
            "WHERE auto AND start_tick = %s",
            (4 * 1440,),
        )
        row = await cur.fetchone()
    assert row is not None
    assert row[0] == "FEE_SURGE" and row[1] == 4 * 1440
    # A different weekday stamps nothing (random roll can still fire --
    # assert only that no MCM row appears).
    async with conn.cursor() as cur:
        await cur.execute(
            "DELETE FROM chaos_events"
        )
        await cur.execute(
            "UPDATE config SET value = '0' WHERE key = 'chaos.auto_prob'"
        )
    assert await chaos.autogen(conn, 3 * 1440) == 0


async def test_kill_switch(conn: AsyncConnection) -> None:
    async with conn.cursor() as cur:
        await cur.execute("UPDATE config SET value = '0' WHERE key = 'chaos.enabled'")
    assert await chaos.active_multiplier(conn, "VOL_STORM", 10) == 1.0
    assert await chaos.on_tick(conn, 10) == 0
