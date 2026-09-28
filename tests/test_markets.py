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
    """Reshape the seeded AS venue (0054) to a small staggered clock for
    tests; returns its market id."""
    async with conn.cursor() as cur:
        await cur.execute(
            """
            UPDATE markets
            SET open_ticks = %s, closed_ticks = %s, offset_ticks = %s
            WHERE code = 'AS'
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


# ---------------------------------------------------------------------------
# R3: region-gated sweeps
# ---------------------------------------------------------------------------


async def _us_market_id(conn: AsyncConnection) -> int:
    async with conn.cursor() as cur:
        await cur.execute("SELECT id FROM markets ORDER BY id LIMIT 1")
        (mid,) = await cur.fetchone()
    return int(mid)


async def _second_stock(conn: AsyncConnection) -> tuple[int, str]:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT id, ticker FROM instruments WHERE is_active "
            "AND kind != 'INDEX' ORDER BY id OFFSET 1 LIMIT 1"
        )
        row = await cur.fetchone()
    assert row is not None
    return int(row[0]), str(row[1])


async def _iid_of(conn: AsyncConnection, ticker: str) -> int:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT id FROM instruments WHERE ticker = %s", (ticker,)
        )
        (iid,) = await cur.fetchone()
    return int(iid)


async def _fund_user(
    conn: AsyncConnection, user_id: int, amount: int = 10_000_000
) -> int:
    account_id = (await bootstrap_user(conn, user_id)).account_id
    await post_transfer(
        conn,
        from_account_id=await get_system_account_id(conn, "FAUCET"),
        to_account_id=account_id,
        amount=amount,
        reason="TEST_TOPUP",
    )
    return account_id


async def test_closed_venue_order_does_not_match(
    conn: AsyncConnection,
) -> None:
    """R3: a marketable resting order on a closed venue stays unfilled
    while other venues trade; it fills once its venue is passed open."""
    from decimal import Decimal

    from stockbot.orders.service import match_orders, place_order

    us_mid = await _us_market_id(conn)
    as_mid = await _add_asia_venue(conn)
    _iid, as_ticker = await _second_stock(conn)
    await _move_ticker_to_market(conn, as_ticker, as_mid)
    await _fund_user(conn, 9102)

    placed = await place_order(
        conn,
        user_id=9102,
        ticker=as_ticker,
        side="BUY",
        quantity=1,
        limit_price=Decimal("1000000"),
    )
    # No ticks applied yet -> every venue counts open for placement.
    assert await match_orders(conn, 0, open_market_ids={us_mid}) == 0
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT status, filled_quantity FROM orders WHERE id = %s",
            (placed.order_id,),
        )
        row = await cur.fetchone()
    assert row is not None
    assert str(row[0]) == "OPEN" and int(row[1]) == 0

    assert await match_orders(conn, 1, open_market_ids={us_mid, as_mid}) >= 1
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT status FROM orders WHERE id = %s", (placed.order_id,)
        )
        (status,) = await cur.fetchone()
    assert status == "FILLED"


async def test_closed_venue_knockout_defers(conn: AsyncConnection) -> None:
    """R3: a KO-eligible bounded short on a closed venue survives the
    sweep and knocks out once its venue is open."""
    from stockbot.shorts.service import open_bounded_short, sweep_knockouts

    us_mid = await _us_market_id(conn)
    as_mid = await _add_asia_venue(conn)
    _iid, as_ticker = await _second_stock(conn)
    await _move_ticker_to_market(conn, as_ticker, as_mid)
    await _fund_user(conn, 9103)

    res = await open_bounded_short(
        conn, user_id=9103, ticker=as_ticker, quantity=1
    )
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE bounded_shorts SET knockout_price = 0 WHERE id = %s",
            (res.short_id,),
        )

    assert await sweep_knockouts(conn, 0, open_market_ids={us_mid}) == 0
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT status FROM bounded_shorts WHERE id = %s",
            (res.short_id,),
        )
        (status,) = await cur.fetchone()
    assert status == "OPEN"

    assert (
        await sweep_knockouts(conn, 1, open_market_ids={us_mid, as_mid}) == 1
    )
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT status FROM bounded_shorts WHERE id = %s",
            (res.short_id,),
        )
        (status,) = await cur.fetchone()
    assert status == "KNOCKED_OUT"


async def test_closed_venue_alert_does_not_fire(
    conn: AsyncConnection,
) -> None:
    """R3: an already-crossed alert on a closed venue waits for the
    reopen rather than firing on the frozen mark."""
    from decimal import Decimal

    from stockbot.alerts.service import create_alert, sweep_alerts

    us_mid = await _us_market_id(conn)
    as_mid = await _add_asia_venue(conn)
    _iid, as_ticker = await _second_stock(conn)
    await _move_ticker_to_market(conn, as_ticker, as_mid)
    await _fund_user(conn, 9104)

    alert_id = await create_alert(
        conn,
        user_id=9104,
        ticker=as_ticker,
        direction="ABOVE",
        target=Decimal("0.0001"),
    )
    assert await sweep_alerts(conn, 0, open_market_ids={us_mid}) == 0
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT status FROM price_alerts WHERE id = %s", (alert_id,)
        )
        (status,) = await cur.fetchone()
    assert status == "OPEN"

    assert await sweep_alerts(conn, 1, open_market_ids={us_mid, as_mid}) == 1
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT status FROM price_alerts WHERE id = %s", (alert_id,)
        )
        (status,) = await cur.fetchone()
    assert status == "TRIGGERED"


async def test_closed_venue_liquidation_legs_defer(
    conn: AsyncConnection,
) -> None:
    """R3: an undermargined account short on both venues gets its
    open-venue legs executed while the closed-venue leg stays open for
    the venue's reopen."""
    from decimal import Decimal

    from stockbot.admin.service import add_instrument
    from stockbot.margin.service import sweep_undermargined

    us_mid = await _us_market_id(conn)
    as_mid = await _add_asia_venue(conn)
    us_iid, us_ticker = await _first_stock(conn)
    await add_instrument(
        conn,
        ticker="ASSTK",
        name="AS Stock Corp",
        sector_key="TECH",
        base_price=10.0,
    )
    as_iid = await _iid_of(conn, "ASSTK")
    await _move_ticker_to_market(conn, "ASSTK", as_mid)

    await _fund_user(conn, 9105)
    async with conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO entitlements (user_id, item_key, quantity) "
            "VALUES (9105, 'margin_tier', 1) "
            "ON CONFLICT (user_id, item_key) "
            "DO UPDATE SET quantity = EXCLUDED.quantity"
        )
        await cur.execute(
            "UPDATE instruments SET init_margin_pct = 0.5, "
            "maint_margin_pct = 0.3 WHERE id IN (%s, %s)",
            (us_iid, as_iid),
        )
        await cur.execute(
            "SELECT balance FROM accounts "
            "WHERE user_id = 9105 AND kind = 'USER'"
        )
        (cash,) = await cur.fetchone()
        await cur.execute(
            "SELECT quoted_price FROM instruments WHERE id = %s", (us_iid,)
        )
        (p_us,) = await cur.fetchone()
        await cur.execute(
            "SELECT quoted_price FROM instruments WHERE id = %s", (as_iid,)
        )
        (p_as,) = await cur.fetchone()
    # Short ~1.8x equity split across the two venues.
    qty_us = max(
        1, int(Decimal(cash) * Decimal("0.9") / (Decimal(p_us) * 100))
    )
    qty_as = max(
        1, int(Decimal(cash) * Decimal("0.9") / (Decimal(p_as) * 100))
    )
    await execute_trade(
        conn, user_id=9105, ticker=us_ticker, side="SELL", quantity=qty_us
    )
    await execute_trade(
        conn, user_id=9105, ticker="ASSTK", side="SELL", quantity=qty_as
    )

    # Gap both marks up 4x -> undermargined on both legs.
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE instruments SET quoted_price = quoted_price * 4, "
            "base_price = base_price * 4, impact = 0 "
            "WHERE id IN (%s, %s)",
            (us_iid, as_iid),
        )

    legs = await sweep_undermargined(conn, 10, open_market_ids={us_mid})
    assert legs >= 1
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT p.quantity FROM positions p JOIN instruments i "
            "ON i.id = p.instrument_id WHERE p.user_id = 9105 "
            "AND i.ticker = 'ASSTK'"
        )
        (as_qty,) = await cur.fetchone()
    assert int(as_qty) < 0  # closed-venue leg untouched

    legs = await sweep_undermargined(
        conn, 11, open_market_ids={us_mid, as_mid}
    )
    assert legs >= 1
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT COUNT(*) FROM positions p JOIN instruments i "
            "ON i.id = p.instrument_id WHERE p.user_id = 9105 "
            "AND i.ticker = 'ASSTK' AND p.quantity < 0"
        )
        (n,) = await cur.fetchone()
    assert int(n) == 0


# ---------------------------------------------------------------------------
# R4: the seeded Asia venue
# ---------------------------------------------------------------------------


async def test_seeded_asia_venue_runs_staggered_clock(
    conn: AsyncConnection,
) -> None:
    """R4 smoke: 0054 seeds AS (600 open / 840 closed / offset 720), 40
    stocks, and the ASX40 index. On the real shape ticks 0-5 have US open
    and AS closed (AS pos = (tick-720) % 1440 >= 600), so US instruments
    step while AS writes flat CLOSED candles and ASX40 holds the seed."""
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT open_ticks, closed_ticks, offset_ticks, "
            "overnight_var_ticks FROM markets WHERE code = 'AS'"
        )
        row = await cur.fetchone()
    assert row is not None
    assert (int(row[0]), int(row[1]), int(row[2])) == (600, 840, 720)
    assert float(row[3]) == pytest.approx(60.0)  # 60/840 ~= 0.0714 frac

    us_mid = await _us_market_id(conn)
    as_mid = await _iid_of_market(conn, "AS")
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT COUNT(*) FROM instruments i WHERE i.market_id = %s "
            "AND i.kind = 'STOCK' AND i.index_member",
            (as_mid,),
        )
        (n_as,) = await cur.fetchone()
        await cur.execute(
            "SELECT id, index_divisor FROM instruments "
            "WHERE ticker = 'ASX40' AND market_id = %s",
            (as_mid,),
        )
        asx = await cur.fetchone()
        await cur.execute(
            "SELECT i.id FROM instruments i WHERE i.market_id = %s "
            "AND i.kind = 'STOCK' ORDER BY i.id LIMIT 1",
            (as_mid,),
        )
        (as_iid,) = await cur.fetchone()
        await cur.execute(
            "SELECT i.id FROM instruments i WHERE i.market_id = %s "
            "AND i.kind = 'STOCK' ORDER BY i.id LIMIT 1",
            (us_mid,),
        )
        (us_iid,) = await cur.fetchone()
    assert int(n_as) == 40
    assert asx is not None and float(asx[1]) > 0

    for _ in range(3):
        await apply_tick(conn, SEED)

    as_states = await _candle_states(conn, as_iid)
    us_states = await _candle_states(conn, us_iid)
    assert all(s == "CLOSED" for s in as_states.values())
    assert all(s == "OPEN" for s in us_states.values())

    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT quoted_price FROM instruments WHERE ticker = 'ASX40'"
        )
        (asx_px,) = await cur.fetchone()
        await cur.execute(
            "SELECT quoted_price FROM instruments WHERE ticker = 'SBX40'"
        )
        (sbx_px,) = await cur.fetchone()
    # ASX40 never traded a tick -- still the 1000 seed level.
    assert float(asx_px) == pytest.approx(1000.0)
    # SBX40 repriced off its live US basket -- moved off the seed level.
    assert float(sbx_px) != pytest.approx(1000.0)


async def _iid_of_market(conn: AsyncConnection, code: str) -> int:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT id FROM markets WHERE code = %s", (code,)
        )
        (mid,) = await cur.fetchone()
    return int(mid)
