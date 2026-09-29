"""DB layer for collectibles: pack opening, collection reads, crafting.

Shard economics (C9): shards live on `users.shards`, OFF the
double-entry ledger -- the ledger is for money, shards are a
non-convertible vanity material. One source (duplicate burns), one sink
(crafting/upgrading). The ledger audit stays untouched because no shard
path ever posts a transfer.

Pull auditability (C2): every draw writes a `card_pulls` row carrying
pull_seq + pity_count_before + a `pull_cfg` snapshot of the weights,
pools, pity threshold, and per-pull floor it consumed, so
`resolve_pull` replays byte-for-byte from recorded inputs alone --
retunes and set expansions can't invalidate history.

Shard auditability: every mutation of `users.shards` writes a signed
`shard_events` row (awards = +delta 'DUPLICATE_BURN', spends = -delta
'CRAFT'/'FRAME_UPGRADE'), making `users.shards = SUM(shard_events.delta)`
a checkable invariant.
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass
from typing import Any

from psycopg import AsyncConnection
from psycopg.rows import dict_row

from stockbot.collectibles.pull import (
    FRAME_RANK,
    classify,
    part_seed,
    pull_seed,
    resolve_pull,
)
from stockbot.feed import emit_feed
from stockbot.ledger.service import record_idempotency_key
from stockbot.market.engine import TICKS_PER_DAY, session_phase
from stockbot.shop.errors import ShopError
from stockbot.shop.service import use_consumable


class PackError(ShopError):
    """Non-consumable pack key, empty pack pool, or other pack failure."""


@dataclass(frozen=True)
class PackConfig:
    tier_weights: dict[str, float]
    shard_values: dict[str, int]  # lowercase frame name -> shards
    craft_costs: dict[str, int]  # lowercase frame name -> shards
    pity_threshold: int
    part_chance: float  # per-pack bonus part-roll probability


@dataclass(frozen=True)
class PullOutcome:
    pull_seq: int
    tier: str
    card_key: str
    name: str
    outcome: str  # NEW | UPGRADE | DUPLICATE
    shards: int
    best_frame: str  # frame now held after the pull
    copies: int
    serial: int  # per-card mint number of this pull
    stamps: tuple[str, ...]  # market context the pull printed in


@dataclass(frozen=True)
class CardRow:
    key: str
    set_key: str
    kind: str
    name: str
    flavor: str
    sector_id: int | None
    rarity: str | None
    instrument_id: int | None


async def _config_map(conn: AsyncConnection, prefix: str) -> dict[str, float]:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT key, value FROM config WHERE key LIKE %s", (f"{prefix}%",)
        )
        return {str(r[0])[len(prefix):]: float(r[1]) for r in await cur.fetchall()}


async def load_pack_config(conn: AsyncConnection) -> PackConfig:
    rates = await _config_map(conn, "pack.rate_")
    shards = await _config_map(conn, "pack.shards_")
    crafts = await _config_map(conn, "pack.craft_")
    async with conn.cursor() as cur:
        await cur.execute("SELECT value FROM config WHERE key = 'pack.pity_threshold'")
        row = await cur.fetchone()
        await cur.execute("SELECT value FROM config WHERE key = 'pack.part_chance'")
        chance_row = await cur.fetchone()
    tier_weights = {k.upper(): v for k, v in rates.items()}
    if sum(tier_weights.values()) <= 0:
        # An all-zero rate set silently mints LEGENDARY on every pull;
        # refuse the config rather than the pull.
        raise PackError("pack.rate_* weights sum to zero — fix via /admin tune")
    return PackConfig(
        tier_weights=tier_weights,
        shard_values={k: int(v) for k, v in shards.items()},
        craft_costs={k: int(v) for k, v in crafts.items()},
        pity_threshold=int(row[0]) if row else 20,
        part_chance=float(chance_row[0]) if chance_row else 0.35,
    )


async def load_part_pool(conn: AsyncConnection) -> list[str]:
    """Ordered PART-card keys eligible for the per-pack bonus roll.
    Same ordering contract as load_pools: the randrange index IS the
    outcome, so the list must be sorted."""
    async with conn.cursor() as cur:
        await cur.execute(
            """
            SELECT c.key FROM cards c
            JOIN card_sets s ON s.key = c.set_key
            WHERE s.in_pack_pool AND c.kind = 'PART'
            ORDER BY c.key
            """
        )
        return [str(r[0]) for r in await cur.fetchall()]


async def load_pools(conn: AsyncConnection) -> dict[str, list[str]]:
    """Tier -> ordered card-key lists for pack pools (C5). ORDER BY key
    is mandatory -- resolve_pull's randrange indexes into these lists,
    so pool ordering IS the pull outcome."""
    pools: dict[str, list[str]] = {"INSTRUMENT": [], "EPIC": [], "LEGENDARY": []}
    async with conn.cursor() as cur:
        await cur.execute(
            """
            SELECT c.key, c.rarity FROM cards c
            JOIN card_sets s ON s.key = c.set_key
            WHERE s.in_pack_pool AND c.kind = 'INSTRUMENT'
            ORDER BY c.key
            """
        )
        pools["INSTRUMENT"] = [str(r[0]) for r in await cur.fetchall()]
        await cur.execute(
            """
            SELECT c.key, c.rarity FROM cards c
            JOIN card_sets s ON s.key = c.set_key
            WHERE s.in_pack_pool AND c.kind = 'LORE'
            ORDER BY c.key
            """
        )
        for key, rarity in await cur.fetchall():
            pools[str(rarity)].append(str(key))
    return pools


async def get_card(conn: AsyncConnection, key: str) -> CardRow | None:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            SELECT key, set_key, kind, name, flavor, sector_id, rarity, instrument_id
            FROM cards WHERE key = %s
            """,
            (key,),
        )
        row = await cur.fetchone()
    if row is None:
        return None
    return CardRow(
        key=str(row["key"]),
        set_key=str(row["set_key"]),
        kind=str(row["kind"]),
        name=str(row["name"]),
        flavor=str(row["flavor"]),
        sector_id=row["sector_id"],
        rarity=row["rarity"],
        instrument_id=row["instrument_id"],
    )


async def find_card(
    conn: AsyncConnection, name_or_key: str
) -> CardRow | None:
    """Resolve a card by key ('card_nort', 'lore_whale'), exact
    case-insensitive name, or ticker for /card autocomplete targets."""
    q = name_or_key.lower()
    row = await get_card(conn, q)
    if row is not None:
        return row
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            SELECT key, set_key, kind, name, flavor, sector_id, rarity, instrument_id
            FROM cards
            WHERE lower(name) = %s
               OR lower(key) = %s
               OR (kind = 'INSTRUMENT' AND instrument_id = (
                     SELECT id FROM instruments WHERE lower(ticker) = %s))
            ORDER BY key LIMIT 1
            """,
            (q, q, q),
        )
        r = await cur.fetchone()
    if r is None:
        return None
    return CardRow(
        key=str(r["key"]),
        set_key=str(r["set_key"]),
        kind=str(r["kind"]),
        name=str(r["name"]),
        flavor=str(r["flavor"]),
        sector_id=r["sector_id"],
        rarity=r["rarity"],
        instrument_id=r["instrument_id"],
    )


async def list_collection(
    conn: AsyncConnection, user_id: int
) -> list[dict[str, Any]]:
    """All held cards with catalog detail, ordered for the binder:
    set, then sector, then card. best_serial/best_stamps describe the
    copy whose frame is held; first_edition when its pull predates the
    set's rotation (rotated_tick NULL = still in print -> never 1st ed).
    """
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            SELECT c.key, c.name, c.kind, c.set_key, c.sector_id, c.rarity,
                   s.name AS sector_name,
                   uc.best_frame, uc.copies, uc.best_serial,
                   COALESCE(bp.stamps, '{}') AS best_stamps,
                   (cs.rotated_tick IS NOT NULL
                    AND bp.tick_index IS NOT NULL
                    AND bp.tick_index < cs.rotated_tick) AS first_edition
            FROM user_cards uc
            JOIN cards c ON c.key = uc.card_key
            JOIN card_sets cs ON cs.key = c.set_key
            LEFT JOIN sectors s ON s.id = c.sector_id
            LEFT JOIN card_pulls bp
                 ON bp.user_id = uc.user_id
                AND bp.card_key = uc.card_key
                AND bp.serial = uc.best_serial
            WHERE uc.user_id = %s
            ORDER BY c.set_key, c.kind, c.sector_id NULLS LAST, c.key
            """,
            (user_id,),
        )
        return [dict(r) for r in await cur.fetchall()]


async def collection_stats(
    conn: AsyncConnection, user_id: int
) -> dict[str, int]:
    """Binder header numbers: collected vs set size, frame histogram."""
    stats: dict[str, int] = {"held": 0, "instrument_total": 0, "lore_total": 0, "lore_held": 0}
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT count(*) FROM cards WHERE kind = 'INSTRUMENT'"
        )
        stats["instrument_total"] = int((await cur.fetchone() or (0,))[0])
        await cur.execute("SELECT count(*) FROM cards WHERE kind = 'LORE'")
        stats["lore_total"] = int((await cur.fetchone() or (0,))[0])
        await cur.execute(
            """
            SELECT uc.best_frame, count(*) FROM user_cards uc
            JOIN cards c ON c.key = uc.card_key
            WHERE uc.user_id = %s AND c.kind = 'INSTRUMENT'
            GROUP BY uc.best_frame
            """,
            (user_id,),
        )
        for frame, n in await cur.fetchall():
            stats[f"frame_{str(frame).lower()}"] = int(n)
            stats["held"] += int(n)
        await cur.execute(
            """
            SELECT count(*) FROM user_cards uc
            JOIN cards c ON c.key = uc.card_key
            WHERE uc.user_id = %s AND c.kind = 'LORE'
            """,
            (user_id,),
        )
        stats["lore_held"] = int((await cur.fetchone() or (0,))[0])
        await cur.execute(
            "SELECT shards FROM users WHERE id = %s", (user_id,)
        )
        row = await cur.fetchone()
        stats["shards"] = int(row[0]) if row else 0
    stats["score"] = await collector_score(conn, user_id)
    return stats


async def open_pack(
    conn: AsyncConnection,
    user_id: int,
    pack_key: str,
    *,
    interaction_id: str,
    master_seed: str,
) -> list[PullOutcome]:
    """Consume one pack and resolve its pulls in a single transaction.

    Per plan C10 the caller releases the connection before animating --
    this function returns the fully-committed outcome list; the RNG is
    never driven by the reveal.
    """
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT key, metadata FROM shop_items WHERE key = %s AND kind = 'CONSUMABLE'",
            (pack_key,),
        )
        item = await cur.fetchone()
    if item is None or "cards" not in (item["metadata"] or {}):
        raise PackError(f"`{pack_key}` isn't an openable card pack.")
    meta = item["metadata"]
    n_cards = int(meta.get("cards", 3))
    floor = meta.get("floor")

    config = await load_pack_config(conn)
    pools = await load_pools(conn)
    # Snapshot every input resolve_pull consumes; written verbatim to
    # card_pulls.pull_cfg so each row self-contains its replay contract.
    cfg_snapshot: dict[str, Any] = {
        "weights": config.tier_weights,
        "pools": pools,
        "pity_threshold": config.pity_threshold,
    }

    async with conn.transaction():
        await record_idempotency_key(conn, interaction_id)
        # Raises NotOwnedError (ShopError) when the pack isn't held.
        await use_consumable(conn, user_id, pack_key)

        # Lock the user row once for the pack-pull sequence + pity + shards.
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT pack_pulls, pity_count, shards FROM users WHERE id = %s FOR UPDATE",
                (user_id,),
            )
            row = await cur.fetchone()
            assert row is not None  # bootstrap_user guarantees the row
            pack_pulls, pity, _shards = row

        # Resolve the whole pack's draws up front: each pull's seq, tier
        # and pity state are pure functions of the earlier pulls, so the
        # stamp context below can be fetched in one batched query.
        pending: list[tuple[int, str, str, int, str | None]] = []
        pulls_next = int(pack_pulls)
        pity_sim = int(pity)
        for i in range(n_cards):
            pull_seq = pulls_next + 1
            pulls_next = pull_seq
            seed = pull_seed(master_seed, user_id, pull_seq)
            # The premium floor lands on the LAST card of the pack.
            card_floor = floor if (floor is not None and i == n_cards - 1) else None
            tier, card_key = resolve_pull(
                seed,
                pity_sim,
                config.tier_weights,
                pools,
                card_floor,
                pity_threshold=config.pity_threshold,
            )
            pending.append((pull_seq, tier, card_key, pity_sim, card_floor))
            # pity: rare+ (GOLD or better) resets the streak, else +1.
            pity_sim = 0 if FRAME_RANK[tier] >= FRAME_RANK["GOLD"] else pity_sim + 1

        # One tick snapshot for the whole pack: serials, stamps and
        # first_acquired all describe "the market at open time".
        pull_tick = await _current_tick(conn)
        stamp_cfg = await _config_map(conn, "stamps.")
        stamp_ctx = await _load_stamp_context(
            conn, [p[2] for p in pending], pull_tick
        )

        outcomes: list[PullOutcome] = []
        for pull_seq, tier, card_key, pity_before, card_floor in pending:
            # Mint the serial: the cards row lock serializes concurrent
            # opens of the same card (packs are user-paced; contention nil).
            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE cards SET minted_count = minted_count + 1"
                    " WHERE key = %s RETURNING minted_count",
                    (card_key,),
                )
                serial_row = await cur.fetchone()
                assert serial_row is not None
                serial = int(serial_row[0])

            stamps = _stamps_for(
                stamp_ctx.get(card_key),
                pull_tick,
                pity_before=pity_before,
                pity_threshold=config.pity_threshold,
                stamp_cfg=stamp_cfg,
            )

            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT best_frame FROM user_cards"
                    " WHERE user_id = %s AND card_key = %s FOR UPDATE",
                    (user_id, card_key),
                )
                held = await cur.fetchone()
            held_frame = str(held["best_frame"]) if held else None
            outcome, shards = classify(tier, held_frame, config.shard_values)

            # best_frame only rises on NEW/UPGRADE; a DUPLICATE keeps
            # the held (higher-or-equal) frame and just banks copies.
            best_frame = tier if outcome in ("NEW", "UPGRADE") else held_frame or tier
            if held is None:
                await _insert_user_card(
                    conn, user_id, card_key, best_frame, serial, pull_tick
                )
                copies = 1
            elif outcome == "UPGRADE":
                copies = await _upgrade_user_card(
                    conn, user_id, card_key, best_frame, serial
                )
            else:
                copies = await _bump_copies(conn, user_id, card_key)

            await _insert_pull(
                conn,
                user_id=user_id,
                pull_seq=pull_seq,
                pack_key=pack_key,
                pity_before=pity_before,
                tier=tier,
                card_key=card_key,
                outcome=outcome,
                shards=shards,
                pull_cfg={**cfg_snapshot, "floor": card_floor},
                serial=serial,
                stamps=stamps,
                tick_index=pull_tick,
            )
            if shards:
                await _award_shards(
                    conn,
                    user_id,
                    shards,
                    card_key=card_key,
                    pull_seq=pull_seq,
                    tick_index=pull_tick,
                )

            card = await get_card(conn, card_key)
            low_serial = int(stamp_cfg.get("low_serial_feed", 10))
            if tier in ("EPIC", "LEGENDARY") or serial <= low_serial:
                # Achievement class: the mention stays (NPC anonymity
                # convention -- bots never open packs, so it's safe).
                await emit_feed(
                    conn,
                    "CARD_PULL",
                    {
                        "tier": tier,
                        "name": card.name if card else card_key,
                        "serial": serial,
                    },
                    user_id=user_id,
                    tick_index=pull_tick,
                )

            outcomes.append(
                PullOutcome(
                    pull_seq=pull_seq,
                    tier=tier,
                    card_key=card_key,
                    name=card.name if card else card_key,
                    outcome=outcome,
                    shards=shards,
                    best_frame=best_frame,
                    copies=copies,
                    serial=serial,
                    stamps=tuple(stamps),
                )
            )

        # Bonus part roll (depth D): one extra draw per pack on a
        # domain-separated seed -- card pulls replay identically. Only a
        # hit consumes a pull_seq and writes a tier='PART' card_pulls
        # row; a miss rolls no pull and touches nothing.
        parts_pool = await load_part_pool(conn)
        eff_chance = float(meta.get("part_chance", config.part_chance))
        if parts_pool and eff_chance > 0:
            pseq = pulls_next + 1
            prng = random.Random(part_seed(master_seed, user_id, pseq))
            if prng.random() < eff_chance:
                pulls_next = pseq
                part_key = parts_pool[prng.randrange(len(parts_pool))]
                async with conn.cursor() as cur:
                    await cur.execute(
                        "UPDATE cards SET minted_count = minted_count + 1"
                        " WHERE key = %s RETURNING minted_count",
                        (part_key,),
                    )
                    part_serial_row = await cur.fetchone()
                    assert part_serial_row is not None
                    part_serial = int(part_serial_row[0])
                    await cur.execute(
                        "SELECT copies FROM user_cards"
                        " WHERE user_id = %s AND card_key = %s FOR UPDATE",
                        (user_id, part_key),
                    )
                    part_held = await cur.fetchone()
                part_shards = 0
                if part_held is None:
                    await _insert_user_card(
                        conn, user_id, part_key, "STANDARD", part_serial,
                        pull_tick,
                    )
                    part_outcome, part_copies = "NEW", 1
                else:
                    # PART copies are the held stack -- dupes bank copies
                    # AND pay the flat part shard value.
                    part_copies = await _bump_copies(conn, user_id, part_key)
                    part_outcome = "DUPLICATE"
                    part_shards = config.shard_values.get("part", 10)
                await _insert_pull(
                    conn,
                    user_id=user_id,
                    pull_seq=pseq,
                    pack_key=pack_key,
                    pity_before=pity_sim,
                    tier="PART",
                    card_key=part_key,
                    outcome=part_outcome,
                    shards=part_shards,
                    pull_cfg={
                        "part": True,
                        "chance": eff_chance,
                        "pool": parts_pool,
                    },
                    serial=part_serial,
                    stamps=[],
                    tick_index=pull_tick,
                )
                if part_shards:
                    await _award_shards(
                        conn, user_id, part_shards, card_key=part_key,
                        pull_seq=pseq, tick_index=pull_tick,
                    )
                part_card = await get_card(conn, part_key)
                outcomes.append(
                    PullOutcome(
                        pull_seq=pseq,
                        tier="PART",
                        card_key=part_key,
                        name=part_card.name if part_card else part_key,
                        outcome=part_outcome,
                        shards=part_shards,
                        best_frame="STANDARD",
                        copies=part_copies,
                        serial=part_serial,
                        stamps=(),
                    )
                )

        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE users SET pack_pulls = %s, pity_count = %s WHERE id = %s",
                (pulls_next, pity_sim, user_id),
            )

    return outcomes


async def _current_tick(conn: AsyncConnection) -> int | None:
    async with conn.cursor() as cur:
        await cur.execute("SELECT MAX(tick_index) FROM market_ticks")
        row = await cur.fetchone()
    return int(row[0]) if row and row[0] is not None else None


async def _load_stamp_context(
    conn: AsyncConnection, card_keys: list[str], tick: int | None
) -> dict[str, dict[str, Any]]:
    """One batched read of everything a pull's stamps derive from:
    the card's instrument row (mark, ATH, halt state), its venue's
    session shape, the pull tick's candle, and any settled IPO."""
    if not card_keys:
        return {}
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            SELECT cd.key,
                   i.quoted_price, i.high_water_price,
                   i.circuit_halted_until_tick,
                   m.open_ticks, m.closed_ticks, m.offset_ticks,
                   c.open AS candle_open, c.close AS candle_close,
                   o.settled_tick
            FROM cards cd
            LEFT JOIN instruments i ON i.id = cd.instrument_id
            LEFT JOIN markets m ON m.id = i.market_id
            LEFT JOIN candles c ON c.instrument_id = i.id
                               AND c.tick_index = %s
            LEFT JOIN ipo_offerings o ON o.instrument_id = i.id
                                    AND o.status = 'SETTLED'
            WHERE cd.key = ANY(%s)
            """,
            (tick, card_keys),
        )
        return {str(r["key"]): dict(r) for r in await cur.fetchall()}


def _stamps_for(
    ctx: dict[str, Any] | None,
    tick: int | None,
    *,
    pity_before: int,
    pity_threshold: int,
    stamp_cfg: dict[str, float],
) -> list[str]:
    """Print-context stamps for one pull. Every stamp replays from
    recorded inputs: the pull tick plus market state at that tick."""
    stamps: list[str] = []
    if pity_before >= pity_threshold:
        stamps.append("PITY_BREAK")
    if ctx is None or tick is None:
        return stamps
    if (
        ctx["circuit_halted_until_tick"] is not None
        and int(ctx["circuit_halted_until_tick"]) >= tick
    ):
        stamps.append("HALT_PRINT")
    if ctx["quoted_price"] is not None and ctx["high_water_price"] is not None:
        if float(ctx["quoted_price"]) >= float(ctx["high_water_price"]):
            stamps.append("PEAK_PRINT")
    if ctx["settled_tick"] is not None:
        day_one = int(stamp_cfg.get("day_one_ticks", 1440))
        if 0 <= tick - int(ctx["settled_tick"]) <= day_one:
            stamps.append("DAY_ONE")
    if ctx["open_ticks"] is not None and (
        session_phase(
            tick,
            int(ctx["open_ticks"]),
            int(ctx["closed_ticks"]),
            int(ctx["offset_ticks"]),
        )
        == "CLOSED"
    ):
        stamps.append("OFF_HOURS")
    if ctx["candle_open"] is not None and ctx["candle_close"] is not None:
        o = float(ctx["candle_open"])
        if o > 0:
            move = float(ctx["candle_close"]) / o - 1
            if move >= float(stamp_cfg.get("moon_pct", 12)) / 100:
                stamps.append("MOON")
            elif move <= -float(stamp_cfg.get("crash_pct", 12)) / 100:
                stamps.append("CRATER")
    return stamps


async def _insert_user_card(
    conn: AsyncConnection,
    user_id: int,
    card_key: str,
    frame: str,
    serial: int,
    tick: int | None,
) -> None:
    async with conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO user_cards
                (user_id, card_key, best_frame, best_serial, first_acquired_tick)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (user_id, card_key, frame, serial, tick),
        )


async def _upgrade_user_card(
    conn: AsyncConnection,
    user_id: int,
    card_key: str,
    frame: str,
    serial: int,
) -> int:
    async with conn.cursor() as cur:
        await cur.execute(
            """
            UPDATE user_cards
            SET best_frame = %s, best_serial = %s,
                copies = copies + 1, upgraded_at = now()
            WHERE user_id = %s AND card_key = %s
            RETURNING copies
            """,
            (frame, serial, user_id, card_key),
        )
        row = await cur.fetchone()
        assert row is not None  # RETURNING with WHERE-matched update
        return int(row[0])


async def _bump_copies(
    conn: AsyncConnection, user_id: int, card_key: str
) -> int:
    async with conn.cursor() as cur:
        await cur.execute(
            """
            UPDATE user_cards SET copies = copies + 1
            WHERE user_id = %s AND card_key = %s
            RETURNING copies
            """,
            (user_id, card_key),
        )
        row = await cur.fetchone()
        assert row is not None  # RETURNING with WHERE-matched update
        return int(row[0])


async def _insert_pull(
    conn: AsyncConnection,
    *,
    user_id: int,
    pull_seq: int,
    pack_key: str,
    pity_before: int,
    tier: str,
    card_key: str,
    outcome: str,
    shards: int,
    pull_cfg: dict[str, Any],
    serial: int,
    stamps: list[str],
    tick_index: int | None,
) -> None:
    async with conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO card_pulls
                (user_id, pull_seq, pack_key, pity_count_before, tier,
                 card_key, outcome, shards_awarded, pull_cfg, serial,
                 stamps, tick_index)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                user_id,
                pull_seq,
                pack_key,
                pity_before,
                tier,
                card_key,
                outcome,
                shards,
                json.dumps(pull_cfg),
                serial,
                stamps,
                tick_index,
            ),
        )


async def _shard_event(
    conn: AsyncConnection,
    user_id: int,
    delta: int,
    reason: str,
    *,
    card_key: str | None = None,
    pull_seq: int | None = None,
    tick_index: int | None = None,
) -> None:
    async with conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO shard_events
                (user_id, delta, reason, card_key, pull_seq, tick_index)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (user_id, delta, reason, card_key, pull_seq, tick_index),
        )


async def _award_shards(
    conn: AsyncConnection,
    user_id: int,
    amount: int,
    *,
    card_key: str,
    pull_seq: int,
    tick_index: int | None = None,
) -> None:
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE users SET shards = shards + %s WHERE id = %s",
            (amount, user_id),
        )
    await _shard_event(
        conn,
        user_id,
        amount,
        "DUPLICATE_BURN",
        card_key=card_key,
        pull_seq=pull_seq,
        tick_index=tick_index,
    )


# ---------------------------------------------------------------------------
# K4: crafting + featuring
# ---------------------------------------------------------------------------


async def craft_card(
    conn: AsyncConnection, user_id: int, card_key: str
) -> tuple[str, int]:
    """Spend shards for a specific missing card at STANDARD frame.
    Returns (card_key, shards_spent)."""
    card = await get_card(conn, card_key)
    if card is None:
        raise PackError(f"Unknown card `{card_key}`.")
    if card.kind == "COMMEMORATIVE":
        raise PackError("Commemorative cards can't be crafted — they're earned.")
    if card.kind == "LORE":
        raise PackError("Lore cards can't be crafted — they're the chase.")
    if card.kind == "PART":
        raise PackError("Parts can't be crafted — they drop from packs.")
    if card.kind == "ASSEMBLED":
        raise PackError("Assembled pieces need their parts — craft consumes the recipe.")

    config = await load_pack_config(conn)
    cost = config.craft_costs.get("standard", 60)

    async with conn.transaction():
        # Claim the card row first: ON CONFLICT waits on a concurrent
        # in-flight insert of the same (user_id, card_key) and reports
        # nothing to return once it commits, so a racing double-craft
        # yields this PackError rather than a bare IntegrityError.
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO user_cards
                    (user_id, card_key, best_frame, first_acquired_tick)
                VALUES (%s, %s, 'STANDARD',
                        (SELECT MAX(tick_index) FROM market_ticks))
                ON CONFLICT (user_id, card_key) DO NOTHING
                RETURNING card_key
                """,
                (user_id, card_key),
            )
            if await cur.fetchone() is None:
                raise PackError(f"You already hold **{card.name}**.")
            await cur.execute(
                "UPDATE users SET shards = shards - %s WHERE id = %s AND shards >= %s",
                (cost, user_id, cost),
            )
            if cur.rowcount == 0:
                raise PackError(
                    f"Not enough shards — crafting **{card.name}** costs {cost}."
                )
        await _shard_event(conn, user_id, -cost, "CRAFT", card_key=card_key)
    return card_key, cost


async def upgrade_frame(
    conn: AsyncConnection, user_id: int, card_key: str
) -> tuple[str, str, int]:
    """Spend shards to raise an owned card one frame step. Returns
    (card_key, new_frame, shards_spent). Lore cards can't be crafted
    up -- their frame is their identity."""
    card = await get_card(conn, card_key)
    if card is None:
        raise PackError(f"Unknown card `{card_key}`.")
    if card.kind != "INSTRUMENT":
        raise PackError("Only instrument cards can be frame-upgraded.")

    config = await load_pack_config(conn)
    async with conn.transaction():
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT best_frame FROM user_cards WHERE user_id = %s AND card_key = %s FOR UPDATE",
                (user_id, card_key),
            )
            row = await cur.fetchone()
            if row is None:
                raise PackError(f"You don't hold **{card.name}**.")
            held = str(row[0])
            rank = FRAME_RANK.get(held)
            if rank is None or rank >= FRAME_RANK["PLATINUM"]:
                raise PackError(f"**{card.name}** is already at {held}.")
            new_frame = next(f for f, r in FRAME_RANK.items() if r == rank + 1)
            cost = config.craft_costs.get(new_frame.lower(), 200)
            await cur.execute(
                "UPDATE users SET shards = shards - %s WHERE id = %s AND shards >= %s",
                (cost, user_id, cost),
            )
            if cur.rowcount == 0:
                raise PackError(
                    f"Not enough shards — {new_frame} frame costs {cost}."
                )
            await cur.execute(
                """
                UPDATE user_cards SET best_frame = %s, upgraded_at = now()
                WHERE user_id = %s AND card_key = %s
                """,
                (new_frame, user_id, card_key),
            )
        await _shard_event(
            conn, user_id, -cost, "FRAME_UPGRADE", card_key=card_key
        )
    return card_key, new_frame, cost


async def recipe_for(conn: AsyncConnection, card_key: str) -> list[dict[str, Any]]:
    """Recipe rows for an ASSEMBLED card: part key, name, qty needed."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            SELECT r.part_key, r.qty, c.name
            FROM card_recipes r JOIN cards c ON c.key = r.part_key
            WHERE r.result_key = %s ORDER BY r.part_key
            """,
            (card_key,),
        )
        return [dict(r) for r in await cur.fetchall()]


async def assemble_card(
    conn: AsyncConnection, user_id: int, card_key: str
) -> tuple[str, int]:
    """Consume a full recipe of held PARTs and mint the ASSEMBLED piece.
    Returns (card_key, serial). No shard cost -- the parts are the price."""
    card = await get_card(conn, card_key)
    if card is None:
        raise PackError(f"Unknown card `{card_key}`.")
    if card.kind != "ASSEMBLED":
        raise PackError(f"**{card.name}** isn't an assembled piece.")

    recipe = await recipe_for(conn, card_key)
    if not recipe:
        raise PackError(f"**{card.name}** has no recipe.")

    async with conn.transaction():
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT 1 FROM user_cards WHERE user_id = %s AND card_key = %s",
                (user_id, card_key),
            )
            if await cur.fetchone() is not None:
                raise PackError(f"You already hold **{card.name}**.")
            await cur.execute(
                """
                SELECT card_key, copies FROM user_cards
                WHERE user_id = %s AND card_key = ANY(%s)
                FOR UPDATE
                """,
                (user_id, [r["part_key"] for r in recipe]),
            )
            held = {str(r["card_key"]): int(r["copies"]) for r in await cur.fetchall()}
            missing = [
                r["name"]
                for r in recipe
                if held.get(str(r["part_key"]), 0) < int(r["qty"])
            ]
            if missing:
                raise PackError(
                    "Missing parts: " + ", ".join(f"**{m}**" for m in missing)
                )
            # Consume the parts: spent stacks leave first (the CHECK on
            # copies > 0 fires per-statement), then decrement the rest.
            for r in recipe:
                await cur.execute(
                    "DELETE FROM user_cards"
                    " WHERE user_id = %s AND card_key = %s AND copies <= %s",
                    (user_id, str(r["part_key"]), int(r["qty"])),
                )
                await cur.execute(
                    "UPDATE user_cards SET copies = copies - %s"
                    " WHERE user_id = %s AND card_key = %s",
                    (int(r["qty"]), user_id, str(r["part_key"])),
                )
            # Assembled pieces mint serials like any card.
            await cur.execute(
                "UPDATE cards SET minted_count = minted_count + 1"
                " WHERE key = %s RETURNING minted_count",
                (card_key,),
            )
            serial_row = await cur.fetchone()
            assert serial_row is not None
            serial = int(serial_row["minted_count"])
            await cur.execute(
                """
                INSERT INTO user_cards
                    (user_id, card_key, best_frame, best_serial, first_acquired_tick)
                VALUES (%s, %s, 'STANDARD', %s,
                        (SELECT MAX(tick_index) FROM market_ticks))
                """,
                (user_id, card_key, serial),
            )
    return card_key, serial


async def set_featured_card(
    conn: AsyncConnection, user_id: int, card_key: str | None
) -> str | None:
    """Pin a held card on the profile; None clears. Returns the key set."""
    async with conn.transaction():
        if card_key is not None:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT 1 FROM user_cards WHERE user_id = %s AND card_key = %s",
                    (user_id, card_key),
                )
                if await cur.fetchone() is None:
                    raise PackError(f"You don't hold `{card_key}`.")
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE users SET featured_card = %s WHERE id = %s",
                (card_key, user_id),
            )
    return card_key


async def grant_commemorative(
    conn: AsyncConnection, user_id: int, card_key: str
) -> bool:
    """Event-mint a commemorative card (never via packs). Idempotent."""
    card = await get_card(conn, card_key)
    if card is None or card.kind != "COMMEMORATIVE":
        raise PackError(f"`{card_key}` isn't a commemorative card.")
    async with conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO user_cards (user_id, card_key, best_frame)
            VALUES (%s, %s, 'STANDARD') ON CONFLICT DO NOTHING
            """,
            (user_id, card_key),
        )
        return cur.rowcount > 0


async def shards_of(conn: AsyncConnection, user_id: int) -> int:
    async with conn.cursor() as cur:
        await cur.execute("SELECT shards FROM users WHERE id = %s", (user_id,))
        row = await cur.fetchone()
    return int(row[0]) if row else 0


# ---------------------------------------------------------------------------
# K5: collector score + daily top pull
# ---------------------------------------------------------------------------

_FRAME_W_SQL = (
    "CASE uc.best_frame WHEN 'STANDARD' THEN %(ws)s WHEN 'SILVER' THEN %(wv)s"
    " WHEN 'GOLD' THEN %(wg)s WHEN 'PLATINUM' THEN %(wp)s"
    " WHEN 'EPIC' THEN %(we)s ELSE %(wl)s END"
)


async def collector_score(conn: AsyncConnection, user_id: int) -> int:
    """One user's collector score (same formula as the leaderboard)."""
    rows = await collector_leaderboard(conn, limit=None, user_id=user_id)
    return int(rows[0]["score"]) if rows else 0


async def collector_leaderboard(
    conn: AsyncConnection, limit: int | None = 15, user_id: int | None = None
) -> list[dict[str, Any]]:
    """Rarity + provenance weighted ranking. Score = per-held-card
    frame weight + kind bonus + serial bonus (mint #1 or a low print).
    Live query -- score.* config retunes apply instantly."""
    cfg = await _config_map(conn, "score.")
    params: dict[str, Any] = {
        "ws": cfg.get("frame_standard", 1),
        "wv": cfg.get("frame_silver", 3),
        "wg": cfg.get("frame_gold", 8),
        "wp": cfg.get("frame_platinum", 20),
        "we": cfg.get("frame_epic", 30),
        "wl": cfg.get("frame_legendary", 60),
        "kl": cfg.get("kind_lore", 10),
        "kc": cfg.get("kind_commemorative", 15),
        "ka": cfg.get("kind_assembled", 25),
        "s1": cfg.get("serial_one", 10),
        "sl": cfg.get("serial_low", 5),
        "smax": cfg.get("serial_low_max", 10),
        "lim": limit,
        "uid": user_id,
    }
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            f"""
            SELECT uc.user_id,
                   SUM({_FRAME_W_SQL}
                     + CASE c.kind WHEN 'LORE' THEN %(kl)s
                                   WHEN 'COMMEMORATIVE' THEN %(kc)s
                                   WHEN 'ASSEMBLED' THEN %(ka)s
                                   ELSE 0 END
                     + CASE WHEN uc.best_serial = 1 THEN %(s1)s
                            WHEN uc.best_serial IS NOT NULL
                                 AND uc.best_serial <= %(smax)s
                            THEN %(sl)s ELSE 0 END) AS score,
                   COUNT(*) AS cards
            FROM user_cards uc
            JOIN cards c ON c.key = uc.card_key
            WHERE (%(uid)s::bigint IS NULL OR uc.user_id = %(uid)s)
            GROUP BY uc.user_id
            ORDER BY score DESC, cards DESC
            LIMIT %(lim)s
            """,
            params,
        )
        rows = [dict(r) for r in await cur.fetchall()]
        if not rows:
            return rows
        uids = [int(r["user_id"]) for r in rows]
        # Headline card per ranked user: highest frame, then lowest mint.
        await cur.execute(
            """
            SELECT DISTINCT ON (uc.user_id)
                   uc.user_id, c.name, uc.best_frame, uc.best_serial
            FROM user_cards uc
            JOIN cards c ON c.key = uc.card_key
            WHERE uc.user_id = ANY(%s)
            ORDER BY uc.user_id,
                     CASE uc.best_frame WHEN 'STANDARD' THEN 0
                          WHEN 'SILVER' THEN 1 WHEN 'GOLD' THEN 2
                          WHEN 'PLATINUM' THEN 3 WHEN 'EPIC' THEN 4
                          ELSE 5 END DESC,
                     uc.best_serial ASC NULLS LAST
            """,
            (uids,),
        )
        top = {int(r["user_id"]): r for r in await cur.fetchall()}
    for r in rows:
        t = top.get(int(r["user_id"]))
        if t is not None:
            r["top_name"] = t["name"]
            r["top_frame"] = t["best_frame"]
            r["top_serial"] = t["best_serial"]
    return rows


async def on_day(conn: AsyncConnection, tick_index: int) -> None:
    """Day-boundary collectibles jobs, self-gating like quests.on_tick:
    broadcast the day's top pull and expire stale trade offers."""
    if tick_index == 0 or tick_index % TICKS_PER_DAY != 0:
        return
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            SELECT p.user_id, p.tier, p.card_key, p.serial, c.name
            FROM card_pulls p
            JOIN cards c ON c.key = p.card_key
            WHERE p.tick_index BETWEEN %s AND %s
              AND p.tier <> 'PART'
            ORDER BY CASE p.tier WHEN 'STANDARD' THEN 0
                     WHEN 'SILVER' THEN 1 WHEN 'GOLD' THEN 2
                     WHEN 'PLATINUM' THEN 3 WHEN 'EPIC' THEN 4
                     WHEN 'LEGENDARY' THEN 5 ELSE -1 END DESC,
                     p.serial ASC NULLS LAST
            LIMIT 1
            """,
            (tick_index - TICKS_PER_DAY, tick_index - 1),
        )
        top = await cur.fetchone()
    if top is not None:
        await emit_feed(
            conn,
            "TOP_PULL",
            {
                "tier": str(top["tier"]),
                "name": str(top["name"]),
                "serial": top["serial"],
            },
            user_id=int(top["user_id"]),
            tick_index=tick_index,
        )
    # Expired open offers close at the day boundary. card_trades lands
    # in 0066 -- the regclass probe keeps this hook harmless before it.
    async with conn.cursor() as cur:
        await cur.execute("SELECT to_regclass('card_trades')")
        if (await cur.fetchone() or (None,))[0] is None:
            return
        await cur.execute(
            """
            UPDATE card_trades SET status = 'EXPIRED', resolved_at = now()
            WHERE status = 'OPEN' AND expires_tick <= %s
            """,
            (tick_index,),
        )
