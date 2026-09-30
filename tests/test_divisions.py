"""Divisions: ladder membership, weekly seasons, promotion/relegation,
no-trade relegation, next-week respawn, leave, bot/suspension exclusion."""

import pytest
from psycopg import AsyncConnection

from stockbot.accounts.service import bootstrap_user
from stockbot.divisions import service as divisions
from stockbot.divisions.errors import NotAMemberError
from stockbot.seasons import service as seasons
from stockbot.trading.errors import TradingError
from stockbot.trading.service import execute_trade


async def _member(conn: AsyncConnection, user_id: int, tier: int = 3) -> None:
    """Direct membership write -- join() drives this, but close/equity
    tests want several members enrolled in the SAME week, which real
    mid-week joins deliberately don't do."""
    await bootstrap_user(conn, user_id)
    async with conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO division_memberships (user_id, tier, active) "
            "VALUES (%s, %s, TRUE)",
            (user_id, tier),
        )


async def _active_tier_season(conn: AsyncConnection, tier: int) -> int | None:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT id FROM seasons WHERE division_tier = %s AND status = 'ACTIVE' "
            "ORDER BY id DESC LIMIT 1",
            (tier,),
        )
        row = await cur.fetchone()
    return int(row[0]) if row else None


async def _tier(conn: AsyncConnection, user_id: int) -> int:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT tier FROM division_memberships WHERE user_id = %s", (user_id,)
        )
        (tier,) = await cur.fetchone()
    return int(tier)


async def test_join_assigns_bottom_tier_and_spawns_week(conn: AsyncConnection) -> None:
    await bootstrap_user(conn, 5001)
    tier = await divisions.join(conn, 5001, tick_index=0)
    assert tier == 3

    season_id = await _active_tier_season(conn, 3)
    assert season_id is not None
    season = await seasons.get_season(conn, season_id)
    assert season is not None and season.division_tier == 3
    assert season.entry_fee_minor == 0

    entry = await seasons.get_active_entry(conn, 5001, season_id)
    assert entry is not None


async def test_second_joiner_queues_for_next_week(conn: AsyncConnection) -> None:
    """Mid-week joins don't land mid-week -- the ladder's entry point is
    the week boundary, so a joiner while a week runs waits for the next."""
    await bootstrap_user(conn, 5002)
    await bootstrap_user(conn, 5003)
    await divisions.join(conn, 5002, tick_index=0)   # spawns + enrolls
    await divisions.join(conn, 5003, tick_index=0)   # queued

    season_id = await _active_tier_season(conn, 3)
    assert season_id is not None
    assert await seasons.get_active_entry(conn, 5002, season_id) is not None
    assert await seasons.get_active_entry(conn, 5003, season_id) is None
    # But the membership is real and active.
    assert await _tier(conn, 5003) == 3


async def test_join_rejects_bots_and_unknown(conn: AsyncConnection) -> None:
    async with conn.cursor() as cur:
        await cur.execute("INSERT INTO users (id, is_bot) VALUES (5004, TRUE)")
    with pytest.raises(TradingError):
        await divisions.join(conn, 5004, tick_index=0)
    with pytest.raises(TradingError):
        await divisions.join(conn, 999_999, tick_index=0)


async def test_leave_stops_reenrollment(conn: AsyncConnection) -> None:
    await _member(conn, 5010)
    await _member(conn, 5011)
    await divisions.ensure_week_seasons(conn, tick_index=0)
    season_id = await _active_tier_season(conn, 3)
    assert season_id is not None

    await divisions.leave(conn, 5010)
    with pytest.raises(NotAMemberError):
        await divisions.leave(conn, 5010)

    # This week still settles the leaver; the NEXT week's spawn skips them.
    await seasons.close_season(conn, season_id)
    next_week = await _active_tier_season(conn, 4)
    assert next_week is not None
    assert await seasons.get_active_entry(conn, 5011, next_week) is not None
    assert await seasons.get_active_entry(conn, 5010, next_week) is None


async def test_week_close_promotes_top_and_relegates_bottom(
    conn: AsyncConnection,
) -> None:
    """3 qualified members: top 2 promote to tier 2, bottom 1 relegates
    to tier 4 (move_count=2). Equity order comes from trading drag: more
    trades -> more fees/impact -> lower equity."""
    for uid in (5020, 5021, 5022):
        await _member(conn, uid, tier=3)
    await divisions.ensure_week_seasons(conn, tick_index=0)
    season_id = await _active_tier_season(conn, 3)
    assert season_id is not None

    # Equity order: 5020 (1 trade) > 5021 (2) > 5022 (3).
    for uid, qty in ((5020, 1), (5021, 2), (5022, 3)):
        await execute_trade(
            conn, user_id=uid, ticker="NORT", side="BUY",
            quantity=qty, season_id=season_id,
        )

    await seasons.close_season(conn, season_id)

    assert await _tier(conn, 5020) == 2
    assert await _tier(conn, 5021) == 2
    assert await _tier(conn, 5022) == 4

    # Result DMs went to every entrant with their rank and move.
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT user_id, payload FROM notifications WHERE kind = 'DIVISION_RESULT'"
        )
        rows = dict(await cur.fetchall())
    assert rows[5020]["rank"] == 1 and rows[5020]["moved"] == "promoted"
    assert rows[5022]["moved"] == "relegated"

    # New weeks spawned for both moved tiers.
    assert await _active_tier_season(conn, 2) is not None
    assert await _active_tier_season(conn, 4) is not None
    tier2_week = await _active_tier_season(conn, 2)
    assert tier2_week is not None
    assert await seasons.get_active_entry(conn, 5020, tier2_week) is not None
    assert await seasons.get_active_entry(conn, 5021, tier2_week) is not None


async def test_no_trade_member_auto_relegates(conn: AsyncConnection) -> None:
    for uid in (5030, 5031, 5032):
        await _member(conn, uid, tier=3)
    await divisions.ensure_week_seasons(conn, tick_index=0)
    season_id = await _active_tier_season(conn, 3)
    assert season_id is not None
    # 5030 and 5031 qualify; 5032 never trades.
    for uid in (5030, 5031):
        await execute_trade(
            conn, user_id=uid, ticker="NORT", side="BUY",
            quantity=1, season_id=season_id,
        )

    await seasons.close_season(conn, season_id)

    # Even with the idle player's untouched equity on top (100k beats any
    # trader's fee-dragged equity), min_trades drops them into relegation.
    assert await _tier(conn, 5032) == 4
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT payload FROM notifications "
            "WHERE kind = 'DIVISION_RESULT' AND user_id = 5032"
        )
        (payload,) = await cur.fetchone()
    assert payload["moved"] == "relegated"


async def test_gold_tier_promotes_nowhere(conn: AsyncConnection) -> None:
    """Tier 1 has no higher rung: the promotion slots empty, but the
    bottom of the table still relegates."""
    for uid in (5040, 5041, 5042):
        await _member(conn, uid, tier=1)
    await divisions.ensure_week_seasons(conn, tick_index=0)
    season_id = await _active_tier_season(conn, 1)
    assert season_id is not None
    for uid, qty in ((5040, 1), (5041, 2), (5042, 3)):
        await execute_trade(
            conn, user_id=uid, ticker="NORT", side="BUY",
            quantity=qty, season_id=season_id,
        )

    await seasons.close_season(conn, season_id)

    # The top move_count would promote but there's no rung higher, so
    # they hold Gold; only the rest of the bottom-K table drops.
    assert await _tier(conn, 5040) == 1
    assert await _tier(conn, 5041) == 1
    assert await _tier(conn, 5042) == 2


async def test_suspended_member_skipped_on_enrollment(conn: AsyncConnection) -> None:
    await _member(conn, 5050)
    await _member(conn, 5051)
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE users SET disabled_at = now() WHERE id = 5051"
        )

    await divisions.ensure_week_seasons(conn, tick_index=0)

    season_id = await _active_tier_season(conn, 3)
    assert season_id is not None
    assert await seasons.get_active_entry(conn, 5050, season_id) is not None
    assert await seasons.get_active_entry(conn, 5051, season_id) is None


async def test_ladder_lists_members_with_live_equity(conn: AsyncConnection) -> None:
    await _member(conn, 5060)
    await _member(conn, 5061)
    await divisions.ensure_week_seasons(conn, tick_index=0)

    rows = await divisions.ladder(conn)

    by_user = {r["user_id"]: r for r in rows}
    assert by_user[5060]["tier"] == 3
    assert by_user[5060]["equity_minor"] is not None
    assert by_user[5061]["season_id"] == by_user[5060]["season_id"]
