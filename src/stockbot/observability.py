"""Continuous observability: periodic invariant audits and service
heartbeats, both persisted to Postgres so "is it healthy right now" is a
query rather than a guess.

The tick loop calls `run_periodic_audit` every `audit.every_n_ticks`
(cheap enough at one row per check per run) and `write_heartbeat` every
tick; the bot heartbeats on an interval from `bot/main.py`. A heartbeat
older than ~2 intervals means "process alive but wedged" -- which
`restart: unless-stopped` alone can never see.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from psycopg import AsyncConnection

log = logging.getLogger("stockbot.observability")


async def write_heartbeat(
    conn: AsyncConnection, service: str, detail: dict[str, Any] | None = None
) -> None:
    """Upsert one row per service. `detail` is free-form JSONB (last tick,
    gateway latency, consecutive failures -- whatever helps triage)."""
    async with conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO service_heartbeats (service, beat_at, detail)
            VALUES (%s, now(), %s)
            ON CONFLICT (service)
            DO UPDATE SET beat_at = now(), detail = EXCLUDED.detail
            """,
            (service, json.dumps(detail or {})),
        )


async def run_periodic_audit(conn: AsyncConnection, tick_index: int) -> bool:
    """Run the cheap invariants and persist one audit_results row each.

    Returns False if any check failed (also log.critical). Invariants you
    don't check continuously are the ones that decay -- this is the
    always-on version of `/admin ledger-audit`, run inside the tick loop
    on its own transaction so an audit hiccup can't poison a tick.
    """
    checks: list[tuple[str, bool, str]] = []

    async with conn.cursor() as cur:
        await cur.execute("SELECT COALESCE(SUM(amount), 0) FROM ledger_entries")
        row = await cur.fetchone()
        ledger_sum = int(row[0]) if row else 0
        checks.append(("ledger_sum_zero", ledger_sum == 0, f"sum={ledger_sum}"))

        await cur.execute(
            "SELECT COUNT(*) FROM accounts WHERE kind IN ('USER','LEAGUE') "
            "AND balance < 0"
        )
        row = await cur.fetchone()
        negative = int(row[0]) if row else 0
        checks.append(("no_negative_balances", negative == 0, f"count={negative}"))

        # accounts.balance is a materialized cache over ledger_entries;
        # any divergence means a write bypassed the ledger.
        await cur.execute(
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
        row = await cur.fetchone()
        drift = int(row[0]) if row else 0
        checks.append(("balance_matches_ledger", drift == 0, f"mismatched={drift}"))

        # Insurance fund: every movement is a recorded flow.
        await cur.execute(
            """
            SELECT
                (SELECT balance FROM accounts
                 WHERE kind = 'SYSTEM' AND system_name = 'INSURANCE_FUND'),
                (SELECT COALESCE(SUM(amount_minor), 0) FROM insurance_fund_flows)
            """
        )
        row = await cur.fetchone()
        fund_bal, flows = (int(row[0]), int(row[1])) if row else (0, 0)
        checks.append(
            ("fund_reconciles", fund_bal == flows, f"balance={fund_bal} flows={flows}")
        )

    all_ok = True
    async with conn.cursor() as cur:
        for name, ok, detail in checks:
            all_ok = all_ok and ok
            if not ok:
                log.critical("invariant %s failed at tick %d: %s", name, tick_index, detail)
            await cur.execute(
                """
                INSERT INTO audit_results (tick_index, check_name, ok, detail)
                VALUES (%s, %s, %s, %s)
                """,
                (tick_index, name, ok, detail),
            )
    return all_ok


async def audit_every_n_ticks(conn: AsyncConnection) -> int:
    async with conn.cursor() as cur:
        await cur.execute("SELECT value FROM config WHERE key = 'audit.every_n_ticks'")
        row = await cur.fetchone()
    return int(row[0]) if row else 60
