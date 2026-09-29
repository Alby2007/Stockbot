"""N1: the transactional outbox poller -- coalescing, opt-out, backoff,
and dead-lettering. The insert sites themselves (margin, shorts, orders,
seasons) are exercised in their own test files; this file is only about
what happens to a row once it's in `notifications`.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from psycopg import AsyncConnection

from stockbot.accounts.service import bootstrap_user
from stockbot.bot.notify import DeliveryForbidden, poll_once


@pytest.fixture(autouse=True)
async def _clean_outbox(conn: AsyncConnection) -> None:
    """poll_once scans the WHOLE pending set -- command-level tests
    elsewhere in the suite commit real outbox rows (e.g. H2's
    ACCOUNT_SUSPENDED), so each test starts from an empty outbox
    (rolled back with everything else)."""
    async with conn.cursor() as cur:
        await cur.execute("DELETE FROM notifications")


async def _insert(
    conn: AsyncConnection,
    user_id: int,
    kind: str,
    payload: dict[str, Any],
) -> int:
    async with conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO notifications (user_id, kind, payload) "
            "VALUES (%s, %s, %s) RETURNING id",
            (user_id, kind, json.dumps(payload)),
        )
        row = await cur.fetchone()
        assert row is not None
        return int(row[0])


async def _row(conn: AsyncConnection, notif_id: int) -> dict[str, Any]:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT sent_at, attempts, last_error, next_attempt_at "
            "FROM notifications WHERE id = %s",
            (notif_id,),
        )
        row = await cur.fetchone()
    assert row is not None
    return {
        "sent_at": row[0],
        "attempts": row[1],
        "last_error": row[2],
        "next_attempt_at": row[3],
    }


async def test_coalesces_same_user_kind_tick_into_one_message(
    conn: AsyncConnection,
) -> None:
    await bootstrap_user(conn, 9001)
    await _insert(
        conn, 9001, "LIQUIDATION",
        {"tick_index": 100, "ticker": "NORT", "side": "SELL", "qty": 5,
         "fill": 10.0, "penalty": 100, "equity_before": 500},
    )
    await _insert(
        conn, 9001, "LIQUIDATION",
        {"tick_index": 100, "ticker": "WEST", "side": "BUY", "qty": 3,
         "fill": 20.0, "penalty": 50, "equity_before": 500},
    )

    delivered: list[tuple[int, str]] = []

    async def deliver(user_id: int, message: str) -> None:
        delivered.append((user_id, message))

    stats = await poll_once(conn, deliver)

    assert stats == {"sent": 2, "failed": 0, "dead_lettered": 0}
    assert len(delivered) == 1  # one message, not two
    user_id, message = delivered[0]
    assert user_id == 9001
    assert "NORT" in message and "WEST" in message
    assert "$1.50" in message  # combined penalty 100+50=150 minor


async def test_different_ticks_do_not_coalesce(conn: AsyncConnection) -> None:
    await bootstrap_user(conn, 9002)
    await _insert(
        conn, 9002, "KNOCKOUT",
        {"tick_index": 1, "ticker": "NORT", "qty": 1, "entry": 10.0,
         "ko_price": 12.5, "payout": 0},
    )
    await _insert(
        conn, 9002, "KNOCKOUT",
        {"tick_index": 2, "ticker": "NORT", "qty": 1, "entry": 10.0,
         "ko_price": 12.5, "payout": 0},
    )

    delivered: list[str] = []

    async def deliver(user_id: int, message: str) -> None:
        delivered.append(message)

    await poll_once(conn, deliver)
    assert len(delivered) == 2


async def test_opted_out_user_row_stays_pending(conn: AsyncConnection) -> None:
    await bootstrap_user(conn, 9003)
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE users SET dm_notifications = FALSE WHERE id = %s", (9003,)
        )
    notif_id = await _insert(
        conn, 9003, "ORDER_FILLED",
        {"tick_index": 1, "order_id": 1, "ticker": "NORT", "side": "BUY",
         "qty": 1, "fill": 10.0, "maker": False},
    )

    called = False

    async def deliver(user_id: int, message: str) -> None:
        nonlocal called
        called = True

    stats = await poll_once(conn, deliver)
    assert stats == {"sent": 0, "failed": 0, "dead_lettered": 0}
    assert not called

    row = await _row(conn, notif_id)
    assert row["sent_at"] is None
    assert row["attempts"] == 0  # untouched, not retried/dead-lettered


async def test_opting_back_in_delivers_the_backlog(conn: AsyncConnection) -> None:
    await bootstrap_user(conn, 9004)
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE users SET dm_notifications = FALSE WHERE id = %s", (9004,)
        )
    notif_id = await _insert(
        conn, 9004, "ORDER_FILLED",
        {"tick_index": 1, "order_id": 1, "ticker": "NORT", "side": "BUY",
         "qty": 1, "fill": 10.0, "maker": False},
    )

    async def deliver(user_id: int, message: str) -> None:
        pass

    await poll_once(conn, deliver)
    row = await _row(conn, notif_id)
    assert row["sent_at"] is None  # still held back

    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE users SET dm_notifications = TRUE WHERE id = %s", (9004,)
        )
    await poll_once(conn, deliver)
    row = await _row(conn, notif_id)
    assert row["sent_at"] is not None  # the backlog surfaced


async def test_forbidden_dead_letters_all_pending_rows_for_user(
    conn: AsyncConnection,
) -> None:
    await bootstrap_user(conn, 9005)
    id_a = await _insert(
        conn, 9005, "ORDER_FILLED",
        {"tick_index": 1, "order_id": 1, "ticker": "NORT", "side": "BUY",
         "qty": 1, "fill": 10.0, "maker": False},
    )
    id_b = await _insert(
        conn, 9005, "ORDER_FILLED",
        {"tick_index": 2, "order_id": 2, "ticker": "WEST", "side": "SELL",
         "qty": 1, "fill": 5.0, "maker": True},
    )

    async def deliver(user_id: int, message: str) -> None:
        raise DeliveryForbidden("dm closed")

    stats = await poll_once(conn, deliver)
    assert stats["dead_lettered"] == 2

    for notif_id in (id_a, id_b):
        row = await _row(conn, notif_id)
        assert row["sent_at"] is None
        assert row["attempts"] >= 5
        assert row["last_error"] == "forbidden: dm closed"

    # A dead-lettered user's rows never get re-picked up.
    called = False

    async def deliver2(user_id: int, message: str) -> None:
        nonlocal called
        called = True

    await poll_once(conn, deliver2)
    assert not called


async def test_transient_failure_schedules_backoff_retry(
    conn: AsyncConnection,
) -> None:
    await bootstrap_user(conn, 9006)
    notif_id = await _insert(
        conn, 9006, "ORDER_FILLED",
        {"tick_index": 1, "order_id": 1, "ticker": "NORT", "side": "BUY",
         "qty": 1, "fill": 10.0, "maker": False},
    )

    async def deliver(user_id: int, message: str) -> None:
        raise RuntimeError("transient network error")

    stats = await poll_once(conn, deliver)
    assert stats["failed"] == 1

    row = await _row(conn, notif_id)
    assert row["sent_at"] is None
    assert row["attempts"] == 1
    assert "RuntimeError" in row["last_error"]

    # Not due yet -- a second immediate poll doesn't retry it.
    called = False

    async def deliver2(user_id: int, message: str) -> None:
        nonlocal called
        called = True

    await poll_once(conn, deliver2)
    assert not called


async def test_dead_letters_after_max_attempts(conn: AsyncConnection) -> None:
    await bootstrap_user(conn, 9007)
    notif_id = await _insert(
        conn, 9007, "ORDER_FILLED",
        {"tick_index": 1, "order_id": 1, "ticker": "NORT", "side": "BUY",
         "qty": 1, "fill": 10.0, "maker": False},
    )

    async def deliver(user_id: int, message: str) -> None:
        raise RuntimeError("still down")

    for _ in range(5):
        async with conn.cursor() as cur:
            # Force the row due immediately -- we're testing the attempt
            # ceiling, not the backoff delay.
            await cur.execute(
                "UPDATE notifications SET next_attempt_at = now() WHERE id = %s",
                (notif_id,),
            )
        await poll_once(conn, deliver)

    row = await _row(conn, notif_id)
    assert row["attempts"] == 5

    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE notifications SET next_attempt_at = now() WHERE id = %s",
            (notif_id,),
        )
    called = False

    async def deliver2(user_id: int, message: str) -> None:
        nonlocal called
        called = True

    await poll_once(conn, deliver2)
    assert not called  # attempts >= 5 excludes it from the next batch


async def test_sent_rows_are_not_repolled(conn: AsyncConnection) -> None:
    await bootstrap_user(conn, 9008)
    await _insert(
        conn, 9008, "ORDER_FILLED",
        {"tick_index": 1, "order_id": 1, "ticker": "NORT", "side": "BUY",
         "qty": 1, "fill": 10.0, "maker": False},
    )

    calls = 0

    async def deliver(user_id: int, message: str) -> None:
        nonlocal calls
        calls += 1

    await poll_once(conn, deliver)
    await poll_once(conn, deliver)
    assert calls == 1


async def test_entitlement_expiring_formats_relative_days(conn: AsyncConnection) -> None:
    await bootstrap_user(conn, 9010)
    await _insert(
        conn, 9010, "ENTITLEMENT_EXPIRING",
        {"tick_index": 5, "item_key": "analyst_tools",
         "name": "Analyst tools", "days_left": 2},
    )
    await _insert(
        conn, 9010, "ENTITLEMENT_EXPIRING",
        {"tick_index": 5, "item_key": "pro_terminal",
         "name": "Pro Terminal", "days_left": 1},
    )

    delivered: list[str] = []

    async def deliver(user_id: int, message: str) -> None:
        delivered.append(message)

    stats = await poll_once(conn, deliver)

    assert stats["sent"] == 2
    assert len(delivered) == 1  # same user/kind/tick coalesces
    msg = delivered[0]
    assert "Expiring soon:" in msg
    assert "**Analyst tools** (in 2d)" in msg
    assert "**Pro Terminal** (tomorrow)" in msg
    assert "/shop" in msg


async def test_gift_received_mentions_sender(conn: AsyncConnection) -> None:
    await bootstrap_user(conn, 9011)
    await _insert(
        conn, 9011, "GIFT_RECEIVED",
        {"from": 4242, "item_key": "title_degen", "name": "Degen"},
    )

    delivered: list[str] = []

    async def deliver(user_id: int, message: str) -> None:
        delivered.append(message)

    await poll_once(conn, deliver)

    assert len(delivered) == 1
    assert "<@4242> sent you **Degen**" in delivered[0]
    assert "/equip" in delivered[0]
