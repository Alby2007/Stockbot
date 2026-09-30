-- Competition: equal-stake duels, winner-take-all liquidation bounties,
-- weekly division seasons, and the trade-scope pin.
--
-- * GAME_ESCROW is one shared escrow account for duel stakes and bounty
--   pots (ledger reasons distinguish the flows; the audit's sum-zero
--   invariant covers it like IPO_ESCROW).
-- * seasons.duel_id / division_tier mark private competitive seasons --
--   same privacy-marker trick as sandbox_user_id: public-surface queries
--   (get_open_season, get_latest_season, standings lists, /league join)
--   exclude them, but get_active_entry keeps them so `league:True`
--   remains their only trade surface.
-- * trade_scope pins which ACTIVE season `league:True` routes to; absent
--   row = auto (most recent entry). Duel accept auto-pins both players;
--   pins die with their season at close.
-- * duels.escrowed stakes are real cash at risk; league stakes inside a
--   duel season are equal FAUCET grants (quarantined like every league).

INSERT INTO accounts (kind, system_name, balance)
SELECT 'SYSTEM', 'GAME_ESCROW', 0
WHERE NOT EXISTS (SELECT 1 FROM accounts WHERE system_name = 'GAME_ESCROW');

CREATE TABLE duels (
    id BIGSERIAL PRIMARY KEY,
    challenger_id BIGINT NOT NULL REFERENCES users (id),
    opponent_id BIGINT NOT NULL REFERENCES users (id),
    stake_minor BIGINT NOT NULL CHECK (stake_minor > 0),
    window_ticks BIGINT NOT NULL CHECK (window_ticks > 0),
    status TEXT NOT NULL DEFAULT 'OPEN'
        CHECK (status IN ('OPEN', 'ACTIVE', 'SETTLED', 'DECLINED',
                          'CANCELLED', 'EXPIRED')),
    created_tick BIGINT NOT NULL,
    expires_tick BIGINT NOT NULL,
    accepted_tick BIGINT,
    end_tick BIGINT,
    season_id BIGINT REFERENCES seasons (id),
    winner_id BIGINT REFERENCES users (id),
    payout_minor BIGINT,
    forfeited_by BIGINT REFERENCES users (id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX duels_open_expiry ON duels (expires_tick) WHERE status = 'OPEN';
CREATE INDEX duels_players ON duels (challenger_id, opponent_id, status);

ALTER TABLE seasons ADD COLUMN duel_id BIGINT REFERENCES duels (id);
ALTER TABLE seasons ADD COLUMN division_tier INT;
-- A season belongs to at most one competition context.
ALTER TABLE seasons ADD CONSTRAINT seasons_one_context CHECK (
    (duel_id IS NULL OR division_tier IS NULL)
    AND (duel_id IS NULL OR sandbox_user_id IS NULL)
    AND (division_tier IS NULL OR sandbox_user_id IS NULL)
);

CREATE TABLE bounties (
    id BIGSERIAL PRIMARY KEY,
    poster_id BIGINT NOT NULL REFERENCES users (id),
    target_id BIGINT NOT NULL REFERENCES users (id),
    amount_minor BIGINT NOT NULL CHECK (amount_minor > 0),
    condition TEXT NOT NULL DEFAULT 'LIQUIDATED'
        CHECK (condition IN ('LIQUIDATED')),
    status TEXT NOT NULL DEFAULT 'OPEN'
        CHECK (status IN ('OPEN', 'CLAIMED', 'CANCELLED', 'EXPIRED')),
    created_tick BIGINT NOT NULL,
    expires_tick BIGINT NOT NULL,
    claimed_by BIGINT REFERENCES users (id),
    claimed_tick BIGINT,
    paid_minor BIGINT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX bounties_target_open ON bounties (target_id) WHERE status = 'OPEN';
CREATE INDEX bounties_expiry ON bounties (expires_tick) WHERE status = 'OPEN';

CREATE TABLE division_memberships (
    user_id BIGINT PRIMARY KEY REFERENCES users (id),
    tier INT NOT NULL CHECK (tier >= 1),
    active BOOLEAN NOT NULL DEFAULT TRUE,
    joined_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE trade_scope (
    user_id BIGINT PRIMARY KEY REFERENCES users (id),
    season_id BIGINT NOT NULL REFERENCES seasons (id),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE notifications DROP CONSTRAINT notifications_kind_check;
ALTER TABLE notifications ADD CONSTRAINT notifications_kind_check
    CHECK (kind IN ('LIQUIDATION', 'KNOCKOUT', 'ORDER_FILLED', 'SEASON_RESULT',
                    'ACCOUNT_SUSPENDED', 'BADGE_EARNED', 'IPO_SETTLED',
                    'MARGIN_CALL', 'SHORT_RECALL', 'ALERT_TRIGGERED',
                    'QUEST_COMPLETED', 'OPTION_SETTLED', 'MOMENT_EARNED',
                    'TRADE_OFFER', 'TRADE_RESULT',
                    'ENTITLEMENT_EXPIRING', 'GIFT_RECEIVED',
                    'DUEL_OFFER', 'DUEL_RESULT',
                    'BOUNTY_PLACED', 'BOUNTY_PAID', 'BOUNTY_EXPIRED',
                    'DIVISION_RESULT'));

INSERT INTO config (key, value) VALUES
    ('duels.enabled', 1),
    ('duel.min_stake_minor', 100),        -- $1.00 floor on the escrowed wager
    ('duel.max_stake_minor', 50000),      -- $500 cap per side
    ('duel.rake_pct', 0.05),
    ('duel.league_stake_minor', 100000),  -- equal FAUCET trading stake
    ('duel.offer_ttl_ticks', 1440),       -- 1 day to accept
    ('duel.max_active', 3),               -- per player, OPEN+ACTIVE combined
    ('bounties.enabled', 1),
    ('bounty.min_amount_minor', 100),
    ('bounty.fee_minor', 100),            -- burned at post: hits aren't free
    ('bounty.rake_pct', 0.05),
    ('bounty.ttl_ticks', 10080),          -- one week
    ('bounty.max_open_per_target', 3),
    ('divisions.enabled', 1),
    ('division.week_ticks', 10080),
    ('division.stake_minor', 100000),
    ('division.move_count', 2),           -- top/bottom K per tier per week
    ('division.min_trades', 1),           -- under this -> relegation zone
    ('division.max_tier', 3);             -- new members start at the bottom
