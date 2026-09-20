-- Phase 1: Seasons + League. A season is an opt-in, time-boxed league with
-- an entry fee (sink) and a fixed, equal, non-withdrawable stake per
-- entrant. League money lives in LEAGUE accounts and league positions are
-- scoped by season_id, so it can never leak into a user's main portfolio.
-- The main portfolio is never reset; the league is the competitive track.

CREATE TABLE seasons (
    id BIGSERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'SCHEDULED'
        CHECK (status IN ('SCHEDULED', 'ACTIVE', 'CLOSED')),
    start_tick BIGINT NOT NULL,
    end_tick BIGINT NOT NULL CHECK (end_tick > start_tick),
    entry_fee_minor BIGINT NOT NULL CHECK (entry_fee_minor >= 0),
    stake_minor BIGINT NOT NULL CHECK (stake_minor > 0),
    prize_pool_minor BIGINT NOT NULL DEFAULT 0,  -- filled at close: fees collected
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- accounts.season_id scopes an account to a season. Only LEAGUE accounts may
-- have one; USER and SYSTEM accounts must keep it NULL.
ALTER TABLE accounts ADD COLUMN season_id BIGINT REFERENCES seasons (id);
ALTER TABLE accounts DROP CONSTRAINT accounts_user_xor_system;
ALTER TABLE accounts ADD CONSTRAINT accounts_kind_shape CHECK (
    (kind = 'USER' AND user_id IS NOT NULL AND system_name IS NULL AND season_id IS NULL)
    OR (kind = 'SYSTEM' AND user_id IS NULL AND system_name IS NOT NULL AND season_id IS NULL)
    OR (kind = 'LEAGUE' AND user_id IS NOT NULL AND system_name IS NULL AND season_id IS NOT NULL)
);
ALTER TABLE accounts DROP CONSTRAINT accounts_balance_nonneg_for_user;
ALTER TABLE accounts ADD CONSTRAINT accounts_balance_nonneg_for_user
    CHECK (kind = 'SYSTEM' OR balance >= 0);
CREATE UNIQUE INDEX accounts_league_user_season_key
    ON accounts (user_id, season_id) WHERE kind = 'LEAGUE';

CREATE TABLE season_entries (
    season_id BIGINT NOT NULL REFERENCES seasons (id),
    user_id BIGINT NOT NULL REFERENCES users (id),
    account_id BIGINT NOT NULL UNIQUE REFERENCES accounts (id),
    joined_tick BIGINT NOT NULL,
    trades_count INT NOT NULL DEFAULT 0,
    final_score NUMERIC(24, 10),
    final_rank INT,
    prize_minor BIGINT NOT NULL DEFAULT 0,
    PRIMARY KEY (season_id, user_id)
);

-- One row per entrant per league day; the Sharpe-like score is computed
-- from daily equity changes at season close.
CREATE TABLE equity_snapshots (
    season_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    day_index INT NOT NULL,
    tick_index BIGINT NOT NULL,
    equity_minor BIGINT NOT NULL,
    PRIMARY KEY (season_id, user_id, day_index),
    FOREIGN KEY (season_id, user_id)
        REFERENCES season_entries (season_id, user_id)
);

-- League positions are scoped by season_id; NULL = the persistent main
-- portfolio. NULLS NOT DISTINCT treats NULL as a real key value so a user's
-- main position stays unique on (user_id, instrument_id).
ALTER TABLE positions ADD COLUMN season_id BIGINT REFERENCES seasons (id);
ALTER TABLE positions DROP CONSTRAINT positions_user_id_instrument_id_key;
ALTER TABLE positions ADD CONSTRAINT positions_user_instrument_season_key
    UNIQUE NULLS NOT DISTINCT (user_id, instrument_id, season_id);

ALTER TABLE trades ADD COLUMN season_id BIGINT REFERENCES seasons (id);

-- Season trophies are permanent entitlements, not purchasable items.
ALTER TABLE shop_items DROP CONSTRAINT shop_items_kind_check;
ALTER TABLE shop_items ADD CONSTRAINT shop_items_kind_check
    CHECK (kind IN ('SLOT', 'ANALYST_TOOL', 'COSMETIC', 'TROPHY'));
