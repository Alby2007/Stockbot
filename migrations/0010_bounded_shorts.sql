-- Phase 1.5: bounded shorts. A defined-risk bearish instrument: collateral
-- Q*P*m is posted upfront, payoff is Q*(entry - exit) floored at -collateral,
-- and the position auto-closes (knockout) if price touches entry*(1+m).
-- Negative equity is structurally impossible -- no margin calls, no
-- liquidation engine. True margin shorts arrive in Phase 2.

ALTER TABLE instruments
    ADD COLUMN short_knockout_pct NUMERIC(6, 4) NOT NULL DEFAULT 0.25;

CREATE TABLE bounded_shorts (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users (id),
    instrument_id INT NOT NULL REFERENCES instruments (id),
    season_id BIGINT REFERENCES seasons (id),      -- NULL = main portfolio
    quantity BIGINT NOT NULL CHECK (quantity > 0),
    entry_price NUMERIC(18, 6) NOT NULL,           -- impact-adjusted fill at open
    knockout_price NUMERIC(18, 6) NOT NULL,        -- entry_price * (1 + m)
    collateral_minor BIGINT NOT NULL CHECK (collateral_minor > 0),
    fee_minor BIGINT NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'OPEN'
        CHECK (status IN ('OPEN', 'CLOSED', 'KNOCKED_OUT')),
    opened_tick BIGINT,
    closed_tick BIGINT,
    close_price NUMERIC(18, 6),
    payout_minor BIGINT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX bounded_shorts_open_idx
    ON bounded_shorts (instrument_id) WHERE status = 'OPEN';
CREATE INDEX bounded_shorts_user_idx ON bounded_shorts (user_id, created_at DESC);
