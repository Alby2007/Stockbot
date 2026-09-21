-- Phase A (real-liquidity plan): dynamic spreads + utilization borrow fees.
--
-- Dynamic half-spread replaces the flat 5bps: wider for high-sigma and
-- illiquid names, elevated right after a circuit halt, and elevated into a
-- pending event. All coefficients live in `config` (spread.*) like the
-- existing margin.* pattern.
--
-- next_event_tick feeds the event-proximity term; last_halt_end_tick feeds
-- the post-halt decay (circuit_halted_until_tick alone can't: it's cleared
-- on resume).

ALTER TABLE instruments
    ADD COLUMN next_event_tick BIGINT,
    ADD COLUMN last_halt_end_tick BIGINT;

UPDATE instruments i SET next_event_tick = (
    SELECT MIN(e.resolve_tick) FROM events e
    WHERE e.instrument_id = i.id AND NOT e.resolved
);

INSERT INTO config (key, value) VALUES
    -- dynamic half-spread = base_bps * (1 + a*sigma/sigma_ref)
    --   * (1 + b*liquidity_ref/liquidity) * (1 + c*exp(-since_halt/H))
    --   * (1 + d*exp(-to_event/W))
    ('spread.base_bps', 5),
    ('spread.sigma_coeff', 1.0),
    ('spread.inv_liquidity_coeff', 0.5),
    ('spread.halt_coeff', 3.0),
    ('spread.event_coeff', 1.0),
    ('spread.sigma_ref', 0.0003),
    ('spread.liquidity_ref', 3000000),
    ('spread.halt_decay_ticks', 60),
    ('spread.event_window_ticks', 360),
    -- borrow fee multiplier = 1 + k * (short_interest / max_interest)^2:
    -- crowded shorts bleed superlinearly
    ('margin.borrow_util_k', 4.0);
