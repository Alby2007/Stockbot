"""Sandbox account: entitlement-gated private practice season (0050)."""

import pytest
from psycopg import AsyncConnection

from stockbot.accounts.service import STARTING_GRANT, bootstrap_user
from stockbot.ledger.service import get_balance, get_user_account_id
from stockbot.seasons.errors import SandboxAlreadyOpenError
from stockbot.seasons.service import (
    close_season,
    get_active_entry,
    get_open_season,
    get_sandbox_entry,
    get_season,
    open_sandbox,
    reset_sandbox,
)
from stockbot.shop.service import buy_item, owns_item
from stockbot.trading.service import execute_trade

SANDBOX_STAKE = 10_000  # $100.00


async def test_buy_sandbox_access_grants_perk(conn: AsyncConnection) -> None:
    await bootstrap_user(conn, 9001)
    assert not await owns_item(conn, 9001, "sandbox_access")
    await buy_item(conn, 9001, "sandbox_access")
    assert await owns_item(conn, 9001, "sandbox_access")
    # PERK purchases are a sink: the $30 left the main account.
    main = await get_user_account_id(conn, 9001)
    assert await get_balance(conn, main) == STARTING_GRANT - 3000


async def test_open_sandbox_grants_stake_without_fee(conn: AsyncConnection) -> None:
    await bootstrap_user(conn, 9002)
    season_id = await open_sandbox(conn, 9002)

    entry = await get_sandbox_entry(conn, 9002)
    assert entry is not None and entry[0] == season_id
    assert await get_balance(conn, entry[1]) == SANDBOX_STAKE
    # Zero entry fee: the main account kept the full starting grant.
    main = await get_user_account_id(conn, 9002)
    assert await get_balance(conn, main) == STARTING_GRANT

    season = await get_season(conn, season_id)
    assert season is not None
    assert season.status == "ACTIVE" and season.sandbox_user_id == 9002


async def test_open_sandbox_twice_rejected(conn: AsyncConnection) -> None:
    await bootstrap_user(conn, 9003)
    await open_sandbox(conn, 9003)
    with pytest.raises(SandboxAlreadyOpenError):
        await open_sandbox(conn, 9003)


async def test_sandbox_invisible_to_league_lookups(conn: AsyncConnection) -> None:
    await bootstrap_user(conn, 9004)
    await open_sandbox(conn, 9004)
    # The sandbox is ACTIVE but must never become "the season" for league
    # joins or the league flag's entry resolution.
    assert await get_open_season(conn) is None
    assert await get_active_entry(conn, 9004) is None


async def test_sandbox_trade_is_quarantined(conn: AsyncConnection) -> None:
    await bootstrap_user(conn, 9005)
    season_id = await open_sandbox(conn, 9005)
    entry = await get_sandbox_entry(conn, 9005)
    assert entry is not None
    sandbox_account = entry[1]

    result = await execute_trade(
        conn, user_id=9005, ticker="NORT", side="BUY", quantity=1, season_id=season_id
    )
    assert result.quantity == 1

    # Cash left the sandbox account; the main account is untouched.
    assert await get_balance(conn, sandbox_account) == (
        SANDBOX_STAKE - result.notional_minor - result.fee_minor
    )
    main = await get_user_account_id(conn, 9005)
    assert await get_balance(conn, main) == STARTING_GRANT

    # League-kind accounts never enter net worth.
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT kind FROM accounts WHERE id = %s", (sandbox_account,)
        )
        assert (await cur.fetchone())[0] == "LEAGUE"  # type: ignore[index]


async def test_reset_sandbox_clean_slate(conn: AsyncConnection) -> None:
    await bootstrap_user(conn, 9006)
    old_season = await open_sandbox(conn, 9006)
    old_entry = await get_sandbox_entry(conn, 9006)
    assert old_entry is not None
    await execute_trade(
        conn, user_id=9006, ticker="NORT", side="BUY", quantity=1,
        season_id=old_season,
    )

    new_season = await reset_sandbox(conn, 9006)
    assert new_season != old_season

    old = await get_season(conn, old_season)
    assert old is not None and old.status == "CLOSED"
    # The old league account was swept; the fresh one holds a clean stake.
    assert await get_balance(conn, old_entry[1]) == 0
    new_entry = await get_sandbox_entry(conn, 9006)
    assert new_entry is not None and new_entry[0] == new_season
    assert new_entry[1] != old_entry[1]
    assert await get_balance(conn, new_entry[1]) == SANDBOX_STAKE
    # Old positions don't leak into the new season.
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT COUNT(*) FROM positions WHERE user_id = %s AND season_id = %s",
            (9006, new_season),
        )
        assert (await cur.fetchone())[0] == 0  # type: ignore[index]


async def test_reset_sandbox_without_open_sandbox_starts_one(
    conn: AsyncConnection,
) -> None:
    await bootstrap_user(conn, 9007)
    season_id = await reset_sandbox(conn, 9007)
    entry = await get_sandbox_entry(conn, 9007)
    assert entry is not None and entry[0] == season_id


async def test_sandbox_close_mints_no_trophy_or_notification(
    conn: AsyncConnection,
) -> None:
    await bootstrap_user(conn, 9008)
    season_id = await open_sandbox(conn, 9008)
    await close_season(conn, season_id)

    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT COUNT(*) FROM entitlements WHERE user_id = %s", (9008,)
        )
        assert (await cur.fetchone())[0] == 0  # type: ignore[index]
        await cur.execute(
            "SELECT COUNT(*) FROM notifications WHERE user_id = %s", (9008,)
        )
        assert (await cur.fetchone())[0] == 0  # type: ignore[index]
        await cur.execute(
            "SELECT COUNT(*) FROM equity_snapshots WHERE season_id = %s", (season_id,)
        )
        assert (await cur.fetchone())[0] == 0  # type: ignore[index]
    season = await get_season(conn, season_id)
    assert season is not None and season.prize_pool_minor == 0
