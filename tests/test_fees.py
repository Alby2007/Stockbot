"""Plan E: maker rebate on book crosses and volume-tiered taker fees.

Cross legs settle through `_apply_fill` with maker_taker tags: the maker
pays zero fee and receives `fee.maker_rebate_bps` of the notional funded
out of the taker's fee (SINK keeps the rest). All user fills accrue
`users.total_traded_minor`, which prices the NEXT fill's taker rate via
the fee.tier* thresholds. MM fills are untouched: flat tier fee, no
rebate.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from psycopg import AsyncConnection
from psycopg.rows import dict_row

from stockbot.accounts.service import bootstrap_user
from stockbot.ledger.service import (
    get_system_account_id,
    get_user_account_id,
    post_transfer,
)
from stockbot.market import engine
from stockbot.orders.service import match_orders, place_order
from stockbot.shorts.service import open_bounded_short
from stockbot.trading.service import execute_trade


async def _liquid_ticker(conn: AsyncConnection) -> str:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT ticker FROM instruments WHERE is_active AND kind != 'INDEX' "
            "ORDER BY liquidity DESC LIMIT 1"
        )
        row = await cur.fetchone()
        assert row is not None
        return str(row[0])


async def _quoted(conn: AsyncConnection, ticker: str) -> Decimal:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT quoted_price FROM instruments WHERE ticker = %s", (ticker,)
        )
        row = await cur.fetchone()
        assert row is not None
        return Decimal(row[0])


async def _fund(conn: AsyncConnection, user_id: int, amount_minor: int) -> None:
    faucet = await get_system_account_id(conn, "FAUCET")
    account = await get_user_account_id(conn, user_id)
    await post_transfer(
        conn,
        from_account_id=faucet,
        to_account_id=account,
        amount=amount_minor,
        reason="TEST_FUND",
    )


async def _set_config(conn: AsyncConnection, key: str, value: float) -> None:
    async with conn.cursor() as cur:
        await cur.execute("UPDATE config SET value = %s WHERE key = %s", (value, key))


async def _total_traded(conn: AsyncConnection, user_id: int) -> int:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT total_traded_minor FROM users WHERE id = %s", (user_id,)
        )
        row = await cur.fetchone()
        assert row is not None
        return int(row[0])


async def _latest_trade(conn: AsyncConnection) -> dict:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            SELECT id, user_id, notional_minor, fee_minor, maker_rebate_minor,
                   maker_taker, order_id
            FROM trades ORDER BY id DESC LIMIT 1
            """
        )
        row = await cur.fetchone()
        assert row is not None
        return dict(row)


async def _ledger_amounts(
    conn: AsyncConnection, account_id: int, reason: str
) -> list[int]:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT amount FROM ledger_entries "
            "WHERE account_id = %s AND reason = %s",
            (account_id, reason),
        )
        return [int(r[0]) for r in await cur.fetchall()]


def _grid(price: Decimal) -> Decimal:
    """Snap a price to the tick grid `place_order` applies at placement."""
    tick = Decimal(
        str(
            engine.tick_size(
                float(price), {"spread.tick_pct": 0.001, "spread.tick_min": 0.01}
            )
        )
    )
    return (price / tick).to_integral_value() * tick


async def test_maker_rebate_funded_from_taker_fee(conn: AsyncConnection) -> None:
    """A resting ask is the maker: pays no fee and collects the rebate
    out of the taker's 10bps -- SINK keeps the remainder."""
    await bootstrap_user(conn, 7001)
    await bootstrap_user(conn, 7002)
    ticker = await _liquid_ticker(conn)
    await _fund(conn, 7001, 10_000_000)
    await execute_trade(conn, user_id=7001, ticker=ticker, side="BUY", quantity=10)
    mark = await _quoted(conn, ticker)
    await _fund(conn, 7002, 10_000_000)
    cross = _grid(mark * Decimal("1.01"))

    ask = await place_order(
        conn, user_id=7001, ticker=ticker, side="SELL", quantity=10,
        limit_price=cross,
    )
    bid = await place_order(
        conn, user_id=7002, ticker=ticker, side="BUY", quantity=10,
        limit_price=cross,
    )
    assert await match_orders(conn, 1) == 1

    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            SELECT order_id, user_id, notional_minor, fee_minor,
                   maker_rebate_minor, maker_taker, fee_transfer_id
            FROM trades WHERE order_id IN (%s, %s)
            """,
            (ask.order_id, bid.order_id),
        )
        by_order = {int(r["order_id"]): dict(r) for r in await cur.fetchall()}
    ask_t, bid_t = by_order[ask.order_id], by_order[bid.order_id]

    assert ask_t["maker_taker"] == "MAKER"
    assert int(ask_t["fee_minor"]) == 0
    assert int(ask_t["maker_rebate_minor"]) == 0

    assert bid_t["maker_taker"] == "TAKER"
    notional = Decimal(int(bid_t["notional_minor"])) / 100
    expected_fee = int(
        (notional * Decimal("10") / 10_000 * 100).quantize(
            Decimal("1"), ROUND_HALF_UP
        )
    )
    expected_rebate = int(
        (notional * Decimal("2") / 10_000 * 100).quantize(
            Decimal("1"), ROUND_HALF_UP
        )
    )
    assert int(bid_t["fee_minor"]) == expected_fee
    assert int(bid_t["maker_rebate_minor"]) == expected_rebate > 0

    # Ledger: maker account credited exactly the rebate; the SINK leg is
    # fee minus rebate.
    maker_acct = await get_user_account_id(conn, 7001)
    assert await _ledger_amounts(conn, maker_acct, "MAKER_REBATE") == [
        expected_rebate
    ]
    sink_id = await get_system_account_id(conn, "SINK")
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT amount FROM ledger_entries "
            "WHERE transfer_id = %s AND account_id = %s",
            (bid_t["fee_transfer_id"], sink_id),
        )
        (sink_got,) = await cur.fetchone()
    assert int(sink_got) == expected_fee - expected_rebate


async def test_mm_fill_charges_tier_fee_no_rebate(conn: AsyncConnection) -> None:
    """A market buy vs the MM: flat tier fee to SINK, no rebate leg,
    maker_taker NULL."""
    await bootstrap_user(conn, 7003)
    await _fund(conn, 7003, 10_000_000)
    ticker = await _liquid_ticker(conn)
    await execute_trade(conn, user_id=7003, ticker=ticker, side="BUY", quantity=5)

    t = await _latest_trade(conn)
    assert t["maker_taker"] is None
    assert int(t["maker_rebate_minor"]) == 0
    acct = await get_user_account_id(conn, 7003)
    assert await _ledger_amounts(conn, acct, "MAKER_REBATE") == []


async def test_volume_tier_lowers_taker_fee(conn: AsyncConnection) -> None:
    """Identical fills before/after crossing tier1_volume: 10bps -> 8bps."""
    await bootstrap_user(conn, 7004)
    await _fund(conn, 7004, 10_000_000)
    ticker = await _liquid_ticker(conn)

    await execute_trade(conn, user_id=7004, ticker=ticker, side="BUY", quantity=2)
    t0 = await _latest_trade(conn)
    # fee quantizes to cents first -- compare against the exact expected
    # amount rather than a ratio on small notionals.
    n0 = Decimal(int(t0["notional_minor"])) / 100
    expected0 = int(
        (n0 * Decimal("10") / 10_000 * 100).quantize(Decimal("1"), ROUND_HALF_UP)
    )
    assert int(t0["fee_minor"]) == expected0

    # Push the user over the tier1 threshold; the NEXT fill prices at 8bps.
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE users SET total_traded_minor = "
            "(SELECT value::bigint FROM config WHERE key = 'fee.tier1_volume') "
            "WHERE id = %s",
            (7004,),
        )
    await execute_trade(conn, user_id=7004, ticker=ticker, side="BUY", quantity=2)
    t1 = await _latest_trade(conn)
    n1 = Decimal(int(t1["notional_minor"])) / 100
    expected1 = int(
        (n1 * Decimal("8") / 10_000 * 100).quantize(Decimal("1"), ROUND_HALF_UP)
    )
    assert int(t1["fee_minor"]) == expected1


async def test_rebate_clamped_to_taker_fee(conn: AsyncConnection) -> None:
    """maker_rebate_bps above the taker's rate rebates the WHOLE fee --
    the maker can never be paid more than was collected."""
    await _set_config(conn, "fee.maker_rebate_bps", 50)
    await bootstrap_user(conn, 7005)
    await bootstrap_user(conn, 7006)
    ticker = await _liquid_ticker(conn)
    await _fund(conn, 7005, 10_000_000)
    await execute_trade(conn, user_id=7005, ticker=ticker, side="BUY", quantity=10)
    mark = await _quoted(conn, ticker)
    await _fund(conn, 7006, 10_000_000)
    cross = _grid(mark * Decimal("1.01"))

    await place_order(
        conn, user_id=7005, ticker=ticker, side="SELL", quantity=10,
        limit_price=cross,
    )
    bid = await place_order(
        conn, user_id=7006, ticker=ticker, side="BUY", quantity=10,
        limit_price=cross,
    )
    assert await match_orders(conn, 1) == 1

    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT fee_minor, maker_rebate_minor, fee_transfer_id "
            "FROM trades WHERE order_id = %s",
            (bid.order_id,),
        )
        t = await cur.fetchone()
    assert int(t["maker_rebate_minor"]) == int(t["fee_minor"]) > 0
    assert t["fee_transfer_id"] is None  # nothing left for SINK


async def test_fills_accrue_lifetime_volume(conn: AsyncConnection) -> None:
    """Shares and bounded-short opens both count toward the tier volume."""
    await bootstrap_user(conn, 7007)
    await _fund(conn, 7007, 10_000_000)
    ticker = await _liquid_ticker(conn)

    before = await _total_traded(conn, 7007)
    await execute_trade(conn, user_id=7007, ticker=ticker, side="BUY", quantity=3)
    after_buy = await _total_traded(conn, 7007)
    assert after_buy > before

    result = await open_bounded_short(
        conn, user_id=7007, ticker=ticker, quantity=2
    )
    after_short = await _total_traded(conn, 7007)
    assert after_short > after_buy
    # The accrued delta is the short's entry notional.
    expected = int(
        (result.entry_price * result.quantity * 100).quantize(Decimal("1"))
    )
    assert after_short - after_buy == expected
