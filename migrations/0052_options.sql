-- 0052: Options -- cash-settled CALL/PUT contracts vs MARKET_MAKER.
-- Model-consistent OU+random-walk pricing (options/pricing.py); hedge
-- impact is deferred to Phase O3 -- options.hedge_frac seeds at 0 so
-- O2's fills never touch the underlying mark. Long options count toward
-- net worth via mark_minor but are NOT margin collateral (documented
-- decision: compute_health is untouched).

CREATE TABLE option_positions (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users (id),
    instrument_id INT NOT NULL REFERENCES instruments (id),
    season_id BIGINT REFERENCES seasons (id),          -- NULL = main
    side TEXT NOT NULL CHECK (side IN ('CALL','PUT')),
    strike NUMERIC(18,6) NOT NULL CHECK (strike > 0),
    expiry_tick BIGINT NOT NULL,
    quantity BIGINT NOT NULL CHECK (quantity > 0),
    premium_minor BIGINT NOT NULL CHECK (premium_minor > 0), -- per-share premium paid (w/ markup)
    mark_minor BIGINT NOT NULL,                        -- latest model value per share
    status TEXT NOT NULL DEFAULT 'OPEN'
        CHECK (status IN ('OPEN','SETTLED','SOLD')),
    opened_tick BIGINT, closed_tick BIGINT,
    settlement_minor BIGINT,                           -- per-share payout at close
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX option_positions_expiry_idx
    ON option_positions (expiry_tick) WHERE status = 'OPEN';
CREATE INDEX option_positions_instrument_idx
    ON option_positions (instrument_id) WHERE status = 'OPEN';
CREATE INDEX option_positions_user_idx ON option_positions (user_id, created_at DESC);

ALTER TABLE notifications DROP CONSTRAINT notifications_kind_check;
ALTER TABLE notifications ADD CONSTRAINT notifications_kind_check
    CHECK (kind IN ('LIQUIDATION', 'KNOCKOUT', 'ORDER_FILLED', 'SEASON_RESULT',
                    'ACCOUNT_SUSPENDED', 'BADGE_EARNED', 'IPO_SETTLED',
                    'MARGIN_CALL', 'SHORT_RECALL', 'ALERT_TRIGGERED',
                    'QUEST_COMPLETED', 'OPTION_SETTLED'));

INSERT INTO config (key, value) VALUES
    ('options.enabled', 1),
    ('options.premium_markup', 0.10),
    ('options.sell_markdown', 0.12),
    ('options.max_oi_frac', 0.5),
    ('options.hedge_frac', 0),
    ('options.strike_min_frac', 0.2),
    ('options.strike_max_frac', 5.0),
    ('options.min_premium_minor', 1)
ON CONFLICT (key) DO NOTHING;
