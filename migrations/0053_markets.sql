-- R1: regional-market tenancy plumbing. One `markets` row (US) seeded
-- with today's exact session.* values; every instrument backfilled to
-- it, so this migration alone changes no behaviour. `overnight_var_ticks`
-- is the venue's absolute stochastic-gap horizon in ticks (the quantity
-- F3 holds constant as sessions shorten): today 480 * 0.125 = 60.
-- `tick_size` is the venue's minimum quote grid (was spread.tick_min);
-- `auction_ticks` the venue's closing-auction window.

CREATE TABLE markets (
    id                  SERIAL PRIMARY KEY,
    code                TEXT NOT NULL UNIQUE,
    name                TEXT NOT NULL,
    open_ticks          INT NOT NULL CHECK (open_ticks > 0),
    closed_ticks        INT NOT NULL CHECK (closed_ticks >= 0),
    offset_ticks        INT NOT NULL DEFAULT 0,
    tick_size           DOUBLE PRECISION NOT NULL CHECK (tick_size > 0),
    auction_ticks       INT NOT NULL DEFAULT 30 CHECK (auction_ticks >= 0),
    overnight_var_ticks DOUBLE PRECISION CHECK (overnight_var_ticks > 0),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO markets
    (code, name, open_ticks, closed_ticks, offset_ticks,
     tick_size, auction_ticks, overnight_var_ticks)
SELECT 'US', 'US Exchange',
       (SELECT value FROM config WHERE key = 'session.open_ticks')::INT,
       (SELECT value FROM config WHERE key = 'session.closed_ticks')::INT,
       COALESCE(
           (SELECT value FROM config WHERE key = 'session.phase_offset_ticks'),
           0)::INT,
       COALESCE((SELECT value FROM config WHERE key = 'spread.tick_min'), 0.01),
       COALESCE(
           (SELECT value FROM config WHERE key = 'session.auction_ticks'),
           30)::INT,
       -- Preserve today's gap variance as an absolute tick horizon.
       (SELECT value FROM config WHERE key = 'session.closed_ticks')
           * COALESCE(
               (SELECT value FROM config WHERE key = 'session.overnight_var_frac'),
               0.125);

ALTER TABLE instruments ADD COLUMN market_id INT REFERENCES markets (id);
UPDATE instruments SET market_id = (SELECT id FROM markets WHERE code = 'US');
ALTER TABLE instruments ALTER COLUMN market_id SET NOT NULL;

-- F1: the phase moves onto candles (per instrument, per tick). With one
-- market every candle matches market_ticks.session_state; pre-history
-- candles with no market_ticks row default OPEN.
ALTER TABLE candles ADD COLUMN session_state TEXT;
UPDATE candles c
SET session_state = COALESCE(
    (SELECT mt.session_state FROM market_ticks mt
     WHERE mt.tick_index = c.tick_index),
    'OPEN');
ALTER TABLE candles ALTER COLUMN session_state SET DEFAULT 'OPEN';
ALTER TABLE candles ALTER COLUMN session_state SET NOT NULL;
ALTER TABLE candles ADD CONSTRAINT candles_session_state_check
    CHECK (session_state IN ('OPEN', 'CLOSED'));
