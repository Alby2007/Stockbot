-- Collectibles: instrument cards, lore cards, frames, packs, shards.
--
-- Design anchors (plan C1-C9):
-- * Frozen sets: card_sets snapshots membership at creation. IPOs and
--   delistings mutate `instruments`, never a released set -- completion
--   denominators, pull pools, and past pull outcomes stay immutable.
-- * One row per (user, card): frames UPGRADE in place; equal-or-lower
--   pulls burn to shards. Shards are off-ledger (C9): minted only by
--   duplicate burns, spent only by crafting, never convertible to cash.
-- * card_pulls is the audit trail making provably-fair verifiable:
--   seed = HMAC(master_seed|packs, user_id|pull_seq) is a pure function
--   of recorded inputs, pity state included.

CREATE TABLE card_sets (
    key TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    released_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    in_pack_pool BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE TABLE cards (
    key TEXT PRIMARY KEY,
    set_key TEXT NOT NULL REFERENCES card_sets(key),
    kind TEXT NOT NULL CHECK (kind IN ('INSTRUMENT','LORE','COMMEMORATIVE')),
    instrument_id INT REFERENCES instruments(id),
    name TEXT NOT NULL,
    flavor TEXT NOT NULL,
    sector_id SMALLINT REFERENCES sectors(id),
    rarity TEXT,
    metadata JSONB NOT NULL DEFAULT '{}',
    CHECK ((kind = 'INSTRUMENT') = (instrument_id IS NOT NULL)),
    CHECK ((kind = 'LORE') = (rarity IS NOT NULL)),
    CHECK ((kind = 'LORE') = (rarity IN ('EPIC','LEGENDARY')))
);

-- C4: one row per (user, card); best_frame is the quality held.
CREATE TABLE user_cards (
    user_id BIGINT NOT NULL REFERENCES users(id),
    card_key TEXT NOT NULL REFERENCES cards(key),
    best_frame TEXT NOT NULL,
    copies INT NOT NULL DEFAULT 1 CHECK (copies > 0),
    first_acquired_tick BIGINT,
    upgraded_at TIMESTAMPTZ,
    PRIMARY KEY (user_id, card_key)
);
CREATE INDEX user_cards_user ON user_cards(user_id);

CREATE TABLE card_pulls (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(id),
    pull_seq INT NOT NULL,
    pack_key TEXT NOT NULL,
    pity_count_before INT NOT NULL,
    tier TEXT NOT NULL,
    card_key TEXT NOT NULL REFERENCES cards(key),
    outcome TEXT NOT NULL CHECK (outcome IN ('NEW','UPGRADE','DUPLICATE')),
    shards_awarded INT NOT NULL DEFAULT 0,
    tick_index BIGINT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id, pull_seq)
);

ALTER TABLE users
    ADD COLUMN shards INT NOT NULL DEFAULT 0 CHECK (shards >= 0),
    ADD COLUMN pack_pulls INT NOT NULL DEFAULT 0,
    ADD COLUMN pity_count INT NOT NULL DEFAULT 0,
    ADD COLUMN featured_card TEXT REFERENCES cards(key);

INSERT INTO card_sets (key, name, in_pack_pool) VALUES
    ('base', 'Base Set', TRUE),
    ('commemoratives', 'Commemoratives', FALSE)
ON CONFLICT DO NOTHING;

-- ---------------------------------------------------------------------------
-- Base Set: every instrument listed at launch (C1 freeze).
-- Flavor is authored per card and lives on the card row, not instruments.
-- ---------------------------------------------------------------------------
INSERT INTO cards (key, set_key, kind, instrument_id, name, flavor, sector_id, metadata)
SELECT
    'card_' || lower(i.ticker),
    'base',
    'INSTRUMENT',
    i.id,
    i.name,
    f.flavor,
    i.sector_id,
    '{}'::jsonb
FROM instruments i
JOIN (VALUES
    ('NORT',  'Filed its first patent in a garage that still stands behind HQ.'),
    ('SOUT',  'Moves quietly, ships constantly, and never misses a roadmap date.'),
    ('EAST',  'Sunrise-side campuses; the ticker that wakes the board first.'),
    ('WEST',  'Volatile as the weather it forecasts -- and twice as hard to predict.'),
    ('GLOB',  'Sells the same server rack on six continents under six names.'),
    ('UNIT',  'The oldest bank on the board; its vault predates the exchange.'),
    ('PACI',  'Finances half the shipping lanes and insures the other half.'),
    ('ATLA',  'Underwrites risk nobody else will read the paperwork on.'),
    ('SUMM',  'Built its tower on the highest hill so the logo reads first.'),
    ('HORI',  'Trades on fundamentals so boring they became a moat.'),
    ('VERT',  'Runs clinical trials the way rivals run marketing campaigns.'),
    ('NOVA',  'Every cure it patents starts as an accident it refuses to explain.'),
    ('PRIM',  'The first call in a panic and the last one billed.'),
    ('MERI',  'Charts patient outcomes the way cartographers chart coastlines.'),
    ('APEX',  'Acquires a biotech startup every quarter and a grudge every year.'),
    ('ORBI',  'Pumps from fields so remote the payroll includes pilots.'),
    ('BEAC',  'Lights harbors on three coasts and budgets like a lighthouse keeper.'),
    ('CASC',  'Turns every mountain river into a line item.'),
    ('IRON',  'Drills where the charts say not to, and is usually right.'),
    ('STER',  'Silver-haired board, silver-standard dividends.'),
    ('REDW',  'Sells furniture built to outlast the showroom.'),
    ('GRAN',  'The grocery chain that prices like it personally grew the lettuce.'),
    ('SILV',  'Premium goods for customers who read the stitching first.'),
    ('BLUE',  'Sells the wave before it breaks -- wetsuits, boards, and vibes.'),
    ('CRES',  'Rides trends up and somehow always exits before the crash.'),
    ('FALC',  'Builds the machines that build everything else.'),
    ('HARB',  'Cranes, docks, and drydocks -- the skyline is its brochure.'),
    ('TIMB',  'Harvests forests on a hundred-year rotation and a hundred-day budget.'),
    ('CORA',  'Salvage rights to half the seabed and lawyers for the rest.'),
    ('WIND',  'Turbines on every ridge the zoning board forgot about.'),
    ('ANCH',  'The grid''s ballast: unglamorous, unmovable, unmissed until it trips.'),
    ('LUME',  'Meters light the way auditors meter decimals.'),
    ('ZENI',  'Peak-hour pricing as a business model, serenity as a brand.'),
    ('BRIG',  'Keeps the lights on first and answers questions later.'),
    ('CLEA',  'Water so clean the CFO drinks it on earnings calls.'),
    ('RIDG',  'Mines the ridge it is named after and named after nothing else.'),
    ('STON',  'Bridges that outlive the contracts that built them.'),
    ('FIEL',  'Quarries patiently; its rocks predate the market and will postdate it.'),
    ('COPP',  'Wires the world and charges by the foot.'),
    ('EVER',  'Grows timber slower than its dividends and twice as reliably.'),
    ('SBX40', 'Forty tickers in a trench coat pretending to be one company.'),
    ('TOYO',  'Ships electronics the rest of the world reverse-engineers.'),
    ('SENK',  'Precision fabs measured in nanometers and national pride.'),
    ('MIRA',  'Its demos predict next year''s products with unsettling accuracy.'),
    ('KAIZ',  'Improves everything 1% a day, including its own improvement process.'),
    ('JADE',  'Circuits etched like artwork and priced like it too.'),
    ('SAIG',  'Banks the fastest-growing corridor on the exchange.'),
    ('MUMB',  'Moves money through monsoon season without blinking.'),
    ('MANI',  'The archipelago''s clearinghouse, typhoon-tested annually.'),
    ('JAKA',  'Underwrites trade routes older than most indexes.'),
    ('HONG',  'Where the harbor''s ledgers meet the mainland''s ambitions.'),
    ('GENK',  'Vitality sold by the vial, researched by the generation.'),
    ('BANY',  'Grows healthcare networks the way its namesake grows roots.'),
    ('ORCH',  'Rare treatments, rarer margins, endless demand.'),
    ('PEAK',  'Summit clinics where altitude is a feature, not a symptom.'),
    ('TULI',  'Knows a thing or two about bubbles and prices accordingly.'),
    ('MONS',  'Generates power on a schedule the weather writes.'),
    ('DELT',  'Channels a river delta into the grid, silt and all.'),
    ('VOLC',  'Geothermal from a volcano that only occasionally objects.'),
    ('REEF',  'Tidal turbines guarded by the reef they were built around.'),
    ('STRA',  'Ferries power across the strait it is named after.'),
    ('LANT',  'Lights festivals for a thousand years, bills for thirty days.'),
    ('SILK',  'Trades along the oldest route on the map and the newest spreadsheet.'),
    ('PAGA',  'Consumer goods stacked like its namesake: tier upon tier.'),
    ('BAMB',  'Grows product lines faster than anyone can count them.'),
    ('RICE',  'Feeds the region and hedges the harvest.'),
    ('SHIP',  'Builds hulls for every flag that pays on time.'),
    ('FORG',  'Stamps steel so hard the futures market feels it.'),
    ('TIDE',  'Dredges, builds, and reclaims -- the coastline is a work in progress.'),
    ('CRAN',  'If it can be lifted, CRAN already quoted the job.'),
    ('VIST',  'Constructs towers tall enough to see the competition coming.'),
    ('GRID',  'Wires the archipelago one substation at a time.'),
    ('AQUA',  'Moves water uphill when the rate case justifies it.'),
    ('SOLA',  'Harvests the equatorial sun and sells it back at dusk.'),
    ('WINDA', 'Wind farms on passes the maps label scenic.'),
    ('DAMM',  'Hydroelectric gravity, monetized daily.'),
    ('IRONO', 'Smelts ore into the beams half the skyline stands on.'),
    ('COPR',  'Digging copper where geologists said check again.'),
    ('QUAR',  'Cuts stone blocks the old way: profitably.'),
    ('SLAT',  'Roofing slate for three generations of weather.'),
    ('CLAY',  'Turns riverbed clay into ceramics and ceramics into margins.'),
    ('ASX40', 'The east half of the world in a single ticker.')
) AS f(ticker, flavor) ON f.ticker = i.ticker
ON CONFLICT DO NOTHING;

-- ---------------------------------------------------------------------------
-- Lore cards: the market's characters. EPIC/LEGENDARY pack tiers only.
-- ---------------------------------------------------------------------------
INSERT INTO cards (key, set_key, kind, instrument_id, name, flavor, sector_id, rarity, metadata) VALUES
    ('lore_faucet',        'base', 'LORE', NULL, 'The Faucet',             'Drips exactly what the economy can afford to lose, and never apologizes for the pressure.', NULL, 'EPIC',      '{}'),
    ('lore_sink',          'base', 'LORE', NULL, 'The Sink',               'Every fee, every pack, every vanity purchase ends here. It does not give refunds; it gives silence.', NULL, 'LEGENDARY', '{}'),
    ('lore_market_maker',  'base', 'LORE', NULL, 'The Market Maker',       'Quotes both sides of everything, sleeps on a pile of filled orders, and calls the spread "rent".', NULL, 'LEGENDARY', '{}'),
    ('lore_insurance',     'base', 'LORE', NULL, 'The Insurance Fund',     'Paid for by other people''s liquidations. Its job is to never be needed and always be blamed.', NULL, 'EPIC',      '{}'),
    ('lore_liquidator',    'base', 'LORE', NULL, 'The Liquidator',         'Arrives at exactly maintenance margin, sells everything at the worst price, and files a tidy report.', NULL, 'LEGENDARY', '{}'),
    ('lore_auditor',       'base', 'LORE', NULL, 'The Auditor',            'Reconciles every ledger entry since genesis. Found one discrepancy once. Nobody asks about it.', NULL, 'EPIC',      '{}'),
    ('lore_breaker',       'base', 'LORE', NULL, 'The Circuit Breaker',    'Stops the market the way a fire alarm stops a conversation: loudly, at the worst moment, correctly.', NULL, 'EPIC',      '{}'),
    ('lore_whale',         'base', 'LORE', NULL, 'The Whale',              'Splashes $5,000 through an order book and is genuinely surprised the water moved.', NULL, 'EPIC',      '{}'),
    ('lore_grinder',       'base', 'LORE', NULL, 'The Grinder',            'Bets 15% of the balance every tick, compounding patience the way others compound interest.', NULL, 'EPIC',      '{}'),
    ('lore_yolo',          'base', 'LORE', NULL, 'The YOLO Kid',           'Ninety-five percent of the account on one candle. Either a genius or a cautionary tale -- TBD.', NULL, 'EPIC',      '{}'),
    ('lore_shorter',       'base', 'LORE', NULL, 'The Shorter',            'Profits on the way down, pays borrow fees like rent, and calls every rally a dead-cat bounce.', NULL, 'EPIC',      '{}'),
    ('lore_lp',            'base', 'LORE', NULL, 'The Liquidity Provider', 'Rests orders on both sides forever, earning the spread one patient tick at a time.', NULL, 'EPIC',      '{}'),
    ('lore_stopmonk',      'base', 'LORE', NULL, 'The Stop-Loss Monk',     'Trails every position by exactly enough to leave before regret arrives. Meditates on drawdown.', NULL, 'EPIC',      '{}'),
    ('lore_oracle',        'base', 'LORE', NULL, 'The Oracle',             'Schedules the earnings, lands the news, and never once says what the number will be.', NULL, 'LEGENDARY', '{}'),
    ('lore_committee',     'base', 'LORE', NULL, 'The Index Committee',    'Decides which forty names get to be "the market" this season. Membership is the whole moat.', NULL, 'LEGENDARY', '{}')
ON CONFLICT DO NOTHING;

-- ---------------------------------------------------------------------------
-- Commemoratives (C8): minted by events, never in the pack pool.
-- Backfill credits history players already lived through.
-- ---------------------------------------------------------------------------
INSERT INTO cards (key, set_key, kind, instrument_id, name, flavor, sector_id, rarity, metadata) VALUES
    ('comm_ipo_subscriber',  'commemoratives', 'COMMEMORATIVE', NULL, 'First Offering',    'Subscribed before the bell on the exchange''s first IPO wave.', NULL, NULL, '{}'),
    ('comm_halt_survivor',   'commemoratives', 'COMMEMORATIVE', NULL, 'Halt Survivor',     'Held a position through a circuit-breaker halt and lived to screenshot it.', NULL, NULL, '{}')
ON CONFLICT DO NOTHING;

-- Backfill: every past IPO subscriber earns the First Offering card.
INSERT INTO user_cards (user_id, card_key, best_frame)
SELECT DISTINCT s.user_id, 'comm_ipo_subscriber', 'STANDARD'
FROM ipo_subscriptions s
ON CONFLICT DO NOTHING;

-- Backfill: holders of an instrument that has recorded a halt candle earn
-- the survivor card (position history isn't snapshotted; holding through
-- the recorded halt is the closest honest claim).
INSERT INTO user_cards (user_id, card_key, best_frame)
SELECT DISTINCT p.user_id, 'comm_halt_survivor', 'STANDARD'
FROM positions p
WHERE p.quantity <> 0
  AND p.season_id IS NULL  -- main-economy holds only; league positions don't count
  AND EXISTS (
      SELECT 1 FROM candles c
      WHERE c.instrument_id = p.instrument_id AND c.halt_kind IS NOT NULL
  )
ON CONFLICT DO NOTHING;

-- Pack pricing + pull math config (tunable via /admin tune).
INSERT INTO shop_items (key, name, description, kind, price_minor, metadata) VALUES
    ('pack_basic',   'Card pack',         'Three cards from the Base Set: instruments at STANDARD-PLATINUM frames, lore at EPIC+.',                         'CONSUMABLE', 800,  '{"cards":3}'),
    ('pack_premium', 'Premium card pack', 'Three cards; the third is guaranteed GOLD frame or better.',                                                     'CONSUMABLE', 2000, '{"cards":3,"floor":"GOLD"}')
ON CONFLICT DO NOTHING;

INSERT INTO config (key, value) VALUES
    ('pack.rate_standard',  55),
    ('pack.rate_silver',    25),
    ('pack.rate_gold',      12),
    ('pack.rate_platinum',  5),
    ('pack.rate_epic',      2.4),
    ('pack.rate_legendary', 0.6),
    ('pack.pity_threshold', 20),
    ('pack.shards_standard',  4),
    ('pack.shards_silver',    8),
    ('pack.shards_gold',      20),
    ('pack.shards_platinum',  50),
    ('pack.shards_epic',      80),
    ('pack.shards_legendary', 150),
    ('pack.craft_standard',   60),
    ('pack.craft_silver',     100),
    ('pack.craft_gold',       200),
    ('pack.craft_platinum',   350)
ON CONFLICT DO NOTHING;
