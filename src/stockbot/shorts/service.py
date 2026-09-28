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

import json
import math
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from psycopg import AsyncConnection
from psycopg.rows import dict_row

from stockbot.ledger.service import (
    get_system_account_id,
    get_user_account_id,
    post_transfer,
    record_idempotency_key,
)
from stockbot.margin import service as margin
from stockbot.market import engine
from stockbot.market.data import (
    assert_feature_enabled,
    assert_market_open,
    event_halt_lead_ticks,
    event_halted,
    flow_config,
    half_spread_for,
    instrument_session_cfg,
    participation_cap,
    record_flow,
    spread_config,
)
from stockbot.seasons.service import get_active_entry
from stockbot.shorts.errors import ShortNotFoundError
from stockbot.trading.errors import (
    EventHaltedError,
    InstrumentHaltedError,
    InsufficientDepthError,
    NotInLeagueError,
    UnknownInstrumentError,
)
from stockbot.trading.service import (
    _to_minor_units,
    record_volume,
    taker_fee_bps,
    update_candle_with_fill,
)


async def _assert_depth(
    conn: AsyncConnection,
    ticker: str,
    fill_price: Decimal,
    quantity: int,
    liquidity: float,
) -> None:
    """Participation cap for OPENING a bounded short only. Closes never
    come through here: the knockout sweep bypasses the cap by design, and
    `cover_bounded_short` must too -- the cover is atomic (no partial
    close exists), so a cap on it could strand a position forever once
    price drift pushed its notional past the limit."""
    cap = await participation_cap(conn)
    cap_minor = _to_minor_units(Decimal(str(cap)) * Decimal(str(liquidity)))
    notional_minor = _to_minor_units(fill_price * quantity)
    if notional_minor > cap_minor:
        raise InsufficientDepthError(ticker, notional_minor, cap_minor)


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
    """Lock + halt gates for OPENING a bounded short -- the only caller.
    Covering keeps its own ungated path: closes stay legal in a halt."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            SELECT id, base_price, impact, liquidity, adv, lambda_impact, max_impact,
                   is_active, circuit_halted_until_tick, short_knockout_pct,
                   vol_state, flow_skew, next_halting_event_tick,
                   COALESCE(sigma_eff, sigma) AS sigma,
                   next_event_tick, last_halt_end_tick
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
    if event_halted(
        row["next_halting_event_tick"],
        await _current_tick(conn),
        await event_halt_lead_ticks(conn),
    ):
        raise EventHaltedError(ticker, int(row["next_halting_event_tick"]))
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


async def _knockout_bounds(conn: AsyncConnection) -> tuple[Decimal, Decimal]:
    """User-selectable knockout band (shorts.min/max_knockout_pct config)."""
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT key, value FROM config "
            "WHERE key IN ('shorts.min_knockout_pct', 'shorts.max_knockout_pct')"
        )
        rows: dict[str, str] = dict(await cur.fetchall())
    return (
        Decimal(rows.get("shorts.min_knockout_pct", "0.02")),
        Decimal(rows.get("shorts.max_knockout_pct", "0.9999")),
    )


async def open_bounded_short(
    conn: AsyncConnection,
    *,
    user_id: int,
    ticker: str,
    quantity: int,
    interaction_id: str | None = None,
    season_id: int | None = None,
    knockout_pct: Decimal | None = None,
) -> ShortResult:
    """Open a bounded short. Lock ordering: instrument, then accounts
    (via post_transfer's ascending-id locks).

    `knockout_pct` overrides the instrument's default: tighter = less
    collateral but a nearer knockout; wider = more skin for more room.
    """
    if quantity <= 0:
        raise ValueError("quantity must be positive")
    ticker = ticker.upper()

    async with conn.transaction():
        if interaction_id is not None:
            await record_idempotency_key(conn, interaction_id)
        await assert_feature_enabled(conn, "shorts.enabled", "bounded shorts")

        instrument = await _lock_instrument(conn, ticker)
        await assert_market_open(conn, int(instrument["id"]))
        account_id = await _resolve_account(conn, user_id, season_id)

        base_price = float(instrument["base_price"])
        tick = await _current_tick(conn)
        spread_cfg = {
            **await spread_config(conn),
            **await instrument_session_cfg(conn, int(instrument["id"])),
            **await flow_config(conn),
        }
        liq_eff = engine.effective_liquidity(
            float(instrument["liquidity"]),
            float(instrument["adv"]),
            spread_cfg,
            float(instrument["vol_state"]),
        )
        half_spread = half_spread_for(instrument, tick, spread_cfg)
        half_spread *= engine.flow_skew_mult(
            -base_price * quantity, float(instrument["flow_skew"]), spread_cfg
        )
        fill_price_f, impact_after = engine.apply_trade_impact(
            base_price=base_price,
            impact_before=float(instrument["impact"]),
            signed_notional=-base_price * quantity,  # sell-side pressure
            liquidity=liq_eff,
            lambda_impact=float(instrument["lambda_impact"]),
            max_impact=float(instrument["max_impact"]),
            half_spread=half_spread,
            tick_size=engine.tick_size(base_price, spread_cfg),
        )
        fill_price = Decimal(str(round(fill_price_f, 6)))
        await _assert_depth(
            conn, ticker, fill_price, quantity, liq_eff
        )
        if knockout_pct is None:
            knockout_pct = Decimal(str(instrument["short_knockout_pct"]))
        else:
            lo, hi = await _knockout_bounds(conn)
            if not lo <= knockout_pct <= hi:
                raise ValueError(
                    f"knockout must be within {lo * 100:g}%–{hi * 100:g}% "
                    f"of entry (got {knockout_pct * 100:g}%)"
                )
        knockout_price = (fill_price * (1 + knockout_pct)).quantize(
            Decimal("0.000001"), rounding=ROUND_HALF_UP
        )

        collateral_minor = _to_minor_units(fill_price * quantity * knockout_pct)
        # Taker fee at the user's volume tier (Plan E) -- MM is the
        # counterparty, so no maker rebate applies.
        fee_bps = await taker_fee_bps(conn, user_id)
        fee_minor = _to_minor_units(
            (fill_price * quantity * fee_bps / 10_000).quantize(
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
        await update_candle_with_fill(conn, int(instrument["id"]), fill_price, quantity)
        await record_flow(
            conn,
            instrument_id=int(instrument["id"]),
            user_id=user_id,
            delta_impact=impact_after - float(instrument["impact"]),
            signed_notional_minor=-_to_minor_units(fill_price * quantity),
        )
        await record_volume(conn, user_id, _to_minor_units(fill_price * quantity))

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
        # Closes are NOT gated on shorts.enabled: kill switches halt new
        # exposure, they must never trap users in open positions (same
        # reason cancel_order isn't gated). Market session still applies
        # on the short's own venue clock.
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT bs.id, bs.user_id, bs.instrument_id, bs.quantity, bs.entry_price,
                       bs.collateral_minor, i.ticker, i.base_price, i.impact,
                       i.liquidity, i.adv, i.vol_state, i.flow_skew,
                       i.lambda_impact, i.max_impact, i.is_active, bs.season_id,
                       COALESCE(i.sigma_eff, i.sigma) AS sigma,
                       i.next_event_tick, i.last_halt_end_tick
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
        await assert_market_open(conn, int(short["instrument_id"]))

        base_price = float(short["base_price"])
        quantity = int(short["quantity"])
        tick = await _current_tick(conn)
        spread_cfg = {
            **await spread_config(conn),
            **await instrument_session_cfg(conn, int(short["instrument_id"])),
            **await flow_config(conn),
        }
        liq_eff = engine.effective_liquidity(
            float(short["liquidity"]),
            float(short["adv"]),
            spread_cfg,
            float(short["vol_state"]),
        )
        half_spread = half_spread_for(short, tick, spread_cfg)
        half_spread *= engine.flow_skew_mult(
            base_price * quantity, float(short["flow_skew"]), spread_cfg
        )
        fill_price_f, impact_after = engine.apply_trade_impact(
            base_price=base_price,
            impact_before=float(short["impact"]),
            signed_notional=base_price * quantity,  # buy-side pressure
            liquidity=liq_eff,
            lambda_impact=float(short["lambda_impact"]),
            max_impact=float(short["max_impact"]),
            half_spread=half_spread,
            tick_size=engine.tick_size(base_price, spread_cfg),
        )
        close_price = Decimal(str(round(fill_price_f, 6)))
        # No participation cap on the close: covering is risk-reducing,
        # and an oversized position can't be split across smaller covers
        # the way a share position can be chunked -- a cap here would
        # strand it until knockout (same reason _liquidate_leg bypasses).

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
        await update_candle_with_fill(conn, int(short["instrument_id"]), close_price, quantity)
        await record_flow(
            conn,
            instrument_id=int(short["instrument_id"]),
            user_id=user_id,
            delta_impact=impact_after - float(short["impact"]),
            signed_notional_minor=_to_minor_units(close_price * quantity),
        )
        await record_volume(conn, user_id, _to_minor_units(close_price * quantity))

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
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            UPDATE bounded_shorts bs
            SET status = 'KNOCKED_OUT', closed_tick = %s,
                close_price = i.quoted_price, payout_minor = 0
            FROM instruments i
            WHERE i.id = bs.instrument_id
              AND bs.status = 'OPEN'
              AND i.quoted_price >= bs.knockout_price
            RETURNING bs.user_id, i.ticker, bs.quantity, bs.entry_price,
                      bs.knockout_price
            """,
            (tick_index,),
        )
        knocked = await cur.fetchall()
        # Outbox: same transaction as the UPDATE above -- one notification
        # row per short, coalesced by the poller per (user, tick).
        for row in knocked:
            await cur.execute(
                """
                INSERT INTO notifications (user_id, kind, payload)
                VALUES (%s, 'KNOCKOUT', %s)
                """,
                (
                    int(row["user_id"]),
                    json.dumps(
                        {
                            "tick_index": tick_index,
                            "ticker": row["ticker"],
                            "qty": int(row["quantity"]),
                            "entry": float(row["entry_price"]),
                            "ko_price": float(row["knockout_price"]),
                            "payout": 0,
                        }
                    ),
                ),
            )
        return len(knocked)


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
