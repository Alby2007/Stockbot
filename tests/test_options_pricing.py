"""Option pricing core (Phase O1): OU-consistent variance + BS bounds."""

import math

import pytest

from stockbot.options.pricing import (
    CALL,
    PUT,
    VolBlend,
    bs_delta,
    bs_price,
    total_variance,
)

# NORT's seeded params (migrations/0003): the calibration the C1 review
# table was computed against. These numbers pin the closed form -- a
# regression here means options went back to being priced ~6x too rich.
NORT_SIGMA = 1.778049e-4
NORT_KAPPA = 0.02873
NORT_FSIGMA = 2.66707e-5
VOL_CFG = VolBlend(sigma=NORT_SIGMA, rho=0.94)


def test_total_variance_matches_model_consistent_table() -> None:
    # (expiry ticks, expected sigma_T from the revised plan's C1 table).
    expected = {
        1440: 0.00125,  # 1d
        4320: 0.00190,  # 3d
        10080: 0.00278,  # 7d
        20160: 0.00386,  # 14d
        43200: 0.00559,  # 30d
    }
    for ticks, want in expected.items():
        var = total_variance(NORT_SIGMA, NORT_KAPPA, NORT_FSIGMA, ticks, VOL_CFG)
        assert math.sqrt(var) == pytest.approx(want, rel=0.02)
        # ...and nowhere near the naive sigma*sqrt(T) that the closed
        # form replaced (the 5-7x overpricing bug this pins against).
        assert math.sqrt(var) < 0.25 * NORT_SIGMA * math.sqrt(ticks)


def test_variance_not_linear_in_t() -> None:
    # As T -> inf the OU term saturates at sigma^2/2k and growth is
    # dominated by the fundamental walk -- a sigma^2*T law diverges.
    var_30d = total_variance(NORT_SIGMA, NORT_KAPPA, NORT_FSIGMA, 43200, VOL_CFG)
    var_300d = total_variance(NORT_SIGMA, NORT_KAPPA, NORT_FSIGMA, 432000, VOL_CFG)
    assert var_300d < 12 * var_30d  # ~10x T -> ~10x var, all fundamental


def test_sigma_eff_blend_decays() -> None:
    # A hot regime (sigma_eff = 4x) lifts short-dated variance, but the
    # EWMA blend washes it out over a 30d horizon.
    hot = total_variance(4 * NORT_SIGMA, NORT_KAPPA, NORT_FSIGMA, 1440, VOL_CFG)
    calm = total_variance(NORT_SIGMA, NORT_KAPPA, NORT_FSIGMA, 1440, VOL_CFG)
    assert hot > calm
    hot_30d = total_variance(4 * NORT_SIGMA, NORT_KAPPA, NORT_FSIGMA, 43200, VOL_CFG)
    calm_30d = total_variance(NORT_SIGMA, NORT_KAPPA, NORT_FSIGMA, 43200, VOL_CFG)
    assert hot_30d < calm_30d * 1.05


def test_put_call_parity() -> None:
    for strike in (50.0, 59.93, 75.0):
        var = total_variance(NORT_SIGMA, NORT_KAPPA, NORT_FSIGMA, 10080, VOL_CFG)
        c = bs_price(CALL, 59.93, strike, var)
        p = bs_price(PUT, 59.93, strike, var)
        assert c - p == pytest.approx(59.93 - strike, abs=1e-9)


def test_price_bounds() -> None:
    var = total_variance(NORT_SIGMA, NORT_KAPPA, NORT_FSIGMA, 10080, VOL_CFG)
    assert 0.0 <= bs_price(CALL, 59.93, 60.0, var) <= 59.93
    assert 0.0 <= bs_price(PUT, 59.93, 60.0, var) <= 60.0


def test_monotonic_in_variance_and_expiry() -> None:
    v1 = total_variance(NORT_SIGMA, NORT_KAPPA, NORT_FSIGMA, 1440, VOL_CFG)
    v2 = total_variance(NORT_SIGMA, NORT_KAPPA, NORT_FSIGMA, 10080, VOL_CFG)
    for side in (CALL, PUT):
        assert bs_price(side, 59.93, 60.0, v2) > bs_price(side, 59.93, 60.0, v1)
        assert bs_price(side, 59.93, 60.0, 2 * v1) > bs_price(side, 59.93, 60.0, v1)


def test_expired_is_intrinsic() -> None:
    assert bs_price(CALL, 65.0, 60.0, 0.0) == 5.0
    assert bs_price(PUT, 65.0, 60.0, 0.0) == 0.0
    assert total_variance(NORT_SIGMA, NORT_KAPPA, NORT_FSIGMA, 0, VOL_CFG) == 0.0


def test_delta_bounds_and_limits() -> None:
    var = total_variance(NORT_SIGMA, NORT_KAPPA, NORT_FSIGMA, 10080, VOL_CFG)
    assert 0.0 < bs_delta(CALL, 59.93, 60.0, var) < 1.0
    assert -1.0 < bs_delta(PUT, 59.93, 60.0, var) < 0.0
    assert bs_delta(CALL, 65.0, 60.0, 0.0) == 1.0
    assert bs_delta(PUT, 65.0, 60.0, 0.0) == 0.0


def test_mm_edge_calibration() -> None:
    # D3: charging fee-on-premium at buy + markdown at sell, nothing at
    # settlement. Hold-to-expiry edge = markup/(1+markup) = 9.1% of
    # premium at 0.10; round-trip edge = 1-(1-md)/(1+mu) = 20% at
    # 0.10/0.12. Per-delta cost comparability with spot is deliberately
    # NOT chased -- options are cheap per unit of exposure; position
    # limits carry that risk.
    markup, markdown = 0.10, 0.12
    model = bs_price(
        CALL,
        59.93,
        60.0,
        total_variance(NORT_SIGMA, NORT_KAPPA, NORT_FSIGMA, 10080, VOL_CFG),
    )
    premium = model * (1 + markup)
    buyback = model * (1 - markdown)
    assert (premium - model) / premium == pytest.approx(0.0909, abs=1e-3)
    assert 1 - buyback / premium == pytest.approx(0.20, abs=1e-3)
