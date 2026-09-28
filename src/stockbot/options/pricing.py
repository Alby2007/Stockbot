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
mean sigma = sigma + (sigma_eff - sigma) * (1 - rho^T) / (T(1 - rho)).

`ticks_left` is CALENDAR ticks, not open ticks -- the session gap tick
steps with dt=closed_ticks, so total variance per cycle is preserved.
Chart windows count open ticks, which is a display choice only.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

CALL = "CALL"
PUT = "PUT"


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
) -> float:
    """Var[log P_T] over `ticks_left` ticks under the price model."""
    t = max(float(ticks_left), 0.0)
    if t == 0.0:
        return 0.0
    if 0.0 <= vol_cfg.rho < 1.0:
        mean_sigma = vol_cfg.sigma + (sigma_eff - vol_cfg.sigma) * (
            (1.0 - vol_cfg.rho**t) / (t * (1.0 - vol_cfg.rho))
        )
    else:
        mean_sigma = sigma_eff
    ou_var: float = (
        mean_sigma**2 / (2.0 * kappa) * (-math.expm1(-2.0 * kappa * t))
        if kappa > 0.0
        else mean_sigma**2 * t
    )
    return float(fundamental_sigma**2 * t + ou_var)


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
