-- ADV warm start + auction-fills telemetry.
--
-- instruments.adv defaulted to 0, which effective_liquidity maps to the
-- adv_mult_min floor (~half depth) until the 7,200-tick sliding window
-- fills -- a ~5-day liquidity depression on deploy and for every
-- add_instrument listing. Backfill adv to liquidity * adv_ref_frac (the
-- neutral multiplier, 1.0); the sliding-window refresh in apply_tick
-- converges it to real flow from there.
UPDATE instruments i
SET adv = i.liquidity * COALESCE(
    (SELECT value FROM config WHERE key = 'flow.adv_ref_frac'), 0.000000025)
WHERE i.adv = 0;

-- _auction_clear already counts stats["auction_fills"]; persist it like
-- the other per-tick counters.
ALTER TABLE market_ticks ADD COLUMN auction_fills INT NOT NULL DEFAULT 0;
