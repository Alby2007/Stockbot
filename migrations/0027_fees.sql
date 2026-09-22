-- Plan E: fee economics -- maker rebate on book crosses and
-- volume-tiered taker fees.
--
-- users.total_traded_minor is lifetime USER-level traded notional (main
-- + league fills both accrue; a whale doesn't reset inside a season).
-- Read at fill time so the tier applies to the NEXT fill, like a real
-- 30d-volume tier (game simplification: lifetime, not trailing window).
ALTER TABLE users
    ADD COLUMN total_traded_minor BIGINT NOT NULL DEFAULT 0;

-- trades.maker_rebate_minor records the rebate portion of the taker's
-- fee on cross fills (the MAKER leg keeps fee_minor = 0 -- it received
-- the rebate, queryable via the shared cash_transfer_id pair).
-- fee_transfer_id is now also NULL when the entire fee went to the maker.
ALTER TABLE trades
    ADD COLUMN maker_rebate_minor BIGINT NOT NULL DEFAULT 0;

INSERT INTO config (key, value) VALUES
    -- Bps of the taker's notional routed to the maker instead of SINK,
    -- clamped to the taker's own fee rate (a rebate can never exceed the
    -- fee collected).
    ('fee.maker_rebate_bps', 2),
    -- Lifetime-volume tiers (minor units = cents): $100k -> 8bps,
    -- $1M -> 6bps. Base stays FEE_BPS (10bps).
    ('fee.tier1_volume', 10000000),
    ('fee.tier1_bps', 8),
    ('fee.tier2_volume', 100000000),
    ('fee.tier2_bps', 6)
ON CONFLICT (key) DO NOTHING;
