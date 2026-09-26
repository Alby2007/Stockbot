-- Short-side mechanics: margin-call warnings, borrow recalls, user-chosen
-- bounded-short knockouts, and the IPO borrow lockout.
--
-- accounts.margin_warned latches when a margined account crosses
-- margin.warn_ratio (equity < warn_ratio * maint_req): the warning DM fires
-- once per episode and the latch clears on recovery, so a hovering account
-- isn't spammed. instruments.shortable_after_tick gates NEW margin shorts
-- (IPO listings set it to close_tick + ipo.short_lockout_ticks -- fresh
-- listings have no borrow); NULL means always shortable. Recalls: when an
-- instrument's short interest exceeds margin.recall_si_pct (below the
-- max_short_interest_pct hard cap, so recall pressure starts before borrow
-- fully closes), sweep_recalls force-covers a pro-rata slice of every open
-- short per tick, capped by the account's cash -- a borrow recall, not a
-- liquidation: no penalty, no liquidations row, own notification kind.

ALTER TABLE accounts
    ADD COLUMN margin_warned BOOLEAN NOT NULL DEFAULT FALSE;

ALTER TABLE instruments
    ADD COLUMN shortable_after_tick BIGINT;

ALTER TABLE notifications DROP CONSTRAINT notifications_kind_check;
ALTER TABLE notifications ADD CONSTRAINT notifications_kind_check
    CHECK (kind IN ('LIQUIDATION', 'KNOCKOUT', 'ORDER_FILLED', 'SEASON_RESULT',
                    'ACCOUNT_SUSPENDED', 'BADGE_EARNED', 'IPO_SETTLED',
                    'MARGIN_CALL', 'SHORT_RECALL'));

INSERT INTO config (key, value) VALUES
    ('margin.warn_ratio', 1.35),
    ('margin.recall_si_pct', 0.25),
    ('margin.recall_fraction_per_tick', 0.10),
    ('ipo.short_lockout_ticks', 480),
    ('shorts.min_knockout_pct', 0.02),
    ('shorts.max_knockout_pct', 0.9999);
