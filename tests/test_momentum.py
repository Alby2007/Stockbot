"""Phase I1: AR(1) momentum regimes.

The load-bearing properties: the regime is stationary and mean-zero (it
must not become a permanent trend), it is clipped hard so it can never
overwhelm kappa's fundamental anchor, and its RNG draw is unconditional
(momentum normal -> idiosyncratic Student-t -> fundamental normal).
"""

from __future__ import annotations

import math

import numpy as np
from psycopg import AsyncConnection

from stockbot.market import engine
from stockbot.market.engine import (
    InstrumentState,
    freeze_instrument,
    step_instrument,
)
from stockbot.market.tick import apply_tick

_SEED = "mom-test-seed"
_MOM = dict(mom_rho=0.995, mom_innov_frac=0.04, mom_max_frac=1.0)


def _inst(**over) -> InstrumentState:
    defaults = dict(
        id=1, sector_key="x", drift=0.0, sigma=0.002, beta=0.0, gamma=0.0,
        kappa=0.02, fundamental_sigma=0.0, tau_ticks=1e9,
        base_price=100.0, fundamental_value=100.0, impact=0.0,
        drift_state=0.0,
    )
    defaults.update(over)
    return InstrumentState(**defaults)


def test_drift_state_is_stationary_mean_zero_and_clipped() -> None:
    """AR(1) with rho=0.995, innov=0.04*sigma: stationary std should be
    ~0.04/sqrt(1-0.995^2) ~= 0.40 sigma, mean ~0, |state| <= max_frac*sigma."""
    rng = engine.rng_for_tick(_SEED, 1)
    inst = _inst()
    states = []
    cur = inst
    for _ in range(20_000):
        res = step_instrument(rng, cur, 0.0, 0.0, **_MOM)
        cur = _inst(
            drift_state=res.drift_state,
            base_price=res.base_price,
            fundamental_value=res.fundamental_value,
            impact=res.impact,
        )
        states.append(res.drift_state)
    arr = np.array(states[1_000:])
    assert abs(float(arr.mean())) < 0.05 * inst.sigma
    std = float(arr.std())
    expected = 0.04 * inst.sigma / math.sqrt(1 - 0.995**2)
    assert math.isclose(std, expected, rel_tol=0.15)
    assert float(np.abs(arr).max()) <= inst.sigma + 1e-12  # max_frac = 1.0


def test_momentum_draw_is_first_and_unconditional() -> None:
    """The momentum draw leads the sampler stream: reproduce the step
    exactly with a manual normal -> standard_t -> normal sequence."""
    inst = _inst()
    rng_a = engine.rng_for_tick(_SEED, 7)
    res = step_instrument(rng_a, inst, 0.1, -0.05, **_MOM)

    rng_b = engine.rng_for_tick(_SEED, 7)
    eps_mom = float(rng_b.standard_normal())
    eps_t = float(rng_b.standard_t(engine.STUDENT_T_DF)) * engine.STUDENT_T_SCALE
    eps_f = float(rng_b.standard_normal())

    drift_state = float(
        np.clip(0.995 * 0.0 + 0.04 * inst.sigma * eps_mom, -inst.sigma, inst.sigma)
    )
    assert math.isclose(res.drift_state, drift_state)

    delta = (inst.drift + drift_state) + eps_t * inst.sigma
    expected_base = inst.base_price * math.exp(min(delta, engine.CIRCUIT_BREAKER_CAP))
    expected_f = inst.fundamental_value * math.exp(0.0 * eps_f)
    assert math.isclose(res.base_price, expected_base, rel_tol=1e-12)
    assert math.isclose(res.fundamental_value, expected_f)


def test_momentum_disabled_by_default() -> None:
    """No mom kwargs -> the zero cap wipes any stale drift_state."""
    rng = engine.rng_for_tick(_SEED, 3)
    res = step_instrument(rng, _inst(drift_state=0.001), 0.0, 0.0)
    assert res.drift_state == 0.0


def test_kappa_still_anchors_under_strong_momentum() -> None:
    """Even a deliberately strong regime (innov 0.2, cap 3 sigma) must not
    overwhelm mean reversion: displacement stays bounded, no runaway."""
    rng = engine.rng_for_tick(_SEED, 11)
    cur = _inst()
    worst = 0.0
    states = []
    for _ in range(5_000):
        res = step_instrument(
            rng, cur, 0.0, 0.0,
            mom_rho=0.999, mom_innov_frac=0.2, mom_max_frac=3.0,
        )
        cur = _inst(
            drift_state=res.drift_state,
            base_price=res.base_price,
            fundamental_value=res.fundamental_value,
            impact=res.impact,
        )
        states.append(res.drift_state)
        worst = max(worst, abs(math.log(res.base_price / res.fundamental_value)))
    assert worst < 0.5  # kappa=0.02 anchors: a 3-sigma regime displaces ~0.3
    arr = np.array(states[500:])
    autocorr = float(np.corrcoef(arr[:-1], arr[1:])[0, 1])
    assert autocorr > 0.9  # genuinely persistent regime, not white noise


def test_freeze_carries_drift_state() -> None:
    res = freeze_instrument(_inst(drift_state=0.0004, impact=0.01))
    assert res.drift_state == 0.0004


async def test_apply_tick_persists_bounded_drift_state(conn: AsyncConnection) -> None:
    """drift_state survives the tick write and stays inside the clip."""
    for _ in range(50):
        await apply_tick(conn, _SEED)
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT COUNT(*), MAX(ABS(drift_state)), "
            "MAX(ABS(drift_state) / NULLIF(COALESCE(sigma_eff, sigma), 0)) "
            "FROM instruments WHERE is_active AND kind = 'STOCK'"
        )
        count, max_abs, max_frac = await cur.fetchone()
    assert count > 0
    assert max_abs is not None and float(max_abs) > 0
    assert float(max_frac) <= 1.0 + 1e-9  # mom.max_frac
