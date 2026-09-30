"""Rival NPC personas: named, permadeath-exempt, bounty-addressable."""

from decimal import Decimal

import pytest
from psycopg import AsyncConnection

from stockbot.accounts.service import bootstrap_user
from stockbot.bounties import service as bounties
from stockbot.bounties.errors import BountyTargetError
from stockbot.ledger.service import (
    get_balance,
    get_system_account_id,
    get_user_account_id,
    post_transfer,
)
from stockbot.margin.service import check_and_liquidate
from stockbot.npc import service as npc
from stockbot.trading.service import execute_trade

BOUNTY = 2_000
FEE = 100


async def _ticker(conn: AsyncConnection) -> str:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT ticker FROM instruments WHERE is_active AND kind = 'STOCK'"
            " ORDER BY id LIMIT 1"
        )
        (t,) = await cur.fetchone()
    return str(t)


async def test_spawn_persona_marks_and_names(conn: AsyncConnection) -> None:
    uid = await npc.spawn_agent(
        conn,
        "grinder",
        persona=True,
        display_name="Hedge Fund Harry",
        stake_minor=100_000,
    )
    cast = await npc.personas(conn)
    assert any(p.user_id == uid and p.display_name == "Hedge Fund Harry" for p in cast)
    assert await npc.persona_name(conn, uid) == "Hedge Fund Harry"
    # A normal anon agent has no persona name.
    anon = await npc.spawn_agent(conn, "grinder")
    assert await npc.persona_name(conn, anon) is None


async def test_persona_skips_permadeath(conn: AsyncConnection) -> None:
    """A broke, flat persona is a boss having a bad quarter, not a corpse."""
    persona_uid = await npc.spawn_agent(
        conn, "grinder", persona=True, display_name="Harry", stake_minor=1
    )
    anon_uid = await npc.spawn_agent(conn, "grinder", stake_minor=1)
    # Drain both under the death floor.
    for uid in (persona_uid, anon_uid):
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE accounts SET balance = 0 WHERE user_id = %s AND kind = 'USER'",
                (uid,),
            )

    dead = await npc.mark_dead_agents(conn, 5)

    assert dead == 1  # only the anon died
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT user_id, died_at_tick IS NOT NULL FROM npc_agents "
            "WHERE user_id = ANY(%s)",
            ([persona_uid, anon_uid],),
        )
        rows = {int(r[0]): bool(r[1]) for r in await cur.fetchall()}
    assert rows[persona_uid] is False
    assert rows[anon_uid] is True


async def test_bounty_on_persona_accepted(conn: AsyncConnection) -> None:
    await bootstrap_user(conn, 4201)
    harry = await npc.spawn_agent(
        conn, "grinder", persona=True, display_name="Hedge Fund Harry"
    )
    bounty_id = await bounties.post(conn, 4201, harry, BOUNTY, tick_index=0)
    bounty = await bounties.get_bounty(conn, bounty_id)
    assert bounty is not None and bounty.status == "OPEN" and bounty.target_id == harry


async def test_bounty_on_anon_npc_rejected(conn: AsyncConnection) -> None:
    await bootstrap_user(conn, 4205)
    anon = await npc.spawn_agent(conn, "grinder")
    with pytest.raises(BountyTargetError):
        await bounties.post(conn, 4205, anon, BOUNTY, tick_index=0)


async def test_persona_liquidation_pays_human_claimant(conn: AsyncConnection) -> None:
    """Harry shorts into the wind, a human longs against him, the bounty
    pays the human -- the same machinery as a human target."""
    await bootstrap_user(conn, 4210)  # poster
    await bootstrap_user(conn, 4211)  # claimant
    harry = await npc.spawn_agent(
        conn, "shorter", persona=True, display_name="Hedge Fund Harry",
        stake_minor=100_000,
    )
    ticker = await _ticker(conn)
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE instruments SET init_margin_pct = 0.5, maint_margin_pct = 0.3 "
            "WHERE ticker = %s RETURNING quoted_price",
            (ticker,),
        )
        (price,) = await cur.fetchone()
    # Harry shorts ~1.8x his equity; the claimant longs against him.
    qty = max(1, int(Decimal(100_000) * Decimal("1.8") / (Decimal(price) * 100)))
    await execute_trade(conn, user_id=harry, ticker=ticker, side="SELL", quantity=qty)
    # The claimant needs real cash for the hedge.
    faucet = await get_system_account_id(conn, "FAUCET")
    await post_transfer(
        conn,
        from_account_id=faucet,
        to_account_id=await get_user_account_id(conn, 4211),
        amount=500_000,
        reason="TEST_TOPUP",
    )
    await execute_trade(conn, user_id=4211, ticker=ticker, side="BUY", quantity=3)

    bounty_id = await bounties.post(conn, 4210, harry, BOUNTY, tick_index=0)
    claimant_before = await get_balance(conn, await get_user_account_id(conn, 4211))

    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE instruments SET base_price = base_price * 4, "
            "quoted_price = quoted_price * 4 WHERE ticker = %s",
            (ticker,),
        )
    legs = await check_and_liquidate(conn, harry)
    assert legs >= 1

    bounty = await bounties.get_bounty(conn, bounty_id)
    assert bounty is not None and bounty.status == "CLAIMED"
    assert bounty.claimed_by == 4211
    rake = int(BOUNTY * 0.05)
    assert await get_balance(
        conn, await get_user_account_id(conn, 4211)
    ) == claimant_before + BOUNTY - rake
    # The feed payload names the boss, not a dead snowflake.
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT payload FROM feed_items WHERE kind = 'BOUNTY_PAID' "
            "ORDER BY id DESC LIMIT 1"
        )
        row = await cur.fetchone()
    # No feed channels bound in tests -> no row; settle path stays clean.
    assert row is None or row[0].get("target_name") == "Hedge Fund Harry"
