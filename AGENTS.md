# Agent notes

Project plan: see the design doc (Phase 0 → Phase 2). Current status:
**Phase 0 complete** (ledger, accounts, migrations, process skeleton,
docker-compose). **Phase 1 complete**: tick engine, 40 seeded instruments,
trading (`/buy` `/sell`), `/balance` `/portfolio` `/market` `/stock` `/claim`
`/movers` `/sectors` `/chart` `/news` `/calendar`, shop, admin
(`/admin tune` `/admin ledger-audit` `/admin wash-trades` `/admin
season-create` `/admin season-close`), wash-trade detection, the economy
simulation harness (`python -m stockbot.simulation.harness`, use a scratch
DB), seasons/league (`/league info|join|standings`, `league` flag on
`/buy` `/sell` `/portfolio`), Phase 1.5 bounded shorts (`/short`
`/shorts` `/cover`, `stockbot.shorts`, knockout sweep inside `apply_tick`),
and Phase 2 true margin (`stockbot.margin`, signed positions, liquidation
engine, insurance fund, short interest + squeeze, SBX-40 index, `/margin`
`/collateral` `/liquidations`, `margin_tier` shop unlock) are all done.
Post-Phase-2: limit orders (`orders` table, `/order buy|sell|list|cancel`,
matched inside `apply_tick` before the margin sweep; fills run through
`execute_trade` in a savepoint so unfillable orders stay OPEN; league
orders are cancelled on season close) and a `shorter` archetype in the sim
harness are done.

Margin design notes: cash stays >= 0 (the USER/LEAGUE balance CHECK is
preserved -- short proceeds credit to cash and are spendable; leverage is
bounded by post-trade margin gates, not negative cash). Maintenance applies
to SHORT notional only, so `sweep_undermargined` only scans accounts with
`quantity < 0`. `check_and_liquidate` opens its own tx (locks the user's
position instruments id-ordered, then the account) because post-trade
liquidation can't take new instrument locks while holding the account lock
without inverting the ordering. The tick path calls `_liquidate_account`
inline (instruments already locked). Backstop chain on negative equity:
INSURANCE_FUND pays what it can -> MARKET_MAKER absorbs the residual;
every leg is recorded in `liquidations` / `insurance_fund_flows`
(reconciliation: fund balance == SUM(flows.amount_minor)). Borrow fees
accrue fractionally on `positions.borrow_fees_accrued` and settle to SINK
on cover/liquidation. Shorts consume portfolio slots (`quantity <> 0`).
League accounts get effective margin tier 1 (equal start). Bounded shorts
do NOT count toward short interest (they're collateralized, not borrowed).

Bounded-shorts design notes: `bounded_shorts` rows are separate from
long-only `positions` (defined-risk product: collateral = Q·entry·
knockout_pct posted to MARKET_MAKER upfront, payout = max(0, collateral +
Q·(entry − close)), KO at entry·(1+knockout_pct) → payout 0).
`sweep_knockouts` runs inside `apply_tick` after instrument updates.
`seasons` equity adds open-short value via `_SHORT_VALUE_SUBQUERY` — when
embedding it in a raw query string, the query must be an f-string (a plain
`"""` query shipped `{_SHORT_VALUE_SUBQUERY}` literally to Postgres once).

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
