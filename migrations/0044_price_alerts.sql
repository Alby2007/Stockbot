-- One-shot price alerts: /alert add stores an OPEN row; the per-tick
-- sweep in apply_tick flips crossed alerts to TRIGGERED and enqueues
-- ALERT_TRIGGERED outbox rows (coalesced per user+tick by the poller).
CREATE TABLE price_alerts (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(id),
    instrument_id INT NOT NULL REFERENCES instruments(id),
    direction TEXT NOT NULL CHECK (direction IN ('ABOVE', 'BELOW')),
    target_price NUMERIC(18,6) NOT NULL CHECK (target_price > 0),
    status TEXT NOT NULL DEFAULT 'OPEN'
        CHECK (status IN ('OPEN', 'TRIGGERED', 'CANCELLED')),
    created_tick BIGINT,
    triggered_tick BIGINT,
    triggered_price NUMERIC(18,6),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Exact-duplicate rejection at the DB level.
CREATE UNIQUE INDEX price_alerts_one_open
    ON price_alerts(user_id, instrument_id, direction, target_price)
    WHERE status = 'OPEN';
-- The sweep's join key.
CREATE INDEX price_alerts_open_iid
    ON price_alerts(instrument_id) WHERE status = 'OPEN';

-- Alert delivery rides the existing outbox; widen the closed kind set.
ALTER TABLE notifications DROP CONSTRAINT notifications_kind_check;
ALTER TABLE notifications ADD CONSTRAINT notifications_kind_check
    CHECK (kind IN ('LIQUIDATION', 'KNOCKOUT', 'ORDER_FILLED', 'SEASON_RESULT',
                    'ACCOUNT_SUSPENDED', 'BADGE_EARNED', 'IPO_SETTLED',
                    'MARGIN_CALL', 'SHORT_RECALL', 'ALERT_TRIGGERED'));

INSERT INTO config (key, value) VALUES ('alerts.max_per_user', 25);
