"""Milestone badges: threshold grants at the day boundary, idempotency,
profile surfacing, and the not-for-sale guard on grant-only items."""

from __future__ import annotations

import pytest
from psycopg import AsyncConnection

from stockbot.accounts.service import bootstrap_user
from stockbot.ledger.service import (
    get_system_account_id,
    get_user_account_id,
    post_transfer,
)
from stockbot.shop.errors import UnknownItemError
from stockbot.shop.service import buy_item
from stockbot.status.service import evaluate_badges, profile_stats

TICKS_PER_DAY = 1440


async def _give_cash(conn: AsyncConnection, user_id: int, amount: int) -> None:
    faucet = await get_system_account_id(conn, "FAUCET")
    account = await get_user_account_id(conn, user_id)
    await post_transfer(
        conn,
        from_account_id=faucet,
        to_account_id=account,
        amount=amount,
        reason="TEST_TOPUP",
    )


async def _badge_keys(conn: AsyncConnection, user_id: int) -> set[str]:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT item_key FROM entitlements WHERE user_id = %s",
            (user_id,),
        )
        return {str(r[0]) for r in await cur.fetchall()}


async def test_net_worth_badges_grant_at_threshold(conn: AsyncConnection) -> None:
    await bootstrap_user(conn, 8101)  # $100 grant -> no badge yet
    n = await evaluate_badges(conn, TICKS_PER_DAY)
    assert "badge_nw_500" not in await _badge_keys(conn, 8101)

    await _give_cash(conn, 8101, 40_000)  # -> $500 total net worth
    n = await evaluate_badges(conn, TICKS_PER_DAY)
    assert n >= 1
    assert "badge_nw_500" in await _badge_keys(conn, 8101)
    assert "badge_nw_2k" not in await _badge_keys(conn, 8101)


async def test_badges_off_boundary_noop(conn: AsyncConnection) -> None:
    await bootstrap_user(conn, 8102)
    await _give_cash(conn, 8102, 10_000_000)
    assert await evaluate_badges(conn, TICKS_PER_DAY + 1) == 0
    assert not await _badge_keys(conn, 8102)


async def test_badges_idempotent_no_double_grant_or_dm(conn: AsyncConnection) -> None:
    await bootstrap_user(conn, 8103)
    await _give_cash(conn, 8103, 100_000)  # $1,000 -> badge_nw_500 only
    await evaluate_badges(conn, TICKS_PER_DAY)
    badges = await _badge_keys(conn, 8103)
    assert badges == {"badge_nw_500"}
    # A second pass grants nothing new -- anywhere, not just for this user.
    assert await evaluate_badges(conn, TICKS_PER_DAY) == 0
    assert await _badge_keys(conn, 8103) == badges
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT COUNT(*) FROM notifications "
            "WHERE user_id = 8103 AND kind = 'BADGE_EARNED'"
        )
        (dm_count,) = await cur.fetchone()
    assert dm_count == len(badges)


async def test_volume_and_streak_badges(conn: AsyncConnection) -> None:
    await bootstrap_user(conn, 8104)
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE users SET total_traded_minor = 150_000 WHERE id = 8104"
        )
        await cur.execute(
            "INSERT INTO claims (user_id, last_claim_date, streak) "
            "VALUES (8104, CURRENT_DATE, 8)"
        )
    await evaluate_badges(conn, TICKS_PER_DAY)
    keys = await _badge_keys(conn, 8104)
    assert "badge_vol_1k" in keys and "badge_vol_10k" not in keys
    assert "badge_streak_7" in keys and "badge_streak_14" not in keys


async def test_badges_surface_on_profile(conn: AsyncConnection) -> None:
    await bootstrap_user(conn, 8105)
    await _give_cash(conn, 8105, 100_000)
    await evaluate_badges(conn, TICKS_PER_DAY)
    stats = await profile_stats(conn, 8105)
    assert stats is not None
    assert "Half Grand" in stats.trophies


async def test_badge_items_not_purchasable(conn: AsyncConnection) -> None:
    await bootstrap_user(conn, 8106)
    await _give_cash(conn, 8106, 10_000_000)
    with pytest.raises(UnknownItemError):
        await buy_item(conn, 8106, "badge_nw_500")
