"""Collectibles: pure pull math, pack opening, crafting, featuring,
and the paginated binder. DB tests run inside the rolled-back `conn`
fixture transaction.
"""

from __future__ import annotations

import pytest
from psycopg import AsyncConnection

from stockbot.accounts.service import bootstrap_user
from stockbot.bot.collection_view import build_page, parse_cid
from stockbot.bot.feed import _fmt_card_pull
from stockbot.collectibles.pull import (
    FRAME_RANK,
    classify,
    pull_seed,
    resolve_pull,
)
from stockbot.collectibles.service import (
    PackError,
    collection_stats,
    craft_card,
    find_card,
    get_card,
    grant_commemorative,
    list_collection,
    load_pack_config,
    load_pools,
    open_pack,
    set_featured_card,
    upgrade_frame,
)
from stockbot.shop.errors import ShopError
from stockbot.status.service import equipped_flair_map, profile_stats
from stockbot.trading.errors import DuplicateInteractionError

_SEED = "test-master-seed"
_USER = 6101


async def _grant_pack(conn: AsyncConnection, user_id: int, pack_key: str, n: int = 1) -> None:
    async with conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO entitlements (user_id, item_key, quantity) VALUES (%s, %s, %s)",
            (user_id, pack_key, n),
        )


async def _hold_card(
    conn: AsyncConnection, user_id: int, card_key: str, frame: str = "STANDARD"
) -> None:
    async with conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO user_cards (user_id, card_key, best_frame) VALUES (%s, %s, %s)",
            (user_id, card_key, frame),
        )


async def _set_shards(conn: AsyncConnection, user_id: int, n: int) -> None:
    async with conn.cursor() as cur:
        await cur.execute("UPDATE users SET shards = %s WHERE id = %s", (n, user_id))


# ---------------------------------------------------------------------------
# Pure pull math (K2)
# ---------------------------------------------------------------------------


def test_pull_seed_deterministic() -> None:
    assert pull_seed(_SEED, 42, 1) == pull_seed(_SEED, 42, 1)
    assert pull_seed(_SEED, 42, 1) != pull_seed(_SEED, 42, 2)
    assert pull_seed(_SEED, 42, 1) != pull_seed(_SEED, 43, 1)
    assert pull_seed(_SEED, 42, 1) != pull_seed(_SEED + "x", 42, 1)
    assert 0 <= pull_seed(_SEED, 42, 1) <= 0x7FFFFFFFFFFFFFFF


def _weights() -> dict[str, float]:
    return {
        "STANDARD": 55,
        "SILVER": 25,
        "GOLD": 12,
        "PLATINUM": 5,
        "EPIC": 2.4,
        "LEGENDARY": 0.6,
    }


def _pools() -> dict[str, list[str]]:
    return {
        "INSTRUMENT": [f"card_{i:02d}" for i in range(80)],
        "EPIC": ["lore_a", "lore_b"],
        "LEGENDARY": ["lore_c"],
    }


def test_resolve_pull_replayable() -> None:
    seed = pull_seed(_SEED, 42, 7)
    a = resolve_pull(seed, 3, _weights(), _pools())
    b = resolve_pull(seed, 3, _weights(), _pools())
    assert a == b  # same recorded inputs -> same outcome, provably fair


def test_resolve_pull_floor() -> None:
    for i in range(200):
        tier, card = resolve_pull(
            pull_seed(_SEED, 1, i), 0, _weights(), _pools(), floor="GOLD"
        )
        assert FRAME_RANK[tier] >= FRAME_RANK["GOLD"]


def test_resolve_pull_pity() -> None:
    # pity_count at the threshold clamps every roll to rare+
    for i in range(200):
        tier, card = resolve_pull(
            pull_seed(_SEED, 1, i), 20, _weights(), _pools(), pity_threshold=20
        )
        assert FRAME_RANK[tier] >= FRAME_RANK["GOLD"]


def test_resolve_pull_distribution() -> None:
    counts: dict[str, int] = {}
    pools = _pools()
    for i in range(20_000):
        tier, card = resolve_pull(pull_seed(_SEED, 1, i), 0, _weights(), pools)
        counts[tier] = counts.get(tier, 0) + 1
        if tier in ("EPIC", "LEGENDARY"):
            assert card in pools[tier]
        else:
            assert card in pools["INSTRUMENT"]
    assert counts["STANDARD"] > counts["SILVER"] > counts["GOLD"]
    assert counts["GOLD"] > counts["PLATINUM"] > counts["EPIC"] > counts["LEGENDARY"]
    assert 0.30 < counts["STANDARD"] / 20_000 < 0.80  # sanity band, not exact


def test_resolve_pull_empty_pool_raises() -> None:
    # force a high tier by weight so the empty pool is hit deterministically
    weights = {t: 0.0 for t in _weights()}
    weights["LEGENDARY"] = 1.0
    with pytest.raises(ValueError, match="empty card pool"):
        resolve_pull(1, 0, weights, {"INSTRUMENT": ["c1"], "EPIC": [], "LEGENDARY": []})


def test_classify_outcomes() -> None:
    shards = {"standard": 4, "silver": 8, "gold": 20, "platinum": 50}
    assert classify("SILVER", None, shards) == ("NEW", 0)
    assert classify("GOLD", "STANDARD", shards) == ("UPGRADE", 0)
    assert classify("STANDARD", "SILVER", shards) == ("DUPLICATE", 4)
    assert classify("PLATINUM", "PLATINUM", shards) == ("DUPLICATE", 50)
    assert classify("LEGENDARY", "LEGENDARY", shards) == ("DUPLICATE", 0)


# ---------------------------------------------------------------------------
# Pack opening (K3)
# ---------------------------------------------------------------------------


async def test_catalog_seeded(conn: AsyncConnection) -> None:
    pools = await load_pools(conn)
    assert len(pools["INSTRUMENT"]) == 82
    assert len(pools["EPIC"]) == 10
    assert len(pools["LEGENDARY"]) == 5
    cfg = await load_pack_config(conn)
    assert sum(cfg.tier_weights.values()) == pytest.approx(100.0)
    assert cfg.pity_threshold == 20


async def test_open_pack_consumes_and_records(conn: AsyncConnection) -> None:
    await bootstrap_user(conn, _USER)
    await _grant_pack(conn, _USER, "pack_basic")
    outcomes = await open_pack(
        conn, _USER, "pack_basic", interaction_id="itx-1", master_seed=_SEED
    )
    assert len(outcomes) == 3
    assert all(o.pull_seq >= 1 for o in outcomes)

    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT quantity FROM entitlements WHERE user_id = %s AND item_key = 'pack_basic'",
            (_USER,),
        )
        assert await cur.fetchone() is None  # last unit deleted
        await cur.execute(
            "SELECT count(*) FROM card_pulls WHERE user_id = %s", (_USER,)
        )
        assert (await cur.fetchone())[0] == 3
        await cur.execute(
            "SELECT pack_pulls FROM users WHERE id = %s", (_USER,)
        )
        assert (await cur.fetchone())[0] == 3


async def test_open_pack_replayable_from_rows(conn: AsyncConnection) -> None:
    """card_pulls rows carry enough to re-derive every outcome (C2)."""
    await bootstrap_user(conn, _USER)
    await _grant_pack(conn, _USER, "pack_basic")
    outcomes = await open_pack(
        conn, _USER, "pack_basic", interaction_id="itx-2", master_seed=_SEED
    )
    pools = await load_pools(conn)
    cfg = await load_pack_config(conn)
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT pull_seq, pity_count_before, tier, card_key, outcome, shards_awarded "
            "FROM card_pulls WHERE user_id = %s ORDER BY pull_seq",
            (_USER,),
        )
        rows = await cur.fetchall()
    assert len(rows) == 3
    for row, out in zip(rows, outcomes, strict=True):
        seed = pull_seed(_SEED, _USER, int(row[0]))
        tier, card_key = resolve_pull(
            seed, int(row[1]), cfg.tier_weights, pools, pity_threshold=cfg.pity_threshold
        )
        assert (tier, card_key) == (row[2], row[3])
        assert row[4] == out.outcome
        assert int(row[5]) == out.shards


async def test_open_pack_duplicate_interaction(conn: AsyncConnection) -> None:
    await bootstrap_user(conn, _USER)
    await _grant_pack(conn, _USER, "pack_basic", n=2)
    await open_pack(conn, _USER, "pack_basic", interaction_id="itx-3", master_seed=_SEED)
    with pytest.raises(DuplicateInteractionError):
        await open_pack(conn, _USER, "pack_basic", interaction_id="itx-3", master_seed=_SEED)
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT quantity FROM entitlements WHERE user_id = %s AND item_key = 'pack_basic'",
            (_USER,),
        )
        assert (await cur.fetchone())[0] == 1  # replay consumed nothing


async def test_open_pack_not_owned(conn: AsyncConnection) -> None:
    await bootstrap_user(conn, _USER)
    with pytest.raises(ShopError):
        await open_pack(conn, _USER, "pack_basic", interaction_id="itx-4", master_seed=_SEED)


async def test_open_pack_rejects_non_pack(conn: AsyncConnection) -> None:
    await bootstrap_user(conn, _USER)
    await _grant_pack(conn, _USER, "alert_pack")
    with pytest.raises(PackError, match="isn't an openable card pack"):
        await open_pack(conn, _USER, "alert_pack", interaction_id="itx-5", master_seed=_SEED)


async def test_premium_pack_floor(conn: AsyncConnection) -> None:
    await bootstrap_user(conn, _USER)
    await _grant_pack(conn, _USER, "pack_premium")
    outcomes = await open_pack(
        conn, _USER, "pack_premium", interaction_id="itx-6", master_seed=_SEED
    )
    # floor lands on the LAST pull (highest pull_seq)
    last = max(outcomes, key=lambda o: o.pull_seq)
    assert FRAME_RANK[last.tier] >= FRAME_RANK["GOLD"]


async def test_pity_grants_rare(conn: AsyncConnection) -> None:
    await bootstrap_user(conn, _USER)
    await _grant_pack(conn, _USER, "pack_basic")
    async with conn.cursor() as cur:
        await cur.execute("UPDATE users SET pity_count = 20 WHERE id = %s", (_USER,))
    outcomes = await open_pack(
        conn, _USER, "pack_basic", interaction_id="itx-7", master_seed=_SEED
    )
    first = min(outcomes, key=lambda o: o.pull_seq)
    assert FRAME_RANK[first.tier] >= FRAME_RANK["GOLD"]


async def test_duplicate_pulls_pay_shards(conn: AsyncConnection) -> None:
    """Hold every instrument at PLATINUM: any instrument pull is a
    DUPLICATE paying the pulled frame's shard value."""
    await bootstrap_user(conn, _USER)
    pools = await load_pools(conn)
    for key in pools["INSTRUMENT"]:
        await _hold_card(conn, _USER, key, "PLATINUM")
    await _grant_pack(conn, _USER, "pack_basic")
    outcomes = await open_pack(
        conn, _USER, "pack_basic", interaction_id="itx-8", master_seed=_SEED
    )
    expected = sum(o.shards for o in outcomes)
    async with conn.cursor() as cur:
        await cur.execute("SELECT shards FROM users WHERE id = %s", (_USER,))
        assert (await cur.fetchone())[0] == expected
    assert any(o.outcome == "DUPLICATE" for o in outcomes)


# ---------------------------------------------------------------------------
# Crafting + featuring (K4)
# ---------------------------------------------------------------------------


async def test_upgrade_frame(conn: AsyncConnection) -> None:
    await bootstrap_user(conn, _USER)
    await _hold_card(conn, _USER, "card_nort", "STANDARD")
    await _set_shards(conn, _USER, 100)
    key, frame, cost = await upgrade_frame(conn, _USER, "card_nort")
    assert (key, frame) == ("card_nort", "SILVER")
    assert cost == 100
    async with conn.cursor() as cur:
        await cur.execute("SELECT shards FROM users WHERE id = %s", (_USER,))
        assert (await cur.fetchone())[0] == 0


async def test_upgrade_frame_cap_and_funds(conn: AsyncConnection) -> None:
    await bootstrap_user(conn, _USER)
    await _hold_card(conn, _USER, "card_nort", "PLATINUM")
    await _set_shards(conn, _USER, 9999)
    with pytest.raises(PackError, match="already at PLATINUM"):
        await upgrade_frame(conn, _USER, "card_nort")
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE user_cards SET best_frame = 'STANDARD' "
            "WHERE user_id = %s AND card_key = 'card_nort'",
            (_USER,),
        )
    await _set_shards(conn, _USER, 5)
    with pytest.raises(PackError, match="Not enough shards"):
        await upgrade_frame(conn, _USER, "card_nort")


async def test_craft_card(conn: AsyncConnection) -> None:
    await bootstrap_user(conn, _USER)
    await _set_shards(conn, _USER, 60)
    key, cost = await craft_card(conn, _USER, "card_nort")
    assert (key, cost) == ("card_nort", 60)
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT best_frame FROM user_cards WHERE user_id = %s AND card_key = 'card_nort'",
            (_USER,),
        )
        assert (await cur.fetchone())[0] == "STANDARD"
    # already held -> refused; lore/commemorative -> refused; broke -> refused
    await _set_shards(conn, _USER, 60)
    with pytest.raises(PackError, match="already hold"):
        await craft_card(conn, _USER, "card_nort")
    with pytest.raises(PackError, match="can't be crafted"):
        await craft_card(conn, _USER, "lore_sink")
    async with conn.cursor() as cur:
        await cur.execute(
            "DELETE FROM user_cards WHERE user_id = %s AND card_key = 'card_anch'",
            (_USER,),
        )
    await _set_shards(conn, _USER, 10)
    with pytest.raises(PackError, match="Not enough shards"):
        await craft_card(conn, _USER, "card_anch")


async def test_featured_card_flair(conn: AsyncConnection) -> None:
    await bootstrap_user(conn, _USER)
    await _hold_card(conn, _USER, "card_nort", "GOLD")
    await set_featured_card(conn, _USER, "card_nort")

    stats = await profile_stats(conn, _USER)
    assert stats is not None
    assert stats.featured_card is not None
    assert "GOLD" in stats.featured_card

    flair = await equipped_flair_map(conn, [_USER])
    assert _USER in flair
    assert "🟡" in flair[_USER]

    # clear
    await set_featured_card(conn, _USER, None)
    stats = await profile_stats(conn, _USER)
    assert stats is not None and stats.featured_card is None


async def test_featured_card_requires_holding(conn: AsyncConnection) -> None:
    await bootstrap_user(conn, _USER)
    with pytest.raises(PackError, match="don't hold"):
        await set_featured_card(conn, _USER, "card_nort")


async def test_grant_commemorative(conn: AsyncConnection) -> None:
    await bootstrap_user(conn, _USER)
    assert await grant_commemorative(conn, _USER, "comm_halt_survivor") is True
    assert await grant_commemorative(conn, _USER, "comm_halt_survivor") is False
    with pytest.raises(PackError):
        await grant_commemorative(conn, _USER, "card_nort")


async def test_delisted_instrument_card_survives(conn: AsyncConnection) -> None:
    """C1: delisting flips is_active, never deletes instruments -- the
    card row, held copy, and frozen pool entry all survive."""
    await bootstrap_user(conn, _USER)
    await _hold_card(conn, _USER, "card_nort", "GOLD")
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE instruments SET is_active = FALSE WHERE ticker = 'NORT'"
        )
    pools = await load_pools(conn)
    assert "card_nort" in pools["INSTRUMENT"]
    card = await get_card(conn, "card_nort")
    assert card is not None
    rows = await list_collection(conn, _USER)
    assert any(r["key"] == "card_nort" and r["best_frame"] == "GOLD" for r in rows)
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE instruments SET is_active = TRUE WHERE ticker = 'NORT'"
        )


async def test_find_card(conn: AsyncConnection) -> None:
    assert (await find_card(conn, "card_nort")).name  # by key
    assert (await find_card(conn, "NORT")).name  # by ticker
    assert (await find_card(conn, "the whale")).name == "The Whale"  # by name
    assert await find_card(conn, "no-such-card-xyz") is None
    assert await get_card(conn, "card_nort") is not None


# ---------------------------------------------------------------------------
# Binder + feed rendering
# ---------------------------------------------------------------------------


async def test_collection_stats_and_list(conn: AsyncConnection) -> None:
    await bootstrap_user(conn, _USER)
    await _hold_card(conn, _USER, "card_nort", "GOLD")
    await _hold_card(conn, _USER, "card_anch", "STANDARD")
    await _hold_card(conn, _USER, "lore_whale", "EPIC")
    stats = await collection_stats(conn, _USER)
    assert stats["held"] == 2
    assert stats["instrument_total"] == 82
    assert stats["lore_held"] == 1
    assert stats["lore_total"] == 15
    assert stats["frame_gold"] == 1
    assert stats["frame_standard"] == 1
    rows = await list_collection(conn, _USER)
    assert len(rows) == 3
    assert {r["kind"] for r in rows} == {"INSTRUMENT", "LORE"}


def test_parse_cid() -> None:
    assert parse_cid("cards:pg:123:2") == ("pg", 123, 2)
    assert parse_cid("cards:pg:0:0") == ("pg", 0, 0)
    assert parse_cid("cards:pg::1") is None
    assert parse_cid("cards:pg:1") is None
    assert parse_cid("shop:pg:1:2") is None
    assert parse_cid("cards:pg:abc:2") is None


def test_build_page_pagination() -> None:
    rows = [
        {
            "key": f"card_{i:02d}",
            "name": f"Card {i:02d}",
            "kind": "INSTRUMENT",
            "set_key": "base",
            "sector_id": 1,
            "sector_name": "Technology",
            "rarity": None,
            "best_frame": "STANDARD",
            "copies": 1,
        }
        for i in range(40)
    ]
    stats = {"held": 40, "instrument_total": 82, "lore_held": 0, "lore_total": 15, "shards": 7}
    embed, view = build_page("Tester", stats, rows, 1234, 0)
    assert "40/82" in (embed.description or "")
    assert "Page 1/3" in (embed.footer.text or "")
    assert len(view.children) == 2

    embed3, view3 = build_page("Tester", stats, rows, 1234, 2)
    assert "Page 3/3" in (embed3.footer.text or "")
    prev, nxt = view3.children
    assert prev.disabled is False
    assert nxt.disabled is True

    # out-of-range page clamps
    embed9, _ = build_page("Tester", stats, rows, 1234, 99)
    assert "Page 3/3" in (embed9.footer.text or "")


def test_build_page_empty() -> None:
    embed, view = build_page("Tester", {}, [], 1, 0)
    assert "No cards yet" in (embed.description or "")
    assert len(view.children) == 0


def test_feed_card_pull_formatter() -> None:
    line = _fmt_card_pull(
        [{"subject": 42, "tier": "LEGENDARY", "name": "The Sink"}]
    )
    assert "<@42>" in line
    assert "LEGENDARY" in line
    assert "The Sink" in line


# ---------------------------------------------------------------------------
# Completion badges (K4: badge_base_set / badge_lore_set / badge_gold_set)
# ---------------------------------------------------------------------------


async def test_completion_badges(conn: AsyncConnection) -> None:
    from stockbot.status.service import evaluate_badges

    await bootstrap_user(conn, _USER)
    pools = await load_pools(conn)
    # All 82 instruments at STANDARD + all 15 lore -> base_set and
    # lore_set grant; gold_set does not (frames too low).
    for key in pools["INSTRUMENT"]:
        await _hold_card(conn, _USER, key, "STANDARD")
    for key in pools["EPIC"] + pools["LEGENDARY"]:
        await _hold_card(conn, _USER, key, "STANDARD")

    n = await evaluate_badges(conn, tick_index=1440, interval_ticks=1)
    assert n >= 2
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT item_key FROM entitlements WHERE user_id = %s AND item_key LIKE 'badge_%%'",
            (_USER,),
        )
        got = {r[0] for r in await cur.fetchall()}
    assert "badge_base_set" in got
    assert "badge_lore_set" in got
    assert "badge_gold_set" not in got

    # Raise all instrument frames to GOLD -> gilded badge lands.
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE user_cards SET best_frame = 'GOLD' WHERE user_id = %s",
            (_USER,),
        )
    await evaluate_badges(conn, tick_index=1440, interval_ticks=1)
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT 1 FROM entitlements WHERE user_id = %s AND item_key = 'badge_gold_set'",
            (_USER,),
        )
        assert await cur.fetchone() is not None

    # Off-day tick grants nothing new; re-running the same day is idempotent.
    assert await evaluate_badges(conn, tick_index=2880, interval_ticks=1440) == 0
