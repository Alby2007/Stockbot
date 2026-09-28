-- Heal zero-adv instruments: databases that applied 0054 before the
-- warm-start columns were added to its inserts have AS rows (and ASX40)
-- stuck at adv=0 -> effective_liquidity sits at the adv_mult_min floor
-- (~half depth) until the 7,200-tick window fills. Same formula as
-- 0032/add_instrument. Idempotent; also repairs any other adv=0 strays.
UPDATE instruments i
SET adv = i.liquidity * COALESCE(
    (SELECT value FROM config WHERE key = 'flow.adv_ref_frac'),
    0.000000025)
WHERE i.adv = 0;
