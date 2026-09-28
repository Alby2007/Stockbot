"""Regional markets: venues in `markets`, per-instrument session phase,
staggered candles, and single-venue byte-identity (R1+R2)."""

from __future__ import annotations

import pytest
from psycopg import AsyncConnection

from stockbot.accounts.service import bootstrap_user
from stockbot.ledger.service import get_system_account_id, post_transfer
from stockbot.market import engine
from stockbot.market.tick import apply_tick
from stockbot.trading.errors import MarketClosedError
from stockbot.trading.service import execute_trade

SEED = "markets-test-seed"


async def _set_us_session(
    conn: AsyncConnection, open_t: int, closed_t: int
) -> None:
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE markets SET open_ticks = %s, closed_ticks = %s, "
            "offset_ticks = 0",
            (open_t, closed_t),
        )


async def _add_asia_venue(
    conn: AsyncConnection,
    open_t: int = 3,
    closed_t: int = 2,
    offset: int = 1,
) -> int:
    """A second venue on a staggered clock; returns the new market id."""
    async with conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO markets
                (code, name, open_ticks, closed_ticks, offset_ticks,
                 tick_size, auction_ticks, overnight_var_ticks)
            VALUES ('AS', 'Asia Exchange', %s, %s, %s, 0.01, 1, 60)
            RETURNING id
            """,
            (open_t, closed_t, offset),
        )
        (mid,) = await cur.fetchone()
    return int(mid)


async def _move_ticker_to_market(
    conn: AsyncConnection, ticker: str, market_id: int
) -> int:
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE instruments SET market_id = %s WHERE ticker = %s "
            "RETURNING id",
            (market_id, ticker),
        )
        row = await cur.fetchone()
    assert row is not None
    return int(row[0])


async def _candle_states(
    conn: AsyncConnection, instrument_id: int
) -> dict[int, str]:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT tick_index, session_state FROM candles "
            "WHERE instrument_id = %s",
            (instrument_id,),
        )
        return {int(t): str(s) for t, s in await cur.fetchall()}


async def _first_stock(conn: AsyncConnection) -> tuple[int, str]:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT id, ticker FROM instruments "
            "WHERE is_active AND kind != 'INDEX' ORDER BY id LIMIT 1"
        )
        row = await cur.fetchone()
    assert row is not None
    return int(row[0]), str(row[1])


async def test_single_market_factor_replay_is_byte_identical(
    conn: AsyncConnection,
) -> None:
    """R2's unified pipeline must store the SAME factor draws the old
    global-phase implementation did: OPEN ticks draw once with (1,1), the
    reopen tick draws with (closed_ticks, overnight_var_ticks), and a
    closed tick stores 0/'{}'. Recomputing the draws per tick pins the
    byte-level contract."""
    await _set_us_session(conn, 4, 3)
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT DISTINCT s.key FROM instruments i "
            "JOIN sectors s ON s.id = i.sector_id WHERE i.is_active"
        )
        sector_keys = sorted(r[0] for r in await cur.fetchall())

    for _ in range(10):  # covers 2 open + 3 closed + reopen + more
        await apply_tick(conn, SEED)

    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT tick_index, market_factor, sector_factors, session_state "
            "FROM market_ticks ORDER BY tick_index"
        )
        rows = await cur.fetchall()

    for tick_index, stored_mf, stored_sf, phase in rows:
        # US venue: open=4, closed=3, offset=0.
        expected_phase = engine.session_phase(tick_index, 4, 3, 0)
        assert phase == expected_phase
        if phase == "CLOSED":
            assert float(stored_mf) == 0.0
            assert stored_sf == {}
            continue
        # Reopen = cycle_pos 0 with a real close behind it.
        pos = tick_index % 7
        dt, vdt = (3.0, 60.0) if pos == 0 and tick_index > 0 else (1.0, 1.0)
        rng = engine.rng_for_tick(SEED, int(tick_index))
        exp_mf = engine.draw_market_factor(rng, dt=dt, var_dt=vdt)
        exp_sf = engine.draw_sector_factors(
            rng, sector_keys, dt=dt, var_dt=vdt
        )
        assert float(stored_mf) == pytest.approx(exp_mf, abs=1e-9)
        for key in sector_keys:
            assert float(stored_sf[key]) == pytest.approx(
                exp_sf[key], abs=1e-9
            )


async def test_staggered_venue_freezes_and_reopens_on_own_clock(
    conn: AsyncConnection,
) -> None:
    """US open=4/closed=3/offset=0; AS open=3/closed=2/offset=1.
    AS ticks 3-4 are closed while US is still open (US closes at 4): the
    AS instrument writes flat CLOSED candles with a frozen mark while US
    names keep stepping, then reopens on its own cycle."""
    await _set_us_session(conn, 4, 3)
    as_mid = await _add_asia_venue(conn, open_t=3, closed_t=2, offset=1)
    us_iid, _us_ticker = await _first_stock(conn)
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT id, ticker FROM instruments WHERE is_active "
            "AND kind != 'INDEX' AND id != %s ORDER BY id LIMIT 1",
            (us_iid,),
        )
        row = await cur.fetchone()
    assert row is not None
    as_iid, _as_ticker = int(row[0]), str(row[1])
    await _move_ticker_to_market(conn, _as_ticker, as_mid)

    for _ in range(6):
        await apply_tick(conn, SEED)

    us_states = await _candle_states(conn, us_iid)
    as_states = await _candle_states(conn, as_iid)

    # AS cycle: (tick-1) % 5 < 3 open -> ticks 1-3 open, 4-5 closed.
    # Wait: offset=1 -> open at pos<3 -> ticks 1,2,3 open; 4,5 closed.
    assert as_states[4] == "CLOSED"
    assert as_states[5] == "CLOSED"
    # US at ticks 4..? US open=4 -> ticks 0-3 open, 4-6 closed.
    assert us_states[4] == "CLOSED"
    assert us_states[0] == "OPEN"

    # The AS mark froze across its close: flat candles at the last open mark.
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT open, close, volume FROM candles "
            "WHERE instrument_id = %s AND tick_index IN (4, 5) "
            "ORDER BY tick_index",
            (as_iid,),
        )
        rows = await cur.fetchall()
        await cur.execute(
            "SELECT close FROM candles WHERE instrument_id = %s "
            "AND tick_index = 3",
            (as_iid,),
        )
        (last_open_close,) = await cur.fetchone()
    for o, c, v in rows:
        assert float(o) == float(c) == float(last_open_close)
        assert int(v) == 0


async def test_market_open_gate_is_venue_scoped(conn: AsyncConnection) -> None:
    """With AS closed and US open, a US trade fills while an AS trade
    raises MarketClosedError naming the venue."""
    await _set_us_session(conn, 4, 3)
    as_mid = await _add_asia_venue(conn, open_t=3, closed_t=2, offset=1)
    us_iid, us_ticker = await _first_stock(conn)
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT id, ticker FROM instruments WHERE is_active "
            "AND kind != 'INDEX' AND id != %s ORDER BY id LIMIT 1",
            (us_iid,),
        )
        row = await cur.fetchone()
    assert row is not None
    as_ticker = str(row[1])
    await _move_ticker_to_market(conn, as_ticker, as_mid)

    account = (await bootstrap_user(conn, 9101)).account_id
    await post_transfer(
        conn,
        from_account_id=await get_system_account_id(conn, "FAUCET"),
        to_account_id=account,
        amount=10_000_000,
        reason="TEST_TOPUP",
    )

    # Tick until AS is closed but US open: AS closed at ticks 4,5; US
    # closed at 4,5,6 -- overlap is full-close. AS also closed at 9,10
    # while US open (9,10,11? US closed 4-6; open 7-10, closed 11-13).
    # tick 9: AS (9-1)%5=3 -> closed; US 9%7=2 -> open.
    last = 0
    for _ in range(20):
        last = await apply_tick(conn, SEED)
        if last == 9:
            break
    assert last == 9

    await execute_trade(conn, user_id=9101, ticker=us_ticker, side="BUY", quantity=1)
    with pytest.raises(MarketClosedError):
        await execute_trade(
            conn, user_id=9101, ticker=as_ticker, side="BUY", quantity=1
        )


async def test_market_ticks_state_is_any_venue_open(
    conn: AsyncConnection,
) -> None:
    """The tick summary row is OPEN iff any venue is open -- a mixed tick
    is OPEN even though one venue's candles are CLOSED."""
    await _set_us_session(conn, 4, 3)
    await _add_asia_venue(conn, open_t=3, closed_t=2, offset=2)
    # AS: (tick-2) % 5 < 3 -> open ticks 2,3,4; closed 5,6.
    # US: tick % 7 < 4 -> open 0-3; closed 4-6.
    # tick 4: US closed, AS open -> summary OPEN.
    for _ in range(5):
        await apply_tick(conn, SEED)
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT session_state FROM market_ticks WHERE tick_index = 4"
        )
        (state,) = await cur.fetchone()
    assert state == "OPEN"


async def test_reopen_gap_uses_venue_horizon(conn: AsyncConnection) -> None:
    """The reopening venue's instruments step with dt=closed_ticks and
    var_dt=overnight_var_ticks; a mid-session venue keeps var_dt=1 in the
    same tick (the shared draw is rescaled per instrument)."""
    await _set_us_session(conn, 4, 3)
    # US: cycle 7 -> reopen at tick 7 (pos 0). AS: offset 5 -> reopen
    # ticks (pos 0) at tick 5 + 5k... offset 5: (tick-5)%7==0 -> 5, 12...
    as_mid = await _add_asia_venue(conn, open_t=4, closed_t=3, offset=5)
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT id, ticker FROM instruments WHERE is_active "
            "AND kind != 'INDEX' ORDER BY id LIMIT 2"
        )
        rows = await cur.fetchall()
    as_iid = int(rows[1][0])
    await _move_ticker_to_market(conn, str(rows[1][1]), as_mid)

    for _ in range(8):
        await apply_tick(conn, SEED)

    # Tick 7: US reopens (pos 0). Stored market_factor is the union draw
    # -- max reopening gap. US vdt=60 (seeded overnight_var_ticks).
    rng = engine.rng_for_tick(SEED, 7)
    exp_mf = engine.draw_market_factor(rng, dt=3.0, var_dt=60.0)
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT market_factor FROM market_ticks WHERE tick_index = 7"
        )
        (mf,) = await cur.fetchone()
        # And the AS instrument's candle at its own reopen (tick 5) is an
        # OPEN candle -- the venue reopened on its own clock.
        await cur.execute(
            "SELECT session_state FROM candles "
            "WHERE instrument_id = %s AND tick_index = 5",
            (as_iid,),
        )
        (as_state5,) = await cur.fetchone()
    assert float(mf) == pytest.approx(exp_mf, abs=1e-9)
    assert as_state5 == "OPEN"
    # Tick 5: AS reopened (dt=3, vdt=60) while US was closed -- the stored
    # factor is exactly AS's gap draw.
    rng5 = engine.rng_for_tick(SEED, 5)
    exp5 = engine.draw_market_factor(rng5, dt=3.0, var_dt=60.0)
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT market_factor FROM market_ticks WHERE tick_index = 5"
        )
        (mf5,) = await cur.fetchone()
    assert float(mf5) == pytest.approx(exp5, abs=1e-9)
