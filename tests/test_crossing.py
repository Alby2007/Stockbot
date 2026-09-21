"""Crossing book: orders cross each other at the maker's price before
falling through to the market maker. Covers price-time priority, partial
fills, self-match, the price collar, ledger conservation across a cross,
and the mark print."""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from psycopg import AsyncConnection
from psycopg.rows import dict_row

from stockbot.accounts.service import bootstrap_user
from stockbot.ledger.service import (
    get_balance,
    get_system_account_id,
    get_user_account_id,
    post_transfer,
)
from stockbot.orders.service import match_orders, place_order
from stockbot.trading.service import execute_trade


async def _instrument(conn: AsyncConnection, ticker: str) -> dict:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT id, quoted_price, base_price, impact FROM instruments "
            "WHERE ticker = %s",
            (ticker,),
        )
        row = await cur.fetchone()
        assert row is not None
        return dict(row)


async def _liquid_ticker(conn: AsyncConnection) -> str:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT ticker FROM instruments WHERE is_active AND kind != 'INDEX' "
            "ORDER BY liquidity DESC LIMIT 1"
        )
        row = await cur.fetchone()
        assert row is not None
        return str(row[0])


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


async def _give_shares(
    conn: AsyncConnection, user_id: int, ticker: str, quantity: int
) -> Decimal:
    """Buy `quantity` shares at market; returns the post-trade mark so the
    caller can place orders around the moved price."""
    await _fund(conn, user_id, 10_000_000)  # $100k -- always enough
    await execute_trade(conn, user_id=user_id, ticker=ticker, side="BUY",
                        quantity=quantity)
    return await _quoted(conn, ticker)


async def _position_qty(conn: AsyncConnection, user_id: int, ticker: str) -> int:
    async with conn.cursor() as cur:
        await cur.execute(
            """
            SELECT p.quantity FROM positions p
            JOIN instruments i ON i.id = p.instrument_id
            WHERE p.user_id = %s AND i.ticker = %s AND p.season_id IS NULL
            """,
            (user_id, ticker),
        )
        row = await cur.fetchone()
        return int(row[0]) if row else 0


async def _order_row(conn: AsyncConnection, order_id: int) -> dict:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT status, filled_quantity, fill_price FROM orders WHERE id = %s",
            (order_id,),
        )
        row = await cur.fetchone()
        assert row is not None
        return dict(row)


async def _trades_for_order(conn: AsyncConnection, order_id: int) -> list[dict]:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            SELECT user_id, side, quantity, fill_price, maker_taker,
                   counterparty_user_id, order_id
            FROM trades WHERE order_id = %s
            """,
            (order_id,),
        )
        return [dict(r) for r in await cur.fetchall()]


async def _quoted(conn: AsyncConnection, ticker: str) -> Decimal:
    return Decimal((await _instrument(conn, ticker))["quoted_price"])


def _minor(dollars: Decimal) -> int:
    return int((dollars * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


async def test_cross_at_maker_price_and_mark_print(conn: AsyncConnection) -> None:
    """An ask resting first is the maker: the pair trades at the ask's
    price and the mark moves to it."""
    await bootstrap_user(conn, 4001)
    await bootstrap_user(conn, 4002)
    ticker = await _liquid_ticker(conn)
    mark = await _give_shares(conn, 4001, ticker, 2)
    await _fund(conn, 4002, 10_000_000)
    cross = (mark * Decimal("1.01")).quantize(Decimal("0.000001"))

    ask = await place_order(
        conn, user_id=4001, ticker=ticker, side="SELL", quantity=2,
        limit_price=cross,
    )
    bid = await place_order(
        conn, user_id=4002, ticker=ticker, side="BUY", quantity=2,
        limit_price=cross,
    )
    fills = await match_orders(conn, 1)

    assert fills == 1
    assert await _position_qty(conn, 4001, ticker) == 0
    assert await _position_qty(conn, 4002, ticker) == 2
    for order_id in (ask.order_id, bid.order_id):
        row = await _order_row(conn, order_id)
        assert row["status"] == "FILLED"
        assert row["filled_quantity"] == 2
        assert Decimal(row["fill_price"]) == cross

    # The cross printed the mark.
    assert abs(await _quoted(conn, ticker) - cross) < Decimal("0.01")

    ask_trades = await _trades_for_order(conn, ask.order_id)
    bid_trades = await _trades_for_order(conn, bid.order_id)
    assert len(ask_trades) == len(bid_trades) == 1
    assert ask_trades[0]["maker_taker"] == "MAKER"
    assert ask_trades[0]["counterparty_user_id"] == 4002
    assert ask_trades[0]["side"] == "SELL"
    assert bid_trades[0]["maker_taker"] == "TAKER"
    assert bid_trades[0]["counterparty_user_id"] == 4001
    assert bid_trades[0]["side"] == "BUY"


async def test_taker_gets_price_improvement(conn: AsyncConnection) -> None:
    """The resting bid is the maker: a crossing ask executes at the bid's
    higher price, not its own lower limit."""
    await bootstrap_user(conn, 4003)
    await bootstrap_user(conn, 4004)
    ticker = await _liquid_ticker(conn)
    mark = await _give_shares(conn, 4004, ticker, 1)
    await _fund(conn, 4003, 10_000_000)
    bid_px = (mark * Decimal("1.015")).quantize(Decimal("0.000001"))

    bid = await place_order(
        conn, user_id=4003, ticker=ticker, side="BUY", quantity=1,
        limit_price=bid_px,
    )
    ask = await place_order(
        conn, user_id=4004, ticker=ticker, side="SELL", quantity=1,
        limit_price=mark,  # willing to sell lower; crosses at bid's price
    )
    fills = await match_orders(conn, 1)

    assert fills == 1
    row = await _order_row(conn, ask.order_id)
    assert Decimal(row["fill_price"]) == bid_px
    bid_trades = await _trades_for_order(conn, bid.order_id)
    assert bid_trades[0]["maker_taker"] == "MAKER"


async def test_partial_fill_leaves_remainder_open(conn: AsyncConnection) -> None:
    await bootstrap_user(conn, 4005)
    await bootstrap_user(conn, 4006)
    ticker = await _liquid_ticker(conn)
    mark = await _give_shares(conn, 4005, ticker, 3)
    await _fund(conn, 4006, 10_000_000)

    ask = await place_order(
        conn, user_id=4005, ticker=ticker, side="SELL", quantity=3,
        limit_price=mark,
    )
    bid = await place_order(
        conn, user_id=4006, ticker=ticker, side="BUY", quantity=7,
        limit_price=mark,
    )
    fills = await match_orders(conn, 1)

    assert fills == 1
    ask_row = await _order_row(conn, ask.order_id)
    bid_row = await _order_row(conn, bid.order_id)
    assert ask_row["status"] == "FILLED" and ask_row["filled_quantity"] == 3
    assert bid_row["status"] == "OPEN" and bid_row["filled_quantity"] == 3
    assert await _position_qty(conn, 4006, ticker) == 3


async def test_self_match_never_crosses(conn: AsyncConnection) -> None:
    """A user's own bid and ask can't trade with each other."""
    await bootstrap_user(conn, 4007)
    ticker = await _liquid_ticker(conn)
    mark = await _give_shares(conn, 4007, ticker, 1)
    await _fund(conn, 4007, 10_000_000)

    ask = await place_order(
        conn, user_id=4007, ticker=ticker, side="SELL", quantity=1,
        limit_price=mark,
    )
    bid = await place_order(
        conn, user_id=4007, ticker=ticker, side="BUY", quantity=1,
        limit_price=mark,
    )
    fills = await match_orders(conn, 1)

    assert fills == 0
    assert (await _order_row(conn, ask.order_id))["status"] == "OPEN"
    assert (await _order_row(conn, bid.order_id))["status"] == "OPEN"
    assert await _position_qty(conn, 4007, ticker) == 1  # still holding


async def test_collar_skips_stale_maker_but_book_still_crosses(
    conn: AsyncConnection,
) -> None:
    """A maker priced far off the mark can't print; a sane pair behind it
    still crosses."""
    await bootstrap_user(conn, 4008)
    await bootstrap_user(conn, 4009)
    await bootstrap_user(conn, 4010)
    ticker = await _liquid_ticker(conn)
    mark = await _give_shares(conn, 4009, ticker, 1)
    await _fund(conn, 4008, 10_000_000)
    await _fund(conn, 4010, 10_000_000)
    sane = (mark * Decimal("1.01")).quantize(Decimal("0.000001"))

    stale_bid = await place_order(
        conn, user_id=4008, ticker=ticker, side="BUY", quantity=1,
        limit_price=mark * 2,  # 100% over mark -- outside the 2% collar
    )
    ask = await place_order(
        conn, user_id=4009, ticker=ticker, side="SELL", quantity=1,
        limit_price=mark,
    )
    good_bid = await place_order(
        conn, user_id=4010, ticker=ticker, side="BUY", quantity=1,
        limit_price=sane,
    )
    fills = await match_orders(conn, 1)

    # One book cross (ask x good_bid) + the stale bid falls through to the
    # market maker -- the collar only gates book prints; an MM fill at the
    # mark is a great deal for a bid priced at 2x.
    assert fills == 2
    stale_row = await _order_row(conn, stale_bid.order_id)
    assert stale_row["status"] == "FILLED"
    assert Decimal(stale_row["fill_price"]) < mark * Decimal("1.05")
    # The ask rested before good_bid -> maker -> cross at the ask's price.
    ask_row = await _order_row(conn, ask.order_id)
    assert ask_row["status"] == "FILLED"
    assert Decimal(ask_row["fill_price"]) == mark
    assert (await _order_row(conn, good_bid.order_id))["status"] == "FILLED"


async def test_cross_moves_cash_and_fees_without_market_maker(
    conn: AsyncConnection,
) -> None:
    """The notional moves buyer -> seller directly; both fees go to SINK;
    MARKET_MAKER is untouched and the ledger still sums to zero."""
    await bootstrap_user(conn, 4011)
    await bootstrap_user(conn, 4012)
    ticker = await _liquid_ticker(conn)
    mark = await _give_shares(conn, 4011, ticker, 2)
    await _fund(conn, 4012, 10_000_000)
    cross = (mark * Decimal("1.005")).quantize(Decimal("0.000001"))

    mm_id = await get_system_account_id(conn, "MARKET_MAKER")
    sink_id = await get_system_account_id(conn, "SINK")
    mm_before = await get_balance(conn, mm_id)
    sink_before = await get_balance(conn, sink_id)
    buyer_account = await get_user_account_id(conn, 4012)
    seller_account = await get_user_account_id(conn, 4011)
    buyer_before = await get_balance(conn, buyer_account)
    seller_before = await get_balance(conn, seller_account)

    await place_order(
        conn, user_id=4011, ticker=ticker, side="SELL", quantity=2,
        limit_price=cross,
    )
    await place_order(
        conn, user_id=4012, ticker=ticker, side="BUY", quantity=2,
        limit_price=cross,
    )
    fills = await match_orders(conn, 1)
    assert fills == 1

    notional = _minor(cross * 2)
    fee = _minor(cross * 2 * Decimal("0.001"))
    assert await get_balance(conn, mm_id) == mm_before
    # Buyer paid notional + fee; seller received notional - fee.
    assert await get_balance(conn, buyer_account) == buyer_before - notional - fee
    assert await get_balance(conn, seller_account) == seller_before + notional - fee
    assert await get_balance(conn, sink_id) == sink_before + 2 * fee

    async with conn.cursor() as cur:
        await cur.execute("SELECT COALESCE(SUM(amount), 0) FROM ledger_entries")
        row = await cur.fetchone()
        assert row is not None and int(row[0]) == 0


async def test_uncrossed_remainder_falls_through_to_market_maker(
    conn: AsyncConnection,
) -> None:
    """A bid that partially crosses keeps going to the MM for the rest if
    its limit still allows the impact-adjusted price."""
    await bootstrap_user(conn, 4013)
    await bootstrap_user(conn, 4014)
    ticker = await _liquid_ticker(conn)
    mark = await _give_shares(conn, 4013, ticker, 2)
    await _fund(conn, 4014, 10_000_000)

    await place_order(  # maker: rests first
        conn, user_id=4013, ticker=ticker, side="SELL", quantity=2,
        limit_price=mark,
    )
    bid = await place_order(  # taker: limit has headroom for MM spread
        conn, user_id=4014, ticker=ticker, side="BUY", quantity=5,
        limit_price=(mark * Decimal("1.019")).quantize(Decimal("0.000001")),
    )
    fills = await match_orders(conn, 1)

    # One cross (2 shares) + one MM fill (3 shares).
    assert fills == 2
    bid_row = await _order_row(conn, bid.order_id)
    assert bid_row["status"] == "FILLED" and bid_row["filled_quantity"] == 5
    assert await _position_qty(conn, 4014, ticker) == 5


async def test_cross_scoped_by_season(conn: AsyncConnection) -> None:
    """Main-portfolio and league orders never cross each other."""
    from stockbot.seasons.service import create_season, join_season, on_tick

    await bootstrap_user(conn, 4015)
    await bootstrap_user(conn, 4016)
    ticker = await _liquid_ticker(conn)
    mark = await _quoted(conn, ticker)
    season_id = await create_season(conn, name="x-scope", start_tick=0, end_tick=100)
    await on_tick(conn, 0)  # activate so league orders are accepted
    await join_season(conn, 4015, season_id)

    ask = await place_order(
        conn, user_id=4015, ticker=ticker, side="SELL", quantity=1,
        limit_price=mark, season_id=season_id,
    )
    bid = await place_order(
        conn, user_id=4016, ticker=ticker, side="BUY", quantity=1,
        limit_price=mark,
    )
    fills = await match_orders(conn, 1)

    assert fills == 0
    assert (await _order_row(conn, ask.order_id))["status"] == "OPEN"
    assert (await _order_row(conn, bid.order_id))["status"] == "OPEN"


async def test_cross_collar_anchors_to_tick_open_mark(conn: AsyncConnection) -> None:
    """Anti-ratchet: the cross collar compares to the tick-open mark, not
    the live mark -- a chain of colluding pairs can't step the price
    collar-width at a time within one tick."""
    for uid in (4017, 4018, 4019, 4020):
        await bootstrap_user(conn, uid)
    ticker = await _liquid_ticker(conn)
    await _give_shares(conn, 4017, ticker, 1)
    mark = await _give_shares(conn, 4019, ticker, 1)
    for uid in (4018, 4020):
        await _fund(conn, uid, 10_000_000)

    step1 = (mark * Decimal("1.019")).quantize(Decimal("0.000001"))
    step2 = (mark * Decimal("1.039")).quantize(Decimal("0.000001"))
    # Pair 1 crosses inside the collar and prints the mark at step1.
    await place_order(
        conn, user_id=4017, ticker=ticker, side="SELL", quantity=1,
        limit_price=step1,
    )
    # Pair 2's maker price is one more collar-width above the *live* mark
    # after pair 1 (|step2-step1|/step1 < 2%) but far outside the collar of
    # the tick-open mark -- it must not print.
    ask2 = await place_order(
        conn, user_id=4019, ticker=ticker, side="SELL", quantity=1,
        limit_price=step2,
    )
    await place_order(
        conn, user_id=4018, ticker=ticker, side="BUY", quantity=1,
        limit_price=mark * Decimal("1.05"),
    )
    await place_order(
        conn, user_id=4020, ticker=ticker, side="BUY", quantity=1,
        limit_price=mark * Decimal("1.06"),
    )

    await match_orders(conn, 1)

    # The mark printed at step1 and could not ratchet to step2.
    new_mark = await _quoted(conn, ticker)
    assert new_mark < mark * Decimal("1.035")
    assert (await _order_row(conn, ask2.order_id))["status"] == "OPEN"
