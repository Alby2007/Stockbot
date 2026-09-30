"""Props: parimutuel escrow, pool accounting, settlement splits,
void/refund paths, autogen."""

import pytest
from psycopg import AsyncConnection

from stockbot.accounts.service import STARTING_GRANT, bootstrap_user
from stockbot.ledger.service import (
    get_balance,
    get_system_account_id,
    get_user_account_id,
)
from stockbot.props import service as props
from stockbot.props.errors import PropBetError, PropStateError


async def _stock(conn: AsyncConnection, n: int = 0) -> tuple[int, str, float]:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT id, ticker, quoted_price FROM instruments "
            "WHERE kind = 'STOCK' AND is_active AND quoted_price IS NOT NULL "
            "ORDER BY id OFFSET %s LIMIT 1",
            (n,),
        )
        row = await cur.fetchone()
    return int(row[0]), str(row[1]), float(row[2])


async def test_bet_escrows_and_pools(conn: AsyncConnection) -> None:
    await bootstrap_user(conn, 4101)
    iid, _, _ = await _stock(conn)
    prop_id = await props.create(
        conn,
        title="t",
        kind="PRICE_ABOVE",
        instrument_id=iid,
        threshold=1.0,
        resolve_tick=100,
        tick_index=0,
        feed_post=False,
    )
    escrow_id = await get_system_account_id(conn, "GAME_ESCROW")
    esc0 = await get_balance(conn, escrow_id)

    total = await props.bet(conn, 4101, prop_id, "yes", 1000, tick_index=0)

    assert total == 1000
    assert await get_balance(conn, await get_user_account_id(conn, 4101)) == (
        STARTING_GRANT - 1000
    )
    assert await get_balance(conn, escrow_id) == esc0 + 1000
    prop = await props.get_prop(conn, prop_id)
    assert prop is not None and prop.pool_yes_minor == 1000

    # Same-side bets aggregate.
    total = await props.bet(conn, 4101, prop_id, "YES", 500, tick_index=0)
    assert total == 1500
    prop = await props.get_prop(conn, prop_id)
    assert prop is not None and prop.pool_yes_minor == 1500


async def test_bet_bounds(conn: AsyncConnection) -> None:
    await bootstrap_user(conn, 4110)
    iid, _, _ = await _stock(conn)
    prop_id = await props.create(
        conn,
        title="t",
        kind="PRICE_ABOVE",
        instrument_id=iid,
        threshold=1.0,
        resolve_tick=100,
        tick_index=0,
        feed_post=False,
    )
    with pytest.raises(PropBetError):
        await props.bet(conn, 4110, prop_id, "yes", 10, tick_index=0)  # under min
    with pytest.raises(PropBetError):
        await props.bet(conn, 4110, prop_id, "sideways", 1000, tick_index=0)
    # Closed to new bets at/past resolve_tick.
    with pytest.raises(PropStateError):
        await props.bet(conn, 4110, prop_id, "yes", 1000, tick_index=100)


async def test_settle_pays_winners_parimutuel(conn: AsyncConnection) -> None:
    """YES $10 vs NO $30 -> YES wins: pool $40 - 5% rake split to YES."""
    await bootstrap_user(conn, 4120)
    await bootstrap_user(conn, 4121)
    iid, _, price = await _stock(conn)
    prop_id = await props.create(
        conn,
        title="t",
        kind="PRICE_ABOVE",
        instrument_id=iid,
        threshold=price * 0.5,  # already above -> resolves YES at settle
        resolve_tick=100,
        tick_index=0,
        feed_post=False,
    )
    await props.bet(conn, 4120, prop_id, "YES", 1000, tick_index=0)
    await props.bet(conn, 4121, prop_id, "NO", 3000, tick_index=0)
    sink_id = await get_system_account_id(conn, "SINK")
    sink0 = await get_balance(conn, sink_id)

    settled = await props.settle_due(conn, 100)

    assert settled == 1
    # Winner takes pool(4000) - rake(200) = 3800 (single winner, no dust).
    assert await get_balance(conn, await get_user_account_id(conn, 4120)) == (
        STARTING_GRANT - 1000 + 3800
    )
    assert await get_balance(conn, await get_user_account_id(conn, 4121)) == (
        STARTING_GRANT - 3000
    )
    assert await get_balance(conn, sink_id) == sink0 + 200
    prop = await props.get_prop(conn, prop_id)
    assert prop is not None and prop.status == "RESOLVED" and prop.outcome == "YES"
    # Idempotent: the second sweep finds nothing due.
    assert await props.settle_due(conn, 200) == 0


async def test_settle_no_winning_side_refunds(conn: AsyncConnection) -> None:
    await bootstrap_user(conn, 4130)
    iid, _, price = await _stock(conn)
    prop_id = await props.create(
        conn,
        title="t",
        kind="PRICE_ABOVE",
        instrument_id=iid,
        threshold=price * 0.5,  # resolves YES, but nobody bet YES
        resolve_tick=100,
        tick_index=0,
        feed_post=False,
    )
    await props.bet(conn, 4130, prop_id, "NO", 2000, tick_index=0)

    await props.settle_due(conn, 100)

    assert await get_balance(conn, await get_user_account_id(conn, 4130)) == STARTING_GRANT


async def test_index_beat_uses_start_marks(conn: AsyncConnection) -> None:
    """A INDEX_BEAT prop compares returns from creation marks -- hand-set
    the end marks so A outperforms B."""
    await bootstrap_user(conn, 4140)
    a_id, _, _ = await _stock(conn, 0)
    b_id, _, _ = await _stock(conn, 1)
    prop_id = await props.create(
        conn,
        title="t",
        kind="INDEX_BEAT",
        instrument_id=a_id,
        instrument_b_id=b_id,
        resolve_tick=100,
        tick_index=0,
        feed_post=False,
    )
    prop = await props.get_prop(conn, prop_id)
    assert prop is not None
    start_a, start_b = prop.meta["start_a"], prop.meta["start_b"]
    # A doubles, B halves -> YES.
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE instruments SET quoted_price = %s WHERE id = %s",
            (start_a * 2, a_id),
        )
        await cur.execute(
            "UPDATE instruments SET quoted_price = %s WHERE id = %s",
            (start_b * 0.5, b_id),
        )
    await props.bet(conn, 4140, prop_id, "YES", 1000, tick_index=0)

    await props.settle_due(conn, 100)

    prop = await props.get_prop(conn, prop_id)
    assert prop is not None and prop.outcome == "YES"
    # Single winner takes the whole pool minus rake: 1000 * 0.95.
    assert await get_balance(conn, await get_user_account_id(conn, 4140)) == (
        STARTING_GRANT - 1000 + 950
    )


async def test_autogen_makes_weekly_props(conn: AsyncConnection) -> None:
    made = await props.autogen(conn, 7 * 1440)  # week boundary
    assert made >= 1
    open_props = await props.list_open(conn)
    assert any(p.auto for p in open_props)
    # Wrong tick: nothing.
    assert await props.autogen(conn, 7 * 1440 + 5) == 0
    assert await props.autogen(conn, 8 * 1440) == 0  # day boundary, not week
