-- Plan D: the information game -- news fizzle + earnings consensus.
--
-- events.estimate is the public "street consensus" log-return for an
-- EARNINGS event, drawn at scheduling and shown on /calendar. At
-- resolution the realized magnitude is estimate + surprise (the existing
-- resolution-time N(0, EARNINGS_SHOCK_SIGMA) draw becomes the surprise
-- term -- no new draws on the resolution path).
ALTER TABLE events
    ADD COLUMN estimate NUMERIC(10, 6),
    -- fizzled: set at creation when the rumor is doomed -- only revealed
    -- on /news AFTER resolution (pre-resolution reads never select it).
    ADD COLUMN fizzled BOOLEAN NOT NULL DEFAULT FALSE;

INSERT INTO config (key, value) VALUES
    -- news.fizzle_pct: fraction of news events whose magnitude is shrunk
    -- toward zero at creation -- the headline still posts, resolution
    -- still happens, the rumor just dies.
    ('news.fizzle_pct', 0.25),
    -- earnings.est_sigma: dispersion of the public estimate around 0.
    ('earnings.est_sigma', 0.03)
ON CONFLICT (key) DO NOTHING;
