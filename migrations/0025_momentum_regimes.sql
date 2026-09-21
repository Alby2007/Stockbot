-- Phase I1: AR(1) momentum regimes.
-- instruments.drift_state: a slowly-evolving additive drift term, stepped as
--   d_t = rho*d_{t-1} + mom.innov_frac*sigma_eff*sqrt(dt)*eps   (clipped to
--   +-mom.max_frac*sigma_eff) and applied as (drift + drift_state)*dt in
--   step_instrument. rho=0.995 -> half-life ~138 ticks, deliberately a few
--   times the seeded kappa half-lives (25-75 ticks): faster regimes would
--   overwhelm the fundamental anchor, much slower ones wash out into the
--   mean-reversion noise.

ALTER TABLE instruments
    ADD COLUMN IF NOT EXISTS drift_state NUMERIC(18,8) NOT NULL DEFAULT 0;

-- Extend the engine-params CHECK (0013/0024) to cover the new state column:
-- the engine clips it to max_frac*sigma_eff, so |drift_state| > 1 is always
-- corruption (sigma <= 1).
ALTER TABLE instruments DROP CONSTRAINT IF EXISTS instruments_engine_params_sane;
ALTER TABLE instruments ADD CONSTRAINT instruments_engine_params_sane CHECK (
    drift BETWEEN -1 AND 1
    AND sigma BETWEEN 0 AND 1
    AND beta BETWEEN -10 AND 10
    AND gamma BETWEEN -10 AND 10
    AND kappa BETWEEN 0 AND 1
    AND fundamental_sigma BETWEEN 0 AND 1
    AND liquidity BETWEEN 0.01 AND 1e18
    AND lambda_impact BETWEEN 0 AND 1e3
    AND tau_ticks BETWEEN 0.01 AND 1e9
    AND max_impact BETWEEN 0 AND 1
    AND init_margin_pct BETWEEN 0.0001 AND 1
    AND maint_margin_pct BETWEEN 0.0001 AND 1
    AND short_knockout_pct BETWEEN 0.0001 AND 0.9999
    AND float_shares >= 0
    AND adv >= 0
    AND drift_state BETWEEN -1 AND 1
);

INSERT INTO config (key, value) VALUES
    -- AR(1) persistence. 0.995 -> half-life ~138 ticks (~1/7 sim-day),
    -- a few times the seeded kappa half-lives (25-75 ticks).
    ('mom.rho', 0.995),
    -- Innovation scale as a fraction of sigma_eff per sqrt-tick. With
    -- rho=0.995 the stationary std is innov_frac/sqrt(1-rho^2) ~= 0.4x
    -- sigma_eff: momentum episodes are visible but secondary to the
    -- idiosyncratic driver.
    ('mom.innov_frac', 0.04),
    -- Hard clip on |drift_state| in units of sigma_eff so a regime can
    -- never create an unbounded trend.
    ('mom.max_frac', 1.0)
ON CONFLICT (key) DO NOTHING;
