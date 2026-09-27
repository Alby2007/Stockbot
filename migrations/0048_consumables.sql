-- Shop expansion Phase C: consumables + stacking perks.
--
-- CONSUMABLE: owned in quantity, spent one at a time via
-- shop.use_consumable (entitlements CHECK forbids a 0 row, so the last
-- unit's spend deletes it). PERK: permanent, stacking flag -- quantity
-- is the multiplier (alert_pack: +10 alerts each). Both stack on
-- purchase: a second buy is `quantity + 1`, not AlreadyOwnedError.
--
-- quest_instances.user_id: NULL = the global rotated board, set = a
-- personal rerolled instance. quest_swaps records (user, original ->
-- replacement) so the swapped quest genuinely stops tracking (the sweep
-- and /quests both exclude it) -- a reroll isn't cosmetic.

ALTER TABLE shop_items DROP CONSTRAINT shop_items_kind_check;
ALTER TABLE shop_items ADD CONSTRAINT shop_items_kind_check
    CHECK (kind IN ('SLOT', 'ANALYST_TOOL', 'COSMETIC', 'TROPHY',
                    'MARGIN_TIER', 'BADGE', 'ORDER_TYPE', 'TITLE',
                    'CONSUMABLE', 'PERK'));

ALTER TABLE quest_instances
    ADD COLUMN user_id BIGINT REFERENCES users (id);

-- Recreate the dedupe key with user_id in it. NULLS NOT DISTINCT keeps
-- global (user_id NULL) rotation replay-safe: two on_tick calls still
-- can't insert the same (def, period, index).
ALTER TABLE quest_instances
    DROP CONSTRAINT quest_instances_def_key_period_period_index_key;
ALTER TABLE quest_instances ADD CONSTRAINT quest_instances_def_period_idx_user_key
    UNIQUE NULLS NOT DISTINCT (def_key, period, period_index, user_id);

CREATE TABLE quest_swaps (
    user_id BIGINT NOT NULL REFERENCES users (id),
    quest_instance_id BIGINT NOT NULL REFERENCES quest_instances (id),
    replacement_instance_id BIGINT NOT NULL REFERENCES quest_instances (id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, quest_instance_id)
);

INSERT INTO shop_items (key, name, description, kind, price_minor, duration_days, metadata) VALUES
    ('quest_reroll', 'Quest reroll', 'Swap one active quest for a different one. Consumed on use; stacks.', 'CONSUMABLE', 1000, NULL, '{}'),
    ('streak_shield', 'Streak shield', 'Saves your daily claim streak across exactly one missed day. Consumed automatically; stacks.', 'CONSUMABLE', 1500, NULL, '{}'),
    ('alert_pack', 'Alert pack', '+10 price alerts, permanently. Stacks.', 'PERK', 1000, NULL, '{}');
