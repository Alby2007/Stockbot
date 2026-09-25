"""Phase H1: snowflake-age grant gating + deferred grants."""

from __future__ import annotations

import random
import time

import pytest
from psycopg import AsyncConnection

from stockbot.accounts.service import (
    STARTING_GRANT,
    bootstrap_user,
    discord_age_days,
)
from stockbot.claims.errors import (
    AccountTooYoungError,
    FirstClaimLockedError,
)
from stockbot.claims.service import claim_daily
from stockbot.ledger.service import get_balance

_DISCORD_EPOCH_MS = 1_420_070_400_000


def _snowflake(age_days: float) -> int:
    """A unique snowflake `age_days` in the past (or future if negative)."""
    rng = random.SystemRandom()
    ts_ms = int(time.time() * 1000) - _DISCORD_EPOCH_MS - int(age_days * 86_400_000)
    return (ts_ms << 22) | rng.randrange(1, 1 << 22)


def test_discord_age_days_math() -> None:
    # Snowflake for exactly epoch + 1s.
    assert discord_age_days((1000 << 22), now_ms=_DISCORD_EPOCH_MS + 1000) == 0
    # ~1 day old.
    sid = _snowflake(1.0)
    assert 0.9 < discord_age_days(sid) < 1.1
    # Future-dated ids (e.g. randrange(0, 2**62)) read as negative age.
    assert discord_age_days((1 << 61) | 1) < 0
    # Small fixture ids (3001 etc.) all resolve to ~2015 -- eligible.
    assert discord_age_days(3001) > 4000


async def test_young_snowflake_gets_pending_account(conn: AsyncConnection) -> None:
    user_id = _snowflake(5)
    result = await bootstrap_user(conn, user_id)
    assert result.created and result.grant_pending
    assert not result.granted_now
    assert result.min_age_days == 30
    assert await get_balance(conn, result.account_id) == 0
    # No grant ledger row exists.
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT count(*) FROM ledger_entries "
            "WHERE reason = 'STARTING_GRANT' AND account_id = %s",
            (result.account_id,),
        )
        (n,) = await cur.fetchone()
    assert n == 0


async def test_grant_self_heals_once_eligible(conn: AsyncConnection) -> None:
    """Nothing stays pending forever: the first bootstrap after aging
    delivers the grant -- simulated by tuning the floor to 0 days."""
    user_id = _snowflake(5)
    first = await bootstrap_user(conn, user_id)
    assert first.grant_pending

    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE config SET value = 0 "
            "WHERE key = 'accounts.min_discord_age_days'"
        )

    second = await bootstrap_user(conn, user_id)
    assert not second.created
    assert second.granted_now and not second.grant_pending
    assert await get_balance(conn, second.account_id) == STARTING_GRANT

    # And never again.
    third = await bootstrap_user(conn, user_id)
    assert not third.granted_now and not third.grant_pending
    assert await get_balance(conn, third.account_id) == STARTING_GRANT


async def test_eligible_snowflake_grants_exactly_once(conn: AsyncConnection) -> None:
    user_id = _snowflake(400)
    first = await bootstrap_user(conn, user_id)
    assert first.created and first.granted_now and not first.grant_pending
    assert await get_balance(conn, first.account_id) == STARTING_GRANT
    second = await bootstrap_user(conn, user_id)
    assert not second.granted_now
    assert await get_balance(conn, second.account_id) == STARTING_GRANT


async def test_claim_rejects_underage_snowflake(conn: AsyncConnection) -> None:
    user_id = _snowflake(5)
    await bootstrap_user(conn, user_id)
    with pytest.raises(AccountTooYoungError):
        await claim_daily(conn, user_id, enforce_first_claim_delay=False)


async def test_first_claim_delay_carries_unlock(conn: AsyncConnection) -> None:
    """The service owns the 24h first-claim gate and reports WHEN it
    unlocks; aging users.created_at lifts it."""
    user_id = _snowflake(400)
    await bootstrap_user(conn, user_id)
    with pytest.raises(FirstClaimLockedError) as exc_info:
        await claim_daily(conn, user_id)
    unlock_at = exc_info.value.unlock_at
    assert unlock_at > _now()

    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE users SET created_at = now() - interval '2 days' "
            "WHERE id = %s",
            (user_id,),
        )
    amount, streak = await claim_daily(conn, user_id)
    assert amount > 0 and streak == 1


def _now():
    from datetime import UTC, datetime

    return datetime.now(UTC)
