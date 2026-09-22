-- Plan A: flow-responsive market making.
--
-- instruments.flow_skew is a decaying EWMA of each instrument's bounded
-- own-flow (the same clipped log-return flow that feeds the vol state),
-- written per tick by apply_tick. Sign tells fill paths which side the
-- crowd is on; fills on the crowded side pay up to flow.skew_coeff extra
-- half-spread (normalized by flow.skew_norm), the contra side is
-- discounted -- the MM pricing adverse selection instead of quoting
-- symmetric into a one-sided tape.
ALTER TABLE instruments
    ADD COLUMN flow_skew NUMERIC(14, 10) NOT NULL DEFAULT 0;

INSERT INTO config (key, value) VALUES
    -- flow.vol_liq_coeff: exponent on 1/max(1, vol_state) scaling
    -- effective liquidity -- storms thin the book, not just widen the
    -- spread. Lives in the flow.* namespace because every fill path
    -- already merges flow_config into its spread cfg.
    ('flow.vol_liq_coeff', 1.0),
    -- flow.skew_*: adverse-selection skew on the half-spread.
    -- mult = 1 + skew_coeff * sign(trade) * (flow_skew / skew_norm),
    -- clipped to [0, skew_max].
    ('flow.skew_coeff', 0.5),
    ('flow.skew_decay', 0.9),
    ('flow.skew_norm', 0.05),
    ('flow.skew_max', 3.0)
ON CONFLICT (key) DO NOTHING;
