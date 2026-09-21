# StockBot

A global Discord synthetic stock market bot: one continuous market, one
shared currency, a factor-model price engine with decaying user price
impact, and a phased path from long-only trading to true margin shorts with
liquidation. See the design doc for the full plan.

Current status: **all phases complete**, including the liquidity plan
(phases A–E: dynamic spreads, crossing book, participation cap + concave
impact, stop orders + dividends, trading sessions with overnight gaps).
The tick engine is live (factor model, impact decay, mean reversion,
circuit breaker, deterministic replay) over 40 seeded instruments across 8
sectors plus the SBX-40 index. Working end to end: `/balance`,
`/portfolio`, `/buy`, `/sell`, `/market`, `/stock`, `/claim`, `/movers`,
`/sectors`, `/chart`, `/news`, `/calendar`, the shop (`/shop list`,
`/shop buy`), bounded shorts (`/short`, `/shorts`, `/cover`), true margin
(`margin_tier` shop item, `/margin`, `/collateral`, `/liquidations`),
seasons/league (`/league info|join|standings`), resting orders
(`/order buy|sell|list|cancel` — limit, stop, stop-limit),
admin tools (`/admin`), and the economy simulation harness
(`python -m stockbot.simulation.harness`).

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

`docker compose up` (no service name) also builds and runs the `bot` and
`market` services. `market` is the singleton tick engine (guarded by a
Postgres advisory lock); `bot` idles until `DISCORD_TOKEN` is set.

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
  bot/          # Discord gateway process (slash commands)
  market/       # singleton tick engine process (advisory-locked)
migrations/     # numbered .sql files, applied in order
tests/          # pytest, real Postgres required
```
