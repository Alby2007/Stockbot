"""Wires the pure math in `market/engine.py` to Postgres: one call per tick."""

from __future__ import annotations

import json
import math

from psycopg import AsyncConnection
from psycopg.rows import dict_row

from stockbot.market import engine


async def apply_tick(conn: AsyncConnection, master_seed: str) -> int:
    """Advance the market by exactly one tick. Returns the tick index applied.

    Must only ever be called by the singleton market process (see
    `market/main.py`'s advisory lock) -- there is no locking here against a
    concurrent second tick, only against concurrent trades touching the same
    instruments.
    """
    async with conn.transaction():
        async with conn.cursor() as cur:
            await cur.execute("SELECT COALESCE(MAX(tick_index), -1) + 1 FROM market_ticks")
            next_tick_row = await cur.fetchone()
            assert next_tick_row is not None
            tick_index: int = next_tick_row[0]

        async with conn.cursor(row_factory=dict_row) as cur:
            # Lock ordering rule: instruments before accounts, sorted by id.
            # No accounts are touched here, so this is just instruments-by-id.
            await cur.execute(
                """
                SELECT i.id, s.key AS sector_key, i.drift, i.sigma, i.beta, i.gamma, i.kappa,
                       i.fundamental_sigma, i.tau_ticks, i.base_price, i.fundamental_value,
                       i.impact, i.circuit_halted_until_tick
                FROM instruments i
                JOIN sectors s ON s.id = i.sector_id
                WHERE i.is_active
                ORDER BY i.id
                FOR UPDATE OF i
                """
            )
            instrument_rows = await cur.fetchall()

        sector_keys = sorted({row["sector_key"] for row in instrument_rows})
        rng = engine.rng_for_tick(master_seed, tick_index)
        market_factor = engine.draw_market_factor(rng)
        sector_factors = engine.draw_sector_factors(rng, sector_keys)

        results: list[engine.InstrumentTickResult] = []
        halts: dict[int, int | None] = {}
        opens: dict[int, float] = {}

        for row in instrument_rows:
            state = engine.InstrumentState(
                id=row["id"],
                sector_key=row["sector_key"],
                drift=float(row["drift"]),
                sigma=float(row["sigma"]),
                beta=float(row["beta"]),
                gamma=float(row["gamma"]),
                kappa=float(row["kappa"]),
                fundamental_sigma=float(row["fundamental_sigma"]),
                tau_ticks=float(row["tau_ticks"]),
                base_price=float(row["base_price"]),
                fundamental_value=float(row["fundamental_value"]),
                impact=float(row["impact"]),
            )
            opens[state.id] = state.base_price * math.exp(state.impact)

            still_halted = (
                row["circuit_halted_until_tick"] is not None
                and row["circuit_halted_until_tick"] > tick_index
            )
            if still_halted:
                result = engine.freeze_instrument(state)
                halts[state.id] = row["circuit_halted_until_tick"]
            else:
                result = engine.step_instrument(
                    rng, state, market_factor, sector_factors[state.sector_key]
                )
                halts[state.id] = (
                    tick_index + engine.CIRCUIT_HALT_TICKS if result.circuit_breached else None
                )
            results.append(result)

        async with conn.cursor() as cur:
            for result in results:
                await cur.execute(
                    """
                    UPDATE instruments
                    SET base_price = %s,
                        fundamental_value = %s,
                        impact = %s,
                        quoted_price = %s,
                        circuit_halted_until_tick = %s
                    WHERE id = %s
                    """,
                    (
                        result.base_price,
                        result.fundamental_value,
                        result.impact,
                        result.quoted_price,
                        halts[result.id],
                        result.id,
                    ),
                )

            await cur.execute(
                """
                INSERT INTO market_ticks (tick_index, market_factor, sector_factors)
                VALUES (%s, %s, %s)
                """,
                (tick_index, market_factor, json.dumps(sector_factors)),
            )

            for result in results:
                open_price = opens[result.id]
                await cur.execute(
                    """
                    INSERT INTO candles (instrument_id, tick_index, open, high, low, close, volume)
                    VALUES (%s, %s, %s, %s, %s, %s, 0)
                    """,
                    (
                        result.id,
                        tick_index,
                        open_price,
                        max(open_price, result.quoted_price),
                        min(open_price, result.quoted_price),
                        result.quoted_price,
                    ),
                )

    return tick_index
