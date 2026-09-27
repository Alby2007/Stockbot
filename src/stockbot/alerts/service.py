"""One-shot price alerts: OPEN rows in `price_alerts` flip to TRIGGERED
inside `apply_tick`'s sweep when the tick-close mark crosses the target;
each fire enqueues an ALERT_TRIGGERED outbox row in the same transaction
(the poller coalesces them into one DM per user per tick).

Delist and suspension bulk-cancel open alerts beside the existing order
cancellations -- a suspended user's alerts firing forever would be the
same gap the orders audit caught.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

import psycopg
from psycopg import AsyncConnection
from psycopg.rows import dict_row

from stockbot.alerts.errors import (
    AlertError,
    AlertLimitError,
    DuplicateAlertError,
)
from stockbot.trading.errors import UnknownInstrumentError

VALID_DIRECTIONS = ("ABOVE", "BELOW")


async def _config_float(conn: AsyncConnection, key: str, default: float) -> float:
    async with conn.cursor() as cur:
        await cur.execute("SELECT value FROM config WHERE key = %s", (key,))
        row = await cur.fetchone()
    return float(row[0]) if row else default


async def create_alert(
    conn: AsyncConnection,
    *,
    user_id: int,
    ticker: str,
    direction: str,
    target: Decimal,
) -> int:
    """Store an OPEN alert; returns the new alert id. Rejects unknown or
    dormant instruments, per-user cap (`alerts.max_per_user`), and exact
    duplicates (unique index). An already-crossed target is allowed --
    it simply fires on the next tick, which is the expected behavior."""
    ticker = ticker.strip().upper()
    direction = direction.upper()
    if direction not in VALID_DIRECTIONS:
        raise AlertError("direction must be above or below")
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT id, is_active, quoted_price FROM instruments WHERE ticker = %s",
            (ticker,),
        )
        inst = await cur.fetchone()
    if inst is None:
        raise UnknownInstrumentError(ticker)
    if not inst["is_active"]:
        raise AlertError(f"{ticker} is not tradeable")

    max_alerts = int(await _config_float(conn, "alerts.max_per_user", 25))
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT COUNT(*) FROM price_alerts "
            "WHERE user_id = %s AND status = 'OPEN'",
            (user_id,),
        )
        count_row = await cur.fetchone()
    open_count = int(count_row[0]) if count_row else 0
    if open_count >= max_alerts:
        raise AlertLimitError(max_alerts)

    async with conn.cursor() as cur:
        await cur.execute("SELECT MAX(tick_index) FROM market_ticks")
        tick_row = await cur.fetchone()
    created_tick = int(tick_row[0]) if tick_row and tick_row[0] is not None else None

    # UniqueViolation on the partial index = exact dup. The savepoint keeps
    # the enclosing transaction usable (command handlers' bootstrap state).
    try:
        async with conn.transaction():
            async with conn.cursor() as cur:
                await cur.execute(
                    "INSERT INTO price_alerts "
                    "(user_id, instrument_id, direction, target_price, "
                    " created_tick) "
                    "VALUES (%s, %s, %s, %s, %s) RETURNING id",
                    (user_id, int(inst["id"]), direction, target, created_tick),
                )
                row = await cur.fetchone()
    except psycopg.errors.UniqueViolation:
        raise DuplicateAlertError(ticker, direction, target) from None
    assert row is not None
    return int(row[0])


async def cancel_alert(
    conn: AsyncConnection, *, user_id: int, alert_id: int
) -> bool:
    """False when the alert isn't OPEN or isn't the caller's -- the command
    layer never distinguishes (no existence leak)."""
    async with conn.transaction(), conn.cursor() as cur:
        await cur.execute(
            "UPDATE price_alerts SET status = 'CANCELLED' "
            "WHERE id = %s AND user_id = %s AND status = 'OPEN'",
            (alert_id, user_id),
        )
        return cur.rowcount == 1


async def list_alerts(
    conn: AsyncConnection, user_id: int, *, history: int = 5
) -> dict[str, list[dict[str, Any]]]:
    """Open alerts joined to the current mark, plus the most recent
    triggered/cancelled rows for history."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            SELECT a.id, i.ticker, a.direction, a.target_price,
                   i.quoted_price, a.created_tick
            FROM price_alerts a JOIN instruments i ON i.id = a.instrument_id
            WHERE a.user_id = %s AND a.status = 'OPEN'
            ORDER BY a.id
            """,
            (user_id,),
        )
        open_rows = await cur.fetchall()
        await cur.execute(
            """
            SELECT a.id, i.ticker, a.direction, a.target_price, a.status,
                   a.triggered_price, a.triggered_tick
            FROM price_alerts a JOIN instruments i ON i.id = a.instrument_id
            WHERE a.user_id = %s AND a.status <> 'OPEN'
            ORDER BY a.id DESC LIMIT %s
            """,
            (user_id, history),
        )
        closed_rows = await cur.fetchall()
    return {"open": open_rows, "closed": closed_rows}


async def sweep_alerts(conn: AsyncConnection, tick_index: int) -> int:
    """Flip every OPEN alert whose instrument's mark crossed its target
    this tick, enqueueing one ALERT_TRIGGERED outbox row per alert. Runs
    inside apply_tick's transaction after all mark-mutating steps, so
    alerts always see the true tick-close price."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            UPDATE price_alerts a
            SET status = 'TRIGGERED', triggered_tick = %s,
                triggered_price = i.quoted_price
            FROM instruments i
            WHERE i.id = a.instrument_id AND a.status = 'OPEN' AND i.is_active
              AND ((a.direction = 'ABOVE' AND i.quoted_price >= a.target_price)
                OR (a.direction = 'BELOW' AND i.quoted_price <= a.target_price))
            RETURNING a.user_id, i.ticker, a.direction, a.target_price,
                      i.quoted_price AS mark
            """,
            (tick_index,),
        )
        fired = await cur.fetchall()
        for row in fired:
            await cur.execute(
                "INSERT INTO notifications (user_id, kind, payload) "
                "VALUES (%s, 'ALERT_TRIGGERED', %s)",
                (
                    int(row["user_id"]),
                    json.dumps(
                        {
                            "tick_index": tick_index,
                            "ticker": row["ticker"],
                            "direction": row["direction"],
                            "target": float(row["target_price"]),
                            "mark": float(row["mark"]),
                        }
                    ),
                ),
            )
        return len(fired)


async def cancel_for_instrument(conn: AsyncConnection, instrument_id: int) -> int:
    """Bulk-cancel a delisted instrument's alerts (dead instruments can
    never cross anything)."""
    async with conn.transaction(), conn.cursor() as cur:
        await cur.execute(
            "UPDATE price_alerts SET status = 'CANCELLED' "
            "WHERE instrument_id = %s AND status = 'OPEN'",
            (instrument_id,),
        )
        return cur.rowcount


async def cancel_for_user(conn: AsyncConnection, user_id: int) -> int:
    """Bulk-cancel a suspended user's alerts -- same gap as resting
    orders: fills never consult bootstrap."""
    async with conn.transaction(), conn.cursor() as cur:
        await cur.execute(
            "UPDATE price_alerts SET status = 'CANCELLED' "
            "WHERE user_id = %s AND status = 'OPEN'",
            (user_id,),
        )
        return cur.rowcount
