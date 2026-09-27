from __future__ import annotations

from datetime import date, timedelta

import pytest
from psycopg import AsyncConnection

from stockbot.accounts.service import STARTING_GRANT
from stockbot.claims.errors import AlreadyClaimedTodayError
from stockbot.claims.service import (
    BASE_CLAIM_MINOR,
    _segment_for_u,
    _wheel_u,
    claim_amount,
    claim_daily,
    wheel_roll,
)
from stockbot.ledger.service import get_balance, get_user_account_id

_TEST_SEED = "test-wheel-seed"


async def _disable_wheel(conn: AsyncConnection) -> None:
    """Force claim.wheel_enabled=0 so flat-amount assertions hold
    regardless of the seeded default (migration 0051 enables the wheel)."""
    async with conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO config (key, value) VALUES ('claim.wheel_enabled', 0) "
            "ON CONFLICT (key) DO UPDATE SET value = 0"
        )


async def test_first_claim_grants_base_amount_with_streak_one(conn: AsyncConnection) -> None:
    await _disable_wheel(conn)
    res = await claim_daily(conn, 2001, enforce_first_claim_delay=False)
    assert res.streak == 1
    assert res.amount_minor == BASE_CLAIM_MINOR
    assert res.roll is None


async def test_second_claim_same_day_is_rejected(conn: AsyncConnection) -> None:
    await claim_daily(conn, 2002, enforce_first_claim_delay=False)
    with pytest.raises(AlreadyClaimedTodayError):
        await claim_daily(conn, 2002, enforce_first_claim_delay=False)


async def test_claim_pays_into_the_users_cash_account(conn: AsyncConnection) -> None:
    res = await claim_daily(conn, 2003, enforce_first_claim_delay=False)
    account_id = await get_user_account_id(conn, 2003)
    # bootstrap's starting grant plus the claim
    assert await get_balance(conn, account_id) == STARTING_GRANT + res.amount_minor


async def test_consecutive_day_claim_increments_streak(conn: AsyncConnection) -> None:
    await _disable_wheel(conn)
    await claim_daily(conn, 2004, enforce_first_claim_delay=False)
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE claims SET last_claim_date = CURRENT_DATE - 1 WHERE user_id = %s", (2004,)
        )
    res = await claim_daily(conn, 2004, enforce_first_claim_delay=False)
    assert res.streak == 2
    assert res.amount_minor == claim_amount(2)


async def test_gap_of_more_than_one_day_resets_streak(conn: AsyncConnection) -> None:
    await _disable_wheel(conn)
    await claim_daily(conn, 2005, enforce_first_claim_delay=False)
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE claims SET last_claim_date = CURRENT_DATE - 5, streak = 7 WHERE user_id = %s",
            (2005,),
        )
    res = await claim_daily(conn, 2005, enforce_first_claim_delay=False)
    assert res.streak == 1
    assert res.amount_minor == BASE_CLAIM_MINOR


async def test_claim_as_of_date_overrides_the_server_date(conn: AsyncConnection) -> None:
    """The sim harness passes simulated dates; the override must drive the
    same per-day uniqueness and streak logic as CURRENT_DATE."""
    await _disable_wheel(conn)
    day = date(2025, 6, 1)
    res = await claim_daily(
        conn, 2006, enforce_first_claim_delay=False, as_of_date=day
    )
    assert res.streak == 1
    assert res.amount_minor == BASE_CLAIM_MINOR

    with pytest.raises(AlreadyClaimedTodayError):
        await claim_daily(
            conn, 2006, enforce_first_claim_delay=False, as_of_date=day
        )

    res = await claim_daily(
        conn, 2006, enforce_first_claim_delay=False,
        as_of_date=day + timedelta(days=1),
    )
    assert res.streak == 2
    assert res.amount_minor == claim_amount(2)


def test_claim_amount_caps_out() -> None:
    assert claim_amount(1) == BASE_CLAIM_MINOR
    capped = claim_amount(100)
    assert capped == claim_amount(10)


# --- wheel ---


def test_segment_for_u_boundaries() -> None:
    """Cumulative weights 50/30/12/6/2 (sum 100) at the default jackpot."""
    assert _segment_for_u(0.0, 2.0)[0] == "cold"
    assert _segment_for_u(0.4999, 2.0)[0] == "cold"
    assert _segment_for_u(0.5, 2.0)[0] == "even"
    assert _segment_for_u(0.7999, 2.0)[0] == "even"
    assert _segment_for_u(0.8, 2.0)[0] == "warm"
    assert _segment_for_u(0.9199, 2.0)[0] == "warm"
    assert _segment_for_u(0.92, 2.0)[0] == "hot"
    assert _segment_for_u(0.9799, 2.0)[0] == "hot"
    assert _segment_for_u(0.98, 2.0)[0] == "jackpot"
    assert _segment_for_u(0.9999, 2.0)[0] == "jackpot"
    # jackpot_pct=0 -> jackpot unreachable; u~1 lands on hot.
    assert _segment_for_u(0.9999, 0.0)[0] == "hot"
    # A fatter jackpot shifts its threshold earlier.
    assert _segment_for_u(0.95, 10.0)[0] == "jackpot"


def test_wheel_u_is_seed_and_date_dependent() -> None:
    day = date(2026, 10, 1)
    assert _wheel_u(42, day, "a") == _wheel_u(42, day, "a")
    assert _wheel_u(42, day, "a") != _wheel_u(42, day, "b")
    assert _wheel_u(42, day, "a") != _wheel_u(42, day + timedelta(days=1), "a")
    assert _wheel_u(42, day, "a") != _wheel_u(43, day, "a")


def test_wheel_roll_is_deterministic() -> None:
    day = date(2026, 10, 1)
    assert wheel_roll(42, day, _TEST_SEED, 2.0) == wheel_roll(42, day, _TEST_SEED, 2.0)


async def test_wheel_claim_pays_the_roll_and_records_it(conn: AsyncConnection) -> None:
    uid = 2007
    day = date(2026, 10, 2)
    res = await claim_daily(
        conn, uid, as_of_date=day, enforce_first_claim_delay=False,
        wheel_seed=_TEST_SEED,
    )
    expected = wheel_roll(uid, day, _TEST_SEED, 2.0)
    assert res.roll == expected
    # Streak 1 multiplies by 1.0 -- the payout is the raw roll.
    assert res.amount_minor == expected.roll_minor

    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT last_segment, last_amount_minor FROM claims WHERE user_id = %s",
            (uid,),
        )
        row = await cur.fetchone()
    assert row == (expected.segment, res.amount_minor)


async def test_wheel_streak_multiplier_applies(conn: AsyncConnection) -> None:
    uid = 2008
    d1 = date(2026, 10, 3)
    d2 = d1 + timedelta(days=1)
    await claim_daily(
        conn, uid, as_of_date=d1, enforce_first_claim_delay=False,
        wheel_seed=_TEST_SEED,
    )
    res = await claim_daily(
        conn, uid, as_of_date=d2, enforce_first_claim_delay=False,
        wheel_seed=_TEST_SEED,
    )
    roll2 = wheel_roll(uid, d2, _TEST_SEED, 2.0)
    assert res.streak == 2
    assert res.amount_minor == round(
        roll2.roll_minor * claim_amount(2) / BASE_CLAIM_MINOR
    )


async def test_wheel_jackpot_pct_config_overrides_weight(conn: AsyncConnection) -> None:
    """An admin-set claim.jackpot_pct changes the segment mapping live."""
    uid = 2009
    day = date(2026, 10, 4)
    async with conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO config (key, value) VALUES ('claim.jackpot_pct', 50) "
            "ON CONFLICT (key) DO UPDATE SET value = 50"
        )
    res = await claim_daily(
        conn, uid, as_of_date=day, enforce_first_claim_delay=False,
        wheel_seed=_TEST_SEED,
    )
    expected = wheel_roll(uid, day, _TEST_SEED, 50.0)
    assert res.roll == expected


def test_wheel_ev_stays_bounded() -> None:
    """Faucet sanity: mean payout should stay near the legacy flat claim.
    A drift here is silent inflation -- same role as the balance audits."""
    total = 0
    n = 400
    for i in range(n):
        total += wheel_roll(10_000 + i, date(2026, 1, 1), "ev-seed", 2.0).roll_minor
    mean = total / n / BASE_CLAIM_MINOR
    assert 1.1 <= mean <= 1.8
