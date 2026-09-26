-- Milestone badges: grant-only shop_items awarded by evaluate_badges() at
-- the day boundary (same cadence as net_worth_snapshots). Thresholds live
-- in shop_items.metadata -- the catalog is data, the metric interpretation
-- is code. price_minor stays NULL: badges are never purchasable (buy_item
-- rejects NULL-price rows, /shop list skips them). BADGE_EARNED widens the
-- outbox CHECK so a grant DMs the user.

ALTER TABLE shop_items DROP CONSTRAINT shop_items_kind_check;
ALTER TABLE shop_items ADD CONSTRAINT shop_items_kind_check
    CHECK (kind IN ('SLOT', 'ANALYST_TOOL', 'COSMETIC', 'TROPHY',
                    'MARGIN_TIER', 'BADGE'));

ALTER TABLE notifications DROP CONSTRAINT notifications_kind_check;
ALTER TABLE notifications ADD CONSTRAINT notifications_kind_check
    CHECK (kind IN ('LIQUIDATION', 'KNOCKOUT', 'ORDER_FILLED', 'SEASON_RESULT',
                    'ACCOUNT_SUSPENDED', 'BADGE_EARNED'));

INSERT INTO shop_items (key, name, description, kind, price_minor, metadata) VALUES
    ('badge_nw_500',    'Half Grand',      'Net worth reached $500.',      'BADGE', NULL, '{"metric": "net_worth", "threshold_minor": 50000}'),
    ('badge_nw_2k',     'Two Grand',       'Net worth reached $2,000.',    'BADGE', NULL, '{"metric": "net_worth", "threshold_minor": 200000}'),
    ('badge_nw_10k',    'Five Figures',    'Net worth reached $10,000.',   'BADGE', NULL, '{"metric": "net_worth", "threshold_minor": 1000000}'),
    ('badge_nw_50k',    'Big Swinger',     'Net worth reached $50,000.',   'BADGE', NULL, '{"metric": "net_worth", "threshold_minor": 5000000}'),
    ('badge_nw_100k',   'Six Figures',     'Net worth reached $100,000.',  'BADGE', NULL, '{"metric": "net_worth", "threshold_minor": 10000000}'),
    ('badge_vol_1k',    'Active Trader',   'Lifetime volume reached $1,000.',   'BADGE', NULL, '{"metric": "volume", "threshold_minor": 100000}'),
    ('badge_vol_10k',   'Heavy Hitter',    'Lifetime volume reached $10,000.',  'BADGE', NULL, '{"metric": "volume", "threshold_minor": 1000000}'),
    ('badge_vol_100k',  'Whale',           'Lifetime volume reached $100,000.', 'BADGE', NULL, '{"metric": "volume", "threshold_minor": 10000000}'),
    ('badge_streak_7',  'Weekly Regular',  'Claimed the faucet 7 days in a row.',  'BADGE', NULL, '{"metric": "streak", "threshold": 7}'),
    ('badge_streak_14', 'Fortnight',       'Claimed the faucet 14 days in a row.', 'BADGE', NULL, '{"metric": "streak", "threshold": 14}'),
    ('badge_streak_30', 'Monthly Grinder', 'Claimed the faucet 30 days in a row.', 'BADGE', NULL, '{"metric": "streak", "threshold": 30}');
