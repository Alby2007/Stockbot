"""Options: cash-settled CALL/PUT contracts vs MARKET_MAKER (Phase O2).

European-style: buy at the model premium plus markup, sell back at model
minus markdown, or auto-settle at expiry for intrinsic value. Premiums go
to MARKET_MAKER (it carries the liability); the fee leg goes to SINK.

Pricing is the model-consistent closed form in `options.pricing` --
Var[log P_T] = sigma_F^2*T + sigma^2/(2k)(1-e^{-2kT}), NOT sigma*sqrt(T);
the naive form overpriced every listed expiry 5-7x under the seeded OU
calibration.

Margin treatment is deliberately net-worth-only: long option marks count
in `_NET_WORTH_EXPR` (leaderboard/profile) but `compute_health` does not
see them -- options are not margin collateral, matching real brokers and
keeping premium-paid assets from supporting leveraged shorts.

Hedge impact is Phase O3: `options.hedge_frac` seeds at 0. The plumbing
(_hedge_flow) carries the participation cap and records flow under the
real user_id so an admin bumping the config gets a *bounded* ramp, not
the unguarded impact path C2 flagged. Per-user OI sub-cap is still TODO
for O3 -- the aggregate `options.max_oi_frac` cap binds meanwhile.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal
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
    instrument_session_cfg,
    market_cfg,
    markets_map,
    participation_cap,
    record_flow,
    session_config,
    spread_config,
)
from stockbot.market.engine import TICKS_PER_DAY
from stockbot.options.errors import OptionNotFoundError, StrikeOutOfBandError
from stockbot.options.pricing import (
    VolBlend,
    bs_delta,
    bs_price,
    session_variance_ticks,
    total_variance,
)
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
)

# Strikes outside [min_frac, max_frac] x mark are refused at the service
# layer (C7): a $10,000 strike on a $60 stock would price at the 1-cent
# floor with true value ~0 -- junk OI rows and an instant -100% mark.
STRIKE_MIN_FRAC = 0.2
STRIKE_MAX_FRAC = 5.0

EXPIRY_CHOICES_DAYS = (3, 7, 14, 30)


@dataclass(frozen=True)
class BuyResult:
    option_id: int
    ticker: str
    side: str
    strike: Decimal
    expiry_tick: int
    quantity: int
    premium_minor: int  # per share, markup included
    total_cost_minor: int  # premium*qty + fee
    fee_minor: int
    delta: float
    model_minor: int  # fair value per share before markup


@dataclass(frozen=True)
class SellResult:
    option_id: int
    ticker: str
    side: str
    quantity: int
    payout_minor: int
    premium_paid_minor: int


async def _config_float(conn: AsyncConnection, key: str, default: float) -> float:
    async with conn.cursor() as cur:
        await cur.execute("SELECT value FROM config WHERE key = %s", (key,))
        row = await cur.fetchone()
    return float(row[0]) if row else default


async def _current_tick(conn: AsyncConnection) -> int | None:
    async with conn.cursor() as cur:
        await cur.execute("SELECT MAX(tick_index) FROM market_ticks")
        row = await cur.fetchone()
    return int(row[0]) if row and row[0] is not None else None


async def _resolve_account(
    conn: AsyncConnection,
    user_id: int,
    season_id: int | None,
    *,
    require_active: bool = True,
) -> int:
    """Main account, an ACTIVE league entry, or the user's own sandbox.

    One explicit query instead of get_active_entry so sandbox seasons
    (which get_active_entry deliberately excludes) still resolve here.
    `require_active=False` is for payouts on rows whose season may have
    closed (sandbox reset / season end) between buy and settle -- the
    entry row still names the right account.
    """
    if season_id is None:
        return await get_user_account_id(conn, user_id)
    async with conn.cursor() as cur:
        await cur.execute(
            f"""
            SELECT e.account_id
            FROM season_entries e
            JOIN seasons s ON s.id = e.season_id
            WHERE e.user_id = %s AND e.season_id = %s
              {"AND s.status = 'ACTIVE'" if require_active else ""}
              AND (s.sandbox_user_id IS NULL OR s.sandbox_user_id = %s)
            """,
            (user_id, season_id, user_id),
        )
        row = await cur.fetchone()
    if row is None:
        raise NotInLeagueError(user_id, season_id)
    return int(row[0])


async def _lock_instrument(conn: AsyncConnection, ticker: str) -> dict[str, Any]:
    """Lock + halt gates for BUYING an option -- the only opening path.
    Sell-back and settlement keep their own ungated reads: closes stay
    legal in a halt."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            SELECT id, base_price, quoted_price, impact, liquidity, adv,
                   lambda_impact, max_impact, is_active,
                   circuit_halted_until_tick, vol_state, flow_skew,
                   next_halting_event_tick, next_event_tick,
                   last_halt_end_tick, kappa, fundamental_sigma, sigma,
                   COALESCE(sigma_eff, sigma) AS sigma_eff
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


async def _vol_blend(conn: AsyncConnection, instrument: dict[str, Any]) -> VolBlend:
    rho = await _config_float(conn, "vol.rho", 0.94)
    return VolBlend(sigma=float(instrument["sigma"]), rho=rho)


async def _horizon_ticks(
    conn: AsyncConnection, now_tick: int, ticks_left: int, instrument_id: int
) -> tuple[float, float, float]:
    """(accrual, calendar, open) ticks in the pricing horizon, counted on
    the instrument's own venue clock. Post-0033 a closed stretch realizes
    only `overnight_var_frac` of nominal variance -- counting calendar
    ticks wholesale overstates long-dated variance ~30%."""
    return session_variance_ticks(
        now_tick,
        ticks_left,
        await instrument_session_cfg(conn, instrument_id),
    )


def _model_values(
    instrument: dict[str, Any],
    side: str,
    strike: float,
    horizon: tuple[float, float, float],
    vol_cfg: VolBlend,
) -> tuple[float, float]:
    """(fair value per share, delta) at the current mark. `horizon` is
    (accrual, calendar, open) ticks from `_horizon_ticks`."""
    var_t, cal_t, open_t = horizon
    var = total_variance(
        float(instrument["sigma_eff"]),
        float(instrument["kappa"]),
        float(instrument["fundamental_sigma"]),
        cal_t,
        vol_cfg,
        var_ticks=var_t,
        open_ticks=open_t,
    )
    spot = float(instrument["quoted_price"])
    return bs_price(side, spot, strike, var), bs_delta(side, spot, strike, var)


async def _assert_oi(
    conn: AsyncConnection,
    instrument: dict[str, Any],
    ticker: str,
    quantity: int,
    liq_eff: float,
) -> None:
    """Aggregate open-interest cap: total OPEN qty x mark per instrument
    must stay under options.max_oi_frac of effective liquidity. (O3 adds
    a per-user sub-cap once hedge impact is live.)"""
    max_oi_frac = await _config_float(conn, "options.max_oi_frac", 0.5)
    cap_minor = _to_minor_units(
        Decimal(str(max_oi_frac)) * Decimal(str(liq_eff))
    )
    async with conn.cursor() as cur:
        await cur.execute(
            """
            SELECT CAST(ROUND(COALESCE(SUM(o.quantity), 0) * %s * 100) AS BIGINT)
            FROM option_positions o
            WHERE o.instrument_id = %s AND o.status = 'OPEN'
            """,
            (instrument["quoted_price"], instrument["id"]),
        )
        row = await cur.fetchone()
    oi_minor = int(row[0]) if row else 0
    new_minor = _to_minor_units(Decimal(str(instrument["quoted_price"])) * quantity)
    if oi_minor + new_minor > cap_minor:
        raise InsufficientDepthError(ticker, oi_minor + new_minor, cap_minor)


async def _hedge_flow(
    conn: AsyncConnection,
    user_id: int,
    instrument: dict[str, Any],
    signed_delta_shares: float,
    liq_eff: float,
    ticker: str,
) -> None:
    """Delta-hedge order-flow impact (O3, dormant at options.hedge_frac=0).

    Guards per C2: buy-side hedge notional faces the same participation
    cap as every other impact path, and record_flow runs under the real
    user_id so the vol.account_flow_cap guard binds. Negative signed
    delta (sell-backs, put opens) is risk-reducing and skips the cap like
    cover_bounded_short does."""
    hedge_frac = await _config_float(conn, "options.hedge_frac", 0.0)
    if hedge_frac <= 0.0 or signed_delta_shares == 0.0:
        return
    base_price = float(instrument["base_price"])
    spot = float(instrument["quoted_price"])
    notional = hedge_frac * signed_delta_shares * spot
    if notional > 0.0:
        cap = await participation_cap(conn)
        cap_minor = _to_minor_units(Decimal(str(cap)) * Decimal(str(liq_eff)))
        notional_minor = _to_minor_units(Decimal(str(notional)))
        if notional_minor > cap_minor:
            raise InsufficientDepthError(ticker, notional_minor, cap_minor)
    _fill, impact_after = engine.apply_trade_impact(
        base_price=base_price,
        impact_before=float(instrument["impact"]),
        signed_notional=notional,
        liquidity=liq_eff,
        lambda_impact=float(instrument["lambda_impact"]),
        max_impact=float(instrument["max_impact"]),
        half_spread=0.0,  # hedge flow is impact-only: no spread on the leg
    )
    new_quoted = Decimal(str(round(base_price * math.exp(impact_after), 6)))
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE instruments SET impact = %s, quoted_price = %s WHERE id = %s",
            (impact_after, new_quoted, instrument["id"]),
        )
    await record_flow(
        conn,
        instrument_id=int(instrument["id"]),
        user_id=user_id,  # real id -- NOT a system sentinel (C2)
        delta_impact=impact_after - float(instrument["impact"]),
        signed_notional_minor=_to_minor_units(Decimal(str(notional))),
    )


async def buy_option(
    conn: AsyncConnection,
    *,
    user_id: int,
    ticker: str,
    side: str,
    strike: Decimal,
    expiry_days: int,
    quantity: int,
    interaction_id: str | None = None,
    season_id: int | None = None,
) -> BuyResult:
    """Buy CALL/PUT contracts from MARKET_MAKER at BS premium x markup."""
    if quantity <= 0:
        raise ValueError("quantity must be positive")
    if side not in ("CALL", "PUT"):
        raise ValueError("side must be CALL or PUT")
    if expiry_days not in EXPIRY_CHOICES_DAYS:
        raise ValueError(f"expiry must be one of {EXPIRY_CHOICES_DAYS} days")
    if strike <= 0:
        raise ValueError("strike must be positive")
    ticker = ticker.upper()

    async with conn.transaction():
        if interaction_id is not None:
            await record_idempotency_key(conn, interaction_id)
        await assert_feature_enabled(conn, "options.enabled", "options")

        instrument = await _lock_instrument(conn, ticker)
        await assert_market_open(conn, int(instrument["id"]))
        account_id = await _resolve_account(conn, user_id, season_id)

        tick = await _current_tick(conn)
        spot = float(instrument["quoted_price"])
        lo = (await _config_float(conn, "options.strike_min_frac", STRIKE_MIN_FRAC)) * spot
        hi = (await _config_float(conn, "options.strike_max_frac", STRIKE_MAX_FRAC)) * spot
        if not lo <= float(strike) <= hi:
            raise StrikeOutOfBandError(float(strike), lo, hi)

        expiry_tick = (tick or 0) + expiry_days * TICKS_PER_DAY
        ticks_left = expiry_tick - (tick or 0)
        vol_cfg = await _vol_blend(conn, instrument)
        model, delta = _model_values(
            instrument,
            side,
            float(strike),
            await _horizon_ticks(
                conn, tick or 0, ticks_left, int(instrument["id"])
            ),
            vol_cfg,
        )
        markup = await _config_float(conn, "options.premium_markup", 0.10)
        premium_per_share_minor = int(
            (Decimal(str(model * (1 + markup))) * 100).quantize(
                Decimal("1"), rounding=ROUND_CEILING
            )
        )
        min_premium = int(
            await _config_float(conn, "options.min_premium_minor", 1)
        )
        if premium_per_share_minor < min_premium:
            raise ValueError(
                "premium rounds below the per-share minimum; "
                "choose a nearer strike or shorter expiry"
            )
        premium_minor = premium_per_share_minor * quantity

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
        await _assert_oi(conn, instrument, ticker, quantity, liq_eff)

        fee_bps = await taker_fee_bps(conn, user_id)
        fee_minor = _to_minor_units(
            (Decimal(premium_minor) / 100 * fee_bps / 10_000).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
        )
        await margin.assert_spend_ok(
            conn, user_id, premium_minor + fee_minor, season_id
        )

        mm_id = await get_system_account_id(conn, "MARKET_MAKER")
        sink_id = await get_system_account_id(conn, "SINK")
        await post_transfer(
            conn,
            from_account_id=account_id,
            to_account_id=mm_id,
            amount=premium_minor,
            reason="OPTION_PREMIUM",
            memo=f"{ticker} {side} {strike:g} {expiry_days}d",
        )
        if fee_minor > 0:
            await post_transfer(
                conn,
                from_account_id=account_id,
                to_account_id=sink_id,
                amount=fee_minor,
                reason="OPTION_FEE",
                memo=ticker,
            )
        # C7: tier credit tracks the premium actually paid, not delta
        # notional -- delta-basis credit would climb fee tiers ~450x
        # faster than spot trading.
        await record_volume(conn, user_id, premium_minor)
        if season_id is not None:
            # Counts toward MIN_TRADES scoring eligibility like a spot
            # fill -- a pure-options league player must be rankable.
            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE season_entries SET trades_count = trades_count + 1 "
                    "WHERE season_id = %s AND user_id = %s",
                    (season_id, user_id),
                )

        # O3: dormant at hedge_frac=0. Signed buy-side for CALL, sell-side
        # for PUT (the MM hedges opposite the user's exposure).
        await _hedge_flow(
            conn, user_id, instrument, delta * quantity, liq_eff, ticker
        )

        mark_minor = _to_minor_units(Decimal(str(model)))
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO option_positions
                    (user_id, instrument_id, season_id, side, strike,
                     expiry_tick, quantity, premium_minor, mark_minor,
                     opened_tick)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    user_id,
                    instrument["id"],
                    season_id,
                    side,
                    strike,
                    expiry_tick,
                    quantity,
                    premium_per_share_minor,
                    mark_minor,
                    tick or 0,
                ),
            )
            row = await cur.fetchone()
            assert row is not None
            option_id = int(row[0])

    return BuyResult(
        option_id=option_id,
        ticker=ticker,
        side=side,
        strike=strike,
        expiry_tick=expiry_tick,
        quantity=quantity,
        premium_minor=premium_per_share_minor,
        total_cost_minor=premium_minor + fee_minor,
        fee_minor=fee_minor,
        delta=delta,
        model_minor=mark_minor,
    )


async def sell_option(
    conn: AsyncConnection, *, user_id: int, option_id: int
) -> SellResult:
    """Sell the whole open position back to MARKET_MAKER at model x
    (1 - sell_markdown). No partial close -- same atomicity contract as
    cover_bounded_short. NOT gated on options.enabled: closes never trap.
    """
    async with conn.transaction():
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT o.*, i.ticker, i.base_price, i.quoted_price, i.impact,
                       i.liquidity, i.adv, i.vol_state, i.flow_skew,
                       i.lambda_impact, i.max_impact,
                       i.kappa, i.fundamental_sigma, i.sigma,
                       COALESCE(i.sigma_eff, i.sigma) AS sigma_eff
                FROM option_positions o
                JOIN instruments i ON i.id = o.instrument_id
                WHERE o.id = %s AND o.status = 'OPEN'
                FOR UPDATE OF o, i
                """,
                (option_id,),
            )
            opt = await cur.fetchone()
        if opt is None or int(opt["user_id"]) != user_id:
            raise OptionNotFoundError(option_id)
        await assert_market_open(conn, int(opt["instrument_id"]))

        tick = await _current_tick(conn)
        ticks_left = int(opt["expiry_tick"]) - (tick or 0)
        vol_cfg = await _vol_blend(conn, opt)
        model, delta = _model_values(
            opt,
            str(opt["side"]),
            float(opt["strike"]),
            await _horizon_ticks(
                conn, tick or 0, ticks_left, int(opt["instrument_id"])
            ),
            vol_cfg,
        )
        markdown = await _config_float(conn, "options.sell_markdown", 0.12)
        payout_minor = _to_minor_units(
            Decimal(str(model * (1 - markdown))) * int(opt["quantity"])
        )

        account_id = await _resolve_account(
            conn,
            user_id,
            int(opt["season_id"]) if opt["season_id"] is not None else None,
            require_active=False,
        )
        if payout_minor > 0:
            mm_id = await get_system_account_id(conn, "MARKET_MAKER")
            await post_transfer(
                conn,
                from_account_id=mm_id,
                to_account_id=account_id,
                amount=payout_minor,
                reason="OPTION_PAYOUT",
                memo=str(opt["ticker"]),
            )
        await record_volume(conn, user_id, payout_minor)
        # Reverse the open's hedge flow at the current delta (O3-dormant).
        spread_cfg = {
            **await spread_config(conn),
            **await instrument_session_cfg(conn, int(opt["instrument_id"])),
            **await flow_config(conn),
        }
        liq_eff = engine.effective_liquidity(
            float(opt["liquidity"]),
            float(opt["adv"]),
            spread_cfg,
            float(opt["vol_state"]),
        )
        await _hedge_flow(
            conn, user_id, opt, -delta * int(opt["quantity"]), liq_eff,
            str(opt["ticker"]),
        )

        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE option_positions
                SET status = 'SOLD', closed_tick = %s, settlement_minor = %s,
                    mark_minor = %s
                WHERE id = %s
                """,
                (
                    tick,
                    # Per-share close price -- nearest, not floored, so
                    # settlement*qty stays within half a unit of payout.
                    int(
                        (Decimal(payout_minor) / int(opt["quantity"])).quantize(
                            Decimal("1"), rounding=ROUND_HALF_UP
                        )
                    ),
                    _to_minor_units(Decimal(str(model))),
                    option_id,
                ),
            )
        if opt["season_id"] is not None:
            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE season_entries SET trades_count = trades_count + 1 "
                    "WHERE season_id = %s AND user_id = %s",
                    (int(opt["season_id"]), user_id),
                )

    return SellResult(
        option_id=option_id,
        ticker=str(opt["ticker"]),
        side=str(opt["side"]),
        quantity=int(opt["quantity"]),
        payout_minor=payout_minor,
        premium_paid_minor=int(opt["premium_minor"]) * int(opt["quantity"]),
    )


async def settle_expired_options(conn: AsyncConnection, tick_index: int) -> int:
    """Settle every OPEN option at expiry for intrinsic value.

    Runs in BOTH tick phases (C5): on a closed tick the mark is frozen at
    the last open print, so an overnight expiry settles "at the close"
    instead of after the gap -- the gap would be pure luck relative to
    the contract bought. Settlement pays FULL intrinsic; MARKET_MAKER is
    a SYSTEM account and may go negative by design (same as the ADL
    backstop) -- never clamp a payout on a contract the system promised
    to honour (C3)."""
    return await _settle_where(
        conn, tick_index, "AND o.expiry_tick <= %s", (tick_index,)
    )


async def settle_season_options(
    conn: AsyncConnection, season_id: int, tick_index: int
) -> int:
    """Intrinsic-settle every still-open option of a CLOSING season at the
    current mark -- regardless of expiry. Callers run this BEFORE the
    entry balances sweep to SINK: the payout lands in the league account
    and follows league wealth, instead of settling post-close into a
    dead account the sweep already passed."""
    return await _settle_where(
        conn, tick_index, "AND o.season_id = %s", (season_id,)
    )


async def _settle_where(
    conn: AsyncConnection,
    tick_index: int,
    extra_where: str,
    params: tuple[object, ...],
) -> int:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            UPDATE option_positions o
            SET status = 'SETTLED', closed_tick = %s,
                settlement_minor = GREATEST(0, ROUND(
                    CASE o.side WHEN 'CALL'
                        THEN (i.quoted_price - o.strike) * 100
                        ELSE (o.strike - i.quoted_price) * 100
                    END)),
                mark_minor = GREATEST(0, ROUND(
                    CASE o.side WHEN 'CALL'
                        THEN (i.quoted_price - o.strike) * 100
                        ELSE (o.strike - i.quoted_price) * 100
                    END))
            FROM instruments i
            WHERE i.id = o.instrument_id
              AND o.status = 'OPEN'
            """
            + extra_where
            + """
            RETURNING o.id, o.user_id, i.ticker, o.side, o.strike,
                      o.quantity, o.season_id, i.quoted_price,
                      o.settlement_minor
            """,
            (tick_index, *params),
        )
        settled = await cur.fetchall()
        mm_id = await get_system_account_id(conn, "MARKET_MAKER")
        for row in settled:
            payout = int(row["settlement_minor"]) * int(row["quantity"])
            if payout > 0:
                account_id = await _resolve_account(
                    conn,
                    int(row["user_id"]),
                    int(row["season_id"]) if row["season_id"] is not None else None,
                    require_active=False,
                )
                await post_transfer(
                    conn,
                    from_account_id=mm_id,
                    to_account_id=account_id,
                    amount=payout,
                    reason="OPTION_SETTLEMENT",
                    memo=str(row["ticker"]),
                )
            # Outbox: same transaction -- one row per settled contract,
            # coalesced by the poller per (user, tick).
            await cur.execute(
                """
                INSERT INTO notifications (user_id, kind, payload)
                VALUES (%s, 'OPTION_SETTLED', %s)
                """,
                (
                    int(row["user_id"]),
                    json.dumps(
                        {
                            "tick_index": tick_index,
                            "ticker": row["ticker"],
                            "side": row["side"],
                            "strike": float(row["strike"]),
                            "qty": int(row["quantity"]),
                            "settle_price": float(row["quoted_price"]),
                            "payout": payout,
                        }
                    ),
                ),
            )
    return len(settled)


async def reprice_open_options(conn: AsyncConnection, tick_index: int) -> int:
    """Refresh mark_minor (mark-to-model) for every open option. Cheap:
    one SELECT + per-row UPDATE; OI stays small under max_oi_frac.
    Runs in both phases so closed-session marks stay current."""
    rho = await _config_float(conn, "vol.rho", 0.94)
    sess_cfg = await session_config(conn)
    venues = await markets_map(conn)
    venue_cfgs = {mid: market_cfg(m, sess_cfg) for mid, m in venues.items()}
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            SELECT o.id, o.side, o.strike, o.expiry_tick,
                   i.quoted_price, i.kappa, i.fundamental_sigma, i.sigma,
                   i.market_id,
                   COALESCE(i.sigma_eff, i.sigma) AS sigma_eff
            FROM option_positions o
            JOIN instruments i ON i.id = o.instrument_id
            WHERE o.status = 'OPEN'
            """,
        )
        rows = await cur.fetchall()
    for row in rows:
        row_cfg = venue_cfgs.get(int(row["market_id"]), sess_cfg)
        var_t, cal_t, open_t = session_variance_ticks(
            tick_index, int(row["expiry_tick"]) - tick_index, row_cfg
        )
        var = total_variance(
            float(row["sigma_eff"]),
            float(row["kappa"]),
            float(row["fundamental_sigma"]),
            cal_t,
            VolBlend(sigma=float(row["sigma"]), rho=rho),
            var_ticks=var_t,
            open_ticks=open_t,
        )
        model = bs_price(
            str(row["side"]),
            float(row["quoted_price"]),
            float(row["strike"]),
            var,
        )
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE option_positions SET mark_minor = %s WHERE id = %s",
                (_to_minor_units(Decimal(str(model))), row["id"]),
            )
    return len(rows)


async def list_open_options(
    conn: AsyncConnection, user_id: int, season_id: int | None = None
) -> list[dict[str, Any]]:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            SELECT o.id, i.ticker, o.side, o.strike, o.expiry_tick,
                   o.quantity, o.premium_minor, o.mark_minor, o.opened_tick,
                   i.quoted_price
            FROM option_positions o
            JOIN instruments i ON i.id = o.instrument_id
            WHERE o.user_id = %s AND o.status = 'OPEN'
              AND o.season_id IS NOT DISTINCT FROM %s
            ORDER BY o.id
            """,
            (user_id, season_id),
        )
        return await cur.fetchall()


async def option_chain(conn: AsyncConnection, ticker: str) -> dict[str, Any]:
    """Display grid for /options chain: strikes 90-110% of the mark x
    the tradeable expiries, model premium + delta per cell."""
    ticker = ticker.upper()
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            SELECT id, quoted_price, kappa, fundamental_sigma, sigma,
                   COALESCE(sigma_eff, sigma) AS sigma_eff, is_active
            FROM instruments WHERE ticker = %s
            """,
            (ticker,),
        )
        inst = await cur.fetchone()
    if inst is None or not inst["is_active"]:
        raise UnknownInstrumentError(ticker)
    rho = await _config_float(conn, "vol.rho", 0.94)
    markup = await _config_float(conn, "options.premium_markup", 0.10)
    sess_cfg = await instrument_session_cfg(conn, int(inst["id"]))
    tick = await _current_tick(conn)
    vol_cfg = VolBlend(sigma=float(inst["sigma"]), rho=rho)
    spot = float(inst["quoted_price"])

    strikes = [round(spot * f, 2) for f in (0.90, 0.95, 1.00, 1.05, 1.10)]
    cells: dict[str, dict[float, dict[str, float]]] = {"CALL": {}, "PUT": {}}
    for days in EXPIRY_CHOICES_DAYS:
        ticks_left = days * TICKS_PER_DAY
        var_t, cal_t, open_t = session_variance_ticks(
            tick or 0, ticks_left, sess_cfg
        )
        var = total_variance(
            float(inst["sigma_eff"]),
            float(inst["kappa"]),
            float(inst["fundamental_sigma"]),
            cal_t,
            vol_cfg,
            var_ticks=var_t,
            open_ticks=open_t,
        )
        for k in strikes:
            for side in ("CALL", "PUT"):
                model = bs_price(side, spot, k, var)
                cells[side][(days, k)] = {  # type: ignore[index]
                    "premium": model * (1 + markup),
                    "delta": bs_delta(side, spot, k, var),
                }
    return {"ticker": ticker, "spot": spot, "strikes": strikes, "cells": cells}


async def quote_option_premium(
    conn: AsyncConnection,
    ticker: str,
    side: str,
    strike: Decimal,
    expiry_days: int,
) -> int:
    """Per-share premium in minor units for /options buy's `dollars`
    sizing -- a read-only reprice, no lock, no gates. Returns the same
    markup-adjusted number buy_option charges."""
    ticker = ticker.upper()
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            SELECT id, quoted_price, kappa, fundamental_sigma, sigma,
                   COALESCE(sigma_eff, sigma) AS sigma_eff, is_active
            FROM instruments WHERE ticker = %s
            """,
            (ticker,),
        )
        inst = await cur.fetchone()
    if inst is None or not inst["is_active"]:
        raise UnknownInstrumentError(ticker)
    rho = await _config_float(conn, "vol.rho", 0.94)
    markup = await _config_float(conn, "options.premium_markup", 0.10)
    tick = await _current_tick(conn)
    var_t, cal_t, open_t = session_variance_ticks(
        tick or 0,
        expiry_days * TICKS_PER_DAY,
        await instrument_session_cfg(conn, int(inst["id"])),
    )
    var = total_variance(
        float(inst["sigma_eff"]),
        float(inst["kappa"]),
        float(inst["fundamental_sigma"]),
        cal_t,
        VolBlend(sigma=float(inst["sigma"]), rho=rho),
        var_ticks=var_t,
        open_ticks=open_t,
    )
    model = bs_price(side, float(inst["quoted_price"]), float(strike), var)
    # Unfloored: buy_option REJECTS below min_premium rather than charging
    # it -- a floored quote would name a price the buy can't fill.
    return int(
        (Decimal(str(model * (1 + markup))) * 100).quantize(
            Decimal("1"), rounding=ROUND_CEILING
        )
    )
