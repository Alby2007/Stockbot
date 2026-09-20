"""Bounded shorts (Phase 1.5): defined-risk bearish positions.

A bounded short of quantity Q at entry fill price P posts collateral
Q*P*m upfront (m = the instrument's ``short_knockout_pct``, e.g. 25%).
Payoff is Q*(P - P_exit), floored at -collateral, and the position
auto-closes (knockout, payout 0) if the quoted price touches P*(1+m).

Negative equity is structurally impossible: worst case is losing the
collateral. No margin calls, no liquidation engine -- that's Phase 2.
MARKET_MAKER is the counterparty throughout, exactly as in share trades.

Money flow:
    open     user -> MARKET_MAKER  collateral (held for the position's life)
             user -> SINK          fee on the notional
    cover    MARKET_MAKER -> user  collateral + payoff, floored at 0
    knockout no transfer           MARKET_MAKER keeps the collateral
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from psycopg import AsyncConnection
from psycopg.rows import dict_row

from stockbot.ledger.service import get_system_account_id, get_user_account_id, post_transfer
from stockbot.margin import service as margin
from stockbot.market import engine
from stockbot.seasons.service import get_active_entry
from stockbot.shorts.errors import ShortNotFoundError
from stockbot.trading.errors import (
    DuplicateInteractionError,
    InstrumentHaltedError,
    NotInLeagueError,
    UnknownInstrumentError,
)
from stockbot.trading.service import FEE_BPS, HALF_SPREAD_BPS, _to_minor_units


@dataclass(frozen=True)
class ShortResult:
    short_id: int
    ticker: str
    quantity: int
    entry_price: Decimal
    knockout_price: Decimal
    collateral_minor: int
    fee_minor: int


@dataclass(frozen=True)
class CoverResult:
    short_id: int
    ticker: str
    quantity: int
    close_price: Decimal
    payoff_minor: int
    payout_minor: int


async def _record_idempotency_key(conn: AsyncConnection, interaction_id: str) -> None:
    async with conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO idempotency_keys (interaction_id) VALUES (%s) ON CONFLICT DO NOTHING",
            (interaction_id,),
        )
        if cur.rowcount == 0:
            raise DuplicateInteractionError(interaction_id)


async def _resolve_account(
    conn: AsyncConnection, user_id: int, season_id: int | None
) -> int:
    if season_id is None:
        return await get_user_account_id(conn, user_id)
    entry = await get_active_entry(conn, user_id, season_id)
    if entry is None:
        raise NotInLeagueError(user_id, season_id)
    return entry[1]


async def _lock_instrument(conn: AsyncConnection, ticker: str) -> dict[str, Any]:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            SELECT id, base_price, impact, liquidity, lambda_impact, max_impact,
                   is_active, circuit_halted_until_tick, short_knockout_pct
            FROM instruments
            WHERE ticker = %s
            FOR UPDATE
            """,
            (ticker,),
        )
        row = await cur.fetchone()
    if row is None or not row["is_active"]:
        raise UnknownInstrumentError(ticker)
    if row["circuit_halted_until_tick"] is not None:
        raise InstrumentHaltedError(ticker)
    return row


async def _set_impact(
    conn: AsyncConnection, instrument_id: int, base_price: float, impact_after: float
) -> Decimal:
    new_quoted = Decimal(str(round(base_price * math.exp(impact_after), 6)))
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE instruments SET impact = %s, quoted_price = %s WHERE id = %s",
            (impact_after, new_quoted, instrument_id),
        )
    return new_quoted


async def _current_tick(conn: AsyncConnection) -> int | None:
    async with conn.cursor() as cur:
        await cur.execute("SELECT MAX(tick_index) FROM market_ticks")
        row = await cur.fetchone()
    return int(row[0]) if row and row[0] is not None else None


async def open_bounded_short(
    conn: AsyncConnection,
    *,
    user_id: int,
    ticker: str,
    quantity: int,
    interaction_id: str | None = None,
    season_id: int | None = None,
) -> ShortResult:
    """Open a bounded short. Lock ordering: instrument, then accounts
    (via post_transfer's ascending-id locks).
    """
    if quantity <= 0:
        raise ValueError("quantity must be positive")
    ticker = ticker.upper()

    async with conn.transaction():
        if interaction_id is not None:
            await _record_idempotency_key(conn, interaction_id)

        instrument = await _lock_instrument(conn, ticker)
        account_id = await _resolve_account(conn, user_id, season_id)

        base_price = float(instrument["base_price"])
        fill_price_f, impact_after = engine.apply_trade_impact(
            base_price=base_price,
            impact_before=float(instrument["impact"]),
            signed_notional=-base_price * quantity,  # sell-side pressure
            liquidity=float(instrument["liquidity"]),
            lambda_impact=float(instrument["lambda_impact"]),
            max_impact=float(instrument["max_impact"]),
            half_spread=float(HALF_SPREAD_BPS) / 10_000,
        )
        fill_price = Decimal(str(round(fill_price_f, 6)))
        knockout_pct = Decimal(str(instrument["short_knockout_pct"]))
        knockout_price = (fill_price * (1 + knockout_pct)).quantize(
            Decimal("0.000001"), rounding=ROUND_HALF_UP
        )

        collateral_minor = _to_minor_units(fill_price * quantity * knockout_pct)
        fee_minor = _to_minor_units(
            (fill_price * quantity * FEE_BPS / 10_000).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
        )
        if collateral_minor <= 0:
            raise ValueError("collateral rounds to zero; increase quantity")
        # The fee is a discretionary equity spend -- don't let it push a
        # margined account below maintenance.
        await margin.assert_spend_ok(conn, user_id, fee_minor, season_id)

        market_maker_id = await get_system_account_id(conn, "MARKET_MAKER")
        sink_id = await get_system_account_id(conn, "SINK")
        await post_transfer(
            conn,
            from_account_id=account_id,
            to_account_id=market_maker_id,
            amount=collateral_minor,
            reason="BSHORT_COLLATERAL",
            memo=ticker,
        )
        if fee_minor > 0:
            await post_transfer(
                conn,
                from_account_id=account_id,
                to_account_id=sink_id,
                amount=fee_minor,
                reason="BSHORT_FEE",
                memo=ticker,
            )

        await _set_impact(conn, int(instrument["id"]), base_price, impact_after)
        tick = await _current_tick(conn)

        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO bounded_shorts
                    (user_id, instrument_id, season_id, quantity, entry_price,
                     knockout_price, collateral_minor, fee_minor, opened_tick)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    user_id,
                    instrument["id"],
                    season_id,
                    quantity,
                    fill_price,
                    knockout_price,
                    collateral_minor,
                    fee_minor,
                    tick,
                ),
            )
            row = await cur.fetchone()
            assert row is not None
            short_id = int(row[0])

    return ShortResult(
        short_id=short_id,
        ticker=ticker,
        quantity=quantity,
        entry_price=fill_price,
        knockout_price=knockout_price,
        collateral_minor=collateral_minor,
        fee_minor=fee_minor,
    )


async def cover_bounded_short(
    conn: AsyncConnection,
    *,
    user_id: int,
    short_id: int,
) -> CoverResult:
    """Close an open bounded short early, at the current impact-adjusted
    (buy-side) fill. Pays out collateral + payoff floored at 0.
    """
    async with conn.transaction():
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT bs.id, bs.user_id, bs.instrument_id, bs.quantity, bs.entry_price,
                       bs.collateral_minor, i.ticker, i.base_price, i.impact, i.liquidity,
                       i.lambda_impact, i.max_impact, i.is_active, bs.season_id
                FROM bounded_shorts bs
                JOIN instruments i ON i.id = bs.instrument_id
                WHERE bs.id = %s AND bs.status = 'OPEN'
                FOR UPDATE OF bs, i
                """,
                (short_id,),
            )
            short = await cur.fetchone()
        if short is None or int(short["user_id"]) != user_id:
            raise ShortNotFoundError(short_id)

        base_price = float(short["base_price"])
        quantity = int(short["quantity"])
        fill_price_f, impact_after = engine.apply_trade_impact(
            base_price=base_price,
            impact_before=float(short["impact"]),
            signed_notional=base_price * quantity,  # buy-side pressure
            liquidity=float(short["liquidity"]),
            lambda_impact=float(short["lambda_impact"]),
            max_impact=float(short["max_impact"]),
            half_spread=float(HALF_SPREAD_BPS) / 10_000,
        )
        close_price = Decimal(str(round(fill_price_f, 6)))

        entry_price = Decimal(short["entry_price"])
        collateral_minor = int(short["collateral_minor"])
        payoff_minor = _to_minor_units((entry_price - close_price) * quantity)
        payout_minor = max(0, collateral_minor + payoff_minor)

        account_id = await _resolve_account(
            conn, user_id, int(short["season_id"]) if short["season_id"] else None
        )
        if payout_minor > 0:
            market_maker_id = await get_system_account_id(conn, "MARKET_MAKER")
            await post_transfer(
                conn,
                from_account_id=market_maker_id,
                to_account_id=account_id,
                amount=payout_minor,
                reason="BSHORT_PAYOUT",
                memo=str(short["ticker"]),
            )

        await _set_impact(conn, int(short["instrument_id"]), base_price, impact_after)
        tick = await _current_tick(conn)

        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE bounded_shorts
                SET status = 'CLOSED', closed_tick = %s, close_price = %s, payout_minor = %s
                WHERE id = %s
                """,
                (tick, close_price, payout_minor, short_id),
            )

    return CoverResult(
        short_id=short_id,
        ticker=str(short["ticker"]),
        quantity=quantity,
        close_price=close_price,
        payoff_minor=payoff_minor,
        payout_minor=payout_minor,
    )


async def sweep_knockouts(conn: AsyncConnection, tick_index: int) -> int:
    """Knock out every open bounded short whose instrument's quoted price has
    reached its knockout level. Called inside apply_tick's transaction after
    instrument prices are written; knockout checks the tick-close price.
    """
    async with conn.cursor() as cur:
        await cur.execute(
            """
            UPDATE bounded_shorts bs
            SET status = 'KNOCKED_OUT', closed_tick = %s,
                close_price = i.quoted_price, payout_minor = 0
            FROM instruments i
            WHERE i.id = bs.instrument_id
              AND bs.status = 'OPEN'
              AND i.quoted_price >= bs.knockout_price
            """,
            (tick_index,),
        )
        return cur.rowcount


async def list_open_shorts(
    conn: AsyncConnection, user_id: int, season_id: int | None = None
) -> list[dict[str, Any]]:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            SELECT bs.id, i.ticker, bs.quantity, bs.entry_price, bs.knockout_price,
                   bs.collateral_minor, i.quoted_price
            FROM bounded_shorts bs
            JOIN instruments i ON i.id = bs.instrument_id
            WHERE bs.user_id = %s AND bs.status = 'OPEN'
              AND bs.season_id IS NOT DISTINCT FROM %s
            ORDER BY bs.id
            """,
            (user_id, season_id),
        )
        return await cur.fetchall()
