# StockBot

A Discord synthetic stock-market bot: continuous trading across two
regional venues (US + Asia), one shared currency, a deterministic
factor-model price engine with decaying user impact, and a Postgres
ledger the whole system reconciles against.

The market runs on a 60-second singleton tick engine (advisory-locked):
session-aware venue clocks with overnight gaps and closing auctions, a
crossing limit/stop order book, events (dividends, earnings), margin +
bounded shorts with liquidation/ADL/insurance-fund cascades, and
deterministic seeded RNG that replays tick-for-tick
(`tools/replay.py`).

## Features

- **Trading** — `/buy` `/sell` market fills; `/order` resting
  LIMIT/STOP/STOP_LIMIT + bracket pairs, cross-book matching
- **Leverage** — bounded shorts (`/short` `/cover`), true margin
  (`/margin` `/collateral` `/liquidations`), cash-settled options
  (`/options chain|buy|positions|sell`)
- **Markets** — `/market` `/stock` `/movers` `/sectors` `/chart`
  (interactive, stateless buttons) `/news` `/calendar` `/alert`
- **Events & leagues** — IPOs (`/ipo`), seasons with isolated league
  accounts (`/league`), the public drama feed (`/feed-setup`)
- **Progression** — `/claim` + wheel, `/quests`, badges, `/title`
  `/theme`, `/profile`, `/leaderboard`, `/sandbox`
- **Collectibles** — provably-fair card packs (`/open`), binder
  (`/collection` `/card`), shard crafting (`/craft`), `/feature`
- **Shop** — interactive `/shop` browser with a deterministic daily deal;
  consumables, margin tiers, cosmetics, `/gift`, `/purchases`,
  `/commission` custom listings
- **NPC traders** — six archetypes on bounded stakes with permadeath,
  invisible to human surfaces
- **Admin** — `/admin` suite: tune, ledger audit, health, wash trades,
  seasons, listings/IPOs, user suspension, NPC control

Full command map: [docs/features.md](docs/features.md).
System docs live in [docs/](docs/README.md) — architecture diagrams,
schema map, determinism/replay, operations.

## Stack

Python 3.13, discord.py, PostgreSQL 17 via `psycopg[binary,pool]`, raw SQL
migrations, pytest against a real Postgres.

## Local setup

```bash
python -m venv .venv
. .venv/Scripts/activate   # or `source .venv/bin/activate` on macOS/Linux
pip install -e ".[dev]"
cp .env.example .env       # then fill in DATABASE_URL / TEST_DATABASE_URL
```

Start Postgres (and the bot/market/npc services) with Docker:

```bash
docker compose up -d postgres
python -m stockbot.migrate
```

`docker compose up` (no service name) also builds and runs `bot`,
`market`, `npc`, and `backup`. `market` and `npc` are advisory-locked
singletons; `bot` idles until `DISCORD_TOKEN` is set; `backup` writes a
daily `pg_dump` to a named volume.

## Tests

Tests run against a real Postgres database (SQLite can't express the
row-locking/concurrency behavior this project depends on). Point
`TEST_DATABASE_URL` at a throwaway database — a second local Postgres
database, or a scratch Supabase project both work:

```bash
# using the docker-compose postgres:
docker exec -it stockbot-postgres-1 psql -U stockbot -c 'CREATE DATABASE stockbot_test;'

pytest
```

Migrations are applied to `TEST_DATABASE_URL` automatically at the start
of the test session (see `tests/conftest.py`).

## Deploy

Pushing to `master` runs the test suite, then packages the checked-out
SHA in the runner, scp's it to the VM, rsyncs it into `~/stockbot`
(preserving `.env`), and rebuilds `postgres`/`bot`/`market`/`npc`/
`backup` (`.github/workflows/deploy.yml`). The repo is private — the
tarball is packaged inside the Actions runner, not fetched by the VM.

Required GitHub secrets: `STOCKBOT_VM_HOST`, `STOCKBOT_VM_SSH_KEY` (SSH
private key for `opc@<host>`).

Required `~/stockbot/.env` on the VM (survives deploys — excluded from
the rsync): `DISCORD_TOKEN`, `MASTER_SEED`, `ADMIN_USER_IDS`,
`POSTGRES_PASSWORD`. Set `MASTER_SEED` and `POSTGRES_PASSWORD` before
the *first* boot — Postgres only applies the password on an empty data
dir, and the seed is baked into all price and pack-pull history.

Postgres is bound to `127.0.0.1:5450` only — unreachable from outside
the host.

## Lint / type-check

```bash
ruff check .
mypy src
```

## Layout

```
src/stockbot/
  config.py     # env-based settings
  db.py         # shared async connection pool
  migrate.py    # tiny migration runner (python -m stockbot.migrate)
  ledger/       # append-only double-entry ledger
  accounts/     # user + account bootstrap
  bot/          # Discord gateway process (commands, views, pollers)
  market/       # singleton tick engine (advisory-locked)
  npc/          # singleton NPC trader runner + soak harness
  trading/ orders/ margin/ shorts/ options/ seasons/ shop/ ipo/
  alerts/ quests/ status/ admin/ compliance/ claims/ feed/
  collectibles/ # domain services called by bot + tick engine
  tools/        # doctor, replay
  simulation/   # economy harness (scratch DB only)
migrations/     # numbered .sql files, applied in filename order
docs/           # system docs (architecture, schema, replay, ops)
tests/          # pytest, real Postgres required
```
