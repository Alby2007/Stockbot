-- Daily/weekly quests: rotating tasks measured from existing
-- tick-stamped tables, completed by a per-tick sweep that pays FAUCET
-- rewards through post_transfer and notifies via the outbox.

CREATE TABLE quest_defs (
    key TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN
        ('TRADE_COUNT','DISTINCT_TICKERS','TRADE_VOLUME','ORDER_PLACED',
         'ORDER_FILLED','SHORT_PROFIT','ALERT_SET','IPO_SUBSCRIBE')),
    name TEXT NOT NULL,
    target BIGINT NOT NULL CHECK (target > 0),
    reward_minor BIGINT NOT NULL CHECK (reward_minor >= 0),
    period TEXT NOT NULL CHECK (period IN ('DAILY','WEEKLY')),
    active BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE TABLE quest_instances (
    id BIGSERIAL PRIMARY KEY,
    def_key TEXT NOT NULL REFERENCES quest_defs(key),
    -- kind is snapshotted with target/reward: editing a def must not
    -- change what a live instance measures.
    kind TEXT NOT NULL,
    period TEXT NOT NULL CHECK (period IN ('DAILY','WEEKLY')),
    period_index BIGINT NOT NULL,          -- day_index or week_index
    window_start BIGINT NOT NULL,
    window_end BIGINT NOT NULL CHECK (window_end > window_start),  -- inclusive ticks
    target BIGINT NOT NULL,                -- snapshot: def edits don't mutate live quests
    reward_minor BIGINT NOT NULL,
    status TEXT NOT NULL DEFAULT 'OPEN' CHECK (status IN ('OPEN','EXPIRED')),
    UNIQUE (def_key, period, period_index) -- replay-safe rotation
);

CREATE TABLE quest_completions (
    quest_instance_id BIGINT NOT NULL REFERENCES quest_instances(id),
    user_id BIGINT NOT NULL REFERENCES users(id),
    completed_tick BIGINT NOT NULL,
    reward_minor BIGINT NOT NULL,
    PRIMARY KEY (quest_instance_id, user_id)  -- the pay-once idempotency key
);

-- Window-scan indexes: the measure queries group tick ranges by user.
CREATE INDEX trades_tick_idx ON trades (tick_index);
CREATE INDEX orders_opened_tick_idx ON orders (opened_tick);
CREATE INDEX orders_filled_tick_idx ON orders (filled_tick);

-- ipo_subscriptions gains the tick stamp the IPO_SUBSCRIBE measure needs
-- (set in ipo.subscribe -- first commit counts; upsert keeps the original).
ALTER TABLE ipo_subscriptions ADD COLUMN created_tick BIGINT;

-- Lifetime quest counter; feeds the 'quests' badge metric.
ALTER TABLE users ADD COLUMN quests_completed INT NOT NULL DEFAULT 0;

ALTER TABLE notifications DROP CONSTRAINT notifications_kind_check;
ALTER TABLE notifications ADD CONSTRAINT notifications_kind_check
    CHECK (kind IN ('LIQUIDATION', 'KNOCKOUT', 'ORDER_FILLED', 'SEASON_RESULT',
                    'ACCOUNT_SUSPENDED', 'BADGE_EARNED', 'IPO_SETTLED',
                    'MARGIN_CALL', 'SHORT_RECALL', 'ALERT_TRIGGERED',
                    'QUEST_COMPLETED'));

INSERT INTO config (key, value) VALUES
    ('quests.enabled', 1),
    ('quests.daily_count', 3),
    ('quests.weekly_count', 2);

INSERT INTO quest_defs (key, kind, name, target, reward_minor, period) VALUES
    ('trade5',   'TRADE_COUNT',      'Make 5 trades',                    5,      500, 'DAILY'),
    ('tickers3', 'DISTINCT_TICKERS', 'Trade 3 different tickers',        3,      800, 'DAILY'),
    ('vol200',   'TRADE_VOLUME',     'Trade $200 of volume',             20000,  600, 'DAILY'),
    ('orders2',  'ORDER_PLACED',     'Place 2 resting orders',           2,      500, 'DAILY'),
    ('fill1',    'ORDER_FILLED',     'Get 1 resting order filled',       1,      600, 'DAILY'),
    ('short1',   'SHORT_PROFIT',     'Close a profitable bounded short', 1,     1000, 'DAILY'),
    ('alert1',   'ALERT_SET',        'Set a price alert',                1,      400, 'DAILY'),
    ('ipo1',     'IPO_SUBSCRIBE',    'Subscribe to an IPO',              1,      500, 'DAILY'),
    ('trade25',  'TRADE_COUNT',      'Make 25 trades this week',         25,    2000, 'WEEKLY'),
    ('tickers8', 'DISTINCT_TICKERS', 'Trade 8 different tickers this week', 8,  1500, 'WEEKLY'),
    ('vol1500',  'TRADE_VOLUME',     'Trade $1,500 of volume this week', 150000, 1800, 'WEEKLY'),
    ('short2',   'SHORT_PROFIT',     'Close 2 profitable shorts this week', 2,  2500, 'WEEKLY');

-- quests-metric badges: evaluate_badges' threshold (int) arm.
INSERT INTO shop_items (key, name, description, kind, price_minor, metadata) VALUES
    ('badge_quest_5',   'Questing',      'Completed 5 quests.',   'BADGE', NULL, '{"metric": "quests", "threshold": 5}'),
    ('badge_quest_25',  'Quest Regular', 'Completed 25 quests.',  'BADGE', NULL, '{"metric": "quests", "threshold": 25}'),
    ('badge_quest_100', 'Quest Legend',  'Completed 100 quests.', 'BADGE', NULL, '{"metric": "quests", "threshold": 100}');
