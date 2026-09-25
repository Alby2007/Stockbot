"""Phase I2: real book depth on /stock.

The displayed book must aggregate resting orders by the same rules the
matcher uses (LIMIT + triggered only, main scope), best price first,
then pad outward with synthetic MM rungs.
"""

from __future__ import annotations

from decimal import Decimal

from psycopg import AsyncConnection

from stockbot.accounts.service import bootstrap_user
from stockbot.ledger.service import get_system_account_id, post_transfer
from stockbot.market.data import book_depth
from stockbot.orders.service import place_order


async def _fund(conn: AsyncConnection, user_id: int, amount_minor: int) -> int:
    account_id = (await bootstrap_user(conn, user_id)).account_id
    faucet = await get_system_account_id(conn, "FAUCET")
    await post_transfer(
        conn,
        from_account_id=faucet,
        to_account_id=account_id,
        amount=amount_minor,
        reason="TEST_TOPUP",
    )
    return account_id


async def _first_stock(conn: AsyncConnection) -> tuple[int, str, Decimal]:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT id, ticker, quoted_price FROM instruments "
            "WHERE is_active AND kind = 'STOCK' ORDER BY id LIMIT 1"
        )
        row = await cur.fetchone()
    return int(row[0]), str(row[1]), Decimal(row[2])


async def test_book_depth_aggregates_and_pads(conn: AsyncConnection) -> None:
    iid, ticker, mark = await _first_stock(conn)
    await _fund(conn, 6101, 10_000_000_00)
    await _fund(conn, 6102, 10_000_000_00)

    bid_px = mark * Decimal("0.5")  # deep, won't fill
    ask_px = mark * Decimal("2.0")
    await place_order(conn, user_id=6101, ticker=ticker, side="BUY",
                      quantity=2, limit_price=bid_px)
    await place_order(conn, user_id=6102, ticker=ticker, side="BUY",
                      quantity=3, limit_price=bid_px)
    await place_order(conn, user_id=6101, ticker=ticker, side="SELL",
                      quantity=4, limit_price=ask_px)

    bids, asks = await book_depth(conn, iid, levels=5)

    # One real bid level aggregating both users, one real ask level.
    real_bids = [lvl for lvl in bids if not lvl.synthetic]
    real_asks = [lvl for lvl in asks if not lvl.synthetic]
    assert len(real_bids) == 1 and real_bids[0].quantity == 5
    assert len(real_asks) == 1 and real_asks[0].quantity == 4

    # Padded out to `levels` rungs with synthetic MM depth.
    assert len(bids) == 5 and len(asks) == 5
    assert all(lvl.synthetic for lvl in bids[1:])
    assert all(lvl.synthetic for lvl in asks[1:])

    # Bids strictly descend from the mark, asks strictly ascend.
    bid_px_f = [float(lvl.price) for lvl in bids]
    ask_px_f = [float(lvl.price) for lvl in asks]
    assert bid_px_f == sorted(bid_px_f, reverse=True)
    assert ask_px_f == sorted(ask_px_f)
    assert all(p < float(mark) for p in bid_px_f)
    assert all(p > float(mark) for p in ask_px_f)


async def test_book_depth_empty_book_is_all_mm(conn: AsyncConnection) -> None:
    iid, _ticker, mark = await _first_stock(conn)
    # Clear any resting orders on this instrument for a clean view.
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE orders SET status = 'CANCELLED' "
            "WHERE instrument_id = %s AND status = 'OPEN'",
            (iid,),
        )
    bids, asks = await book_depth(conn, iid, levels=4)
    assert len(bids) == 4 and len(asks) == 4
    assert all(lvl.synthetic for lvl in bids + asks)
    assert max(float(lvl.price) for lvl in bids) < float(mark)
    assert min(float(lvl.price) for lvl in asks) > float(mark)


async def test_book_depth_hides_untriggered_stops(conn: AsyncConnection) -> None:
    iid, ticker, mark = await _first_stock(conn)
    await _fund(conn, 6103, 10_000_000_00)
    # A STOP buy triggers above the mark -- untriggered, so invisible.
    await place_order(
        conn, user_id=6103, ticker=ticker, side="BUY", quantity=1,
        limit_price=None, stop_price=mark * Decimal("1.5"),
    )
    bids, _asks = await book_depth(conn, iid, levels=5)
    assert all(lvl.synthetic for lvl in bids)
