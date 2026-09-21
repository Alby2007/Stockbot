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

import math
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal

from psycopg import AsyncConnection
from psycopg.rows import dict_row

from stockbot.ledger.errors import LedgerError
from stockbot.ledger.service import (
    get_user_account_id,
    post_transfer,
    record_idempotency_key,
)
from stockbot.margin.errors import MarginError
from stockbot.market import engine
from stockbot.market.data import (
    assert_market_open,
    half_spread_for,
    participation_cap,
    spread_config,
)
from stockbot.trading.errors import NotInLeagueError, TradingError, UnknownInstrumentError
from stockbot.trading.service import (
    _apply_fill,
    _to_minor_units,
    execute_trade,
    update_candle_with_fill,
)

Side = Literal["BUY", "SELL"]
OrderType = Literal["LIMIT", "STOP", "STOP_LIMIT"]


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
) -> OrderResult:
    """Validate and rest an order. Marketability/margin are re-checked at
    fill time; placement only rejects structurally bad orders.

    limit only -> LIMIT; stop only -> STOP (triggers to a marketable
    order); both -> STOP_LIMIT (triggers to a limit order at limit_price).
    """
    if quantity <= 0:
        raise ValueError("quantity must be positive")
    if limit_price is not None and limit_price <= 0:
        raise ValueError("limit price must be positive")
    if stop_price is not None and stop_price <= 0:
        raise ValueError("stop price must be positive")
    order_type: OrderType
    if stop_price is None:
        if limit_price is None:
            raise ValueError("need a limit price, a stop price, or both")
        order_type = "LIMIT"
    else:
        order_type = "STOP_LIMIT" if limit_price is not None else "STOP"
    ticker = ticker.upper()

    async with conn.transaction(), conn.cursor() as cur:
        if interaction_id is not None:
            await record_idempotency_key(conn, interaction_id)
        await assert_market_open(conn)
        await cur.execute(
            "SELECT id, is_active FROM instruments WHERE ticker = %s", (ticker,)
        )
        row = await cur.fetchone()
        if row is None or not row[1]:
            raise UnknownInstrumentError(ticker)
        instrument_id = int(row[0])
        tick = await _current_tick(conn)
        expires_tick = (
            None if expires_in_ticks is None else (tick or 0) + expires_in_ticks
        )
        await cur.execute(
            """
            INSERT INTO orders
                (user_id, instrument_id, season_id, side, quantity,
                 limit_price, opened_tick, expires_tick, order_type, stop_price)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
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
    )


async def cancel_order(conn: AsyncConnection, *, user_id: int, order_id: int) -> bool:
    """Cancel an open order owned by `user_id`. Returns False if not found."""
    async with conn.transaction(), conn.cursor() as cur:
        await cur.execute(
            """
            UPDATE orders SET status = 'CANCELLED'
            WHERE id = %s AND user_id = %s AND status = 'OPEN'
            """,
            (order_id, user_id),
        )
        return cur.rowcount > 0


async def list_open_orders(
    conn: AsyncConnection, user_id: int, season_id: int | None = None
) -> list[dict[str, Any]]:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            SELECT o.id, i.ticker, o.side, o.quantity, o.filled_quantity,
                   o.limit_price, o.order_type, o.stop_price, o.triggered_tick,
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
    # and the per-tick circuit-breaker cap. A breach halts the book.
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
                tick_index + engine.CIRCUIT_HALT_TICKS,
                halted,
                tick_index + engine.CIRCUIT_HALT_TICKS,
                instrument_id,
            ),
        )
        for order in (bid, ask):
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
                WHERE id = %s
                """,
                (quantity, quantity, quantity, tick_index, cross_price, order["id"]),
            )

    await update_candle_with_fill(conn, instrument_id, cross_price, quantity)
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


async def match_orders(conn: AsyncConnection, tick_index: int) -> int:
    """Match the book (crosses, then MM fills) for this tick.

    Called inside apply_tick's transaction: all instruments are already
    locked, accounts lock afterwards in ascending id order -- consistent
    with the project's lock ordering. Returns the number of fill events
    (a cross counts once, an MM fill counts once).

    Stop orders add an outer loop: triggering converts them to marketable
    orders whose fills move the mark, which can trigger more stops on the
    same tick (a cascade). The loop is bounded by
    order.stop_cascade_max_iters, and `depth_used` enforces the per-tick
    participation budget across iterations so a cascade can't drain more
    depth than one tick allows.
    """
    async with conn.cursor() as cur:
        await cur.execute(
            """
            UPDATE orders SET status = 'EXPIRED'
            WHERE status = 'OPEN' AND expires_tick IS NOT NULL
              AND expires_tick <= %s
            """,
            (tick_index,),
        )
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT key, value FROM config WHERE key IN "
            "('cross.collar_pct', 'cross.trade_through_epsilon', "
            " 'order.stop_cascade_max_iters')"
        )
        cfg_rows = {str(r["key"]): float(r["value"]) for r in await cur.fetchall()}
    collar = Decimal(str(cfg_rows.get("cross.collar_pct", 0.02)))
    epsilon = float(cfg_rows.get("cross.trade_through_epsilon", 0.0005))
    cascade_max = int(cfg_rows.get("order.stop_cascade_max_iters", 8))

    depth_used: dict[int, int] = {}
    fills = 0
    await _trigger_due_stops(conn, tick_index)
    for _ in range(cascade_max):
        fills += await _match_once(
            conn, tick_index, collar=collar, epsilon=epsilon, depth_used=depth_used
        )
        if await _trigger_due_stops(conn, tick_index) == 0:
            break
    return fills


async def _match_once(
    conn: AsyncConnection,
    tick_index: int,
    *,
    collar: Decimal,
    epsilon: float,
    depth_used: dict[int, int],
) -> int:
    """One matching pass over the book: crosses, then MM fallback.
    `depth_used` accumulates MM-filled shares per order across cascade
    iterations so the participation cap is per-tick, not per-pass."""
    fills = 0
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            SELECT o.id, o.user_id, o.season_id, o.side, o.quantity,
                   o.filled_quantity, o.limit_price, o.order_type,
                   o.opened_tick,
                   o.instrument_id, i.ticker, i.base_price, i.impact,
                   i.quoted_price, i.liquidity, i.lambda_impact, i.max_impact,
                   i.sigma, i.next_event_tick, i.last_halt_end_tick,
                   i.circuit_halted_until_tick
            FROM orders o
            JOIN instruments i ON i.id = o.instrument_id
            WHERE o.status = 'OPEN'
              AND (o.order_type = 'LIMIT' OR o.triggered_tick IS NOT NULL)
            ORDER BY o.instrument_id, o.id
            """,
        )
        open_orders = await cur.fetchall()

    # --- Pass 1: crossing book -------------------------------------------
    # Per (instrument, season scope): bids and asks walk price-time
    # priority; a pair crosses at the maker's price if it's within the
    # collar of the mark. League orders only cross within their season.
    books: dict[tuple[int, Any], dict[str, list[dict[str, Any]]]] = {}
    for order in open_orders:
        if order["circuit_halted_until_tick"] is not None:
            continue  # halted instruments can't cross this tick
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
                cross_price = Decimal(str(mark))
            if abs(cross_price - Decimal(str(mark))) / Decimal(str(mark)) > collar:
                # The maker's price is too far off the mark to print --
                # it rests; try the pair without it.
                if bid_maker:
                    i += 1
                else:
                    j += 1
                continue
            quantity = min(remaining[int(bid["id"])], remaining[int(ask["id"])])
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
            remaining[int(bid["id"])] -= quantity
            remaining[int(ask["id"])] -= quantity
            if remaining[int(bid["id"])] == 0:
                i += 1
            if remaining[int(ask["id"])] == 0:
                j += 1
            if halted:
                break

    # --- Pass 2: market-maker fallback ------------------------------------
    async with conn.cursor(row_factory=dict_row) as cur:
        # A BUY needs quoted <= limit*(1-eps), a SELL quoted >= limit*(1+eps)
        # -- the mark must cross the limit by epsilon, not merely touch it
        # (the crossing book provides the real queue priority; this is the
        # cheap trade-through rule for the infinite-depth MM). A triggered
        # STOP has no limit: it's always a candidate.
        await cur.execute(
            """
            SELECT o.id, o.user_id, o.season_id, o.side, o.quantity,
                   o.filled_quantity, o.limit_price, o.order_type, i.ticker,
                   i.base_price,
                   i.impact, i.liquidity, i.lambda_impact, i.max_impact,
                   i.sigma, i.next_event_tick, i.last_halt_end_tick
            FROM orders o
            JOIN instruments i ON i.id = o.instrument_id
            WHERE o.status = 'OPEN'
              AND i.circuit_halted_until_tick IS NULL
              AND (o.order_type = 'LIMIT' OR o.triggered_tick IS NOT NULL)
              AND (o.order_type = 'STOP'
                OR (o.side = 'BUY'  AND i.quoted_price <= o.limit_price * (1 - %s))
                OR (o.side = 'SELL' AND i.quoted_price >= o.limit_price * (1 + %s)))
            ORDER BY o.id
            """,
            (epsilon, epsilon),
        )
        candidates = await cur.fetchall()

    spread_cfg = await spread_config(conn)
    cap = await participation_cap(conn)
    for order in candidates:
        remaining_qty = int(order["quantity"]) - int(order["filled_quantity"])
        sign = 1 if order["side"] == "BUY" else -1
        # Participation cap: a resting order works over multiple ticks --
        # fill up to cap*liquidity in notional this tick, the rest next
        # tick. Estimated on the full-size fill price (conservative). The
        # budget is per-tick: `depth_used` carries it across stop-cascade
        # iterations.
        fill_full, _ = engine.apply_trade_impact(
            base_price=float(order["base_price"]),
            impact_before=float(order["impact"]),
            signed_notional=float(order["base_price"]) * remaining_qty * sign,
            liquidity=float(order["liquidity"]),
            lambda_impact=float(order["lambda_impact"]),
            max_impact=float(order["max_impact"]),
            half_spread=half_spread_for(order, tick_index, spread_cfg),
        )
        cap_qty = int(cap * float(order["liquidity"]) / fill_full)
        budget = cap_qty - depth_used.get(int(order["id"]), 0)
        trade_qty = min(remaining_qty, budget)
        if trade_qty < 1:
            continue
        # Executable price check at the actual size: the fill path includes
        # half-spread and impact; verify the fill respects the limit before
        # committing to the trade. (Triggered STOPs have no limit.)
        fill_f, _ = engine.apply_trade_impact(
            base_price=float(order["base_price"]),
            impact_before=float(order["impact"]),
            signed_notional=float(order["base_price"]) * trade_qty * sign,
            liquidity=float(order["liquidity"]),
            lambda_impact=float(order["lambda_impact"]),
            max_impact=float(order["max_impact"]),
            half_spread=half_spread_for(order, tick_index, spread_cfg),
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
                        WHERE id = %s
                        """,
                        (trade_qty, trade_qty, trade_qty, tick_index,
                         result.fill_price, order["id"]),
                    )
                depth_used[int(order["id"])] = (
                    depth_used.get(int(order["id"]), 0) + trade_qty
                )
                fills += 1
        except (TradingError, MarginError, LedgerError, ValueError):
            # Not fillable right now (funds, margin gates, slot limits) --
            # the savepoint rolls the attempt back and the order stays OPEN.
            continue

    return fills
