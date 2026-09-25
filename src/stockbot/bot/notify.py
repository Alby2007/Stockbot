"""Transactional outbox delivery: poll `notifications`, coalesce rows into
one message per (user, kind, tick), and hand each off to an injectable
`deliver` callable -- tests substitute a stub, no Discord needed.

Insert sites (all inside the event's own transaction):
    margin/service.py     _liquidate_leg   -> LIQUIDATION
    shorts/service.py     sweep_knockouts  -> KNOCKOUT
    orders/service.py     fill sites       -> ORDER_FILLED
    seasons/service.py    close_season     -> SEASON_RESULT
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from itertools import groupby
from typing import Any

from psycopg import AsyncConnection
from psycopg.rows import dict_row

from stockbot.bot.format import format_money, format_price

log = logging.getLogger("stockbot.bot.notify")

MAX_ATTEMPTS = 5
POLL_BATCH = 100
# Exponential backoff in minutes, capped; index by (attempts - 1).
_BACKOFF_MINUTES = (1, 5, 15, 60, 240)


class DeliveryForbidden(Exception):
    """Raised by `deliver` when the recipient's DMs are closed -- the
    caller dead-letters every pending row for that user instead of
    retrying (which would just repeat the same failure forever)."""


Deliver = Callable[[int, str], Awaitable[None]]


async def _fetch_batch(conn: AsyncConnection) -> list[dict[str, Any]]:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            SELECT n.id, n.user_id, n.kind, n.payload
            FROM notifications n
            JOIN users u ON u.id = n.user_id
            WHERE n.sent_at IS NULL AND n.attempts < %s
              AND n.next_attempt_at <= now()
              AND u.dm_notifications
            ORDER BY n.user_id, n.kind, n.id
            LIMIT %s
            FOR UPDATE OF n SKIP LOCKED
            """,
            (MAX_ATTEMPTS, POLL_BATCH),
        )
        return await cur.fetchall()


def _group_key(row: dict[str, Any]) -> tuple[int, str, Any]:
    return (int(row["user_id"]), str(row["kind"]), row["payload"].get("tick_index"))


def _fmt_liquidation(items: list[dict[str, Any]]) -> str:
    parts = []
    total_penalty = 0
    for p in items:
        verb = "covered" if p["side"] == "BUY" else "sold"
        parts.append(f"{verb} {p['qty']:,} {p['ticker']} @ {format_price(p['fill'])}")
        total_penalty += int(p.get("penalty", 0))
    noun = "position" if len(items) == 1 else "positions"
    return (
        f"Liquidated on {len(items)} {noun}: " + ", ".join(parts)
        + f" — penalty {format_money(total_penalty)} total. Check `/margin`."
    )


def _fmt_knockout(items: list[dict[str, Any]]) -> str:
    parts = [
        f"{p['ticker']} {p['qty']:,} @ entry {format_price(p['entry'])} "
        f"(KO {format_price(p['ko_price'])})"
        for p in items
    ]
    noun = "short" if len(items) == 1 else "shorts"
    return f"Bounded {noun} knocked out: " + ", ".join(parts) + "."


def _fmt_order_filled(items: list[dict[str, Any]]) -> str:
    parts = [
        f"{p['side']} {p['qty']:,} {p['ticker']} @ {format_price(p['fill'])} "
        f"(#{p['order_id']}, {'maker' if p.get('maker') else 'taker'})"
        for p in items
    ]
    noun = "order" if len(items) == 1 else "orders"
    return f"{len(items)} {noun} filled: " + ", ".join(parts) + "."


def _fmt_season_result(items: list[dict[str, Any]]) -> str:
    lines = []
    for p in items:
        if p.get("rank") is None:
            lines.append(
                f"**{p['season_name']}** finished — you didn't qualify "
                "for a rank (not enough active trading days)."
            )
            continue
        lines.append(
            f"**{p['season_name']}** finished — rank #{p['rank']}, "
            f"score {p['score']:.2f}"
            + (f", prize {format_money(p['prize'])}" if p.get("prize") else "")
        )
    return "\n".join(lines)


_FORMATTERS: dict[str, Callable[[list[dict[str, Any]]], str]] = {
    "LIQUIDATION": _fmt_liquidation,
    "KNOCKOUT": _fmt_knockout,
    "ORDER_FILLED": _fmt_order_filled,
    "SEASON_RESULT": _fmt_season_result,
}


async def _mark_sent(conn: AsyncConnection, ids: list[int]) -> None:
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE notifications SET sent_at = now() WHERE id = ANY(%s)", (ids,)
        )


async def _dead_letter_user(conn: AsyncConnection, user_id: int, reason: str) -> None:
    """Forbidden (DMs closed): stop retrying *every* pending row for this
    user right away, not just the ones in the current batch -- otherwise
    the next poll just rediscovers the same failure and repeats it."""
    async with conn.cursor() as cur:
        await cur.execute(
            """
            UPDATE notifications SET attempts = %s, last_error = %s
            WHERE user_id = %s AND sent_at IS NULL
            """,
            (MAX_ATTEMPTS, reason, user_id),
        )


async def _retry_later(conn: AsyncConnection, ids: list[int], error: str) -> None:
    """Bump attempts and schedule the next try with exponential backoff.
    Every expression here reads the *old* row (Postgres UPDATE semantics
    -- SET targets never see each other's new values), so `attempts + 1`
    consistently means "the attempt count this failure produces"."""
    async with conn.cursor() as cur:
        await cur.execute(
            """
            UPDATE notifications
            SET attempts = attempts + 1,
                last_error = %s,
                next_attempt_at = now() + (
                    CASE WHEN attempts + 1 <= array_length(%s::int[], 1)
                         THEN (%s::int[])[attempts + 1]
                         ELSE (%s::int[])[array_length(%s::int[], 1)]
                    END * interval '1 minute'
                )
            WHERE id = ANY(%s)
            """,
            (
                error,
                list(_BACKOFF_MINUTES),
                list(_BACKOFF_MINUTES),
                list(_BACKOFF_MINUTES),
                list(_BACKOFF_MINUTES),
                ids,
            ),
        )


async def poll_once(conn: AsyncConnection, deliver: Deliver) -> dict[str, int]:
    """One poll cycle: fetch pending rows, coalesce, deliver, and record
    the outcome -- all in the caller's transaction (one poll = one tx).

    Returns delivery stats for the bot heartbeat detail.
    """
    rows = await _fetch_batch(conn)
    sent = failed = dead_lettered = 0
    rows.sort(key=_group_key)
    for (user_id, kind, _tick), group_iter in groupby(rows, key=_group_key):
        group = list(group_iter)
        ids = [int(r["id"]) for r in group]
        payloads = [r["payload"] for r in group]
        formatter = _FORMATTERS.get(kind)
        message = formatter(payloads) if formatter else f"{kind} notification"
        try:
            await deliver(user_id, message)
        except DeliveryForbidden:
            await _dead_letter_user(conn, user_id, "forbidden: dm closed")
            dead_lettered += len(ids)
        except Exception as exc:  # noqa: BLE001 -- any delivery failure retries
            log.warning("notification delivery failed user=%s kind=%s: %s", user_id, kind, exc)
            await _retry_later(conn, ids, f"{type(exc).__name__}: {exc}"[:200])
            failed += len(ids)
        else:
            await _mark_sent(conn, ids)
            sent += len(ids)
    return {"sent": sent, "failed": failed, "dead_lettered": dead_lettered}
