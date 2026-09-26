"""IPO subscriptions (0042): offering creation, escrow commits, pro-rata
allocation with affordability caps, refunds, activation, and the
zero-subscription cancellation path."""

from __future__ import annotations

from decimal import Decimal

import pytest
from psycopg import AsyncConnection
from psycopg.rows import dict_row

from stockbot.accounts.service import bootstrap_user
from stockbot.ipo.service import (
    create_offering,
    list_offerings,
    settle_due,
    subscribe,
)
from stockbot.ledger.errors import InsufficientFundsError
from stockbot.ledger.service import (
    get_balance,
    get_system_account_id,
    get_user_account_id,
    post_transfer,
)

START = 10_000_000  # $100k test funding


async def _fund(conn: AsyncConnection, user_id: int, amount: int = START) -> None:
    await bootstrap_user(conn, user_id)
    await post_transfer(
        conn,
        from_account_id=await get_system_account_id(conn, "FAUCET"),
        to_account_id=await get_user_account_id(conn, user_id),
        amount=amount,
        reason="TEST_TOPUP",
    )


async def _make_ipo(
    conn: AsyncConnection,
    ticker: str,
    *,
    price: float = 10.0,
    shares: int = 100,
    duration: int = 50,
) -> int:
    return await create_offering(
        conn,
        ticker=ticker,
        name=f"{ticker} Corp",
        sector_key="TECH",
        offer_price=price,
        shares_offered=shares,
        duration_ticks=duration,
    )


async def _offering(conn: AsyncConnection, offering_id: int) -> dict:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT * FROM ipo_offerings WHERE id = %s", (offering_id,)
        )
        return await cur.fetchone()


async def _is_active(conn: AsyncConnection, ticker: str) -> bool:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT is_active FROM instruments WHERE ticker = %s", (ticker,)
        )
        (active,) = await cur.fetchone()
    return bool(active)


async def _position(conn: AsyncConnection, user_id: int, ticker: str) -> dict | None:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            SELECT p.quantity, p.avg_cost FROM positions p
            JOIN instruments i ON i.id = p.instrument_id
            WHERE p.user_id = %s AND i.ticker = %s AND p.season_id IS NULL
            """,
            (user_id, ticker),
        )
        return await cur.fetchone()


async def test_create_offering_lists_inactive(conn: AsyncConnection) -> None:
    offering_id = await _make_ipo(conn, "IPOA")
    row = await _offering(conn, offering_id)
    assert row["status"] == "OPEN"
    assert row["close_tick"] == row["open_tick"] + 50
    assert not await _is_active(conn, "IPOA")  # dormant until settlement


async def test_subscribe_commits_to_escrow_and_stacks(conn: AsyncConnection) -> None:
    await _fund(conn, 9201)
    await _make_ipo(conn, "IPOB")
    escrow = await get_system_account_id(conn, "IPO_ESCROW")
    escrow_before = await get_balance(conn, escrow)

    r1 = await subscribe(conn, user_id=9201, ticker="IPOB", amount_minor=40_000)
    r2 = await subscribe(conn, user_id=9201, ticker="IPOB", amount_minor=5_000)
    assert r1.committed_minor == 40_000
    assert r2.total_committed_minor == 45_000
    assert await get_balance(conn, escrow) == escrow_before + 45_000
    assert await get_balance(conn, await get_user_account_id(conn, 9201)) == (
        START + 10_000 - 45_000  # grant + topup - committed
    )


async def test_subscribe_rejects_closed_and_unknown(conn: AsyncConnection) -> None:
    await _fund(conn, 9202)
    with pytest.raises(ValueError, match="no open IPO"):
        await subscribe(conn, user_id=9202, ticker="NOPE", amount_minor=100)
    offering_id = await _make_ipo(conn, "IPOC", duration=10)
    offering = await _offering(conn, offering_id)
    # After settlement the window is gone: status SETTLED -> "no open IPO".
    await settle_due(conn, int(offering["close_tick"]))
    with pytest.raises(ValueError, match="no open IPO"):
        await subscribe(conn, user_id=9202, ticker="IPOC", amount_minor=100)


async def test_subscribe_rejects_insufficient_funds(conn: AsyncConnection) -> None:
    await bootstrap_user(conn, 9203)
    await _make_ipo(conn, "IPOD")
    with pytest.raises(InsufficientFundsError):
        await subscribe(conn, user_id=9203, ticker="IPOD", amount_minor=START)


async def test_settle_oversubscribed_prorata_refund_and_activate(
    conn: AsyncConnection,
) -> None:
    await _fund(conn, 9210)
    await _fund(conn, 9211)
    # $10 offer x 100 shares = $1,000 float; A commits $400, B commits $800.
    offering_id = await _make_ipo(conn, "IPOE", price=10.0, shares=100)
    offering = await _offering(conn, offering_id)
    await subscribe(conn, user_id=9210, ticker="IPOE", amount_minor=40_000)
    await subscribe(conn, user_id=9211, ticker="IPOE", amount_minor=80_000)

    assert await settle_due(conn, int(offering["close_tick"])) == 1

    pos_a = await _position(conn, 9210, "IPOE")
    pos_b = await _position(conn, 9211, "IPOE")
    assert pos_a is not None and pos_b is not None
    # 33/66 pro-rata + the dust share to the largest remainder (B).
    assert int(pos_a["quantity"]) == 33
    assert int(pos_b["quantity"]) == 67
    assert Decimal(pos_a["avg_cost"]) == Decimal("10")
    assert Decimal(pos_b["avg_cost"]) == Decimal("10")

    # Cash conservation: escrow out = SINK proceeds + refunds.
    bal_a = await get_balance(conn, await get_user_account_id(conn, 9210))
    bal_b = await get_balance(conn, await get_user_account_id(conn, 9211))
    assert bal_a == START + 10_000 - 33 * 1000  # refund $70
    assert bal_b == START + 10_000 - 67 * 1000  # refund $130

    row = await _offering(conn, offering_id)
    assert row["status"] == "SETTLED"
    assert int(row["allocated_qty"]) == 100
    assert await _is_active(conn, "IPOE")

    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT COUNT(*) FROM notifications "
            "WHERE kind = 'IPO_SETTLED' AND user_id IN (9210, 9211)"
        )
        (dm_count,) = await cur.fetchone()
    assert dm_count == 2


async def test_settle_undersubscribed_full_allocation(conn: AsyncConnection) -> None:
    await _fund(conn, 9220)
    offering_id = await _make_ipo(conn, "IPOF", price=10.0, shares=100)
    offering = await _offering(conn, offering_id)
    await subscribe(conn, user_id=9220, ticker="IPOF", amount_minor=5_000)  # $50

    await settle_due(conn, int(offering["close_tick"]))
    pos = await _position(conn, 9220, "IPOF")
    assert pos is not None
    assert int(pos["quantity"]) == 5  # affordable cap, not 100
    row = await _offering(conn, offering_id)
    assert row["status"] == "SETTLED" and int(row["allocated_qty"]) == 5
    assert await get_balance(conn, await get_user_account_id(conn, 9220)) == (
        START + 10_000 - 5_000  # no refund due: all $50 converted
    )


async def test_settle_zero_subscriptions_cancels(conn: AsyncConnection) -> None:
    offering_id = await _make_ipo(conn, "IPOG")
    offering = await _offering(conn, offering_id)
    await settle_due(conn, int(offering["close_tick"]))
    row = await _offering(conn, offering_id)
    assert row["status"] == "CANCELLED"
    assert not await _is_active(conn, "IPOG")


async def test_settle_is_idempotent(conn: AsyncConnection) -> None:
    await _fund(conn, 9230)
    offering_id = await _make_ipo(conn, "IPOH", price=10.0, shares=10)
    offering = await _offering(conn, offering_id)
    await subscribe(conn, user_id=9230, ticker="IPOH", amount_minor=10_000)
    await settle_due(conn, int(offering["close_tick"]))
    assert await settle_due(conn, int(offering["close_tick"]) + 1) == 0
    pos = await _position(conn, 9230, "IPOH")
    assert int(pos["quantity"]) == 10


async def test_list_offerings_shows_my_commitment(conn: AsyncConnection) -> None:
    await _fund(conn, 9240)
    await _make_ipo(conn, "IPOI")
    await subscribe(conn, user_id=9240, ticker="IPOI", amount_minor=7_500)
    rows = await list_offerings(conn, 9240)
    mine = next(r for r in rows if r["ticker"] == "IPOI")
    assert int(mine["my_committed_minor"]) == 7_500
