-- Phase G: tick-size rounding + U-shaped intraday spread.
--
-- Tick grid: engine.tick_size(price) returns max(tick_min, nice-1/2/5
-- multiple >= price * tick_pct). Fill prices round to the nearest grid
-- point; displayed bid/ask round outward so the quote is the worst case.
INSERT INTO config (key, value) VALUES
    -- tick size as a fraction of price (10bp -> $0.10 ticks on a $100 name)
    ('spread.tick_pct', 0.001),
    -- absolute floor on the tick size
    ('spread.tick_min', 0.01),
    -- U-shape: spreads are widest right after the session opens (the
    -- overnight gap just landed -- maximum uncertainty), decay over
    -- open_decay_ticks, then widen again into the close.
    ('spread.open_coeff', 1.0),
    ('spread.open_decay_ticks', 90),
    ('spread.close_coeff', 0.5),
    ('spread.close_decay_ticks', 60)
ON CONFLICT (key) DO NOTHING;
