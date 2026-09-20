-- Phase 2: true margin shorts.
--
-- Model: positions go signed (negative = short). Cash stays >= 0 for
-- USER/LEAGUE accounts (the double-spend CHECK is preserved) -- short sale
-- proceeds credit to cash and are spendable, which is the leverage
-- mechanism; every trade is gated by post-trade margin health instead.
--
--   equity     = cash + SUM(qty * quoted) - accrued_borrow_fees
--   maint_req  = SUM over SHORT positions of |qty| * quoted * maint_pct
--   init_req   = same with init_margin_pct
--   ratio      = equity / maint_req        (no shorts => not margined)
--
-- Maintenance applies to short notional only: longs are cash-paid, so only
-- short-holders can go undermargined. That keeps the per-tick sweep cheap.

ALTER TABLE positions DROP CONSTRAINT positions_quantity_nonneg;
-- Borrow fees accrue on the position (minor units, fractional) and settle to
-- SINK at cover/liquidation -- real per-tick ledger postings per short would
-- be thousands of rows per tick. Equity math treats accrual as owed debt.
ALTER TABLE positions
    ADD COLUMN borrow_fees_accrued NUMERIC(20, 6) NOT NULL DEFAULT 0;

ALTER TABLE instruments
    ADD COLUMN kind TEXT NOT NULL DEFAULT 'STOCK'
        CHECK (kind IN ('STOCK', 'INDEX')),
    ADD COLUMN init_margin_pct NUMERIC(6, 4) NOT NULL DEFAULT 0.50,
    ADD COLUMN maint_margin_pct NUMERIC(6, 4) NOT NULL DEFAULT 0.30,
    ADD COLUMN float_shares BIGINT NOT NULL DEFAULT 0,
    -- cached per tick: shares short / float. Drives the squeeze mechanic and
    -- the published short interest on /stock.
    ADD COLUMN short_interest_pct NUMERIC(10, 6) NOT NULL DEFAULT 0,
    -- INDEX rows only: index level = SUM(float*quoted) / index_divisor.
    ADD COLUMN index_divisor NUMERIC(30, 10);

-- Floats: sized so dollar float is proportional to liquidity (liquid names
-- are the big caps). Then scale margin requirements by liquidity and vol:
-- illiquid/volatile names get punitive requirements (manipulation defense).
UPDATE instruments SET
    float_shares = GREATEST(10_000, ROUND(liquidity * 50 / base_price)),
    maint_margin_pct = LEAST(0.90, GREATEST(0.15,
        0.30 * (3_000_000 / liquidity) * (sigma / 0.0003)));

UPDATE instruments SET
    init_margin_pct = LEAST(0.95, maint_margin_pct / 0.6);

-- Global margin tunables. Read fresh every call; hot-adjustable by UPDATE.
CREATE TABLE config (
    key TEXT PRIMARY KEY,
    value NUMERIC NOT NULL
);

INSERT INTO config (key, value) VALUES
    -- borrow fee per tick on short notional: 0.01bp/tick ~= 0.14%/day
    ('margin.borrow_fee_bps_per_tick', 0.01),
    -- partial liquidation restores ratio to at least this
    ('margin.liquidation_target_ratio', 1.2),
    -- penalty on liquidated notional, paid to INSURANCE_FUND
    ('margin.liquidation_penalty_bps', 200),
    -- max aggregate short interest as a fraction of float, per instrument
    ('margin.max_short_interest_pct', 0.30),
    -- ceiling on the tier-based gross-notional leverage cap
    ('margin.max_gross_leverage', 4.0),
    -- squeeze: buy-side lambda boost kicks in above this SI/float fraction
    ('margin.squeeze_si_threshold', 0.10),
    -- lambda multiplier = 1 + boost * (si - threshold)
    ('margin.squeeze_lambda_boost', 2.0);

CREATE TABLE liquidations (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users (id),
    season_id BIGINT REFERENCES seasons (id),
    account_id BIGINT NOT NULL REFERENCES accounts (id),
    instrument_id INT NOT NULL REFERENCES instruments (id),
    side TEXT NOT NULL CHECK (side IN ('BUY', 'SELL')),  -- BUY = covered a short, SELL = sold a long
    quantity_closed BIGINT NOT NULL CHECK (quantity_closed > 0),
    fill_price NUMERIC(18, 6) NOT NULL,
    notional_minor BIGINT NOT NULL,
    penalty_minor BIGINT NOT NULL DEFAULT 0,
    equity_before_minor BIGINT NOT NULL,
    maint_req_before_minor BIGINT NOT NULL,
    tick_index BIGINT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX liquidations_user_idx ON liquidations (user_id, created_at DESC);
CREATE INDEX liquidations_instrument_idx ON liquidations (instrument_id, created_at DESC);

CREATE TABLE insurance_fund_flows (
    id BIGSERIAL PRIMARY KEY,
    -- signed minor units: positive = into the fund, negative = paid out
    amount_minor BIGINT NOT NULL,
    reason TEXT NOT NULL CHECK (reason IN ('SEED', 'LIQUIDATION_PENALTY', 'COVER_SHORTFALL', 'ADL')),
    liquidation_id BIGINT REFERENCES liquidations (id),
    tick_index BIGINT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Seed the insurance fund from the faucet (real ledger postings, not a
-- direct balance poke, so the audit trail is intact).
DO $$
DECLARE
    faucet_id BIGINT;
    fund_id BIGINT;
    tid UUID := gen_random_uuid();
BEGIN
    SELECT id INTO faucet_id FROM accounts WHERE system_name = 'FAUCET';
    SELECT id INTO fund_id FROM accounts WHERE system_name = 'INSURANCE_FUND';
    INSERT INTO ledger_entries (transfer_id, account_id, amount, reason, memo)
    VALUES
        (tid, faucet_id, -50_000_00, 'INSURANCE_SEED', 'initial seed'),
        (tid, fund_id,    50_000_00, 'INSURANCE_SEED', 'initial seed');
    UPDATE accounts SET balance = balance - 50_000_00 WHERE id = faucet_id;
    UPDATE accounts SET balance = balance + 50_000_00 WHERE id = fund_id;
    INSERT INTO insurance_fund_flows (amount_minor, reason)
    VALUES (50_000_00, 'SEED');
END $$;

-- Margin tier entitlement kind: quantity on the entitlement row = tier.
ALTER TABLE shop_items DROP CONSTRAINT shop_items_kind_check;
ALTER TABLE shop_items ADD CONSTRAINT shop_items_kind_check
    CHECK (kind IN ('SLOT', 'ANALYST_TOOL', 'COSMETIC', 'TROPHY', 'MARGIN_TIER'));

INSERT INTO shop_items (key, name, description, kind, price_minor, duration_days, metadata)
VALUES (
    'margin_tier',
    'Margin tier',
    'Unlocks true margin shorts. Each tier raises your gross-leverage cap (tier N = (N+1)x equity, max 4x).',
    'MARGIN_TIER', NULL, NULL, '{}'
);

-- SBX-40: cap-weighted index over the STOCK instruments. Its price is
-- computed in apply_tick from component prices; it trades like a normal
-- instrument (own impact, positions, marginable).
INSERT INTO sectors (key, name) VALUES ('index', 'Index')
ON CONFLICT (key) DO NOTHING;

INSERT INTO instruments (
    ticker, name, sector_id, kind,
    drift, sigma, beta, gamma, kappa, fundamental_sigma,
    liquidity, lambda_impact, tau_ticks, max_impact,
    fundamental_value, base_price, impact, quoted_price,
    init_margin_pct, maint_margin_pct, float_shares, index_divisor,
    short_knockout_pct
)
SELECT
    'SBX40', 'SBX-40 Index', s.id, 'INDEX',
    0, 0, 0, 0, 0, 0,
    50_000_000, 0.000001, 50, 0.03,
    1000, 1000, 0, 1000,
    0.20, 0.10, 0, d.divisor,
    0.25
FROM sectors s,
     (SELECT SUM(i.float_shares * i.base_price) / 1000 AS divisor
      FROM instruments i WHERE i.kind = 'STOCK') d
WHERE s.key = 'index';
