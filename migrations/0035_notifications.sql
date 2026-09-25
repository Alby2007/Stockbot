-- Phase N1: transactional outbox for outbound DMs. A row is written
-- inside the *same* transaction as the event it describes (liquidation
-- leg, knockout, order fill, season result) -- if that transaction rolls
-- back, the notification never existed. The poller (bot/notify.py) is
-- the only reader/writer after that, on its own connection.
--
-- `payload` always carries `tick_index` -- the poller coalesces N rows
-- into one DM per (user_id, kind, tick_index) so a liquidation with 3
-- legs sends once, not three times.
--
-- Retry policy lives on the row, not in poller memory (the poller is
-- stateless across polls): `attempts` + `next_attempt_at` implement
-- exponential backoff, dead-lettering at `attempts >= 5`; a Forbidden
-- (DMs closed) jumps straight to dead-lettered rather than retrying.
CREATE TABLE notifications (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL,
    kind TEXT NOT NULL
        CHECK (kind IN ('LIQUIDATION', 'KNOCKOUT', 'ORDER_FILLED', 'SEASON_RESULT')),
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    sent_at TIMESTAMPTZ,
    attempts INT NOT NULL DEFAULT 0,
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_error TEXT
);

-- Matches the poller's WHERE clause exactly (partial index -- the table
-- is dominated by long-since-sent rows).
CREATE INDEX notifications_pending_idx ON notifications (next_attempt_at)
    WHERE sent_at IS NULL AND attempts < 5;

-- Opt-out toggle (/notify). Read at *poll* time, not write time: the
-- outbox row is written regardless, so opting back in surfaces the
-- backlog instead of silently dropping it.
ALTER TABLE users ADD COLUMN dm_notifications BOOLEAN NOT NULL DEFAULT TRUE;
