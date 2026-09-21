"""Buy/sell execution.

Every trade is one transaction, per the project's concurrency rules:

1. optionally record the interaction id (idempotency; replay is a no-op)
2. lock the instrument, then the user's account, in that order (lock
   ordering rule: instruments before accounts)
3. validate, compute the impact-adjusted fill, write the ledger postings
   (cash leg + fee leg), upsert the position, update the instrument's
   impact/quoted price, insert the trade row

`execute_trade` accepts a caller-managed connection so it composes with the
same transaction the caller (e.g. a Discord command handler) is using.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal

from psycopg import AsyncConnection
from psycopg.rows import dict_row

from stockbot.ledger.service import (
    get_system_account_id,
    get_user_account_id,
    post_transfer,
    record_idempotency_key,
)
from stockbot.margin import service as margin
from stockbot.margin.errors import (
    InsufficientMarginError,
    MarginNotUnlockedError,
    PositionLimitError,
    ShortInterestLimitError,
)
from stockbot.market import engine
from stockbot.market.data import (
    assert_feature_enabled,
    assert_market_open,
    current_tick_index,
    half_spread_for,
    participation_cap,
    spread_config,
)
from stockbot.shop.service import BASE_SLOTS, get_slot_count
from stockbot.trading.errors import (
    InstrumentHaltedError,
    InsufficientDepthError,
    NotInLeagueError,
    TooManyPositionsError,
    UnknownInstrumentError,
)

FEE_BPS = Decimal("10")  # 0.10% trading fee, paid to SINK
# The half-spread folded into fills is dynamic now: market.data.half_spread_for
# computes it per instrument from the `spread.*` config namespace (vol,
# liquidity, post-halt decay, event proximity).

Side = Literal["BUY", "SELL"]


@dataclass(frozen=True)
class TradeResult:
    trade_id: int
    ticker: str
    side: Side
    quantity: int
    fill_price: Decimal
    notional_minor: int
    fee_minor: int


def _to_minor_units(major_units: Decimal) -> int:
    return int((major_units * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


async def update_candle_with_fill(
    conn: AsyncConnection, instrument_id: int, fill_price: Decimal, quantity: int
) -> None:
    """Print a fill onto the instrument's most recent candle: extend high/low
    and add to volume. Candles are written once per tick, so without this an
    intra-tick trade would leave no trace on `/chart` (and `volume` would
    stay 0 forever). No-op before the market's first tick.
    """
    async with conn.cursor() as cur:
        await cur.execute(
            """
            UPDATE candles
            SET high = GREATEST(high, %s), low = LEAST(low, %s), volume = volume + %s
            WHERE instrument_id = %s
              AND tick_index = (SELECT MAX(tick_index) FROM market_ticks)
            """,
            (fill_price, fill_price, quantity, instrument_id),
        )


async def _apply_fill(
    conn: AsyncConnection,
    *,
    user_id: int,
    account_id: int,
    instrument_id: int,
    ticker: str,
    side: Side,
    quantity: int,
    fill_price: Decimal,
    season_id: int | None,
    current_tick: int | None,
    cash_leg: Literal["pay", "receive", "none"],
    counterparty_account_id: int,
    cash_transfer_id: str | None = None,
    maker_taker: str | None = None,
    counterparty_user_id: int | None = None,
    order_id: int | None = None,
    half_spread: Decimal | None = None,
    impact_delta: Decimal | None = None,
) -> TradeResult:
    """Settle one side of a fill: cash leg, fee leg, position upsert, trade
    row, and post-fill margin gates.

    `counterparty_account_id` is MARKET_MAKER for market fills, or the
    other user's account for book crosses. `cash_leg` controls who moves
    the notional: "pay" sends it account -> counterparty, "receive" pulls
    it counterparty -> account, and "none" means the caller already moved
    it (a book cross posts one buyer->seller transfer shared by both legs)
    -- in that case `cash_transfer_id` carries the transfer to record.

    Caller must hold the instrument lock and run inside a transaction.
    """
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT id FROM accounts WHERE id = %s FOR UPDATE", (account_id,)
        )
        await cur.fetchone()

    notional = fill_price * quantity
    fee = (notional * FEE_BPS / 10_000).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    notional_minor = _to_minor_units(notional)
    fee_minor = _to_minor_units(fee)

    sink_id = await get_system_account_id(conn, "SINK")

    async with conn.cursor() as cur:
        await cur.execute(
            """
            SELECT quantity, avg_cost, borrow_fees_accrued, dividends_accrued
            FROM positions
            WHERE user_id = %s AND instrument_id = %s
              AND season_id IS NOT DISTINCT FROM %s
            FOR UPDATE
            """,
            (user_id, instrument_id, season_id),
        )
        position = await cur.fetchone()
    held_quantity = int(position[0]) if position else 0
    avg_cost = Decimal(position[1]) if position else Decimal(0)
    accrued = Decimal(position[2]) if position else Decimal(0)
    div_accrued = Decimal(position[3]) if position else Decimal(0)

    if held_quantity == 0:
        # League plays with the base allotment for everyone --
        # purchased slots are a main-economy advantage and the league
        # promise is an equal start.
        slot_count = (
            BASE_SLOTS if season_id is not None else await get_slot_count(conn, user_id)
        )
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT COUNT(*) FROM positions
                WHERE user_id = %s AND quantity <> 0
                  AND season_id IS NOT DISTINCT FROM %s
                """,
                (user_id, season_id),
            )
            open_positions_row = await cur.fetchone()
            assert open_positions_row is not None
            open_positions = open_positions_row[0]
        if open_positions >= slot_count:
            raise TooManyPositionsError(open_positions, slot_count)

    settle_accrued = False
    if side == "BUY":
        if cash_leg == "pay":
            cash_transfer_id = str(
                await post_transfer(
                    conn,
                    from_account_id=account_id,
                    to_account_id=counterparty_account_id,
                    amount=notional_minor,
                    reason="TRADE_BUY",
                    memo=ticker,
                )
            )
        elif cash_leg == "receive":
            cash_transfer_id = str(
                await post_transfer(
                    conn,
                    from_account_id=counterparty_account_id,
                    to_account_id=account_id,
                    amount=notional_minor,
                    reason="TRADE_SELL",
                    memo=ticker,
                )
            )
        new_quantity = held_quantity + quantity
        if held_quantity < 0:
            # Covering a short settles its accrued borrow fees to SINK and
            # its accrued dividend obligations to MARKET_MAKER (who fronts
            # the payouts to longs at the ex-date). Deliberately settles the
            # FULL accrual even on a partial cover (settle_accrued zeroes the
            # columns) -- unlike the liquidation path, which settles
            # proportionally to the closed quantity. Consistent enough: fees
            # restart from zero on the remaining short either way.
            accrued_minor = int(accrued.quantize(Decimal("1"), ROUND_HALF_UP))
            if accrued_minor > 0:
                await post_transfer(
                    conn,
                    from_account_id=account_id,
                    to_account_id=sink_id,
                    amount=accrued_minor,
                    reason="BORROW_FEE",
                    memo=ticker,
                )
            div_minor = int(div_accrued.quantize(Decimal("1"), ROUND_HALF_UP))
            if div_minor > 0:
                mm_id = await get_system_account_id(conn, "MARKET_MAKER")
                await post_transfer(
                    conn,
                    from_account_id=account_id,
                    to_account_id=mm_id,
                    amount=div_minor,
                    reason="DIVIDEND",
                    memo=ticker,
                )
            settle_accrued = True
            # Any remainder beyond the short becomes a fresh long.
            if new_quantity > 0:
                avg_cost = fill_price
        else:
            avg_cost = ((avg_cost * held_quantity) + (fill_price * quantity)) / new_quantity
    else:
        # SELL always credits proceeds -- selling past zero opens a true
        # short (margin-gated below).
        if cash_leg == "pay":
            cash_transfer_id = str(
                await post_transfer(
                    conn,
                    from_account_id=account_id,
                    to_account_id=counterparty_account_id,
                    amount=notional_minor,
                    reason="TRADE_BUY",
                    memo=ticker,
                )
            )
        elif cash_leg == "receive":
            cash_transfer_id = str(
                await post_transfer(
                    conn,
                    from_account_id=counterparty_account_id,
                    to_account_id=account_id,
                    amount=notional_minor,
                    reason="TRADE_SELL",
                    memo=ticker,
                )
            )
        new_quantity = held_quantity - quantity
        if held_quantity <= 0:
            avg_cost = (
                (avg_cost * abs(held_quantity)) + (fill_price * quantity)
            ) / abs(new_quantity)
        elif new_quantity <= 0:
            avg_cost = fill_price
        if new_quantity == 0:
            avg_cost = Decimal(0)
    assert cash_transfer_id is not None

    fee_transfer_id = None
    if fee_minor > 0:
        fee_transfer_id = await post_transfer(
            conn,
            from_account_id=account_id,
            to_account_id=sink_id,
            amount=fee_minor,
            reason="TRADE_FEE",
            memo=ticker,
        )

    async with conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO positions
                (user_id, instrument_id, season_id, quantity, avg_cost, updated_at)
            VALUES (%s, %s, %s, %s, %s, now())
            ON CONFLICT (user_id, instrument_id, season_id)
            DO UPDATE SET
                quantity = EXCLUDED.quantity,
                avg_cost = EXCLUDED.avg_cost,
                borrow_fees_accrued = CASE
                    WHEN EXCLUDED.quantity >= 0 OR %s THEN 0
                    ELSE positions.borrow_fees_accrued
                END,
                dividends_accrued = CASE
                    WHEN EXCLUDED.quantity >= 0 OR %s THEN 0
                    ELSE positions.dividends_accrued
                END,
                updated_at = now()
            """,
            (
                user_id,
                instrument_id,
                season_id,
                new_quantity,
                avg_cost,
                settle_accrued,
                settle_accrued,
            ),
        )

        await cur.execute(
            """
            INSERT INTO trades (
                user_id, instrument_id, side, quantity, fill_price,
                notional_minor, fee_minor, cash_transfer_id, fee_transfer_id,
                tick_index, season_id, maker_taker, counterparty_user_id,
                order_id, half_spread, impact_delta
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                user_id,
                instrument_id,
                side,
                quantity,
                fill_price,
                notional_minor,
                fee_minor,
                cash_transfer_id,
                fee_transfer_id,
                current_tick,
                season_id,
                maker_taker,
                counterparty_user_id,
                order_id,
                half_spread,
                impact_delta,
            ),
        )
        row = await cur.fetchone()
        assert row is not None
        trade_id = row[0]

    # Margin gates, evaluated on the post-trade state (raising rolls the
    # whole trade back). Reducing a position is always allowed.
    short_grew = max(0, -new_quantity) > max(0, -held_quantity)
    gross_grew = abs(new_quantity) > abs(held_quantity)
    if short_grew or gross_grew:
        cfg = await margin.margin_config(conn)
        tier = await margin.margin_tier(conn, user_id, season_id)
        health = await margin.compute_health(conn, user_id, season_id)
        if short_grew:
            if tier < 1:
                raise MarginNotUnlockedError(user_id)
            si_shares, float_shares = await margin._instrument_short_interest(
                conn, instrument_id
            )
            if (
                float_shares > 0
                and Decimal(si_shares)
                > cfg["margin.max_short_interest_pct"] * float_shares
            ):
                raise ShortInterestLimitError(ticker)
            if health.equity_minor < health.init_req_minor:
                raise InsufficientMarginError(
                    health.equity_minor, health.init_req_minor
                )
        if gross_grew:
            cap = margin.leverage_cap(tier, cfg)
            if Decimal(health.gross_notional_minor) > cap * health.equity_minor:
                raise PositionLimitError(
                    health.gross_notional_minor, int(cap * health.equity_minor)
                )

    if season_id is not None:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE season_entries SET trades_count = trades_count + 1
                WHERE season_id = %s AND user_id = %s
                """,
                (season_id, user_id),
            )

    return TradeResult(
        trade_id=trade_id,
        ticker=ticker,
        side=side,
        quantity=quantity,
        fill_price=fill_price,
        notional_minor=notional_minor,
        fee_minor=fee_minor,
    )


async def execute_trade(
    conn: AsyncConnection,
    *,
    user_id: int,
    ticker: str,
    side: Side,
    quantity: int,
    interaction_id: str | None = None,
    season_id: int | None = None,
    order_id: int | None = None,
) -> TradeResult:
    """When `season_id` is set, the trade runs against the user's LEAGUE
    account and a season-scoped position (league portfolio), keeping league
    wealth isolated from the persistent main portfolio. `order_id` links the
    trade row back to the resting order that produced it (MM order fills).
    """
    if quantity <= 0:
        raise ValueError("quantity must be positive")
    ticker = ticker.upper()

    async with conn.transaction():
        if interaction_id is not None:
            await record_idempotency_key(conn, interaction_id)
        await assert_feature_enabled(conn, "trading.enabled", "trading")
        await assert_market_open(conn)

        # Lock ordering: instrument before account.
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT id, base_price, impact, liquidity, lambda_impact, max_impact,
                       is_active, circuit_halted_until_tick, short_interest_pct,
                       sigma, next_event_tick, last_halt_end_tick
                FROM instruments
                WHERE ticker = %s
                FOR UPDATE
                """,
                (ticker,),
            )
            instrument = await cur.fetchone()
        if instrument is None or not instrument["is_active"]:
            raise UnknownInstrumentError(ticker)
        if instrument["circuit_halted_until_tick"] is not None:
            raise InstrumentHaltedError(ticker)
        current_tick = await current_tick_index(conn)

        if season_id is None:
            account_id = await get_user_account_id(conn, user_id)
        else:
            # League-scoped trade: the account must be the user's LEAGUE
            # account in an ACTIVE season. Queried inline rather than via
            # seasons.service -- importing that module here would create a
            # cycle (seasons.errors -> trading.errors -> trading package ->
            # this module).
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
                entry_row = await cur.fetchone()
            if entry_row is None:
                raise NotInLeagueError(user_id, season_id)
            account_id = int(entry_row[0])

        instrument_id = instrument["id"]
        base_price = float(instrument["base_price"])
        impact_before = float(instrument["impact"])
        liquidity = float(instrument["liquidity"])
        lambda_impact = float(instrument["lambda_impact"])
        max_impact = float(instrument["max_impact"])

        # Squeeze mechanic: elevated short interest makes buy-side fills
        # progressively more expensive -- covering gets harder exactly when
        # everyone needs to.
        signed_notional = base_price * quantity * (1 if side == "BUY" else -1)
        if side == "BUY":
            cfg = await margin.margin_config(conn)
            si = Decimal(instrument["short_interest_pct"] or 0)
            over = si - cfg["margin.squeeze_si_threshold"]
            if over > 0:
                lambda_impact *= float(
                    1 + cfg["margin.squeeze_lambda_boost"] * over
                )
        spread_cfg = await spread_config(conn)
        half_spread = half_spread_for(instrument, current_tick, spread_cfg)
        fill_price_f, impact_after = engine.apply_trade_impact(
            base_price=base_price,
            impact_before=impact_before,
            signed_notional=signed_notional,
            liquidity=liquidity,
            lambda_impact=lambda_impact,
            max_impact=max_impact,
            half_spread=half_spread,
        )
        fill_price = Decimal(str(round(fill_price_f, 6)))

        # Participation cap: one marketable fill can't exceed a fraction of
        # the instrument's depth. Crosses don't come through here (the book
        # is its own depth); forced liquidation closes bypass the cap by
        # calling _apply_fill directly via margin's liquidation legs.
        cap = await participation_cap(conn)
        cap_minor = _to_minor_units(Decimal(str(cap)) * Decimal(str(liquidity)))
        notional_minor = _to_minor_units(fill_price * quantity)
        if notional_minor > cap_minor:
            raise InsufficientDepthError(ticker, notional_minor, cap_minor)

        market_maker_id = await get_system_account_id(conn, "MARKET_MAKER")
        result = await _apply_fill(
            conn,
            user_id=user_id,
            account_id=account_id,
            instrument_id=instrument_id,
            ticker=ticker,
            side=side,
            quantity=quantity,
            fill_price=fill_price,
            season_id=season_id,
            current_tick=current_tick,
            cash_leg="pay" if side == "BUY" else "receive",
            counterparty_account_id=market_maker_id,
            order_id=order_id,
            half_spread=Decimal(str(round(half_spread, 8))),
            impact_delta=Decimal(str(impact_after - impact_before)),
        )

        async with conn.cursor() as cur:
            new_quoted_price = Decimal(str(round(base_price * math.exp(impact_after), 6)))
            await cur.execute(
                "UPDATE instruments SET impact = %s, quoted_price = %s WHERE id = %s",
                (impact_after, new_quoted_price, instrument_id),
            )

        await update_candle_with_fill(conn, instrument_id, fill_price, quantity)

    return result
