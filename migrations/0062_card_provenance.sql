-- Collectibles depth A: provenance. Every pull mints a per-card serial
-- (cards.minted_count serializes assignment) and stamps the market
-- context it printed in. user_cards.best_serial shows the serial of
-- the copy whose frame is held; stamps replay from recorded inputs
-- (tick_index -> candles / halts / venue session phase).
--
-- * instruments.high_water_price is the all-time mark, updated with a
--   GREATEST in apply_tick's instruments UPDATE so a pull at the top
--   can stamp PEAK. Forward-only: no honest pre-deploy history exists.
-- * card_sets.rotated_tick is the first-edition hook: NULL = still in
--   print; when a set rotates out of the pool, pulls before that tick
--   render "1st Ed." Purely derivable -- nothing extra is stored.

ALTER TABLE cards ADD COLUMN minted_count BIGINT NOT NULL DEFAULT 0;
ALTER TABLE card_pulls ADD COLUMN serial INT;
ALTER TABLE card_pulls ADD COLUMN stamps TEXT[] NOT NULL DEFAULT '{}';
ALTER TABLE user_cards ADD COLUMN best_serial INT;
ALTER TABLE card_sets ADD COLUMN rotated_tick BIGINT;
ALTER TABLE instruments ADD COLUMN high_water_price NUMERIC(18,6);

-- ATH backfill from candle highs; instruments with no candles start at
-- their current mark.
UPDATE instruments i
SET high_water_price = COALESCE(
    (SELECT MAX(c.high) FROM candles c WHERE c.instrument_id = i.id),
    i.quoted_price
);

-- Serial backfill: pull order within each card is the mint order.
WITH numbered AS (
    SELECT id, ROW_NUMBER() OVER (PARTITION BY card_key ORDER BY id) AS rn
    FROM card_pulls
)
UPDATE card_pulls p SET serial = n.rn FROM numbered n WHERE p.id = n.id;

UPDATE cards c SET minted_count = COALESCE(
    (SELECT MAX(p.serial) FROM card_pulls p WHERE p.card_key = c.key), 0);

CREATE UNIQUE INDEX card_pulls_card_serial
    ON card_pulls (card_key, serial) WHERE serial IS NOT NULL;

-- best_serial backfill: serial of the earliest pull that produced the
-- held frame's tier (tier -> frame rank via the C4 ordering).
WITH ranked AS (
    SELECT p.user_id, p.card_key, p.serial, p.id,
           CASE p.tier WHEN 'STANDARD' THEN 0 WHEN 'SILVER' THEN 1
                WHEN 'GOLD' THEN 2 WHEN 'PLATINUM' THEN 3
                WHEN 'EPIC' THEN 4 WHEN 'LEGENDARY' THEN 5
                ELSE -1 END AS rnk
    FROM card_pulls p
),
best AS (
    SELECT DISTINCT ON (r.user_id, r.card_key)
           r.user_id, r.card_key, r.serial
    FROM ranked r
    JOIN user_cards uc
      ON uc.user_id = r.user_id AND uc.card_key = r.card_key
    WHERE r.rnk = CASE uc.best_frame WHEN 'STANDARD' THEN 0
                WHEN 'SILVER' THEN 1 WHEN 'GOLD' THEN 2
                WHEN 'PLATINUM' THEN 3 WHEN 'EPIC' THEN 4
                WHEN 'LEGENDARY' THEN 5 ELSE -1 END
    ORDER BY r.user_id, r.card_key, r.id
)
UPDATE user_cards uc SET best_serial = b.serial
FROM best b
WHERE uc.user_id = b.user_id AND uc.card_key = b.card_key;

-- Stamp backfill: only what recorded inputs honestly replay. PEAK is
-- forward-only (high_water tracking starts with this deploy); the rest
-- derive from the pull tick. A pull counts as HALT_PRINT when a halt
-- candle lands in the CIRCUIT_HALT_TICKS window ending at that tick.
UPDATE card_pulls p SET stamps = COALESCE((
    SELECT array_agg(s.stamp) FROM (
        SELECT 'HALT_PRINT' AS stamp WHERE EXISTS (
            SELECT 1 FROM candles c
            JOIN cards cd ON cd.instrument_id = c.instrument_id
            WHERE cd.key = p.card_key
              AND c.tick_index BETWEEN p.tick_index - 5 AND p.tick_index
              AND c.halt_kind IS NOT NULL)
        UNION ALL
        SELECT 'MOON' WHERE EXISTS (
            SELECT 1 FROM candles c
            JOIN cards cd ON cd.instrument_id = c.instrument_id
            WHERE cd.key = p.card_key AND c.tick_index = p.tick_index
              AND c.open > 0 AND c.close >= c.open * 1.12)
        UNION ALL
        SELECT 'CRATER' WHERE EXISTS (
            SELECT 1 FROM candles c
            JOIN cards cd ON cd.instrument_id = c.instrument_id
            WHERE cd.key = p.card_key AND c.tick_index = p.tick_index
              AND c.open > 0 AND c.close <= c.open * 0.88)
        UNION ALL
        SELECT 'DAY_ONE' WHERE EXISTS (
            SELECT 1 FROM ipo_offerings o
            JOIN cards cd ON cd.instrument_id = o.instrument_id
            WHERE cd.key = p.card_key AND o.settled_tick IS NOT NULL
              AND p.tick_index BETWEEN o.settled_tick
                                       AND o.settled_tick + 1440)
        UNION ALL
        SELECT 'OFF_HOURS' WHERE EXISTS (
            SELECT 1 FROM cards cd
            JOIN instruments i ON i.id = cd.instrument_id
            JOIN markets m ON m.id = i.market_id
            WHERE cd.key = p.card_key
              AND mod(p.tick_index - m.offset_ticks,
                      m.open_ticks + m.closed_ticks) >= m.open_ticks)
        UNION ALL
        SELECT 'PITY_BREAK'
        WHERE p.pity_count_before >=
              COALESCE((p.pull_cfg ->> 'pity_threshold')::int, 20)
    ) s
), '{}'::text[])
WHERE p.tick_index IS NOT NULL;

INSERT INTO config (key, value) VALUES
    ('stamps.moon_pct', 12),
    ('stamps.crash_pct', 12),
    ('stamps.day_one_ticks', 1440),
    ('stamps.low_serial_feed', 10)
ON CONFLICT DO NOTHING;
