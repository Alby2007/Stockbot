"""Moments: the market mints its own commemoratives.

`sweep_moments` runs inside apply_tick's open-tick transaction and
reads only what the tick already computed (results / opens /
rows_by_id / flow_breached) -- detection is pure in-memory and costs
zero extra queries; writes happen only on a trigger.

Detectors (config-gated via `moment.*`):
  MOVER      -- |close/open - 1| >= moment.mover_pct in one tick.
  MASS_HALT  -- >= moment.mass_halt_min fresh circuit/flow halts in one
                tick (model breaches AND flow-attributed halts).
  RECORD     -- close >= moment.record_multiple x the instrument's
                prior all-time mark (rows_by_id carries the pre-tick
                high_water_price; the GREATEST update has already run).

Witnesses = DISTINCT holders of the affected instrument(s) in the main
economy (positions.season_id IS NULL -- league positions don't count).
Award rides `grant_commemorative` (idempotent), DMs through
'MOMENT_EARNED', and every mint broadcasts 'MOMENT_MINTED' to the feed.
`moment.max_per_tick` caps mints on chaotic ticks: priority order
MASS_HALT < RECORD < MOVER, movers ranked by |move|.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from psycopg import AsyncConnection

from stockbot.collectibles.service import grant_commemorative
from stockbot.feed import emit_feed

log = logging.getLogger("stockbot.collectibles.moments")


@dataclass(frozen=True)
class MomentTrigger:
    rule: str  # MOVER | MASS_HALT | RECORD
    instrument_id: int | None
    name: str
    flavor: str
    payload: dict[str, Any]
    witness_iids: list[int]
    priority: int  # lower survives max_per_tick first


async def _moment_config(conn: AsyncConnection) -> dict[str, float]:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT key, value FROM config WHERE key LIKE 'moment.%'"
        )
        return {
            str(r[0])[len("moment."):]: float(r[1]) for r in await cur.fetchall()
        }


async def sweep_moments(
    conn: AsyncConnection,
    tick_index: int,
    *,
    results: list[Any],
    opens: dict[int, float],
    flow_breached: set[int],
    rows_by_id: dict[int, dict[str, Any]],
) -> int:
    """Detect + mint moments for this tick. Returns cards minted."""
    cfg = await _moment_config(conn)
    if cfg.get("enabled", 1.0) == 0.0:
        return 0
    mover_pct = float(cfg.get("mover_pct", 12.0)) / 100
    record_mult = float(cfg.get("record_multiple", 2.0))
    mass_halt_min = int(cfg.get("mass_halt_min", 5))
    max_per_tick = int(cfg.get("max_per_tick", 3))

    triggers: list[MomentTrigger] = []
    fresh_halts: list[int] = [
        int(r.id) for r in results if r.circuit_breached
    ] + [int(i) for i in flow_breached]

    for res in results:
        row = rows_by_id.get(res.id)
        if row is None:
            continue
        ticker = str(row["ticker"])
        name_long = str(row.get("name") or ticker)
        open_mark = opens.get(res.id)
        if open_mark and open_mark > 0:
            move = res.quoted_price / open_mark - 1
            if abs(move) >= mover_pct:
                pct = move * 100
                word = "Spike" if move > 0 else "Crater"
                triggers.append(
                    MomentTrigger(
                        rule="MOVER",
                        instrument_id=res.id,
                        name=f"The {ticker} {word}",
                        flavor=(
                            f"{name_long} moved {pct:+.1f}% in a single tick. "
                            "Everyone holding it that minute owns the print."
                        ),
                        payload={"ticker": ticker, "move_pct": round(pct, 2)},
                        witness_iids=[res.id],
                        priority=2,
                    )
                )
        hw = row.get("high_water_price")
        if hw is not None and float(hw) > 0:
            if res.quoted_price >= float(hw) * record_mult:
                mult = res.quoted_price / float(hw)
                triggers.append(
                    MomentTrigger(
                        rule="RECORD",
                        instrument_id=res.id,
                        name=f"The {ticker} Record",
                        flavor=(
                            f"{name_long} printed {mult:.1f}x its all-time "
                            "high in one tick. History books, rewritten."
                        ),
                        payload={
                            "ticker": ticker,
                            "record_x": round(mult, 2),
                        },
                        witness_iids=[res.id],
                        priority=1,
                    )
                )

    halted_ids = sorted(set(fresh_halts))
    if len(halted_ids) >= mass_halt_min:
        triggers.append(
            MomentTrigger(
                rule="MASS_HALT",
                instrument_id=None,
                name="The Freeze",
                flavor=(
                    f"{len(halted_ids)} names locked at once on tick "
                    f"{tick_index}; the tape stopped breathing."
                ),
                payload={"halted": len(halted_ids), "tick": tick_index},
                witness_iids=halted_ids,
                priority=0,
            )
        )

    triggers.sort(key=lambda t: t.priority)
    minted = 0
    for trig in triggers[:max_per_tick]:
        minted += await _mint_and_award(conn, trig, tick_index)
    if len(triggers) > max_per_tick:
        log.info(
            "tick=%d moments capped: %d triggers, %d minted",
            tick_index, len(triggers), minted,
        )
    return minted


async def _mint_and_award(
    conn: AsyncConnection, trig: MomentTrigger, tick_index: int
) -> int:
    """Mint the card + moments row, grant to witnesses, feed + DM."""
    async with conn.cursor() as cur:
        # Dedup first: a moment for (rule, instrument, tick) mints once.
        await cur.execute(
            "SELECT 1 FROM moments WHERE rule = %s AND tick_index = %s"
            " AND COALESCE(instrument_id, 0) = %s",
            (trig.rule, tick_index, trig.instrument_id or 0),
        )
        if await cur.fetchone() is not None:
            return 0
        await cur.execute("SELECT nextval('moment_card_seq')")
        seq_row = await cur.fetchone()
        assert seq_row is not None
        card_key = f"moment_{int(seq_row[0])}"
        await cur.execute(
            """
            INSERT INTO cards (key, set_key, kind, name, flavor, metadata)
            VALUES (%s, 'moments', 'COMMEMORATIVE', %s, %s, %s)
            """,
            (
                card_key,
                trig.name,
                trig.flavor,
                json.dumps({**trig.payload, "rule": trig.rule, "tick": tick_index}),
            ),
        )
        await cur.execute(
            """
            INSERT INTO moments
                (rule, card_key, instrument_id, tick_index, payload)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (rule, COALESCE(instrument_id, 0), tick_index)
            DO NOTHING
            RETURNING id
            """,
            (
                trig.rule,
                card_key,
                trig.instrument_id,
                tick_index,
                json.dumps(trig.payload),
            ),
        )
        if await cur.fetchone() is None:
            # Lost the race -- another tx minted this moment; drop the
            # card row we just made so nothing orphans.
            await cur.execute("DELETE FROM cards WHERE key = %s", (card_key,))
            return 0
        # Witnesses: main-economy holders with nonzero exposure.
        await cur.execute(
            """
            SELECT DISTINCT user_id FROM positions
            WHERE instrument_id = ANY(%s)
              AND quantity <> 0 AND season_id IS NULL
            """,
            (trig.witness_iids,),
        )
        witnesses = [int(r[0]) for r in await cur.fetchall()]

    awarded = 0
    for uid in witnesses:
        if await grant_commemorative(conn, uid, card_key):
            awarded += 1
            async with conn.cursor() as cur:
                await cur.execute(
                    "INSERT INTO notifications (user_id, kind, payload)"
                    " VALUES (%s, 'MOMENT_EARNED', %s)",
                    (uid, json.dumps({"name": trig.name, "card": card_key})),
                )
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE moments SET awarded_count = %s WHERE card_key = %s",
            (awarded, card_key),
        )
    # Moments are market history, not user achievement: no subject
    # mention, just the tape entry.
    await emit_feed(
        conn,
        "MOMENT_MINTED",
        {**trig.payload, "name": trig.name, "awarded": awarded},
        tick_index=tick_index,
    )
    return 1
