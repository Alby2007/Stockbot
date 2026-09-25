-- Phase H1+H2: account-level gating. `grant_issued` decouples the
-- $100 starting grant from account creation so bootstrap can defer it
-- for under-age Discord accounts (anti-multiaccount-farm); the disabled_*
-- columns back per-user suspension. `disabled_by` is deliberately not an
-- FK -- users.id IS a Discord snowflake, so this is just another one.
ALTER TABLE users
    ADD COLUMN grant_issued BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN disabled_at TIMESTAMPTZ,
    ADD COLUMN disabled_reason TEXT,
    ADD COLUMN disabled_by BIGINT;

-- Every pre-existing user already received the grant on first use.
UPDATE users SET grant_issued = TRUE;

-- The disable action enqueues an ACCOUNT_SUSPENDED outbox row (the DM
-- tells the user why). CHECK lists are closed sets, so widen it.
ALTER TABLE notifications DROP CONSTRAINT notifications_kind_check;
ALTER TABLE notifications ADD CONSTRAINT notifications_kind_check
    CHECK (kind IN ('LIQUIDATION', 'KNOCKOUT', 'ORDER_FILLED', 'SEASON_RESULT',
                    'ACCOUNT_SUSPENDED'));

-- Min Discord account age (days) before the starting grant issues.
-- Shares the /claim gate's number by default; independently tunable via
-- /admin config (bounds live in admin.service.CONFIG_BOUNDS).
INSERT INTO config (key, value) VALUES ('accounts.min_discord_age_days', 30)
ON CONFLICT (key) DO NOTHING;
