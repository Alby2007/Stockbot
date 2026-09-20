# Agent notes

Project plan: see the design doc (Phase 0 → Phase 2). Current status:
**Phase 0 complete** (ledger, accounts, migrations, process skeleton,
docker-compose). **Phase 1 complete**: tick engine, 40 seeded instruments,
trading (`/buy` `/sell`), `/balance` `/portfolio` `/market` `/stock` `/claim`
`/movers` `/sectors` `/chart` `/news` `/calendar`, shop, admin
(`/admin tune` `/admin ledger-audit` `/admin wash-trades` `/admin
season-create` `/admin season-close`), wash-trade detection, the economy
simulation harness (`python -m stockbot.simulation.harness`, use a scratch
DB), and seasons/league (`/league info|join|standings`, `league` flag on
`/buy` `/sell` `/portfolio`) are all done. Phase 2 (margin, shorts,
liquidation) not started.

Seasons design notes: LEAGUE accounts are `accounts` rows with
`season_id` set; positions/trades carry `season_id` (NULL = main portfolio,
via `NULLS NOT DISTINCT` unique key). `seasons.on_tick` runs inside
`apply_tick`'s transaction: activation, day-boundary equity snapshots,
close (score/rank/prizes/sweep-to-SINK). Note: `ALTER TYPE ... ADD VALUE`
can't be referenced in its own transaction -- that's why the enum lives in
0008 and everything that touches 'LEAGUE' is in 0009.

## Gotchas already hit once -- don't re-debug these

- **Every long-lived connection must commit before looping.** Any code that
  runs a bare `cur.execute(...)` on a connection (not wrapped in
  `conn.transaction()`) leaves that connection sitting in an open,
  uncommitted implicit transaction (autocommit=False is the pool default).
  If you then loop and call service functions that each open their own
  `async with conn.transaction()`, those nest as *savepoints* of that
  never-committed outer transaction instead of real commits -- writes look
  like they succeed (no exception, logs look fine) but never reach disk and
  hold row locks forever, silently wedging every other connection that
  touches the same rows. This bit `market/main.py`'s tick loop: it checks
  `pg_try_advisory_lock` with a bare `execute()` before starting the loop.
  Fix is one explicit `await conn.commit()` right after acquiring the lock
  (`pg_try_advisory_lock`, not `pg_try_advisory_xact_lock`, is session-scoped
  so this doesn't release it). If you add another long-running loop that
  reuses one connection, do the same audit: every bare statement before the
  first `conn.transaction()` block needs an explicit commit (or just don't
  run bare statements on a connection you intend to reuse across a loop).
- Short-lived per-command connections (`async with db.connection() as conn:`
  then one or more `service_function(conn, ...)` calls, as in
  `bot/commands.py`) don't have this problem: the pool rolls back any
  leftover open transaction when the connection is released, and each
  top-level `conn.transaction()` call still commits for real as long as nothing
  upstream nested it.
- The simulation harness hit the same bug differently: `run_simulation` calls
  `bootstrap_user`/`net_worth_by_user` (bare SELECTs) before the tick loop, so
  the whole run silently became one never-committed transaction full of
  savepoints -- progressively slower, holding `instruments` row locks,
  invisible to other connections. Fix: `simulation/harness.py::_main_async`
  sets `conn.set_autocommit(True)`, which also mirrors production (each
  service call = one real transaction). Tests keep using rollback-wrapped
  conns; do NOT add `conn.commit()` inside `run_simulation` or it would break
  that isolation.
- `executemany()` is not a batch: psycopg3 still waits per row (~186 socket
  waits per 40-instrument tick measured, ~96ms/tick). `market/tick.py` uses a
  single `UPDATE ... FROM (VALUES ...)` and one multi-row `INSERT` instead --
  ~8ms/tick. Note VALUES columns with mixed None/int rows infer as `text`;
  cast explicitly (`v.col::bigint`). For real bulk loads, consider COPY.
- `rng.choice(rows)` is only deterministic if the SQL has `ORDER BY` --
  Postgres row order shifts as UPDATEs rearrange tuples, which made the
  wash-trader sim test flaky until `_random_active_ticker` got `ORDER BY
  ticker`.

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
