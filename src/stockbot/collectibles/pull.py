"""Pure pull math for card packs -- no DB, fully replayable (C2).

`pull_seed` mirrors `market.engine.tick_seed`: HMAC-SHA256 with domain
separation, so "user X's Nth pull" is a pure function of recorded inputs
(user_id, pull_seq, pity state, and the per-pull `pull_cfg` snapshot of
weights/pools/floor stored on the `card_pulls` row) and any verifier can
re-derive the same outcome from `card_pulls` rows alone.

Tier-first resolution (C5): roll a tier from config weights, then draw
uniformly from that tier's pool. Instrument tiers (STANDARD..PLATINUM)
all draw from the instrument pool -- the tier IS the frame awarded.
Lore tiers draw only lore pools. Pity and the premium pack's rare+
floor are clamps on the tier roll applied BEFORE the tier is read, so
they're expressible inside the pure function (a post-hoc reroll would
not be).
"""

from __future__ import annotations

import hashlib
import hmac
import random
from collections.abc import Mapping, Sequence

# C4: canonical frame ordering. Instrument cards occupy 0-3 (frame ==
# tier); lore cards live at 4-5 keyed by `cards.rarity`. COMMEMORATIVE
# cards never enter the pack pool, so they need no rank.
FRAME_RANK: dict[str, int] = {
    "STANDARD": 0,
    "SILVER": 1,
    "GOLD": 2,
    "PLATINUM": 3,
    "EPIC": 4,
    "LEGENDARY": 5,
}

# Tiers the pack roll can produce, in ascending rarity. The tier list is
# fixed code, not config: tier_weights keys must cover it exactly.
TIERS: tuple[str, ...] = tuple(FRAME_RANK.keys())

# "rare+" for pity and the premium floor: GOLD frame or better.
RARE_FLOOR = "GOLD"

# Tier -> pool key. The four instrument tiers share one pool; the drawn
# tier stamps the frame, not the card pool.
_POOL_FOR_TIER: dict[str, str] = {
    "STANDARD": "INSTRUMENT",
    "SILVER": "INSTRUMENT",
    "GOLD": "INSTRUMENT",
    "PLATINUM": "INSTRUMENT",
    "EPIC": "EPIC",
    "LEGENDARY": "LEGENDARY",
}


def pull_seed(master_seed: str, user_id: int, pull_seq: int) -> int:
    """Deterministic per-pull seed: HMAC(master|packs, user|seq).

    Domain separation (`|packs`) keeps pack pulls independent of tick
    seeds even if a user_id/pull_seq ever collides with a tick_index.
    """
    mac = hmac.new(
        f"{master_seed}|packs".encode(),
        f"{user_id}|{pull_seq}".encode(),
        hashlib.sha256,
    ).digest()
    return int.from_bytes(mac[:8], "big") & 0x7FFFFFFFFFFFFFFF


def _clamp_tier(tier: str, floor: str | None) -> str:
    if floor is not None and FRAME_RANK[tier] < FRAME_RANK[floor]:
        return floor
    return tier


def resolve_pull(
    seed: int,
    pity_count: int,
    tier_weights: Mapping[str, float],
    pools: Mapping[str, Sequence[str]],
    floor: str | None = None,
    *,
    pity_threshold: int = 20,
) -> tuple[str, str]:
    """Draw (tier, card_key) for one pull. `pity_count` is the counter
    BEFORE this pull: when it reaches `pity_threshold` the tier clamps
    to >= RARE_FLOOR. `floor` (premium pack) clamps one pull the same
    way; the stronger of the two clamps wins.
    """
    rng = random.Random(seed)

    # Draw 1: the tier. Pools/weights keys must cover every tier -- a
    # config typo surfaces as KeyError here, not a silent skew.
    u = rng.random()
    total = sum(tier_weights[t] for t in TIERS)
    if total <= 0:
        # All-zero weights would silently mint TIERS[-1] (LEGENDARY) on
        # every pull; refuse instead.
        raise ValueError(f"pack tier weights sum to {total}; must be > 0")
    roll = u * total
    tier = TIERS[-1]
    for t in TIERS:
        roll -= tier_weights[t]
        if roll < 0:
            tier = t
            break

    effective_floor: str | None = floor
    if pity_count >= pity_threshold:
        if effective_floor is None or FRAME_RANK[RARE_FLOOR] > FRAME_RANK[effective_floor]:
            effective_floor = RARE_FLOOR
    tier = _clamp_tier(tier, effective_floor)

    # Draw 2: the card inside the tier's pool. Ordered pools are
    # mandatory (the caller supplies sorted key lists) or randrange
    # isn't replayable.
    pool = pools[_POOL_FOR_TIER[tier]]
    if not pool:
        raise ValueError(f"empty card pool for tier {tier}")
    return tier, pool[rng.randrange(len(pool))]


def classify(
    pulled_frame: str,
    held_frame: str | None,
    shard_values: Mapping[str, int],
) -> tuple[str, int]:
    """(outcome, shards) for a pull against what's already held (C4/C6).

    Higher frame -> UPGRADE (the card's best_frame rises; the displaced
    copy is absorbed, no shards). Equal or lower -> DUPLICATE, paying
    shards scaled by the PULLED frame (a Platinum dupe stings less than
    a Standard one). Nothing held -> NEW.
    """
    if held_frame is None:
        return "NEW", 0
    if FRAME_RANK[pulled_frame] > FRAME_RANK[held_frame]:
        return "UPGRADE", 0
    return "DUPLICATE", int(shard_values.get(pulled_frame.lower(), 0))
