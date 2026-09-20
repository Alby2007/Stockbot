-- Seed data: 8 sectors, 40 instruments. Placeholder names/tickers per the
-- design doc's open items; swap for real flavor whenever. Params are
-- generated (not hand-tuned) to give a plausible spread of vol/beta/liquidity
-- across sectors -- expect to retune against the Phase 1 simulation harness.

INSERT INTO sectors (key, name) VALUES
    ('TECH', 'Technology'),
    ('FIN', 'Financials'),
    ('HEALTH', 'Healthcare'),
    ('ENERGY', 'Energy'),
    ('CONSUMER', 'Consumer Goods'),
    ('INDUSTRIAL', 'Industrials'),
    ('UTILITIES', 'Utilities'),
    ('MATERIALS', 'Materials');

INSERT INTO instruments (
    ticker, name, sector_id,
    drift, sigma, beta, gamma, kappa, fundamental_sigma,
    liquidity, lambda_impact, tau_ticks, max_impact,
    fundamental_value, base_price, impact, quoted_price
)
SELECT v.ticker, v.name, s.id,
       v.drift, v.sigma, v.beta, v.gamma, v.kappa, v.fundamental_sigma,
       v.liquidity, v.lambda_impact, v.tau_ticks, v.max_impact,
       v.base_price, v.base_price, 0, v.base_price
FROM (VALUES
    ('NORT', 'North Technology', 'TECH', 3.2951e-05, 0.0001778049, 0.993, 0.939, 0.02873, 2.66707e-05, 4016903.92, 0.7259, 166.87, 0.03, 59.93::numeric),
    ('SOUT', 'South Technology', 'TECH', -4.1128e-06, 0.0001923804, 0.99, 0.934, 0.026108, 2.88571e-05, 2078527.91, 0.8209, 170.84, 0.03, 94.0::numeric),
    ('EAST', 'East Technology', 'TECH', -1.63391e-05, 0.0002517009, 1.234, 1.044, 0.02326, 3.77551e-05, 2516310.95, 1.1559, 218.93, 0.03, 80.21::numeric),
    ('WEST', 'West Technology', 'TECH', 2.35804e-05, 0.000573476, 1.095, 0.95, 0.018022, 8.60214e-05, 4153129.0, 1.0131, 204.26, 0.03, 34.3::numeric),
    ('GLOB', 'Global Technology', 'TECH', 1.47722e-05, 0.0004549613, 1.256, 0.979, 0.029664, 6.82442e-05, 2862817.42, 1.0615, 222.0, 0.03, 86.45::numeric),
    ('UNIT', 'United Financials', 'FIN', -7.9119e-06, 0.0001093941, 1.075, 1.056, 0.010933, 1.64091e-05, 6055988.95, 0.5139, 201.7, 0.03, 24.51::numeric),
    ('PACI', 'Pacific Financials', 'FIN', 4.221e-06, 0.0001064091, 1.066, 1.004, 0.009611, 1.59614e-05, 4473236.25, 0.4967, 166.17, 0.03, 65.15::numeric),
    ('ATLA', 'Atlantic Financials', 'FIN', 1.0437e-05, 0.000111125, 0.838, 0.992, 0.026514, 1.66687e-05, 4959602.88, 0.6279, 119.51, 0.03, 58.02::numeric),
    ('SUMM', 'Summit Financials', 'FIN', -1.35473e-05, 0.0001207062, 0.837, 0.996, 0.014719, 1.81059e-05, 6314302.18, 0.5738, 217.56, 0.03, 22.83::numeric),
    ('HORI', 'Horizon Financials', 'FIN', 1.90007e-05, 0.0001148492, 1.05, 1.059, 0.016315, 1.72274e-05, 5393449.34, 0.6212, 213.04, 0.03, 24.33::numeric),
    ('VERT', 'Vertex Healthcare', 'HEALTH', 2.82326e-05, 0.0001671606, 1.078, 0.94, 0.024273, 2.50741e-05, 4679078.78, 0.5868, 151.06, 0.03, 26.47::numeric),
    ('NOVA', 'Nova Healthcare', 'HEALTH', -1.79942e-05, 0.0001866392, 0.968, 1.143, 0.016962, 2.79959e-05, 4783108.95, 0.7961, 92.06, 0.03, 49.73::numeric),
    ('PRIM', 'Prime Healthcare', 'HEALTH', 3.95317e-05, 0.0004257078, 0.937, 0.863, 0.014801, 6.38562e-05, 3166145.76, 0.7393, 109.47, 0.03, 62.23::numeric),
    ('MERI', 'Meridian Healthcare', 'HEALTH', -1.8458e-05, 0.0003584642, 0.866, 0.931, 0.009473, 5.37696e-05, 3085482.62, 0.5762, 201.12, 0.03, 59.57::numeric),
    ('APEX', 'Apex Healthcare', 'HEALTH', 3.59801e-05, 0.0001422191, 0.852, 1.137, 0.024049, 2.13329e-05, 4699106.86, 0.4706, 187.24, 0.03, 68.64::numeric),
    ('ORBI', 'Orbit Energy', 'ENERGY', 4.537e-07, 0.0001741431, 1.062, 0.902, 0.025446, 2.61215e-05, 3247073.4, 0.8351, 135.18, 0.03, 85.89::numeric),
    ('BEAC', 'Beacon Energy', 'ENERGY', 2.07985e-05, 0.0001632504, 1.119, 0.997, 0.025056, 2.44876e-05, 2027032.54, 1.4217, 179.28, 0.03, 66.32::numeric),
    ('CASC', 'Cascade Energy', 'ENERGY', 2.57879e-05, 0.0003506304, 1.002, 0.882, 0.01034, 5.25946e-05, 2271680.84, 1.348, 220.81, 0.03, 76.08::numeric),
    ('IRON', 'Ironclad Energy', 'ENERGY', 3.17931e-05, 0.0001913383, 0.939, 1.099, 0.025996, 2.87007e-05, 1414572.26, 1.345, 163.81, 0.03, 88.53::numeric),
    ('STER', 'Sterling Energy', 'ENERGY', 1.81264e-05, 0.0001856677, 1.26, 1.118, 0.028532, 2.78502e-05, 1435006.62, 1.2517, 156.06, 0.03, 37.86::numeric),
    ('REDW', 'Redwood Consumer', 'CONSUMER', 1.70916e-05, 0.000135158, 0.822, 0.95, 0.028498, 2.02737e-05, 4796653.61, 0.6389, 127.96, 0.03, 34.14::numeric),
    ('GRAN', 'Granite Consumer', 'CONSUMER', 2.45847e-05, 0.0001178654, 0.866, 1.094, 0.008534, 1.76798e-05, 4745332.93, 0.4938, 227.62, 0.03, 35.27::numeric),
    ('SILV', 'Silverline Consumer', 'CONSUMER', 3.69928e-05, 0.0001324963, 1.034, 0.956, 0.016563, 1.98745e-05, 4499887.92, 0.5949, 126.95, 0.03, 43.86::numeric),
    ('BLUE', 'Bluewave Consumer', 'CONSUMER', -1.05004e-05, 0.000124511, 0.774, 1.073, 0.01467, 1.86766e-05, 5576676.06, 0.5653, 173.17, 0.03, 34.6::numeric),
    ('CRES', 'Crestview Consumer', 'CONSUMER', -8.7517e-06, 0.0001502957, 0.843, 1.095, 0.013829, 2.25444e-05, 4929341.36, 0.4413, 225.9, 0.03, 27.08::numeric),
    ('FALC', 'Falcon Industrials', 'INDUSTRIAL', 2.48422e-05, 0.0001430966, 0.903, 0.878, 0.021236, 2.14645e-05, 4006836.05, 0.9445, 206.63, 0.03, 41.99::numeric),
    ('HARB', 'Harbor Industrials', 'INDUSTRIAL', 9.9429e-06, 0.0001408472, 1.139, 1.047, 0.00898, 2.11271e-05, 4996306.16, 0.9629, 117.67, 0.03, 74.25::numeric),
    ('TIMB', 'Timberline Industrials', 'INDUSTRIAL', 1.12559e-05, 0.0001612611, 1.065, 1.106, 0.008754, 2.41892e-05, 2772231.92, 0.7673, 151.56, 0.03, 41.19::numeric),
    ('CORA', 'Coral Industrials', 'INDUSTRIAL', -9.3168e-06, 0.000251711, 0.923, 1.016, 0.011875, 3.77567e-05, 4065234.58, 0.6631, 142.99, 0.03, 28.98::numeric),
    ('WIND', 'Windward Industrials', 'INDUSTRIAL', 2.23197e-05, 0.0003192919, 0.946, 1.013, 0.018297, 4.78938e-05, 3089294.9, 0.582, 215.36, 0.03, 63.04::numeric),
    ('ANCH', 'Anchor Utilities', 'UTILITIES', 1.66935e-05, 7.90154e-05, 0.675, 0.946, 0.022344, 1.18523e-05, 7067828.1, 0.435, 109.33, 0.03, 10.04::numeric),
    ('LUME', 'Lumen Utilities', 'UTILITIES', -1.59868e-05, 0.0002155018, 0.536, 0.894, 0.026353, 3.23253e-05, 7086560.51, 0.3925, 139.06, 0.03, 31.45::numeric),
    ('ZENI', 'Zenith Utilities', 'UTILITIES', 2.3348e-05, 6.37262e-05, 0.622, 0.884, 0.017007, 9.5589e-06, 6585068.0, 0.3958, 189.12, 0.03, 21.98::numeric),
    ('BRIG', 'Bright Utilities', 'UTILITIES', 1.4637e-05, 7.97368e-05, 0.627, 0.911, 0.010434, 1.19605e-05, 5498870.76, 0.417, 95.73, 0.03, 10.27::numeric),
    ('CLEA', 'Clearwater Utilities', 'UTILITIES', 1.14816e-05, 6.53835e-05, 0.619, 1.111, 0.01761, 9.8075e-06, 5901640.25, 0.3902, 152.89, 0.03, 19.18::numeric),
    ('RIDG', 'Ridgeline Materials', 'MATERIALS', 3.92912e-05, 0.0001409318, 1.024, 0.872, 0.028838, 2.11398e-05, 1923661.96, 0.8024, 122.07, 0.03, 89.79::numeric),
    ('STON', 'Stonebridge Materials', 'MATERIALS', 2.35894e-05, 0.0002999055, 1.039, 1.09, 0.011738, 4.49858e-05, 3432888.29, 1.1607, 119.36, 0.03, 48.73::numeric),
    ('FIEL', 'Fieldstone Materials', 'MATERIALS', 3.64299e-05, 0.0002571872, 0.929, 0.962, 0.02912, 3.85781e-05, 2795641.25, 0.9285, 218.46, 0.03, 41.86::numeric),
    ('COPP', 'Copperline Materials', 'MATERIALS', 1.17158e-05, 0.0003636107, 0.912, 0.9, 0.021182, 5.45416e-05, 3507894.14, 1.0575, 117.24, 0.03, 33.87::numeric),
    ('EVER', 'Evergreen Materials', 'MATERIALS', -1.32075e-05, 0.0001691888, 1.154, 0.897, 0.029877, 2.53783e-05, 2956686.55, 0.7847, 168.0, 0.03, 46.16::numeric)
     ) AS v(ticker, name, sector_key, drift, sigma, beta, gamma, kappa, fundamental_sigma,
             liquidity, lambda_impact, tau_ticks, max_impact, base_price)
JOIN sectors s ON s.key = v.sector_key;
