"""Economy simulation harness.

Runs N synthetic users through M simulated days against the *real* tick
engine and the *real* accounts/claims/trading service code -- this is not a
separate model of the economy, it exercises production code paths end to
end. Per the design doc, this is the tuning instrument for the whole
economy: money supply over time, wealth (in)equality, the faucet/sink
ratio, and whether a farmer or a colluding wash-trading pair comes out
ahead of honest play.

Behavioral archetypes:
    grinder      claims daily, trades occasionally and modestly
    yolo         claims daily, occasionally bets a large fraction of cash
    farmer       claims daily, never trades (pure faucet farming)
    wash_trader  two-account pairs that pump-and-dump an illiquid ticker:
                 one buys, its partner sells into the pump, same tick --
                 exactly what compliance/wash_trade.py looks for
    whale        starts with a large synthetic balance, trades large blocks
    shorter      buys a margin tier, opens/covers true shorts sized to equity
    liquidity_provider  rests two-sided limit quotes around the mark each
                 day without cancelling -- stale quotes at old marks cross
                 fresh ones (and the MM) as prices drift, exercising the
                 crossing book
    stop_loss    holds one name under a protective SELL stop trailing
                 ~4-9% below the mark; re-enters after being stopped out,
                 exercising stop triggers and cascades

Usage:
    python -m stockbot.simulation.harness --database-url ... --users 200 --days 90

Strongly prefer a scratch/throwaway database: this inserts a large number
of rows and advances the tick sequence a lot. Synthetic users get ids
starting at SYNTHETIC_USER_ID_BASE (safely below any real Discord snowflake,
which are all >= ~1e17 -- the Discord epoch starts 2015-01-01),
so it *won't* touch real accounts if you do point it at a shared database,
but the price/tick history it generates is real and permanent.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import random
import sys
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from psycopg import AsyncConnection

from stockbot import db
from stockbot.accounts.service import bootstrap_user
from stockbot.claims.errors import AlreadyClaimedTodayError
from stockbot.claims.service import claim_daily
from stockbot.ledger.errors import InsufficientFundsError
from stockbot.ledger.service import (
    get_balance,
    get_system_account_id,
    get_user_account_id,
    post_transfer,
)
from stockbot.margin.errors import MarginError
from stockbot.margin.service import compute_health
from stockbot.market.tick import apply_tick
from stockbot.orders.service import cancel_order, list_open_orders, place_order
from stockbot.shop.service import buy_item
from stockbot.simulation.metrics import faucet_sink_ratio, gini_coefficient
from stockbot.trading.errors import TradingError
from stockbot.trading.service import execute_trade

log = logging.getLogger("stockbot.simulation")

SYNTHETIC_USER_ID_BASE = 900_000_000_000_000
WHALE_STARTING_GRANT_MINOR = 500_000  # $5,000
SHORTER_STARTING_GRANT_MINOR = 200_000  # $2,000: tier 1 ($500) + trading cash
LP_STARTING_GRANT_MINOR = 300_000  # $3,000: two-sided quotes need cash + inventory
STOPLOSS_STARTING_GRANT_MINOR = 100_000  # $1,000: needs a position worth protecting
TICKS_PER_SIMULATED_DAY = 1440
# Simulated days are mapped onto consecutive calendar dates starting here so
# `claim_daily` runs its real per-day logic (streaks included). Without this
# a 90-simulated-day run inside one real hour would give each agent a single
# claim and faucet income would barely exist in the results.
SIM_CLAIM_EPOCH = date(2025, 1, 1)


@dataclass
class Agent:
    user_id: int
    archetype: str
    wash_partner_id: int | None = None
    wash_ticker: str | None = None
    short_ticker: str | None = None
    quote_ticker: str | None = None
    stop_ticker: str | None = None


def build_cohort(num_users: int, rng: random.Random) -> list[Agent]:
    whale_count = max(1, num_users // 100)
    wash_pairs = max(1, num_users // 50)
    remaining = max(0, num_users - whale_count - wash_pairs * 2)
    grinder_count = round(remaining * 0.35)
    yolo_count = round(remaining * 0.2)
    shorter_count = round(remaining * 0.1)
    lp_count = round(remaining * 0.1)
    stoploss_count = round(remaining * 0.1)
    farmer_count = max(
        0,
        remaining
        - grinder_count
        - yolo_count
        - shorter_count
        - lp_count
        - stoploss_count,
    )

    agents: list[Agent] = []
    next_id = SYNTHETIC_USER_ID_BASE

    def _add(archetype: str, count: int, **kwargs: int | None) -> None:
        nonlocal next_id
        for _ in range(count):
            agents.append(Agent(next_id, archetype, **kwargs))  # type: ignore[arg-type]
            next_id += 1

    _add("whale", whale_count)
    for _ in range(wash_pairs):
        a, b = next_id, next_id + 1
        next_id += 2
        agents.append(Agent(a, "wash_trader", wash_partner_id=b))
        agents.append(Agent(b, "wash_trader", wash_partner_id=a))
    _add("grinder", grinder_count)
    _add("yolo", yolo_count)
    _add("shorter", shorter_count)
    _add("liquidity_provider", lp_count)
    _add("stop_loss", stoploss_count)
    _add("farmer", farmer_count)

    rng.shuffle(agents)
    return agents


async def initialize_agents(conn: AsyncConnection, agents: list[Agent]) -> None:
    faucet_id = await get_system_account_id(conn, "FAUCET")
    # Liquidity providers quote the most liquid names, assigned
    # deterministically round-robin so their books overlap.
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT ticker FROM instruments WHERE is_active AND kind != 'INDEX' "
            "ORDER BY liquidity DESC, ticker LIMIT 8"
        )
        lp_tickers = [row[0] for row in await cur.fetchall()]
    lp_slot = 0
    for agent in agents:
        await bootstrap_user(conn, agent.user_id)
        if agent.archetype in ("whale", "shorter", "liquidity_provider", "stop_loss"):
            account_id = await get_user_account_id(conn, agent.user_id)
            grant = {
                "whale": WHALE_STARTING_GRANT_MINOR,
                "shorter": SHORTER_STARTING_GRANT_MINOR,
                "liquidity_provider": LP_STARTING_GRANT_MINOR,
                "stop_loss": STOPLOSS_STARTING_GRANT_MINOR,
            }[agent.archetype]
            await post_transfer(
                conn,
                from_account_id=faucet_id,
                to_account_id=account_id,
                amount=grant,
                reason="SIM_SEED",
            )
        if agent.archetype == "shorter":
            # Unlock margin tier 1 (2x gross leverage) before the sim starts.
            from stockbot.shop.errors import ShopError

            try:
                await buy_item(conn, agent.user_id, "margin_tier")
            except (ShopError, InsufficientFundsError):
                log.warning("shorter %s couldn't buy margin tier", agent.user_id)
        if agent.archetype == "liquidity_provider" and lp_tickers:
            # Starter inventory so the ask side can fill from day one.
            agent.quote_ticker = str(lp_tickers[lp_slot % len(lp_tickers)])
            lp_slot += 1
            try:
                await execute_trade(
                    conn,
                    user_id=agent.user_id,
                    ticker=agent.quote_ticker,
                    side="BUY",
                    quantity=10,
                )
            except (InsufficientFundsError, TradingError, MarginError):
                log.warning(
                    "liquidity_provider %s couldn't seed inventory", agent.user_id
                )


async def _random_active_ticker(conn: AsyncConnection, rng: random.Random) -> str | None:
    async with conn.cursor() as cur:
        # ORDER BY matters: rng.choice() over the result must be
        # deterministic for a given seed, and Postgres row order isn't.
        await cur.execute(
            """
            SELECT ticker FROM instruments
            WHERE is_active AND circuit_halted_until_tick IS NULL
            ORDER BY ticker
            """
        )
        tickers = [row[0] for row in await cur.fetchall()]
    return rng.choice(tickers) if tickers else None


async def _safe_claim(conn: AsyncConnection, user_id: int, sim_today: date) -> None:
    try:
        await claim_daily(conn, user_id, as_of_date=sim_today)
    except AlreadyClaimedTodayError:
        pass


async def _quantity_for_spend(spend_minor: int, ticker_price_minor: int) -> int:
    return max(1, spend_minor // max(ticker_price_minor, 1))


async def _run_agent_day(
    conn: AsyncConnection, agent: Agent, rng: random.Random, sim_today: date
) -> None:
    try:
        if agent.archetype == "farmer":
            await _safe_claim(conn, agent.user_id, sim_today)
            return

        if agent.archetype in ("whale", "grinder", "yolo"):
            await _safe_claim(conn, agent.user_id, sim_today)
            chance, spend_range = {
                "whale": (0.4, (0.05, 0.2)),
                "grinder": (0.3, (0.05, 0.15)),
                "yolo": (0.5, (0.5, 0.95)),
            }[agent.archetype]
            if rng.random() >= chance:
                return
            ticker = await _random_active_ticker(conn, rng)
            if ticker is None:
                return
            account_id = await get_user_account_id(conn, agent.user_id)
            balance = await get_balance(conn, account_id)
            spend = int(balance * rng.uniform(*spend_range))
            if spend < 100:
                return
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT quoted_price FROM instruments WHERE ticker = %s", (ticker,)
                )
                row = await cur.fetchone()
            price_minor = int(float(row[0]) * 100) if row else 100
            quantity = await _quantity_for_spend(spend, price_minor)
            await execute_trade(
                conn, user_id=agent.user_id, ticker=ticker, side="BUY", quantity=quantity
            )
            return

        if agent.archetype == "wash_trader" and agent.wash_partner_id is not None:
            # Only the lower-id half of each pair drives the pair's schedule.
            if agent.user_id > agent.wash_partner_id:
                return
            await _safe_claim(conn, agent.user_id, sim_today)
            await _safe_claim(conn, agent.wash_partner_id, sim_today)

            if agent.wash_ticker is None:
                agent.wash_ticker = await _random_active_ticker(conn, rng)
            ticker = agent.wash_ticker
            if ticker is None:
                return
            partner_id = agent.wash_partner_id

            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    SELECT p.quantity FROM positions p
                    JOIN instruments i ON i.id = p.instrument_id
                    WHERE p.user_id = %s AND i.ticker = %s AND p.season_id IS NULL
                    """,
                    (partner_id, ticker),
                )
                row = await cur.fetchone()
            partner_holding = row[0] if row else 0

            if partner_holding < 5:
                # Seed the partner's position at a fair, unhurried moment.
                await execute_trade(
                    conn, user_id=partner_id, ticker=ticker, side="BUY", quantity=10
                )
                return

            # Pump (A buys) then dump into it (B sells), same tick -- the
            # exact pattern compliance/wash_trade.py is designed to catch.
            await execute_trade(conn, user_id=agent.user_id, ticker=ticker, side="BUY", quantity=20)
            await execute_trade(conn, user_id=partner_id, ticker=ticker, side="SELL", quantity=5)

        if agent.archetype == "liquidity_provider" and agent.quote_ticker:
            await _safe_claim(conn, agent.user_id, sim_today)
            ticker = agent.quote_ticker
            # Prune anything beyond ~2 days of accumulated quotes, then add
            # today's two-sided quote. Stale quotes deliberately rest on:
            # as the mark drifts they cross fresh quotes and fill vs the MM.
            open_orders = await list_open_orders(conn, agent.user_id)
            for open_order in open_orders[6:]:
                await cancel_order(
                    conn, user_id=agent.user_id, order_id=int(open_order["id"])
                )
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT quoted_price FROM instruments WHERE ticker = %s "
                    "AND circuit_halted_until_tick IS NULL",
                    (ticker,),
                )
                row = await cur.fetchone()
            if row is None:
                return
            mark = Decimal(str(row[0]))
            account_id = await get_user_account_id(conn, agent.user_id)
            balance = await get_balance(conn, account_id)
            offset = Decimal(str(round(rng.uniform(0.005, 0.015), 4)))
            bid_qty = max(1, int(balance / 100 * 0.15 / float(mark)))
            await place_order(
                conn,
                user_id=agent.user_id,
                ticker=ticker,
                side="BUY",
                quantity=bid_qty,
                limit_price=(mark * (1 - offset)).quantize(Decimal("0.000001")),
                expires_in_ticks=2 * TICKS_PER_SIMULATED_DAY,
            )
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    SELECT p.quantity FROM positions p
                    JOIN instruments i ON i.id = p.instrument_id
                    WHERE p.user_id = %s AND i.ticker = %s AND p.season_id IS NULL
                    """,
                    (agent.user_id, ticker),
                )
                pos_row = await cur.fetchone()
            held = int(pos_row[0]) if pos_row else 0
            if held > 0:
                await place_order(
                    conn,
                    user_id=agent.user_id,
                    ticker=ticker,
                    side="SELL",
                    quantity=min(held, bid_qty),
                    limit_price=(mark * (1 + offset)).quantize(Decimal("0.000001")),
                    expires_in_ticks=2 * TICKS_PER_SIMULATED_DAY,
                )
            return

        if agent.archetype == "stop_loss":
            # Holds one name with a protective SELL stop trailing ~4-9%
            # under the mark; re-enters a day or two after being stopped
            # out. Exercises the stop trigger/cascade path every run.
            await _safe_claim(conn, agent.user_id, sim_today)
            if agent.stop_ticker is None:
                agent.stop_ticker = await _random_active_ticker(conn, rng)
            ticker = agent.stop_ticker
            if ticker is None:
                return
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    SELECT p.quantity FROM positions p
                    JOIN instruments i ON i.id = p.instrument_id
                    WHERE p.user_id = %s AND i.ticker = %s AND p.season_id IS NULL
                    """,
                    (agent.user_id, ticker),
                )
                pos_row = await cur.fetchone()
                await cur.execute(
                    "SELECT quoted_price FROM instruments WHERE ticker = %s "
                    "AND circuit_halted_until_tick IS NULL",
                    (ticker,),
                )
                px_row = await cur.fetchone()
            if px_row is None:
                return  # halted or gone: leave the stop to rest
            mark = Decimal(str(px_row[0]))
            held = int(pos_row[0]) if pos_row else 0
            open_orders = await list_open_orders(conn, agent.user_id)
            has_stop = any(
                o["order_type"] != "LIMIT" and o["ticker"] == ticker
                for o in open_orders
            )
            if held <= 0:
                # Flat (fresh start or just stopped out): re-enter at market
                # most days, then re-arm the stop below.
                if rng.random() >= 0.6:
                    return
                account_id = await get_user_account_id(conn, agent.user_id)
                balance = await get_balance(conn, account_id)
                spend = int(balance * rng.uniform(0.4, 0.7))
                qty = await _quantity_for_spend(spend, int(mark * 100))
                if qty < 1:
                    return
                await execute_trade(
                    conn, user_id=agent.user_id, ticker=ticker, side="BUY",
                    quantity=qty,
                )
                held = qty
            if held > 0 and not has_stop:
                stop_pct = Decimal(str(round(rng.uniform(0.04, 0.09), 4)))
                await place_order(
                    conn,
                    user_id=agent.user_id,
                    ticker=ticker,
                    side="SELL",
                    quantity=held,
                    limit_price=None,
                    stop_price=(mark * (1 - stop_pct)).quantize(Decimal("0.000001")),
                    expires_in_ticks=3 * TICKS_PER_SIMULATED_DAY,
                )
            return

        if agent.archetype == "shorter":
            await _safe_claim(conn, agent.user_id, sim_today)
            if rng.random() >= 0.4:
                return
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    SELECT i.ticker, p.quantity FROM positions p
                    JOIN instruments i ON i.id = p.instrument_id
                    WHERE p.user_id = %s AND p.quantity < 0 AND p.season_id IS NULL
                    ORDER BY i.ticker
                    """,
                    (agent.user_id,),
                )
                shorts_held = await cur.fetchall()
            if shorts_held:
                # Cover an existing short with some probability.
                if rng.random() < 0.5:
                    ticker, qty = rng.choice(shorts_held)
                    await execute_trade(
                        conn,
                        user_id=agent.user_id,
                        ticker=str(ticker),
                        side="BUY",
                        quantity=int(-qty),
                    )
                return
            # Open a short sized to a fraction of equity.
            health = await compute_health(conn, agent.user_id, None)
            ticker = await _random_active_ticker(conn, rng)
            if ticker is None:
                return
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT quoted_price FROM instruments WHERE ticker = %s", (ticker,)
                )
                row = await cur.fetchone()
            price_minor = int(float(row[0]) * 100) if row else 100
            target_notional = int(health.equity_minor * rng.uniform(0.3, 0.8))
            quantity = target_notional // max(price_minor, 1)
            if quantity < 1:
                return
            await execute_trade(
                conn, user_id=agent.user_id, ticker=ticker, side="SELL", quantity=quantity
            )
    except (InsufficientFundsError, TradingError, MarginError):
        pass


async def net_worth_by_user(conn: AsyncConnection, user_ids: list[int]) -> dict[int, int]:
    """Cash + mark value of positions minus accrued borrow/dividend
    liabilities (at current quoted price), minor units."""
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT user_id, balance FROM accounts WHERE kind = 'USER' AND user_id = ANY(%s)",
            (user_ids,),
        )
        cash: dict[int, int] = dict(await cur.fetchall())

    async with conn.cursor() as cur:
        await cur.execute(
            """
            SELECT p.user_id, SUM(p.quantity * i.quoted_price * 100
                                  - p.borrow_fees_accrued
                                  - p.dividends_accrued)
            FROM positions p
            JOIN instruments i ON i.id = p.instrument_id
            WHERE p.user_id = ANY(%s) AND p.quantity <> 0 AND p.season_id IS NULL
            GROUP BY p.user_id
            """,
            (user_ids,),
        )
        holdings_minor: dict[int, float] = dict(await cur.fetchall())

    return {
        uid: cash.get(uid, 0) + int(holdings_minor.get(uid, 0)) for uid in user_ids
    }


async def economy_snapshot(conn: AsyncConnection) -> dict[str, float]:
    faucet_id = await get_system_account_id(conn, "FAUCET")
    sink_id = await get_system_account_id(conn, "SINK")
    faucet_balance = await get_balance(conn, faucet_id)
    sink_balance = await get_balance(conn, sink_id)
    # "Money supply" = printed minus destroyed = whatever USER, LEAGUE,
    # MARKET_MAKER and INSURANCE_FUND accounts currently hold. MM's balance
    # drifts with net trade flow (it's the counterparty of every fill), so
    # supply moves even when no faucet/sink activity happens -- intentional:
    # it's cash sitting in the system, not destroyed.
    money_supply = -(faucet_balance + sink_balance)
    insurance_id = await get_system_account_id(conn, "INSURANCE_FUND")
    insurance_balance = await get_balance(conn, insurance_id)
    async with conn.cursor() as cur:
        await cur.execute("SELECT COUNT(*) FROM liquidations")
        row = await cur.fetchone()
        liquidation_count = int(row[0]) if row else 0
        await cur.execute(
            """
            SELECT COUNT(*), COALESCE(SUM(-p.quantity * i.quoted_price), 0)
            FROM positions p
            JOIN instruments i ON i.id = p.instrument_id
            WHERE p.quantity < 0 AND p.season_id IS NULL
            """
        )
        short_row = await cur.fetchone()
        short_positions = int(short_row[0]) if short_row else 0
        short_notional = float(short_row[1]) if short_row else 0.0
        await cur.execute(
            "SELECT COALESCE(SUM(amount), 0) FROM ledger_entries "
            "WHERE reason = 'DIVIDEND' AND amount > 0"
        )
        div_row = await cur.fetchone()
        dividends_paid = int(div_row[0]) if div_row else 0
    return {
        "faucet_printed": -faucet_balance,
        "sink_destroyed": sink_balance,
        "money_supply": money_supply,
        "faucet_sink_ratio": faucet_sink_ratio(-faucet_balance, sink_balance),
        "insurance_fund": insurance_balance,
        "liquidations": liquidation_count,
        "open_shorts": short_positions,
        "short_notional_major": short_notional,
        "dividends_paid": dividends_paid,
    }


async def run_simulation(
    conn: AsyncConnection,
    *,
    num_users: int,
    num_days: int,
    master_seed: str,
    snapshot_every_days: int = 7,
    seed: int = 0,
    ticks_per_day: int = TICKS_PER_SIMULATED_DAY,
) -> dict[str, object]:
    """`ticks_per_day` defaults to a real day (1440); tests pass a much
    smaller number to exercise the harness without literally applying
    thousands of ticks.

    `conn` must either be in autocommit mode (what `_main_async` uses, so
    every service-level `conn.transaction()` commits like production) or be
    a deliberately wrapped test transaction. With a plain non-autocommit
    connection and no commits, every tick/trade nests as a savepoint inside
    one implicit transaction that never ends -- progressively slower, holding
    row locks, and invisible to every other connection.
    """
    rng = random.Random(seed)
    agents = build_cohort(num_users, rng)
    await initialize_agents(conn, agents)

    user_ids = [a.user_id for a in agents]
    archetype_by_user = {a.user_id: a.archetype for a in agents}

    starting_net_worth = await net_worth_by_user(conn, user_ids)
    timeline: list[dict[str, object]] = []

    for day in range(num_days):
        for _ in range(ticks_per_day):
            await apply_tick(conn, master_seed)
        sim_today = SIM_CLAIM_EPOCH + timedelta(days=day)
        for agent in agents:
            await _run_agent_day(conn, agent, rng, sim_today)

        if day % snapshot_every_days == 0 or day == num_days - 1:
            net_worth = await net_worth_by_user(conn, user_ids)
            econ = await economy_snapshot(conn)
            gini = gini_coefficient(list(net_worth.values()))
            timeline.append({"day": day, "gini": gini, **econ})
            log.info(
                "day %d: money_supply=%s gini=%.3f faucet_sink=%.3f "
                "liquidations=%d insurance=%s open_shorts=%d dividends=%s",
                day,
                econ["money_supply"],
                gini,
                econ["faucet_sink_ratio"],
                int(econ["liquidations"]),
                econ["insurance_fund"],
                int(econ["open_shorts"]),
                int(econ["dividends_paid"]),
            )

    final_net_worth = await net_worth_by_user(conn, user_ids)

    by_archetype: dict[str, dict[str, float]] = {}
    for archetype in {
        "grinder",
        "yolo",
        "farmer",
        "wash_trader",
        "whale",
        "shorter",
        "liquidity_provider",
        "stop_loss",
    }:
        members = [uid for uid in user_ids if archetype_by_user[uid] == archetype]
        if not members:
            continue
        gained = sum(final_net_worth[uid] - starting_net_worth[uid] for uid in members)
        by_archetype[archetype] = {
            "members": len(members),
            "total_gain_minor": gained,
            "avg_gain_minor": gained / len(members),
        }

    grinder_avg = by_archetype.get("grinder", {}).get("avg_gain_minor", 0.0)
    for stats in by_archetype.values():
        stats["outperformed_grinder"] = stats["avg_gain_minor"] > grinder_avg

    return {
        "num_users": num_users,
        "num_days": num_days,
        "timeline": timeline,
        "final_gini": gini_coefficient(list(final_net_worth.values())),
        "by_archetype": by_archetype,
    }


async def _main_async(args: argparse.Namespace) -> None:
    await db.init_pool(args.database_url, min_size=1, max_size=5)
    try:
        async with db.connection() as conn:
            # Autocommit so each service-level `conn.transaction()` is a real
            # commit like production. Without it, the first bare SELECT opens
            # an implicit transaction and the entire run becomes one giant
            # never-committed transaction of nested savepoints.
            await conn.set_autocommit(True)
            report = await run_simulation(
                conn,
                num_users=args.users,
                num_days=args.days,
                master_seed=args.master_seed,
                snapshot_every_days=args.snapshot_every_days,
                seed=args.seed,
                ticks_per_day=args.ticks_per_day,
            )
    finally:
        await db.close_pool()

    output = json.dumps(report, indent=2, default=str)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(output)
        log.info("wrote report to %s", args.output)
    else:
        print(output)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--database-url", required=True, help="Postgres connection string. Use a scratch DB."
    )
    parser.add_argument("--users", type=int, default=100)
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--master-seed", default="simulation")
    parser.add_argument("--seed", type=int, default=0, help="Python RNG seed for agent behavior")
    parser.add_argument("--snapshot-every-days", type=int, default=7)
    parser.add_argument(
        "--ticks-per-day",
        type=int,
        default=TICKS_PER_SIMULATED_DAY,
        help="Market ticks per simulated day; lower values trade realism for speed",
    )
    parser.add_argument(
        "--output", default=None, help="Write the JSON report here instead of stdout"
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING)

    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(_main_async(args))
    return 0


if __name__ == "__main__":
    sys.exit(main())
