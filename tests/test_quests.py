"""Daily/weekly quests: rotation, per-kind completion sweep, payouts."""

from decimal import Decimal

from psycopg import AsyncConnection

from stockbot.accounts.service import bootstrap_user
from stockbot.alerts.service import create_alert
from stockbot.ipo.service import create_offering, subscribe
from stockbot.ledger.service import (
    get_balance,
    get_system_account_id,
    get_user_account_id,
    post_transfer,
)
from stockbot.market.engine import TICKS_PER_DAY
from stockbot.market.tick import apply_tick
from stockbot.orders.service import place_order
from stockbot.quests.service import list_quests, on_tick, sweep_completions
from stockbot.seasons.service import create_season, join_season
from stockbot.shorts.service import cover_bounded_short, open_bounded_short
from stockbot.status.service import evaluate_badges
from stockbot.trading.service import execute_trade

SEED = "test-quests-seed"
DAY = TICKS_PER_DAY


async def _instance(
    conn: AsyncConnection,
    def_key: str,
    *,
    target: int | None = None,
    lo: int = 0,
    hi: int = DAY - 1,
    period_index: int = 0,
) -> int:
    """Insert a quest_instance directly -- isolates sweep mechanics from
    rotation's random pick. Returns the instance id."""
    async with conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO quest_instances
                (def_key, kind, period, period_index, window_start,
                 window_end, target, reward_minor)
            SELECT key, kind, period, %s, %s, %s,
                   COALESCE(%s, target), reward_minor
            FROM quest_defs WHERE key = %s
            RETURNING id
            """,
            (period_index, lo, hi, target, def_key),
        )
        row = await cur.fetchone()
    assert row is not None
    return int(row[0])


async def _give_cash(conn: AsyncConnection, user_id: int, amount: int) -> None:
    faucet_id = await get_system_account_id(conn, "FAUCET")
    await post_transfer(
        conn,
        from_account_id=faucet_id,
        to_account_id=await get_user_account_id(conn, user_id),
        amount=amount,
        reason="TEST_TOPUP",
    )


async def _no_rotation(conn: AsyncConnection) -> None:
    """apply_tick runs the quest rotation internally at boundary ticks --
    zero the counts so tests only see their directly-inserted instances."""
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE config SET value = 0 "
            "WHERE key IN ('quests.daily_count', 'quests.weekly_count')"
        )


async def _completion_count(conn: AsyncConnection, user_id: int) -> int:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT COUNT(*) FROM quest_completions WHERE user_id = %s",
            (user_id,),
        )
        row = await cur.fetchone()
    assert row is not None
    return int(row[0])


async def _quest_count(conn: AsyncConnection, period: str, period_index: int) -> int:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT COUNT(*) FROM quest_instances "
            "WHERE period = %s AND period_index = %s",
            (period, period_index),
        )
        row = await cur.fetchone()
    assert row is not None
    return int(row[0])


# --- rotation ---------------------------------------------------------


async def test_rotation_creates_daily_and_weekly_at_day_zero(
    conn: AsyncConnection,
) -> None:
    created = await on_tick(conn, 0)
    assert created == 5  # daily_count 3 + weekly_count 2 (day 0 % 7 == 0)
    assert await _quest_count(conn, "DAILY", 0) == 3
    assert await _quest_count(conn, "WEEKLY", 0) == 2
    # Replay-safe: the same boundary again adds nothing.
    assert await on_tick(conn, 0) == 0
    assert await _quest_count(conn, "DAILY", 0) == 3


async def test_rotation_only_at_day_boundaries(conn: AsyncConnection) -> None:
    assert await on_tick(conn, 1) == 0
    assert await on_tick(conn, DAY - 1) == 0
    assert await _quest_count(conn, "DAILY", 0) == 0


async def test_daily_rotates_without_weekly_on_other_days(
    conn: AsyncConnection,
) -> None:
    assert await on_tick(conn, DAY) == 3  # day_index 1: daily only
    assert await _quest_count(conn, "DAILY", 1) == 3
    assert await _quest_count(conn, "WEEKLY", 1) == 0


async def test_expiry_marks_past_windows(conn: AsyncConnection) -> None:
    await on_tick(conn, 0)  # day-0 dailies end at 1439; weeklies run 7 days
    await on_tick(conn, DAY)  # boundary: expires the dailies
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT COUNT(*) FROM quest_instances "
            "WHERE period = 'DAILY' AND period_index = 0 AND status = 'EXPIRED'"
        )
        row = await cur.fetchone()
        await cur.execute(
            "SELECT COUNT(*) FROM quest_instances "
            "WHERE period = 'WEEKLY' AND status = 'OPEN'"
        )
        weekly = await cur.fetchone()
    assert row is not None and int(row[0]) == 3
    assert weekly is not None and int(weekly[0]) == 2  # still open


async def test_ipo_def_skipped_without_open_offering(
    conn: AsyncConnection,
) -> None:
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE config SET value = 10 WHERE key = 'quests.daily_count'"
        )
    await on_tick(conn, 0)
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT COUNT(*) FROM quest_instances qi "
            "JOIN quest_defs d ON d.key = qi.def_key "
            "WHERE d.kind = 'IPO_SUBSCRIBE'"
        )
        row = await cur.fetchone()
    assert row is not None and int(row[0]) == 0
    # With an open offering the def becomes eligible next rotation.
    await create_offering(
        conn, ticker="IPOQ", name="IPOQ Corp", sector_key="TECH",
        offer_price=10.0, shares_offered=100, duration_ticks=100,
    )
    await on_tick(conn, DAY)
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT COUNT(*) FROM quest_instances qi "
            "JOIN quest_defs d ON d.key = qi.def_key "
            "WHERE d.kind = 'IPO_SUBSCRIBE'"
        )
        row = await cur.fetchone()
    assert row is not None and int(row[0]) == 1


# --- completion sweep ---------------------------------------------------


async def test_trade_count_completes_and_pays(conn: AsyncConnection) -> None:
    await _no_rotation(conn)
    await bootstrap_user(conn, 1)
    await _instance(conn, "trade5", target=1)
    await apply_tick(conn, SEED)
    await execute_trade(conn, user_id=1, ticker="NORT", side="BUY", quantity=1)
    main = await get_user_account_id(conn, 1)
    before = await get_balance(conn, main)

    assert await sweep_completions(conn, 10) == 1
    assert await get_balance(conn, main) == before + 500
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT kind, payload FROM notifications "
            "WHERE user_id = 1 AND kind = 'QUEST_COMPLETED'"
        )
        notes = await cur.fetchall()
        await cur.execute(
            "SELECT quests_completed FROM users WHERE id = 1"
        )
        qc = await cur.fetchone()
    assert len(notes) == 1
    assert notes[0][1]["name"] == "Make 5 trades"
    assert notes[0][1]["reward_minor"] == 500
    assert qc is not None and qc[0] == 1
    # Pay-once: a second sweep never double-pays.
    assert await sweep_completions(conn, 11) == 0
    assert await get_balance(conn, main) == before + 500


async def test_distinct_tickers_progress_and_completion(
    conn: AsyncConnection,
) -> None:
    await _no_rotation(conn)
    await bootstrap_user(conn, 2)
    await _give_cash(conn, 2, 100_000)  # three buys exceed the $100 grant
    iid = await _instance(conn, "tickers3")
    await apply_tick(conn, SEED)
    await execute_trade(conn, user_id=2, ticker="NORT", side="BUY", quantity=1)
    await execute_trade(conn, user_id=2, ticker="HARB", side="BUY", quantity=1)

    quests = await list_quests(conn, 2, 10)
    quest = next(q for q in quests if q["id"] == iid)
    assert quest["progress"] == 2
    assert quest["target"] == 3
    assert not quest["completed"]

    await execute_trade(conn, user_id=2, ticker="SOUT", side="BUY", quantity=1)
    assert await sweep_completions(conn, 11) == 1
    quests = await list_quests(conn, 2, 11)
    quest = next(q for q in quests if q["id"] == iid)
    assert quest["completed"]


async def test_volume_quest_sums_notional(conn: AsyncConnection) -> None:
    await _no_rotation(conn)
    await bootstrap_user(conn, 3)
    await _instance(conn, "vol200", target=1000)  # $10 of notional
    await apply_tick(conn, SEED)
    await execute_trade(conn, user_id=3, ticker="NORT", side="BUY", quantity=1)
    assert await sweep_completions(conn, 10) == 1


async def test_order_placed_and_filled(conn: AsyncConnection) -> None:
    await _no_rotation(conn)
    await bootstrap_user(conn, 4)
    await _give_cash(conn, 4, 100_000)
    await _instance(conn, "orders2", target=1)
    await _instance(conn, "fill1", target=1)
    await apply_tick(conn, SEED)
    # A limit buy far above the mark is marketable -- rests this tick,
    # fills on the next tick's book pass.
    await place_order(
        conn, user_id=4, ticker="NORT", side="BUY", quantity=1,
        limit_price=Decimal("10000"),
    )
    # Placed counts immediately.
    assert await sweep_completions(conn, 10) == 1
    await apply_tick(conn, SEED)  # match_orders fills it; the tick's own
    # sweep may pay fill1 inside the tick transaction -- either way both
    # completions exist and each paid exactly once.
    assert await _completion_count(conn, 4) == 2


async def test_short_profit_requires_winning_cover(conn: AsyncConnection) -> None:
    await _no_rotation(conn)
    await bootstrap_user(conn, 5)
    await _give_cash(conn, 5, 100_000)  # two shorts need collateral headroom
    await _instance(conn, "short1")
    await apply_tick(conn, SEED)
    result = await open_bounded_short(conn, user_id=5, ticker="NORT", quantity=1)
    # Unprofitable close (fill price up 20%): does NOT complete. Covers
    # fill at base_price + spread/impact, so both columns move together.
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE instruments SET base_price = base_price * 1.2, "
            "quoted_price = quoted_price * 1.2 WHERE ticker = 'NORT'"
        )
    first = await cover_bounded_short(
        conn, user_id=5, short_id=result.short_id
    )
    assert first.payoff_minor < 0
    assert await sweep_completions(conn, 10) == 0
    # A winning close completes it.
    result2 = await open_bounded_short(conn, user_id=5, ticker="HARB", quantity=1)
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE instruments SET base_price = base_price * 0.9, "
            "quoted_price = quoted_price * 0.9 WHERE ticker = 'HARB'"
        )
    await cover_bounded_short(conn, user_id=5, short_id=result2.short_id)
    assert await sweep_completions(conn, 11) == 1


async def test_alert_set_completes(conn: AsyncConnection) -> None:
    await _no_rotation(conn)
    await bootstrap_user(conn, 6)
    await _instance(conn, "alert1")
    await apply_tick(conn, SEED)  # created_tick needs a market_ticks row
    await create_alert(
        conn, user_id=6, ticker="NORT", direction="ABOVE",
        target=Decimal("1"),  # created_tick stamps inside the window
    )
    assert await sweep_completions(conn, 10) == 1


async def test_ipo_subscribe_completes(conn: AsyncConnection) -> None:
    await _no_rotation(conn)
    await bootstrap_user(conn, 7)
    await _instance(conn, "ipo1", lo=0, hi=10_000)
    await create_offering(
        conn, ticker="IPOW", name="IPOW Corp", sector_key="TECH",
        offer_price=10.0, shares_offered=100, duration_ticks=100,
    )
    await subscribe(conn, user_id=7, ticker="IPOW", amount_minor=100)
    assert await sweep_completions(conn, 10) == 1


async def test_league_trade_counts_and_pays_main(conn: AsyncConnection) -> None:
    season_id = await create_season(
        conn, name="Quest Season", start_tick=0, end_tick=10_000,
        entry_fee_minor=0, stake_minor=50_000,
    )
    from stockbot.seasons.service import on_tick as seasons_on_tick

    await seasons_on_tick(conn, 0)
    await bootstrap_user(conn, 8)
    await join_season(conn, 8, season_id)
    await _no_rotation(conn)
    await _instance(conn, "trade5", target=1)
    await apply_tick(conn, SEED)
    await execute_trade(
        conn, user_id=8, ticker="NORT", side="BUY", quantity=1,
        season_id=season_id,
    )
    main = await get_user_account_id(conn, 8)
    before = await get_balance(conn, main)
    assert await sweep_completions(conn, 10) == 1
    # League activity counts, but the reward pays the MAIN account.
    assert await get_balance(conn, main) == before + 500


async def test_disabled_gates_rotation_and_sweep(conn: AsyncConnection) -> None:
    async with conn.cursor() as cur:
        await cur.execute("UPDATE config SET value = 0 WHERE key = 'quests.enabled'")
    assert await on_tick(conn, 0) == 0
    assert await _quest_count(conn, "DAILY", 0) == 0
    await bootstrap_user(conn, 9)
    await _instance(conn, "trade5", target=1)
    await apply_tick(conn, SEED)
    await execute_trade(conn, user_id=9, ticker="NORT", side="BUY", quantity=1)
    assert await sweep_completions(conn, 10) == 0
    assert await _completion_count(conn, 9) == 0


async def test_quests_badge_metric(conn: AsyncConnection) -> None:
    await bootstrap_user(conn, 10)
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE users SET quests_completed = 5 WHERE id = 10"
        )
    await evaluate_badges(conn, DAY)
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT item_key FROM entitlements WHERE user_id = 10 "
            "AND item_key LIKE 'badge_quest%'"
        )
        rows = await cur.fetchall()
    assert {r[0] for r in rows} == {"badge_quest_5"}
