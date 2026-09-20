"""Wires the pure math in `market/engine.py` to Postgres: one call per tick."""

from __future__ import annotations

import json
import math

from psycopg import AsyncConnection, sql
from psycopg.rows import dict_row

from stockbot.market import engine, events
from stockbot.seasons import service as seasons
from stockbot.shorts import service as shorts


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
                SELECT i.id, i.ticker, s.key AS sector_key, i.drift, i.sigma, i.beta, i.gamma,
                       i.kappa, i.fundamental_sigma, i.tau_ticks, i.base_price,
                       i.fundamental_value, i.impact, i.circuit_halted_until_tick
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

        # Events (earnings + news) use their own deterministic RNG stream so
        # they can't perturb the price engine's own draw sequence.
        events_rng = engine.rng_for_tick(f"{master_seed}|events", tick_index)
        await events.schedule_initial_earnings(conn, events_rng, tick_index)
        fundamentals = {row["id"]: float(row["fundamental_value"]) for row in instrument_rows}
        fundamentals = await events.resolve_due_events(conn, events_rng, tick_index, fundamentals)
        await events.maybe_create_news(conn, events_rng, tick_index, instrument_rows)

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
                fundamental_value=fundamentals[row["id"]],
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

        # Single-statement writes: executemany still round-trips per row,
        # which dominates tick latency (~100ms -> ~15ms measured on a local
        # Docker Postgres). One UPDATE ... FROM (VALUES ...) and one
        # multi-row INSERT collapse ~80 waits into 2.
        async with conn.cursor() as cur:
            if results:
                update_rows = sql.SQL(", ").join(
                    sql.SQL("({})").format(
                        sql.SQL(", ").join(sql.Placeholder() for _ in range(6))
                    )
                    for _ in results
                )
                await cur.execute(
                    sql.SQL(
                        """
                        UPDATE instruments i
                        SET base_price = v.base_price::numeric,
                            fundamental_value = v.fundamental_value::numeric,
                            impact = v.impact::numeric,
                            quoted_price = v.quoted_price::numeric,
                            circuit_halted_until_tick = v.circuit_halted_until_tick::bigint
                        FROM (VALUES {}) AS v(
                            base_price, fundamental_value, impact, quoted_price,
                            circuit_halted_until_tick, id
                        )
                        WHERE i.id = v.id::int
                        """
                    ).format(update_rows),
                    [
                        param
                        for result in results
                        for param in (
                            result.base_price,
                            result.fundamental_value,
                            result.impact,
                            result.quoted_price,
                            halts[result.id],
                            result.id,
                        )
                    ],
                )

            await cur.execute(
                """
                INSERT INTO market_ticks (tick_index, market_factor, sector_factors)
                VALUES (%s, %s, %s)
                """,
                (tick_index, market_factor, json.dumps(sector_factors)),
            )

            if results:
                candle_rows = sql.SQL(", ").join(
                    sql.SQL("({}, 0)").format(
                        sql.SQL(", ").join(sql.Placeholder() for _ in range(6))
                    )
                    for _ in results
                )
                await cur.execute(
                    sql.SQL(
                        """
                        INSERT INTO candles
                            (instrument_id, tick_index, open, high, low, close, volume)
                        VALUES {}
                        """
                    ).format(candle_rows),
                    [
                        param
                        for result in results
                        for param in (
                            result.id,
                            tick_index,
                            opens[result.id],
                            max(opens[result.id], result.quoted_price),
                            min(opens[result.id], result.quoted_price),
                            result.quoted_price,
                        )
                    ],
                )

        # Bounded shorts: knock out any position whose instrument reached its
        # knockout price this tick. Before season snapshots so closed shorts
        # stop contributing to equity.
        await shorts.sweep_knockouts(conn, tick_index)

        # Season lifecycle: activate due seasons, write day-boundary equity
        # snapshots, close finished seasons (all inside this tick's tx).
        await seasons.on_tick(conn, tick_index)

    return tick_index
