-- Phase E: trading sessions. Ticks form an open/closed cycle derived
-- purely from tick_index: the first `session.open_ticks` of each
-- (open + closed) cycle are the trading session. 960 open + 480 closed
-- = a 1440-tick day that is open 16h, closed 8h -- the overnight-gap
-- mechanic without making the game unplayable.
ALTER TABLE market_ticks
    ADD COLUMN session_state TEXT NOT NULL DEFAULT 'OPEN';

INSERT INTO config (key, value) VALUES
    ('session.open_ticks', 960),
    ('session.closed_ticks', 480),
    ('session.phase_offset_ticks', 0),
    -- Fraction of accumulated trade impact retained at the open (the rest
    -- is assumed to have been absorbed by the book overnight).
    ('session.open_impact_reset', 0.5);
