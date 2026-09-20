-- Records which market tick each trade happened in (needed to detect
-- same-tick opposing trades) and a table for the wash-trade anomaly job's
-- output.

ALTER TABLE trades ADD COLUMN tick_index BIGINT;
CREATE INDEX trades_instrument_tick_idx ON trades (instrument_id, tick_index);

CREATE TABLE wash_trade_flags (
    id BIGSERIAL PRIMARY KEY,
    buy_trade_id BIGINT NOT NULL REFERENCES trades (id),
    sell_trade_id BIGINT NOT NULL REFERENCES trades (id),
    instrument_id INT NOT NULL REFERENCES instruments (id),
    buyer_id BIGINT NOT NULL REFERENCES users (id),
    seller_id BIGINT NOT NULL REFERENCES users (id),
    tick_index BIGINT NOT NULL,
    detected_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (buy_trade_id, sell_trade_id)
);
