"""Card trading: non-custodial card-for-card offers. Accept re-verifies
both binders under row locks, merges frames/serials/copies, moves shard
legs through 'TRADE' shard_events, and clears featured pins.
"""

from __future__ import annotations

import pytest
from psycopg import AsyncConnection

from stockbot.accounts.service import bootstrap_user
from stockbot.collectibles.trades import (
    TradeError,
    accept,
    cancel,
    create_offer,
    decline,
    get_trade,
    list_trades,
)

_A = 8101
_B = 8102
_C = 8103


async def _hold(
    conn: AsyncConnection,
    user_id: int,
    card_key: str,
    frame: str = "STANDARD",
    serial: int | None = None,
    copies: int = 1,
) -> None:
    async with conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO user_cards"
            " (user_id, card_key, best_frame, best_serial, copies)"
            " VALUES (%s, %s, %s, %s, %s)",
            (user_id, card_key, frame, serial, copies),
        )


async def _frame(conn: AsyncConnection, user_id: int, card_key: str):
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT best_frame, best_serial, copies FROM user_cards"
            " WHERE user_id = %s AND card_key = %s",
            (user_id, card_key),
        )
        return await cur.fetchone()


async def test_offer_lifecycle_accept(conn: AsyncConnection) -> None:
    await bootstrap_user(conn, _A)
    await bootstrap_user(conn, _B)
    await _hold(conn, _A, "card_nort", "GOLD", serial=3)
    await _hold(conn, _B, "card_sout", "SILVER", serial=12)

    tid = await create_offer(
        conn, _A, _B, ["card_nort"], ["card_sout"], 0, 0, 100
    )
    # counterparty was notified of the offer
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT 1 FROM notifications WHERE user_id = %s"
            " AND kind = 'TRADE_OFFER'",
            (_B,),
        )
        assert await cur.fetchone() is not None

    await accept(conn, tid, _B, 110)
    trade = await get_trade(conn, tid)
    assert trade is not None and trade.status == "ACCEPTED"

    # cards swapped wholesale: A holds sout, B holds nort
    assert await _frame(conn, _A, "card_nort") is None
    assert (await _frame(conn, _A, "card_sout"))[0] == "SILVER"
    assert (await _frame(conn, _B, "card_nort"))[0] == "GOLD"
    assert await _frame(conn, _B, "card_sout") is None

    # both parties got the result DM
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT count(*) FROM notifications WHERE kind = 'TRADE_RESULT'"
        )
        assert int((await cur.fetchone())[0]) == 2


async def test_accept_merges_rows(conn: AsyncConnection) -> None:
    """Same card both ways: best frame wins, lowest serial survives,
    copies sum."""
    await bootstrap_user(conn, _A)
    await bootstrap_user(conn, _B)
    await _hold(conn, _A, "card_nort", "STANDARD", serial=5, copies=2)
    await _hold(conn, _B, "card_nort", "PLATINUM", serial=120, copies=1)
    tid = await create_offer(conn, _A, _B, ["card_nort"], [], 0, 0, 100)
    await accept(conn, tid, _B, 110)
    row = await _frame(conn, _B, "card_nort")
    assert row[0] == "PLATINUM"  # better frame survives
    assert int(row[1]) == 5  # lowest mint wins the merge
    assert int(row[2]) == 3  # copies sum


async def test_accept_moves_shards_with_audit(conn: AsyncConnection) -> None:
    await bootstrap_user(conn, _A)
    await bootstrap_user(conn, _B)
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE users SET shards = 100 WHERE id IN (%s, %s)", (_A, _B)
        )
        # ledger the seeding so the SUM invariant holds
        for uid in (_A, _B):
            await cur.execute(
                "INSERT INTO shard_events (user_id, delta, reason)"
                " VALUES (%s, 100, 'LEGACY_ADJUST')",
                (uid,),
            )
    await _hold(conn, _A, "card_nort")
    tid = await create_offer(conn, _A, _B, ["card_nort"], [], 30, 0, 100)
    await accept(conn, tid, _B, 110)
    async with conn.cursor() as cur:
        for uid, want in ((_A, 70), (_B, 130)):
            await cur.execute("SELECT shards FROM users WHERE id = %s", (uid,))
            assert int((await cur.fetchone())[0]) == want
        await cur.execute(
            "SELECT count(*) FROM shard_events WHERE reason = 'TRADE'"
        )
        assert int((await cur.fetchone())[0]) == 2  # one signed pair


async def test_self_trade_rejected(conn: AsyncConnection) -> None:
    await bootstrap_user(conn, _A)
    await _hold(conn, _A, "card_nort")
    with pytest.raises(TradeError, match="yourself"):
        await create_offer(conn, _A, _A, ["card_nort"], [], 0, 0, 100)


async def test_offer_rejects_unheld_card(conn: AsyncConnection) -> None:
    await bootstrap_user(conn, _A)
    await bootstrap_user(conn, _B)
    with pytest.raises(TradeError, match="don't hold"):
        await create_offer(conn, _A, _B, ["card_nort"], [], 0, 0, 100)


async def test_accept_gates(conn: AsyncConnection) -> None:
    await bootstrap_user(conn, _A)
    await bootstrap_user(conn, _B)
    await bootstrap_user(conn, _C)
    await _hold(conn, _A, "card_nort")
    tid = await create_offer(conn, _A, _B, ["card_nort"], [], 0, 0, 100)
    with pytest.raises(TradeError, match="counterparty"):
        await accept(conn, tid, _A, 100)  # proposer can't self-accept
    with pytest.raises(TradeError, match="counterparty"):
        await accept(conn, tid, _C, 100)  # stranger can't accept
    with pytest.raises(TradeError, match="expired"):
        await accept(conn, tid, _B, 2000)  # past expires_tick (100+1440)


async def test_decline_and_cancel(conn: AsyncConnection) -> None:
    await bootstrap_user(conn, _A)
    await bootstrap_user(conn, _B)
    await _hold(conn, _A, "card_nort")
    t1 = await create_offer(conn, _A, _B, ["card_nort"], [], 0, 0, 100)
    t2 = await create_offer(conn, _A, _B, ["card_nort"], [], 0, 0, 100)
    # same card twice is fine: nothing is escrowed until accept
    with pytest.raises(TradeError, match="proposer"):
        await cancel(conn, t1, _B)
    with pytest.raises(TradeError, match="counterparty"):
        await decline(conn, t1, _A)
    await decline(conn, t1, _B)
    await cancel(conn, t2, _A)
    assert (await get_trade(conn, t1)).status == "DECLINED"
    assert (await get_trade(conn, t2)).status == "CANCELLED"
    # resolved trades can't resolve twice
    with pytest.raises(TradeError, match="already"):
        await cancel(conn, t1, _A)


async def test_stale_binder_voids_accept(conn: AsyncConnection) -> None:
    """The offered card left the binder between offer and accept."""
    await bootstrap_user(conn, _A)
    await bootstrap_user(conn, _B)
    await _hold(conn, _A, "card_nort")
    tid = await create_offer(conn, _A, _B, ["card_nort"], [], 0, 0, 100)
    async with conn.cursor() as cur:
        await cur.execute(
            "DELETE FROM user_cards WHERE user_id = %s AND card_key = 'card_nort'",
            (_A,),
        )
    with pytest.raises(TradeError, match="no longer held"):
        await accept(conn, tid, _B, 110)
    assert (await get_trade(conn, tid)).status == "OPEN"  # unchanged


async def test_featured_card_unpins_on_trade(conn: AsyncConnection) -> None:
    await bootstrap_user(conn, _A)
    await bootstrap_user(conn, _B)
    await _hold(conn, _A, "card_nort")
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE users SET featured_card = 'card_nort' WHERE id = %s", (_A,)
        )
    tid = await create_offer(conn, _A, _B, ["card_nort"], [], 0, 0, 100)
    await accept(conn, tid, _B, 110)
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT featured_card FROM users WHERE id = %s", (_A,)
        )
        assert (await cur.fetchone())[0] is None


async def test_list_trades_shows_open_only(conn: AsyncConnection) -> None:
    await bootstrap_user(conn, _A)
    await bootstrap_user(conn, _B)
    await _hold(conn, _A, "card_nort")
    t1 = await create_offer(conn, _A, _B, ["card_nort"], [], 0, 0, 100)
    await cancel(conn, t1, _A)
    t2 = await create_offer(conn, _A, _B, ["card_nort"], [], 0, 0, 100)
    rows = await list_trades(conn, _A)
    assert [r.id for r in rows] == [t2]
    rows_b = await list_trades(conn, _B)
    assert [r.id for r in rows_b] == [t2]  # visible to both parties
