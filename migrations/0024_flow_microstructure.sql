-- Phase H: flow-driven microstructure.
-- instruments.adv: trailing per-tick notional volume (sliding-window SMA of
-- candles volume*close), refreshed once per tick inside apply_tick.
-- flow.* config: cross-impact coefficient, permanent-impact fraction and its
-- per-tick fundamental cap, and the ADV liquidity multiplier bounds.

ALTER TABLE instruments
    ADD COLUMN IF NOT EXISTS adv NUMERIC(24,6) NOT NULL DEFAULT 0;

-- Extend the engine-params CHECK (0013) to cover adv: a negative trailing
-- volume would corrupt the liquidity multiplier.
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
);

INSERT INTO config (key, value) VALUES
    -- H2: fraction of an instrument's bounded tick flow that bleeds into
    -- same-sector peers, scaled by the peer's gamma loading.
    ('flow.cross_impact_coeff', 0.15),
    -- H3: fraction of own bounded flow transferred into F_i (permanent,
    -- k-anchored) instead of living in the decaying impact term.
    ('flow.permanent_frac', 0.10),
    -- Hard per-tick cap on the fundamental shift (log units) so a funded
    -- coordinated group can't walk a fundamental arbitrarily fast.
    ('flow.max_fundamental_move', 0.005),
    -- H4: trailing ADV window in ticks (~5 sim days), and the clamped
    -- liquidity multiplier range. adv_ref_frac is the ADV/liquidity ratio
    -- that maps to multiplier 1.0 (measured ~2.5e-8 in the sim harness).
    ('flow.adv_window_ticks', 7200),
    ('flow.adv_mult_min', 0.5),
    ('flow.adv_mult_max', 2.0),
    ('flow.adv_ref_frac', 0.000000025)
ON CONFLICT (key) DO NOTHING;
