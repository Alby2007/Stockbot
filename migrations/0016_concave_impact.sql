-- Phase C: concave (square-root) impact + participation cap.
--
-- Impact went from lambda * N / L (linear) to
-- lambda * sign(N) * sqrt(|N| / L) in market.engine.apply_trade_impact.
-- The coefficient's meaning changed with it -- for small N/L,
-- sqrt(N/L) >> N/L, so without rescaling every trade's impact would jump
-- by orders of magnitude.
--
-- Rescale anchor: a "typical" user order is ~$500 notional. Setting
--   lambda_new = lambda_old * sqrt(500 / L)
-- makes a $500 fill produce the same impact under sqrt as under linear;
-- smaller fills pay relatively more, larger fills relatively less (the
-- intended concavity). Per-instrument because L differs per row.

UPDATE instruments
SET lambda_impact = lambda_impact * SQRT(500.0 / liquidity);

-- Single marketable fills can't exceed this fraction of an instrument's
-- liquidity in notional (split or rest a limit; resting orders fill up to
-- the cap each tick; forced liquidation closes bypass it).
INSERT INTO config (key, value) VALUES
    ('impact.participation_cap', 0.10)
ON CONFLICT (key) DO NOTHING;

-- A resting order only fills against the market maker when the mark
-- crosses its limit by this fraction -- touching isn't enough; the
-- crossing book already provides real queue priority.
INSERT INTO config (key, value) VALUES
    ('cross.trade_through_epsilon', 0.0005)
ON CONFLICT (key) DO NOTHING;
