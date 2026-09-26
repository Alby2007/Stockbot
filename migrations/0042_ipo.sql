-- IPO subscriptions: an instrument lists is_active=FALSE behind an
-- offering row; users commit cash into the IPO_ESCROW system account
-- during [open_tick, close_tick). At close, ipo.settle_due() allocates
-- shares_offered pro-rata by commitment (allocated dollars escrow -> SINK
-- = the issuer's take, the sink side of the offering), refunds the
-- excess, lands positions at the offer price, and flips is_active so the
-- listing-day pop is real market action. Zero subscriptions cancels the
-- offering and leaves the instrument dormant.

ALTER TABLE notifications DROP CONSTRAINT notifications_kind_check;
ALTER TABLE notifications ADD CONSTRAINT notifications_kind_check
    CHECK (kind IN ('LIQUIDATION', 'KNOCKOUT', 'ORDER_FILLED', 'SEASON_RESULT',
                    'ACCOUNT_SUSPENDED', 'BADGE_EARNED', 'IPO_SETTLED'));

INSERT INTO accounts (kind, system_name, balance)
SELECT 'SYSTEM', 'IPO_ESCROW', 0
WHERE NOT EXISTS (SELECT 1 FROM accounts WHERE system_name = 'IPO_ESCROW');

CREATE TABLE ipo_offerings (
    id BIGSERIAL PRIMARY KEY,
    instrument_id INT NOT NULL UNIQUE REFERENCES instruments (id),
    offer_price_minor BIGINT NOT NULL CHECK (offer_price_minor > 0),
    shares_offered BIGINT NOT NULL CHECK (shares_offered > 0),
    open_tick BIGINT NOT NULL,
    close_tick BIGINT NOT NULL CHECK (close_tick > open_tick),
    status TEXT NOT NULL DEFAULT 'OPEN' CHECK (status IN ('OPEN', 'SETTLED', 'CANCELLED')),
    committed_minor BIGINT NOT NULL DEFAULT 0,
    allocated_qty BIGINT NOT NULL DEFAULT 0,
    settled_tick BIGINT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE ipo_subscriptions (
    offering_id BIGINT NOT NULL REFERENCES ipo_offerings (id),
    user_id BIGINT NOT NULL REFERENCES users (id),
    amount_minor BIGINT NOT NULL CHECK (amount_minor > 0),
    allocated_qty BIGINT NOT NULL DEFAULT 0,
    refund_minor BIGINT NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (offering_id, user_id)
);
