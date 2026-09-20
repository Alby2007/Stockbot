-- Earnings calendar + delayed news, per the "learnable structure" section of
-- the design doc: these are the two cheap sources of tradeable information
-- asymmetry that keep the market from being a pure coin flip.

CREATE TABLE events (
    id BIGSERIAL PRIMARY KEY,
    instrument_id INT NOT NULL REFERENCES instruments (id),
    kind TEXT NOT NULL CHECK (kind IN ('EARNINGS', 'NEWS')),
    scheduled_tick BIGINT NOT NULL,  -- when this became publicly known (posted to /calendar or /news)
    resolve_tick BIGINT NOT NULL,    -- when the price effect actually lands
    headline TEXT,                  -- NEWS only: the public hint text
    magnitude NUMERIC(10, 6),        -- log-return jump applied to the fundamental at resolution;
                                      -- for NEWS this is decided at creation but only revealed once resolved
    resolved BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX events_resolve_idx ON events (resolve_tick) WHERE NOT resolved;
CREATE INDEX events_instrument_idx ON events (instrument_id, kind, resolved);
