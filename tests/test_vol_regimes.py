"""Phase F: EWMA vol regimes, Student-t tails, flow-bounded halts.

The load-bearing tests are the adversarial ones: flow-fed vol plus live
halts means a trader could deliberately halt an instrument, so the F5
guards (bounded flow contribution, per-account cap, risk-reducing trades
during halts, short flow halts) are tested as hard as the regime math.
"""

from __future__ import annotations

import math
import os

import numpy as np
import pytest
from psycopg import AsyncConnection, conninfo

from stockbot.accounts.service import bootstrap_user
from stockbot.config import get_settings
from stockbot.ledger.service import get_system_account_id, post_transfer
from stockbot.margin.service import sweep_undermargined
from stockbot.market import engine
from stockbot.market.engine import InstrumentState, step_instrument
from stockbot.market.tick import apply_tick
from stockbot.migrate import run_migrations
from stockbot.tools import replay
from stockbot.trading.errors import InstrumentHaltedError
from stockbot.trading.service import execute_trade

_SEED = "vol-test-seed"


async def _first_stock(conn: AsyncConnection) -> dict:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT id, ticker, liquidity, quoted_price, sigma "
            "FROM instruments WHERE is_active AND kind = 'STOCK' ORDER BY id LIMIT 1"
        )
        r = await cur.fetchone()
    return {"id": r[0], "ticker": r[1], "liquidity": float(r[2]),
            "quoted": float(r[3]), "sigma": float(r[4])}


async def _fund(conn: AsyncConnection, user_id: int, amount_minor: int) -> int:
    account_id = (await bootstrap_user(conn, user_id)).account_id
    faucet = await get_system_account_id(conn, "FAUCET")
    await post_transfer(
        conn,
        from_account_id=faucet,
        to_account_id=account_id,
        amount=amount_minor,
        reason="TEST_TOPUP",
    )
    return account_id


async def _grant_tier(conn: AsyncConnection, user_id: int, tier: int = 1) -> None:
    async with conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO entitlements (user_id, item_key, quantity)
            VALUES (%s, 'margin_tier', %s)
            ON CONFLICT (user_id, item_key)
            DO UPDATE SET quantity = EXCLUDED.quantity
            """,
            (user_id, tier),
        )


async def _set_config(conn: AsyncConnection, key: str, value: float) -> None:
    async with conn.cursor() as cur:
        await cur.execute("UPDATE config SET value = %s WHERE key = %s", (value, key))


async def _halted_until(conn: AsyncConnection, instrument_id: int) -> int | None:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT circuit_halted_until_tick FROM instruments WHERE id = %s",
            (instrument_id,),
        )
        row = await cur.fetchone()
    return None if row is None or row[0] is None else int(row[0])


async def _cap_size_qty(conn: AsyncConnection, inst: dict) -> int:
    """Largest per-fill quantity that stays under the participation cap,
    with headroom for the whale's own impact raising the fill price.
    The cap binds on EFFECTIVE liquidity (Plan A) -- the whale's flow
    raises vol_state and thins the book mid-loop -- so this re-reads the
    live adv/vol_state/mark each call. Sizing as a fraction of liq_eff
    keeps the per-fill impact ~constant under the sqrt law, which is what
    the F5 assertions depend on."""
    from stockbot.market.data import flow_config, participation_cap

    cap = await participation_cap(conn)
    cfg = await flow_config(conn)
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT quoted_price, adv, vol_state FROM instruments WHERE id = %s",
            (inst["id"],),
        )
        quoted, adv, vol_state = await cur.fetchone()
    liq_eff = engine.effective_liquidity(
        inst["liquidity"], float(adv), cfg, float(vol_state)
    )
    return max(1, int(cap * liq_eff / float(quoted) * 0.60))


# --- pure-math pieces -------------------------------------------------------


def test_ewma_vol_update_has_unit_stationary_mean() -> None:
    """Finding 1: |r|/sigma alone settles ~0.8; normalized by sqrt(2/pi)
    the stationary mean is 1."""
    rng = np.random.default_rng(7)
    v = 1.0
    vs = []
    for z in rng.standard_normal(20_000):
        v = engine.ewma_vol_update(v, abs(float(z)) / engine.SQRT_2_OVER_PI, 0.94)
        vs.append(v)
    mean_v = float(np.mean(vs[1_000:]))
    assert 0.95 < mean_v < 1.05


def test_student_t_idiosyncratic_has_fat_tails_unit_variance() -> None:
    """F2: the idiosyncratic draw is Student-t(5) scaled to unit variance --
    excess kurtosis ~6 (Gaussian: 0), variance still sigma^2. sigma must be
    small enough that the breaker cap (0.03) never binds: at 0.002 the cap
    is 15+ sigma out."""
    rng = engine.rng_for_tick("kurtosis", 1)
    sigma = 0.002
    inst = InstrumentState(
        id=1, sector_key="x", drift=0.0, sigma=sigma, beta=0.0, gamma=0.0,
        kappa=0.0, fundamental_sigma=0.0, tau_ticks=1e9,
        base_price=100.0, fundamental_value=100.0, impact=0.0,
    )
    rets = np.array(
        [
            math.log(step_instrument(rng, inst, 0.0, 0.0).base_price / 100.0)
            for _ in range(30_000)
        ]
    )
    var = float(rets.var())
    excess_kurt = float(((rets - rets.mean()) ** 4).mean() / var**2) - 3.0
    assert math.isclose(var, sigma * sigma, rel_tol=0.05)
    assert excess_kurt > 3.0  # ideal t(5): 6; Gaussian would be ~0


def test_bound_flow_caps_per_account_and_total() -> None:
    from stockbot.market.data import bound_flow

    bounded, raw = bound_flow(
        {100: 0.05, 200: -0.04, 0: 0.03}, account_cap=0.02, total_cap=0.05
    )
    # user 100 clipped to +0.02, user 200 to -0.02, system flow unclipped
    assert math.isclose(bounded, 0.03)  # 0.02 - 0.02 + 0.03
    assert math.isclose(raw, 0.04)


def test_bound_flow_total_cap_binds() -> None:
    from stockbot.market.data import bound_flow

    bounded, raw = bound_flow({u: 0.02 for u in range(1, 8)}, 0.02, 0.05)
    assert math.isclose(bounded, 0.05)  # 7 * 0.02 = 0.14, clipped to 0.05
    assert math.isclose(raw, 0.14)


# --- tick-level regime behavior ---------------------------------------------


async def test_vol_state_stays_bounded_and_writes_sigma_eff(conn) -> None:
    for _ in range(30):
        await apply_tick(conn, _SEED)
    async with conn.cursor() as cur:
        await cur.execute(
            """
            SELECT vol_state, sigma_eff, sigma FROM instruments
            WHERE is_active AND kind = 'STOCK'
            """
        )
        rows = await cur.fetchall()
    assert rows
    for vol_state, sigma_eff, sigma in rows:
        v, se, s = float(vol_state), float(sigma_eff), float(sigma)
        assert 0 < v < 5.0
        # sigma_eff = sigma * clip(multiplier, 0.5, 4.0)
        assert math.isclose(se, s * min(4.0, max(0.5, se / s)), rel_tol=1e-9)
        assert 0.4 * s <= se <= 4.5 * s


async def test_injected_vol_regime_persists_and_decays(conn) -> None:
    """Clustering: a shocked vol_state keeps |returns| elevated for many
    ticks, then mean-reverts toward baseline as rho decays it. sigma_eff
    scales only the idiosyncratic term, so the measured lift is diluted by
    the unchanged market/sector factors -- 4x sigma_eff means <4x |r|."""
    inst = await _first_stock(conn)

    async def tick_return() -> float:
        await apply_tick(conn, _SEED)
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT open, close FROM candles "
                "WHERE instrument_id = %s AND tick_index = "
                "(SELECT MAX(tick_index) FROM market_ticks)",
                (inst["id"],),
            )
            o, c = await cur.fetchone()
        return abs(math.log(float(c) / float(o)))

    baseline_r = [await tick_return() for _ in range(30)]
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE instruments SET vol_state = 4.0, sigma_eff = sigma * 4.0 "
            "WHERE id = %s",
            (inst["id"],),
        )
    regime_r = [await tick_return() for _ in range(45)]

    # The first regime ticks run at ~4x idiosyncratic sigma; diluted by the
    # systematic terms that's still clearly above this instrument's own
    # baseline. By ~45 ticks rho=0.94 has mostly reverted the state.
    assert float(np.mean(regime_r[:8])) > 1.4 * float(np.mean(baseline_r))
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT vol_state FROM instruments WHERE id = %s", (inst["id"],)
        )
        (v,) = await cur.fetchone()
    assert float(v) < 2.5  # decayed well off the 4.0 shock


async def test_sigma_eff_widens_fill_spread(conn) -> None:
    """F3: fill pricing reads COALESCE(sigma_eff, sigma) -- an elevated
    regime must show up in the recorded half_spread, not just the row."""
    inst = await _first_stock(conn)
    uid = 9401
    await _fund(conn, uid, 1_000_000_00)

    async def buy_and_spread() -> float:
        await execute_trade(conn, user_id=uid, ticker=inst["ticker"],
                            side="BUY", quantity=1)
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT half_spread FROM trades WHERE user_id = %s "
                "ORDER BY id DESC LIMIT 1",
                (uid,),
            )
            (hs,) = await cur.fetchone()
        return float(hs)

    await apply_tick(conn, _SEED)
    baseline = await buy_and_spread()
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE instruments SET sigma_eff = sigma * 4.0 WHERE id = %s",
            (inst["id"],),
        )
    elevated = await buy_and_spread()
    # The sigma term is one of several spread components, so the ratio is
    # diluted below 4x -- but it must widen clearly.
    assert elevated > baseline * 1.5


async def test_gap_tick_does_not_pin_vol_clip(conn) -> None:
    """Finding 2: the overnight gap's |r| is normalized by sigma*sqrt(dt),
    so a routine reopen must not slam vol_state into the clip."""
    await _set_config(conn, "session.open_ticks", 8)
    await _set_config(conn, "session.closed_ticks", 4)
    # Two full cycles: tick 12 and tick 24 are gap ticks (dt=4).
    for _ in range(26):
        await apply_tick(conn, _SEED)
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT vol_state, sigma_eff / sigma FROM instruments "
            "WHERE is_active AND kind = 'STOCK' AND sigma > 0"
        )
        rows = await cur.fetchall()
        await cur.execute(
            "SELECT vol_state FROM market_ticks ORDER BY tick_index DESC LIMIT 1"
        )
        (v_mkt,) = await cur.fetchone()
    # Without sqrt(dt) normalization every gap would drive every stock's
    # multiplier to the 4.0 clip and keep it there. (Low-sigma stocks do
    # sit at a legitimately elevated v_i -- the systematic factors dominate
    # their total vol -- so the assertion is on the clip fraction, not a
    # low absolute ceiling.)
    pinned = sum(1 for _v, mult in rows if float(mult) >= 3.99)
    assert pinned < len(rows) // 4
    assert 0 < float(v_mkt) < 3.0


async def test_closed_ticks_freeze_vol_state(conn) -> None:
    await _set_config(conn, "session.open_ticks", 3)
    await _set_config(conn, "session.closed_ticks", 2)
    for _ in range(5):  # ticks 0-2 open, 3-4 closed
        await apply_tick(conn, _SEED)
    async with conn.cursor() as cur:
        await cur.execute(
            """
            SELECT tick_index, vol_state FROM market_ticks ORDER BY tick_index
            """
        )
        rows = await cur.fetchall()
    # closed rows (ticks 3, 4) carry the last open value forward
    assert float(rows[3][1]) == float(rows[2][1]) == float(rows[4][1])


# --- F5 manipulation guards ---------------------------------------------------


async def test_lone_whale_cannot_trip_breaker(conn, monkeypatch) -> None:
    """F5.1/F5.2: one account hammering an instrument at the participation
    cap can never trip the breaker -- its bounded contribution sits below
    the cap by construction."""
    monkeypatch.setattr(engine, "CIRCUIT_BREAKER_CAP", 0.03)
    await _set_config(conn, "vol.account_flow_cap", 0.01)
    await _set_config(conn, "vol.flow_ret_cap", 0.05)
    inst = await _first_stock(conn)
    await _fund(conn, 9101, 10_000_000_000_00)  # $10B

    await apply_tick(conn, _SEED)  # establish tick-0 candles
    for _ in range(4):
        for _ in range(10):
            qty = await _cap_size_qty(conn, inst)
            await execute_trade(
                conn, user_id=9101, ticker=inst["ticker"], side="BUY", quantity=qty
            )
        applied = await apply_tick(conn, _SEED)
        assert await _halted_until(conn, inst["id"]) is None
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT ABS(flow_ret) FROM candles "
                "WHERE instrument_id = %s AND tick_index = %s",
                (inst["id"], applied),
            )
            (flow_ret,) = await cur.fetchone()
        # raw flow was ~10 * 0.3% = ~3%; the recorded contribution is the
        # bounded 1%.
        assert float(flow_ret) <= 0.01 + 1e-9


async def test_crowd_flow_trips_short_halt_not_full_halt(conn, monkeypatch) -> None:
    """F5.4: crowd-scale flow CAN breach (bounded total > cap), but the
    halt is the short flow-attributed one, not CIRCUIT_HALT_TICKS."""
    monkeypatch.setattr(engine, "CIRCUIT_BREAKER_CAP", 0.03)
    await _set_config(conn, "vol.account_flow_cap", 0.02)
    await _set_config(conn, "vol.flow_ret_cap", 0.08)
    await _set_config(conn, "vol.flow_halt_ticks", 2)
    inst = await _first_stock(conn)

    await apply_tick(conn, _SEED)
    users = [9200 + i for i in range(6)]
    for uid in users:
        await _fund(conn, uid, 10_000_000_000_00)
        # ~8 cap-sized buys per user -> raw ~2.4%, clipped at the 2% cap;
        # 6 users -> bounded total min(6*0.02, 0.08) = 0.08 > 0.03 cap.
        for _ in range(8):
            qty = await _cap_size_qty(conn, inst)
            await execute_trade(
                conn, user_id=uid, ticker=inst["ticker"], side="BUY", quantity=qty
            )
    applied = await apply_tick(conn, _SEED)
    halted = await _halted_until(conn, inst["id"])
    assert halted == applied + 2  # flow halt, not the 5-tick model halt


async def test_halt_permits_risk_reduction_only(conn) -> None:
    """F5.3: while halted, closing/shrinking an existing position is legal;
    opening, adding, or flipping is not."""
    short_uid, long_uid, flat_uid = 9301, 9302, 9303
    await _fund(conn, short_uid, 1_000_000_00)
    await _grant_tier(conn, short_uid)
    await _fund(conn, long_uid, 1_000_000_00)
    await _fund(conn, flat_uid, 1_000_000_00)
    inst = await _first_stock(conn)

    await execute_trade(conn, user_id=short_uid, ticker=inst["ticker"], side="SELL", quantity=4)
    await execute_trade(conn, user_id=long_uid, ticker=inst["ticker"], side="BUY", quantity=5)

    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE instruments SET circuit_halted_until_tick = 9999 WHERE id = %s",
            (inst["id"],),
        )

    # Covers and reductions proceed.
    await execute_trade(conn, user_id=short_uid, ticker=inst["ticker"], side="BUY", quantity=1)
    await execute_trade(conn, user_id=short_uid, ticker=inst["ticker"], side="BUY", quantity=3)
    await execute_trade(conn, user_id=long_uid, ticker=inst["ticker"], side="SELL", quantity=2)

    # Opens, adds, and flips stay blocked.
    with pytest.raises(InstrumentHaltedError):
        await execute_trade(conn, user_id=flat_uid, ticker=inst["ticker"], side="BUY", quantity=1)
    with pytest.raises(InstrumentHaltedError):
        await execute_trade(conn, user_id=short_uid, ticker=inst["ticker"], side="SELL", quantity=1)
    with pytest.raises(InstrumentHaltedError):
        # long_uid holds 3 now; selling 4 would flip to a short
        await execute_trade(conn, user_id=long_uid, ticker=inst["ticker"], side="SELL", quantity=4)


async def test_liquidation_proceeds_during_halt(conn) -> None:
    """Symmetry check: the margin sweep already force-closes during halts
    (no halt check in _liquidate_leg) -- verify it still does. The account
    is left grant-poor on purpose: the 10x adverse move must actually push
    it under maintenance, not just dent a topped-up balance."""
    uid = 9401
    await bootstrap_user(conn, uid)  # $100 grant only
    await _grant_tier(conn, uid)
    inst = await _first_stock(conn)
    await execute_trade(conn, user_id=uid, ticker=inst["ticker"], side="SELL", quantity=1)

    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE instruments SET circuit_halted_until_tick = 9999, "
            "base_price = base_price * 10, quoted_price = quoted_price * 10 "
            "WHERE id = %s",
            (inst["id"],),
        )
    legs = await sweep_undermargined(conn, 1)
    assert legs > 0


async def test_replay_vol_check_passes_on_committed_ticks() -> None:
    """F4: replay's vol-state check verifies the EWMA transition rule
    against stored candles/flow_ret. Needs committed state, so it runs on
    a disposable database (same pattern as the concurrency tests)."""
    base = conninfo.conninfo_to_dict(get_settings().test_database_url)
    admin_url = conninfo.make_conninfo(**{**base, "dbname": "postgres"})
    scratch = f"stockbot_volrep_{os.getpid()}"
    scratch_url = conninfo.make_conninfo(**{**base, "dbname": scratch})

    admin = await AsyncConnection.connect(admin_url, autocommit=True)
    try:
        try:
            await admin.execute(f'CREATE DATABASE "{scratch}"')
        except Exception as exc:
            pytest.skip(f"cannot create scratch DB ({exc})")
    finally:
        await admin.close()

    try:
        run_migrations(scratch_url)
        market_conn = await AsyncConnection.connect(scratch_url, autocommit=False)
        try:
            for _ in range(6):
                await apply_tick(market_conn, "replay-vol-seed")
        finally:
            await market_conn.close()

        report = replay.run(scratch_url, "replay-vol-seed", 0, 5)
        assert report.clean, (
            report.factor_mismatches
            + report.mark_inconsistencies
            + report.candle_inconsistencies
            + report.vol_mismatches
        )
    finally:
        admin = await AsyncConnection.connect(admin_url, autocommit=True)
        try:
            await admin.execute(f'DROP DATABASE IF EXISTS "{scratch}" WITH (FORCE)')
        finally:
            await admin.close()
