from __future__ import annotations

from psycopg import AsyncConnection

from stockbot.market import engine
from stockbot.market.tick import apply_tick


async def _instrument_count(conn: AsyncConnection) -> int:
    async with conn.cursor() as cur:
        await cur.execute("SELECT COUNT(*) FROM instruments WHERE is_active")
        (count,) = await cur.fetchone()
    return count


async def test_apply_tick_writes_a_candle_per_instrument(conn: AsyncConnection) -> None:
    tick_index = await apply_tick(conn, "test-master-seed")
    assert tick_index == 0

    async with conn.cursor() as cur:
        await cur.execute("SELECT COUNT(*) FROM candles WHERE tick_index = %s", (tick_index,))
        (candle_count,) = await cur.fetchone()
    assert candle_count == await _instrument_count(conn)

    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT market_factor, sector_factors FROM market_ticks WHERE tick_index = %s",
            (tick_index,),
        )
        row = await cur.fetchone()
    assert row is not None


async def test_apply_tick_increments_tick_index_each_call(conn: AsyncConnection) -> None:
    first = await apply_tick(conn, "test-master-seed")
    second = await apply_tick(conn, "test-master-seed")
    assert second == first + 1


async def test_apply_tick_is_deterministic_given_the_same_seed_and_starting_state(
    conn: AsyncConnection,
) -> None:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT id, base_price, fundamental_value FROM instruments ORDER BY id LIMIT 1"
        )
        instrument_id, base_price_before, fundamental_before = await cur.fetchone()

    await apply_tick(conn, "deterministic-seed")

    async with conn.cursor() as cur:
        await cur.execute("SELECT quoted_price FROM instruments WHERE id = %s", (instrument_id,))
        (quoted_after_first_run,) = await cur.fetchone()

    # Reset that instrument back to its pre-tick state and the tick ledger,
    # then replay the same tick index with the same seed. vol_state/sigma_eff
    # are engine inputs now too -- the reset must restore them or the replay
    # steps with the regime-scaled sigma the first run left behind.
    async with conn.cursor() as cur:
        await cur.execute(
            """
            UPDATE instruments
            SET base_price = %s, fundamental_value = %s, impact = 0, quoted_price = %s,
                vol_state = 1.0, sigma_eff = NULL, drift_state = 0
            WHERE id = %s
            """,
            (base_price_before, fundamental_before, base_price_before, instrument_id),
        )
        await cur.execute("DELETE FROM candles")
        await cur.execute("DELETE FROM market_ticks")
        await cur.execute("DELETE FROM events")

    await apply_tick(conn, "deterministic-seed")

    async with conn.cursor() as cur:
        await cur.execute("SELECT quoted_price FROM instruments WHERE id = %s", (instrument_id,))
        (quoted_after_replay,) = await cur.fetchone()

    assert quoted_after_first_run == quoted_after_replay


async def test_circuit_breaker_halts_and_then_resumes(conn: AsyncConnection) -> None:
    async with conn.cursor() as cur:
        await cur.execute("SELECT id FROM instruments ORDER BY id LIMIT 1")
        (instrument_id,) = await cur.fetchone()
        # Force a guaranteed breach: drift=1 gives a deterministic
        # delta_log_price way past the circuit cap (and stays within the
        # instruments_engine_params_sane bounds, unlike absurd sigma).
        await cur.execute("UPDATE instruments SET drift = 1 WHERE id = %s", (instrument_id,))

    tick_index = await apply_tick(conn, "halt-test-seed")

    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT circuit_halted_until_tick, base_price FROM instruments WHERE id = %s",
            (instrument_id,),
        )
        halted_until, base_price_after_halt = await cur.fetchone()
    assert halted_until is not None
    assert halted_until > tick_index

    # While halted, price must not move at all, even though sigma is still huge.
    await apply_tick(conn, "halt-test-seed")
    async with conn.cursor() as cur:
        await cur.execute("SELECT base_price FROM instruments WHERE id = %s", (instrument_id,))
        (base_price_still_halted,) = await cur.fetchone()
    assert base_price_still_halted == base_price_after_halt

    # Once the halt window passes, the column should clear and pricing resumes.
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE instruments SET drift = 0, sigma = 0.0001 WHERE id = %s", (instrument_id,)
        )
    for _ in range(10):
        await apply_tick(conn, "halt-test-seed")

    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT circuit_halted_until_tick FROM instruments WHERE id = %s", (instrument_id,)
        )
        (halted_until_after,) = await cur.fetchone()
    assert halted_until_after is None


async def test_circuit_breaker_freezes_exactly_circuit_halt_ticks(conn: AsyncConnection) -> None:
    """A breach at tick T must freeze exactly CIRCUIT_HALT_TICKS ticks
    (T+1 .. T+N) before the instrument steps again."""
    async with conn.cursor() as cur:
        await cur.execute("SELECT id FROM instruments ORDER BY id LIMIT 1")
        (instrument_id,) = await cur.fetchone()
        # Force a guaranteed breach via drift (deterministic and in-bounds).
        await cur.execute("UPDATE instruments SET drift = 1 WHERE id = %s", (instrument_id,))

    await apply_tick(conn, "halt-length-seed")

    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE instruments SET drift = 0, sigma = 0.0001 WHERE id = %s", (instrument_id,)
        )
        await cur.execute(
            "SELECT base_price FROM instruments WHERE id = %s", (instrument_id,)
        )
        (base_at_halt,) = await cur.fetchone()

    frozen = 0
    for _ in range(engine.CIRCUIT_HALT_TICKS + 2):
        await apply_tick(conn, "halt-length-seed")
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT base_price FROM instruments WHERE id = %s", (instrument_id,)
            )
            (price,) = await cur.fetchone()
        if price == base_at_halt:
            frozen += 1
        else:
            break

    assert frozen == engine.CIRCUIT_HALT_TICKS
