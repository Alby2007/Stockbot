from __future__ import annotations

from datetime import date, timedelta

import pytest
from psycopg import AsyncConnection

from stockbot.accounts.service import STARTING_GRANT
from stockbot.claims.errors import AlreadyClaimedTodayError
from stockbot.claims.service import BASE_CLAIM_MINOR, claim_amount, claim_daily
from stockbot.ledger.service import get_balance, get_user_account_id


async def test_first_claim_grants_base_amount_with_streak_one(conn: AsyncConnection) -> None:
    amount, streak = await claim_daily(conn, 2001)
    assert streak == 1
    assert amount == BASE_CLAIM_MINOR


async def test_second_claim_same_day_is_rejected(conn: AsyncConnection) -> None:
    await claim_daily(conn, 2002)
    with pytest.raises(AlreadyClaimedTodayError):
        await claim_daily(conn, 2002)


async def test_claim_pays_into_the_users_cash_account(conn: AsyncConnection) -> None:
    amount, _ = await claim_daily(conn, 2003)
    account_id = await get_user_account_id(conn, 2003)
    # bootstrap's starting grant plus the claim
    assert await get_balance(conn, account_id) == STARTING_GRANT + amount


async def test_consecutive_day_claim_increments_streak(conn: AsyncConnection) -> None:
    await claim_daily(conn, 2004)
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE claims SET last_claim_date = CURRENT_DATE - 1 WHERE user_id = %s", (2004,)
        )
    amount, streak = await claim_daily(conn, 2004)
    assert streak == 2
    assert amount == claim_amount(2)


async def test_gap_of_more_than_one_day_resets_streak(conn: AsyncConnection) -> None:
    await claim_daily(conn, 2005)
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE claims SET last_claim_date = CURRENT_DATE - 5, streak = 7 WHERE user_id = %s",
            (2005,),
        )
    amount, streak = await claim_daily(conn, 2005)
    assert streak == 1
    assert amount == BASE_CLAIM_MINOR


async def test_claim_as_of_date_overrides_the_server_date(conn: AsyncConnection) -> None:
    """The sim harness passes simulated dates; the override must drive the
    same per-day uniqueness and streak logic as CURRENT_DATE."""
    day = date(2025, 6, 1)
    amount, streak = await claim_daily(conn, 2006, as_of_date=day)
    assert streak == 1
    assert amount == BASE_CLAIM_MINOR

    with pytest.raises(AlreadyClaimedTodayError):
        await claim_daily(conn, 2006, as_of_date=day)

    amount, streak = await claim_daily(conn, 2006, as_of_date=day + timedelta(days=1))
    assert streak == 2
    assert amount == claim_amount(2)


def test_claim_amount_caps_out() -> None:
    assert claim_amount(1) == BASE_CLAIM_MINOR
    capped = claim_amount(100)
    assert capped == claim_amount(10)
