-- Collectible completion badges (K4): grant-only BADGE rows the daily
-- `evaluate_badges` sweep awards via the 'cards' metric class.
-- metadata filters: card_set (card_sets.key), card_kind, min_frame
-- (frame rank floor -- STANDARD..LEGENDARY), threshold (held count).

INSERT INTO shop_items (key, name, description, kind, price_minor, metadata) VALUES
    ('badge_base_set',   'Base Set Complete', 'Owns every instrument card in the Base Set (82/82).',        'BADGE', NULL, '{"emoji":"📒","metric":"cards","card_set":"base","card_kind":"INSTRUMENT","threshold":82}'),
    ('badge_lore_set',   'Loremaster',        'Owns every lore card in the Base Set (15/15).',              'BADGE', NULL, '{"emoji":"📜","metric":"cards","card_set":"base","card_kind":"LORE","threshold":15}'),
    ('badge_gold_set',   'Gilded Portfolio',  'Owns every instrument card in the Base Set at Gold frame or better.', 'BADGE', NULL, '{"emoji":"🥇","metric":"cards","card_set":"base","card_kind":"INSTRUMENT","min_frame":"GOLD","threshold":82}');
