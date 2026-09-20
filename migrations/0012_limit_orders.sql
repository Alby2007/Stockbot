-- Limit orders. An order rests until the impact-adjusted executable price
-- satisfies its limit, evaluated once per tick inside apply_tick. Orders do
-- not reserve cash or shares at placement; executable price is checked at
-- fill time and orders that can't fill stay open (price may come back).

CREATE TABLE orders (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users (id),
    instrument_id INT NOT NULL REFERENCES instruments (id),
    season_id BIGINT REFERENCES seasons (id),      -- NULL = main portfolio
    side TEXT NOT NULL CHECK (side IN ('BUY', 'SELL')),
    quantity BIGINT NOT NULL CHECK (quantity > 0),
    limit_price NUMERIC(18, 6) NOT NULL CHECK (limit_price > 0),
    status TEXT NOT NULL DEFAULT 'OPEN'
        CHECK (status IN ('OPEN', 'FILLED', 'CANCELLED', 'EXPIRED')),
    opened_tick BIGINT,
    filled_tick BIGINT,
    fill_price NUMERIC(18, 6),
    expires_tick BIGINT,                           -- NULL = good-til-cancelled
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- the tick-time matcher scans OPEN orders by instrument
CREATE INDEX orders_open_idx ON orders (instrument_id) WHERE status = 'OPEN';
CREATE INDEX orders_user_idx ON orders (user_id, created_at DESC);
