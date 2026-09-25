-- N4.2: explicit opt-in for order-driven shorts. A resting SELL with
-- allow_short = FALSE never opens or extends a margin short at fill
-- time -- the matcher clamps its fillable size to the position (floor
-- 0) and the remainder just stays OPEN until filled holdings-side,
-- cancelled, or expired. TRUE is the pre-N4 behavior: the fill reaches
-- execute_trade's margin gates (tier, initial margin, SI cap).

ALTER TABLE orders
    ADD COLUMN IF NOT EXISTS allow_short BOOLEAN NOT NULL DEFAULT FALSE;
