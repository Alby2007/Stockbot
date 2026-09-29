-- Shop lifecycle: entitlement-expiry DM warnings, gifting, the daily
-- deal, a player-commissioned listing consumable, and faster badges.
--
-- * expiry_notice_at is the warn-once marker: set when the expiring-soon
--   DM goes out, cleared on renewal so a renewed item warns again.
-- * listing_credit is a stackable CONSUMABLE that /commission spends to
--   list a custom instrument ($500 -- a whale-priced vanity sink).
-- * sandbox_access reprices $30 -> $10 (plan: accessibility over price).
-- * shop.deal_* drive the daily deal; badges.interval_ticks drops the
--   badge sweep from day-boundary to hourly cadence.

ALTER TABLE notifications DROP CONSTRAINT notifications_kind_check;
ALTER TABLE notifications ADD CONSTRAINT notifications_kind_check
    CHECK (kind IN ('LIQUIDATION', 'KNOCKOUT', 'ORDER_FILLED', 'SEASON_RESULT',
                    'ACCOUNT_SUSPENDED', 'BADGE_EARNED', 'IPO_SETTLED',
                    'MARGIN_CALL', 'SHORT_RECALL', 'ALERT_TRIGGERED',
                    'QUEST_COMPLETED', 'OPTION_SETTLED', 'MOMENT_EARNED',
                    'TRADE_OFFER', 'TRADE_RESULT',
                    'ENTITLEMENT_EXPIRING', 'GIFT_RECEIVED'));

ALTER TABLE entitlements ADD COLUMN expiry_notice_at TIMESTAMPTZ;
CREATE INDEX entitlements_expiring
    ON entitlements (expires_at)
    WHERE expires_at IS NOT NULL AND expiry_notice_at IS NULL;

INSERT INTO shop_items (key, name, description, kind, price_minor, metadata) VALUES
    ('listing_credit', 'Listing commission', 'Commission a custom instrument — pick the ticker, name, sector, and venue, then run /commission.', 'CONSUMABLE', 50000, '{}');

UPDATE shop_items SET price_minor = 1000 WHERE key = 'sandbox_access';

INSERT INTO config (key, value) VALUES
    ('shop.deal_enabled', 1),
    ('shop.deal_discount_pct', 0.25),
    ('shop.expiry_warn_days', 3),
    ('badges.interval_ticks', 60);
