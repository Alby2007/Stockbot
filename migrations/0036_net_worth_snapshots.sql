-- Phase N2: daily net-worth snapshots for every main USER account, so
-- /compare and /leaderboard can show a real 24h change instead of "n/a"
-- forever. Mirrors seasons' equity_snapshots (same day-boundary cadence,
-- same INSERT ... SELECT over all accounts at once rather than a loop),
-- but scoped to the persistent main portfolio (season_id IS NULL) instead
-- of one season's league accounts.
CREATE TABLE net_worth_snapshots (
    user_id BIGINT NOT NULL REFERENCES users (id),
    day_index BIGINT NOT NULL,
    tick_index BIGINT NOT NULL,
    equity_minor BIGINT NOT NULL,
    PRIMARY KEY (user_id, day_index)
);

CREATE INDEX net_worth_snapshots_user_idx
    ON net_worth_snapshots (user_id, day_index DESC);
