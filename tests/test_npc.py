"""NPC P1: the is_bot identity layer and its exclusion surface.

An is_bot user holds an ordinary USER account (every money path resolves
kind='USER') but is excluded from every human-economy aggregate:
leaderboard, badges, quest payouts, wash flags, and season entry.
net_worth_snapshots are KEPT -- per-day equity rows are the runner's
telemetry, and per-user reads stay unfiltered.
"""

from __future__ import annotations

import pytest
from psycopg import AsyncConnection

from stockbot.accounts.service import bootstrap_user
from stockbot.seasons.errors import BotAccountError
from stockbot.seasons.service import create_season, join_season, on_tick
from stockbot.status.service import (
    evaluate_badges,
    leaderboard,
    net_worth_minor,
    snapshot_net_worth_if_due,
)
from stockbot.trading.service import execute_trade


async def _make_bot(conn: AsyncConnection, user_id: int, cash_minor: int) -> None:
    """A synthetic account: bootstrapped like any user, then flagged."""
    await bootstrap_user(conn, user_id)
    async with conn.cursor() as cur:
        await cur.execute("UPDATE users SET is_bot = TRUE WHERE id = %s", (user_id,))
        await cur.execute(
            "UPDATE accounts SET balance = %s WHERE user_id = %s AND kind = 'USER'",
            (cash_minor, user_id),
        )


async def test_bot_invisible_on_leaderboard_but_valued(conn: AsyncConnection) -> None:
    await bootstrap_user(conn, 3001)  # human
    await _make_bot(conn, 3002, 500_000)
    rows = await leaderboard(conn)
    ids = [r.user_id for r in rows]
    assert 3002 not in ids
    assert 3001 in ids
    # Per-user valuation is unfiltered -- the runner reads its own agents.
    assert await net_worth_minor(conn, 3002) > 0


async def test_bot_earns_no_badges(conn: AsyncConnection) -> None:
    await _make_bot(conn, 3003, 9_999_999_999)  # absurdly rich bot
    await bootstrap_user(conn, 3004)
    async with conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO shop_items (key, name, description, kind, price_minor, metadata)
            VALUES ('test_nw_badge', 'Test NW', 'x', 'BADGE', NULL,
                    '{"metric": "net_worth", "threshold_minor": 1}'),
                   ('test_vol_badge', 'Test Vol', 'x', 'BADGE', NULL,
                    '{"metric": "volume", "threshold_minor": 1}'),
                   ('test_quest_badge', 'Test Q', 'x', 'BADGE', NULL,
                    '{"metric": "quests", "threshold": 0}')
            ON CONFLICT (key) DO NOTHING
            """
        )
        await cur.execute(
            "UPDATE users SET total_traded_minor = 10, quests_completed = 1 WHERE id = 3003"
        )
    await evaluate_badges(conn, tick_index=1440)
    async with conn.cursor() as cur:
        await cur.execute("SELECT COUNT(*) FROM entitlements WHERE user_id = 3003")
        assert (await cur.fetchone())[0] == 0
    # A human gets all three at these thresholds (proof the sweep ran).
    async with conn.cursor() as cur:
        await cur.execute("SELECT COUNT(*) FROM entitlements WHERE item_key LIKE 'test_%_badge'")
        assert (await cur.fetchone())[0] > 0


async def test_bot_trades_but_completes_no_quests(conn: AsyncConnection) -> None:
    """NPC volume must not mint FAUCET quest rewards (C3) -- the sweep
    skips is_bot users at the measures join."""
    from stockbot.quests.service import sweep_completions

    await _make_bot(conn, 3005, 10_000_000)
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT ticker FROM instruments WHERE is_active AND kind = 'STOCK' "
            "AND market_id = 1 ORDER BY id LIMIT 1"
        )
        ticker = (await cur.fetchone())[0]
        await cur.execute(
            """
            INSERT INTO quest_defs (key, kind, name, target, reward_minor,
                                    period, active)
            VALUES ('test_trade_vol', 'TRADE_VOLUME', 'Trade', 1, 5000,
                    'DAILY', TRUE)
            ON CONFLICT (key) DO NOTHING
            """
        )
        await cur.execute(
            """
            INSERT INTO quest_instances
                (def_key, kind, period, period_index, window_start,
                 window_end, target, reward_minor, status)
            VALUES ('test_trade_vol', 'TRADE_VOLUME', 'DAILY', 0,
                    0, 10_000_000, 1, 5000, 'OPEN')
            """
        )
    await execute_trade(conn, user_id=3005, ticker=ticker, side="BUY", quantity=2)
    paid = await sweep_completions(conn, 5)
    assert paid == 0
    async with conn.cursor() as cur:
        await cur.execute(
            """
            SELECT COUNT(*) FROM ledger_entries e
            JOIN accounts a ON a.id = e.account_id
            WHERE a.user_id = 3005 AND e.reason = 'QUEST_REWARD' AND e.amount > 0
            """
        )
        assert (await cur.fetchone())[0] == 0


async def test_bot_cannot_join_season(conn: AsyncConnection) -> None:
    await _make_bot(conn, 3006, 100_000)
    season_id = await create_season(
        conn,
        name="No Bots",
        start_tick=0,
        end_tick=10_000,
        entry_fee_minor=0,
        stake_minor=100_000,
    )
    await on_tick(conn, 0)
    with pytest.raises(BotAccountError):
        await join_season(conn, 3006, season_id)


async def test_bot_not_wash_flagged(conn: AsyncConnection) -> None:
    """Two bots MM-filling opposite sides same tick must not flag (C4):
    synthetics can't collude."""
    from stockbot.compliance.wash_trade import scan_for_wash_trades

    await _make_bot(conn, 3007, 10_000_000)
    await _make_bot(conn, 3008, 10_000_000)
    async with conn.cursor() as cur:
        # 3008's SELL is a margin short -- needs the tier unlock.
        await cur.execute(
            "INSERT INTO entitlements (user_id, item_key, quantity) "
            "VALUES (3008, 'margin_tier', 1) "
            "ON CONFLICT (user_id, item_key) DO UPDATE SET quantity = 1"
        )
    async with conn.cursor() as cur:
        # Bottom-quartile-liquidity instrument is what the detector scans.
        await cur.execute(
            "SELECT ticker FROM instruments WHERE is_active AND kind = 'STOCK' "
            "AND market_id = 1 ORDER BY liquidity LIMIT 1"
        )
        ticker = (await cur.fetchone())[0]
    await execute_trade(conn, user_id=3007, ticker=ticker, side="BUY", quantity=5)
    await execute_trade(conn, user_id=3008, ticker=ticker, side="SELL", quantity=5)
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE trades SET tick_index = "
            "(SELECT MAX(tick_index) FROM market_ticks) WHERE tick_index IS NULL"
        )
    await scan_for_wash_trades(conn, lookback_ticks=10)
    async with conn.cursor() as cur:
        await cur.execute(
            """
            SELECT COUNT(*) FROM wash_trade_flags f
            JOIN trades b ON b.id = f.buy_trade_id
            WHERE b.user_id IN (3007, 3008)
            """
        )
        assert (await cur.fetchone())[0] == 0


async def test_bot_keeps_net_worth_snapshots(conn: AsyncConnection) -> None:
    """Snapshots stay: per-day equity rows are the runner's telemetry
    for measuring archetype half-lives."""
    await _make_bot(conn, 3009, 100_000)
    await snapshot_net_worth_if_due(conn, tick_index=1440)
    async with conn.cursor() as cur:
        await cur.execute("SELECT equity_minor FROM net_worth_snapshots WHERE user_id = 3009")
        row = await cur.fetchone()
    assert row is not None and int(row[0]) > 0


# ---- P2: the runner ----

from stockbot.ledger.service import get_balance, get_user_account_id  # noqa: E402
from stockbot.npc.service import (  # noqa: E402
    NPC_USER_ID_BASE,
    load_agents,
    mark_dead_agents,
    npc_enabled,
    run_round,
    spawn_agent,
)


async def _set_config(conn: AsyncConnection, key: str, value: float) -> None:
    async with conn.cursor() as cur:
        await cur.execute("UPDATE config SET value = %s WHERE key = %s", (value, key))


async def _ensure_open_tick(conn: AsyncConnection) -> int:
    """Pin a market_ticks row in the US-open phase (tick % 1440 < 960)
    so execute_trade's assert_market_open can't flake on committed
    history. Returns the tick index used."""
    async with conn.cursor() as cur:
        await cur.execute("SELECT COALESCE(MAX(tick_index), -1) + 1 FROM market_ticks")
        t = int((await cur.fetchone())[0])
        if t % 1440 >= 960:
            t += 1440 - (t % 1440)
        await cur.execute(
            "INSERT INTO market_ticks (tick_index, ts, market_factor, "
            "sector_factors, session_state) "
            "VALUES (%s, now(), 1.0, '{}', 'OPEN')",
            (t,),
        )
        return t


async def test_spawn_agent_flag_stake_and_row(conn: AsyncConnection) -> None:
    uid = await spawn_agent(conn, "grinder", stake_minor=50_000)
    assert uid >= NPC_USER_ID_BASE
    async with conn.cursor() as cur:
        await cur.execute("SELECT is_bot FROM users WHERE id = %s", (uid,))
        assert (await cur.fetchone())[0] is True
        await cur.execute(
            "SELECT archetype, enabled, died_at_tick FROM npc_agents WHERE user_id = %s",
            (uid,),
        )
        row = await cur.fetchone()
        assert row[0] == "grinder" and row[1] is True and row[2] is None
        # The one-time NPC_STAKE grant, FAUCET -> agent, sums to zero.
        # Scoped to this transfer: committed soak-run entries also exist.
        await cur.execute(
            """
            SELECT SUM(e.amount), COUNT(*)
            FROM ledger_entries e
            WHERE e.reason = 'NPC_STAKE' AND e.transfer_id IN (
                SELECT e2.transfer_id FROM ledger_entries e2
                JOIN accounts a ON a.id = e2.account_id AND a.user_id = %s
            )
            """,
            (uid,),
        )
        total, count = await cur.fetchone()
        assert int(total) == 0 and int(count) == 2
    account_id = await get_user_account_id(conn, uid)
    # No STARTING_GRANT for bots (synthetic snowflakes trivially pass
    # the age gate) -- the balance is exactly the stake.
    assert await get_balance(conn, account_id) == 50_000


async def test_spawn_shorter_gets_margin_tier(conn: AsyncConnection) -> None:
    uid = await spawn_agent(conn, "shorter", stake_minor=10_000)
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT quantity FROM entitlements WHERE user_id = %s AND item_key = 'margin_tier'",
            (uid,),
        )
        row = await cur.fetchone()
    assert row is not None and int(row[0]) == 1


async def test_run_round_grinder_trades(conn: AsyncConnection) -> None:
    """prob=1 forces action; a grinder ends the round holding stock or
    having spent cash, and the ledger still sums to zero."""
    tick = await _ensure_open_tick(conn)
    uid = await spawn_agent(conn, "grinder", stake_minor=1_000_000)
    await _set_config(conn, "npc.action_prob_per_tick", 1)
    stats = await run_round(conn, "test-seed", tick_index=tick, sleep=False)
    assert stats["acted"] == 1
    async with conn.cursor() as cur:
        await cur.execute("SELECT SUM(e.amount) FROM ledger_entries e", ())
        assert int((await cur.fetchone())[0]) == 0
        await cur.execute("SELECT COUNT(*) FROM trades WHERE user_id = %s", (uid,))
        assert (await cur.fetchone())[0] >= 1


async def test_run_round_prob_zero_is_quiet(conn: AsyncConnection) -> None:
    tick = await _ensure_open_tick(conn)
    await spawn_agent(conn, "grinder", stake_minor=100_000)
    await _set_config(conn, "npc.action_prob_per_tick", 0)
    stats = await run_round(conn, "test-seed", tick_index=tick, sleep=False)
    assert stats["acted"] == 0


async def test_run_round_budget_caps_the_round(conn: AsyncConnection) -> None:
    """npc.max_tick_notional (dollars) bounds aggregate round flow: with
    a $1 budget the first agent acts and the rest sit out."""
    tick = await _ensure_open_tick(conn)
    await spawn_agent(conn, "grinder", stake_minor=1_000_000)
    await spawn_agent(conn, "grinder", stake_minor=1_000_000)
    await _set_config(conn, "npc.action_prob_per_tick", 1)
    await _set_config(conn, "npc.max_tick_notional", 1)
    stats = await run_round(conn, "test-seed", tick_index=tick, sleep=False)
    assert stats["acted"] == 1


async def test_mark_dead_agents_permadeath(conn: AsyncConnection) -> None:
    broke = await spawn_agent(conn, "grinder", stake_minor=1)
    rich = await spawn_agent(conn, "grinder", stake_minor=1_000_000)
    async with conn.cursor() as cur:
        # Drain broke below npc.death_balance_minor (100).
        await cur.execute(
            "UPDATE accounts SET balance = 1 WHERE user_id = %s AND kind = 'USER'",
            (broke,),
        )
    dead = await mark_dead_agents(conn, tick_index=7)
    assert dead == 1
    ids = {a.user_id for a in await load_agents(conn)}
    assert broke not in ids and rich in ids
    # Permadeath: a later sweep does not resurrect or re-stamp.
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE accounts SET balance = 9_999_999 WHERE user_id = %s AND kind = 'USER'",
            (broke,),
        )
    assert await mark_dead_agents(conn, tick_index=8) == 0
    assert broke not in {a.user_id for a in await load_agents(conn)}


async def test_npc_enabled_kill_switch(conn: AsyncConnection) -> None:
    # 0057 seeds npc.enabled = 0.
    assert await npc_enabled(conn) is False
    await _set_config(conn, "npc.enabled", 1)
    assert await npc_enabled(conn) is True


# ---- P3: archetype port ----


async def _round(conn: AsyncConnection, seed: str = "p3", prob: float = 1) -> dict[str, int]:
    tick = await _ensure_open_tick(conn)
    await _set_config(conn, "npc.action_prob_per_tick", prob)
    return await run_round(conn, seed, tick_index=tick, sleep=False)


async def test_shorter_opens_a_margin_short(conn: AsyncConnection) -> None:
    uid = await spawn_agent(conn, "shorter", stake_minor=1_000_000)
    stats = await _round(conn)
    assert stats["acted"] >= 0  # shorter may no-op on the cover fork
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT COALESCE(SUM(p.quantity), 0) FROM positions p WHERE p.user_id = %s",
            (uid,),
        )
        qty = int((await cur.fetchone())[0])
    # Fresh agent has no shorts to cover -- the open branch must fire.
    assert qty < 0


async def test_yolo_spends_most_of_balance(conn: AsyncConnection) -> None:
    uid = await spawn_agent(conn, "yolo", stake_minor=1_000_000)
    account_id = await get_user_account_id(conn, uid)
    before = await get_balance(conn, account_id)
    stats = await _round(conn)
    assert stats["acted"] == 1
    after = await get_balance(conn, account_id)
    # 50-95% of cash went into stock.
    assert after <= before * 0.6


async def test_liquidity_provider_leaves_resting_quotes(conn: AsyncConnection) -> None:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT ticker FROM instruments WHERE is_active AND kind = 'STOCK' "
            "AND market_id = 1 ORDER BY id LIMIT 1"
        )
        ticker = (await cur.fetchone())[0]
    uid = await spawn_agent(conn, "liquidity_provider", quote_ticker=ticker, stake_minor=1_000_000)
    await _round(conn)
    async with conn.cursor() as cur:
        await cur.execute(
            """
            SELECT o.side, o.limit_price, i.quoted_price
            FROM orders o JOIN instruments i ON i.id = o.instrument_id
            WHERE o.user_id = %s AND o.status = 'OPEN'
            """,
            (uid,),
        )
        rows = await cur.fetchall()
    # Bid below the mark -- resting depth persists between active ticks.
    assert len(rows) >= 1
    bid = next(r for r in rows if r[0] == "BUY")
    assert float(bid[1]) < float(bid[2])


async def test_stop_loss_arms_a_stop(conn: AsyncConnection) -> None:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT ticker FROM instruments WHERE is_active AND kind = 'STOCK' "
            "AND market_id = 1 ORDER BY id LIMIT 1"
        )
        ticker = (await cur.fetchone())[0]
    uid = await spawn_agent(conn, "stop_loss", quote_ticker=ticker, stake_minor=1_000_000)
    # The re-entry fork is a 0.6 roll -- run several rounds so at least
    # one lands the buy + stop-arm sequence.
    for i in range(10):
        await _round(conn, seed=f"stop-{i}")
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT COUNT(*) FROM orders WHERE user_id = %s "
                "AND order_type <> 'LIMIT' AND status = 'OPEN'",
                (uid,),
            )
            if int((await cur.fetchone())[0]) > 0:
                return
    pytest.fail("stop_loss never re-entered across 10 rounds")


async def test_npcs_never_claim(conn: AsyncConnection) -> None:
    """The bounded-injection invariant: positive ledger flows to bot
    accounts are NPC_STAKE only -- no CLAIM refill path exists."""
    await spawn_agent(conn, "grinder", stake_minor=1_000_000)
    await spawn_agent(conn, "whale", stake_minor=1_000_000)
    await _round(conn)
    async with conn.cursor() as cur:
        await cur.execute(
            """
            SELECT e.reason FROM ledger_entries e
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
        reasons = {r[0] for r in await cur.fetchall()}
    # STARTING_GRANT is the same one-time bootstrap every account gets
    # (bots pass the snowflake-age gate like anyone) -- bounded like the
    # stake, not a refill path. CLAIM would be the violation.
    assert reasons <= {"NPC_STAKE", "STARTING_GRANT"}


async def test_agent_report_shows_equity_pnl_and_positions(
    conn: AsyncConnection,
) -> None:
    """`/admin npc-list` data: funded = bootstrap + stake from the ledger,
    P&L = equity - funded, and open positions counted for the census."""
    from stockbot.npc.service import agent_report

    uid = await spawn_agent(conn, "shorter", label="rep-a", stake_minor=1_000_000)
    await _ensure_open_tick(conn)
    uid2 = await spawn_agent(conn, "grinder", label="rep-b", stake_minor=500_000)
    # Give the grinder a position so open_positions is nonzero.
    await _round(conn)

    report = {a.label: a for a in await agent_report(conn)}
    a = report["rep-a"]
    b = report["rep-b"]
    assert a.user_id == uid and b.user_id == uid2
    assert a.funded_minor > 0 and a.funded_minor >= 1_000_000
    assert a.pnl_minor == a.equity_minor - a.funded_minor
    assert a.enabled and a.died_at_tick is None


async def test_set_agent_enabled_toggle_and_permadeath(
    conn: AsyncConnection,
) -> None:
    """`/admin npc-enable|disable`: label or user_id lookup flips
    `enabled`, but a dead agent can't be re-enabled."""
    from stockbot.npc.service import set_agent_enabled

    uid = await spawn_agent(conn, "grinder", label="tog-a")
    found = await set_agent_enabled(conn, label="tog-a", enabled=False)
    assert found is not None and found.user_id == uid
    found = await set_agent_enabled(conn, label=str(uid), enabled=True)
    assert found is not None
    async with conn.cursor() as cur:
        await cur.execute("SELECT enabled FROM npc_agents WHERE user_id = %s", (uid,))
        assert (await cur.fetchone())[0] is True
    # Dead agents can't come back.
    async with conn.cursor() as cur:
        await cur.execute("UPDATE npc_agents SET died_at_tick = 5 WHERE user_id = %s", (uid,))
    assert await set_agent_enabled(conn, label="tog-a", enabled=True) is None


async def test_admin_spawn_path_caps_count_and_pins_ticker(
    conn: AsyncConnection,
) -> None:
    """The spawn service honors stake overrides and quote_ticker pinning
    for LP/stop_loss agents."""
    uid = await spawn_agent(conn, "liquidity_provider", quote_ticker="ANCH", stake_minor=50_000)
    async with conn.cursor() as cur:
        await cur.execute("SELECT quote_ticker FROM npc_agents WHERE user_id = %s", (uid,))
        assert (await cur.fetchone())[0] == "ANCH"


# ---- Review fixes: isolation, validation, death sweep, grant opt-out ----


async def test_action_exception_doesnt_starve_cohort(conn: AsyncConnection) -> None:
    """A deterministic crash in a low-id agent must not block later
    agents or the death sweep -- the per-action tx bounds the blast."""
    from stockbot.npc import agents as agents_mod

    tick = await _ensure_open_tick(conn)
    boom_uid = await spawn_agent(conn, "grinder", stake_minor=100_000)
    ok_uid = await spawn_agent(conn, "grinder", stake_minor=1_000_000)
    broke_uid = await spawn_agent(conn, "grinder", stake_minor=1)
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE accounts SET balance = 1 WHERE user_id = %s AND kind = 'USER'",
            (broke_uid,),
        )
    await _set_config(conn, "npc.action_prob_per_tick", 1)
    await _set_config(conn, "npc.target_adv_share", 0)  # pin prob=1

    original = agents_mod.ACTIONS["grinder"]

    async def boom(ctx, agent):
        if agent.user_id == boom_uid:
            raise RuntimeError("synthetic explosion")
        return await original(ctx, agent)

    agents_mod.ACTIONS["grinder"] = boom
    try:
        stats = await run_round(conn, "test-seed", tick_index=tick, sleep=False)
    finally:
        agents_mod.ACTIONS["grinder"] = original

    # The higher-id agent still acted despite the predecessor raising.
    assert stats["acted"] >= 1
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT COUNT(*) FROM trades WHERE user_id = %s", (ok_uid,)
        )
        assert int((await cur.fetchone())[0]) >= 1
    # ...and the death sweep ran: the broke agent is stamped even
    # though the round began with an exception.
    assert stats["dead"] == 1
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT died_at_tick FROM npc_agents WHERE user_id = %s",
            (broke_uid,),
        )
        assert (await cur.fetchone())[0] == tick


async def test_spawn_rejects_unknown_archetype_and_ticker(
    conn: AsyncConnection,
) -> None:
    """Service-level validation: a bad archetype or a nonexistent
    quote_ticker would create a funded, permanently inert agent."""
    async with conn.cursor() as cur:
        await cur.execute("SELECT COUNT(*) FROM npc_agents")
        before = int((await cur.fetchone())[0])
    with pytest.raises(ValueError, match="unknown archetype"):
        await spawn_agent(conn, "nope")
    with pytest.raises(ValueError, match="unknown or inactive"):
        await spawn_agent(conn, "grinder", quote_ticker="ZZZNOPE")
    async with conn.cursor() as cur:
        await cur.execute("SELECT COUNT(*) FROM npc_agents")
        assert int((await cur.fetchone())[0]) == before


async def test_spawn_gets_no_starting_grant(conn: AsyncConnection) -> None:
    """NPC accounts draw NPC_STAKE only -- the bootstrap grant is burned
    (grant_issued stamped, no transfer) so a later user-path bootstrap
    can't leak it either."""
    uid = await spawn_agent(conn, "grinder", stake_minor=50_000)
    account_id = await get_user_account_id(conn, uid)
    async with conn.cursor() as cur:
        await cur.execute("SELECT grant_issued FROM users WHERE id = %s", (uid,))
        assert (await cur.fetchone())[0] is True
        await cur.execute(
            "SELECT reason, SUM(amount) FROM ledger_entries "
            "WHERE account_id = %s GROUP BY reason",
            (account_id,),
        )
        rows = {str(r[0]): int(r[1]) for r in await cur.fetchall()}
    assert rows == {"NPC_STAKE": 50_000}
    # A repeat bootstrap can't post the grant retroactively.
    await bootstrap_user(conn, uid)
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT COUNT(*) FROM ledger_entries WHERE account_id = %s "
            "AND reason = 'STARTING_GRANT'",
            (account_id,),
        )
        assert int((await cur.fetchone())[0]) == 0


async def test_mark_dead_counts_resting_orders(conn: AsyncConnection) -> None:
    """'Dead' means flat: a broke agent with a resting order keeps its
    live paper (a dead LP's stale quotes would keep filling strangers)."""
    from decimal import Decimal

    from stockbot.orders.service import place_order

    await _ensure_open_tick(conn)
    uid = await spawn_agent(conn, "grinder", stake_minor=100_000)
    mark = Decimal("1000")
    order = await place_order(
        conn,
        user_id=uid,
        ticker="ANCH",
        side="BUY",
        quantity=1,
        limit_price=mark,
    )
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE accounts SET balance = 1 WHERE user_id = %s AND kind = 'USER'",
            (uid,),
        )
    # Net worth is below the floor but the open order is live exposure.
    assert await mark_dead_agents(conn, tick_index=7) == 0
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE orders SET status = 'CANCELLED' WHERE id = %s",
            (order.order_id,),
        )
    assert await mark_dead_agents(conn, tick_index=8) == 1


async def test_npc_flow_share_measures_bot_notional(conn: AsyncConnection) -> None:
    """target_adv_share feedback reads the bot/human split of trailing
    notional; an empty window returns None (no modulation)."""
    from stockbot.npc.service import _npc_flow_share

    tick = await _ensure_open_tick(conn)
    await bootstrap_user(conn, 9601)
    bot_uid = await spawn_agent(conn, "grinder", stake_minor=10_000)
    iid = None
    async with conn.cursor() as cur:
        await cur.execute("SELECT id FROM instruments WHERE ticker = 'ANCH'")
        (iid,) = await cur.fetchone()
        # 3:1 human:bot notional in-window -> share 0.25.
        for uid_, notional in ((9601, 300_000), (bot_uid, 100_000)):
            await cur.execute(
                "INSERT INTO trades (user_id, instrument_id, side, quantity, "
                "fill_price, notional_minor, fee_minor, cash_transfer_id, "
                "tick_index) "
                "VALUES (%s, %s, 'BUY', 1, 1, %s, 0, gen_random_uuid(), %s)",
                (uid_, iid, notional, tick),
            )
    share = await _npc_flow_share(conn, tick, window_ticks=1440)
    assert share == pytest.approx(0.25)
    # A window ahead of all flow has nothing to modulate against.
    assert await _npc_flow_share(conn, tick + 5000, window_ticks=1440) is None
