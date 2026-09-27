-- Daily-claim wheel: the claim pays a seeded weighted-segment draw
-- instead of a flat amount. last_segment/last_amount_minor record the
-- most recent roll for audit/history (the ledger records the payout
-- itself). claim.wheel_enabled=0 falls back to the flat legacy formula;
-- claim.jackpot_pct replaces the jackpot segment's weight -- the EV dial.
ALTER TABLE claims
    ADD COLUMN last_segment TEXT,
    ADD COLUMN last_amount_minor BIGINT;

INSERT INTO config (key, value) VALUES
    ('claim.wheel_enabled', 1),
    ('claim.jackpot_pct', 2)
ON CONFLICT (key) DO NOTHING;
