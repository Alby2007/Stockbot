-- Split MM absorption out of the fund-flow amount.
--
-- The ADL backstop is MARKET_MAKER's loss, not the insurance fund's -- but
-- it was being recorded as amount_minor = -residual, so
-- `fund_balance == SUM(amount_minor)` (the reconciliation asserted in
-- test_margin.py) broke the first time an ADL actually fired. amount_minor
-- now means strictly "fund balance delta" (ADL rows write 0); the absorbed
-- amount is kept here for audit.
ALTER TABLE insurance_fund_flows
    ADD COLUMN mm_absorbed_minor BIGINT NOT NULL DEFAULT 0;

COMMENT ON COLUMN insurance_fund_flows.mm_absorbed_minor IS
    'Loss absorbed by MARKET_MAKER on ADL rows (audit only -- not a fund movement)';
