-- NPC P3: LP min-spread floor. The liquidity_provider archetype quotes
-- at an offset of at least (MM dynamic half-spread + npc.lp_min_spread)
-- from the mark, so it can't be scalped inside the market maker's own
-- spread the moment the mark drifts. Tuning knob, not a safety
-- invariant (permadeath already bounds NPC losses).

INSERT INTO config (key, value) VALUES
    ('npc.lp_min_spread', 0.002)
ON CONFLICT (key) DO NOTHING;
