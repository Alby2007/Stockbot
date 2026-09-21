-- Phase D: stop orders + dividends.
--
-- Stops: orders.order_type LIMIT/STOP/STOP_LIMIT, stop_price, and
-- triggered_tick (set when the mark crosses the stop; a triggered stop is
-- a marketable order in the book). limit_price goes nullable: STOP has
-- none. The per-type price invariant replaces relying on it directly.
--
-- Dividends: events.kind 'DIVIDEND' carries dividend_per_share and
-- drift_offset (the per-tick log-drift reduction that funds the payout by
-- lowering the expected price path, so dividends redistribute rather than
-- mint). positions.dividends_accrued is the short-side liability, settled
-- to MARKET_MAKER on cover/liquidation like borrow_fees_accrued -> SINK.
-- instruments.dividend_drift_offset is the recomputed-per-tick aggregate
-- the factor model subtracts from drift.

ALTER TABLE orders ADD COLUMN order_type TEXT NOT NULL DEFAULT 'LIMIT';
ALTER TABLE orders ADD CONSTRAINT orders_order_type_check
    CHECK (order_type IN ('LIMIT', 'STOP', 'STOP_LIMIT'));
ALTER TABLE orders ADD COLUMN stop_price NUMERIC(18,6);
ALTER TABLE orders ADD COLUMN triggered_tick BIGINT;
ALTER TABLE orders ALTER COLUMN limit_price DROP NOT NULL;
ALTER TABLE orders ADD CONSTRAINT orders_stop_price_check
    CHECK (stop_price IS NULL OR stop_price > 0);
ALTER TABLE orders ADD CONSTRAINT orders_type_prices_check
    CHECK (
        (order_type = 'LIMIT'
            AND limit_price IS NOT NULL AND stop_price IS NULL)
        OR (order_type = 'STOP'
            AND stop_price IS NOT NULL AND limit_price IS NULL)
        OR (order_type = 'STOP_LIMIT'
            AND stop_price IS NOT NULL AND limit_price IS NOT NULL)
    );
CREATE INDEX orders_open_stops_idx ON orders (instrument_id)
    WHERE status = 'OPEN' AND order_type <> 'LIMIT' AND triggered_tick IS NULL;

ALTER TABLE events DROP CONSTRAINT events_kind_check;
ALTER TABLE events ADD CONSTRAINT events_kind_check
    CHECK (kind = ANY (ARRAY['EARNINGS'::text, 'NEWS'::text, 'DIVIDEND'::text]));
ALTER TABLE events ADD COLUMN dividend_per_share NUMERIC(18,6);
ALTER TABLE events ADD COLUMN drift_offset NUMERIC(14,10);
ALTER TABLE events ADD CONSTRAINT events_dividend_fields_check
    CHECK (
        (kind = 'DIVIDEND' AND dividend_per_share IS NOT NULL
            AND dividend_per_share > 0 AND drift_offset IS NOT NULL)
        OR (kind <> 'DIVIDEND' AND dividend_per_share IS NULL
            AND drift_offset IS NULL)
    );

ALTER TABLE positions ADD COLUMN dividends_accrued NUMERIC(20,6) NOT NULL DEFAULT 0;
ALTER TABLE instruments ADD COLUMN dividend_drift_offset NUMERIC(14,10) NOT NULL DEFAULT 0;

INSERT INTO config (key, value) VALUES
    ('order.stop_cascade_max_iters', 8),
    ('dividend.interval_ticks', 86400),
    ('dividend.jitter_ticks', 20160),
    ('dividend.min_yield', 0.004),
    ('dividend.max_yield', 0.012),
    ('dividend.payer_pct', 35)
ON CONFLICT (key) DO NOTHING;
