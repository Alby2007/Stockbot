-- Collectibles depth B: moments. The market mints its own history --
-- when the tick produces something remarkable (single-tick move, mass
-- halt, record break), a COMMEMORATIVE card lands in the 'moments' set
-- and is granted to witnesses: users holding main-economy exposure in
-- the affected instrument(s) that tick.
--
-- Moments never enter the pack pool (in_pack_pool = FALSE): they are
-- earned, not pulled. The moments table is the dedup + audit record;
-- awarded_count is written after the witness grant pass.

CREATE TABLE moments (
    id BIGSERIAL PRIMARY KEY,
    rule TEXT NOT NULL CHECK (rule IN ('MOVER', 'MASS_HALT', 'RECORD')),
    card_key TEXT NOT NULL UNIQUE REFERENCES cards (key),
    instrument_id INT REFERENCES instruments (id),
    tick_index BIGINT NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}',
    awarded_count INT NOT NULL DEFAULT 0
);
-- MASS_HALT carries instrument_id NULL; COALESCE keeps dedup working.
CREATE UNIQUE INDEX moments_dedup
    ON moments (rule, COALESCE(instrument_id, 0), tick_index);

CREATE SEQUENCE moment_card_seq START 1;

INSERT INTO card_sets (key, name, in_pack_pool) VALUES
    ('moments', 'Market Moments', FALSE)
ON CONFLICT DO NOTHING;

ALTER TABLE notifications DROP CONSTRAINT notifications_kind_check;
ALTER TABLE notifications ADD CONSTRAINT notifications_kind_check
    CHECK (kind IN ('LIQUIDATION', 'KNOCKOUT', 'ORDER_FILLED', 'SEASON_RESULT',
                    'ACCOUNT_SUSPENDED', 'BADGE_EARNED', 'IPO_SETTLED',
                    'MARGIN_CALL', 'SHORT_RECALL', 'ALERT_TRIGGERED',
                    'QUEST_COMPLETED', 'OPTION_SETTLED', 'MOMENT_EARNED'));

INSERT INTO config (key, value) VALUES
    ('moment.enabled', 1),
    ('moment.mover_pct', 12),
    ('moment.mass_halt_min', 5),
    ('moment.record_multiple', 2),
    ('moment.max_per_tick', 3)
ON CONFLICT DO NOTHING;
