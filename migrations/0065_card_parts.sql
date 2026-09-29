-- Collectibles depth D: parts and assembled pieces. Parts are pack
-- pulls that live OUTSIDE the tier pools -- each pack gets one bonus
-- roll (pack.part_chance) for a PART card, so card-pull math and the
-- pull_cfg replay contract are untouched. Collect the full recipe and
-- /craft assembles the piece: parts are consumed, no shard cost.
--
-- PART copies = stackable held count (consumed on assemble); ASSEMBLED
-- pieces mint a serial like any card (cards.minted_count covers all
-- kinds). Neither enters the tier pools: load_pools selects only
-- INSTRUMENT and LORE kinds.

ALTER TABLE cards DROP CONSTRAINT cards_kind_check;
ALTER TABLE cards ADD CONSTRAINT cards_kind_check
    CHECK (kind IN ('INSTRUMENT', 'LORE', 'COMMEMORATIVE', 'PART', 'ASSEMBLED'));

CREATE TABLE card_recipes (
    result_key TEXT NOT NULL REFERENCES cards (key),
    part_key   TEXT NOT NULL REFERENCES cards (key),
    qty INT NOT NULL CHECK (qty > 0),
    PRIMARY KEY (result_key, part_key)
);

INSERT INTO cards (key, set_key, kind, instrument_id, name, flavor, sector_id, rarity, metadata) VALUES
    -- The Charter: five prospectus pages
    ('part_charter_cover', 'base', 'PART', NULL, 'Prospectus: Cover',
     'The IPO prospectus, page one. The logo is bigger than the risk section.', NULL, NULL, '{}'),
    ('part_charter_risk',  'base', 'PART', NULL, 'Prospectus: Risk Factors',
     '"Past performance" is doing a lot of work in this paragraph.', NULL, NULL, '{}'),
    ('part_charter_terms', 'base', 'PART', NULL, 'Prospectus: Terms',
     'Page 34 of 40. The subscription price is circled twice.', NULL, NULL, '{}'),
    ('part_charter_sig',   'base', 'PART', NULL, 'Prospectus: Signatures',
     'Signed in three inks; the fourth pen ran dry mid-initial.', NULL, NULL, '{}'),
    ('part_charter_seal',  'base', 'PART', NULL, 'Prospectus: The Seal',
     'Stamped by the Index Committee. The ink was barely dry before the pop.', NULL, NULL, '{}'),
    -- The Golden Ticket: four stubs
    ('part_ticket_a', 'base', 'PART', NULL, 'Golden Stub I',
     'First stub of the set. It smells faintly of a jackpot.', NULL, NULL, '{}'),
    ('part_ticket_b', 'base', 'PART', NULL, 'Golden Stub II',
     'The perforation was torn with unusual confidence.', NULL, NULL, '{}'),
    ('part_ticket_c', 'base', 'PART', NULL, 'Golden Stub III',
     'Rumor says whoever holds all four gets to ring the bell.', NULL, NULL, '{}'),
    ('part_ticket_d', 'base', 'PART', NULL, 'Golden Stub IV',
     'The last stub. It always is.', NULL, NULL, '{}'),
    -- Assembled pieces
    ('asm_charter', 'base', 'ASSEMBLED', NULL, 'The Charter',
     'All five pages of the founding prospectus, bound. The exchange''s birth certificate.', NULL, NULL, '{}'),
    ('asm_golden_ticket', 'base', 'ASSEMBLED', NULL, 'The Golden Ticket',
     'Four stubs, one legend. Admits one believer to the moon.', NULL, NULL, '{}')
ON CONFLICT DO NOTHING;

INSERT INTO card_recipes (result_key, part_key, qty) VALUES
    ('asm_charter', 'part_charter_cover', 1),
    ('asm_charter', 'part_charter_risk',  1),
    ('asm_charter', 'part_charter_terms', 1),
    ('asm_charter', 'part_charter_sig',   1),
    ('asm_charter', 'part_charter_seal',  1),
    ('asm_golden_ticket', 'part_ticket_a', 1),
    ('asm_golden_ticket', 'part_ticket_b', 1),
    ('asm_golden_ticket', 'part_ticket_c', 1),
    ('asm_golden_ticket', 'part_ticket_d', 1)
ON CONFLICT DO NOTHING;

INSERT INTO config (key, value) VALUES
    ('pack.part_chance',  0.35),
    ('pack.shards_part',  10)
ON CONFLICT DO NOTHING;
