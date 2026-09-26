"""Resting limit orders, matched once per tick inside apply_tick.

Two matching passes per tick, per (instrument, season scope):

1. Crossing book -- bids and asks cross each other at the maker's price
   (the earlier order by opened_tick/id), price-time priority, partial
   fills tracked on `orders.filled_quantity`. Crosses are collared to
   `cross.collar_pct` around the mark (stale off-market makers are
   skipped), banned between a user's own orders, and each prints the mark
   (quoted = cross price, clamped by max_impact and the circuit breaker).
   Uncrossed remainder then falls through to pass 2.
2. Market-maker fallback -- an order fills when the impact-adjusted
   executable price satisfies its limit, via `execute_trade` (same fees,
   impact, margin gates as a market order) inside a savepoint, so a
   failing order rolls back cleanly and stays OPEN for a later tick.

Orders don't reserve cash or shares at placement; fill-time validation is
authoritative.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Literal

from psycopg import AsyncConnection
from psycopg.rows import dict_row

from stockbot.ledger.errors import LedgerError
from stockbot.ledger.service import (
    get_user_account_id,
    post_transfer,
    record_idempotency_key,
)
from stockbot.margin.errors import (
    InstrumentNotShortableError,
    MarginError,
)
from stockbot.market import engine
from stockbot.market.data import (
    assert_feature_enabled,
    assert_market_open,
    event_halt_lead_ticks,
    event_halted,
    flow_config,
    half_spread_for,
    participation_cap,
    record_flow,
    session_config,
    spread_config,
)
from stockbot.trading.errors import (
    EventHaltedError,
    FeatureDisabledError,
    MarketClosedError,
    NotInLeagueError,
    TradingError,
    UnknownInstrumentError,
)
from stockbot.trading.service import (
    _apply_fill,
    _to_minor_units,
    execute_trade,
    update_candle_with_fill,
)

Side = Literal["BUY", "SELL"]
OrderType = Literal["LIMIT", "STOP", "STOP_LIMIT"]


class _LimitBreach(Exception):
    """An actual fill landed worse than the order's limit.

    Deliberately distinct from the deterministic failure classes: a breach
    is mark-dependent (the order may fill next tick), so it must not count
    toward `orders.fill_failures` auto-cancellation.
    """


@dataclass(frozen=True)
class OrderResult:
    order_id: int
    ticker: str
    side: Side
    quantity: int
    limit_price: Decimal | None
    expires_tick: int | None
    order_type: OrderType = "LIMIT"
    stop_price: Decimal | None = None
    display_qty: int | None = None
    trail_amount: Decimal | None = None


async def _require_entitlement(
    conn: AsyncConnection, user_id: int, item_key: str
) -> None:
    """Gate for paid order-type unlocks (shop_items kind='ORDER_TYPE').
    Raw SQL rather than a shop import: the entitlement check is one row."""
    async with conn.cursor() as cur:
        await cur.execute(
            """
            SELECT 1 FROM entitlements
            WHERE user_id = %s AND item_key = %s
              AND (expires_at IS NULL OR expires_at > now())
            """,
            (user_id, item_key),
        )
        if await cur.fetchone() is None:
            raise TradingError(
                f"`{item_key}` is a paid unlock — buy it in `/shop list` first."
            )


async def _cancel_oco_siblings(conn: AsyncConnection, order_ids: list[int]) -> int:
    """One-cancels-other: when any leg of an oco_group goes terminal
    (filled, cancelled, expired, struck out on fill_failures), its
    still-open siblings die with it -- that IS the contract. Sweeps that
    cancel whole scopes at once (season close, delist, user suspension)
    already take both legs in one UPDATE; the per-order paths route here.
    """
    if not order_ids:
        return 0
    async with conn.cursor() as cur:
        await cur.execute(
            """
            UPDATE orders SET status = 'CANCELLED'
            WHERE status = 'OPEN' AND oco_group IN (
                SELECT oco_group FROM orders
                WHERE id = ANY(%s) AND oco_group IS NOT NULL
            )
            """,
            (list(order_ids),),
        )
        return cur.rowcount


async def _current_tick(conn: AsyncConnection) -> int | None:
    async with conn.cursor() as cur:
        await cur.execute("SELECT MAX(tick_index) FROM market_ticks")
        row = await cur.fetchone()
    return int(row[0]) if row and row[0] is not None else None


async def place_order(
    conn: AsyncConnection,
    *,
    user_id: int,
    ticker: str,
    side: Side,
    quantity: int,
    limit_price: Decimal | None,
    season_id: int | None = None,
    expires_in_ticks: int | None = None,
    interaction_id: str | None = None,
    stop_price: Decimal | None = None,
    display_qty: int | None = None,
    allow_short: bool = False,
    trail_amount: Decimal | None = None,
) -> OrderResult:
    """Validate and rest an order. Marketability/margin are re-checked at
    fill time; placement only rejects structurally bad orders.

    limit only -> LIMIT; stop only -> STOP (triggers to a marketable
    order); both -> STOP_LIMIT (triggers to a limit order at limit_price).
    `display_qty` caps the size shown in the /stock depth ladder (an
    iceberg): the matcher still works the real remaining quantity, and
    the visible slice refills until the order drains.

    `trail_amount` (paid unlock) makes a pure STOP trailing: with no
    explicit stop_price the initial stop anchors one trail-width off the
    mark, then `_ratchet_trailing_stops` chases the mark each tick.

    `allow_short` is the N4 oversell opt-in: a SELL without it is
    clamped to the seller's position at fill time (the unbacked part
    stays OPEN) instead of silently opening a margin short.
    """
    if quantity <= 0:
        raise ValueError("quantity must be positive")
    if limit_price is not None and limit_price <= 0:
        raise ValueError("limit price must be positive")
    if stop_price is not None and stop_price <= 0:
        raise ValueError("stop price must be positive")
    if display_qty is not None and not 0 < display_qty <= quantity:
        raise ValueError("display quantity must be between 1 and the order quantity")
    if trail_amount is not None:
        if trail_amount <= 0:
            raise ValueError("trail amount must be positive")
        if limit_price is not None:
            raise ValueError(
                "trailing is only supported on pure stop orders (no limit)"
            )
    order_type: OrderType
    if stop_price is None and trail_amount is None:
        if limit_price is None:
            raise ValueError("need a limit price, a stop price, or both")
        order_type = "LIMIT"
    else:
        order_type = "STOP_LIMIT" if limit_price is not None else "STOP"
    ticker = ticker.upper()

    async with conn.transaction(), conn.cursor() as cur:
        if interaction_id is not None:
            await record_idempotency_key(conn, interaction_id)
        await assert_feature_enabled(conn, "orders.enabled", "order placement")
        await assert_market_open(conn)
        # Paid order-type unlocks (0041): gated at placement; a resting
        # order keeps working even if the entitlement later lapses.
        if display_qty is not None:
            await _require_entitlement(conn, user_id, "order_iceberg")
        if trail_amount is not None:
            await _require_entitlement(conn, user_id, "order_trailing")
        await cur.execute(
            "SELECT id, is_active, quoted_price, next_halting_event_tick, "
            "shortable_after_tick FROM instruments WHERE ticker = %s",
            (ticker,),
        )
        row = await cur.fetchone()
        if row is None or not row[1]:
            raise UnknownInstrumentError(ticker)
        instrument_id = int(row[0])
        next_halt_tick = row[3]

        # Tick-size rounding (G3): resting prices snap to the same grid
        # fills print on, so a displayed quote is always attainable.
        tick_cfg = await spread_config(conn)
        tick_grid = Decimal(str(engine.tick_size(float(row[2]), tick_cfg)))
        if trail_amount is not None and stop_price is None:
            # Trail-only stop: anchor the initial stop one trail-width off
            # the mark; the per-tick ratchet takes it from there.
            mark = Decimal(row[2])
            raw_stop = (
                mark - trail_amount if side == "SELL" else mark + trail_amount
            )
            if raw_stop <= 0:
                raise ValueError(
                    "trail amount is wider than the mark — the stop would "
                    "anchor at zero"
                )
            stop_price = raw_stop
        if limit_price is not None:
            limit_price = (
                Decimal(str(limit_price)) / tick_grid
            ).to_integral_value(rounding=ROUND_HALF_UP) * tick_grid
        if stop_price is not None:
            stop_price = (
                Decimal(str(stop_price)) / tick_grid
            ).to_integral_value(rounding=ROUND_HALF_UP) * tick_grid
        if season_id is not None:
            # Placement-time league check (the same rule execute_trade
            # enforces at fill): an order on a season the user isn't
            # actively entered in would rest OPEN forever, raising
            # NotInLeagueError on every match pass.
            await cur.execute(
                """
                SELECT 1 FROM season_entries e
                JOIN seasons s ON s.id = e.season_id
                WHERE e.user_id = %s AND e.season_id = %s AND s.status = 'ACTIVE'
                """,
                (user_id, season_id),
            )
            if await cur.fetchone() is None:
                raise NotInLeagueError(user_id, season_id)
        tick = await _current_tick(conn)
        # T1 halt: no new resting orders inside the lead window before a
        # scheduled earnings print -- consistent with closed-market
        # placement being blocked. Existing orders keep resting.
        if event_halted(
            next_halt_tick, tick, await event_halt_lead_ticks(conn)
        ):
            raise EventHaltedError(ticker, int(next_halt_tick))
        # Fail-fast borrow lockout (IPO listings): the fill-time gate in
        # _apply_fill is authoritative, but letting a SELL+short order rest
        # through the lockout would just churn fill_failures.
        if (
            side == "SELL"
            and allow_short
            and row[4] is not None
            and (tick or 0) < int(row[4])
        ):
            raise InstrumentNotShortableError(ticker, int(row[4]))
        expires_tick = (
            None if expires_in_ticks is None else (tick or 0) + expires_in_ticks
        )
        await cur.execute(
            """
            INSERT INTO orders
                (user_id, instrument_id, season_id, side, quantity,
                 limit_price, opened_tick, expires_tick, order_type, stop_price,
                 display_qty, allow_short, trail_amount)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                user_id,
                instrument_id,
                season_id,
                side,
                quantity,
                limit_price,
                tick,
                expires_tick,
                order_type,
                stop_price,
                display_qty,
                allow_short,
                trail_amount,
            ),
        )
        order_row = await cur.fetchone()
        assert order_row is not None
        order_id = int(order_row[0])

    return OrderResult(
        order_id=order_id,
        ticker=ticker,
        side=side,
        quantity=quantity,
        limit_price=limit_price,
        expires_tick=expires_tick,
        order_type=order_type,
        stop_price=stop_price,
        display_qty=display_qty,
        trail_amount=trail_amount,
    )


async def place_oco(
    conn: AsyncConnection,
    *,
    user_id: int,
    ticker: str,
    side: Side,
    quantity: int,
    take_profit: Decimal,
    stop_price: Decimal,
    season_id: int | None = None,
    expires_in_ticks: int | None = None,
    interaction_id: str | None = None,
    allow_short: bool = False,
) -> tuple[OrderResult, OrderResult]:
    """Rest a one-cancels-other bracket: a take-profit LIMIT leg plus a
    stop-loss STOP leg sharing an `oco_group` (the take-profit leg's id).
    Whichever leg resolves first -- fill, cancel, expiry, strike-out --
    cancels the other via `_cancel_oco_siblings`.

    SELL exits a long (take_profit above, stop below); BUY exits a short
    (mirrored). Placement validates only that the pair isn't self-crossed;
    position fit is checked at fill time like every other order.
    """
    if take_profit <= 0 or stop_price <= 0:
        raise ValueError("bracket prices must be positive")
    if side == "SELL" and take_profit <= stop_price:
        raise ValueError("a SELL bracket needs take_profit above stop_loss")
    if side == "BUY" and take_profit >= stop_price:
        raise ValueError("a BUY bracket needs take_profit below stop_loss")
    async with conn.transaction():
        # One key for the pair: a redelivered interaction must not rest a
        # second bracket. The inner place_order calls get None -- the outer
        # claim is what makes the whole pair atomic.
        if interaction_id is not None:
            await record_idempotency_key(conn, interaction_id)
        await _require_entitlement(conn, user_id, "order_oco")
        tp_leg = await place_order(
            conn,
            user_id=user_id,
            ticker=ticker,
            side=side,
            quantity=quantity,
            limit_price=take_profit,
            season_id=season_id,
            expires_in_ticks=expires_in_ticks,
            allow_short=allow_short,
        )
        sl_leg = await place_order(
            conn,
            user_id=user_id,
            ticker=ticker,
            side=side,
            quantity=quantity,
            limit_price=None,
            stop_price=stop_price,
            season_id=season_id,
            expires_in_ticks=expires_in_ticks,
            allow_short=allow_short,
        )
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE orders SET oco_group = %s WHERE id IN (%s, %s)",
                (tp_leg.order_id, tp_leg.order_id, sl_leg.order_id),
            )
    return tp_leg, sl_leg


async def cancel_order(conn: AsyncConnection, *, user_id: int, order_id: int) -> bool:
    """Cancel an open order owned by `user_id`. Returns False if not found.
    Cancelling one OCO leg cancels its sibling -- one decision, one pair."""
    async with conn.transaction(), conn.cursor() as cur:
        await cur.execute(
            """
            UPDATE orders SET status = 'CANCELLED'
            WHERE id = %s AND user_id = %s AND status = 'OPEN'
            """,
            (order_id, user_id),
        )
        cancelled = cur.rowcount > 0
        if cancelled:
            await _cancel_oco_siblings(conn, [order_id])
        return cancelled


async def list_open_orders(
    conn: AsyncConnection, user_id: int, season_id: int | None = None
) -> list[dict[str, Any]]:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            SELECT o.id, i.ticker, o.side, o.quantity, o.filled_quantity,
                   o.limit_price, o.order_type, o.stop_price, o.triggered_tick,
                   o.display_qty, o.trail_amount, o.oco_group,
                   i.quoted_price, o.expires_tick, o.created_at
            FROM orders o
            JOIN instruments i ON i.id = o.instrument_id
            WHERE o.user_id = %s AND o.status = 'OPEN'
              AND o.season_id IS NOT DISTINCT FROM %s
            ORDER BY o.id
            """,
            (user_id, season_id),
        )
        return await cur.fetchall()


def _is_maker(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """The maker is the earlier order by (opened_tick, id) -- orders placed
    before the first tick have opened_tick NULL and sort first."""
    return (a["opened_tick"] or -1, a["id"]) <= (b["opened_tick"] or -1, b["id"])


async def _account_for_order(conn: AsyncConnection, order: dict[str, Any]) -> int:
    """Resolve the cash account for an order: main account, or the user's
    LEAGUE account in an ACTIVE season (same rule as execute_trade)."""
    user_id = int(order["user_id"])
    season_id = order["season_id"]
    if season_id is None:
        return await get_user_account_id(conn, user_id)
    async with conn.cursor() as cur:
        await cur.execute(
            """
            SELECT e.account_id
            FROM season_entries e
            JOIN seasons s ON s.id = e.season_id
            WHERE e.user_id = %s AND e.season_id = %s AND s.status = 'ACTIVE'
            """,
            (user_id, season_id),
        )
        row = await cur.fetchone()
    if row is None:
        raise NotInLeagueError(user_id, int(season_id))
    return int(row[0])


async def _sellable_qty(conn: AsyncConnection, order: dict[str, Any]) -> int:
    """Shares a SELL order may move without opening or growing a margin
    short: the position floored at 0 (N4.2, `orders.allow_short`)."""
    async with conn.cursor() as cur:
        await cur.execute(
            """
            SELECT quantity FROM positions
            WHERE user_id = %s AND instrument_id = %s
              AND season_id IS NOT DISTINCT FROM %s
            """,
            (order["user_id"], order["instrument_id"], order["season_id"]),
        )
        row = await cur.fetchone()
    return max(0, int(row[0])) if row else 0


async def _settle_cross(
    conn: AsyncConnection,
    *,
    bid: dict[str, Any],
    ask: dict[str, Any],
    cross_price: Decimal,
    quantity: int,
    base_price: float,
    mark: float,
    max_impact: float,
    tick_index: int,
    flow_halt_ticks: int,
) -> tuple[float, float, bool]:
    """Settle one book cross inside the caller's savepoint: one buyer ->
    seller cash transfer, both position/margin legs via `_apply_fill`, the
    mark print, and `filled_quantity` bookkeeping.

    Returns (new_mark, new_impact, halted): the mark moves to the cross
    price, clamped by max_impact and the circuit-breaker cap (a breach
    halts the instrument like a tick-time move would).
    """
    buyer_id = int(bid["user_id"])
    seller_id = int(ask["user_id"])
    season_id = bid["season_id"]
    instrument_id = int(bid["instrument_id"])
    ticker = str(bid["ticker"])

    buyer_account = await _account_for_order(conn, bid)
    seller_account = await _account_for_order(conn, ask)
    # Lock ordering: instruments (held by apply_tick), then accounts in
    # ascending id order.
    for account_id in sorted((buyer_account, seller_account)):
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT id FROM accounts WHERE id = %s FOR UPDATE", (account_id,)
            )
            await cur.fetchone()

    notional_minor = _to_minor_units(cross_price * quantity)
    transfer_id = str(
        await post_transfer(
            conn,
            from_account_id=buyer_account,
            to_account_id=seller_account,
            amount=notional_minor,
            reason="TRADE_CROSS",
            memo=ticker,
        )
    )
    bid_maker = _is_maker(bid, ask)
    common = dict(
        instrument_id=instrument_id,
        ticker=ticker,
        quantity=quantity,
        fill_price=cross_price,
        season_id=season_id,
        current_tick=tick_index,
        cash_leg="none",
        cash_transfer_id=transfer_id,
    )
    await _apply_fill(
        conn,
        user_id=buyer_id,
        account_id=buyer_account,
        side="BUY",
        counterparty_account_id=seller_account,
        maker_taker="MAKER" if bid_maker else "TAKER",
        counterparty_user_id=seller_id,
        order_id=int(bid["id"]),
        **common,
    )
    await _apply_fill(
        conn,
        user_id=seller_id,
        account_id=seller_account,
        side="SELL",
        counterparty_account_id=buyer_account,
        maker_taker="TAKER" if bid_maker else "MAKER",
        counterparty_user_id=buyer_id,
        order_id=int(ask["id"]),
        **common,
    )

    # Mark print: quoted moves to the cross price, clamped by max_impact
    # and the per-tick circuit-breaker cap. A breach halts the book --
    # a cross print is flow by definition, so it gets the shorter
    # flow-attributed halt (vol.flow_halt_ticks), not the model one.
    desired_impact = math.log(float(cross_price) / base_price)
    new_impact = max(-max_impact, min(max_impact, desired_impact))
    new_mark = base_price * math.exp(new_impact)
    delta = math.log(new_mark / mark)
    halted = abs(delta) > engine.CIRCUIT_BREAKER_CAP
    if halted:
        new_mark = mark * math.exp(math.copysign(engine.CIRCUIT_BREAKER_CAP, delta))
        new_impact = math.log(new_mark / base_price)

    async with conn.cursor() as cur:
        await cur.execute(
            """
            UPDATE instruments
            SET impact = %s, quoted_price = %s,
                circuit_halted_until_tick = CASE WHEN %s THEN %s ELSE circuit_halted_until_tick END,
                last_halt_end_tick = CASE WHEN %s THEN %s ELSE last_halt_end_tick END
            WHERE id = %s
            """,
            (
                new_impact,
                Decimal(str(round(new_mark, 6))),
                halted,
                tick_index + flow_halt_ticks,
                halted,
                tick_index + flow_halt_ticks,
                instrument_id,
            ),
        )
        if halted:
            # The candle for this tick was already written during the
            # step phase -- amend halt_kind so the cross-attributed halt
            # is visible to chart/replay instead of NULL.
            await cur.execute(
                "UPDATE candles SET halt_kind = 'FLOW' "
                "WHERE instrument_id = %s AND tick_index = %s",
                (instrument_id, tick_index),
            )
        for order, is_maker in ((bid, bid_maker), (ask, not bid_maker)):
            await cur.execute(
                """
                UPDATE orders
                SET filled_quantity = filled_quantity + %s,
                    status = CASE
                        WHEN filled_quantity + %s >= quantity THEN 'FILLED'
                        ELSE status END,
                    filled_tick = CASE
                        WHEN filled_quantity + %s >= quantity THEN %s
                        ELSE filled_tick END,
                    fill_price = %s
                WHERE id = %s AND status = 'OPEN'
                RETURNING status, side, user_id
                """,
                (quantity, quantity, quantity, tick_index, cross_price, order["id"]),
            )
            updated = await cur.fetchone()
            if updated is None:
                # A cancel committed between the book snapshot and this
                # write: without the re-check the fill would overwrite
                # 'CANCELLED'. Roll back the whole cross via the savepoint.
                raise ValueError(f"order {order['id']} is no longer open")
            if updated[0] == "FILLED":
                # OCO: the filled leg going terminal kills its sibling.
                await _cancel_oco_siblings(conn, [int(order["id"])])
                await cur.execute(
                    """
                    INSERT INTO notifications (user_id, kind, payload)
                    VALUES (%s, 'ORDER_FILLED', %s)
                    """,
                    (
                        int(updated[2]),
                        json.dumps(
                            {
                                "tick_index": tick_index,
                                "order_id": int(order["id"]),
                                "ticker": ticker,
                                "side": updated[1],
                                "qty": order["quantity"],
                                "fill": float(cross_price),
                                "maker": is_maker,
                            }
                        ),
                    ),
                )

    await update_candle_with_fill(conn, instrument_id, cross_price, quantity)
    # Flow attribution: the cross's mark move charges to the taker (the
    # aggressor who lifted the print) for the per-account vol cap.
    await record_flow(
        conn,
        instrument_id=instrument_id,
        user_id=int(ask["user_id"]) if bid_maker else int(bid["user_id"]),
        delta_impact=new_impact - math.log(mark / base_price),
        signed_notional_minor=(
            -notional_minor if bid_maker else notional_minor
        ),
    )
    return new_mark, new_impact, halted


def _marketable(order: dict[str, Any]) -> bool:
    """A triggered STOP has no limit price -- it takes whatever the book
    or MM gives it. (Untriggered stops are excluded from matching.)"""
    return bool(order["order_type"] == "STOP")


def _bid_sort_key(o: dict[str, Any]) -> tuple[Decimal, Any, Any]:
    # Marketable bids sort first (effective limit +inf -> -inf key).
    return (
        Decimal("-Infinity") if _marketable(o) else -o["limit_price"],
        o["opened_tick"] or -1,
        o["id"],
    )


def _ask_sort_key(o: dict[str, Any]) -> tuple[Decimal, Any, Any]:
    # Marketable asks sort first (effective limit 0).
    return (
        Decimal(0) if _marketable(o) else o["limit_price"],
        o["opened_tick"] or -1,
        o["id"],
    )


async def _trigger_due_stops(conn: AsyncConnection, tick_index: int) -> int:
    """Flip every OPEN stop whose stop price the mark has crossed to
    triggered (triggered_tick = this tick). Returns how many fired.

    BUY stops trigger at/above their stop, SELL stops at/below -- the
    standard stop-loss/stop-buy semantics.
    """
    async with conn.cursor() as cur:
        await cur.execute(
            """
            UPDATE orders o SET triggered_tick = %s
            FROM instruments i
            WHERE o.instrument_id = i.id
              AND o.status = 'OPEN'
              AND o.triggered_tick IS NULL
              AND o.order_type <> 'LIMIT'
              AND ((o.side = 'BUY'  AND i.quoted_price >= o.stop_price)
                OR (o.side = 'SELL' AND i.quoted_price <= o.stop_price))
            """,
            (tick_index,),
        )
        return cur.rowcount


async def _ratchet_trailing_stops(conn: AsyncConnection) -> int:
    """Trailing-stop ratchet, once per tick BEFORE the trigger sweep: a
    SELL trail lifts its stop toward mark - trail_amount (only ever up),
    a BUY trail lowers its toward mark + trail_amount (only ever down).

    The new stop snaps to the tick grid like a placed price, biased one
    grid step AWAY from the mark -- a sub-tick trail can never park the
    stop on the mark and self-trigger. Untriggered orders only (a
    triggered stop already converted). Rows are few (paid unlock), so a
    per-order loop beats SQLizing the 1-2-5 grid."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            SELECT o.id, o.side, o.stop_price, o.trail_amount, i.quoted_price
            FROM orders o
            JOIN instruments i ON i.id = o.instrument_id
            WHERE o.status = 'OPEN' AND o.triggered_tick IS NULL
              AND o.order_type = 'STOP' AND o.trail_amount IS NOT NULL
              AND i.is_active
            """
        )
        rows = await cur.fetchall()
    if not rows:
        return 0
    spread_cfg = await spread_config(conn)
    moved = 0
    async with conn.cursor() as cur:
        for order in rows:
            mark = float(order["quoted_price"])
            grid = engine.tick_size(mark, spread_cfg)
            raw = (
                mark - float(order["trail_amount"])
                if order["side"] == "SELL"
                else mark + float(order["trail_amount"])
            )
            snapped = engine.round_to_tick(raw, grid)
            if order["side"] == "SELL" and snapped >= mark:
                snapped -= grid
            elif order["side"] == "BUY" and snapped <= mark:
                snapped += grid
            if snapped <= 0:
                continue
            new_stop = Decimal(str(round(snapped, 6)))
            old_stop = Decimal(order["stop_price"])
            improves = (
                (order["side"] == "SELL" and new_stop > old_stop)
                or (order["side"] == "BUY" and new_stop < old_stop)
            )
            if not improves:
                continue
            await cur.execute(
                "UPDATE orders SET stop_price = %s "
                "WHERE id = %s AND status = 'OPEN'",
                (new_stop, int(order["id"])),
            )
            moved += cur.rowcount
    return moved


async def match_orders(
    conn: AsyncConnection,
    tick_index: int,
    stats: dict[str, int] | None = None,
    closing_auction: bool = False,
) -> int:
    """Match the book (crosses, then MM fills) for this tick.

    Called inside apply_tick's transaction: all instruments are already
    locked, accounts lock afterwards in ascending id order -- consistent
    with the project's lock ordering. Returns the number of fill events
    (a cross counts once, an MM fill counts once).

    `stats`, when given, accumulates tick telemetry: "crosses",
    "mm_fills", "stops_triggered" (in addition to "fills" = the return).

    Stop orders add an outer loop: triggering converts them to marketable
    orders whose fills move the mark, which can trigger more stops on the
    same tick (a cascade). The loop is bounded by
    order.stop_cascade_max_iters, and `depth_used` enforces the per-tick
    participation budget across iterations so a cascade can't drain more
    depth than one tick allows.

    `closing_auction` (Plan C) is set on the session's last open ticks
    (`session.auction_ticks`): pass 1 becomes a single-price clearing
    auction per book and the MM fallback is restricted to books that had
    no auction -- event-halted names keep their risk-reducing MM path.
    """
    if stats is not None:
        stats.setdefault("crosses", 0)
        stats.setdefault("mm_fills", 0)
        stats.setdefault("stops_triggered", 0)
        stats.setdefault("auction_fills", 0)
    async with conn.cursor() as cur:
        await cur.execute(
            """
            UPDATE orders SET status = 'EXPIRED'
            WHERE status = 'OPEN' AND expires_tick IS NOT NULL
              AND expires_tick <= %s
            RETURNING id
            """,
            (tick_index,),
        )
        expired_ids = [int(r[0]) for r in await cur.fetchall()]
    # OCO legs share an expiry, but a stale one-sided group still cleans up.
    await _cancel_oco_siblings(conn, expired_ids)
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT key, value FROM config WHERE key IN "
            "('cross.collar_pct', 'cross.trade_through_epsilon', "
            " 'order.stop_cascade_max_iters', 'order.max_fill_failures', "
            " 'vol.flow_halt_ticks', 'event.halt_lead_ticks')"
        )
        cfg_rows = {str(r["key"]): float(r["value"]) for r in await cur.fetchall()}
    collar = Decimal(str(cfg_rows.get("cross.collar_pct", 0.02)))
    epsilon = float(cfg_rows.get("cross.trade_through_epsilon", 0.0005))
    cascade_max = int(cfg_rows.get("order.stop_cascade_max_iters", 8))
    max_fill_failures = int(cfg_rows.get("order.max_fill_failures", 5))
    flow_halt_ticks = int(cfg_rows.get("vol.flow_halt_ticks", 2))
    event_halt_lead = int(cfg_rows.get("event.halt_lead_ticks", 10))

    # Per-order, not per-instrument: the participation budget is keyed by
    # order id, so N resting orders on one instrument get N*cap notional
    # per tick (consistent with execute_trade's per-fill cap -- splitting a
    # big order multiplies throughput).
    depth_used: dict[int, int] = {}
    # Tick-open mark per instrument, anchored on first sight. The cross
    # collar compares against this -- not the live mark -- so a chain of
    # colluding pairs can't ratchet the mark 2% per cross (each cross
    # re-anchored the next collar check to the print it just made).
    open_marks: dict[int, float] = {}
    fills = 0
    # Trailing stops re-anchor off this tick's mark before triggers
    # evaluate -- the ratchet runs once per tick, not per cascade pass, so
    # same-tick fills can't chase a stop upward and re-trigger it.
    await _ratchet_trailing_stops(conn)
    triggered = await _trigger_due_stops(conn, tick_index)
    if stats is not None:
        stats["stops_triggered"] += triggered
    for _ in range(cascade_max):
        fills += await _match_once(
            conn,
            tick_index,
            collar=collar,
            epsilon=epsilon,
            depth_used=depth_used,
            open_marks=open_marks,
            max_fill_failures=max_fill_failures,
            flow_halt_ticks=flow_halt_ticks,
            event_halt_lead=event_halt_lead,
            closing_auction=closing_auction,
            stats=stats,
        )
        triggered = await _trigger_due_stops(conn, tick_index)
        if stats is not None:
            stats["stops_triggered"] += triggered
        if triggered == 0:
            break
    return fills


async def _auction_clear(
    conn: AsyncConnection,
    *,
    bids: list[dict[str, Any]],
    asks: list[dict[str, Any]],
    remaining: dict[int, int],
    base_price: float,
    max_impact: float,
    open_mark: float,
    collar: Decimal,
    tick_index: int,
    flow_halt_ticks: int,
    spread_cfg: dict[str, float],
    sellable: dict[int, int],
    stats: dict[str, int] | None,
) -> int:
    """Closing auction (Plan C): clear one book at a single uniform price.

    The clearing price maximizes executable volume over the union of
    resting limit prices plus the tick-open mark; ties break toward the
    open mark, then toward the lower price -- deterministic, no RNG. The
    result is collar-checked against `cross.collar_pct` of the open mark:
    a maximizing price outside the collar is clamped to the collar
    boundary rather than abandoned, so resting off-market orders still
    clear at the boundary price like a real auction's price cap.

    Eligible pairs then settle at THE SAME price via `_settle_cross` --
    the maker/taker split still applies (the earlier order earns the
    rebate) but there's no price improvement to take: an auction is one
    print. Self-matches stay banned; the later order steps past, matching
    pass 1's taker-advances convention.
    """
    tick = engine.tick_size(open_mark, spread_cfg)
    open_mark_d = Decimal(str(open_mark))
    snapped_mark = Decimal(str(engine.round_to_tick(open_mark, tick)))

    def _eff(order: dict[str, Any], marketable: Decimal) -> Decimal:
        return marketable if _marketable(order) else Decimal(order["limit_price"])

    # Candidate prices: every resting limit (already grid-snapped at
    # placement) plus the snapped open mark -- the tie-break anchor must
    # be a candidate so the auction can clear at the mark even when no
    # order rests exactly there.
    candidates = sorted(
        {
            Decimal(o["limit_price"])
            for o in bids + asks
            if o["limit_price"] is not None
        }
        | {snapped_mark}
    )
    if not candidates:
        return 0

    def _volume_at(price: Decimal) -> int:
        demand = sum(
            remaining[int(b["id"])]
            for b in bids
            if _eff(b, Decimal("Infinity")) >= price
        )
        # N4.2: unbacked asks (no allow_short) can only supply what the
        # seller holds -- `sellable` is a per-user pool, so this still
        # overestimates slightly when one user rests several asks, but
        # settle-time clamping keeps that cosmetic, never a short.
        supply = sum(
            min(
                remaining[int(a["id"])],
                sellable.get(int(a["user_id"]), remaining[int(a["id"])]),
            )
            for a in asks
            if _eff(a, Decimal(0)) <= price
        )
        return min(demand, supply)

    best_price: Decimal | None = None
    best_vol = 0
    for price in candidates:
        vol = _volume_at(price)
        if vol == 0:
            continue
        better = vol > best_vol
        if not better and vol == best_vol and best_price is not None:
            # Tie-break: closest to the open mark, then the lower price.
            better = (abs(price - open_mark_d), price) < (
                abs(best_price - open_mark_d),
                best_price,
            )
        if better:
            best_price, best_vol = price, vol
    if best_price is None or best_vol == 0:
        return 0

    clear_price = best_price
    if abs(clear_price - open_mark_d) / open_mark_d > collar:
        boundary = open_mark_d * (Decimal(1) + collar * (1 if clear_price > open_mark_d else -1))
        clear_price = Decimal(
            str(engine.round_to_tick(float(boundary), tick))
        )
        # round_to_tick is nearest-grid: a boundary between grid points
        # can round up to half a tick OUTSIDE the collar. Step one tick
        # back toward the mark so the print never exceeds it.
        if clear_price > open_mark_d and clear_price > boundary:
            clear_price -= Decimal(str(tick))
        elif clear_price < open_mark_d and clear_price < boundary:
            clear_price += Decimal(str(tick))

    fills = 0
    i = j = 0
    mark = open_mark
    while i < len(bids) and j < len(asks):
        bid, ask = bids[i], asks[j]
        # Bids sort by effective limit descending, asks ascending: the
        # first ineligible order on a side ends that side's book.
        if _eff(bid, Decimal("Infinity")) < clear_price:
            break
        if _eff(ask, Decimal(0)) > clear_price:
            break
        if bid["user_id"] == ask["user_id"]:
            # Self-match banned: the later order steps past (same rule as
            # the continuous book -- there is no aggressor in an auction,
            # so the maker convention stands in for one).
            if _is_maker(bid, ask):
                j += 1
            else:
                i += 1
            continue
        quantity = min(remaining[int(bid["id"])], remaining[int(ask["id"])])
        if not ask["allow_short"]:
            quantity = min(quantity, sellable.get(int(ask["user_id"]), 0))
            if quantity <= 0:
                # Unbacked seller -- nothing to clear against; next ask.
                j += 1
                continue
        try:
            async with conn.transaction():
                mark, _impact, halted = await _settle_cross(
                    conn,
                    bid=bid,
                    ask=ask,
                    cross_price=clear_price,
                    quantity=quantity,
                    base_price=base_price,
                    mark=mark,
                    max_impact=max_impact,
                    tick_index=tick_index,
                    flow_halt_ticks=flow_halt_ticks,
                )
        except (TradingError, MarginError, LedgerError, ValueError):
            # A side can't settle: the later order steps past, matching
            # the continuous book's taker-advances rule.
            if _is_maker(bid, ask):
                j += 1
            else:
                i += 1
            continue
        fills += 1
        if stats is not None:
            stats["crosses"] += 1
            stats["auction_fills"] = stats.get("auction_fills", 0) + 1
        remaining[int(bid["id"])] -= quantity
        remaining[int(ask["id"])] -= quantity
        if int(ask["user_id"]) in sellable:
            sellable[int(ask["user_id"])] -= quantity
        if remaining[int(bid["id"])] == 0:
            i += 1
        if remaining[int(ask["id"])] == 0:
            j += 1
        if halted:
            break
    return fills


async def _match_once(
    conn: AsyncConnection,
    tick_index: int,
    *,
    collar: Decimal,
    epsilon: float,
    depth_used: dict[int, int],
    open_marks: dict[int, float],
    max_fill_failures: int,
    flow_halt_ticks: int,
    event_halt_lead: int,
    closing_auction: bool,
    stats: dict[str, int] | None = None,
) -> int:
    """One matching pass over the book: crosses (or the closing auction),
    then MM fallback. `depth_used` accumulates MM-filled shares per order
    across cascade iterations so the participation cap is per-tick, not
    per-pass."""
    fills = 0
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            SELECT o.id, o.user_id, o.season_id, o.side, o.quantity,
                   o.filled_quantity, o.limit_price, o.order_type,
                   o.opened_tick, o.allow_short,
                   o.instrument_id, i.ticker, i.base_price, i.impact,
                   i.quoted_price, i.liquidity, i.lambda_impact, i.max_impact,
                   i.next_halting_event_tick,
                   COALESCE(i.sigma_eff, i.sigma) AS sigma,
                   i.next_event_tick, i.last_halt_end_tick,
                   i.circuit_halted_until_tick
            FROM orders o
            JOIN instruments i ON i.id = o.instrument_id
            WHERE o.status = 'OPEN'
              AND i.is_active
              AND (o.order_type = 'LIMIT' OR o.triggered_tick IS NOT NULL)
            ORDER BY o.instrument_id, o.id
            """,
        )
        open_orders = await cur.fetchall()

    spread_cfg = {
        **await spread_config(conn),
        **await session_config(conn),
        **await flow_config(conn),
    }

    # --- Pass 1: crossing book -------------------------------------------
    # Per (instrument, season scope): bids and asks walk price-time
    # priority; a pair crosses at the maker's price if it's within the
    # collar of the mark. League orders only cross within their season.
    books: dict[tuple[int, Any], dict[str, list[dict[str, Any]]]] = {}
    for order in open_orders:
        if order["circuit_halted_until_tick"] is not None:
            continue  # halted instruments can't cross this tick
        if event_halted(
            order["next_halting_event_tick"], tick_index, event_halt_lead
        ):
            # T1 halt into an earnings print: the book freezes entirely --
            # no risk-reducing exception for crosses (the cross settles
            # order-to-order without a position view). Resting orders that
            # would reduce risk can still MM-fill via pass 2, where
            # execute_trade applies the position-aware gate.
            continue
        key = (int(order["instrument_id"]), order["season_id"])
        books.setdefault(key, {"BUY": [], "SELL": []})[order["side"]].append(order)

    for (_iid, _sid), sides in books.items():
        ref = (sides["BUY"] or sides["SELL"])[0]
        base_price = float(ref["base_price"])
        max_impact = float(ref["max_impact"])
        mark = float(ref["quoted_price"])
        # Effective limit: a marketable (triggered STOP) bid pays anything,
        # a marketable ask takes anything -- they sort first on their side.
        bids = sorted(sides["BUY"], key=_bid_sort_key)
        asks = sorted(sides["SELL"], key=_ask_sort_key)
        remaining = {
            int(o["id"]): int(o["quantity"]) - int(o["filled_quantity"])
            for o in bids + asks
        }
        # N4.2: a SELL without allow_short can only move shares the
        # seller actually holds. Pool per user (all their asks share one
        # position), decremented as crosses settle; a buy leg filling
        # first can't be seen here, so this is a conservative floor.
        sellable: dict[int, int] = {}
        for a in asks:
            uid = int(a["user_id"])
            if not a["allow_short"] and uid not in sellable:
                sellable[uid] = await _sellable_qty(conn, a)
        # Tick-open anchor for the collar: `mark` drifts as crosses print,
        # so comparing each new cross to the live mark lets pairs ratchet
        # the price collar-width at a time. Anchor to the mark this
        # instrument opened the tick with instead.
        open_mark = open_marks.setdefault(_iid, mark)
        if closing_auction:
            # Plan C: on the last open tick the continuous book clears
            # once at a uniform price -- the closing auction.
            fills += await _auction_clear(
                conn,
                bids=bids,
                asks=asks,
                remaining=remaining,
                base_price=base_price,
                max_impact=max_impact,
                open_mark=open_mark,
                collar=collar,
                tick_index=tick_index,
                flow_halt_ticks=flow_halt_ticks,
                spread_cfg=spread_cfg,
                sellable=sellable,
                stats=stats,
            )
            continue
        i = j = 0
        while i < len(bids) and j < len(asks):
            bid, ask = bids[i], asks[j]
            bid_eff = Decimal("Infinity") if _marketable(bid) else bid["limit_price"]
            ask_eff = Decimal(0) if _marketable(ask) else ask["limit_price"]
            if bid_eff < ask_eff:
                break
            bid_maker = _is_maker(bid, ask)
            if bid["user_id"] == ask["user_id"]:
                # Self-match banned: the taker (later order) tries the next
                # counterparty instead.
                if bid_maker:
                    j += 1
                else:
                    i += 1
                continue
            maker = bid if bid_maker else ask
            # The print is the maker's price -- except a marketable order
            # has none, so a marketable maker defers to the counterparty's
            # limit, and two marketable orders cross at the mark.
            if maker["limit_price"] is not None:
                cross_price = Decimal(maker["limit_price"])
            elif (ask if bid_maker else bid)["limit_price"] is not None:
                cross_price = Decimal((ask if bid_maker else bid)["limit_price"])
            else:
                # Two marketable orders have no maker price -- print at the
                # mark, snapped to the grid like every other fill.
                tick = engine.tick_size(mark, spread_cfg)
                cross_price = Decimal(str(engine.round_to_tick(mark, tick)))
            if abs(cross_price - Decimal(str(open_mark))) / Decimal(
                str(open_mark)
            ) > collar:
                # The maker's price is too far off the mark to print --
                # it rests; try the pair without it.
                if bid_maker:
                    i += 1
                else:
                    j += 1
                continue
            quantity = min(remaining[int(bid["id"])], remaining[int(ask["id"])])
            if not ask["allow_short"]:
                quantity = min(quantity, sellable.get(int(ask["user_id"]), 0))
                if quantity <= 0:
                    # Nothing this seller can part with -- the ask is
                    # unbacked, not stale; it stays OPEN and the next
                    # seller gets a shot (position-dependent, like
                    # _LimitBreach: never counts as a fill failure).
                    j += 1
                    continue
            try:
                async with conn.transaction():
                    mark, _impact, halted = await _settle_cross(
                        conn,
                        bid=bid,
                        ask=ask,
                        cross_price=cross_price,
                        quantity=quantity,
                        base_price=base_price,
                        mark=mark,
                        max_impact=max_impact,
                        tick_index=tick_index,
                        flow_halt_ticks=flow_halt_ticks,
                    )
            except (TradingError, MarginError, LedgerError, ValueError):
                # A side can't settle (funds, margin, slots): the taker
                # tries the next counterparty.
                if bid_maker:
                    j += 1
                else:
                    i += 1
                continue
            fills += 1
            if stats is not None:
                stats["crosses"] += 1
            remaining[int(bid["id"])] -= quantity
            remaining[int(ask["id"])] -= quantity
            if int(ask["user_id"]) in sellable:
                sellable[int(ask["user_id"])] -= quantity
            if remaining[int(bid["id"])] == 0:
                i += 1
            if remaining[int(ask["id"])] == 0:
                j += 1
            if halted:
                break

    # --- Pass 2: market-maker fallback ------------------------------------
    # On the closing-auction tick the auction print IS the close, so pass 2
    # only runs for books that had no auction to clear in: event-halted
    # names keep their normal risk-reducing MM path (execute_trade's
    # position-aware gate decides per fill). Without this clause a resting
    # order on a T1-halted name would lose its reduce-only path for the
    # whole auction window -- the halt semantics would silently differ by
    # order type.
    async with conn.cursor(row_factory=dict_row) as cur:
        # A BUY needs quoted <= limit*(1-eps), a SELL quoted >= limit*(1+eps)
        # -- the mark must cross the limit by epsilon, not merely touch it
        # (the crossing book provides the real queue priority; this is the
        # cheap trade-through rule for the infinite-depth MM). A triggered
        # STOP has no limit: it's always a candidate.
        await cur.execute(
            """
            SELECT o.id, o.user_id, o.season_id, o.side, o.quantity,
                   o.filled_quantity, o.limit_price, o.order_type,
                   o.allow_short, o.instrument_id, i.ticker,
                   i.base_price,
                   i.impact, i.liquidity, i.adv, i.lambda_impact, i.max_impact,
                   i.vol_state, i.flow_skew,
                   COALESCE(i.sigma_eff, i.sigma) AS sigma,
                   i.next_event_tick, i.last_halt_end_tick
            FROM orders o
            JOIN instruments i ON i.id = o.instrument_id
            WHERE o.status = 'OPEN'
              AND i.is_active
              AND i.circuit_halted_until_tick IS NULL
              AND (o.order_type = 'LIMIT' OR o.triggered_tick IS NOT NULL)
              AND (o.order_type = 'STOP'
                OR (o.side = 'BUY'  AND i.quoted_price <= o.limit_price * (1 - %s))
                OR (o.side = 'SELL' AND i.quoted_price >= o.limit_price * (1 + %s)))
              AND (NOT %s OR %s >= i.next_halting_event_tick - %s)
            ORDER BY o.id
            """,
            (epsilon, epsilon, closing_auction, tick_index, event_halt_lead),
        )
        candidates = await cur.fetchall()

    cap = await participation_cap(conn)
    for order in candidates:
        remaining_qty = int(order["quantity"]) - int(order["filled_quantity"])
        sign = 1 if order["side"] == "BUY" else -1
        tick = engine.tick_size(float(order["base_price"]), spread_cfg)
        # Participation cap: a resting order works over multiple ticks --
        # fill up to cap*liquidity in notional this tick, the rest next
        # tick. Estimated on the full-size fill price (conservative). The
        # budget is per-tick: `depth_used` carries it across stop-cascade
        # iterations.
        liq_eff = engine.effective_liquidity(
            float(order["liquidity"]),
            float(order["adv"]),
            spread_cfg,
            float(order["vol_state"]),
        )
        # One half-spread for both the cap-estimate and actual-size fills:
        # same order, same side -- the flow skew depends only on direction.
        half_spread = half_spread_for(order, tick_index, spread_cfg)
        half_spread *= engine.flow_skew_mult(
            float(order["base_price"]) * remaining_qty * sign,
            float(order["flow_skew"]),
            spread_cfg,
        )
        fill_full, _ = engine.apply_trade_impact(
            base_price=float(order["base_price"]),
            impact_before=float(order["impact"]),
            signed_notional=float(order["base_price"]) * remaining_qty * sign,
            liquidity=liq_eff,
            lambda_impact=float(order["lambda_impact"]),
            max_impact=float(order["max_impact"]),
            half_spread=half_spread,
            tick_size=tick,
        )
        cap_qty = int(cap * liq_eff / fill_full)
        budget = cap_qty - depth_used.get(int(order["id"]), 0)
        trade_qty = min(remaining_qty, budget)
        if (
            trade_qty >= 1
            and order["side"] == "SELL"
            and not order["allow_short"]
        ):
            # N4.2: an unbacked SELL can only move shares the seller
            # holds -- the rest stays resting (position-dependent skip,
            # never a fill_failure).
            trade_qty = min(trade_qty, await _sellable_qty(conn, order))
        if trade_qty < 1:
            continue
        # Executable price check at the actual size: the fill path includes
        # half-spread and impact; verify the fill respects the limit before
        # committing to the trade. (Triggered STOPs have no limit.)
        fill_f, _ = engine.apply_trade_impact(
            base_price=float(order["base_price"]),
            impact_before=float(order["impact"]),
            signed_notional=float(order["base_price"]) * trade_qty * sign,
            liquidity=liq_eff,
            lambda_impact=float(order["lambda_impact"]),
            max_impact=float(order["max_impact"]),
            half_spread=half_spread,
            tick_size=tick,
        )
        if order["limit_price"] is not None:
            limit = Decimal(order["limit_price"])
            if order["side"] == "BUY" and Decimal(str(fill_f)) > limit:
                continue
            if order["side"] == "SELL" and Decimal(str(fill_f)) < limit:
                continue

        try:
            async with conn.transaction():
                result = await execute_trade(
                    conn,
                    user_id=int(order["user_id"]),
                    ticker=str(order["ticker"]),
                    side=order["side"],
                    quantity=trade_qty,
                    season_id=(
                        int(order["season_id"])
                        if order["season_id"] is not None
                        else None
                    ),
                    order_id=int(order["id"]),
                )
                if order["limit_price"] is not None:
                    # Authoritative limit check on the *actual* fill: the
                    # candidate pre-check ran on a snapshot that's stale by
                    # now (an earlier fill in this pass moved `impact`) and
                    # never modeled the squeeze boost execute_trade adds on
                    # BUYs. Roll back on breach -- the order stays OPEN.
                    limit = Decimal(order["limit_price"])
                    if (
                        order["side"] == "BUY" and result.fill_price > limit
                    ) or (
                        order["side"] == "SELL" and result.fill_price < limit
                    ):
                        raise _LimitBreach(
                            f"order {order['id']} fill {result.fill_price} "
                            f"breached limit {limit}"
                        )
                async with conn.cursor() as cur:
                    await cur.execute(
                        """
                        UPDATE orders
                        SET filled_quantity = filled_quantity + %s,
                            status = CASE
                                WHEN filled_quantity + %s >= quantity THEN 'FILLED'
                                ELSE status END,
                            filled_tick = CASE
                                WHEN filled_quantity + %s >= quantity THEN %s
                                ELSE filled_tick END,
                            fill_price = %s
                        WHERE id = %s AND status = 'OPEN'
                        RETURNING status, quantity
                        """,
                        (trade_qty, trade_qty, trade_qty, tick_index,
                         result.fill_price, order["id"]),
                    )
                    updated = await cur.fetchone()
                    if updated is None:
                        # Cancelled between the candidate snapshot and this
                        # write -- roll the fill back rather than overwrite
                        # 'CANCELLED'.
                        raise ValueError(
                            f"order {order['id']} is no longer open"
                        )
                    if updated[0] == "FILLED":
                        # OCO: the filled leg going terminal kills its
                        # sibling.
                        await _cancel_oco_siblings(conn, [int(order["id"])])
                        # MM fallback: the order's counterparty is always
                        # the market maker -- this side is always taker.
                        await cur.execute(
                            """
                            INSERT INTO notifications (user_id, kind, payload)
                            VALUES (%s, 'ORDER_FILLED', %s)
                            """,
                            (
                                int(order["user_id"]),
                                json.dumps(
                                    {
                                        "tick_index": tick_index,
                                        "order_id": int(order["id"]),
                                        "ticker": str(order["ticker"]),
                                        "side": order["side"],
                                        "qty": int(updated[1]),
                                        "fill": float(result.fill_price),
                                        "maker": False,
                                    }
                                ),
                            ),
                        )
                depth_used[int(order["id"])] = (
                    depth_used.get(int(order["id"]), 0) + trade_qty
                )
                fills += 1
                if stats is not None:
                    stats["mm_fills"] += 1
        except _LimitBreach:
            # Mark-dependent, not deterministic: leave fill_failures alone.
            continue
        except (FeatureDisabledError, MarketClosedError, EventHaltedError):
            # Global transient, not an order-deterministic failure: a kill
            # switch, closed session, or T1 halt window must not burn
            # strikes toward auto-cancel -- the order just waits it out.
            continue
        except (TradingError, MarginError, LedgerError, ValueError):
            # Not fillable right now (funds, margin gates, slot limits) --
            # the savepoint rolls the attempt back and the order stays OPEN.
            # A deterministic failure would retry identically every tick
            # forever, so count strikes and auto-cancel after a few -- a
            # parked zombie order inflates tick latency linearly.
            async with conn.transaction(), conn.cursor() as cur:
                await cur.execute(
                    """
                    UPDATE orders
                    SET fill_failures = fill_failures + 1,
                        status = CASE
                            WHEN fill_failures + 1 >= %s THEN 'CANCELLED'
                            ELSE status END
                    WHERE id = %s
                    RETURNING status
                    """,
                    (max_fill_failures, order["id"]),
                )
                struck = await cur.fetchone()
                if struck is not None and struck[0] == "CANCELLED":
                    # Strike-out is terminal too: an OCO leg that can
                    # never fill can't leave its sibling trading alone.
                    await _cancel_oco_siblings(conn, [int(order["id"])])
            continue

    return fills
