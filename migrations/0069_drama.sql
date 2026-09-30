-- Phase 2 game layer: parimutuel props, scheduled chaos events, NPC
-- personas, and hidden badges.
--
-- * props are binary parimutuel contracts: every bet escrows into
--   GAME_ESCROW (shared with duels/bounties; ledger reasons split the
--   flows). Winners split the WHOLE pool pro-rata by stake minus rake --
--   no market-maker exposure, the losing side funds the winning side.
--   INDEX_BEAT resolution needs both legs' marks captured at creation:
--   meta.start_a/start_b, compared against marks at resolve_tick.
-- * chaos_events are the state for scheduled regime shocks. ACTIVE rows
--   are read at consume time (chaos.active_multiplier) so multipliers
--   revert by expiring the row -- no config write-back to go stale.
--   FLASH_CRASH applies its mark shift once at activation; VOL_STORM and
--   FEE_SURGE multipliers apply for the whole active window.
-- * npc_agents.is_persona marks openly-named antagonists (Hedge Fund
--   Harry): they skip permadeath (a boss isn't allowed to bleed out),
--   are the ONLY bot-kind bounty targets, and render by display_name in
--   feed/DM payloads instead of a useless synthetic <@id> mention. The
--   anonymous population stays anonymous -- personas are bosses, not
--   tape prints.
-- * shop_items.metadata->>'hidden' hides a badge from the /shop catalog;
--   grants and profile trophies are unaffected (discovery content).

CREATE TABLE props (
    id BIGSERIAL PRIMARY KEY,
    title TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('PRICE_ABOVE', 'INDEX_BEAT')),
    instrument_id INT REFERENCES instruments (id),
    instrument_b_id INT REFERENCES instruments (id),
    threshold NUMERIC(18, 6),
    meta JSONB NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'OPEN'
        CHECK (status IN ('OPEN', 'RESOLVED', 'CANCELLED')),
    outcome TEXT CHECK (outcome IN ('YES', 'NO')),
    created_tick BIGINT NOT NULL,
    resolve_tick BIGINT NOT NULL,
    pool_yes_minor BIGINT NOT NULL DEFAULT 0,
    pool_no_minor BIGINT NOT NULL DEFAULT 0,
    auto BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX props_resolve ON props (resolve_tick) WHERE status = 'OPEN';

CREATE TABLE prop_bets (
    id BIGSERIAL PRIMARY KEY,
    prop_id BIGINT NOT NULL REFERENCES props (id) ON DELETE CASCADE,
    user_id BIGINT NOT NULL REFERENCES users (id),
    side TEXT NOT NULL CHECK (side IN ('YES', 'NO')),
    amount_minor BIGINT NOT NULL CHECK (amount_minor > 0),
    paid_minor BIGINT,
    created_tick BIGINT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (prop_id, user_id, side)
);
CREATE INDEX prop_bets_user ON prop_bets (user_id, created_at DESC);

CREATE TABLE chaos_events (
    id BIGSERIAL PRIMARY KEY,
    kind TEXT NOT NULL
        CHECK (kind IN ('FLASH_CRASH', 'VOL_STORM', 'FEE_SURGE')),
    status TEXT NOT NULL DEFAULT 'SCHEDULED'
        CHECK (status IN ('SCHEDULED', 'ACTIVE', 'EXPIRED')),
    start_tick BIGINT NOT NULL,
    end_tick BIGINT NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}',   -- pct for FLASH_CRASH; mult for storms
    auto BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX chaos_start ON chaos_events (start_tick) WHERE status = 'SCHEDULED';
CREATE INDEX chaos_end ON chaos_events (end_tick) WHERE status = 'ACTIVE';

ALTER TABLE npc_agents
    ADD COLUMN is_persona BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN display_name TEXT;

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
                    'DIVISION_RESULT',
                    'PROP_RESOLVED', 'BOSS_BEATEN'));

INSERT INTO config (key, value) VALUES
    ('props.enabled', 1),
    ('props.min_bet_minor', 100),          -- $1.00 minimum bet
    ('props.max_bet_minor', 50000),        -- $500 per side per prop
    ('props.rake_pct', 0.05),
    ('props.window_ticks', 10080),         -- one week resolution for auto props
    ('props.autogen', 1),                  -- weekly auto-generated prop
    ('chaos.enabled', 1),
    ('chaos.auto_prob', 0.20),             -- per-day chance the scheduler fires
    ('chaos.lead_ticks', 2880),            -- auto events land ~2 days out
    ('chaos.crash_pct', 0.03),             -- flash crash mark shift
    ('chaos.vol_mult', 2.0),               -- vol-storm sigma multiplier
    ('chaos.fee_mult', 2.0),               -- fee-surge borrow multiplier
    ('chaos.storm_ticks', 1440),           -- storm/fee windows last a day
    ('chaos.mcm_enabled', 1),              -- Margin Call Monday
    ('chaos.mcm_day_mod', 4),              -- day_index % 7 == this -> fee surge
    ('personas.enabled', 1);

-- Hidden competition badges: metadata.hidden marks them secret -- the
-- catalog never renders them before earning (badges are grant-only and
-- invisible in /shop already; the flag just names the intent). The boss
-- badge is granted directly by the persona weekly sweep, not the metric
-- path, so its metadata carries no metric/threshold.
INSERT INTO shop_items (key, name, description, kind, price_minor, metadata) VALUES
    ('badge_duelist',     'Duelist',      'Won 3 duels.',                          'BADGE', NULL, '{"emoji":"⚔️","metric":"duel_wins","threshold":3,"hidden":true}'),
    ('badge_gladiator',   'Gladiator',    'Won 10 duels.',                         'BADGE', NULL, '{"emoji":"🏟️","metric":"duel_wins","threshold":10,"hidden":true}'),
    ('badge_headhunter',  'Headhunter',   'Collected a bounty.',                   'BADGE', NULL, '{"emoji":"🎯","metric":"bounty_claims","threshold":1,"hidden":true}'),
    ('badge_deadeye',     'Deadeye',      'Collected 5 bounties.',                 'BADGE', NULL, '{"emoji":"💀","metric":"bounty_claims","threshold":5,"hidden":true}'),
    ('badge_singed',      'Singed',       'Survived a main-book liquidation.',     'BADGE', NULL, '{"emoji":"🔥","metric":"liquidations","threshold":1,"hidden":true}'),
    ('badge_sharpie',     'The Sharpie',  'Won a prop bet.',                       'BADGE', NULL, '{"emoji":"🎲","metric":"prop_wins","threshold":1,"hidden":true}'),
    ('badge_oracle',      'Oracle',       'Won 5 prop bets.',                      'BADGE', NULL, '{"emoji":"🔮","metric":"prop_wins","threshold":5,"hidden":true}'),
    ('badge_boss_slayer', 'Giant Slayer', 'Out-traded the boss for a whole week.', 'BADGE', NULL, '{"emoji":"🗡️","hidden":true}');
