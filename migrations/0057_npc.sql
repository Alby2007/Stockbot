-- NPC traders P1: identity. `users.is_bot` flags synthetic accounts --
-- they hold ordinary USER accounts (every money path resolves
-- kind='USER'; a BOT account kind would make them invisible to the
-- ledger) and are excluded from human surfaces instead. `npc_agents`
-- carries the runner's per-agent state; `label` is an INTERNAL ops
-- label for /admin npc and telemetry, never rendered to players.

ALTER TABLE users ADD COLUMN IF NOT EXISTS is_bot BOOLEAN NOT NULL DEFAULT FALSE;

CREATE TABLE IF NOT EXISTS npc_agents (
    user_id BIGINT PRIMARY KEY REFERENCES users(id),
    archetype TEXT NOT NULL,
    label TEXT NOT NULL UNIQUE,     -- INTERNAL ops label only, never rendered
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    quote_ticker TEXT,              -- LP assignment, mirrors Agent.quote_ticker
    died_at_tick BIGINT,            -- permadeath marker; set once, never cleared
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS users_is_bot_idx ON users (id) WHERE is_bot;

INSERT INTO config (key, value) VALUES
    -- Master switch for the npc service (P2 runner reads it per round).
    -- Ships OFF: P1 lands the identity/exclusion layer with no agents.
    ('npc.enabled', 0),
    -- Aggregate per-tick NPC flow cap in DOLLARS: the participation cap
    -- bounds one fill and resting orders are keyed by order id, so a
    -- whole population acting legally can still become the market --
    -- this is the only aggregate bound (P1-C5).
    ('npc.max_tick_notional', 25000),
    -- Target share of trailing-day notional NPCs should represent; the
    -- runner multiplies action_prob by target/actual each round, clamped
    -- to [0.25x, 4x]. 0 disables the feedback (base rate only).
    ('npc.target_adv_share', 0.30),
    -- Per-agent probability of acting on any given tick (~1-5 actions/day
    -- at 1440-tick days for the default).
    ('npc.action_prob_per_tick', 0.002),
    -- Net-worth floor in minor units: an enabled agent below this with
    -- no open positions is marked dead (permadeath -- never cleared).
    ('npc.death_balance_minor', 100),
    -- Per-day FAUCET budget (minor) for auto-spawned agents. 0 = manual
    -- spawns only (/admin npc spawn) until real half-lives are measured.
    ('npc.spawn_budget_minor_per_day', 0),
    -- One-time grant per spawned agent (minor). No top-ups: permadeath.
    ('npc.stake_minor', 100000)
ON CONFLICT (key) DO NOTHING;
