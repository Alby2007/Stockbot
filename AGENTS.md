# Agent notes

Project plan: see the design doc (Phase 0 → Phase 2). Current status: **Phase
0 complete** (ledger, accounts, migrations, process skeleton, docker-compose).

## Environment

- Requires Python 3.13, not 3.14 (discord.py 2.7.1 has open issues on 3.14).
  On a machine where the default `python` is 3.14, create the venv with
  `py -3.13 -m venv .venv` instead.
- Windows only: psycopg's async mode refuses to run under the default
  `ProactorEventLoop`. `tests/conftest.py` and both service `main()` entrypoints
  already switch to `WindowsSelectorEventLoopPolicy` on `sys.platform ==
  "win32"`. This is a no-op on Linux (where the Docker image runs), so it's
  safe to leave in.
- This dev machine already has native Postgres services bound to ports 5432
  and 5433 (`postgresql-x64-13`, `postgresql-x64-18` Windows services). The
  docker-compose Postgres is mapped to host port **5450** instead. If you hit
  `password authentication failed` or `role ... does not exist` against a
  `localhost` connection, suspect a port clash first — check
  `netstat -ano | grep <port>` on Windows.

## Commands

```bash
# one-time
py -3.13 -m venv .venv
.venv/Scripts/pip install -e ".[dev]"
cp .env.example .env

# local Postgres
docker compose up -d postgres
docker exec stockbot-postgres-1 psql -U stockbot -d stockbot -c "CREATE DATABASE stockbot_test;"
.venv/Scripts/python -m stockbot.migrate

# verification (run all three after any change)
.venv/Scripts/python -m pytest
.venv/Scripts/python -m ruff check .
.venv/Scripts/python -m mypy src

# full stack smoke test
docker compose up -d --build
docker logs stockbot-migrate-1   # should say "No pending migrations." on repeat runs
docker logs stockbot-bot-1       # idles without DISCORD_TOKEN (expected pre-Phase-1)
docker logs stockbot-market-1    # "acquired singleton advisory lock..."
```

Tests need a real Postgres (`TEST_DATABASE_URL`) — no SQLite fallback, by
design (row-locking/concurrency behavior can't be expressed there).
