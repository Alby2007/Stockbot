"""Bounded shorts (Phase 1.5): open, cover, knockout, isolation."""

import pytest
from psycopg import AsyncConnection

from stockbot.accounts.service import STARTING_GRANT, bootstrap_user
from stockbot.ledger.service import get_balance, get_system_account_id, get_user_account_id
from stockbot.market.tick import apply_tick
from stockbot.seasons.service import create_season, join_season, league_equity_minor, on_tick
from stockbot.shorts.errors import ShortNotFoundError
from stockbot.shorts.service import (
    cover_bounded_short,
    list_open_shorts,
    open_bounded_short,
    sweep_knockouts,
)
from stockbot.trading.errors import DuplicateInteractionError

SEED = "test-shorts-seed"


async def _open(conn: AsyncConnection, user_id: int = 1, quantity: int = 1):
    await bootstrap_user(conn, user_id)
    return await open_bounded_short(
        conn, user_id=user_id, ticker="NORT", quantity=quantity
    )


async def test_open_posts_collateral_and_fee(conn: AsyncConnection) -> None:
    result = await _open(conn)

    # Collateral = qty * fill * knockout_pct (25% default).
    expected_collateral = round(float(result.entry_price) * 1 * 0.25 * 100)
    assert result.collateral_minor == expected_collateral
    assert result.knockout_price > result.entry_price

    main = await get_user_account_id(conn, 1)
    assert await get_balance(conn, main) == STARTING_GRANT - expected_collateral - (
        result.fee_minor
    )

    shorts = await list_open_shorts(conn, 1)
    assert len(shorts) == 1 and shorts[0]["id"] == result.short_id


async def test_open_rejects_insufficient_collateral(conn: AsyncConnection) -> None:
    from stockbot.ledger.errors import InsufficientFundsError

    await bootstrap_user(conn, 2)
    # Big enough that collateral exceeds the grant, small enough that the
    # participation cap doesn't reject it first.
    with pytest.raises(InsufficientFundsError):
        await open_bounded_short(conn, user_id=2, ticker="NORT", quantity=10)


async def test_duplicate_interaction_rejected(conn: AsyncConnection) -> None:
    await bootstrap_user(conn, 3)
    await open_bounded_short(
        conn, user_id=3, ticker="NORT", quantity=1, interaction_id="dup-1"
    )
    with pytest.raises(DuplicateInteractionError):
        await open_bounded_short(
            conn, user_id=3, ticker="NORT", quantity=1, interaction_id="dup-1"
        )


async def test_cover_pays_collateral_plus_payoff(conn: AsyncConnection) -> None:
    result = await _open(conn, user_id=4)

    # Price drops 10%: the short profits.
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE instruments SET base_price = base_price * 0.9, "
            "quoted_price = quoted_price * 0.9 WHERE ticker = 'NORT'"
        )
    covered = await cover_bounded_short(conn, user_id=4, short_id=result.short_id)

    assert covered.payoff_minor > 0
    assert covered.payout_minor == result.collateral_minor + covered.payoff_minor
    # Total back exceeds collateral -> user net-positive vs the collateral posted.
    main = await get_user_account_id(conn, 4)
    balance = await get_balance(conn, main)
    assert balance > STARTING_GRANT - result.collateral_minor - result.fee_minor


async def test_cover_loses_when_price_rises(conn: AsyncConnection) -> None:
    result = await _open(conn, user_id=5)

    # Price rises 10% (under the 25% knockout): short loses but survives.
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE instruments SET base_price = base_price * 1.10, "
            "quoted_price = quoted_price * 1.10 WHERE ticker = 'NORT'"
        )
    covered = await cover_bounded_short(conn, user_id=5, short_id=result.short_id)

    assert covered.payoff_minor < 0
    assert covered.payout_minor < result.collateral_minor
    assert covered.payout_minor > 0  # defined-risk: some collateral returns


async def test_knockout_forfeits_collateral(conn: AsyncConnection) -> None:
    result = await _open(conn, user_id=6)

    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE instruments SET quoted_price = %s WHERE ticker = 'NORT'",
            (float(result.knockout_price) * 1.01,),
        )
    knocked = await sweep_knockouts(conn, 5)
    assert knocked == 1

    assert await list_open_shorts(conn, 6) == []
    main = await get_user_account_id(conn, 6)
    # Nothing returned: user is down collateral + fee.
    assert await get_balance(conn, main) == (
        STARTING_GRANT - result.collateral_minor - result.fee_minor
    )


async def test_apply_tick_runs_knockout_sweep(conn: AsyncConnection) -> None:
    """End-to-end: a real tick knocks out a short whose barrier is breached."""
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE instruments SET short_knockout_pct = 0.05 WHERE ticker = 'NORT'"
        )
    result = await _open(conn, user_id=7)  # KO at ~entry * 1.05

    # A 10% price move (under the 15% circuit clamp) breaches the 5% barrier.
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE instruments SET base_price = base_price * 1.10, "
            "fundamental_value = fundamental_value * 1.10 WHERE ticker = 'NORT'"
        )
    await apply_tick(conn, SEED)

    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT status, payout_minor FROM bounded_shorts WHERE id = %s",
            (result.short_id,),
        )
        row = await cur.fetchone()
    assert row is not None
    assert row[0] == "KNOCKED_OUT" and row[1] == 0


async def test_cover_rejects_foreign_or_closed_short(conn: AsyncConnection) -> None:
    result = await _open(conn, user_id=8)
    with pytest.raises(ShortNotFoundError):
        await cover_bounded_short(conn, user_id=999, short_id=result.short_id)
    await cover_bounded_short(conn, user_id=8, short_id=result.short_id)
    with pytest.raises(ShortNotFoundError):
        await cover_bounded_short(conn, user_id=8, short_id=result.short_id)


async def test_league_short_uses_league_account(conn: AsyncConnection) -> None:
    season_id = await create_season(
        conn,
        name="Short Season",
        start_tick=0,
        end_tick=10_000,
        entry_fee_minor=0,
        stake_minor=50_000,
    )
    await on_tick(conn, 0)
    await bootstrap_user(conn, 9)
    await join_season(conn, 9, season_id)

    await open_bounded_short(conn, user_id=9, ticker="NORT", quantity=4, season_id=season_id)

    # League equity dropped by the fee + impact cost (short marked to market).
    assert await league_equity_minor(conn, season_id, 9) < 50_000
    # Main account untouched (no entry fee in this test season).
    main = await get_user_account_id(conn, 9)
    assert await get_balance(conn, main) == STARTING_GRANT
    # Scoped: shows in league shorts list, not main.
    assert len(await list_open_shorts(conn, 9, season_id)) == 1
    assert await list_open_shorts(conn, 9) == []


async def test_market_maker_is_counterparty(conn: AsyncConnection) -> None:
    """The MM absorbs the short's P&L: collateral in, payout out."""
    mm = await get_system_account_id(conn, "MARKET_MAKER")
    mm_before = await get_balance(conn, mm)
    result = await _open(conn, user_id=10)
    assert await get_balance(conn, mm) == mm_before + result.collateral_minor
