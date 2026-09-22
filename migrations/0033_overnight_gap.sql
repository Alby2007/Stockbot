-- Overnight-gap variance fix. The reopen step applied dt=closed_ticks to
-- EVERY term, so gap sigma was sqrt(480)~=22x per-tick vol and ~85% of
-- instruments pinned the 4.5% breaker cap at every reopen -- every
-- session restarted into a mass halt. Empirically overnight variance is
-- closer to an hour of trading than a full session, so the stochastic
-- terms (factors, idiosyncratic, fundamental shock, momentum innovation
-- and the carried momentum drift) now scale by sqrt(closed_ticks*frac)
-- while clock-time terms (base drift, mean reversion, impact decay) keep
-- the full elapsed dt. 1.0 restores the old behaviour; 0.125 on
-- closed_ticks=480 gives a 60-tick variance horizon.
INSERT INTO config (key, value) VALUES
    ('session.overnight_var_frac', 0.125)
ON CONFLICT (key) DO NOTHING;
