-- Zombie-order strike counter and dividend model fix.
--
-- orders.fill_failures: pass-2 MM fills that fail deterministically (funds,
-- margin, league state) retry every tick forever. match_orders increments
-- this per failure and auto-cancels at order.max_fill_failures.
ALTER TABLE orders ADD COLUMN fill_failures INT NOT NULL DEFAULT 0;

INSERT INTO config (key, value) VALUES ('order.max_fill_failures', 5)
ON CONFLICT (key) DO NOTHING;

-- The dividend drift offset is retired: the ex-date drop is self-funding
-- under the MM-as-counterparty model (MM's cash payout is offset by its
-- mark-to-market on the aggregate short book), so a pre-bleed plus the
-- ex-drop suppressed ~2x the dividend -- a per-cycle short arb. In-flight
-- events still carry nonzero offsets, so zero them here.
UPDATE events SET drift_offset = 0 WHERE kind = 'DIVIDEND' AND NOT resolved;

-- The 0017 CHECK required drift_offset on every DIVIDEND row; new events
-- no longer write it. Keep the non-DIVIDEND side (no stray dividend
-- fields) and the per-share requirement.
ALTER TABLE events DROP CONSTRAINT events_dividend_fields_check;
ALTER TABLE events ADD CONSTRAINT events_dividend_fields_check CHECK (
    (kind = 'DIVIDEND'
        AND dividend_per_share IS NOT NULL
        AND dividend_per_share > 0)
    OR (kind <> 'DIVIDEND'
        AND dividend_per_share IS NULL
        AND drift_offset IS NULL)
);
