-- Bound every runtime-tunable engine parameter. `/admin tune` validates
-- against admin.service.PARAM_BOUNDS, but this CHECK is the schema-level
-- backstop so a direct UPDATE can't brick the market either: tau_ticks=0
-- -> exp(-dt/0) in step_instrument every tick, liquidity=0 -> division by
-- zero in apply_trade_impact on every trade, and negative sigma/kappa or
-- a knockout/margin pct outside its domain silently corrupt the model.
--
-- Finite BETWEEN bounds (not `> 0` alone) deliberately also reject NaN and
-- +/-Infinity: Postgres numeric comparisons treat NaN as greater than
-- every other value, so NaN would pass a naive `>= 0` check.
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
);
