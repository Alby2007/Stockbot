-- Per-user chart viewing preferences. The chart UI is stateless (window
-- state rides in each button's custom_id), so "the way a user likes their
-- charts" lives here: every chart interaction upserts the resolved
-- (span, axis), and /chart falls back to the row when its timeframe/axis
-- args aren't passed. `span` is in open-tick units -- the same unit the
-- cid protocol and render_candle_chart use. `end` is deliberately NOT
-- persisted: panning is exploration, and a fresh /chart stays anchored
-- at the latest tick.
--
-- No FK to users: chart viewing isn't gated on having an account, so
-- prefs are keyed by the Discord snowflake directly.

CREATE TABLE chart_prefs (
    user_id BIGINT PRIMARY KEY,  -- Discord user snowflake
    span INT NOT NULL CHECK (span > 0),
    axis TEXT NOT NULL CHECK (axis IN ('time', 'ticks')),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
