-- Collectibles depth E: item-for-item trading. Offers are non-custodial
-- -- nothing is escrowed at offer time; accept() re-verifies both
-- binders under FOR UPDATE locks and auto-voids if a side changed.
-- A trade moves the whole user_cards row (frame + serial + copies) and
-- MERGES into a held row: best frame wins, lowest serial survives,
-- copies sum. Shards move via signed 'TRADE' shard_events, preserving
-- users.shards = SUM(shard_events.delta). No cash leg exists, by
-- design: nothing converts back to currency.

CREATE TABLE card_trades (
    id BIGSERIAL PRIMARY KEY,
    proposer BIGINT NOT NULL REFERENCES users (id),
    counterparty BIGINT NOT NULL REFERENCES users (id),
    give_cards TEXT[] NOT NULL DEFAULT '{}',
    want_cards TEXT[] NOT NULL DEFAULT '{}',
    give_shards INT NOT NULL DEFAULT 0 CHECK (give_shards >= 0),
    want_shards INT NOT NULL DEFAULT 0 CHECK (want_shards >= 0),
    status TEXT NOT NULL DEFAULT 'OPEN'
        CHECK (status IN ('OPEN', 'ACCEPTED', 'DECLINED', 'CANCELLED', 'EXPIRED')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_tick BIGINT NOT NULL,
    resolved_at TIMESTAMPTZ
);
CREATE INDEX card_trades_proposer_open
    ON card_trades (proposer) WHERE status = 'OPEN';
CREATE INDEX card_trades_counterparty_open
    ON card_trades (counterparty) WHERE status = 'OPEN';

ALTER TABLE notifications DROP CONSTRAINT notifications_kind_check;
ALTER TABLE notifications ADD CONSTRAINT notifications_kind_check
    CHECK (kind IN ('LIQUIDATION', 'KNOCKOUT', 'ORDER_FILLED', 'SEASON_RESULT',
                    'ACCOUNT_SUSPENDED', 'BADGE_EARNED', 'IPO_SETTLED',
                    'MARGIN_CALL', 'SHORT_RECALL', 'ALERT_TRIGGERED',
                    'QUEST_COMPLETED', 'OPTION_SETTLED', 'MOMENT_EARNED',
                    'TRADE_OFFER', 'TRADE_RESULT'));

ALTER TABLE shard_events DROP CONSTRAINT shard_events_reason_check;
ALTER TABLE shard_events ADD CONSTRAINT shard_events_reason_check
    CHECK (reason IN ('DUPLICATE_BURN', 'CRAFT', 'FRAME_UPGRADE',
                      'LEGACY_ADJUST', 'TRADE'));

INSERT INTO config (key, value) VALUES
    ('trade.max_open', 10),
    ('trade.ttl_ticks', 1440)
ON CONFLICT DO NOTHING;
