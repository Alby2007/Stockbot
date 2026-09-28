"""Model-consistent option pricing (Phase O1).

`step_instrument` mean-reverts log price toward `fundamental_value` with
exact OU decay at rate `kappa` per tick, while the fundamental itself
random-walks at `fundamental_sigma` per sqrt-tick. So log-price variance
to expiry is NOT sigma*sqrt(T):

    Var[log P_T] = sigma_F^2 * T            (fundamental's own walk)
                 + (sigma^2 / 2k) * (1 - e^{-2kT})   (stationary OU deviation)

A naive sigma*sqrt(T) overprices every listed expiry 5-7x for the seeded
calibration (sigma=1.78e-4, kappa=0.029 -> 28-tick half-life pins the
price to its fundamental); tests/test_options.py pins the closed form
against that table so the bug can't silently return.

`sigma_eff` (the vol-regime-scaled sigma) is an EWMA that decays back to
the long-run `sigma` with persistence `vol.rho` -- pricing a 30d
contract off today's regime value would misprice it across regimes, so
the OU term uses the EWMA's time-mean over the horizon:
mean sigma = sigma + (sigma_eff - sigma) * (1 - rho^N) / (N(1 - rho)),
where N counts STEP CALLS in the horizon = open ticks (the EWMA updates
once per step; closed ticks never step).

Three clocks, post-0033: stochastic accrual runs on EFFECTIVE ticks
(each open tick = 1 unit, each closed tick = `session.overnight_var_frac`
-- the gap tick steps once with var_dt = closed*frac, so a closed
stretch realizes frac of its nominal variance), OU/mean-reversion decay
runs on CALENDAR ticks (the gap step reverts with full dt=closed_ticks),
and the vol EWMA runs on open ticks. Callers pass all three via
`var_ticks`/`open_ticks` from `session_variance_ticks`; the one-argument
form treats every tick as open (session-less market).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

CALL = "CALL"
PUT = "PUT"


def session_variance_ticks(
    now_tick: int, ticks_left: int, session_cfg: dict[str, float]
) -> tuple[float, float, float]:
    """(accrual, calendar, open) tick counts over the horizon.

    Stochastic variance accrues at 1 unit per open tick and
    `session.overnight_var_frac` per closed tick (the whole closed
    stretch lands on the reopen's gap step with var_dt = closed*frac).
    The horizon is ticks (now_tick, now_tick + ticks_left]: the expiry
    tick's own step has run before settlement reads its mark. With
    sessions disabled (closed_ticks = 0) every tick accrues 1 --
    identical to the pre-session model.
    """
    t = max(int(ticks_left), 0)
    open_t = int(session_cfg.get("session.open_ticks", 0))
    closed_t = int(session_cfg.get("session.closed_ticks", 0))
    span = open_t + closed_t
    if t == 0 or span <= 0 or closed_t == 0:
        return float(t), float(t), float(t)
    frac = float(session_cfg.get("session.overnight_var_frac", 1.0))
    offset = int(session_cfg.get("session.phase_offset_ticks", 0))

    # Open ticks i in [0, x]: pos(i) = (i - offset) % span < open_t. For
    # i >= offset the j = i - offset range is non-negative and counts
    # straightforwardly. For i < offset (phase_offset > 0 lands tick 0
    # mid-cycle) j wraps negative; m = -j is open iff m % span > span -
    # open_t or m % span == 0 -- again open_t residues per cycle.
    def opens_upto(x: int) -> int:
        if x < 0:
            return 0

        def neg_open(m_max: int) -> int:
            """m in [1, m_max] with (-m) % span < open_t."""
            q, rem = divmod(max(m_max, 0), span)
            return q * open_t + max(0, rem + open_t - span)

        n = 0
        hi = x - offset
        if hi >= 0:
            n += (hi // span) * open_t + min(hi % span + 1, open_t)
        pre = min(x, offset - 1)  # i in [0, pre] sits left of the offset
        if pre >= 0:
            # m = offset - i runs from offset - pre through offset.
            n += neg_open(offset) - neg_open(offset - pre - 1)
        return n

    n_open = opens_upto(now_tick + t) - opens_upto(now_tick)
    n_closed = t - n_open
    return n_open + frac * n_closed, float(t), float(n_open)


@dataclass(frozen=True)
class VolBlend:
    """How the current effective sigma reverts to its long-run level."""

    sigma: float  # long-run per-tick sigma (instruments.sigma)
    rho: float  # vol-regime EWMA persistence per tick (vol.rho)


def total_variance(
    sigma_eff: float,
    kappa: float,
    fundamental_sigma: float,
    ticks_left: float,
    vol_cfg: VolBlend,
    *,
    var_ticks: float | None = None,
    open_ticks: float | None = None,
) -> float:
    """Var[log P_T] over `ticks_left` CALENDAR ticks under the price model.

    `var_ticks` is the stochastic-accrual horizon (closed ticks count at
    `overnight_var_frac`, post-0033) and `open_ticks` the vol-EWMA window
    (one update per step call = per open tick). Both default to
    `ticks_left` -- the session-less market.
    """
    t = max(float(ticks_left), 0.0)
    var_t = t if var_ticks is None else max(float(var_ticks), 0.0)
    blend_t = var_t if open_ticks is None else max(float(open_ticks), 0.0)
    if t == 0.0 or var_t == 0.0:
        return 0.0
    if 0.0 <= vol_cfg.rho < 1.0 and blend_t > 0.0:
        mean_sigma = vol_cfg.sigma + (sigma_eff - vol_cfg.sigma) * (
            (1.0 - vol_cfg.rho**blend_t) / (blend_t * (1.0 - vol_cfg.rho))
        )
    else:
        mean_sigma = sigma_eff
    # The OU deviation accrues innovation only on step calls (var_t/t of
    # calendar rate on average) but mean-reverts in calendar time.
    accrual = var_t / t
    ou_var: float = (
        accrual
        * mean_sigma**2
        / (2.0 * kappa)
        * (-math.expm1(-2.0 * kappa * t))
        if kappa > 0.0
        else mean_sigma**2 * var_t
    )
    return float(fundamental_sigma**2 * var_t + ou_var)


def _norm_cdf(x: float) -> float:
    return 0.5 * math.erfc(-x / math.sqrt(2.0))


def intrinsic(side: str, spot: float, strike: float) -> float:
    if side == CALL:
        return max(spot - strike, 0.0)
    return max(strike - spot, 0.0)


def bs_price(side: str, spot: float, strike: float, total_var: float) -> float:
    """r=0 Black-Scholes on total variance to expiry. total_var<=0 -> intrinsic."""
    if spot <= 0.0 or strike <= 0.0:
        raise ValueError("spot and strike must be positive")
    if total_var <= 0.0:
        return intrinsic(side, spot, strike)
    vol = math.sqrt(total_var)
    d1 = (math.log(spot / strike) + total_var / 2.0) / vol
    d2 = d1 - vol
    if side == CALL:
        return spot * _norm_cdf(d1) - strike * _norm_cdf(d2)
    return strike * _norm_cdf(-d2) - spot * _norm_cdf(-d1)


def bs_delta(side: str, spot: float, strike: float, total_var: float) -> float:
    """Share-equivalent sensitivity; the (currently disabled) hedge-flow size."""
    if total_var <= 0.0:
        itm = (spot > strike) if side == CALL else (spot < strike)
        if spot == strike:
            return 0.5 if side == CALL else -0.5
        return (1.0 if itm else 0.0) if side == CALL else (-1.0 if itm else 0.0)
    vol = math.sqrt(total_var)
    d1 = (math.log(spot / strike) + total_var / 2.0) / vol
    return _norm_cdf(d1) if side == CALL else _norm_cdf(d1) - 1.0
