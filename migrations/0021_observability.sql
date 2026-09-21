-- Observability batch: tick telemetry, periodic invariant audit,
-- service heartbeats, per-command stats, kill switches, fill provenance.

-- Per-tick counters on the existing one-row-per-tick table: "the market
-- went weird at tick 4532" becomes one SELECT.
ALTER TABLE market_ticks
    ADD COLUMN duration_ms NUMERIC(10,3),
    ADD COLUMN fills INT NOT NULL DEFAULT 0,
    ADD COLUMN crosses INT NOT NULL DEFAULT 0,
    ADD COLUMN stops_triggered INT NOT NULL DEFAULT 0,
    ADD COLUMN knockouts INT NOT NULL DEFAULT 0,
    ADD COLUMN liquidations INT NOT NULL DEFAULT 0,
    ADD COLUMN events_resolved INT NOT NULL DEFAULT 0;

-- Invariant checks run inside the tick loop every audit.every_n_ticks
-- and persist here; log.critical fires on drift.
CREATE TABLE audit_results (
    id BIGSERIAL PRIMARY KEY,
    tick_index BIGINT NOT NULL,
    check_name TEXT NOT NULL,
    ok BOOLEAN NOT NULL,
    detail TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX audit_results_tick_idx ON audit_results (tick_index);

-- Process liveness: market upserts every tick, bot on an interval.
-- "Alive" means "heartbeat younger than two intervals", which
-- restart: unless-stopped alone can't see (wedged-but-running).
CREATE TABLE service_heartbeats (
    service TEXT PRIMARY KEY,
    beat_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    detail JSONB NOT NULL DEFAULT '{}'::jsonb
);

-- Per-command latency + outcome counters ("which commands fail most").
CREATE TABLE command_stats (
    id BIGSERIAL PRIMARY KEY,
    command TEXT NOT NULL,
    ok BOOLEAN NOT NULL,
    duration_ms NUMERIC(10,3) NOT NULL,
    error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX command_stats_command_idx ON command_stats (command, created_at);

-- Kill switches: service entry points check these so an exploit or a
-- poisoned path can be disabled with one UPDATE instead of a deploy.
INSERT INTO config (key, value) VALUES
    ('trading.enabled', 1),
    ('orders.enabled', 1),
    ('shorts.enabled', 1),
    ('audit.every_n_ticks', 60);

-- Fill provenance: "why did this fill cost X" becomes a query rather
-- than a re-simulation.
ALTER TABLE trades
    ADD COLUMN half_spread NUMERIC(12,8),
    ADD COLUMN impact_delta NUMERIC(14,10);
