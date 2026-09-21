"""Pure math for the price engine.

No database or I/O here on purpose: this is what the determinism and
price-statistics tests exercise directly. `market/tick.py` wires this up to
Postgres.

Model, one tick at a time (dt is in ticks; Phase 1 always calls with dt=1):

    Δlog P_i = drift_i·dt
             + β_i · M_t              # shared market factor, one draw per tick
             + γ_i · S_{sector(i),t}  # sector factor, one draw per sector per tick
             + σ_i · √dt · ε_i        # idiosyncratic
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

CIRCUIT_BREAKER_CAP = 0.15  # max |Δlog P| allowed in a single tick
CIRCUIT_HALT_TICKS = 5  # ticks an instrument stays halted after a breach


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


@dataclass(frozen=True)
class InstrumentTickResult:
    id: int
    base_price: float
    fundamental_value: float
    impact: float
    quoted_price: float
    circuit_breached: bool


def step_instrument(
    rng: np.random.Generator,
    inst: InstrumentState,
    market_factor: float,
    sector_factor: float,
    dt: float = 1.0,
) -> InstrumentTickResult:
    """Advance one instrument by one tick. Consumes exactly two draws from `rng`
    (idiosyncratic return, then fundamental shock) -- instruments must be
    processed in a stable order for the tick to be reproducible.
    """
    idiosyncratic = float(rng.standard_normal())
    mean_reversion = inst.kappa * float(np.log(inst.fundamental_value / inst.base_price)) * dt
    delta_log_price = (
        inst.drift * dt
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
    )


def freeze_instrument(inst: InstrumentState, dt: float = 1.0) -> InstrumentTickResult:
    """A halted instrument: price and fundamental stay put, impact still decays."""
    new_impact = inst.impact * float(np.exp(-dt / inst.tau_ticks))
    quoted_price = inst.base_price * float(np.exp(new_impact))
    return InstrumentTickResult(
        id=inst.id,
        base_price=inst.base_price,
        fundamental_value=inst.fundamental_value,
        impact=new_impact,
        quoted_price=quoted_price,
        circuit_breached=False,
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

    return fill_price, impact_after


def half_spread_fraction(
    *,
    cfg: dict[str, float],
    sigma: float,
    liquidity: float,
    ticks_since_halt: float | None,
    ticks_to_event: float | None,
) -> float:
    """Dynamic half-spread as a fraction of price (0.0005 = 5bps).

    Wider for high-sigma and illiquid names (that's where adverse selection
    lives), elevated right after a circuit halt ends and into a pending
    event's resolution -- the two moments a taker is most likely to know
    something the mark doesn't. `cfg` is the `spread.*` config namespace.
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
    bps = (
        cfg["spread.base_bps"]
        * (1 + cfg["spread.sigma_coeff"] * sigma / cfg["spread.sigma_ref"])
        * (
            1
            + cfg["spread.inv_liquidity_coeff"] * cfg["spread.liquidity_ref"] / liquidity
        )
        * (1 + cfg["spread.halt_coeff"] * halt_term)
        * (1 + cfg["spread.event_coeff"] * event_term)
    )
    return bps / 10_000
