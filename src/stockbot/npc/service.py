"""NPC runner service layer: agent lifecycle and the per-tick action
round. Discord-free -- the process wrapper lives in `npc/main.py`.

`spawn_agent` is the ONLY money path an NPC ever sees: bootstrap +
is_bot flag + one `NPC_STAKE` grant + the npc_agents row, all in the
caller's transaction. There is deliberately no top-up function --
permadeath is the safety model (dead agents stay dead; population
replenishment is new spawns, a fresh bounded grant each).

`run_round` executes one tick's worth of agent actions. Each agent's
decision RNG is keyed `master_seed|npc|user_id|tick` so a given tick's
decisions reproduce; wall-clock ordering vs. humans does not (same
limitation replay.py documents). Actions are staggered across the
tick interval in `main.py` -- this function only picks and executes.
"""

from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass

from psycopg import AsyncConnection
from psycopg.rows import dict_row

from stockbot.accounts.service import bootstrap_user
from stockbot.ledger.errors import InsufficientFundsError
from stockbot.ledger.service import (
    get_balance,
    get_system_account_id,
    get_user_account_id,
    post_transfer,
)
from stockbot.margin.errors import MarginError
from stockbot.market.data import open_market_ids
from stockbot.status.service import net_worth_minor
from stockbot.trading.errors import TradingError

log = logging.getLogger("stockbot.npc")

# user_ids far below any Discord snowflake (>= ~1.6e17) and below the
# sim harness's 9e14 block so a leaked sim user can't collide either.
NPC_USER_ID_BASE = 800_000_000_000_000


@dataclass(frozen=True)
class NpcAgent:
    user_id: int
    archetype: str
    label: str
    quote_ticker: str | None


async def _config_float(conn: AsyncConnection, key: str, default: float) -> float:
    async with conn.cursor() as cur:
        await cur.execute("SELECT value FROM config WHERE key = %s", (key,))
        row = await cur.fetchone()
    return float(row[0]) if row else default


async def npc_enabled(conn: AsyncConnection) -> bool:
    return await _config_float(conn, "npc.enabled", 0.0) != 0.0


async def spawn_agent(
    conn: AsyncConnection,
    archetype: str,
    *,
    label: str | None = None,
    quote_ticker: str | None = None,
    stake_minor: int | None = None,
) -> int:
    """Create one synthetic agent. Returns its user_id.

    Runs inside the caller's transaction. user_id allocation is
    MAX(is_bot)+1 from NPC_USER_ID_BASE -- serialized by the singleton
    runner lock in production; concurrent manual spawns could race on
    the MAX and the second loses to the users PK (acceptable: spawns
    are rare and the loser just retries).
    """
    stake = (
        stake_minor
        if stake_minor is not None
        else int(await _config_float(conn, "npc.stake_minor", 100_000.0))
    )
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT COALESCE(MAX(id), %s) + 1 FROM users WHERE is_bot",
            (NPC_USER_ID_BASE - 1,),
        )
        row = await cur.fetchone()
        assert row is not None
        user_id = int(row[0])
    await bootstrap_user(conn, user_id)
    async with conn.cursor() as cur:
        await cur.execute("UPDATE users SET is_bot = TRUE WHERE id = %s", (user_id,))
    account_id = await get_user_account_id(conn, user_id)
    faucet_id = await get_system_account_id(conn, "FAUCET")
    # The ONLY FAUCET->NPC flow anywhere: one bounded grant at spawn.
    # The ledger audit's sum-zero invariant is preserved by post_transfer.
    await post_transfer(
        conn,
        from_account_id=faucet_id,
        to_account_id=account_id,
        amount=stake,
        reason="NPC_STAKE",
        memo=archetype,
    )
    async with conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO npc_agents (user_id, archetype, label, quote_ticker)
            VALUES (%s, %s, %s, %s)
            """,
            (user_id, archetype, label or f"npc-{archetype}-{user_id}", quote_ticker),
        )
    if archetype == "shorter":
        # Margin tier is an ops grant, not a purchase -- debiting the
        # stake for it would just move the same bounded money internally.
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO entitlements (user_id, item_key, quantity)
                VALUES (%s, 'margin_tier', 1)
                ON CONFLICT (user_id, item_key) DO NOTHING
                """,
                (user_id,),
            )
    return user_id


async def load_agents(conn: AsyncConnection) -> list[NpcAgent]:
    """Enabled, living agents (died_at_tick IS NULL -- permadeath)."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            SELECT user_id, archetype, label, quote_ticker
            FROM npc_agents
            WHERE enabled AND died_at_tick IS NULL
            ORDER BY user_id
            """
        )
        return [
            NpcAgent(
                user_id=int(r["user_id"]),
                archetype=str(r["archetype"]),
                label=str(r["label"]),
                quote_ticker=r["quote_ticker"],
            )
            for r in await cur.fetchall()
        ]


async def _random_open_ticker(
    conn: AsyncConnection, rng: random.Random, open_mids: set[int]
) -> str | None:
    """Venue-scoped pick (C10): a closed-venue ticker would only raise
    MarketClosedError into the broad catch -- the same trap that once
    made the harness silently place zero orders."""
    if not open_mids:
        return None
    async with conn.cursor() as cur:
        # ORDER BY: rng.choice() must be deterministic for a given seed.
        await cur.execute(
            """
            SELECT ticker FROM instruments
            WHERE is_active AND circuit_halted_until_tick IS NULL
              AND market_id = ANY(%s)
            ORDER BY ticker
            """,
            (sorted(open_mids),),
        )
        tickers = [row[0] for row in await cur.fetchall()]
    return rng.choice(tickers) if tickers else None


@dataclass
class RoundContext:
    conn: AsyncConnection
    rng: random.Random
    tick_index: int
    open_mids: set[int]
    budget_minor: int  # remaining npc.max_tick_notional this round
    acted: int = 0
    trades: int = 0


async def _act_grinder(ctx: RoundContext, agent: NpcAgent) -> int:
    """Slow accumulators: buy a fraction of balance on an open-venue
    name. Ported per-tick from the harness's 0.3/day, 5-15% spend."""
    ticker = await _random_open_ticker(ctx.conn, ctx.rng, ctx.open_mids)
    if ticker is None:
        return 0
    account_id = await get_user_account_id(ctx.conn, agent.user_id)
    balance = await get_balance(ctx.conn, account_id)
    spend = int(balance * ctx.rng.uniform(0.05, 0.15))
    if spend < 100:
        return 0
    from stockbot.trading.service import execute_trade  # lazy: avoids cycles

    async with ctx.conn.cursor() as cur:
        await cur.execute("SELECT quoted_price FROM instruments WHERE ticker = %s", (ticker,))
        row = await cur.fetchone()
    price_minor = int(float(row[0]) * 100) if row else 100
    quantity = max(1, spend // max(price_minor, 1))
    await execute_trade(
        ctx.conn, user_id=agent.user_id, ticker=ticker, side="BUY", quantity=quantity
    )
    return min(spend, quantity * price_minor)


# Archetype dispatch -- P2 ships grinder only; P3 ports the rest.
_ACTIONS = {
    "grinder": _act_grinder,
}


async def run_round(
    conn: AsyncConnection,
    master_seed: str,
    tick_index: int,
    *,
    interval_seconds: float = 60.0,
    sleep: bool = True,
) -> dict[str, int]:
    """One NPC action round for `tick_index`.

    Picks acting agents (per-agent `action_prob_per_tick` draw on a
    tick-keyed RNG), assigns each a random delay inside the tick
    interval (the stagger -- keeps NPC flow out of one pending_flow
    batch and off apply_tick's instrument locks at tick+0), then runs
    them serially under the aggregate `npc.max_tick_notional` budget
    (C5 -- the only population-level bound). `sleep=False` executes
    immediately (tests, manual rounds).

    Transaction discipline (the market/main.py footgun): every phase
    sits in an explicit `conn.transaction()`. A bare cursor read would
    leave an ambient transaction open and silently demote every later
    transaction() to a never-committed savepoint. Each ACTION is its own
    transaction too -- commits are per-action like a human's slash
    command, and the sleep happens OUTSIDE the tx so a long stagger
    doesn't hold locks.
    """
    async with conn.transaction():
        agents = await load_agents(conn)
        if not agents:
            return {"acted": 0, "agents": 0, "dead": 0}
        open_mids = await open_market_ids(conn, tick_index)
        action_prob = await _config_float(conn, "npc.action_prob_per_tick", 0.002)
        budget_minor = int((await _config_float(conn, "npc.max_tick_notional", 25_000.0)) * 100)
    ctx = RoundContext(
        conn=conn,
        rng=random.Random(f"{master_seed}|npc-round|{tick_index}"),
        tick_index=tick_index,
        open_mids=open_mids,
        budget_minor=budget_minor,
    )
    for agent in agents:
        # Per-agent decision RNG: reproducible given (seed, uid, tick).
        agent_rng = random.Random(f"{master_seed}|npc|{agent.user_id}|{tick_index}")
        if agent_rng.random() >= action_prob:
            continue
        if ctx.budget_minor <= 0:
            break
        delay = ctx.rng.uniform(0, interval_seconds * 0.75) if sleep else 0.0
        if delay > 0:
            await asyncio.sleep(delay)
        action = _ACTIONS.get(agent.archetype)
        if action is None:
            continue
        act_ctx = RoundContext(
            conn=conn,
            rng=agent_rng,
            tick_index=tick_index,
            open_mids=open_mids,
            budget_minor=ctx.budget_minor,
        )
        try:
            async with conn.transaction():
                notional = await action(act_ctx, agent)
        except (InsufficientFundsError, TradingError, MarginError) as exc:
            # Broke/blocked agents just don't act this round -- the death
            # sweep decides whether they're done for good.
            log.debug("npc %s (%s) skipped: %s", agent.user_id, agent.archetype, exc)
            notional = 0
        if notional > 0:
            ctx.acted += 1
            ctx.trades += 1
            ctx.budget_minor -= notional
    async with conn.transaction():
        dead = await mark_dead_agents(conn, tick_index)
    return {"acted": ctx.acted, "agents": len(agents), "dead": dead}


async def mark_dead_agents(conn: AsyncConnection, tick_index: int) -> int:
    """Permadeath sweep: enabled agents whose NET WORTH (not cash -- a
    fully-invested agent isn't dead) sits below `npc.death_balance_minor`
    with no open positions get `died_at_tick` stamped once, never cleared."""
    floor = int(await _config_float(conn, "npc.death_balance_minor", 100.0))
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            SELECT n.user_id FROM npc_agents n
            WHERE n.enabled AND n.died_at_tick IS NULL
            ORDER BY n.user_id
            """
        )
        candidates = [int(r["user_id"]) for r in await cur.fetchall()]
    dead = 0
    for uid in candidates:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT COUNT(*) FROM positions WHERE user_id = %s AND quantity <> 0",
                (uid,),
            )
            row = await cur.fetchone()
            assert row is not None
            has_positions = int(row[0]) > 0
        if has_positions:
            continue
        if await net_worth_minor(conn, uid) < floor:
            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE npc_agents SET died_at_tick = %s "
                    "WHERE user_id = %s AND died_at_tick IS NULL",
                    (tick_index, uid),
                )
                dead += cur.rowcount
    return dead
