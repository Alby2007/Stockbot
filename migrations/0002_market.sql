-- Phase 1: instruments, the tick engine's own bookkeeping, positions/trades,
-- and per-interaction idempotency.

CREATE TABLE sectors (
    id SMALLSERIAL PRIMARY KEY,
    key TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL
);

CREATE TABLE instruments (
    id SERIAL PRIMARY KEY,
    ticker TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    sector_id SMALLINT NOT NULL REFERENCES sectors (id),

    -- Price engine parameters (see market/engine.py for the model). All
    -- per-tick, not annualized. Safe to tune at runtime with an UPDATE; the
    -- tick loop always reads fresh values.
    drift NUMERIC(14, 10) NOT NULL DEFAULT 0,          -- drift_i
    sigma NUMERIC(14, 10) NOT NULL,                    -- idiosyncratic per-tick vol
    beta NUMERIC(8, 4) NOT NULL DEFAULT 1,             -- market factor loading
    gamma NUMERIC(8, 4) NOT NULL DEFAULT 1,            -- sector factor loading
    kappa NUMERIC(10, 6) NOT NULL DEFAULT 0.02,        -- mean-reversion speed toward fundamental
    fundamental_sigma NUMERIC(14, 10) NOT NULL,        -- per-tick vol of F_i's own random walk

    liquidity NUMERIC(18, 2) NOT NULL,                 -- L_i, notional depth for impact scaling
    lambda_impact NUMERIC(10, 6) NOT NULL,             -- impact coefficient
    tau_ticks NUMERIC(10, 2) NOT NULL,                 -- impact decay time constant, in ticks
    max_impact NUMERIC(6, 4) NOT NULL DEFAULT 0.03,    -- per-trade impact clamp, e.g. 0.03 = 3%

    fundamental_value NUMERIC(18, 6) NOT NULL,         -- F_i
    base_price NUMERIC(18, 6) NOT NULL,                -- P_base, pre-impact
    impact NUMERIC(10, 6) NOT NULL DEFAULT 0,          -- current log-impact offset
    quoted_price NUMERIC(18, 6) NOT NULL,              -- cached P_base * exp(impact)

    circuit_halted_until_tick BIGINT,                  -- NULL = not halted
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT instruments_positive_prices CHECK (base_price > 0 AND quoted_price > 0 AND fundamental_value > 0)
);

-- One row per completed tick: the shared draws every instrument's move that
-- tick is derived from, plus the tick index used to derive the deterministic
-- per-tick RNG seed. Doubles as "how many ticks has the market run".
CREATE TABLE market_ticks (
    tick_index BIGINT PRIMARY KEY,
    ts TIMESTAMPTZ NOT NULL DEFAULT now(),
    market_factor NUMERIC(14, 10) NOT NULL,
    sector_factors JSONB NOT NULL
);

-- 1-minute OHLCV, one row per instrument per tick (1 tick = 1 minute).
CREATE TABLE candles (
    instrument_id INT NOT NULL REFERENCES instruments (id),
    tick_index BIGINT NOT NULL REFERENCES market_ticks (tick_index),
    open NUMERIC(18, 6) NOT NULL,
    high NUMERIC(18, 6) NOT NULL,
    low NUMERIC(18, 6) NOT NULL,
    close NUMERIC(18, 6) NOT NULL,
    volume NUMERIC(18, 2) NOT NULL DEFAULT 0,
    PRIMARY KEY (instrument_id, tick_index)
);

CREATE INDEX candles_instrument_tick_idx ON candles (instrument_id, tick_index DESC);

-- Long-only in Phase 1: quantity is always >= 0. Shorts arrive in Phase 1.5+.
CREATE TABLE positions (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users (id),
    instrument_id INT NOT NULL REFERENCES instruments (id),
    quantity BIGINT NOT NULL DEFAULT 0,
    avg_cost NUMERIC(18, 6) NOT NULL DEFAULT 0,   -- per-share cost basis, currency major units
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id, instrument_id),
    CONSTRAINT positions_quantity_nonneg CHECK (quantity >= 0)
);

CREATE TABLE trades (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users (id),
    instrument_id INT NOT NULL REFERENCES instruments (id),
    side TEXT NOT NULL CHECK (side IN ('BUY', 'SELL')),
    quantity BIGINT NOT NULL CHECK (quantity > 0),
    fill_price NUMERIC(18, 6) NOT NULL,
    notional_minor BIGINT NOT NULL,               -- cash leg, minor units, before fee
    fee_minor BIGINT NOT NULL,
    cash_transfer_id UUID NOT NULL,
    fee_transfer_id UUID,                          -- NULL only if fee rounds to 0
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX trades_user_id_idx ON trades (user_id, created_at DESC);
CREATE INDEX trades_instrument_id_idx ON trades (instrument_id, created_at DESC);

-- Discord interaction ids are unique per click; replaying one (retry, double
-- click) must be a no-op, not a double trade.
CREATE TABLE idempotency_keys (
    interaction_id TEXT PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
