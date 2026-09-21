"""stockbot doctor: one-command deployment health check.

Runs the read-only checks you'd otherwise do by hand after a deploy or
when something smells: pending migrations, market singleton lock, tick
staleness, ledger invariants, config sanity, index presence, heartbeat
freshness. Deliberately synchronous and dependency-light so it still
works when the async stack is what's broken.

Usage:
    python -m stockbot.tools.doctor                  # uses DATABASE_URL
    python -m stockbot.tools.doctor --database-url postgresql://...

Exit code 0 = all checks pass, 1 = at least one finding.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import psycopg

from stockbot.admin.service import CONFIG_BOUNDS
from stockbot.config import get_settings

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent.parent.parent / "migrations"

# Indexes the hot paths actually depend on -- a missing one is a
# silent performance regression, not a crash, so it needs a check.
EXPECTED_INDEXES = (
    "candles_instrument_tick_idx",
    "ledger_entries_account_id_idx",
    "trades_instrument_tick_idx",
    "orders_open_idx",
    "orders_open_stops_idx",
)


@dataclass(frozen=True)
class Finding:
    check: str
    ok: bool
    detail: str


def _check_pending_migrations(cur: psycopg.Cursor) -> Finding:
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            name TEXT PRIMARY KEY,
            applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    cur.execute("SELECT name FROM schema_migrations")
    applied = {row[0] for row in cur.fetchall()}
    files = {p.name for p in MIGRATIONS_DIR.glob("*.sql")}
    pending = sorted(files - applied)
    recorded_missing = sorted(applied - files)
    detail_parts = []
    if pending:
        detail_parts.append(f"pending: {pending}")
    if recorded_missing:
        detail_parts.append(f"recorded but no file: {recorded_missing}")
    return Finding(
        "migrations",
        not detail_parts,
        "; ".join(detail_parts) or f"all {len(files)} applied",
    )


def _check_advisory_lock(cur: psycopg.Cursor) -> Finding:
    cur.execute(
        "SELECT COUNT(*) FROM pg_locks WHERE locktype = 'advisory' AND granted"
    )
    row = cur.fetchone()
    holders = int(row[0]) if row else 0
    return Finding(
        "market_lock",
        True,  # informational: zero holders just means the market is down
        f"{holders} advisory lock(s) held (0 = market service not running)",
    )


def _check_tick_staleness(cur: psycopg.Cursor) -> Finding:
    cur.execute(
        "SELECT tick_index, ts, session_state FROM market_ticks "
        "ORDER BY tick_index DESC LIMIT 1"
    )
    row = cur.fetchone()
    if row is None:
        return Finding("tick_staleness", False, "no ticks recorded")
    tick_index, ts, phase = row
    age = (datetime.now(UTC) - ts).total_seconds()
    ok = age < 180  # ~3 tick intervals of slack
    return Finding(
        "tick_staleness",
        ok,
        f"last tick {tick_index} ({phase}) {age:.0f}s ago",
    )


def _check_ledger(cur: psycopg.Cursor) -> Finding:
    cur.execute("SELECT COALESCE(SUM(amount), 0) FROM ledger_entries")
    row = cur.fetchone()
    ledger_sum = int(row[0]) if row else 0
    cur.execute(
        "SELECT COUNT(*) FROM accounts WHERE kind IN ('USER','LEAGUE') AND balance < 0"
    )
    row = cur.fetchone()
    negative = int(row[0]) if row else 0
    cur.execute(
        """
        SELECT COUNT(*) FROM (
            SELECT a.id
            FROM accounts a
            LEFT JOIN ledger_entries le ON le.account_id = a.id
            GROUP BY a.id
            HAVING a.balance <> COALESCE(SUM(le.amount), 0)
        ) mismatched
        """
    )
    row = cur.fetchone()
    drifted = int(row[0]) if row else 0
    ok = ledger_sum == 0 and negative == 0 and drifted == 0
    return Finding(
        "ledger",
        ok,
        f"sum={ledger_sum} negative={negative} drifted={drifted}",
    )


def _check_config_sanity(cur: psycopg.Cursor) -> Finding:
    cur.execute("SELECT key, value FROM config")
    raw_rows = cur.fetchall()
    rows: dict[str, float] = {}
    problems: list[str] = []
    for key, value in raw_rows:
        try:
            rows[key] = float(value)
        except (TypeError, ValueError):
            problems.append(f"{key}: non-numeric value {value!r}")
    for key, (lo, hi) in CONFIG_BOUNDS.items():
        if key not in rows and not any(p.startswith(f"{key}:") for p in problems):
            problems.append(f"{key}: missing row")
        elif key in rows and not lo <= rows[key] <= hi:
            problems.append(f"{key}: {rows[key]} outside [{lo}, {hi}]")
    unknown = sorted(set(rows) - set(CONFIG_BOUNDS))
    for key in unknown:
        problems.append(f"{key}: no bounds registered")
    return Finding(
        "config_sanity",
        not problems,
        "; ".join(problems) or f"{len(rows)} keys in bounds",
    )


def _check_indexes(cur: psycopg.Cursor) -> Finding:
    cur.execute("SELECT indexname FROM pg_indexes WHERE schemaname = 'public'")
    present = {row[0] for row in cur.fetchall()}
    missing = [name for name in EXPECTED_INDEXES if name not in present]
    return Finding(
        "indexes",
        not missing,
        f"missing: {missing}" if missing else f"all {len(EXPECTED_INDEXES)} present",
    )


def _check_heartbeats(cur: psycopg.Cursor) -> Finding:
    try:
        cur.execute("SELECT service, beat_at FROM service_heartbeats")
    except psycopg.errors.UndefinedTable:
        return Finding("heartbeats", False, "service_heartbeats table missing")
    rows = cur.fetchall()
    if not rows:
        return Finding("heartbeats", False, "no heartbeats recorded")
    now = datetime.now(UTC)
    stale = [
        f"{service} {int((now - beat_at).total_seconds())}s"
        for service, beat_at in rows
        if (now - beat_at).total_seconds() > 120
    ]
    return Finding(
        "heartbeats",
        not stale,
        f"stale: {stale}" if stale else f"{len(rows)} service(s) fresh",
    )


def run_checks(database_url: str) -> list[Finding]:
    findings: list[Finding] = []
    with psycopg.connect(database_url) as conn:
        with conn.cursor() as cur:
            for check in (
                _check_pending_migrations,
                _check_advisory_lock,
                _check_tick_staleness,
                _check_ledger,
                _check_config_sanity,
                _check_indexes,
                _check_heartbeats,
            ):
                conn.rollback()  # each check sees a clean snapshot
                try:
                    findings.append(check(cur))
                except Exception as exc:
                    findings.append(
                        Finding(check.__name__, False, f"check itself failed: {exc}")
                    )
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", default=None)
    args = parser.parse_args(argv)
    database_url = args.database_url or get_settings().database_url

    findings = run_checks(database_url)
    width = max(len(f.check) for f in findings)
    for f in findings:
        mark = "ok  " if f.ok else "FAIL"
        print(f"[{mark}] {f.check:<{width}}  {f.detail}")
    failed = sum(not f.ok for f in findings)
    print(f"\n{len(findings) - failed}/{len(findings)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
