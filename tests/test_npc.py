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
            "UPDATE users SET total_traded_minor = 10, quests_completed = 1 "
            "WHERE id = 3003"
        )
    await evaluate_badges(conn, tick_index=1440)
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT COUNT(*) FROM entitlements WHERE user_id = 3003"
        )
        assert (await cur.fetchone())[0] == 0
    # A human gets all three at these thresholds (proof the sweep ran).
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT COUNT(*) FROM entitlements "
            "WHERE item_key LIKE 'test_%_badge'"
        )
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
        conn, name="No Bots", start_tick=0, end_tick=10_000,
        entry_fee_minor=0, stake_minor=100_000,
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
        await cur.execute(
            "SELECT equity_minor FROM net_worth_snapshots WHERE user_id = 3009"
        )
        row = await cur.fetchone()
    assert row is not None and int(row[0]) > 0
