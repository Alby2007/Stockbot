"""Seasons + League: entry, account isolation, snapshots, scoring, close."""

import pytest
from psycopg import AsyncConnection

from stockbot.accounts.service import STARTING_GRANT, bootstrap_user
from stockbot.ledger.service import get_balance, get_user_account_id
from stockbot.seasons.errors import AlreadyEnteredError, SeasonNotOpenError
from stockbot.seasons.service import (
    MIN_ACTIVE_DAYS,
    MIN_TRADES,
    close_season,
    create_season,
    get_active_entry,
    join_season,
    league_equity_minor,
    league_score,
    on_tick,
    standings,
)
from stockbot.trading.errors import NotInLeagueError, TradingError
from stockbot.trading.service import execute_trade

ENTRY_FEE = 500
STAKE = 50_000


async def _open_season(conn: AsyncConnection, end_tick: int = 10_000) -> int:
    """Create a season and activate it via the tick hook."""
    season_id = await create_season(
        conn,
        name="Test Season",
        start_tick=0,
        end_tick=end_tick,
        entry_fee_minor=ENTRY_FEE,
        stake_minor=STAKE,
    )
    await on_tick(conn, 0)
    return season_id


async def test_join_charges_fee_and_grants_equal_stake(conn: AsyncConnection) -> None:
    season_id = await _open_season(conn)
    await bootstrap_user(conn, 1001)

    await join_season(conn, 1001, season_id)

    main_account = await get_user_account_id(conn, 1001)
    assert await get_balance(conn, main_account) == STARTING_GRANT - ENTRY_FEE

    entry = await get_active_entry(conn, 1001, season_id)
    assert entry is not None
    assert await get_balance(conn, entry[1]) == STAKE


async def test_join_rejects_second_entry(conn: AsyncConnection) -> None:
    season_id = await _open_season(conn)
    await bootstrap_user(conn, 1002)
    await join_season(conn, 1002, season_id)
    with pytest.raises(AlreadyEnteredError):
        await join_season(conn, 1002, season_id)


async def test_join_rejects_closed_season(conn: AsyncConnection) -> None:
    season_id = await _open_season(conn, end_tick=5)
    await on_tick(conn, 10)  # past end_tick -> closes
    await bootstrap_user(conn, 1003)
    with pytest.raises(SeasonNotOpenError):
        await join_season(conn, 1003, season_id)


async def test_league_trade_is_isolated_from_main_portfolio(conn: AsyncConnection) -> None:
    season_id = await _open_season(conn)
    await bootstrap_user(conn, 1004)
    league_account = await join_season(conn, 1004, season_id)
    main_before = await get_balance(conn, await get_user_account_id(conn, 1004))

    result = await execute_trade(
        conn, user_id=1004, ticker="NORT", side="BUY", quantity=2, season_id=season_id
    )
    assert result.quantity == 2

    # Cash came out of the league account, not the main one.
    assert await get_balance(conn, league_account) == STAKE - result.notional_minor - (
        result.fee_minor
    )
    assert await get_balance(conn, await get_user_account_id(conn, 1004)) == main_before

    # The position is season-scoped and invisible to the main portfolio.
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT COUNT(*) FROM positions WHERE user_id = %s AND season_id = %s",
            (1004, season_id),
        )
        assert (await cur.fetchone())[0] == 1  # type: ignore[index]
        await cur.execute(
            "SELECT COUNT(*) FROM positions WHERE user_id = %s AND season_id IS NULL",
            (1004,),
        )
        assert (await cur.fetchone())[0] == 0  # type: ignore[index]


async def test_league_trade_without_entry_rejected(conn: AsyncConnection) -> None:
    season_id = await _open_season(conn)
    await bootstrap_user(conn, 1005)
    with pytest.raises(NotInLeagueError):
        await execute_trade(
            conn, user_id=1005, ticker="NORT", side="BUY", quantity=1, season_id=season_id
        )


async def test_season_close_sweeps_league_balances(conn: AsyncConnection) -> None:
    season_id = await _open_season(conn, end_tick=5)
    await bootstrap_user(conn, 1006)
    league_account = await join_season(conn, 1006, season_id)

    await on_tick(conn, 10)  # past end_tick -> close

    assert await get_balance(conn, league_account) == 0
    async with conn.cursor() as cur:
        await cur.execute("SELECT status FROM seasons WHERE id = %s", (season_id,))
        assert (await cur.fetchone())[0] == "CLOSED"  # type: ignore[index]


def test_league_score_floors() -> None:
    # Too few snapshots -> not qualified.
    assert league_score([100_000] * (MIN_ACTIVE_DAYS)) is None
    # Flat series with enough points -> 0 (never trading earns nothing).
    assert league_score([100_000] * (MIN_ACTIVE_DAYS + 1)) == 0.0
    # Steadily rising series scores positive; steadily falling, negative.
    rising = [100_000 * (1.001**i) for i in range(MIN_ACTIVE_DAYS + 5)]
    assert league_score([int(e) for e in rising]) is not None
    assert league_score([int(e) for e in rising]) > 0  # type: ignore[operator]


async def test_close_ranks_qualifying_entrants_and_pays_prizes(conn: AsyncConnection) -> None:
    season_id = await _open_season(conn, end_tick=MIN_ACTIVE_DAYS + 5)
    await bootstrap_user(conn, 2001)
    await bootstrap_user(conn, 2002)
    await join_season(conn, 2001, season_id)
    await join_season(conn, 2002, season_id)

    # User 2001 trades enough to qualify (each trade also moves equity via
    # fees), user 2002 never trades -> disqualified by MIN_TRADES.
    for _ in range(MIN_TRADES):
        await execute_trade(
            conn, user_id=2001, ticker="NORT", side="BUY", quantity=1, season_id=season_id
        )
        await execute_trade(
            conn, user_id=2001, ticker="NORT", side="SELL", quantity=1, season_id=season_id
        )

    # Day-boundary snapshots for both entrants (interval=1 -> every tick).
    for tick in range(1, MIN_ACTIVE_DAYS + 3):
        await on_tick(conn, tick, snapshot_interval_ticks=1)

    main_2001_before = await get_balance(conn, await get_user_account_id(conn, 2001))
    await close_season(conn, season_id)

    board = await standings(conn, season_id)
    by_user = {s.user_id: s for s in board}
    assert by_user[2001].final_rank == 1
    assert by_user[2001].final_score is not None
    assert by_user[2002].final_rank is None
    assert by_user[2002].final_score is None

    # Prize pool = collected entry fees; rank 1 takes the 50% share.
    expected_prize = int(ENTRY_FEE * 2 * 0.5)
    assert by_user[2001].prize_minor == expected_prize
    assert by_user[2002].prize_minor == 0
    main_2001_after = await get_balance(conn, await get_user_account_id(conn, 2001))
    assert main_2001_after == main_2001_before + expected_prize

    # Trophy entitlement recorded for the winner.
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT item_key FROM entitlements WHERE user_id = %s AND item_key LIKE 'trophy_%%'",
            (2001,),
        )
        assert await cur.fetchone() is not None


async def test_league_positions_scored_as_equity(conn: AsyncConnection) -> None:
    """Equity includes mark-to-market league positions, not just cash."""
    season_id = await _open_season(conn)
    await bootstrap_user(conn, 2003)
    await join_season(conn, 2003, season_id)

    cash_equity = await league_equity_minor(conn, season_id, 2003)
    assert cash_equity == STAKE

    result = await execute_trade(
        conn, user_id=2003, ticker="NORT", side="BUY", quantity=3, season_id=season_id
    )
    equity_after = await league_equity_minor(conn, season_id, 2003)
    # Cash fell by notional+fee; position worth ~notional -> equity drops by ~fee+impact.
    assert equity_after < cash_equity
    assert cash_equity - equity_after >= result.fee_minor


async def test_main_trade_unaffected_by_league_entry(conn: AsyncConnection) -> None:
    season_id = await _open_season(conn)
    await bootstrap_user(conn, 2004)
    await join_season(conn, 2004, season_id)

    # A main-portfolio trade still uses the main account and NULL scope.
    await execute_trade(conn, user_id=2004, ticker="NORT", side="BUY", quantity=1)
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT COUNT(*) FROM positions WHERE user_id = %s AND season_id IS NULL",
            (2004,),
        )
        assert (await cur.fetchone())[0] == 1  # type: ignore[index]


async def test_trading_error_subclassing_keeps_bot_handlers_simple(conn: AsyncConnection) -> None:
    """Season errors subclass TradingError so /buy league:true reuses the
    same error path as ordinary trades."""
    assert issubclass(NotInLeagueError, TradingError)


async def test_join_race_maps_unique_violation_to_already_entered(
    conn: AsyncConnection,
) -> None:
    """A league account row with no season_entries row is what a lost join
    race leaves behind; the second join must surface AlreadyEnteredError,
    not a raw UniqueViolation."""
    season_id = await _open_season(conn)
    await bootstrap_user(conn, 1008)
    async with conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO accounts (kind, user_id, season_id, balance) "
            "VALUES ('LEAGUE', %s, %s, 0)",
            (1008, season_id),
        )
    with pytest.raises(AlreadyEnteredError):
        await join_season(conn, 1008, season_id)


async def test_close_season_twice_is_idempotent(conn: AsyncConnection) -> None:
    """The second close must no-op: no re-sweep, no double prize transfers."""
    season_id = await _open_season(conn, end_tick=5)
    await bootstrap_user(conn, 2005)
    league_account = await join_season(conn, 2005, season_id)

    await close_season(conn, season_id)
    await close_season(conn, season_id)  # must not raise or re-run

    assert await get_balance(conn, league_account) == 0
    async with conn.cursor() as cur:
        await cur.execute("SELECT status FROM seasons WHERE id = %s", (season_id,))
        row = await cur.fetchone()
        assert row is not None and row[0] == "CLOSED"
        await cur.execute(
            "SELECT COUNT(*) FROM ledger_entries "
            "WHERE account_id = %s AND reason = 'LEAGUE_RETURN'",
            (league_account,),
        )
        row = await cur.fetchone()
        assert row is not None and row[0] == 1
