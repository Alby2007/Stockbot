-- Paid order types (capability sink): iceberg display, trailing stops, and
-- OCO brackets gated on ORDER_TYPE entitlements. orders.trail_amount is the
-- dollar distance the stop chases behind the mark (ratcheted once per tick
-- by _ratchet_trailing_stops before the trigger sweep); orders.oco_group
-- links a bracket's legs -- any terminal transition on one leg cancels the
-- other.

ALTER TABLE shop_items DROP CONSTRAINT shop_items_kind_check;
ALTER TABLE shop_items ADD CONSTRAINT shop_items_kind_check
    CHECK (kind IN ('SLOT', 'ANALYST_TOOL', 'COSMETIC', 'TROPHY',
                    'MARGIN_TIER', 'BADGE', 'ORDER_TYPE'));

ALTER TABLE orders
    ADD COLUMN trail_amount NUMERIC(18, 6) CHECK (trail_amount > 0),
    ADD COLUMN oco_group BIGINT;

INSERT INTO shop_items (key, name, description, kind, price_minor, metadata) VALUES
    ('order_iceberg',  'Iceberg orders',   'Show only a slice of a resting order in the /stock depth ladder.', 'ORDER_TYPE', 2000, '{}'),
    ('order_trailing', 'Trailing stops',   'A stop that ratchets behind the mark as it moves in your favor (trail: on /order).', 'ORDER_TYPE', 5000, '{}'),
    ('order_oco',      'OCO brackets',     'One-cancels-other: pair a take-profit with a stop-loss; first to resolve cancels the other (/order bracket).', 'ORDER_TYPE', 7500, '{}');
