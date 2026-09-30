"""Duels: escrow, season lifecycle, settlement, ties, forfeits, scope pins."""

import pytest
from psycopg import AsyncConnection

from stockbot.accounts.service import STARTING_GRANT, bootstrap_user
from stockbot.duels import service as duels
from stockbot.duels.errors import DuelLimitError, DuelStateError, DuelTargetError
from stockbot.ledger.service import (
    get_balance,
    get_system_account_id,
    get_user_account_id,
)
from stockbot.seasons import service as seasons
from stockbot.trading.service import execute_trade

STAKE = 2_000  # $20 -- inside the 100..50_000 bounds
WINDOW = 1_440


async def _accepted_duel(
    conn: AsyncConnection, a: int = 3101, b: int = 3102, stake: int = STAKE
) -> tuple[duels.Duel, int]:
    """Offer + accept; returns (duel, season_id)."""
    await bootstrap_user(conn, a)
    await bootstrap_user(conn, b)
    duel_id = await duels.create_offer(conn, a, b, stake, WINDOW, tick_index=0)
    duel = await duels.accept(conn, duel_id, b, tick_index=0)
    assert duel.season_id is not None
    return duel, duel.season_id


async def test_offer_escrows_challenger_stake(conn: AsyncConnection) -> None:
    await bootstrap_user(conn, 3001)
    await bootstrap_user(conn, 3002)
    escrow_id = await get_system_account_id(conn, "GAME_ESCROW")
    before = await get_balance(conn, escrow_id)

    duel_id = await duels.create_offer(conn, 3001, 3002, STAKE, WINDOW, tick_index=0)

    account = await get_user_account_id(conn, 3001)
    assert await get_balance(conn, account) == STARTING_GRANT - STAKE
    assert await get_balance(conn, escrow_id) == before + STAKE
    duel = await duels.get_duel(conn, duel_id)
    assert duel is not None and duel.status == "OPEN"


async def test_self_challenge_rejected(conn: AsyncConnection) -> None:
    await bootstrap_user(conn, 3003)
    with pytest.raises(DuelTargetError):
        await duels.create_offer(conn, 3003, 3003, STAKE, WINDOW, tick_index=0)


async def test_offer_to_unstarted_user_rejected(conn: AsyncConnection) -> None:
    await bootstrap_user(conn, 3004)
    with pytest.raises(DuelTargetError):
        await duels.create_offer(conn, 3004, 999_999, STAKE, WINDOW, tick_index=0)


async def test_stake_bounds_enforced(conn: AsyncConnection) -> None:
    await bootstrap_user(conn, 3005)
    await bootstrap_user(conn, 3006)
    with pytest.raises(DuelTargetError):
        await duels.create_offer(conn, 3005, 3006, 1, WINDOW, tick_index=0)
    with pytest.raises(DuelTargetError):
        await duels.create_offer(conn, 3005, 3006, 999_999_999, WINDOW, tick_index=0)


async def test_active_cap(conn: AsyncConnection) -> None:
    await bootstrap_user(conn, 3010)
    for i in range(3):
        await bootstrap_user(conn, 3011 + i)
        await duels.create_offer(conn, 3010, 3011 + i, STAKE, WINDOW, tick_index=0)
    await bootstrap_user(conn, 3015)
    with pytest.raises(DuelLimitError):
        await duels.create_offer(conn, 3010, 3015, STAKE, WINDOW, tick_index=0)


async def test_accept_escrows_both_and_creates_private_season(
    conn: AsyncConnection,
) -> None:
    duel, season_id = await _accepted_duel(conn)

    escrow_id = await get_system_account_id(conn, "GAME_ESCROW")
    assert await get_balance(conn, escrow_id) == 2 * STAKE

    season = await seasons.get_season(conn, season_id)
    assert season is not None
    assert season.duel_id == duel.id
    assert season.status == "ACTIVE"
    assert season.entry_fee_minor == 0

    # Both players entered with equal league stakes and pinned scopes.
    for uid in (duel.challenger_id, duel.opponent_id):
        entry = await seasons.get_active_entry(conn, uid, season_id)
        assert entry is not None
        assert await seasons.resolve_trade_entry(conn, uid) == entry
        assert await seasons.pinned_scope(conn, uid) == season_id

    # The duel season is invisible to public league surfaces.
    public = await seasons.get_open_season(conn)
    assert public is None or public.id != season_id


async def test_decline_refunds_challenger(conn: AsyncConnection) -> None:
    await bootstrap_user(conn, 3020)
    await bootstrap_user(conn, 3021)
    duel_id = await duels.create_offer(conn, 3020, 3021, STAKE, WINDOW, tick_index=0)

    await duels.decline(conn, duel_id, 3021)

    assert await get_balance(conn, await get_user_account_id(conn, 3020)) == STARTING_GRANT
    duel = await duels.get_duel(conn, duel_id)
    assert duel is not None and duel.status == "DECLINED"


async def test_cancel_refunds_challenger(conn: AsyncConnection) -> None:
    await bootstrap_user(conn, 3022)
    await bootstrap_user(conn, 3023)
    duel_id = await duels.create_offer(conn, 3022, 3023, STAKE, WINDOW, tick_index=0)

    await duels.cancel(conn, duel_id, 3022)

    assert await get_balance(conn, await get_user_account_id(conn, 3022)) == STARTING_GRANT
    duel = await duels.get_duel(conn, duel_id)
    assert duel is not None and duel.status == "CANCELLED"


async def test_opponent_cannot_cancel(conn: AsyncConnection) -> None:
    await bootstrap_user(conn, 3024)
    await bootstrap_user(conn, 3025)
    duel_id = await duels.create_offer(conn, 3024, 3025, STAKE, WINDOW, tick_index=0)
    with pytest.raises(DuelStateError):
        await duels.cancel(conn, duel_id, 3025)


async def test_offer_expiry_refunds_via_sweep(conn: AsyncConnection) -> None:
    await bootstrap_user(conn, 3026)
    await bootstrap_user(conn, 3027)
    duel_id = await duels.create_offer(conn, 3026, 3027, STAKE, WINDOW, tick_index=0)

    expired = await duels.on_tick(conn, 1_441)  # past the 1440-tick ttl

    assert expired == 1
    assert await get_balance(conn, await get_user_account_id(conn, 3026)) == STARTING_GRANT
    duel = await duels.get_duel(conn, duel_id)
    assert duel is not None and duel.status == "EXPIRED"


async def test_accept_after_expiry_refunds_and_rejects(conn: AsyncConnection) -> None:
    await bootstrap_user(conn, 3028)
    await bootstrap_user(conn, 3029)
    duel_id = await duels.create_offer(conn, 3028, 3029, STAKE, WINDOW, tick_index=0)

    with pytest.raises(DuelStateError):
        await duels.accept(conn, duel_id, 3029, tick_index=2_000)

    assert await get_balance(conn, await get_user_account_id(conn, 3028)) == STARTING_GRANT
    duel = await duels.get_duel(conn, duel_id)
    assert duel is not None and duel.status == "EXPIRED"


async def test_settlement_winner_takes_pot_minus_rake(conn: AsyncConnection) -> None:
    duel, season_id = await _accepted_duel(conn, 3030, 3031)
    # The loser trades: fees + impact drag their equity below the stake.
    await execute_trade(
        conn, user_id=duel.challenger_id, ticker="NORT", side="BUY",
        quantity=1, season_id=season_id,
    )
    challenger_before = await get_balance(
        conn, await get_user_account_id(conn, duel.challenger_id)
    )
    opponent_before = await get_balance(
        conn, await get_user_account_id(conn, duel.opponent_id)
    )

    await seasons.close_season(conn, season_id)

    duel = await duels.get_duel(conn, duel.id)
    assert duel is not None and duel.status == "SETTLED"
    assert duel.winner_id == duel.opponent_id
    pot = 2 * STAKE
    rake = int(pot * 0.05)
    assert duel.payout_minor == pot - rake
    assert await get_balance(
        conn, await get_user_account_id(conn, duel.opponent_id)
    ) == opponent_before + pot - rake
    assert await get_balance(
        conn, await get_user_account_id(conn, duel.challenger_id)
    ) == challenger_before

    # Escrow is empty again; the rake went to SINK.
    escrow_id = await get_system_account_id(conn, "GAME_ESCROW")
    assert await get_balance(conn, escrow_id) == 0
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT COALESCE(SUM(amount), 0) FROM ledger_entries "
            "WHERE account_id = (SELECT id FROM accounts WHERE system_name = 'SINK') "
            "AND reason = 'DUEL_RAKE'"
        )
        (sink_rake,) = await cur.fetchone()
    assert sink_rake == rake


async def test_equity_tie_refunds_minus_rake(conn: AsyncConnection) -> None:
    duel, season_id = await _accepted_duel(conn, 3040, 3041)

    await seasons.close_season(conn, season_id)

    duel = await duels.get_duel(conn, duel.id)
    assert duel is not None and duel.status == "SETTLED"
    assert duel.winner_id is None
    assert duel.payout_minor is None
    rake_each = int(STAKE * 0.05)
    for uid in (duel.challenger_id, duel.opponent_id):
        assert await get_balance(
            conn, await get_user_account_id(conn, uid)
        ) == STARTING_GRANT - STAKE + (STAKE - rake_each)
    assert await get_balance(conn, await get_system_account_id(conn, "GAME_ESCROW")) == 0


async def test_forfeit_other_player_wins(conn: AsyncConnection) -> None:
    duel, season_id = await _accepted_duel(conn, 3050, 3051)
    # The challenger trades, which would WIN on equity-vs-losses... no:
    # forfeit overrides equity entirely, so the trader still loses.
    await execute_trade(
        conn, user_id=duel.challenger_id, ticker="NORT", side="BUY",
        quantity=1, season_id=season_id,
    )

    result = await duels.forfeit(conn, duel.challenger_id, tick_index=10)

    assert result.status == "SETTLED"
    assert result.winner_id == duel.opponent_id
    assert result.forfeited_by == duel.challenger_id
    pot = 2 * STAKE
    rake = int(pot * 0.05)
    assert result.payout_minor == pot - rake


async def test_close_duel_season_twice_idempotent(conn: AsyncConnection) -> None:
    duel, season_id = await _accepted_duel(conn, 3060, 3061)
    await seasons.close_season(conn, season_id)
    await seasons.close_season(conn, season_id)  # no double payout

    duel = await duels.get_duel(conn, duel.id)
    assert duel is not None and duel.status == "SETTLED"
    assert await get_balance(conn, await get_system_account_id(conn, "GAME_ESCROW")) == 0


async def test_scope_pin_survives_newer_division_season(conn: AsyncConnection) -> None:
    """A division week starting mid-duel must not steal league-flag routing."""
    duel, duel_season = await _accepted_duel(conn, 3070, 3071)

    # A newer ACTIVE division season the challenger is also entered in.
    async with conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO seasons (name, status, start_tick, end_tick,
                                 entry_fee_minor, stake_minor, division_tier)
            VALUES ('Bronze Division', 'ACTIVE', 5, 10_085, 0, 100_000, 3)
            RETURNING id
            """
        )
        (div_season,) = await cur.fetchone()
    await seasons.join_season(conn, duel.challenger_id, div_season)

    # Newer season exists -- but the pin still routes to the duel.
    resolved = await seasons.resolve_trade_entry(conn, duel.challenger_id)
    assert resolved is not None and resolved[0] == duel_season

    # Unpinned opponent... also pinned (accept pins both).
    resolved_opp = await seasons.resolve_trade_entry(conn, duel.opponent_id)
    assert resolved_opp is not None and resolved_opp[0] == duel_season

    # When the duel closes, the pin dies and recency takes over.
    await seasons.close_season(conn, duel_season)
    assert await seasons.pinned_scope(conn, duel.challenger_id) is None
    resolved = await seasons.resolve_trade_entry(conn, duel.challenger_id)
    assert resolved is not None and resolved[0] == div_season


async def test_stale_pin_falls_back_to_recency(conn: AsyncConnection) -> None:
    """A pin pointing at a season with no active entry resolves recent-first."""
    await bootstrap_user(conn, 3080)
    # Hand-write a stale pin to a season the user isn't entered in.
    async with conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO seasons (name, status, start_tick, end_tick,
                                 entry_fee_minor, stake_minor)
            VALUES ('Ghost', 'ACTIVE', 0, 10_000, 0, 100_000)
            RETURNING id
            """
        )
        (ghost,) = await cur.fetchone()
        await cur.execute(
            "INSERT INTO trade_scope (user_id, season_id) VALUES (%s, %s)",
            (3080, ghost),
        )
        await cur.execute(
            """
            INSERT INTO seasons (name, status, start_tick, end_tick,
                                 entry_fee_minor, stake_minor)
            VALUES ('Real', 'ACTIVE', 0, 10_000, 100, 50_000)
            RETURNING id
            """
        )
        (real,) = await cur.fetchone()
    await seasons.join_season(conn, 3080, real)

    resolved = await seasons.resolve_trade_entry(conn, 3080)
    assert resolved is not None and resolved[0] == real


async def test_duel_league_quarantined_from_main(conn: AsyncConnection) -> None:
    """Duel trades live in the duel season, invisible to the main book."""
    duel, season_id = await _accepted_duel(conn, 3090, 3091)
    await execute_trade(
        conn, user_id=duel.challenger_id, ticker="NORT", side="BUY",
        quantity=2, season_id=season_id,
    )
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT COUNT(*) FROM positions WHERE user_id = %s AND season_id IS NULL",
            (duel.challenger_id,),
        )
        (main_count,) = await cur.fetchone()
    assert main_count == 0
