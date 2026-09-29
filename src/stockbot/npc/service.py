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
from datetime import datetime

from psycopg import AsyncConnection
from psycopg.rows import dict_row

from stockbot.accounts.service import bootstrap_user
from stockbot.ledger.errors import InsufficientFundsError
from stockbot.ledger.service import (
    get_system_account_id,
    get_user_account_id,
    post_transfer,
)
from stockbot.margin.errors import MarginError
from stockbot.market.data import open_market_ids
from stockbot.npc.agents import ACTIONS, TICKS_PER_DAY, NpcAgent, RoundContext
from stockbot.status.service import net_worth_minor
from stockbot.trading.errors import TradingError

log = logging.getLogger("stockbot.npc")

# user_ids far below any Discord snowflake (>= ~1.6e17) and below the
# sim harness's 9e14 block so a leaked sim user can't collide either.
NPC_USER_ID_BASE = 800_000_000_000_000

# Bounds on the target_adv_share feedback multiplier: enough to chase
# the target, not enough for a quiet tape to dump the whole population's
# probability budget into one tick.
_SHARE_MULT_LO, _SHARE_MULT_HI = 0.25, 4.0


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

    Raises ValueError for an unknown archetype or a quote_ticker that
    isn't an active instrument -- without the checks the spawn produces
    a funded, enabled, permanently inert agent (ACTIONS.get -> skip).
    """
    if archetype not in ACTIONS:
        raise ValueError(
            f"unknown archetype {archetype!r} "
            f"(known: {', '.join(sorted(ACTIONS))})"
        )
    if quote_ticker is not None:
        quote_ticker = quote_ticker.upper()
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT 1 FROM instruments WHERE ticker = %s AND is_active",
                (quote_ticker,),
            )
            if await cur.fetchone() is None:
                raise ValueError(
                    f"unknown or inactive quote_ticker {quote_ticker!r}"
                )
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
    # No STARTING_GRANT: synthetic snowflakes trivially pass the age
    # gate, and spawn (NPC_STAKE) is the ONLY faucet draw an agent gets.
    await bootstrap_user(conn, user_id, starting_grant=False)
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


async def _npc_flow_share(
    conn: AsyncConnection, tick_index: int, window_ticks: int
) -> float | None:
    """NPC share of executed notional over the trailing `window_ticks`.
    Returns None when the window had no flow at all -- nothing to
    modulate against."""
    async with conn.cursor() as cur:
        await cur.execute(
            """
            SELECT COALESCE(SUM(t.notional_minor)
                            FILTER (WHERE u.is_bot), 0),
                   COALESCE(SUM(t.notional_minor), 0)
            FROM trades t JOIN users u ON u.id = t.user_id
            WHERE t.tick_index > %s AND t.tick_index <= %s
            """,
            (tick_index - window_ticks, tick_index),
        )
        row = await cur.fetchone()
    assert row is not None
    bot_minor, total_minor = int(row[0]), int(row[1])
    return bot_minor / total_minor if total_minor > 0 else None


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
        # npc.target_adv_share is the feedback loop: scale the per-agent
        # draw toward NPC flow being `target` of trailing-day notional.
        # The multiplier is clamped so a quiet tape can't detonate the
        # whole population's probability budget in one tick, and an
        # empty window (total flow 0) leaves the base rate alone.
        target_share = await _config_float(conn, "npc.target_adv_share", 0.0)
        if target_share > 0:
            share = await _npc_flow_share(conn, tick_index, TICKS_PER_DAY)
            if share is not None:
                mult = target_share / max(share, 0.01)
                action_prob *= min(_SHARE_MULT_HI, max(_SHARE_MULT_LO, mult))
    acted = 0
    ctx = RoundContext(
        conn=conn,
        rng=random.Random(f"{master_seed}|npc-round|{tick_index}"),
        tick_index=tick_index,
        open_mids=open_mids,
        budget_minor=budget_minor,
    )
    try:
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
            action = ACTIONS.get(agent.archetype)
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
                # Broke/blocked agents just don't act this round -- the
                # death sweep decides whether they're done for good.
                log.debug("npc %s (%s) skipped: %s", agent.user_id, agent.archetype, exc)
                notional = 0
            except Exception:
                # Unexpected errors are per-action too: the transaction
                # rolled back, so the blast radius is this one action.
                # Without this an agent that throws deterministically
                # starves every successor AND skips the death sweep --
                # the loop already consumed last_tick, so the round
                # never retries.
                log.warning(
                    "npc %s (%s) action failed",
                    agent.user_id,
                    agent.archetype,
                    exc_info=True,
                )
                notional = 0
            if notional > 0:
                acted += 1
                ctx.budget_minor -= notional
    finally:
        # The sweep runs even if the loop unwinds: dead agents must be
        # stamped this round, not whenever a future round succeeds.
        async with conn.transaction():
            dead = await mark_dead_agents(conn, tick_index)
    return {"acted": acted, "agents": len(agents), "dead": dead}


async def _live_instrument_count(conn: AsyncConnection, user_id: int) -> int:
    """Everything an agent still rides: positions, resting orders,
    bounded shorts, open options. 'Dead' means flat -- an agent with
    sub-floor cash but paper outstanding isn't done (a dead LP's stale
    quotes would otherwise keep filling strangers for days)."""
    async with conn.cursor() as cur:
        await cur.execute(
            """
            SELECT (SELECT COUNT(*) FROM positions
                     WHERE user_id = %s AND quantity <> 0)
                 + (SELECT COUNT(*) FROM orders
                     WHERE user_id = %s AND status = 'OPEN')
                 + (SELECT COUNT(*) FROM bounded_shorts
                     WHERE user_id = %s AND status = 'OPEN')
                 + (SELECT COUNT(*) FROM option_positions
                     WHERE user_id = %s AND status = 'OPEN')
            """,
            (user_id, user_id, user_id, user_id),
        )
        row = await cur.fetchone()
    return int(row[0]) if row else 0


async def mark_dead_agents(conn: AsyncConnection, tick_index: int) -> int:
    """Permadeath sweep: enabled agents whose NET WORTH (not cash -- a
    fully-invested agent isn't dead) sits below `npc.death_balance_minor`
    with no live instrument exposure get `died_at_tick` stamped once,
    never cleared."""
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
        if await _live_instrument_count(conn, uid) > 0:
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


# --- Admin surface (P4) -------------------------------------------------
# Read-only telemetry and the two controls the plan allows: per-agent
# enable/disable and bounded manual spawns. Dead agents stay dead --
# "replenishment" is a new spawn, never a revive.


@dataclass(frozen=True)
class AgentReport:
    user_id: int
    archetype: str
    label: str
    enabled: bool
    died_at_tick: int | None
    quote_ticker: str | None
    created_at: datetime
    equity_minor: int
    funded_minor: int  # NPC_STAKE (+ STARTING_GRANT on legacy rows)
    live_instruments: int  # positions + open orders + shorts + options

    @property
    def pnl_minor(self) -> int:
        return self.equity_minor - self.funded_minor


async def agent_report(conn: AsyncConnection) -> list[AgentReport]:
    """Census + per-agent P&L for `/admin npc-list`. Net worth comes from
    the same user-scoped path the runner's death check uses; funded is
    the ledger's own record of spawn money (STARTING_GRANT + NPC_STAKE),
    so P&L is equity minus exactly what ops put in. `live_instruments`
    counts all outstanding paper -- the same predicate the death sweep
    uses, so the census can't show a 'flat' agent that isn't."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            SELECT n.user_id, n.archetype, n.label, n.enabled,
                   n.died_at_tick, n.quote_ticker, n.created_at,
                   a.id AS account_id
            FROM npc_agents n
            JOIN accounts a ON a.user_id = n.user_id AND a.kind = 'USER'
            ORDER BY n.user_id
            """
        )
        rows = await cur.fetchall()
    out: list[AgentReport] = []
    for r in rows:
        uid = int(r["user_id"])
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT COALESCE(SUM(amount), 0) FROM ledger_entries "
                "WHERE account_id = %s AND reason IN ('NPC_STAKE', 'STARTING_GRANT')",
                (int(r["account_id"]),),
            )
            funded = int((await cur.fetchone() or (0,))[0])
        out.append(
            AgentReport(
                user_id=uid,
                archetype=str(r["archetype"]),
                label=str(r["label"]),
                enabled=bool(r["enabled"]),
                died_at_tick=r["died_at_tick"],
                quote_ticker=r["quote_ticker"],
                created_at=r["created_at"],
                equity_minor=await net_worth_minor(conn, uid),
                funded_minor=funded,
                live_instruments=await _live_instrument_count(conn, uid),
            )
        )
    return out


async def set_agent_enabled(conn: AsyncConnection, *, label: str, enabled: bool) -> NpcAgent | None:
    """Flip one agent's enabled flag by label (or numeric user_id).
    Dead agents can't be re-enabled -- died_at_tick is the permadeath
    stamp, not a state to toggle back."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            UPDATE npc_agents SET enabled = %s
            WHERE died_at_tick IS NULL AND (label = %s OR user_id::text = %s)
            RETURNING user_id, archetype, label, quote_ticker
            """,
            (enabled, label, label),
        )
        row = await cur.fetchone()
    if row is None:
        return None
    return NpcAgent(
        user_id=int(row["user_id"]),
        archetype=str(row["archetype"]),
        label=str(row["label"]),
        quote_ticker=row["quote_ticker"],
    )
