-- Phase F: flow-responsive EWMA volatility regimes, fat tails, and live
-- circuit halts with manipulation guards.
--
-- vol_state is the per-instrument EWMA of normalized |returns| (mean ~1 at
-- baseline); sigma_eff is last tick's effective sigma (sigma_i * clipped
-- regime multiplier), written by apply_tick so fill paths read it off the
-- row instead of recomputing. NULL sigma_eff = pre-first-tick -> callers
-- COALESCE to sigma.
ALTER TABLE instruments
    ADD COLUMN vol_state NUMERIC(12, 6) NOT NULL DEFAULT 1.0,
    ADD COLUMN sigma_eff NUMERIC(14, 10);

-- Shared market-wide EWMA state, one value per tick (frozen on CLOSED
-- ticks: no returns, no information).
ALTER TABLE market_ticks
    ADD COLUMN vol_state NUMERIC(12, 6);

-- candles.vol_state snapshots the post-update EWMA per instrument per tick
-- (chartable regime history); candles.flow_ret records the bounded flow
-- component that fed it, which makes the replay vol check exact instead of
-- approximate -- the raw per-account breakdown is consumed and gone.
-- candles.model_ret is the post-step log return BEFORE this tick's fills
-- amend the candle close (replay can't recover it from close once fills
-- land), and candles.halt_kind marks flow-attributed breaches vs
-- model-driven ones.
ALTER TABLE candles
    ADD COLUMN vol_state NUMERIC(12, 6),
    ADD COLUMN flow_ret NUMERIC(14, 10),
    ADD COLUMN model_ret NUMERIC(14, 10),
    ADD COLUMN halt_kind TEXT;

-- Tick-boundary flow accumulator: every impact-moving path upserts its
-- log-impact delta here inside the per-trade instrument lock (no new
-- locks). apply_tick holds every instrument lock, so the table is
-- quiescent when it consumes the whole thing. user_id 0 = system flow
-- (liquidations); trades/crosses/shorts record the acting user for the
-- per-account vol contribution cap. signed_notional_minor feeds Phase H
-- cross-impact. tick_index is informational (the write-time mark tick).
CREATE TABLE pending_flow (
    instrument_id INT NOT NULL REFERENCES instruments (id),
    user_id BIGINT NOT NULL DEFAULT 0,
    tick_index BIGINT NOT NULL DEFAULT 0,
    delta_impact DOUBLE PRECISION NOT NULL DEFAULT 0,
    signed_notional_minor BIGINT NOT NULL DEFAULT 0,
    PRIMARY KEY (instrument_id, user_id)
);

ALTER TABLE instruments ADD CONSTRAINT instruments_vol_params_sane CHECK (
    vol_state BETWEEN 0 AND 1e9
    AND (sigma_eff IS NULL OR sigma_eff BETWEEN 0 AND 10)
);

INSERT INTO config (key, value) VALUES
    -- vol.rho: EWMA persistence (~16-tick time constant at 0.94)
    ('vol.rho', 0.94),
    -- regime multiplier clip: sigma_eff = sigma * clip(w*v_mkt + (1-w)*v_i)
    ('vol.clip_min', 0.5),
    ('vol.clip_max', 4.0),
    ('vol.market_weight', 0.5),
    -- F5.1: the impact component's contribution to the EWMA numerator and
    -- the breaker's total-return check is bounded per tick...
    ('vol.flow_ret_cap', 0.05),
    -- F5.2: ...and per account, so a lone whale cannot manufacture a halt
    -- (account cap is deliberately below any sane breaker cap).
    ('vol.account_flow_cap', 0.02),
    -- F5.4: flow-attributed breaches halt for fewer ticks than
    -- model-driven ones -- a manufactured halt is worth less than a real
    -- one.
    ('vol.flow_halt_ticks', 2);
