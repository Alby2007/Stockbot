"""DB layer for collectibles: pack opening, collection reads, crafting.

Shard economics (C9): shards live on `users.shards`, OFF the
double-entry ledger -- the ledger is for money, shards are a
non-convertible vanity material. One source (duplicate burns), one sink
(crafting/upgrading). The ledger audit stays untouched because no shard
path ever posts a transfer.

Pull auditability (C2): every draw writes a `card_pulls` row carrying
pull_seq + pity_count_before, so `resolve_pull` can be replayed
byte-for-byte from recorded inputs.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from psycopg import AsyncConnection
from psycopg.rows import dict_row

from stockbot.collectibles.pull import (
    FRAME_RANK,
    classify,
    pull_seed,
    resolve_pull,
)
from stockbot.feed import emit_feed
from stockbot.ledger.service import record_idempotency_key
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
    return PackConfig(
        tier_weights={k.upper(): v for k, v in rates.items()},
        shard_values={k: int(v) for k, v in shards.items()},
        craft_costs={k: int(v) for k, v in crafts.items()},
        pity_threshold=int(row[0]) if row else 20,
    )


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
    """Resolve a card by key ('card_nort', 'lore_whale') or by ticker /
    name fragment for /card autocomplete targets."""
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
    set, then sector, then card."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            SELECT c.key, c.name, c.kind, c.set_key, c.sector_id, c.rarity,
                   s.name AS sector_name,
                   uc.best_frame, uc.copies
            FROM user_cards uc
            JOIN cards c ON c.key = uc.card_key
            LEFT JOIN sectors s ON s.id = c.sector_id
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

        outcomes: list[PullOutcome] = []
        for i in range(n_cards):
            pull_seq = int(pack_pulls) + 1
            pack_pulls = pull_seq
            pity_before = int(pity)
            seed = pull_seed(master_seed, user_id, pull_seq)
            # The premium floor lands on the LAST card of the pack.
            card_floor = floor if (floor is not None and i == n_cards - 1) else None
            tier, card_key = resolve_pull(
                seed,
                pity_before,
                config.tier_weights,
                pools,
                card_floor,
                pity_threshold=config.pity_threshold,
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

            # pity: rare+ (GOLD or better) resets the streak, else +1.
            pity = 0 if FRAME_RANK[tier] >= FRAME_RANK["GOLD"] else pity_before + 1

            # best_frame only rises on NEW/UPGRADE; a DUPLICATE keeps
            # the held (higher-or-equal) frame and just banks copies.
            best_frame = tier if outcome in ("NEW", "UPGRADE") else held_frame or tier
            if held is None:
                await _insert_user_card(conn, user_id, card_key, best_frame)
                copies = 1
            elif outcome == "UPGRADE":
                copies = await _upgrade_user_card(
                    conn, user_id, card_key, best_frame
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
            )
            if shards:
                await _award_shards(conn, user_id, shards)

            card = await get_card(conn, card_key)
            if tier in ("EPIC", "LEGENDARY"):
                # Achievement class: the mention stays (NPC anonymity
                # convention -- bots never open packs, so it's safe).
                await emit_feed(
                    conn,
                    "CARD_PULL",
                    {"tier": tier, "name": card.name if card else card_key},
                    user_id=user_id,
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
                )
            )

        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE users SET pack_pulls = %s, pity_count = %s WHERE id = %s",
                (int(pack_pulls), int(pity), user_id),
            )

    return outcomes


async def _insert_user_card(
    conn: AsyncConnection, user_id: int, card_key: str, frame: str
) -> None:
    async with conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO user_cards (user_id, card_key, best_frame, first_acquired_tick)
            VALUES (%s, %s, %s, (SELECT MAX(tick_index) FROM market_ticks))
            """,
            (user_id, card_key, frame),
        )


async def _upgrade_user_card(
    conn: AsyncConnection, user_id: int, card_key: str, frame: str
) -> int:
    async with conn.cursor() as cur:
        await cur.execute(
            """
            UPDATE user_cards
            SET best_frame = %s, copies = copies + 1, upgraded_at = now()
            WHERE user_id = %s AND card_key = %s
            RETURNING copies
            """,
            (frame, user_id, card_key),
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
) -> None:
    async with conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO card_pulls
                (user_id, pull_seq, pack_key, pity_count_before, tier,
                 card_key, outcome, shards_awarded, tick_index)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s,
                    (SELECT MAX(tick_index) FROM market_ticks))
            """,
            (user_id, pull_seq, pack_key, pity_before, tier, card_key, outcome, shards),
        )


async def _award_shards(conn: AsyncConnection, user_id: int, amount: int) -> None:
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE users SET shards = shards + %s WHERE id = %s",
            (amount, user_id),
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

    config = await load_pack_config(conn)
    cost = config.craft_costs.get("standard", 60)

    async with conn.transaction():
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT 1 FROM user_cards WHERE user_id = %s AND card_key = %s",
                (user_id, card_key),
            )
            if await cur.fetchone() is not None:
                raise PackError(f"You already hold **{card.name}**.")
            await cur.execute(
                "UPDATE users SET shards = shards - %s WHERE id = %s AND shards >= %s",
                (cost, user_id, cost),
            )
            if cur.rowcount == 0:
                raise PackError(
                    f"Not enough shards — crafting **{card.name}** costs {cost}."
                )
        await _insert_user_card(conn, user_id, card_key, "STANDARD")
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
    return card_key, new_frame, cost


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
