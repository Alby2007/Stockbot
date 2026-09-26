"""IPO subscriptions: a listing pipeline with a real allocation mechanic.

`create_offering` lists the instrument is_active=FALSE at the offer price
(invisible to the engine, candles, and events until it activates) and opens
a subscription window [open_tick, close_tick). `subscribe` moves committed
cash user -> IPO_ESCROW now (it can't be spent twice) -- subscriptions are
a commitment, not an order. `settle_due` runs inside apply_tick's lifecycle
block on every tick: at close it allocates `shares_offered` pro-rata by
commitment (capped at what each commitment can afford, dust distributed by
largest remainder), burns the proceeds escrow -> SINK (the issuer's take --
the sink side of the offering), refunds the excess, lands positions at the
offer price as avg_cost, and activates the instrument. The listing-day pop
is then ordinary market action.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from psycopg import AsyncConnection
from psycopg.rows import dict_row

from stockbot.admin.service import add_instrument
from stockbot.ledger.service import (
    get_system_account_id,
    get_user_account_id,
    post_transfer,
    record_idempotency_key,
)
from stockbot.margin import service as margin

log = logging.getLogger("stockbot.ipo")


async def _current_tick(conn: AsyncConnection) -> int:
    async with conn.cursor() as cur:
        await cur.execute("SELECT COALESCE(MAX(tick_index), -1) + 1 FROM market_ticks")
        row = await cur.fetchone()
        assert row is not None
        return int(row[0])


async def create_offering(
    conn: AsyncConnection,
    *,
    ticker: str,
    name: str,
    sector_key: str,
    offer_price: float,
    shares_offered: int,
    duration_ticks: int,
    sigma: float | None = None,
    beta: float | None = None,
    gamma: float | None = None,
    liquidity: float | None = None,
) -> int:
    """Create an IPO: instrument lands inactive at the offer price, window
    opens on the next tick. Returns the offering id."""
    if shares_offered <= 0:
        raise ValueError("shares_offered must be positive")
    if duration_ticks <= 0:
        raise ValueError("duration_ticks must be positive")
    offer_minor = int(
        (Decimal(str(offer_price)) * 100).quantize(Decimal("1"), ROUND_HALF_UP)
    )
    if offer_minor <= 0:
        raise ValueError("offer price must be positive")
    async with conn.transaction():
        # add_instrument lands is_active (it validates ticker/sector/params
        # and seeds engine medians); the IPO flip below holds it dormant
        # until settlement -- dormant is just delisted-looking, and the
        # settle path is what flips it live.
        instrument_id = await add_instrument(
            conn,
            ticker=ticker,
            name=name,
            sector_key=sector_key,
            base_price=offer_price,
            sigma=sigma,
            beta=beta,
            gamma=gamma,
            liquidity=liquidity,
        )
        open_tick = await _current_tick(conn)
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT value FROM config WHERE key = 'ipo.short_lockout_ticks'"
            )
            cfg_row = await cur.fetchone()
            lockout = int(cfg_row[0]) if cfg_row else 480
            await cur.execute(
                """
                UPDATE instruments
                SET is_active = FALSE,
                    shortable_after_tick = %s
                WHERE id = %s
                """,
                # Borrow lockout: a fresh listing has no borrow inventory,
                # so margin shorts stay barred for a while after it trades.
                (open_tick + duration_ticks + lockout, instrument_id),
            )
            await cur.execute(
                """
                INSERT INTO ipo_offerings
                    (instrument_id, offer_price_minor, shares_offered,
                     open_tick, close_tick)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    instrument_id,
                    offer_minor,
                    shares_offered,
                    open_tick,
                    open_tick + duration_ticks,
                ),
            )
            row = await cur.fetchone()
            assert row is not None
            return int(row[0])


@dataclass(frozen=True)
class SubscribeResult:
    ticker: str
    committed_minor: int
    total_committed_minor: int
    close_tick: int


async def subscribe(
    conn: AsyncConnection,
    *,
    user_id: int,
    ticker: str,
    amount_minor: int,
    interaction_id: str | None = None,
) -> SubscribeResult:
    """Commit cash to an open IPO window. The debit settles now (escrow),
    the allocation settles at close_tick -- additional commits stack."""
    if amount_minor <= 0:
        raise ValueError("subscription must be positive")
    ticker = ticker.upper()
    async with conn.transaction(), conn.cursor() as cur:
        if interaction_id is not None:
            await record_idempotency_key(conn, interaction_id)
        await cur.execute(
            """
            SELECT o.id, o.open_tick, o.close_tick
            FROM ipo_offerings o
            JOIN instruments i ON i.id = o.instrument_id
            WHERE i.ticker = %s AND o.status = 'OPEN'
            FOR UPDATE OF o
            """,
            (ticker,),
        )
        offering = await cur.fetchone()
        if offering is None:
            raise ValueError(f"no open IPO for {ticker}")
        offering_id, open_tick, close_tick = (int(offering[0]), int(offering[1]), int(offering[2]))
        tick = await _current_tick(conn)
        if tick < open_tick:
            raise ValueError(f"the {ticker} IPO window opens at tick {open_tick}")
        if tick >= close_tick:
            raise ValueError(f"the {ticker} IPO window closed at tick {close_tick}")

        account_id = await get_user_account_id(conn, user_id)
        await margin.assert_spend_ok(conn, user_id, amount_minor)
        escrow = await get_system_account_id(conn, "IPO_ESCROW")
        await post_transfer(
            conn,
            from_account_id=account_id,
            to_account_id=escrow,
            amount=amount_minor,
            reason="IPO_COMMIT",
            memo=ticker,
        )
        await cur.execute(
            """
            INSERT INTO ipo_subscriptions (offering_id, user_id, amount_minor)
            VALUES (%s, %s, %s)
            ON CONFLICT (offering_id, user_id) DO UPDATE
                SET amount_minor = ipo_subscriptions.amount_minor + EXCLUDED.amount_minor,
                    updated_at = now()
            """,
            (offering_id, user_id, amount_minor),
        )
        await cur.execute(
            """
            UPDATE ipo_offerings SET committed_minor = committed_minor + %s
            WHERE id = %s
            RETURNING committed_minor
            """,
            (amount_minor, offering_id),
        )
        total_row = await cur.fetchone()
        assert total_row is not None
    return SubscribeResult(
        ticker=ticker,
        committed_minor=amount_minor,
        total_committed_minor=int(total_row[0]),
        close_tick=close_tick,
    )


async def list_offerings(
    conn: AsyncConnection, user_id: int | None = None
) -> list[dict[str, Any]]:
    """Offerings newest-first, with the caller's own commitment when
    `user_id` is given. OPEN first, then settled/cancelled history."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            SELECT o.id, i.ticker, i.name, o.offer_price_minor, o.shares_offered,
                   o.open_tick, o.close_tick, o.status, o.committed_minor,
                   o.allocated_qty, o.settled_tick,
                   s.amount_minor AS my_committed_minor,
                   s.allocated_qty AS my_allocated_qty,
                   s.refund_minor AS my_refund_minor
            FROM ipo_offerings o
            JOIN instruments i ON i.id = o.instrument_id
            LEFT JOIN ipo_subscriptions s
              ON s.offering_id = o.id AND s.user_id = %s
            ORDER BY (o.status = 'OPEN') DESC, o.id DESC
            LIMIT 15
            """,
            (user_id,),
        )
        return await cur.fetchall()


async def settle_due(conn: AsyncConnection, tick_index: int) -> int:
    """Settle every offering whose close_tick has arrived. Runs inside
    apply_tick's transaction on open AND closed ticks (lifecycle, like
    seasons) -- positions land, escrow unwinds, the instrument activates
    and steps on the next open tick."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            SELECT o.id, o.instrument_id, o.offer_price_minor, o.shares_offered,
                   i.ticker
            FROM ipo_offerings o
            JOIN instruments i ON i.id = o.instrument_id
            WHERE o.status = 'OPEN' AND o.close_tick <= %s
            ORDER BY o.id
            FOR UPDATE OF o
            """,
            (tick_index,),
        )
        due = await cur.fetchall()
    for offering in due:
        await _settle_offering(conn, offering, tick_index)
    if due:
        log.info(
            "ipo settled %d offering(s) at tick %d", len(due), tick_index
        )
    return len(due)


async def _settle_offering(
    conn: AsyncConnection, offering: dict[str, Any], tick_index: int
) -> None:
    offering_id = int(offering["id"])
    instrument_id = int(offering["instrument_id"])
    ticker = str(offering["ticker"])
    price_minor = int(offering["offer_price_minor"])
    shares = int(offering["shares_offered"])
    escrow = await get_system_account_id(conn, "IPO_ESCROW")
    sink = await get_system_account_id(conn, "SINK")

    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            SELECT user_id, amount_minor FROM ipo_subscriptions
            WHERE offering_id = %s ORDER BY user_id
            FOR UPDATE
            """,
            (offering_id,),
        )
        subs = await cur.fetchall()

    if not subs:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE ipo_offerings SET status = 'CANCELLED', settled_tick = %s "
                "WHERE id = %s",
                (tick_index, offering_id),
            )
        return

    total = sum(int(s["amount_minor"]) for s in subs)
    amounts = {int(s["user_id"]): int(s["amount_minor"]) for s in subs}
    # Pro-rata share of the float, capped at what the commitment buys.
    allocs = {
        uid: min(amounts[uid] // price_minor, shares * amounts[uid] // total)
        for uid in amounts
    }
    # Largest-remainder dust pass: one extra share each, biggest fractional
    # loss first (ties by user_id -- deterministic), while it stays
    # affordable.
    leftover = shares - sum(allocs.values())
    ranked = sorted(
        amounts,
        key=lambda uid: (-(shares * amounts[uid] - allocs[uid] * total), uid),
    )
    for uid in ranked:
        if leftover <= 0:
            break
        if (allocs[uid] + 1) * price_minor <= amounts[uid]:
            allocs[uid] += 1
            leftover -= 1

    offer_price = Decimal(price_minor) / 100
    for uid, amt in amounts.items():
        qty = allocs[uid]
        cost = qty * price_minor
        refund = amt - cost
        account_id = await get_user_account_id(conn, uid)
        if cost > 0:
            await post_transfer(
                conn,
                from_account_id=escrow,
                to_account_id=sink,
                amount=cost,
                reason="IPO_SETTLE",
                memo=ticker,
            )
        if refund > 0:
            await post_transfer(
                conn,
                from_account_id=escrow,
                to_account_id=account_id,
                amount=refund,
                reason="IPO_REFUND",
                memo=ticker,
            )
        if qty > 0:
            async with conn.cursor() as cur:
                # Pre-IPO instruments have no positions, but a closed
                # zero-qty row can linger -- the upsert merges into it and
                # the weighted-avg keeps the existing row's cost at 0 when
                # it holds nothing.
                await cur.execute(
                    """
                    INSERT INTO positions
                        (user_id, instrument_id, season_id, quantity, avg_cost,
                         updated_at)
                    VALUES (%s, %s, NULL, %s, %s, now())
                    ON CONFLICT (user_id, instrument_id, season_id)
                    DO UPDATE SET
                        quantity = positions.quantity + EXCLUDED.quantity,
                        avg_cost = CASE
                            WHEN positions.quantity + EXCLUDED.quantity <> 0
                            THEN (positions.avg_cost * positions.quantity
                                  + EXCLUDED.avg_cost * EXCLUDED.quantity)
                                 / (positions.quantity + EXCLUDED.quantity)
                            ELSE EXCLUDED.avg_cost
                        END,
                        updated_at = now()
                    """,
                    (uid, instrument_id, qty, offer_price),
                )
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE ipo_subscriptions
                SET allocated_qty = %s, refund_minor = %s, updated_at = now()
                WHERE offering_id = %s AND user_id = %s
                """,
                (qty, refund, offering_id, uid),
            )
            await cur.execute(
                """
                INSERT INTO notifications (user_id, kind, payload)
                VALUES (%s, 'IPO_SETTLED', %s)
                """,
                (
                    uid,
                    json.dumps(
                        {
                            "tick_index": tick_index,
                            "ticker": ticker,
                            "allocated": qty,
                            "refund": refund,
                            "price": float(offer_price),
                        }
                    ),
                ),
            )

    async with conn.cursor() as cur:
        await cur.execute(
            """
            UPDATE ipo_offerings
            SET status = 'SETTLED', settled_tick = %s, allocated_qty = %s
            WHERE id = %s
            """,
            (tick_index, sum(allocs.values()), offering_id),
        )
        await cur.execute(
            "UPDATE instruments SET is_active = TRUE WHERE id = %s",
            (instrument_id,),
        )
