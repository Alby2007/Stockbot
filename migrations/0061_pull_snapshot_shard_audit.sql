-- K-review followups: make every card_pulls row self-verifying, and
-- close the shard-flow audit gap.
--
-- pull_cfg snapshots every input resolve_pull consumed for THIS pull:
-- tier weights, pity threshold, the ordered pools, and the per-pull
-- premium floor. Replay then needs only (master_seed, row) -- config
-- retunes and card-pool expansions can no longer invalidate history.
-- NULL means the row predates snapshotting: such rows replay only
-- against a frozen config/pool, same as before this migration.
ALTER TABLE card_pulls ADD COLUMN pull_cfg JSONB;

-- shard_events is the complete shard journal: every mutation of
-- users.shards writes one signed row, so
--   users.shards = COALESCE(SUM(shard_events.delta), 0)
-- is a checkable invariant. Pull awards (DUPLICATE_BURN) link back to
-- the pull via (user_id, pull_seq); spends record the card and reason.
-- card_pulls has forever retention by design (it is the fairness
-- record); this is the matching material ledger.
CREATE TABLE shard_events (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(id),
    delta INT NOT NULL CHECK (delta <> 0),
    reason TEXT NOT NULL CHECK (reason IN
        ('DUPLICATE_BURN', 'CRAFT', 'FRAME_UPGRADE', 'LEGACY_ADJUST')),
    card_key TEXT,
    pull_seq INT,
    tick_index BIGINT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX shard_events_user ON shard_events(user_id);

-- Backfill: every historical award is reconstructable from card_pulls.
INSERT INTO shard_events (user_id, delta, reason, card_key, pull_seq,
                          tick_index, created_at)
SELECT p.user_id, p.shards_awarded, 'DUPLICATE_BURN', p.card_key,
       p.pull_seq, p.tick_index, p.created_at
FROM card_pulls p
WHERE p.shards_awarded > 0;

-- Historical burns were never recorded; one adjustment row per user
-- reconciles the journal to the actual balance so the invariant holds
-- exactly for rows predating this migration.
INSERT INTO shard_events (user_id, delta, reason)
SELECT u.id, u.shards - COALESCE(e.net, 0), 'LEGACY_ADJUST'
FROM users u
LEFT JOIN (
    SELECT user_id, SUM(delta) AS net FROM shard_events GROUP BY user_id
) e ON e.user_id = u.id
WHERE u.shards <> COALESCE(e.net, 0);
