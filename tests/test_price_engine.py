from __future__ import annotations

import math

import numpy as np

from stockbot.market import engine


def test_same_seed_same_draws() -> None:
    """Replay determinism: same master seed + tick index always gives the same seed."""
    rng1 = engine.rng_for_tick("seed-a", 42)
    rng2 = engine.rng_for_tick("seed-a", 42)
    assert rng1.standard_normal(5).tolist() == rng2.standard_normal(5).tolist()


def test_different_tick_index_gives_different_draws() -> None:
    rng1 = engine.rng_for_tick("seed-a", 42)
    rng2 = engine.rng_for_tick("seed-a", 43)
    assert rng1.standard_normal(5).tolist() != rng2.standard_normal(5).tolist()


def test_different_master_seed_gives_different_draws() -> None:
    rng1 = engine.rng_for_tick("seed-a", 42)
    rng2 = engine.rng_for_tick("seed-b", 42)
    assert rng1.standard_normal(5).tolist() != rng2.standard_normal(5).tolist()


def _base_instrument(**overrides: float) -> engine.InstrumentState:
    defaults: dict = dict(
        id=1,
        sector_key="TECH",
        drift=0.0,
        sigma=0.0005,
        beta=1.0,
        gamma=1.0,
        kappa=0.02,
        fundamental_sigma=0.0001,
        tau_ticks=120.0,
        base_price=100.0,
        fundamental_value=100.0,
        impact=0.0,
    )
    defaults.update(overrides)
    return engine.InstrumentState(**defaults)


def test_impact_decays_toward_zero_over_tau() -> None:
    inst = _base_instrument(
        sigma=0.0, fundamental_sigma=0.0, kappa=0.0, impact=0.03, tau_ticks=50.0
    )
    rng = np.random.default_rng(0)
    for _ in range(500):
        result = engine.step_instrument(rng, inst, market_factor=0.0, sector_factor=0.0)
        inst = engine.InstrumentState(
            id=inst.id,
            sector_key=inst.sector_key,
            drift=inst.drift,
            sigma=inst.sigma,
            beta=inst.beta,
            gamma=inst.gamma,
            kappa=inst.kappa,
            fundamental_sigma=inst.fundamental_sigma,
            tau_ticks=inst.tau_ticks,
            base_price=result.base_price,
            fundamental_value=result.fundamental_value,
            impact=result.impact,
        )
    assert abs(inst.impact) < 1e-5


def test_mean_reversion_pulls_price_toward_fundamental() -> None:
    # Price well below fundamental, no noise: it should climb every tick.
    inst = _base_instrument(
        sigma=0.0, fundamental_sigma=0.0, kappa=0.05, base_price=50.0, fundamental_value=100.0
    )
    rng = np.random.default_rng(0)
    result = engine.step_instrument(rng, inst, market_factor=0.0, sector_factor=0.0)
    assert result.base_price > inst.base_price
    assert result.base_price < inst.fundamental_value


def test_circuit_breaker_clamps_extreme_moves() -> None:
    # A deliberately huge idiosyncratic vol should still be clamped to the cap.
    inst = _base_instrument(sigma=10.0, fundamental_sigma=0.0, kappa=0.0)
    rng = np.random.default_rng(1)
    result = engine.step_instrument(rng, inst, market_factor=0.0, sector_factor=0.0)
    log_move = math.log(result.base_price / inst.base_price)
    assert abs(log_move) <= engine.CIRCUIT_BREAKER_CAP + 1e-9
    assert result.circuit_breached is True


def test_realized_vol_matches_configured_sigma() -> None:
    """Statistical sanity check: idiosyncratic-only moves should realize ~sigma per tick."""
    inst = _base_instrument(sigma=0.001, fundamental_sigma=0.0, kappa=0.0, beta=0.0, gamma=0.0)
    rng = np.random.default_rng(7)
    log_returns = []
    price = inst.base_price
    for _ in range(20_000):
        step_inst = engine.InstrumentState(
            id=inst.id,
            sector_key=inst.sector_key,
            drift=inst.drift,
            sigma=inst.sigma,
            beta=inst.beta,
            gamma=inst.gamma,
            kappa=inst.kappa,
            fundamental_sigma=inst.fundamental_sigma,
            tau_ticks=inst.tau_ticks,
            base_price=price,
            fundamental_value=price,  # pin fundamental to price: no mean-reversion drift
            impact=0.0,
        )
        result = engine.step_instrument(rng, step_inst, market_factor=0.0, sector_factor=0.0)
        log_returns.append(math.log(result.base_price / price))
        price = result.base_price

    realized_sigma = float(np.std(log_returns))
    assert abs(realized_sigma - inst.sigma) < inst.sigma * 0.05


def test_apply_trade_impact_buy_moves_price_up_and_decays_are_separate() -> None:
    fill_price, impact_after = engine.apply_trade_impact(
        base_price=100.0,
        impact_before=0.0,
        signed_notional=10_000.0,
        liquidity=1_000_000.0,
        lambda_impact=1.0,
        max_impact=0.03,
        half_spread=0.0005,
    )
    assert impact_after > 0
    assert fill_price > 100.0


def test_apply_trade_impact_is_concave_in_size() -> None:
    """Square-root law: doubling order size multiplies impact by sqrt(2),
    not 2 -- large orders move the mark less per marginal share."""
    kwargs = dict(
        base_price=100.0,
        impact_before=0.0,
        liquidity=1_000_000.0,
        lambda_impact=0.01,
        max_impact=0.03,
        half_spread=0.0,
    )
    _, single = engine.apply_trade_impact(signed_notional=10_000.0, **kwargs)
    _, double = engine.apply_trade_impact(signed_notional=20_000.0, **kwargs)
    assert abs(double / single - math.sqrt(2)) < 1e-9


def test_apply_trade_impact_is_clamped() -> None:
    _, impact_after = engine.apply_trade_impact(
        base_price=100.0,
        impact_before=0.0,
        signed_notional=1_000_000_000.0,
        liquidity=1_000.0,
        lambda_impact=1.0,
        max_impact=0.03,
        half_spread=0.0,
    )
    assert impact_after == 0.03


def test_apply_trade_impact_sell_moves_price_down() -> None:
    fill_price, impact_after = engine.apply_trade_impact(
        base_price=100.0,
        impact_before=0.0,
        signed_notional=-10_000.0,
        liquidity=1_000_000.0,
        lambda_impact=1.0,
        max_impact=0.03,
        half_spread=0.0005,
    )
    assert impact_after < 0
    assert fill_price < 100.0
