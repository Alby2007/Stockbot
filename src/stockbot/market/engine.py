"""Pure math for the price engine.

No database or I/O here on purpose: this is what the determinism and
price-statistics tests exercise directly. `market/tick.py` wires this up to
Postgres.

Model, one tick at a time (dt is in ticks; Phase 1 always calls with dt=1):

    Δlog P_i = drift_i·dt
             + β_i · M_t              # shared market factor, one draw per tick
             + γ_i · S_{sector(i),t}  # sector factor, one draw per sector per tick
             + σ_eff_i · √dt · ε_i    # idiosyncratic, unit-variance Student-t(5)
             + κ_i · ln(F_i / P_i)·dt # mean reversion toward the fundamental

clamped by a per-tick circuit breaker. Impact (`impact`, a separate log-offset
applied on top of the fundamental path) is not driven by this model at all --
it only decays here (`impact *= exp(-dt/τ)`); trades are what add to it, in
`trading/service.py`.
"""

from __future__ import annotations

import hashlib
import hmac
import math
from dataclasses import dataclass

import numpy as np

TICKS_PER_DAY = 1440

# Shared-factor parameters. Not per-instrument, so they live here rather than
# in the `instruments` table; promote to a `config` table if these need to be
# hot-tunable later.
MARKET_DRIFT = 0.0
MARKET_DAILY_VOL = 0.008
MARKET_SIGMA = MARKET_DAILY_VOL / TICKS_PER_DAY**0.5

SECTOR_DRIFT = 0.0
SECTOR_DAILY_VOL = 0.012
SECTOR_SIGMA = SECTOR_DAILY_VOL / TICKS_PER_DAY**0.5

# F6 calibration: the realized |r_model + bounded_flow| distribution from a
# 21-day harness run is bimodal -- intraday moves essentially never reach
# 2% even with t(5) tails and 4x vol regimes, while dt=480 overnight gaps
# breach at ~37% per reopen at cap 0.03. A 14-day validation run at this
# cap measured 2.43 halts/instrument/sim-week (target: a few per week,
# ~2-5). The cap is absolute (not dt-normalized) so the breaker mostly
# guards gap reopens -- the real-world analogue of limit-halts at the
# open. Lowering it further mostly raises gap-halt frequency; raising it
# removes halts entirely.
# Max |Δlog P| in a single tick. Calibrated in F6 to a few halts per
# instrument per simulated week. Every measured breach is an overnight-gap
# tick (intraday |r| never gets near this), and Phase I's drift_state
# applies its full drift to the gap, so the realized gap distribution is
# wide: 0.03 -> ~5.4 halts/instr/wk, 0.035 -> ~5.2, 0.045 lands mid-band.
CIRCUIT_BREAKER_CAP = 0.045
CIRCUIT_HALT_TICKS = 5  # ticks an instrument stays halted after a breach

# Fat tails: the idiosyncratic draw is Student-t(5) rescaled to unit
# variance (t_df has variance df/(df-2)). One draw either way, so the
# "consumes exactly two draws" contract below is unchanged -- branching
# jump terms would break draw-count determinism, which is why tails come
# from the distribution shape instead.
STUDENT_T_DF = 5.0
STUDENT_T_SCALE = math.sqrt((STUDENT_T_DF - 2.0) / STUDENT_T_DF)

# E|N(0,1)| -- the EWMA vol-state normalizer, so v_t has stationary mean
# ~1 under Gaussian input rather than settling ~20% low.
SQRT_2_OVER_PI = math.sqrt(2.0 / math.pi)


def ewma_vol_update(prev: float, normalized_abs_ret: float, rho: float) -> float:
    """One EWMA step on a |return|/sigma observation. `normalized_abs_ret`
    should already be divided by the tick's sigma scale (sigma_i * sqrt(dt)
    for model moves) and by SQRT_2_OVER_PI so the state mean-reverts to 1."""
    return rho * prev + (1.0 - rho) * normalized_abs_ret


def vol_multiplier(v_market: float, v_inst: float, w: float, lo: float, hi: float) -> float:
    """The regime multiplier applied to sigma_i: blends the shared
    market-wide vol state with the instrument's own, clipped to [lo, hi]."""
    return min(hi, max(lo, w * v_market + (1.0 - w) * v_inst))


def session_phase(
    tick_index: int, open_ticks: int, closed_ticks: int, offset_ticks: int = 0
) -> str:
    """'OPEN' or 'CLOSED' for a tick index. Pure: the first `open_ticks`
    ticks of each (open+closed) cycle are the trading session; session
    state is derived from the tick index so every process agrees on it
    without a shared flag. `offset_ticks` shifts where the cycle lands
    relative to tick 0 (used to align "market open" with a wall clock)."""
    span = open_ticks + closed_ticks
    return "OPEN" if (tick_index - offset_ticks) % span < open_ticks else "CLOSED"


def ticks_until_open(
    tick_index: int, open_ticks: int, closed_ticks: int, offset_ticks: int = 0
) -> int:
    """Ticks until the next open tick (0 when `tick_index` is itself open)."""
    span = open_ticks + closed_ticks
    pos = (tick_index - offset_ticks) % span
    return 0 if pos < open_ticks else span - pos


def ticks_until_close(
    tick_index: int, open_ticks: int, closed_ticks: int, offset_ticks: int = 0
) -> int:
    """Ticks until the session closes (0 when already closed)."""
    pos = (tick_index - offset_ticks) % (open_ticks + closed_ticks)
    return open_ticks - pos if pos < open_ticks else 0


def tick_seed(master_seed: str, tick_index: int) -> int:
    """Deterministic seed for a tick: HMAC(master_seed, tick_index).

    Same master seed + tick index always produces the same seed, which is
    what makes price history replayable and auditable.
    """
    mac = hmac.new(master_seed.encode(), str(tick_index).encode(), hashlib.sha256).digest()
    return int.from_bytes(mac[:8], "big") & 0x7FFFFFFFFFFFFFFF


def rng_for_tick(master_seed: str, tick_index: int) -> np.random.Generator:
    return np.random.default_rng(tick_seed(master_seed, tick_index))


def draw_market_factor(rng: np.random.Generator, dt: float = 1.0) -> float:
    return float(MARKET_DRIFT * dt + MARKET_SIGMA * dt**0.5 * rng.standard_normal())


def draw_sector_factors(
    rng: np.random.Generator, sector_keys: list[str], dt: float = 1.0
) -> dict[str, float]:
    """One draw per sector, consumed from the same tick's RNG stream.

    `sector_keys` must be passed in a stable (e.g. sorted) order for the draw
    to be reproducible.
    """
    draws = rng.standard_normal(len(sector_keys))
    return {
        key: float(SECTOR_DRIFT * dt + SECTOR_SIGMA * dt**0.5 * draw)
        for key, draw in zip(sector_keys, draws, strict=True)
    }


@dataclass(frozen=True)
class InstrumentState:
    id: int
    sector_key: str
    drift: float
    sigma: float
    beta: float
    gamma: float
    kappa: float
    fundamental_sigma: float
    tau_ticks: float
    base_price: float
    fundamental_value: float
    impact: float
    drift_state: float = 0.0


@dataclass(frozen=True)
class InstrumentTickResult:
    id: int
    base_price: float
    fundamental_value: float
    impact: float
    quoted_price: float
    circuit_breached: bool
    drift_state: float = 0.0


def step_instrument(
    rng: np.random.Generator,
    inst: InstrumentState,
    market_factor: float,
    sector_factor: float,
    dt: float = 1.0,
    *,
    mom_rho: float = 1.0,
    mom_innov_frac: float = 0.0,
    mom_max_frac: float = 0.0,
) -> InstrumentTickResult:
    """Advance one instrument by one tick. Makes exactly three sampler calls
    on `rng` (momentum-regime normal, idiosyncratic Student-t, then
    fundamental normal) -- unconditional and in a fixed instrument order so
    the tick is reproducible. (standard_t's internal gamma rejection
    sampling may consume a variable number of uniforms, but that count is
    itself a deterministic function of the seed -- what must never vary is
    the *call pattern*.)

    `inst.sigma` is the *effective* per-tick sigma: `market/tick.py` passes
    sigma_eff (sigma_i scaled by the vol-regime multiplier), so regime state
    enters the model through this input alone. The momentum regime
    `drift_state` is an additive per-tick drift in the same units, evolved
    AR(1) with persistence `mom_rho` and innovation scale
    `mom_innov_frac * sigma` per sqrt-tick, clipped to
    `mom_max_frac * sigma` (sigma_eff == 0 names keep drift_state pinned at
    0). Defaults disable the regime entirely.
    """
    drift_innov = (
        mom_innov_frac * inst.sigma * dt**0.5 * float(rng.standard_normal())
    )
    drift_cap = mom_max_frac * inst.sigma
    drift_state = float(
        np.clip(mom_rho * inst.drift_state + drift_innov, -drift_cap, drift_cap)
    )

    idiosyncratic = float(rng.standard_t(STUDENT_T_DF)) * STUDENT_T_SCALE
    mean_reversion = inst.kappa * float(np.log(inst.fundamental_value / inst.base_price)) * dt
    delta_log_price = (
        (inst.drift + drift_state) * dt
        + inst.beta * market_factor
        + inst.gamma * sector_factor
        + inst.sigma * dt**0.5 * idiosyncratic
        + mean_reversion
    )

    circuit_breached = abs(delta_log_price) > CIRCUIT_BREAKER_CAP
    if circuit_breached:
        delta_log_price = float(np.clip(delta_log_price, -CIRCUIT_BREAKER_CAP, CIRCUIT_BREAKER_CAP))

    new_base_price = inst.base_price * float(np.exp(delta_log_price))

    fundamental_shock = inst.fundamental_sigma * dt**0.5 * float(rng.standard_normal())
    new_fundamental = inst.fundamental_value * float(np.exp(fundamental_shock))

    new_impact = inst.impact * float(np.exp(-dt / inst.tau_ticks))
    quoted_price = new_base_price * float(np.exp(new_impact))

    return InstrumentTickResult(
        id=inst.id,
        base_price=new_base_price,
        fundamental_value=new_fundamental,
        impact=new_impact,
        quoted_price=quoted_price,
        circuit_breached=circuit_breached,
        drift_state=drift_state,
    )


def freeze_instrument(inst: InstrumentState, dt: float = 1.0) -> InstrumentTickResult:
    """A halted instrument: price and fundamental stay put, impact still
    decays. The momentum regime freezes too -- no draw, drift_state carried
    through unchanged (consistent with the no-draw contract for frozen
    names)."""
    new_impact = inst.impact * float(np.exp(-dt / inst.tau_ticks))
    quoted_price = inst.base_price * float(np.exp(new_impact))
    return InstrumentTickResult(
        id=inst.id,
        base_price=inst.base_price,
        fundamental_value=inst.fundamental_value,
        impact=new_impact,
        quoted_price=quoted_price,
        circuit_breached=False,
        drift_state=inst.drift_state,
    )


def apply_trade_impact(
    *,
    base_price: float,
    impact_before: float,
    signed_notional: float,
    liquidity: float,
    lambda_impact: float,
    max_impact: float,
    half_spread: float,
    tick_size: float = 0.0,
) -> tuple[float, float]:
    """Compute a trade's fill price and the resulting post-trade impact.

    `signed_notional` is positive for a buy, negative for a sell. Returns
    `(fill_price, impact_after)`. Impact decay is *not* applied here -- it
    only happens on tick boundaries (`step_instrument` / `freeze_instrument`).

    Impact is concave in size: `Δimpact = λ·sign(N)·√(|N|/L)`, the
    square-root law -- doubling order size multiplies impact by √2, so
    large orders move the mark less per marginal share than small ones.
    `lambda_impact` was rescaled in migration 0016 to preserve typical-size
    fills (`λ_new = λ_old·√(N_typical/L)`, N_typical = $500).
    """
    delta_impact = lambda_impact * math.copysign(
        math.sqrt(abs(signed_notional) / liquidity), signed_notional
    )
    delta_impact = max(-max_impact, min(max_impact, delta_impact))
    impact_after = impact_before + delta_impact

    # Execution fills at the midpoint of the impact path, plus half-spread
    # against the trader (buys pay it, sells receive less).
    midpoint_impact = impact_before + delta_impact / 2
    fill_price = base_price * float(np.exp(midpoint_impact))
    spread_sign = 1.0 if signed_notional >= 0 else -1.0
    fill_price *= 1.0 + spread_sign * half_spread
    if tick_size > 0:
        fill_price = round_to_tick(fill_price, tick_size)

    return fill_price, impact_after


def tick_size(price: float, cfg: dict[str, float]) -> float:
    """The price grid for quotes/fills: a 1-2-5 multiple of 10^n at or
    above `price * spread.tick_pct`, floored at `spread.tick_min`. Keeps
    ticks human-scale at any price level ($100 -> $0.10, $5 -> $0.01)."""
    tick_min = cfg.get("spread.tick_min", 0.01)
    raw = price * cfg.get("spread.tick_pct", 0.001)
    if raw <= tick_min or price <= 0:
        return tick_min
    base = 10.0 ** math.floor(math.log10(raw))
    for m in (1.0, 2.0, 5.0, 10.0):
        if m * base >= raw:
            return m * base
    return 10.0 * base


def round_to_tick(price: float, tick: float) -> float:
    """Nearest grid point, ties away from zero -- same rule place_order
    uses to snap resting prices (ROUND_HALF_UP)."""
    return math.floor(price / tick + 0.5) * tick


def quote_ticks(mark: float, half_spread: float, tick: float) -> tuple[float, float]:
    """Displayed (bid, ask) on the tick grid, rounded OUTWARD so the
    shown quote is the worst case a market fill can land at."""
    bid = math.floor(mark * (1.0 - half_spread) / tick) * tick
    ask = math.ceil(mark * (1.0 + half_spread) / tick) * tick
    return bid, ask


def effective_liquidity(
    liquidity: float, adv_notional: float, cfg: dict[str, float]
) -> float:
    """Volume-responsive liquidity for IMPACT only (H4): trailing ADV
    relative to `flow.adv_ref_frac` of static liquidity scales the impact
    denominator, clamped to [flow.adv_mult_min, flow.adv_mult_max]. A dead
    tape halves effective liquidity (2x impact); a frenzied tape doubles
    it. The participation cap deliberately stays on static liquidity --
    finding 7's death-spiral guard."""
    lo = cfg.get("flow.adv_mult_min", 0.5)
    hi = cfg.get("flow.adv_mult_max", 2.0)
    ref = liquidity * cfg.get("flow.adv_ref_frac", 2.5e-8)
    mult = lo if ref <= 0 else min(max(adv_notional / ref, lo), hi)
    return liquidity * mult


def half_spread_fraction(
    *,
    cfg: dict[str, float],
    sigma: float,
    liquidity: float,
    ticks_since_halt: float | None,
    ticks_to_event: float | None,
    ticks_since_open: float | None = None,
    ticks_to_close: float | None = None,
) -> float:
    """Dynamic half-spread as a fraction of price (0.0005 = 5bps).

    Wider for high-sigma and illiquid names (that's where adverse selection
    lives), elevated right after a circuit halt ends and into a pending
    event's resolution -- the two moments a taker is most likely to know
    something the mark doesn't. The open/close terms give the classic
    U-shaped intraday pattern: widest just after the overnight gap lands,
    decaying through the session, widening again into the close.
    `cfg` is the `spread.*` config namespace.
    """
    halt_term = (
        0.0
        if ticks_since_halt is None
        else np.exp(-ticks_since_halt / cfg["spread.halt_decay_ticks"])
    )
    event_term = (
        0.0
        if ticks_to_event is None
        else np.exp(-max(ticks_to_event, 0.0) / cfg["spread.event_window_ticks"])
    )
    open_term = (
        0.0
        if ticks_since_open is None
        else np.exp(
            -max(ticks_since_open, 0.0) / cfg.get("spread.open_decay_ticks", 90.0)
        )
    )
    close_term = (
        0.0
        if ticks_to_close is None
        else np.exp(
            -max(ticks_to_close, 0.0) / cfg.get("spread.close_decay_ticks", 60.0)
        )
    )
    bps = (
        cfg["spread.base_bps"]
        * (1 + cfg["spread.sigma_coeff"] * sigma / cfg["spread.sigma_ref"])
        * (
            1
            + cfg["spread.inv_liquidity_coeff"] * cfg["spread.liquidity_ref"] / liquidity
        )
        * (1 + cfg["spread.halt_coeff"] * halt_term)
        * (1 + cfg["spread.event_coeff"] * event_term)
        * (
            1
            + cfg.get("spread.open_coeff", 0.0) * open_term
            + cfg.get("spread.close_coeff", 0.0) * close_term
        )
    )
    return bps / 10_000
