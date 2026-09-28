-- R4: second venue -- Asia Exchange. 600 open / 840 closed ticks with
-- offset 720 (AS opens 720 ticks into the US cycle, overlapping US's
-- close then trading through it). overnight_var_ticks stays 60 -- the
-- ABSOLUTE stochastic-gap horizon (0053): 60/840 = 0.0714 effective
-- overnight-variance fraction on the longer close, exactly the plan's
-- recalibration. 40 stocks across the same eight sectors plus a
-- venue-scoped cap-weighted index (ASX40) on the SBX-40 divisor
-- convention (basket float-cap / 1000 -> opens at 1000). Params mirror
-- the US seed distributions; float_shares uses the 0011 backfill
-- formula so margin/index math treats both venues identically.

INSERT INTO markets
    (code, name, open_ticks, closed_ticks, offset_ticks,
     tick_size, auction_ticks, overnight_var_ticks)
VALUES ('AS', 'Asia Exchange', 600, 840, 720, 0.01, 30, 60)
ON CONFLICT (code) DO NOTHING;

INSERT INTO instruments (
    ticker, name, sector_id,
    drift, sigma, beta, gamma, kappa, fundamental_sigma,
    liquidity, lambda_impact, tau_ticks, max_impact,
    fundamental_value, base_price, impact, quoted_price,
    market_id, index_member, float_shares, adv
)
SELECT v.ticker, v.name, s.id,
       v.drift, v.sigma, v.beta, v.gamma, v.kappa, v.fundamental_sigma,
       v.liquidity, v.lambda_impact, v.tau_ticks, v.max_impact,
       v.base_price, v.base_price, 0, v.base_price,
       m.id, TRUE,
       GREATEST(10_000, ROUND(v.liquidity * 50 / v.base_price)),
       -- 0032 warm-start: adv=0 clamps effective liquidity to the floor
       -- until the window fills; seed it like add_instrument does.
       v.liquidity * COALESCE(
           (SELECT value FROM config WHERE key = 'flow.adv_ref_frac'),
           0.000000025)
FROM (VALUES
    ('TOYO', 'Toyoden Technology', 'TECH', -2.927614e-05, 0.0005208747, 0.669, 0.983, 0.02701, 7.224731e-05, 1878996.29, 0.4052, 129.63, 0.03, 43.14::numeric),
    ('SENK', 'Senkai Technology', 'TECH', 1.891067e-06, 0.0005474288, 1.251, 0.971, 0.01501, 7.510536e-05, 5663478.84, 0.7523, 207.76, 0.03, 60.44::numeric),
    ('MIRA', 'Mirai Technology', 'TECH', 1.137667e-05, 0.000349823, 0.983, 1.027, 0.01638, 5.708186e-05, 1902475.65, 0.5854, 167.93, 0.03, 21.21::numeric),
    ('KAIZ', 'Kaizen Technology', 'TECH', -1.097686e-05, 0.0001506834, 0.681, 0.922, 0.02504, 2.052542e-05, 1764429.41, 1.1612, 222.21, 0.03, 39.78::numeric),
    ('JADE', 'Jade Circuit Technology', 'TECH', 1.220668e-05, 0.0004013577, 0.689, 1.076, 0.01552, 6.695594e-05, 6377511.68, 0.8410, 214.27, 0.03, 59.18::numeric),
    ('SAIG', 'Saigon Financials', 'FIN', -8.718218e-06, 0.0002019392, 1.083, 1.102, 0.01134, 2.803694e-05, 3395236.74, 1.2915, 171.65, 0.03, 60.66::numeric),
    ('MUMB', 'Mumbai Financials', 'FIN', -1.063381e-05, 0.0003901384, 0.926, 0.919, 0.02202, 6.857349e-05, 2339686.68, 1.0648, 194.17, 0.03, 21.29::numeric),
    ('MANI', 'Manila Financials', 'FIN', -2.050133e-05, 0.0001921252, 0.724, 1.083, 0.02900, 3.152736e-05, 5559040.63, 0.5220, 138.69, 0.03, 45.21::numeric),
    ('JAKA', 'Jakarta Financials', 'FIN', -1.838914e-05, 0.0005030077, 0.504, 1.026, 0.01908, 7.187928e-05, 5876618.48, 1.3263, 98.34, 0.03, 90.21::numeric),
    ('HONG', 'Harbor Financials Asia', 'FIN', 2.022378e-05, 0.0002909714, 1.001, 1.047, 0.02482, 4.73292e-05, 4431636.05, 0.7954, 178.41, 0.03, 16.99::numeric),
    ('GENK', 'Genki Healthcare', 'HEALTH', 2.418619e-06, 9.028822e-05, 0.963, 1.050, 0.02065, 1.35052e-05, 2965990.45, 0.8653, 219.82, 0.03, 45.51::numeric),
    ('BANY', 'Banyan Healthcare', 'HEALTH', 3.958162e-05, 0.0005051474, 1.235, 1.014, 0.01554, 7.074733e-05, 3496085.21, 0.3996, 171.24, 0.03, 13.47::numeric),
    ('ORCH', 'Orchid Healthcare', 'HEALTH', 2.54335e-06, 0.0005298781, 0.771, 1.041, 0.01477, 7.601665e-05, 2910880.53, 0.9812, 215.56, 0.03, 38.76::numeric),
    ('PEAK', 'Peak Healthcare Asia', 'HEALTH', -7.689382e-07, 0.0004090951, 0.936, 0.992, 0.02162, 5.480694e-05, 6432288.89, 1.2609, 176.59, 0.03, 83.10::numeric),
    ('TULI', 'Tulip Healthcare', 'HEALTH', 2.459686e-05, 0.0001731483, 1.247, 0.935, 0.02400, 2.721911e-05, 4800513.89, 0.4042, 183.07, 0.03, 69.39::numeric),
    ('MONS', 'Monsoon Energy', 'ENERGY', -3.04958e-05, 0.0003391016, 0.883, 0.976, 0.01080, 6.082544e-05, 4472055.96, 1.2039, 195.20, 0.03, 75.12::numeric),
    ('DELT', 'Delta Energy Asia', 'ENERGY', -7.674122e-06, 7.073045e-05, 0.915, 1.138, 0.01231, 8.496253e-06, 6010317.02, 0.7253, 188.45, 0.03, 63.51::numeric),
    ('VOLC', 'Volcano Energy', 'ENERGY', -3.173409e-05, 0.0004767308, 0.587, 1.090, 0.02635, 6.138654e-05, 2431705.13, 0.9682, 103.74, 0.03, 19.97::numeric),
    ('REEF', 'Reef Energy', 'ENERGY', -3.962379e-05, 0.0005222833, 1.244, 1.017, 0.01737, 8.856023e-05, 4054596.96, 0.8540, 158.02, 0.03, 75.12::numeric),
    ('STRA', 'Strait Energy', 'ENERGY', -1.592309e-05, 0.0004188219, 0.973, 0.913, 0.00913, 6.098075e-05, 5283050.33, 1.3042, 110.51, 0.03, 65.82::numeric),
    ('LANT', 'Lantern Consumer', 'CONSUMER', 2.710429e-05, 0.0005048037, 1.044, 0.914, 0.01930, 8.861339e-05, 2893701.37, 1.1136, 171.88, 0.03, 56.89::numeric),
    ('SILK', 'Silk Road Consumer', 'CONSUMER', 3.176155e-05, 0.0002631066, 1.069, 0.960, 0.01891, 3.447526e-05, 6315513.63, 1.1400, 107.34, 0.03, 20.55::numeric),
    ('PAGA', 'Pagoda Consumer', 'CONSUMER', -2.063206e-05, 0.0001208162, 0.728, 1.079, 0.02311, 1.844629e-05, 5881721.83, 0.5780, 154.33, 0.03, 67.73::numeric),
    ('BAMB', 'Bamboo Consumer', 'CONSUMER', -1.701696e-05, 0.0004850509, 0.896, 1.112, 0.00962, 8.458141e-05, 1828567.67, 0.9996, 110.64, 0.03, 73.02::numeric),
    ('RICE', 'Ricefield Consumer', 'CONSUMER', 2.829456e-07, 0.0005887391, 0.568, 1.120, 0.01013, 0.0001044902, 7053699.32, 0.6815, 160.22, 0.03, 52.38::numeric),
    ('SHIP', 'Shipyard Industrials', 'INDUSTRIAL', 3.224251e-05, 0.0004869081, 0.523, 0.869, 0.00858, 8.068261e-05, 3124446.28, 0.6273, 150.15, 0.03, 91.65::numeric),
    ('FORG', 'Forge Industrials Asia', 'INDUSTRIAL', 4.197195e-05, 0.0005062128, 1.265, 1.024, 0.00923, 7.905271e-05, 3312654.45, 0.4681, 172.15, 0.03, 85.24::numeric),
    ('TIDE', 'Tidewater Industrials', 'INDUSTRIAL', 9.189216e-06, 0.0002699316, 0.792, 1.063, 0.00993, 3.458283e-05, 5809010.93, 0.9418, 206.99, 0.03, 82.97::numeric),
    ('CRAN', 'Crane Industrials', 'INDUSTRIAL', 2.63453e-05, 7.606315e-05, 0.716, 0.922, 0.01733, 1.122372e-05, 6643417.19, 0.5823, 194.16, 0.03, 68.30::numeric),
    ('VIST', 'Vista Industrials', 'INDUSTRIAL', 2.223464e-05, 0.0002509118, 0.954, 0.964, 0.01776, 4.306504e-05, 5999775.60, 1.1361, 109.66, 0.03, 81.95::numeric),
    ('GRID', 'Gridline Utilities', 'UTILITIES', -1.359258e-06, 6.022893e-05, 1.170, 0.973, 0.00888, 7.388451e-06, 5818865.08, 0.5075, 139.37, 0.03, 44.71::numeric),
    ('AQUA', 'Aqueduct Utilities', 'UTILITIES', -3.47689e-05, 5.046612e-05, 1.091, 1.124, 0.02908, 6.655697e-06, 4380463.61, 0.5503, 147.80, 0.03, 84.65::numeric),
    ('SOLA', 'Solaris Utilities', 'UTILITIES', -3.780283e-05, 9.26546e-05, 0.517, 0.974, 0.01685, 1.664154e-05, 1640557.34, 0.7911, 131.17, 0.03, 74.09::numeric),
    ('WINDA', 'Windfield Utilities', 'UTILITIES', 3.170143e-06, 6.055998e-05, 0.835, 1.078, 0.02860, 1.031301e-05, 4268425.73, 1.2283, 190.08, 0.03, 10.70::numeric),
    ('DAMM', 'Damson Utilities', 'UTILITIES', -4.233781e-05, 0.0001109529, 1.010, 0.882, 0.02229, 1.661815e-05, 5251795.92, 0.8591, 157.44, 0.03, 65.03::numeric),
    ('IRONO', 'Ironworks Materials', 'MATERIALS', 4.206183e-05, 8.584665e-05, 0.652, 0.980, 0.01081, 1.431852e-05, 3567997.68, 0.7314, 125.14, 0.03, 67.10::numeric),
    ('COPR', 'Copperline Materials', 'MATERIALS', 2.540276e-05, 0.0002404042, 1.281, 1.047, 0.02550, 3.649647e-05, 2445750.94, 1.1430, 135.57, 0.03, 60.47::numeric),
    ('QUAR', 'Quarry Materials', 'MATERIALS', 7.895312e-06, 0.0004109361, 0.966, 0.862, 0.01720, 5.866728e-05, 5151691.77, 1.1318, 106.59, 0.03, 73.02::numeric),
    ('SLAT', 'Slate Materials', 'MATERIALS', 4.055669e-05, 0.0001313286, 1.004, 0.990, 0.01713, 1.917343e-05, 1821003.69, 0.4103, 176.30, 0.03, 71.02::numeric),
    ('CLAY', 'Claystone Materials', 'MATERIALS', 1.212347e-05, 6.018457e-05, 0.653, 1.055, 0.01581, 1.049452e-05, 1912220.63, 0.7210, 144.82, 0.03, 63.17::numeric)
) AS v(ticker, name, sector_key, drift, sigma, beta, gamma, kappa,
       fundamental_sigma, liquidity, lambda_impact, tau_ticks,
       max_impact, base_price)
JOIN sectors s ON s.key = v.sector_key
JOIN markets m ON m.code = 'AS'
ON CONFLICT (ticker) DO NOTHING;

-- ASX-40: cap-weighted over the AS basket (venue-scoped -- the tick's
-- index computation already filters members by market_id).
INSERT INTO instruments (
    ticker, name, sector_id, kind,
    drift, sigma, beta, gamma, kappa, fundamental_sigma,
    liquidity, lambda_impact, tau_ticks, max_impact,
    fundamental_value, base_price, impact, quoted_price,
    init_margin_pct, maint_margin_pct, float_shares, index_divisor,
    short_knockout_pct, market_id, adv
)
SELECT
    'ASX40', 'ASX-40 Index', s.id, 'INDEX',
    0, 0, 0, 0, 0, 0,
    50_000_000, 0.000001, 50, 0.03,
    1000, 1000, 0, 1000,
    0.20, 0.10, 0, d.divisor,
    0.25, m.id,
    50_000_000 * COALESCE(
        (SELECT value FROM config WHERE key = 'flow.adv_ref_frac'),
        0.000000025)
FROM sectors s,
     markets m,
     (SELECT SUM(i.float_shares * i.base_price) / 1000 AS divisor
      FROM instruments i
      JOIN markets mi ON mi.id = i.market_id
      WHERE i.kind = 'STOCK' AND i.index_member AND mi.code = 'AS') d
WHERE s.key = 'index' AND m.code = 'AS'
  AND d.divisor IS NOT NULL
ON CONFLICT (ticker) DO NOTHING;
