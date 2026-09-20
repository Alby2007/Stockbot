# StockBot

A global Discord synthetic stock market bot: one continuous market, one
shared currency, a factor-model price engine with decaying user price
impact, and a phased path from long-only trading to true margin shorts with
liquidation. See the design doc for the full plan.

Current status: **Phase 0 — foundation** (double-entry ledger, account
bootstrap, migrations, process skeleton). No trading or Discord commands
yet.

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

Start Postgres (and the bot/market services) with Docker:

```bash
docker compose up -d postgres
python -m stockbot.migrate
```

`docker compose up` (no service name) will also build and run the `bot` and
`market` services, which are Phase 0 placeholders (no Discord commands, no
tick loop yet).

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

Migrations are applied to `TEST_DATABASE_URL` automatically at the start of
the test session (see `tests/conftest.py`).

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
  bot/          # Discord gateway process (Phase 1+)
  market/       # tick engine process (Phase 1+)
migrations/     # numbered .sql files, applied in order
tests/          # pytest, real Postgres required
```
