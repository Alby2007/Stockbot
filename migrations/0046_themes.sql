-- Shop expansion Phase A: chart themes actually render. The theme_*
-- COSMETIC rows sold since 0006 were placebo -- `metadata.chart_color` was
-- never read by the renderer. Now metadata IS the render palette and
-- chart_prefs.theme stores the user's equipped theme item key (written by
-- buy_item's auto-equip; /equip switches it later).
--
-- Palette contract: shop_items.metadata carries a partial palette over
-- {up, down, bg, grid, text, accent, spine, muted, halt_flow, halt_model,
-- pill_text}; keys a theme omits fall back to the renderer defaults. The
-- legacy `chart_color` key is kept for back-compat and maps onto
-- up+accent when the full keys are absent.

ALTER TABLE chart_prefs
    ADD COLUMN theme TEXT REFERENCES shop_items (key);

UPDATE shop_items SET
    description = 'Warm dawn palette for /chart. Auto-equips on buy; /equip switches.',
    metadata = '{"up":"#FF8C42","down":"#B85C7A","bg":"#1C1315","grid":"#FFFFFF","text":"#EAD4C0","accent":"#FF8C42","spine":"#3A2A30","muted":"#8A6E62","chart_color":"#FF8C42"}'::jsonb
WHERE key = 'theme_sunrise';

UPDATE shop_items SET
    description = 'Deep violet palette for /chart. Auto-equips on buy; /equip switches.',
    metadata = '{"up":"#7C4DFF","down":"#FF5C8A","bg":"#0D0F1E","grid":"#FFFFFF","text":"#A8B0D8","accent":"#7C4DFF","spine":"#232744","muted":"#5C6288","chart_color":"#7C4DFF"}'::jsonb
WHERE key = 'theme_midnight';

INSERT INTO shop_items (key, name, description, kind, price_minor, duration_days, metadata) VALUES
    ('theme_bloomberg', 'Terminal theme', 'Amber-on-black terminal palette for /chart. Auto-equips on buy; /equip switches.', 'COSMETIC', 400, NULL,
     '{"up":"#FFB000","down":"#FF6200","bg":"#0C0A06","grid":"#FFB000","text":"#FFB000","accent":"#FFB000","spine":"#33290F","muted":"#7A6230"}'),
    ('theme_vapor', 'Vaporwave theme', 'Neon teal/pink palette for /chart. Auto-equips on buy; /equip switches.', 'COSMETIC', 400, NULL,
     '{"up":"#29F3D6","down":"#FF71CE","bg":"#150A2E","grid":"#FFFFFF","text":"#B8A9E8","accent":"#FF71CE","spine":"#2E1E52","muted":"#6E5FA0"}'),
    ('theme_paper', 'Paper theme', 'Light print palette for /chart. Auto-equips on buy; /equip switches.', 'COSMETIC', 500, NULL,
     '{"up":"#2E8B57","down":"#C0392B","bg":"#F4F1E8","grid":"#000000","text":"#33302A","accent":"#8A7B4F","spine":"#C9C2B0","muted":"#9A9284","pill_text":"#FFFFFF"}'),
    ('theme_mono', 'Mono theme', 'Greyscale palette for /chart. Auto-equips on buy; /equip switches.', 'COSMETIC', 300, NULL,
     '{"up":"#E0E0E0","down":"#6E6E6E","bg":"#151515","grid":"#FFFFFF","text":"#C9C9C9","accent":"#B0B0B0","spine":"#2E2E2E","muted":"#7A7A7A"}');
