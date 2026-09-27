"""One-shot price alerts: create/list/cancel, per-tick sweep, outbox."""

from decimal import Decimal

import pytest
from psycopg import AsyncConnection

from stockbot.accounts.service import bootstrap_user
from stockbot.admin.service import delist_instrument, disable_user
from stockbot.alerts.errors import (
    AlertError,
    AlertLimitError,
    DuplicateAlertError,
)
from stockbot.alerts.service import (
    cancel_alert,
    create_alert,
    list_alerts,
    sweep_alerts,
)
from stockbot.bot.notify import _fmt_alert_triggered
from stockbot.market.tick import apply_tick
from stockbot.trading.errors import UnknownInstrumentError

SEED = "test-alerts-seed"


async def _mark(conn: AsyncConnection, ticker: str = "NORT") -> Decimal:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT quoted_price FROM instruments WHERE ticker = %s", (ticker,)
        )
        row = await cur.fetchone()
    assert row is not None
    return Decimal(str(row[0]))


async def _set_mark(conn: AsyncConnection, price: float, ticker: str = "NORT") -> None:
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE instruments SET quoted_price = %s WHERE ticker = %s",
            (price, ticker),
        )


async def _alert_status(conn: AsyncConnection, alert_id: int) -> str:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT status FROM price_alerts WHERE id = %s", (alert_id,)
        )
        row = await cur.fetchone()
    assert row is not None
    return str(row[0])


async def _notifications(conn: AsyncConnection, user_id: int):
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT kind, payload FROM notifications WHERE user_id = %s",
            (user_id,),
        )
        return await cur.fetchall()


async def test_create_and_list_shows_mark(conn: AsyncConnection) -> None:
    await bootstrap_user(conn, 1)
    alert_id = await create_alert(
        conn, user_id=1, ticker="nort", direction="above", target=Decimal("999")
    )
    rows = await list_alerts(conn, 1)
    assert len(rows["open"]) == 1
    row = rows["open"][0]
    assert row["id"] == alert_id
    assert row["ticker"] == "NORT"
    assert row["direction"] == "ABOVE"
    assert Decimal(str(row["quoted_price"])) == await _mark(conn)
    assert rows["closed"] == []


async def test_above_alert_triggers_on_cross_up(conn: AsyncConnection) -> None:
    await bootstrap_user(conn, 2)
    mark = await _mark(conn)
    alert_id = await create_alert(
        conn, user_id=2, ticker="NORT", direction="ABOVE", target=mark * 2
    )
    await _set_mark(conn, float(mark) * 2.5)
    fired = await sweep_alerts(conn, 5)
    assert fired == 1
    assert await _alert_status(conn, alert_id) == "TRIGGERED"


async def test_below_alert_triggers_on_cross_down(conn: AsyncConnection) -> None:
    await bootstrap_user(conn, 3)
    mark = await _mark(conn)
    alert_id = await create_alert(
        conn, user_id=3, ticker="NORT", direction="BELOW", target=mark / 2
    )
    await _set_mark(conn, float(mark) * 0.4)
    fired = await sweep_alerts(conn, 6)
    assert fired == 1
    assert await _alert_status(conn, alert_id) == "TRIGGERED"


async def test_non_crossing_tick_leaves_alert_open(conn: AsyncConnection) -> None:
    await bootstrap_user(conn, 4)
    mark = await _mark(conn)
    alert_id = await create_alert(
        conn, user_id=4, ticker="NORT", direction="ABOVE", target=mark * 10
    )
    fired = await sweep_alerts(conn, 7)
    assert fired == 0
    assert await _alert_status(conn, alert_id) == "OPEN"


async def test_triggered_alert_is_one_shot(conn: AsyncConnection) -> None:
    await bootstrap_user(conn, 5)
    await create_alert(
        conn, user_id=5, ticker="NORT", direction="ABOVE", target=Decimal("1")
    )
    first = await sweep_alerts(conn, 8)
    second = await sweep_alerts(conn, 9)
    assert first == 1
    assert second == 0
    notes = await _notifications(conn, 5)
    assert len(notes) == 1  # no duplicate notification on the second sweep


async def test_duplicate_alert_rejected(conn: AsyncConnection) -> None:
    await bootstrap_user(conn, 6)
    await create_alert(
        conn, user_id=6, ticker="NORT", direction="ABOVE", target=Decimal("50")
    )
    with pytest.raises(DuplicateAlertError):
        await create_alert(
            conn, user_id=6, ticker="NORT", direction="ABOVE", target=Decimal("50")
        )
    # ...but the opposite direction at the same price is a different alert.
    other = await create_alert(
        conn, user_id=6, ticker="NORT", direction="BELOW", target=Decimal("50")
    )
    assert other > 0
    # And after cancelling, the same alert can be re-created.
    rows = await list_alerts(conn, 6)
    dup_id = next(r["id"] for r in rows["open"] if r["direction"] == "ABOVE")
    assert await cancel_alert(conn, user_id=6, alert_id=dup_id)
    assert await create_alert(
        conn, user_id=6, ticker="NORT", direction="ABOVE", target=Decimal("50")
    )


async def test_per_user_cap(conn: AsyncConnection) -> None:
    await bootstrap_user(conn, 7)
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE config SET value = 2 WHERE key = 'alerts.max_per_user'"
        )
    await create_alert(
        conn, user_id=7, ticker="NORT", direction="ABOVE", target=Decimal("10")
    )
    await create_alert(
        conn, user_id=7, ticker="NORT", direction="ABOVE", target=Decimal("20")
    )
    with pytest.raises(AlertLimitError):
        await create_alert(
            conn, user_id=7, ticker="NORT", direction="ABOVE", target=Decimal("30")
        )


async def test_triggered_records_price_and_payload(conn: AsyncConnection) -> None:
    await bootstrap_user(conn, 8)
    alert_id = await create_alert(
        conn, user_id=8, ticker="NORT", direction="ABOVE", target=Decimal("1")
    )
    await _set_mark(conn, 42.5)
    await sweep_alerts(conn, 11)
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT triggered_tick, triggered_price FROM price_alerts WHERE id = %s",
            (alert_id,),
        )
        row = await cur.fetchone()
    assert row is not None
    assert row[0] == 11
    assert Decimal(str(row[1])) == Decimal("42.5")

    notes = await _notifications(conn, 8)
    assert len(notes) == 1
    kind, payload = notes[0]
    assert kind == "ALERT_TRIGGERED"
    assert payload["ticker"] == "NORT"
    assert payload["direction"] == "ABOVE"
    assert payload["tick_index"] == 11
    assert float(payload["mark"]) == pytest.approx(42.5)


async def test_inactive_instrument_not_swept(conn: AsyncConnection) -> None:
    await bootstrap_user(conn, 9)
    alert_id = await create_alert(
        conn, user_id=9, ticker="NORT", direction="ABOVE", target=Decimal("1")
    )
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE instruments SET is_active = FALSE WHERE ticker = 'NORT'"
        )
    assert await sweep_alerts(conn, 12) == 0
    assert await _alert_status(conn, alert_id) == "OPEN"
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE instruments SET is_active = TRUE WHERE ticker = 'NORT'"
        )


async def test_create_rejects_unknown_and_dormant(conn: AsyncConnection) -> None:
    await bootstrap_user(conn, 10)
    with pytest.raises(UnknownInstrumentError):
        await create_alert(
            conn, user_id=10, ticker="NOPE", direction="ABOVE", target=Decimal("1")
        )
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE instruments SET is_active = FALSE WHERE ticker = 'NORT'"
        )
    with pytest.raises(AlertError, match="not tradeable"):
        await create_alert(
            conn, user_id=10, ticker="NORT", direction="ABOVE", target=Decimal("1")
        )
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE instruments SET is_active = TRUE WHERE ticker = 'NORT'"
        )
    with pytest.raises(AlertError, match="direction"):
        await create_alert(
            conn, user_id=10, ticker="NORT", direction="SIDEWAYS",
            target=Decimal("1"),
        )


async def test_cancel_only_own_alert(conn: AsyncConnection) -> None:
    await bootstrap_user(conn, 11)
    await bootstrap_user(conn, 12)
    alert_id = await create_alert(
        conn, user_id=11, ticker="NORT", direction="ABOVE", target=Decimal("9999")
    )
    # Another user's cancel never finds it -- no existence leak.
    assert not await cancel_alert(conn, user_id=12, alert_id=alert_id)
    assert await _alert_status(conn, alert_id) == "OPEN"
    assert await cancel_alert(conn, user_id=11, alert_id=alert_id)
    assert await _alert_status(conn, alert_id) == "CANCELLED"
    # Second cancel is a no-op.
    assert not await cancel_alert(conn, user_id=11, alert_id=alert_id)


async def test_delist_cancels_open_alerts(conn: AsyncConnection) -> None:
    await bootstrap_user(conn, 13)
    await create_alert(
        conn, user_id=13, ticker="NORT", direction="ABOVE", target=Decimal("1")
    )
    await create_alert(
        conn, user_id=13, ticker="NORT", direction="BELOW", target=Decimal("1e9")
    )
    report = await delist_instrument(conn, "NORT")
    assert report.alerts_cancelled == 2
    rows = await list_alerts(conn, 13)
    assert rows["open"] == []
    assert {r["status"] for r in rows["closed"]} == {"CANCELLED"}


async def test_suspend_cancels_open_alerts(conn: AsyncConnection) -> None:
    await bootstrap_user(conn, 14)
    alert_id = await create_alert(
        conn, user_id=14, ticker="NORT", direction="ABOVE", target=Decimal("9999")
    )
    assert await disable_user(conn, 14, "test suspension", 99)
    assert await _alert_status(conn, alert_id) == "CANCELLED"


async def test_apply_tick_runs_alert_sweep(conn: AsyncConnection) -> None:
    """End-to-end: a real open tick fires an already-crossed alert. A BELOW
    target far above the mark crosses no matter which way the model steps
    (the breaker caps any single-tick move far under 2x)."""
    await bootstrap_user(conn, 15)
    mark = await _mark(conn)
    alert_id = await create_alert(
        conn, user_id=15, ticker="NORT", direction="BELOW",
        target=mark * 2,
    )
    await apply_tick(conn, SEED)
    assert await _alert_status(conn, alert_id) == "TRIGGERED"
    notes = await _notifications(conn, 15)
    assert len(notes) == 1 and notes[0][0] == "ALERT_TRIGGERED"


async def test_grouped_notification_format() -> None:
    text = _fmt_alert_triggered(
        [
            {
                "tick_index": 5,
                "ticker": "NORT",
                "direction": "ABOVE",
                "target": 50.0,
                "mark": 51.25,
            },
            {
                "tick_index": 5,
                "ticker": "HARB",
                "direction": "BELOW",
                "target": 10.0,
                "mark": 9.5,
            },
        ]
    )
    assert text.startswith("Price alerts: ")
    assert "**NORT** crossed above $50.00 (now $51.25)" in text
    assert "**HARB** crossed below $10.00 (now $9.50)" in text


async def test_single_notification_format() -> None:
    text = _fmt_alert_triggered(
        [
            {
                "tick_index": 3,
                "ticker": "NORT",
                "direction": "ABOVE",
                "target": 50.0,
                "mark": 50.5,
            }
        ]
    )
    assert text.startswith("Price alert: ")
