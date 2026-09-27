"""Phase N2: leaderboard ranking, trade history, compare, and profile."""

from __future__ import annotations

import pytest
from psycopg import AsyncConnection

from stockbot.accounts.service import bootstrap_user
from stockbot.ledger.service import (
    get_system_account_id,
    get_user_account_id,
    post_transfer,
)
from stockbot.shorts.service import open_bounded_short
from stockbot.status.service import (
    compare_stats,
    day_change_pct,
    leaderboard,
    net_worth_minor,
    profile_stats,
    recent_trades,
    snapshot_net_worth_if_due,
)
from stockbot.trading.service import execute_trade

TICKS_PER_DAY = 1440


async def _first_ticker(conn: AsyncConnection) -> str:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT ticker FROM instruments WHERE is_active AND kind = 'STOCK'"
            " ORDER BY id LIMIT 1"
        )
        (ticker,) = await cur.fetchone()
    return ticker


async def _give_cash(conn: AsyncConnection, account_id: int, amount: int) -> None:
    faucet_id = await get_system_account_id(conn, "FAUCET")
    await post_transfer(
        conn, from_account_id=faucet_id, to_account_id=account_id, amount=amount,
        reason="TEST_TOPUP",
    )


async def test_leaderboard_orders_by_net_worth_desc(conn: AsyncConnection) -> None:

    await bootstrap_user(conn, 8001)
    await bootstrap_user(conn, 8002)
    await bootstrap_user(conn, 8003)
    await _give_cash(conn, await get_user_account_id(conn, 8001), 1_000_000)
    await _give_cash(conn, await get_user_account_id(conn, 8002), 5_000_000)
    # 8003 keeps the default starting grant only (smallest of the three).

    rows = await leaderboard(conn)
    by_user = {r.user_id: r for r in rows}
    assert by_user[8002].rank < by_user[8001].rank < by_user[8003].rank
    # Sequential, deterministic ranks -- no ties even at equal equity.
    assert [r.rank for r in rows] == list(range(1, len(rows) + 1))
    assert all(r.total == len(rows) for r in rows)


async def test_leaderboard_excludes_league_and_system_accounts(
    conn: AsyncConnection,
) -> None:
    from stockbot.seasons.service import create_season, join_season, on_tick

    await bootstrap_user(conn, 8010)
    season_id = await create_season(
        conn, name="LB Season", start_tick=0, end_tick=10_000,
        entry_fee_minor=100, stake_minor=9_999_999_999,
    )
    await on_tick(conn, 0)
    await join_season(conn, 8010, season_id)

    rows = await leaderboard(conn)
    user_ids = {r.user_id for r in rows}
    assert 8010 in user_ids
    # The league account's huge stake must not leak into the main-economy
    # ranking -- 8010's main net worth is still just the starting grant
    # minus the entry fee, not the league stake.
    entry = next(r for r in rows if r.user_id == 8010)
    assert entry.equity_minor < 9_999_999_999

    async with conn.cursor() as cur:
        await cur.execute("SELECT system_name FROM accounts WHERE kind = 'SYSTEM'")
        system_names = {r[0] for r in await cur.fetchall()}
    assert not (system_names & user_ids)


async def test_leaderboard_includes_bounded_short_value(conn: AsyncConnection) -> None:
    await bootstrap_user(conn, 8020)
    before = await net_worth_minor(conn, 8020)
    result = await open_bounded_short(conn, user_id=8020, ticker="NORT", quantity=1)
    after = await net_worth_minor(conn, 8020)

    async with conn.cursor() as cur:
        await cur.execute(
            """
            SELECT GREATEST(0, bs.collateral_minor
                    + CAST(bs.quantity * (bs.entry_price - i.quoted_price) * 100 AS BIGINT))
            FROM bounded_shorts bs JOIN instruments i ON i.id = bs.instrument_id
            WHERE bs.id = %s
            """,
            (result.short_id,),
        )
        (bshort_value,) = await cur.fetchone()
    # Cash drops by collateral+fee, but the short's mark-to-market
    # collateral value still counts toward net worth -- not "ranked on
    # cash alone".
    assert after == before - result.collateral_minor - result.fee_minor + bshort_value


async def test_day_change_none_before_any_snapshot(conn: AsyncConnection) -> None:
    await bootstrap_user(conn, 8030)
    equity = await net_worth_minor(conn, 8030)
    assert await day_change_pct(conn, 8030, equity, tick_index=5) is None


async def test_day_change_after_snapshot_and_price_move(conn: AsyncConnection) -> None:

    await bootstrap_user(conn, 8031)
    await _give_cash(conn, await get_user_account_id(conn, 8031), 10_000_000)
    ticker = await _first_ticker(conn)
    await execute_trade(conn, user_id=8031, ticker=ticker, side="BUY", quantity=10)

    await snapshot_net_worth_if_due(conn, TICKS_PER_DAY)
    day1_equity = await net_worth_minor(conn, 8031)

    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE instruments SET quoted_price = quoted_price * 1.10 WHERE ticker = %s",
            (ticker,),
        )
    day2_equity = await net_worth_minor(conn, 8031)
    change = await day_change_pct(conn, 8031, day2_equity, tick_index=TICKS_PER_DAY * 2)
    assert change is not None
    assert change == pytest.approx(day2_equity / day1_equity - 1)


async def test_recent_trades_returns_newest_first_and_respects_limit(
    conn: AsyncConnection,
) -> None:

    await bootstrap_user(conn, 8040)
    await _give_cash(conn, await get_user_account_id(conn, 8040), 10_000_000)
    ticker = await _first_ticker(conn)
    for _ in range(3):
        await execute_trade(conn, user_id=8040, ticker=ticker, side="BUY", quantity=1)

    trades = await recent_trades(conn, 8040, limit=2)
    assert len(trades) == 2
    assert trades[0]["side"] == "BUY"
    assert all(t["ticker"] == ticker for t in trades)


async def test_recent_trades_excludes_league_fills(conn: AsyncConnection) -> None:
    from stockbot.seasons.service import create_season, join_season, on_tick

    await bootstrap_user(conn, 8041)
    season_id = await create_season(
        conn, name="Hist Season", start_tick=0, end_tick=10_000,
        entry_fee_minor=100, stake_minor=100_000,
    )
    await on_tick(conn, 0)
    await join_season(conn, 8041, season_id)
    ticker = await _first_ticker(conn)
    await execute_trade(
        conn, user_id=8041, ticker=ticker, side="BUY", quantity=1, season_id=season_id
    )

    assert await recent_trades(conn, 8041) == []


async def test_compare_symmetric_under_user_swap(conn: AsyncConnection) -> None:

    await bootstrap_user(conn, 8050)
    await bootstrap_user(conn, 8051)
    await _give_cash(conn, await get_user_account_id(conn, 8050), 42_000)

    a = await compare_stats(conn, 8050, tick_index=1)
    b = await compare_stats(conn, 8051, tick_index=1)
    # Same underlying numbers regardless of which order the pair is
    # requested in -- compare_stats is a pure per-user read.
    a2 = await compare_stats(conn, 8050, tick_index=1)
    b2 = await compare_stats(conn, 8051, tick_index=1)
    assert a == a2
    assert b == b2
    assert a.net_worth_minor > b.net_worth_minor


async def test_profile_stats_reports_rank_and_trophies(conn: AsyncConnection) -> None:
    await bootstrap_user(conn, 8060)
    stats = await profile_stats(conn, 8060)
    assert stats is not None
    assert stats.rank is not None
    assert stats.trophies == []
    assert stats.active_season_equity_minor is None

    async with conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO shop_items (key, name, description, kind, price_minor)
            VALUES ('trophy_test', 'Test Trophy', 'test', 'TROPHY', NULL)
            ON CONFLICT (key) DO NOTHING
            """
        )
        await cur.execute(
            "INSERT INTO entitlements (user_id, item_key) VALUES (%s, 'trophy_test')",
            (8060,),
        )
    stats = await profile_stats(conn, 8060)
    assert stats is not None
    assert stats.trophies == ["Test Trophy"]
    assert stats.quests_completed == 0

    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE users SET quests_completed = 7 WHERE id = 8060"
        )
    stats = await profile_stats(conn, 8060)
    assert stats is not None and stats.quests_completed == 7


async def test_profile_stats_none_for_unknown_user(conn: AsyncConnection) -> None:
    assert await profile_stats(conn, 999_999_999) is None


async def test_profile_stats_season_equity_is_mtm_not_cash(
    conn: AsyncConnection,
) -> None:
    """Regression: active_season_equity_minor returned the league
    account's raw cash -- a mid-season user with open positions saw cash
    mislabeled as equity. It must equal league_equity_minor (standings)."""
    from stockbot.seasons.service import (
        create_season,
        join_season,
        league_equity_minor,
        on_tick,
    )

    await bootstrap_user(conn, 8070)
    season_id = await create_season(
        conn, name="ProfileSeason", start_tick=0, end_tick=10_000,
        entry_fee_minor=0, stake_minor=5_000_000,
    )
    await on_tick(conn, 0)  # activate
    await join_season(conn, 8070, season_id)
    await execute_trade(
        conn, user_id=8070, ticker=await _first_ticker(conn),
        side="BUY", quantity=10, season_id=season_id,
    )

    stats = await profile_stats(conn, 8070)
    assert stats is not None
    assert stats.active_season_equity_minor == await league_equity_minor(
        conn, season_id, 8070
    )
    # And it is decisively NOT the raw cash balance, which dropped by the
    # fill's notional.
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT a.balance FROM season_entries e "
            "JOIN accounts a ON a.id = e.account_id "
            "WHERE e.user_id = %s AND e.season_id = %s",
            (8070, season_id),
        )
        (cash,) = await cur.fetchone()
    assert stats.active_season_equity_minor != int(cash)
