"""Limit orders: placement, tick-time matching, expiry, cancellation."""

from __future__ import annotations

from decimal import Decimal

import pytest
from psycopg import AsyncConnection
from psycopg.rows import dict_row

from stockbot.accounts.service import bootstrap_user
from stockbot.ledger.service import (
    get_balance,
    get_system_account_id,
    get_user_account_id,
    post_transfer,
)
from stockbot.margin.service import margin_config
from stockbot.market import engine
from stockbot.market.data import half_spread_for, spread_config
from stockbot.market.tick import apply_tick
from stockbot.orders.service import (
    cancel_order,
    list_open_orders,
    match_orders,
    place_order,
)
from stockbot.trading.errors import NotInLeagueError, UnknownInstrumentError
from stockbot.trading.service import execute_trade

SEED = "orders-test-seed"


async def _quoted(conn: AsyncConnection, ticker: str) -> Decimal:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT quoted_price FROM instruments WHERE ticker = %s", (ticker,)
        )
        row = await cur.fetchone()
        assert row is not None
        return Decimal(row[0])


async def _first_ticker(conn: AsyncConnection) -> str:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT ticker FROM instruments WHERE is_active AND kind != 'INDEX' "
            "ORDER BY ticker LIMIT 1"
        )
        row = await cur.fetchone()
        assert row is not None
        return str(row[0])


async def _order_status(conn: AsyncConnection, order_id: int) -> str:
    async with conn.cursor() as cur:
        await cur.execute("SELECT status FROM orders WHERE id = %s", (order_id,))
        row = await cur.fetchone()
        assert row is not None
        return str(row[0])


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


async def test_place_order_rejects_unknown_ticker(conn: AsyncConnection) -> None:
    await bootstrap_user(conn, 3001)
    with pytest.raises(UnknownInstrumentError):
        await place_order(
            conn,
            user_id=3001,
            ticker="NOPE",
            side="BUY",
            quantity=1,
            limit_price=Decimal("10"),
        )


async def test_limit_buy_fills_when_mark_dips(conn: AsyncConnection) -> None:
    await bootstrap_user(conn, 3002)
    ticker = await _first_ticker(conn)
    mark = await _quoted(conn, ticker)
    # Limit far above the mark fills next tick; limit far below stays open.
    high = await place_order(
        conn, user_id=3002, ticker=ticker, side="BUY", quantity=1,
        limit_price=mark * 2,
    )
    low = await place_order(
        conn, user_id=3002, ticker=ticker, side="BUY", quantity=1,
        limit_price=Decimal("0.01"),
    )
    await apply_tick(conn, SEED)
    assert await _order_status(conn, high.order_id) == "FILLED"
    assert await _order_status(conn, low.order_id) == "OPEN"
    assert await _position_qty(conn, 3002, ticker) == 1

    # N1 outbox: the MM-fallback fill notifies (taker, since MM is the
    # counterparty); the still-OPEN order does not.
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT payload FROM notifications "
            "WHERE kind = 'ORDER_FILLED' AND user_id = 3002"
        )
        rows = await cur.fetchall()
    assert len(rows) == 1
    assert rows[0][0]["order_id"] == high.order_id
    assert rows[0][0]["maker"] is False


async def test_limit_sell_fills_when_mark_rises(conn: AsyncConnection) -> None:
    await bootstrap_user(conn, 3003)
    ticker = await _first_ticker(conn)
    await execute_trade(conn, user_id=3003, ticker=ticker, side="BUY", quantity=1)
    order = await place_order(
        conn, user_id=3003, ticker=ticker, side="SELL", quantity=1,
        limit_price=Decimal("0.01"),  # absurdly low -> fills next tick
    )
    await apply_tick(conn, SEED)
    assert await _order_status(conn, order.order_id) == "FILLED"
    assert await _position_qty(conn, 3003, ticker) == 0


async def test_order_expires(conn: AsyncConnection) -> None:
    await bootstrap_user(conn, 3004)
    ticker = await _first_ticker(conn)
    order = await place_order(
        conn, user_id=3004, ticker=ticker, side="BUY", quantity=1,
        limit_price=Decimal("0.01"), expires_in_ticks=1,
    )
    await apply_tick(conn, SEED)
    await apply_tick(conn, SEED)
    assert await _order_status(conn, order.order_id) == "EXPIRED"


async def test_cancel_order(conn: AsyncConnection) -> None:
    await bootstrap_user(conn, 3005)
    ticker = await _first_ticker(conn)
    order = await place_order(
        conn, user_id=3005, ticker=ticker, side="BUY", quantity=1,
        limit_price=Decimal("0.01"),
    )
    assert await cancel_order(conn, user_id=3005, order_id=order.order_id)
    assert await _order_status(conn, order.order_id) == "CANCELLED"
    # can't cancel someone else's or a non-open order
    assert not await cancel_order(conn, user_id=3005, order_id=order.order_id)
    assert not await cancel_order(conn, user_id=9999, order_id=order.order_id)


async def test_order_unfillable_stays_open(conn: AsyncConnection) -> None:
    """A limit buy that crosses the mark but exceeds cash stays open."""
    await bootstrap_user(conn, 3006)
    ticker = await _first_ticker(conn)
    mark = await _quoted(conn, ticker)
    # Drain the account so the fill can't afford itself.
    account_id = await get_user_account_id(conn, 3006)
    balance = await get_balance(conn, account_id)
    sink = await get_system_account_id(conn, "SINK")
    await post_transfer(
        conn,
        from_account_id=account_id,
        to_account_id=sink,
        amount=balance - 1,  # leave $0.01 -- can't afford anything
        reason="TEST",
    )
    order = await place_order(
        conn, user_id=3006, ticker=ticker, side="BUY", quantity=10,
        limit_price=mark * 2,
    )
    fills = await match_orders(conn, 999999)
    assert fills == 0
    assert await _order_status(conn, order.order_id) == "OPEN"


async def test_place_order_rejects_unjoined_season(conn: AsyncConnection) -> None:
    """A league order on a season the user isn't actively entered in is
    rejected at placement, not left to fail NotInLeagueError every tick."""
    from stockbot.seasons.service import create_season

    await bootstrap_user(conn, 3010)
    ticker = await _first_ticker(conn)
    season_id = await create_season(
        conn, name="orders-nope", start_tick=0, end_tick=10_000
    )
    with pytest.raises(NotInLeagueError):
        await place_order(
            conn,
            user_id=3010,
            ticker=ticker,
            side="BUY",
            quantity=1,
            limit_price=Decimal("0.01"),
            season_id=season_id,
        )


async def test_list_open_orders_scopes_by_season(conn: AsyncConnection) -> None:
    from stockbot.seasons.service import create_season, join_season, on_tick

    await bootstrap_user(conn, 3007)
    ticker = await _first_ticker(conn)
    season_id = await create_season(
        conn, name="orders-test", start_tick=0, end_tick=10_000
    )
    await on_tick(conn, 0)  # activate so league orders are accepted
    await join_season(conn, 3007, season_id)
    await place_order(
        conn, user_id=3007, ticker=ticker, side="BUY", quantity=1,
        limit_price=Decimal("0.01"),
    )
    await place_order(
        conn, user_id=3007, ticker=ticker, side="BUY", quantity=1,
        limit_price=Decimal("0.02"), season_id=season_id,
    )
    main = await list_open_orders(conn, 3007)
    league = await list_open_orders(conn, 3007, season_id=season_id)
    assert len(main) == 1 and len(league) == 1
    assert league[0]["limit_price"] == Decimal("0.02")


async def test_mm_fill_that_breaches_limit_stays_open(conn: AsyncConnection) -> None:
    """execute_trade re-reads the instrument and adds the squeeze boost on
    BUYs -- the candidate pre-check can't see it, so a fill can land above
    its limit even though the estimate passed. The post-fill check rolls it
    back and the order stays OPEN."""
    await bootstrap_user(conn, 3008)
    ticker = await _first_ticker(conn)

    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute("SELECT * FROM instruments WHERE ticker = %s", (ticker,))
        inst = await cur.fetchone()
        # Crowded short: execute_trade boosts buy-side impact over the
        # squeeze threshold.
        await cur.execute(
            "UPDATE instruments SET short_interest_pct = 0.95 WHERE id = %s",
            (inst["id"],),
        )

    cfg = await margin_config(conn)
    over = Decimal("0.95") - cfg["margin.squeeze_si_threshold"]
    assert over > 0
    boosted_lam = float(inst["lambda_impact"]) * float(
        1 + cfg["margin.squeeze_lambda_boost"] * over
    )
    # Tick rounding would collapse the engineered est..act window to a
    # single grid point, and the U-shape term makes the fill depend on the
    # *real* current tick (execute_trade uses MAX(tick_index)) rather than
    # the synthetic index match_orders is called with. Flatten both for
    # the duration of this test -- it targets the post-fill re-check.
    async with conn.cursor() as cur:
        await cur.execute("UPDATE config SET value = 0 WHERE key = 'spread.tick_pct'")
        await cur.execute(
            "UPDATE config SET value = 0.0000001 WHERE key = 'spread.tick_min'"
        )
        # Venue session shape and tick grid live on the markets row
        # post-0053 (tick_size overrides spread.tick_min).
        await cur.execute(
            "UPDATE markets SET open_ticks = 1000000000, closed_ticks = 0, "
            "tick_size = 0.0000001"
        )
    spread_cfg = await spread_config(conn)
    base = float(inst["base_price"])
    imp = float(inst["impact"])
    liq = float(inst["liquidity"])
    lam = float(inst["lambda_impact"])
    mi = float(inst["max_impact"])
    mark = Decimal(inst["quoted_price"])
    hs = half_spread_for(inst, None, spread_cfg)

    # Find a quantity where the pre-check estimate passes its limit while
    # the squeezed actual fill breaches it. The limit sits at the midpoint
    # of the est..act window -- the exact gap the candidate scan can't see.
    qty: int | None = None
    limit: Decimal | None = None
    for q in (1, 2, 5, 10, 25, 50, 100, 250, 500, 1000, 2500):
        est, _ = engine.apply_trade_impact(
            base_price=base, impact_before=imp, signed_notional=base * q,
            liquidity=liq, lambda_impact=lam, max_impact=mi, half_spread=hs,
        )
        act, _ = engine.apply_trade_impact(
            base_price=base, impact_before=imp, signed_notional=base * q,
            liquidity=liq, lambda_impact=boosted_lam, max_impact=mi, half_spread=hs,
        )
        cand = Decimal(str((est + act) / 2))
        # Candidate filter needs quoted <= limit*(1 - epsilon=0.0005).
        if mark <= cand * Decimal("0.9995"):
            qty, limit = q, cand
            break
    assert qty is not None and limit is not None

    order = await place_order(
        conn, user_id=3008, ticker=ticker, side="BUY", quantity=qty,
        limit_price=limit,
    )
    assert await match_orders(conn, 999999) == 0
    assert await _order_status(conn, order.order_id) == "OPEN"

    # The order was genuinely fillable: with normal short interest the
    # same fill lands under the limit and completes.
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE instruments SET short_interest_pct = 0 WHERE id = %s",
            (inst["id"],),
        )
    assert await match_orders(conn, 999999) == 1
    assert await _order_status(conn, order.order_id) == "FILLED"


async def test_deterministic_fill_failures_auto_cancel(conn: AsyncConnection) -> None:
    """An order that can never settle retries every tick; after
    order.max_fill_failures strikes the matcher auto-cancels it."""
    await bootstrap_user(conn, 3015)  # starting grant only: fills always fail
    ticker = await _first_ticker(conn)
    mark = await _quoted(conn, ticker)
    order = await place_order(
        conn, user_id=3015, ticker=ticker, side="BUY", quantity=50,
        limit_price=mark * 2,
    )
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE config SET value = 3 WHERE key = 'order.max_fill_failures'"
        )

    for _ in range(2):
        await match_orders(conn, 999999)
        assert await _order_status(conn, order.order_id) == "OPEN"
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT fill_failures FROM orders WHERE id = %s", (order.order_id,)
            )
            assert (await cur.fetchone())[0] < 3

    await match_orders(conn, 999999)
    assert await _order_status(conn, order.order_id) == "CANCELLED"


async def test_iceberg_shows_display_qty_but_fills_full_size(
    conn: AsyncConnection,
) -> None:
    """B2: `display_qty` caps what the /stock depth ladder shows -- the
    matcher still works the real remaining quantity, so a cross can fill
    the whole hidden size in one tick."""
    from stockbot.market.data import book_depth

    await bootstrap_user(conn, 3020)
    buyer_account = await get_user_account_id(conn, 3020)
    await post_transfer(
        conn,
        from_account_id=await get_system_account_id(conn, "FAUCET"),
        to_account_id=buyer_account,
        amount=10_000_000,
        reason="TEST_TOPUP",
    )
    await bootstrap_user(conn, 3021)
    seller_account = await get_user_account_id(conn, 3021)
    await post_transfer(
        conn,
        from_account_id=await get_system_account_id(conn, "FAUCET"),
        to_account_id=seller_account,
        amount=10_000_000,
        reason="TEST_TOPUP",
    )
    # Iceberg display is a paid unlock (0041).
    async with conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO entitlements (user_id, item_key) "
            "VALUES (3021, 'order_iceberg')"
        )
    ticker = await _first_ticker(conn)
    mark = await _quoted(conn, ticker)

    # Seller needs inventory to sell 200 shares.
    await execute_trade(conn, user_id=3021, ticker=ticker, side="BUY", quantity=200)
    # Refresh the mark after the buy moved it, then rest the iceberg ask
    # just above it and a crossing bid at the same price. 1% stays well
    # inside the 2% cross collar -- mark*1.02 lands right on the boundary
    # after tick-grid snapping and flips on rounding dust.
    mark = await _quoted(conn, ticker)
    ask = await place_order(
        conn, user_id=3021, ticker=ticker, side="SELL", quantity=200,
        limit_price=mark * Decimal("1.01"), display_qty=25,
    )

    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT id FROM instruments WHERE ticker = %s", (ticker,)
        )
        (instrument_id,) = await cur.fetchone()
    bids, asks = await book_depth(conn, instrument_id)
    visible = next(
        (lv for lv in asks if not lv.synthetic and lv.price == ask.limit_price),
        None,
    )
    assert visible is not None
    assert visible.quantity == 25  # the displayed slice, not 200
    assert visible.cumulative >= 25

    bid = await place_order(
        conn, user_id=3020, ticker=ticker, side="BUY", quantity=200,
        limit_price=mark * Decimal("1.01"),
    )
    await match_orders(conn, 999999)
    assert await _order_status(conn, ask.order_id) == "FILLED"
    assert await _order_status(conn, bid.order_id) == "FILLED"
    assert await _position_qty(conn, 3020, ticker) == 200


async def test_place_order_rejects_bad_display_qty(conn: AsyncConnection) -> None:
    await bootstrap_user(conn, 3022)
    async with conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO entitlements (user_id, item_key) "
            "VALUES (3022, 'order_iceberg')"
        )
    ticker = await _first_ticker(conn)
    mark = await _quoted(conn, ticker)
    with pytest.raises(ValueError):
        await place_order(
            conn, user_id=3022, ticker=ticker, side="BUY", quantity=10,
            limit_price=mark, display_qty=11,
        )
    with pytest.raises(ValueError):
        await place_order(
            conn, user_id=3022, ticker=ticker, side="BUY", quantity=10,
            limit_price=mark, display_qty=0,
        )
