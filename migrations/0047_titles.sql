-- Shop expansion Phase B: equippable titles + flair surfaces.
--
-- TITLE is a new shop kind: owned via entitlements like any item, but its
-- "effect" is users.equipped_title (explicit equip slot, not a generic
-- framework -- two slots, two columns). metadata.emoji is the optional
-- glyph that renders before the name on leaderboards/profile/whois.
--
-- equipped_title REFERENCES shop_items(key): shop rows are never deleted
-- (delisting isn't a concept for them), so a dangling title can't happen.

ALTER TABLE shop_items DROP CONSTRAINT shop_items_kind_check;
ALTER TABLE shop_items ADD CONSTRAINT shop_items_kind_check
    CHECK (kind IN ('SLOT', 'ANALYST_TOOL', 'COSMETIC', 'TROPHY',
                    'MARGIN_TIER', 'BADGE', 'ORDER_TYPE', 'TITLE'));

ALTER TABLE users
    ADD COLUMN equipped_title TEXT REFERENCES shop_items (key);

INSERT INTO shop_items (key, name, description, kind, price_minor, duration_days, metadata) VALUES
    ('title_oracle', 'The Oracle', 'Equippable title. Shows on leaderboards, /profile, and /whois. Auto-equips on buy.', 'TITLE', 1500, NULL, '{"emoji":"🔮"}'),
    ('title_survivor', 'Margin Call Survivor', 'Equippable title. Shows on leaderboards, /profile, and /whois. Auto-equips on buy.', 'TITLE', 1200, NULL, '{"emoji":"🪂"}'),
    ('title_degen', 'Certified Degen', 'Equippable title. Shows on leaderboards, /profile, and /whois. Auto-equips on buy.', 'TITLE', 1000, NULL, '{"emoji":"🎰"}'),
    ('title_fund', 'Fund Manager', 'Equippable title. Shows on leaderboards, /profile, and /whois. Auto-equips on buy.', 'TITLE', 1500, NULL, '{"emoji":"📊"}'),
    ('title_bagholder', 'Bagholder Prime', 'Equippable title. Shows on leaderboards, /profile, and /whois. Auto-equips on buy.', 'TITLE', 800, NULL, '{"emoji":"🎒"}'),
    ('title_patient', 'Patient Zero', 'Equippable title. Shows on leaderboards, /profile, and /whois. Auto-equips on buy.', 'TITLE', 1000, NULL, '{"emoji":"🧘"}');
