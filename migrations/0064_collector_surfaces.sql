-- Collectibles depth C: demand surfaces. Collector-score weights for
-- /collectors and the daily top-pull broadcast cadence. Scores are
-- computed live from user_cards (+ the best pull's serial) -- nothing
-- materialized, so retunes take effect instantly.

INSERT INTO config (key, value) VALUES
    ('score.frame_standard',   1),
    ('score.frame_silver',     3),
    ('score.frame_gold',       8),
    ('score.frame_platinum',  20),
    ('score.frame_epic',      30),
    ('score.frame_legendary', 60),
    ('score.kind_lore',       10),
    ('score.kind_commemorative', 15),
    ('score.kind_assembled',  25),
    ('score.serial_one',      10),
    ('score.serial_low',       5),
    ('score.serial_low_max',  10)
ON CONFLICT DO NOTHING;
