# Operations

## Local development

```bash
python -m venv .venv
. .venv/Scripts/activate        # macOS/Linux: source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env            # fill DATABASE_URL / TEST_DATABASE_URL

docker compose up -d postgres   # Postgres 17 on 127.0.0.1:5450
python -m stockbot.migrate
```

`.env` keys:

| Key | Required | Purpose |
|-----|----------|---------|
| `DATABASE_URL` | yes | runtime Postgres DSN |
| `TEST_DATABASE_URL` | tests | scratch DB — **must be disposable**, the suite mutates it |
| `DISCORD_TOKEN` | bot only | gateway login; unset → `bot` service idles |
| `MASTER_SEED` | yes | seeded-RNG root — set before first boot, it is baked into all history |
| `ADMIN_USER_IDS` | no | comma-separated snowflakes allowed to run `/admin` |

## Services

```bash
docker compose up -d            # everything
docker compose up -d postgres   # db only
```

| Service | Role | Failure mode |
|---------|------|--------------|
| `postgres` | the database | everything halts; compose healthcheck gates the rest |
| `migrate` | applies `migrations/*.sql`, then exits | a failed migration stops boot of dependents |
| `market` | singleton tick engine, 60 s cadence | advisory-lock guarded; a second instance waits, never double-ticks |
| `npc` | singleton NPC trader runner | same lock pattern; gated per-poll by `npc.enabled` config |
| `bot` | Discord gateway + outbox pollers | idles without `DISCORD_TOKEN`; heartbeats keep beating |
| `backup` | daily `pg_dump -Fc` → named volume, 14-day retention | restore: `docker compose exec backup ls /backups`, gunzip \| psql |

## Deploy

Push to `master` → GitHub Actions runs the suite, packages the SHA in
the runner, scp's to the VM, rsyncs into `~/stockbot`, and rebuilds
(`postgres`/`bot`/`market`/`npc`/`backup`). Required
secrets: `STOCKBOT_VM_HOST`, `STOCKBOT_VM_SSH_KEY`. `~/stockbot/.env` on
the VM survives deploys (excluded from rsync) and must carry
`DISCORD_TOKEN`, `MASTER_SEED`, `ADMIN_USER_IDS`, `POSTGRES_PASSWORD` —
`POSTGRES_PASSWORD` only applies on an empty data dir, so set it before
the first boot.

Postgres binds `127.0.0.1:5450` only — never publicly reachable.

## Tests

```bash
pytest                  # whole suite against TEST_DATABASE_URL
ruff check .
mypy src
```

The suite needs a real Postgres (row locking, advisory locks, ON
CONFLICT, savepoint semantics are the features under test). Migrations
apply automatically at session start (`tests/conftest.py`). Each test
runs inside a rolled-back outer transaction — service
`conn.transaction()` blocks nest as savepoints — except
`test_concurrency.py`, which deliberately uses committed connections to
catch cross-connection bugs.

## Tooling

```bash
python -m stockbot.tools.doctor \
    [--database-url postgresql://...]
# read-only health checks: pending migrations, market lock held,
# tick staleness, ledger invariants, config sanity, heartbeat age.
# exit 0 = clean, 1 = a finding.

python -m stockbot.tools.replay --from N --to M --master-seed S
# re-derives stored factor draws and flags mismatches (see
# docs/determinism.md).

python -m stockbot.simulation.harness --database-url postgresql://...
# drives a synthetic economy on a scratch DB (agents, orders, events)
# -- never point it at the real one.

python -m stockbot.npc.soak --database-url postgresql://... --ticks 2000
# NPC cohort soak test on a scratch DB.
```

## Watching the system

- `doctor` covers the ops checklist after deploys.
- `market_ticks` carries a heartbeat; the `bot` process logs heartbeat,
  notify/feed poll, and leaderboard loops.
- Admin surface: `/admin health`, `/admin ledger-audit`,
  `/admin recalc-balances`, `/admin wash-trades`, `/admin tune`,
  `/admin user-info`, `/admin disable|enable`, `/admin npc-*`.
