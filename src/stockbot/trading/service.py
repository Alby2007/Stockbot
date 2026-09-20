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

from stockbot.ledger.service import get_system_account_id, get_user_account_id, post_transfer
from stockbot.market import engine
from stockbot.shop.service import get_slot_count
from stockbot.trading.errors import (
    DuplicateInteractionError,
    InstrumentHaltedError,
    InsufficientSharesError,
    TooManyPositionsError,
    UnknownInstrumentError,
)

FEE_BPS = Decimal("10")  # 0.10% trading fee, paid to SINK
HALF_SPREAD_BPS = Decimal("5")  # 0.05% half-spread folded into the fill price

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


async def _record_idempotency_key(conn: AsyncConnection, interaction_id: str) -> None:
    async with conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO idempotency_keys (interaction_id) VALUES (%s) ON CONFLICT DO NOTHING",
            (interaction_id,),
        )
        if cur.rowcount == 0:
            raise DuplicateInteractionError(interaction_id)


async def execute_trade(
    conn: AsyncConnection,
    *,
    user_id: int,
    ticker: str,
    side: Side,
    quantity: int,
    interaction_id: str | None = None,
) -> TradeResult:
    if quantity <= 0:
        raise ValueError("quantity must be positive")
    ticker = ticker.upper()

    async with conn.transaction():
        if interaction_id is not None:
            await _record_idempotency_key(conn, interaction_id)

        # Lock ordering: instrument before account.
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT id, base_price, impact, liquidity, lambda_impact, max_impact,
                       is_active, circuit_halted_until_tick
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

        account_id = await get_user_account_id(conn, user_id)
        async with conn.cursor() as cur:
            await cur.execute("SELECT id FROM accounts WHERE id = %s FOR UPDATE", (account_id,))
            await cur.fetchone()

        instrument_id = instrument["id"]
        base_price = float(instrument["base_price"])
        impact_before = float(instrument["impact"])
        liquidity = float(instrument["liquidity"])
        lambda_impact = float(instrument["lambda_impact"])
        max_impact = float(instrument["max_impact"])

        signed_notional = base_price * quantity * (1 if side == "BUY" else -1)
        fill_price_f, impact_after = engine.apply_trade_impact(
            base_price=base_price,
            impact_before=impact_before,
            signed_notional=signed_notional,
            liquidity=liquidity,
            lambda_impact=lambda_impact,
            max_impact=max_impact,
            half_spread=float(HALF_SPREAD_BPS) / 10_000,
        )
        fill_price = Decimal(str(round(fill_price_f, 6)))

        notional = fill_price * quantity
        fee = (notional * FEE_BPS / 10_000).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        notional_minor = _to_minor_units(notional)
        fee_minor = _to_minor_units(fee)

        market_maker_id = await get_system_account_id(conn, "MARKET_MAKER")
        sink_id = await get_system_account_id(conn, "SINK")

        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT quantity, avg_cost FROM positions
                WHERE user_id = %s AND instrument_id = %s
                FOR UPDATE
                """,
                (user_id, instrument_id),
            )
            position = await cur.fetchone()
        held_quantity = int(position[0]) if position else 0
        avg_cost = Decimal(position[1]) if position else Decimal(0)

        if side == "BUY":
            if held_quantity == 0:
                slot_count = await get_slot_count(conn, user_id)
                async with conn.cursor() as cur:
                    await cur.execute(
                        "SELECT COUNT(*) FROM positions WHERE user_id = %s AND quantity > 0",
                        (user_id,),
                    )
                    open_positions_row = await cur.fetchone()
                    assert open_positions_row is not None
                    open_positions = open_positions_row[0]
                if open_positions >= slot_count:
                    raise TooManyPositionsError(open_positions, slot_count)

            cash_transfer_id = await post_transfer(
                conn,
                from_account_id=account_id,
                to_account_id=market_maker_id,
                amount=notional_minor,
                reason="TRADE_BUY",
                memo=ticker,
            )
            new_quantity = held_quantity + quantity
            avg_cost = ((avg_cost * held_quantity) + (fill_price * quantity)) / new_quantity
        else:
            if quantity > held_quantity:
                raise InsufficientSharesError(ticker, quantity, held_quantity)
            cash_transfer_id = await post_transfer(
                conn,
                from_account_id=market_maker_id,
                to_account_id=account_id,
                amount=notional_minor,
                reason="TRADE_SELL",
                memo=ticker,
            )
            new_quantity = held_quantity - quantity
            if new_quantity == 0:
                avg_cost = Decimal(0)

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
                INSERT INTO positions (user_id, instrument_id, quantity, avg_cost, updated_at)
                VALUES (%s, %s, %s, %s, now())
                ON CONFLICT (user_id, instrument_id)
                DO UPDATE SET
                    quantity = EXCLUDED.quantity,
                    avg_cost = EXCLUDED.avg_cost,
                    updated_at = now()
                """,
                (user_id, instrument_id, new_quantity, avg_cost),
            )

            new_quoted_price = Decimal(str(round(base_price * math.exp(impact_after), 6)))
            await cur.execute(
                "UPDATE instruments SET impact = %s, quoted_price = %s WHERE id = %s",
                (impact_after, new_quoted_price, instrument_id),
            )

            await cur.execute(
                """
                INSERT INTO trades (
                    user_id, instrument_id, side, quantity, fill_price,
                    notional_minor, fee_minor, cash_transfer_id, fee_transfer_id
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
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
                ),
            )
            row = await cur.fetchone()
            assert row is not None
            trade_id = row[0]

    return TradeResult(
        trade_id=trade_id,
        ticker=ticker,
        side=side,
        quantity=quantity,
        fill_price=fill_price,
        notional_minor=notional_minor,
        fee_minor=fee_minor,
    )
