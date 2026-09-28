"""P3 exit soak: run the NPC roster against real ticks on a SCRATCH
database and report the population economics the plan asks for.

    python -m stockbot.npc.soak --database-url postgresql://... --ticks 2000

WARNING: advances the market (apply_tick) and writes committed state --
point it at a scratch DB, never production.

Reported per phase boundary:
- money supply / Gini over ALL user net worths (human + NPC),
- per-archetype alive/dead counts -> half-life signal (C7),
- open resting orders -> does LP depth persist between active ticks,
- NPC_STAKE ledger conservation: credits == spawned stakes exactly
  (the bounded-injection invariant), and no other FAUCET->bot flow.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass

from psycopg import AsyncConnection

from stockbot.config import get_settings
from stockbot.market.tick import apply_tick
from stockbot.npc.service import mark_dead_agents, run_round, spawn_agent
from stockbot.simulation.harness import economy_snapshot, net_worth_by_user

DEFAULT_ROSTER: dict[str, int] = {
    "whale": 4,
    "grinder": 10,
    "yolo": 6,
    "shorter": 4,
    "liquidity_provider": 4,
    "stop_loss": 4,
}


@dataclass
class Snapshot:
    tick: int
    money_supply: float
    gini: float
    resting_orders: int
    alive: dict[str, int]
    dead: dict[str, int]


def _gini(values: list[int]) -> float:
    if not values:
        return 0.0
    v = sorted(max(0, x) for x in values)
    total = sum(v)
    if total <= 0:
        return 0.0
    n = len(v)
    cum = sum((i + 1) * x for i, x in enumerate(v))
    return (2 * cum) / (n * total) - (n + 1) / n


async def _census(conn: AsyncConnection) -> tuple[dict[str, int], dict[str, int]]:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT archetype, died_at_tick IS NOT NULL AS dead, COUNT(*) "
            "FROM npc_agents GROUP BY archetype, dead"
        )
        alive: dict[str, int] = {}
        dead: dict[str, int] = {}
        for archetype, is_dead, count in await cur.fetchall():
            (dead if is_dead else alive)[str(archetype)] = int(count)
    return alive, dead


async def _snapshot(conn: AsyncConnection, tick: int) -> Snapshot:
    econ = await economy_snapshot(conn)
    async with conn.cursor() as cur:
        await cur.execute("SELECT id FROM users")
        user_ids = [int(r[0]) for r in await cur.fetchall()]
        await cur.execute("SELECT COUNT(*) FROM orders WHERE status = 'OPEN'")
        row = await cur.fetchone()
        assert row is not None
        resting = int(row[0])
    net_worths = await net_worth_by_user(conn, user_ids)
    alive, dead = await _census(conn)
    return Snapshot(
        tick=tick,
        money_supply=float(econ.get("money_supply", 0.0)),
        gini=_gini(list(net_worths.values())),
        resting_orders=resting,
        alive=alive,
        dead=dead,
    )


async def _audit_stakes(conn: AsyncConnection) -> dict[str, int]:
    """Bounded-injection invariant: group positive credits to bot
    accounts by reason, but ONLY where the transfer's debit leg is the
    FAUCET system account -- MM fill payments (TRADE_*) are earned P&L,
    not injection."""
    async with conn.cursor() as cur:
        await cur.execute(
            """
            SELECT e.reason, COALESCE(SUM(e.amount), 0)
            FROM ledger_entries e
            JOIN accounts a ON a.id = e.account_id
            JOIN users u ON u.id = a.user_id AND u.is_bot
            JOIN ledger_entries f
              ON f.transfer_id = e.transfer_id AND f.amount < 0
            JOIN accounts fa
              ON fa.id = f.account_id AND fa.system_name = 'FAUCET'
            WHERE e.amount > 0
            GROUP BY e.reason
            """
        )
        return {str(r[0]): int(r[1]) for r in await cur.fetchall()}


async def run_soak(
    conn: AsyncConnection,
    *,
    master_seed: str,
    ticks: int,
    roster: dict[str, int],
    stake_minor: int,
    action_prob: float,
    report_every: int,
) -> list[Snapshot]:
    async with conn.transaction():
        await conn.execute("UPDATE config SET value = 1 WHERE key = 'npc.enabled'")
        await conn.execute(
            "UPDATE config SET value = %s WHERE key = 'npc.action_prob_per_tick'",
            (action_prob,),
        )
        user_ids: dict[str, list[int]] = {}
        for archetype, count in roster.items():
            user_ids[archetype] = [
                await spawn_agent(conn, archetype, stake_minor=stake_minor) for _ in range(count)
            ]
    snapshots: list[Snapshot] = []
    for tick in range(ticks):
        applied = await apply_tick(conn, master_seed)
        await run_round(conn, master_seed, applied, sleep=False)
        # mark_dead_agents runs inside run_round; nothing extra here.
        if tick % report_every == 0 or tick == ticks - 1:
            snap = await _snapshot(conn, applied)
            snapshots.append(snap)
            print(
                f"tick {applied:>6} | supply {snap.money_supply:>14,.0f} | "
                f"gini {snap.gini:.3f} | resting {snap.resting_orders:>4} | "
                f"alive {sum(snap.alive.values()):>3} dead {sum(snap.dead.values()):>3}"
            )
    audit = await _audit_stakes(conn)
    stake_credits = audit.get("NPC_STAKE", 0)
    expected = stake_minor * sum(roster.values())
    print(f"\nNPC_STAKE credits: {stake_credits:,} (expected {expected:,})")
    others = {k: v for k, v in audit.items() if k != "NPC_STAKE"}
    print(f"Other FAUCET-ish positive flows to bots: {others or 'none'}")
    alive, dead = await _census(conn)
    print(f"Final census -- alive: {alive} | dead: {dead}")
    # mark_dead_agents once more for a clean final census read.
    await mark_dead_agents(conn, ticks)
    return snapshots


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database-url",
        required=True,
        help="SCRATCH database to soak against -- the soak advances the "
        "market and commits agents/trades, so it MUST NOT point at the "
        "suite's TEST_DATABASE_URL (committed state leaks between tests).",
    )
    parser.add_argument("--ticks", type=int, default=2880)
    parser.add_argument("--stake-minor", type=int, default=200_000)
    parser.add_argument("--action-prob", type=float, default=0.02)
    parser.add_argument("--report-every", type=int, default=240)
    args = parser.parse_args(argv)

    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    settings = get_settings()
    seed = settings.master_seed

    async def _run() -> None:
        conn = await AsyncConnection.connect(args.database_url, autocommit=False)
        try:
            await run_soak(
                conn,
                master_seed=seed,
                ticks=args.ticks,
                roster=DEFAULT_ROSTER,
                stake_minor=args.stake_minor,
                action_prob=args.action_prob,
                report_every=args.report_every,
            )
            await conn.commit()
        finally:
            await conn.close()

    asyncio.run(_run())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
