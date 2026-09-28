-- Public tape: a per-guild broadcast feed for market drama (liquidations,
-- knockouts, whale fills, jackpots, IPOs, season results, landed news).
-- `feed_channels` binds one channel per guild; `feed_items` is a fan-out
-- outbox -- one row per (bound channel, event) written inside the
-- emitting event's own transaction, mirroring the notifications outbox.
-- Cosmetic only: nothing here feeds back into prices or fundamentals.

CREATE TABLE IF NOT EXISTS feed_channels (
    guild_id BIGINT PRIMARY KEY,
    channel_id BIGINT NOT NULL UNIQUE,  -- one feed per channel; rebinds update this
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS feed_items (
    id BIGSERIAL PRIMARY KEY,
    channel_id BIGINT NOT NULL REFERENCES feed_channels(channel_id)
        ON UPDATE CASCADE ON DELETE CASCADE,  -- rebinds carry pending rows; unbinds drop them
    kind TEXT NOT NULL,
    user_id BIGINT,                       -- subject for grouping/mention; NULL for market news
    tick_index BIGINT,                    -- coalescing key, NULL for between-tick events
    payload JSONB NOT NULL,
    sent_at TIMESTAMPTZ,                  -- NULL = pending
    attempts INT NOT NULL DEFAULT 0,
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS feed_items_pending ON feed_items (channel_id, id)
    WHERE sent_at IS NULL;

INSERT INTO config (key, value) VALUES
    -- Emit-side kill switch: checked at emit_feed so a disabled feed
    -- leaves no backlog that would flood channels on re-enable.
    ('feed.enabled', 1),
    -- Whale threshold in DOLLARS (display-tuned): a fill at or above this
    -- notional posts to the tape. Tuned to the live economy; crosses are
    -- deliberately exempt (P2P whale prints are the collusion vector).
    ('news.whale_min_notional', 5000),
    -- Option-settlement payout threshold in DOLLARS -- routine expiries
    -- would spam the tape, so only real payouts post.
    ('news.option_payout_min', 500)
ON CONFLICT (key) DO NOTHING;
