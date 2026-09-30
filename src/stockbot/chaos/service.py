"""Scheduled chaos: regime events that bend the market's own rules.

`chaos_events` rows are the whole state machine. Three kinds:

- FLASH_CRASH -- applies its mark shift ONCE at activation (every active
  STOCK's impact term drops by ln(1-pct)); `end_tick` is bookkeeping, the
  crash is a point event with a feed announcement at both ends.
- VOL_STORM -- `apply_tick` folds `active_multiplier('VOL_STORM')` into
  each instrument's sigma_eff for the whole active window; sigma_eff is
  recomputed every tick, so expiry reverts cleanly.
- FEE_SURGE -- `margin_config` folds `active_multiplier('FEE_SURGE')`
  into `margin.borrow_fee_bps_per_tick`, so the borrow accrual AND the
  /margin rate display both show the surge for the window. Margin Call
  Monday is a weekly FEE_SURGE the scheduler stamps at its day boundary.

Multipliers are consumed at READ time (`active_multiplier` multiplies
ACTIVE rows whose window covers the latest tick) -- nothing is
materialized into `config`, so there's no stale key to unstick if the
process dies mid-event. Activation/expiry transitions are bookkeeping
plus feed announcements, all inside the tick's transaction.

`autogen` is deterministic: at each day boundary a seeded RNG decides
whether a chaos event lands `chaos.lead_ticks` out, so replays schedule
the same calendar.
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass
from typing import Any

from psycopg import AsyncConnection
from psycopg.rows import dict_row

from stockbot.feed import emit_feed
from stockbot.market.engine import TICKS_PER_DAY

log = logging.getLogger("stockbot.chaos")


@dataclass(frozen=True)
class ChaosEvent:
    id: int
    kind: str
    status: str
    start_tick: int
    end_tick: int
    payload: dict[str, Any]
    auto: bool


def _from_row(row: dict[str, Any]) -> ChaosEvent:
    return ChaosEvent(
        id=int(row["id"]),
        kind=str(row["kind"]),
        status=str(row["status"]),
        start_tick=int(row["start_tick"]),
        end_tick=int(row["end_tick"]),
        payload=dict(row["payload"] or {}),
        auto=bool(row["auto"]),
    )


async def _config_float(conn: AsyncConnection, key: str, default: float) -> float:
    async with conn.cursor() as cur:
        await cur.execute("SELECT value FROM config WHERE key = %s", (key,))
        row = await cur.fetchone()
    return float(row[0]) if row else default


async def _enabled(conn: AsyncConnection) -> bool:
    return await _config_float(conn, "chaos.enabled", 1.0) != 0.0


async def active_multiplier(
    conn: AsyncConnection, kind: str, tick_index: int | None = None
) -> float:
    """Product of `payload.mult` over ACTIVE events of `kind` whose window
    covers `tick_index` (or the latest tick when None -- margin_config
    doesn't carry one). Called from hot paths -- one indexed aggregate,
    no materialized config key to go stale."""
    if not await _enabled(conn):
        return 1.0
    async with conn.cursor() as cur:
        await cur.execute(
            """
            SELECT COALESCE(EXP(SUM(LN((payload->>'mult')::numeric))), 1.0)
            FROM chaos_events
            WHERE status = 'ACTIVE' AND kind = %s
              AND start_tick <= COALESCE(
                      %s, (SELECT MAX(tick_index) FROM market_ticks))
              AND end_tick > COALESCE(
                      %s, (SELECT MAX(tick_index) FROM market_ticks))
            """,
            (kind, tick_index, tick_index),
        )
        row = await cur.fetchone()
    return float(row[0]) if row and row[0] is not None else 1.0


async def schedule(
    conn: AsyncConnection,
    *,
    kind: str,
    start_tick: int,
    duration_ticks: int,
    payload: dict[str, Any] | None = None,
    auto: bool = False,
) -> int:
    """Queue a chaos event. Runs inside the caller's transaction."""
    import json

    async with conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO chaos_events (kind, start_tick, end_tick, payload, auto)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING id
            """,
            (kind, start_tick, start_tick + duration_ticks,
             json.dumps(payload or {}), auto),
        )
        row = await cur.fetchone()
        assert row is not None
        return int(row[0])


async def _activate(conn: AsyncConnection, ev: ChaosEvent, tick_index: int) -> None:
    """Apply the activation effect + feed post."""
    if ev.kind == "FLASH_CRASH":
        pct = float(ev.payload.get("pct", 0.03))
        async with conn.cursor() as cur:
            # SET clauses see the OLD column values: impact becomes
            # old+ln(1-pct), quoted recomputes off base*new impact in the
            # same statement. Indexes derive from their basket -- only
            # tradable STOCKs take the shock.
            await cur.execute(
                """
                UPDATE instruments
                SET impact = impact + LN(1 - %s),
                    quoted_price = base_price * EXP(impact + LN(1 - %s))
                WHERE is_active AND kind = 'STOCK'
                """,
                (pct, pct),
            )
        await emit_feed(
            conn,
            "CHAOS_FLASH_CRASH",
            {"pct": pct, "event_id": ev.id},
            tick_index=tick_index,
        )
    elif ev.kind == "VOL_STORM":
        await emit_feed(
            conn,
            "CHAOS_VOL_STORM",
            {"mult": float(ev.payload.get("mult", 2.0)),
             "end_tick": ev.end_tick, "event_id": ev.id},
            tick_index=tick_index,
        )
    elif ev.kind == "FEE_SURGE":
        await emit_feed(
            conn,
            "CHAOS_FEE_SURGE",
            {"mult": float(ev.payload.get("mult", 2.0)),
             "end_tick": ev.end_tick, "event_id": ev.id},
            tick_index=tick_index,
        )


async def autogen(conn: AsyncConnection, tick_index: int) -> int:
    """Day-boundary scheduler: Margin Call Monday on its configured day,
    plus a seeded random roll for flash crashes and storms. Deterministic
    per day_index."""
    if not await _enabled(conn):
        return 0
    if tick_index % TICKS_PER_DAY != 0 or tick_index == 0:
        return 0
    day_index = tick_index // TICKS_PER_DAY
    made = 0

    # Margin Call Monday: borrow fees surge for the day.
    mcm_day = int(await _config_float(conn, "chaos.mcm_day_mod", 4))
    if (
        await _config_float(conn, "chaos.mcm_enabled", 1.0) != 0.0
        and day_index % 7 == mcm_day
    ):
        await schedule(
            conn,
            kind="FEE_SURGE",
            start_tick=tick_index,
            duration_ticks=TICKS_PER_DAY,
            payload={
                "mult": await _config_float(conn, "chaos.fee_mult", 2.0),
                "label": "Margin Call Monday",
            },
            auto=True,
        )
        made += 1

    # The seeded random roll: one shot per day, lands `lead_ticks` out.
    prob = await _config_float(conn, "chaos.auto_prob", 0.20)
    rng = random.Random(f"chaos-{day_index}")
    if rng.random() < prob:
        lead = int(await _config_float(conn, "chaos.lead_ticks", 2880))
        storm_ticks = int(await _config_float(conn, "chaos.storm_ticks", 1440))
        pick = rng.choice(["FLASH_CRASH", "VOL_STORM", "FEE_SURGE"])
        if pick == "FLASH_CRASH":
            payload: dict[str, Any] = {
                "pct": await _config_float(conn, "chaos.crash_pct", 0.03)
            }
            dur = storm_ticks
        elif pick == "VOL_STORM":
            payload = {"mult": await _config_float(conn, "chaos.vol_mult", 2.0)}
            dur = storm_ticks
        else:
            payload = {"mult": await _config_float(conn, "chaos.fee_mult", 2.0)}
            dur = storm_ticks
        await schedule(
            conn,
            kind=pick,
            start_tick=tick_index + lead,
            duration_ticks=dur,
            payload=payload,
            auto=True,
        )
        made += 1
    return made


async def on_tick(conn: AsyncConnection, tick_index: int) -> int:
    """Lifecycle sweep: activate due events (applying one-shot effects),
    expire finished windows, autogen at the day boundary. Runs in both
    tick phases -- a scheduled crash doesn't wait for the session to
    reopen (the mark just gaps, which is the point of a crash)."""
    if not await _enabled(conn):
        return 0
    fired = 0
    async with conn.transaction():
        made = await autogen(conn, tick_index)
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                UPDATE chaos_events SET status = 'ACTIVE'
                WHERE status = 'SCHEDULED' AND start_tick <= %s
                RETURNING *
                """,
                (tick_index,),
            )
            activating = [_from_row(r) for r in await cur.fetchall()]
        for ev in activating:
            await _activate(conn, ev, tick_index)
        fired += len(activating)
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                UPDATE chaos_events SET status = 'EXPIRED'
                WHERE status = 'ACTIVE' AND end_tick <= %s
                RETURNING id, kind
                """,
                (tick_index,),
            )
            expiring = await cur.fetchall()
        for row in expiring:
            await emit_feed(
                conn,
                "CHAOS_ENDED",
                {"kind": str(row["kind"]), "event_id": int(row["id"])},
                tick_index=tick_index,
            )
        fired += len(expiring)
    return made + fired
