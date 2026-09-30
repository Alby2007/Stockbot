"""Cross-margin account health and the liquidation engine.

Model (see migrations/0011_margin.sql):

    equity    = cash + SUM(qty * quoted) - accrued_borrow_fees
    maint_req = SUM over short positions of |qty| * quoted * maint_pct
    init_req  = same with init_margin_pct
    ratio     = equity / maint_req          (no shorts => not margined)

Cash itself stays >= 0 (the USER/LEAGUE balance CHECK is preserved): short
proceeds credit to cash and are spendable, and position-opening trades are
gated by post-trade initial margin instead. Only short-holders can breach
maintenance, so the per-tick sweep only has to look at `quantity < 0` rows.

Lock ordering: the tick sweep runs inside apply_tick where every instrument
is already locked, then locks accounts -- consistent with the project rule
(instruments before accounts). The post-trade path (`check_and_liquidate`)
opens its own transaction and locks the account's position instruments in id
order before the account for the same reason.

Backstop: if an account can't be made whole by liquidating its own book,
the insurance fund pays what it can; any residual is absorbed by
MARKET_MAKER (the counterparty to every trade). Both legs are recorded in
`insurance_fund_flows` so the never-negative-equity guarantee is auditable.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal
from typing import Any

from psycopg import AsyncConnection
from psycopg.rows import dict_row

from stockbot.feed import emit_feed
from stockbot.ledger.service import get_system_account_id, post_transfer
from stockbot.margin.errors import MarginSpendBlockedError
from stockbot.market import engine
from stockbot.market.data import (
    flow_config,
    half_spread_for,
    instrument_session_cfg,
    open_market_ids,
    record_flow,
    spread_config,
)

MAX_LIQUIDATION_LEGS = 64


@dataclass(frozen=True)
class MarginHealth:
    account_id: int
    cash_minor: int
    positions_value_minor: int
    bshort_value_minor: int
    accrued_fees_minor: int
    accrued_dividends_minor: int
    equity_minor: int
    maint_req_minor: int
    init_req_minor: int
    gross_notional_minor: int
    short_notional_minor: int

    @property
    def margined(self) -> bool:
        return self.maint_req_minor > 0

    @property
    def ratio(self) -> Decimal | None:
        """equity / maintenance requirement; None when not margined."""
        if self.maint_req_minor <= 0:
            return None
        return Decimal(self.equity_minor) / Decimal(self.maint_req_minor)

    @property
    def undermargined(self) -> bool:
        return self.margined and self.equity_minor < self.maint_req_minor


async def margin_config(conn: AsyncConnection) -> dict[str, Decimal]:
    async with conn.cursor() as cur:
        await cur.execute("SELECT key, value FROM config WHERE key LIKE 'margin.%'")
        cfg = {key: Decimal(value) for key, value in await cur.fetchall()}
    # FEE_SURGE (Margin Call Monday, scheduled chaos) multiplies the
    # borrow rate for its active window. Evaluated at read time off the
    # events table -- expiry drops the row's ACTIVE status, nothing is
    # materialized into `config` to go stale.
    from stockbot.chaos import service as chaos  # lazy: margin <- chaos cycle

    fee_mult = await chaos.active_multiplier(conn, "FEE_SURGE")
    if fee_mult != 1.0 and "margin.borrow_fee_bps_per_tick" in cfg:
        cfg["margin.borrow_fee_bps_per_tick"] *= Decimal(str(fee_mult))
    return cfg


async def margin_tier(conn: AsyncConnection, user_id: int, season_id: int | None) -> int:
    """Margin tier for an account. League accounts get tier 1 unconditionally --
    the league promise is an equal start, and margin is part of the game."""
    if season_id is not None:
        return 1
    async with conn.cursor() as cur:
        await cur.execute(
            """
            SELECT quantity FROM entitlements
            WHERE user_id = %s AND item_key = 'margin_tier'
              AND (expires_at IS NULL OR expires_at > now())
            """,
            (user_id,),
        )
        row = await cur.fetchone()
    return int(row[0]) if row else 0


def leverage_cap(tier: int, cfg: dict[str, Decimal]) -> Decimal:
    """Gross notional / equity cap for a tier: tier N -> N+1x, floored by config."""
    return min(Decimal(tier + 1), cfg["margin.max_gross_leverage"])


async def compute_health(
    conn: AsyncConnection, user_id: int, season_id: int | None = None
) -> MarginHealth:
    """Read-only account health. Positions are marked at quoted_price."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            SELECT a.id AS account_id, a.balance,
                   COALESCE(pv.positions_value, 0) AS positions_value,
                   COALESCE(pv.accrued, 0) AS accrued_fees,
                   COALESCE(pv.accrued_div, 0) AS accrued_div,
                   COALESCE(pv.maint_req, 0) AS maint_req,
                   COALESCE(pv.init_req, 0) AS init_req,
                   COALESCE(pv.gross, 0) AS gross_notional,
                   COALESCE(pv.short_notional, 0) AS short_notional,
                   COALESCE(bv.bshort_value, 0) AS bshort_value
            FROM accounts a
            LEFT JOIN LATERAL (
                SELECT
                    SUM(p.quantity * i.quoted_price * 100) AS positions_value,
                    SUM(p.borrow_fees_accrued) AS accrued,
                    SUM(p.dividends_accrued) AS accrued_div,
                    SUM(CASE WHEN p.quantity < 0
                        THEN -p.quantity * i.quoted_price * i.maint_margin_pct * 100
                        END) AS maint_req,
                    SUM(CASE WHEN p.quantity < 0
                        THEN -p.quantity * i.quoted_price * i.init_margin_pct * 100
                        END) AS init_req,
                    SUM(ABS(p.quantity) * i.quoted_price * 100) AS gross,
                    SUM(CASE WHEN p.quantity < 0
                        THEN -p.quantity * i.quoted_price * 100
                        END) AS short_notional
                FROM positions p
                JOIN instruments i ON i.id = p.instrument_id
                WHERE p.user_id = a.user_id
                  AND p.season_id IS NOT DISTINCT FROM a.season_id
                  AND p.quantity <> 0
            ) pv ON TRUE
            LEFT JOIN LATERAL (
                SELECT SUM(GREATEST(0, bs.collateral_minor
                        + bs.quantity * (bs.entry_price - i.quoted_price) * 100))
                       AS bshort_value
                FROM bounded_shorts bs
                JOIN instruments i ON i.id = bs.instrument_id
                WHERE bs.user_id = a.user_id
                  AND bs.season_id IS NOT DISTINCT FROM a.season_id
                  AND bs.status = 'OPEN'
            ) bv ON TRUE
            WHERE a.user_id = %s
              AND a.season_id IS NOT DISTINCT FROM %s
              AND a.kind <> 'SYSTEM'
            """,
            (user_id, season_id),
        )
        row = await cur.fetchone()
    assert row is not None, f"no account for user {user_id} season {season_id}"

    cash = int(row["balance"])
    positions_value = int(Decimal(row["positions_value"]).quantize(Decimal("1"), ROUND_HALF_UP))
    accrued = int(Decimal(row["accrued_fees"]).quantize(Decimal("1"), ROUND_HALF_UP))
    accrued_div = int(Decimal(row["accrued_div"]).quantize(Decimal("1"), ROUND_HALF_UP))
    bshort_value = int(Decimal(row["bshort_value"]).quantize(Decimal("1"), ROUND_HALF_UP))
    equity = cash + positions_value + bshort_value - accrued - accrued_div

    return MarginHealth(
        account_id=int(row["account_id"]),
        cash_minor=cash,
        positions_value_minor=positions_value,
        bshort_value_minor=bshort_value,
        accrued_fees_minor=accrued,
        accrued_dividends_minor=accrued_div,
        equity_minor=equity,
        maint_req_minor=int(Decimal(row["maint_req"]).quantize(Decimal("1"), ROUND_HALF_UP)),
        init_req_minor=int(Decimal(row["init_req"]).quantize(Decimal("1"), ROUND_HALF_UP)),
        gross_notional_minor=int(
            Decimal(row["gross_notional"]).quantize(Decimal("1"), ROUND_HALF_UP)
        ),
        short_notional_minor=int(
            Decimal(row["short_notional"]).quantize(Decimal("1"), ROUND_HALF_UP)
        ),
    )


async def assert_spend_ok(
    conn: AsyncConnection,
    user_id: int,
    amount_minor: int,
    season_id: int | None = None,
) -> None:
    """Refuse a discretionary spend (shop, league entry) that would leave a
    margined account below maintenance -- spending must not be a way to walk
    away from short exposure."""
    if amount_minor <= 0:
        return
    health = await compute_health(conn, user_id, season_id)
    if health.margined and health.equity_minor - amount_minor < health.maint_req_minor:
        raise MarginSpendBlockedError(
            health.equity_minor - amount_minor, health.maint_req_minor
        )


async def _current_tick(conn: AsyncConnection) -> int | None:
    async with conn.cursor() as cur:
        await cur.execute("SELECT MAX(tick_index) FROM market_ticks")
        row = await cur.fetchone()
    return int(row[0]) if row and row[0] is not None else None


async def _instrument_short_interest(
    conn: AsyncConnection, instrument_id: int
) -> tuple[int, int]:
    """(shares short, float_shares) for an instrument."""
    async with conn.cursor() as cur:
        await cur.execute(
            """
            SELECT COALESCE(-SUM(p.quantity), 0), i.float_shares
            FROM instruments i
            LEFT JOIN positions p
              ON p.instrument_id = i.id AND p.quantity < 0
            WHERE i.id = %s
            GROUP BY i.float_shares
            """,
            (instrument_id,),
        )
        row = await cur.fetchone()
    assert row is not None
    return int(row[0]), int(row[1])


async def _record_fund_flow(
    conn: AsyncConnection,
    amount_minor: int,
    reason: str,
    liquidation_id: int | None,
    tick_index: int | None,
    *,
    mm_absorbed_minor: int = 0,
) -> None:
    """Audit row for a fund movement. `amount_minor` is strictly the fund's
    balance delta (so `fund_balance == SUM(amount_minor)` holds); a loss
    MARKET_MAKER absorbed instead goes on `mm_absorbed_minor` -- the fund
    never paid it, so it must not enter the sum."""
    async with conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO insurance_fund_flows
                (amount_minor, reason, liquidation_id, tick_index, mm_absorbed_minor)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (amount_minor, reason, liquidation_id, tick_index, mm_absorbed_minor),
        )


async def _fund_balance(conn: AsyncConnection, fund_id: int) -> int:
    async with conn.cursor() as cur:
        await cur.execute("SELECT balance FROM accounts WHERE id = %s", (fund_id,))
        row = await cur.fetchone()
    assert row is not None
    return int(row[0])


async def _liquidate_leg(
    conn: AsyncConnection,
    *,
    user_id: int,
    season_id: int | None,
    account_id: int,
    position: dict[str, Any],
    close_qty: int,
    equity_before: int,
    maint_before: int,
    tick_index: int | None,
    penalty_bps: Decimal,
    kind: str = "LIQUIDATION",
) -> None:
    """Force-close `close_qty` of one position at the impact-adjusted price.

    For shorts (BUY leg) the cover cost is paid from cash, then the insurance
    fund, then implicitly by MARKET_MAKER (recorded as an ADL flow). For longs
    (SELL leg) MARKET_MAKER pays out proceeds. The penalty is taken from the
    account's cash when it can cover it.

    `kind="RECALL"` reuses the same settlement for a borrow recall: caller
    passes penalty_bps=0 and this leg cash-caps close_qty at the ACTUAL
    fill's per-share cost (so the fund/ADL backstop is unreachable in fact,
    not just by the caller's estimate), the cover posts as RECALL_COVER,
    no `liquidations` row is written, and the user gets SHORT_RECALL
    instead of LIQUIDATION.
    """
    # Lazy: trading.service imports this module, so a top-level import here
    # would be circular.
    from stockbot.trading.service import FEE_BPS, update_candle_with_fill

    instrument_id = int(position["instrument_id"])
    venue_cfg = await instrument_session_cfg(conn, instrument_id)
    qty = int(position["quantity"])
    base_price = float(position["base_price"])
    signed = base_price * close_qty * (1 if qty < 0 else -1)
    # Deliberately no participation-cap check here: this is a FORCED close.
    # A capped leg would leave an oversized position un-liquidatable and
    # wedge the account below maintenance forever -- depth protection is
    # for user-initiated fills (execute_trade, shorts), not the safety net.
    spread_cfg = {
        **await spread_config(conn),
        **venue_cfg,
        **await flow_config(conn),
    }
    half_spread = half_spread_for(position, tick_index, spread_cfg)
    half_spread *= engine.flow_skew_mult(
        signed, float(position["flow_skew"] or 0.0), spread_cfg
    )
    fill_f, impact_after = engine.apply_trade_impact(
        base_price=base_price,
        impact_before=float(position["impact"]),
        signed_notional=signed,
        liquidity=engine.effective_liquidity(
            float(position["liquidity"]),
            float(position["adv"]),
            spread_cfg,
            float(position["vol_state"] or 1.0),
        ),
        lambda_impact=float(position["lambda_impact"]),
        max_impact=float(position["max_impact"]),
        half_spread=half_spread,
        tick_size=engine.tick_size(base_price, spread_cfg),
    )
    fill = Decimal(str(round(fill_f, 6)))
    notional_minor = int((fill * close_qty * 100).quantize(Decimal("1"), ROUND_HALF_UP))
    fee_minor = int(
        (notional_minor * FEE_BPS / 10_000).quantize(Decimal("1"), ROUND_HALF_UP)
    )
    penalty_minor = int(
        (notional_minor * penalty_bps / 10_000).quantize(Decimal("1"), ROUND_HALF_UP)
    )

    mm_id = await get_system_account_id(conn, "MARKET_MAKER")
    sink_id = await get_system_account_id(conn, "SINK")
    fund_id = await get_system_account_id(conn, "INSURANCE_FUND")

    liquidation_id: int | None = None
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT balance FROM accounts WHERE id = %s FOR UPDATE", (account_id,)
        )
        cash_row = await cur.fetchone()
        assert cash_row is not None
        cash = int(cash_row[0])

        fund_pays = 0
        residual = 0
        shortfall = 0
        if qty < 0 and kind != "LIQUIDATION":
            # Cash-capped cover at the REAL per-share cost. The caller's
            # estimate is only a bound guess (it omits the half-spread and
            # uses the linear impact bound, not the exp); when it
            # undershoots, shortfall>0 below would draw the insurance fund
            # for a recall -- unaudited, since flow rows are
            # LIQUIDATION-gated. Clamping here makes the backstop
            # unreachable in fact. A smaller close_qty only lowers the
            # fill (less buy pressure), so this bound stays conservative
            # even though `fill` was priced at the larger quantity.
            per_share_minor = int(
                (fill * 100 * (1 + FEE_BPS / 10_000)).to_integral_value(
                    rounding=ROUND_CEILING
                )
            )
            affordable = cash // per_share_minor if per_share_minor > 0 else 0
            if close_qty > affordable:
                close_qty = int(affordable)
                if close_qty <= 0:
                    return
                notional_minor = int(
                    (fill * close_qty * 100).quantize(Decimal("1"), ROUND_HALF_UP)
                )
                fee_minor = int(
                    (notional_minor * FEE_BPS / 10_000).quantize(
                        Decimal("1"), ROUND_HALF_UP
                    )
                )
        if qty < 0:
            # Covering a short: cash leaves the account.
            cost = notional_minor + fee_minor
            user_pays = min(cash, cost)
            shortfall = cost - user_pays
            if user_pays > 0:
                await post_transfer(
                    conn,
                    from_account_id=account_id,
                    to_account_id=mm_id,
                    amount=user_pays,
                    reason="LIQ_COVER" if kind == "LIQUIDATION" else "RECALL_COVER",
                    memo=str(position["ticker"]),
                )
            if shortfall > 0:
                fund_pays = min(await _fund_balance(conn, fund_id), shortfall)
                if fund_pays > 0:
                    await post_transfer(
                        conn,
                        from_account_id=fund_id,
                        to_account_id=mm_id,
                        amount=fund_pays,
                        reason="LIQ_COVER_FUND",
                        memo=str(position["ticker"]),
                    )
                residual = shortfall - fund_pays
                if residual > 0:
                    # MARKET_MAKER implicitly absorbs the rest: it receives
                    # less than the shares are worth. Recorded as an ADL flow.
                    pass
            cash -= user_pays
        else:
            # Selling a long: MARKET_MAKER pays proceeds into the account.
            proceeds = max(0, notional_minor - fee_minor)
            await post_transfer(
                conn,
                from_account_id=mm_id,
                to_account_id=account_id,
                amount=proceeds,
                reason="LIQ_SELL",
                memo=str(position["ticker"]),
            )
            cash += proceeds

        # Liquidation penalty -> insurance fund, limited by cash on hand.
        paid_penalty = min(penalty_minor, cash)
        if paid_penalty > 0:
            await post_transfer(
                conn,
                from_account_id=account_id,
                to_account_id=fund_id,
                amount=paid_penalty,
                reason="LIQ_PENALTY",
                memo=str(position["ticker"]),
            )
            cash -= paid_penalty

        # Settle accrued borrow fees (to SINK) and dividend obligations
        # (to MARKET_MAKER) proportionally on the closed quantity.
        accrued = Decimal(position["borrow_fees_accrued"])
        settled_minor = 0
        div_accrued = Decimal(position["dividends_accrued"])
        if qty < 0 and accrued > 0:
            settle = accrued * Decimal(close_qty) / Decimal(-qty)
            settled_minor = int(settle.quantize(Decimal("1"), ROUND_HALF_UP))
            paid = min(settled_minor, cash)
            if paid > 0:
                await post_transfer(
                    conn,
                    from_account_id=account_id,
                    to_account_id=sink_id,
                    amount=paid,
                    reason="BORROW_FEE",
                    memo=str(position["ticker"]),
                )
            accrued -= settle
        if qty < 0 and div_accrued > 0:
            div_settle = div_accrued * Decimal(close_qty) / Decimal(-qty)
            div_minor = int(div_settle.quantize(Decimal("1"), ROUND_HALF_UP))
            div_paid = min(div_minor, cash)
            if div_paid > 0:
                await post_transfer(
                    conn,
                    from_account_id=account_id,
                    to_account_id=mm_id,
                    amount=div_paid,
                    reason="DIVIDEND",
                    memo=str(position["ticker"]),
                )
            div_accrued -= div_settle

        new_qty = qty + close_qty if qty < 0 else qty - close_qty
        await cur.execute(
            """
            UPDATE positions
            SET quantity = %s,
                avg_cost = CASE WHEN %s = 0 THEN 0 ELSE avg_cost END,
                borrow_fees_accrued = CASE WHEN %s = 0 THEN 0 ELSE %s END,
                dividends_accrued = CASE WHEN %s = 0 THEN 0 ELSE %s END,
                updated_at = now()
            WHERE id = %s
            """,
            (new_qty, new_qty, new_qty, accrued, new_qty, div_accrued, position["id"]),
        )

        new_quoted = Decimal(str(round(base_price * math.exp(impact_after), 6)))
        await cur.execute(
            "UPDATE instruments SET impact = %s, quoted_price = %s WHERE id = %s",
            (impact_after, new_quoted, instrument_id),
        )

        await update_candle_with_fill(conn, instrument_id, fill, close_qty)
        # Forced flow: recorded under the reserved system user_id 0 -- it
        # counts toward the tick's flow cap but no single account's cap.
        await record_flow(
            conn,
            instrument_id=instrument_id,
            user_id=0,
            delta_impact=impact_after - float(position["impact"]),
            signed_notional_minor=(
                notional_minor if qty < 0 else -notional_minor
            ),
        )

        if kind == "LIQUIDATION":
            await cur.execute(
                """
                INSERT INTO liquidations (
                    user_id, season_id, account_id, instrument_id, side,
                    quantity_closed, fill_price, notional_minor, penalty_minor,
                    equity_before_minor, maint_req_before_minor, tick_index
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    user_id,
                    season_id,
                    account_id,
                    instrument_id,
                    "BUY" if qty < 0 else "SELL",
                    close_qty,
                    fill,
                    notional_minor,
                    paid_penalty,
                    equity_before,
                    maint_before,
                    tick_index,
                ),
            )
            row = await cur.fetchone()
            assert row is not None
            liquidation_id = int(row[0])

        # Outbox: written in the same transaction as the liquidation/recall
        # above, so a rollback can never leave a notification for an
        # event that didn't happen. The poller coalesces per tick_index
        # -- a multi-leg liquidation sends one DM, not N.
        payload: dict[str, Any] = {
            "tick_index": tick_index,
            "ticker": position["ticker"],
            "side": "BUY" if qty < 0 else "SELL",
            "qty": close_qty,
            "fill": float(fill),
        }
        if kind == "LIQUIDATION":
            payload["penalty"] = paid_penalty
            payload["equity_before"] = equity_before
        await cur.execute(
            """
            INSERT INTO notifications (user_id, kind, payload)
            VALUES (%s, %s, %s)
            """,
            (user_id, "LIQUIDATION" if kind == "LIQUIDATION" else "SHORT_RECALL",
             json.dumps(payload)),
        )
        if season_id is None:
            # Public tape: one row per leg, coalesced per (user, tick) by
            # the poller. League stakes are faucet-seeded -- a league
            # liquidation isn't main-tape drama.
            await emit_feed(
                conn,
                "LIQUIDATION" if kind == "LIQUIDATION" else "SQUEEZE",
                payload,
                user_id=user_id,
                tick_index=tick_index,
            )

    if kind != "LIQUIDATION":
        return
    if paid_penalty > 0:
        await _record_fund_flow(
            conn, paid_penalty, "LIQUIDATION_PENALTY", liquidation_id, tick_index
        )
    if qty < 0:
        if shortfall > 0 and fund_pays > 0:
            await _record_fund_flow(
                conn, -fund_pays, "COVER_SHORTFALL", liquidation_id, tick_index
            )
        if shortfall > 0 and residual > 0:
            # amount_minor stays 0: the fund paid nothing here -- the
            # residual is MARKET_MAKER's loss, tracked on its own column.
            await _record_fund_flow(
                conn,
                0,
                "ADL",
                liquidation_id,
                tick_index,
                mm_absorbed_minor=residual,
            )


async def _liquidate_account(
    conn: AsyncConnection,
    *,
    user_id: int,
    season_id: int | None,
    account_id: int,
    tick_index: int | None,
    open_market_ids: set[int] | None = None,
) -> int:
    """Reduce an undermargined account until ratio >= target or it has no
    positions left. Returns the number of liquidation legs executed.

    `open_market_ids` (regional markets R3) scopes liquidation legs to
    venues open this tick: a closed venue's frozen mark can't fill, so
    those positions are skipped and the account stays undermargined --
    the sweep retries it next tick (deferral is the sweep itself). The
    insurance-fund/ADL backstop likewise defers while any closed-venue
    short remains unliquidatable. None = every venue eligible.

    Assumes the caller's transaction already holds locks on the account's
    position instruments (true inside apply_tick and in check_and_liquidate).
    """
    from stockbot.trading.service import FEE_BPS  # lazy: circular at top level

    _mids = (
        sorted(open_market_ids) if open_market_ids is not None else None
    )
    cfg = await margin_config(conn)
    target = cfg["margin.liquidation_target_ratio"]
    penalty_bps = cfg["margin.liquidation_penalty_bps"]

    legs = 0
    for _ in range(MAX_LIQUIDATION_LEGS):
        health = await compute_health(conn, user_id, season_id)
        if not health.undermargined:
            break

        async with conn.cursor(row_factory=dict_row) as cur:
            # Worst offender: the short with the largest maintenance
            # contribution. Positions aren't part of the instrument/account
            # lock ordering, so FOR UPDATE here is safe.
            await cur.execute(
                """
                SELECT p.id, p.instrument_id, p.quantity, p.borrow_fees_accrued,
                       p.dividends_accrued,
                       i.ticker, i.base_price, i.impact, i.liquidity, i.adv,
                       i.lambda_impact, i.max_impact, i.quoted_price, i.maint_margin_pct,
                       i.vol_state, i.flow_skew,
                       COALESCE(i.sigma_eff, i.sigma) AS sigma,
                       i.next_event_tick, i.last_halt_end_tick
                FROM positions p
                JOIN instruments i ON i.id = p.instrument_id
                WHERE p.user_id = %s
                  AND p.season_id IS NOT DISTINCT FROM %s
                  AND p.quantity < 0
                  AND (%s::int[] IS NULL OR i.market_id = ANY(%s))
                ORDER BY -p.quantity * i.quoted_price * i.maint_margin_pct DESC
                LIMIT 1
                FOR UPDATE OF p
                """,
                (user_id, season_id, _mids, _mids),
            )
            worst = await cur.fetchone()
        if worst is None:
            break

        qty = abs(int(worst["quantity"]))
        price_minor = Decimal(worst["quoted_price"]) * 100
        maint_pct = Decimal(worst["maint_margin_pct"])

        # Covering dq shares: equity loses dq*price*cost_frac (fees+penalty+
        # impact, approximated by max_impact), maint drops dq*price*maint_pct.
        # Solve (E - dq*p*c) >= target * (M - dq*p*m) for dq.
        cost_frac = (
            FEE_BPS / 10_000 + penalty_bps / 10_000 + Decimal(worst["max_impact"])
        )
        denom = price_minor * (target * maint_pct - cost_frac)
        deficit = target * Decimal(health.maint_req_minor) - Decimal(health.equity_minor)
        if denom > 0 and deficit > 0:
            close_qty = min(qty, math.ceil(deficit / denom))
        else:
            close_qty = qty

        # Covering needs cash. Sell longs (largest first) until the account
        # can afford the cover, then cover.
        est_cost = int(
            (
                Decimal(worst["quoted_price"])
                * (1 + Decimal(worst["max_impact"]))
                * close_qty
                * 100
            ).quantize(Decimal("1"), ROUND_HALF_UP)
        )
        while health.cash_minor < est_cost:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT p.id, p.instrument_id, p.quantity, p.borrow_fees_accrued,
                           p.dividends_accrued,
                           i.ticker, i.base_price, i.impact, i.liquidity, i.adv,
                           i.lambda_impact, i.max_impact, i.quoted_price,
                           i.maint_margin_pct, i.vol_state, i.flow_skew,
                           COALESCE(i.sigma_eff, i.sigma) AS sigma,
                           i.next_event_tick, i.last_halt_end_tick
                    FROM positions p
                    JOIN instruments i ON i.id = p.instrument_id
                    WHERE p.user_id = %s
                      AND p.season_id IS NOT DISTINCT FROM %s
                      AND p.quantity > 0
                      AND (%s::int[] IS NULL OR i.market_id = ANY(%s))
                    ORDER BY p.quantity * i.quoted_price DESC
                    LIMIT 1
                    FOR UPDATE OF p
                    """,
                    (user_id, season_id, _mids, _mids),
                )
                longest = await cur.fetchone()
            if longest is None:
                break
            await _liquidate_leg(
                conn,
                user_id=user_id,
                season_id=season_id,
                account_id=account_id,
                position=longest,
                close_qty=int(longest["quantity"]),
                equity_before=health.equity_minor,
                maint_before=health.maint_req_minor,
                tick_index=tick_index,
                penalty_bps=penalty_bps,
            )
            legs += 1
            health = await compute_health(conn, user_id, season_id)

        await _liquidate_leg(
            conn,
            user_id=user_id,
            season_id=season_id,
            account_id=account_id,
            position=worst,
            close_qty=close_qty,
            equity_before=health.equity_minor,
            maint_before=health.maint_req_minor,
            tick_index=tick_index,
            penalty_bps=penalty_bps,
        )
        legs += 1

    # Backstop: still negative equity (or shorts that can't be covered) ->
    # insurance fund pays what it can, MARKET_MAKER absorbs the residual.
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            SELECT p.id, p.instrument_id, p.quantity, p.borrow_fees_accrued,
                   p.dividends_accrued,
                   i.ticker, i.base_price, i.impact, i.liquidity, i.adv,
                   i.lambda_impact, i.max_impact, i.quoted_price, i.maint_margin_pct,
                   i.vol_state, i.flow_skew,
                   COALESCE(i.sigma_eff, i.sigma) AS sigma,
                   i.next_event_tick, i.last_halt_end_tick,
                   i.market_id
            FROM positions p
            JOIN instruments i ON i.id = p.instrument_id
            WHERE p.user_id = %s
              AND p.season_id IS NOT DISTINCT FROM %s
              AND p.quantity < 0
            FOR UPDATE OF p
            """,
            (user_id, season_id),
        )
        remaining_shorts = await cur.fetchall()

    health = await compute_health(conn, user_id, season_id)
    # Deferral (R3): shorts on closed venues can't fill at a frozen
    # mark -- the sweep retries them at that venue's reopen.
    if open_market_ids is not None and any(
        int(pos["market_id"]) not in open_market_ids
        for pos in remaining_shorts
    ):
        await _settle_bounties(conn, user_id, season_id, tick_index, legs)
        return legs
    if health.equity_minor < 0 or health.undermargined:
        for pos in remaining_shorts:
            await _liquidate_leg(
                conn,
                user_id=user_id,
                season_id=season_id,
                account_id=account_id,
                position=pos,
                close_qty=abs(int(pos["quantity"])),
                equity_before=health.equity_minor,
                maint_before=health.maint_req_minor,
                tick_index=tick_index,
                penalty_bps=penalty_bps,
            )
            legs += 1
            health = await compute_health(conn, user_id, season_id)

        health = await compute_health(conn, user_id, season_id)
        if health.equity_minor < 0:
            fund_id = await get_system_account_id(conn, "INSURANCE_FUND")
            mm_id = await get_system_account_id(conn, "MARKET_MAKER")
            shortfall = -health.equity_minor
            fund_pays = min(await _fund_balance(conn, fund_id), shortfall)
            if fund_pays > 0:
                await post_transfer(
                    conn,
                    from_account_id=fund_id,
                    to_account_id=account_id,
                    amount=fund_pays,
                    reason="COVER_SHORTFALL",
                )
                await _record_fund_flow(
                    conn, -fund_pays, "COVER_SHORTFALL", None, tick_index
                )
            residual = shortfall - fund_pays
            if residual > 0:
                await post_transfer(
                    conn,
                    from_account_id=mm_id,
                    to_account_id=account_id,
                    amount=residual,
                    reason="ADL_BACKSTOP",
                )
                await _record_fund_flow(
                    conn, 0, "ADL", None, tick_index, mm_absorbed_minor=residual
                )

    await _settle_bounties(conn, user_id, season_id, tick_index, legs)
    return legs


async def _settle_bounties(
    conn: AsyncConnection,
    user_id: int,
    season_id: int | None,
    tick_index: int | None,
    legs: int,
) -> None:
    """Pay out open bounties on a just-liquidated MAIN portfolio. League
    stakes are faucet-seeded, so a league liquidation never triggers
    (same gate as the public tape). Lazy import keeps bounties.service
    -> margin.service free of a module cycle."""
    if legs <= 0 or season_id is not None:
        return
    from stockbot.bounties import service as bounties_svc

    await bounties_svc.settle_for_user(conn, user_id, tick_index)


async def check_and_liquidate(
    conn: AsyncConnection, user_id: int, season_id: int | None = None
) -> int:
    """Post-trade sweep in its own transaction: locks the account's position
    instruments (id order) before the account, preserving lock ordering."""
    async with conn.transaction():
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT i.id FROM instruments i
                JOIN positions p ON p.instrument_id = i.id
                WHERE p.user_id = %s
                  AND p.season_id IS NOT DISTINCT FROM %s
                  AND p.quantity <> 0
                ORDER BY i.id
                FOR UPDATE OF i
                """,
                (user_id, season_id),
            )
            await cur.fetchall()
            await cur.execute(
                """
                SELECT a.id FROM accounts a
                WHERE a.user_id = %s AND a.season_id IS NOT DISTINCT FROM %s
                  AND a.kind <> 'SYSTEM'
                FOR UPDATE
                """,
                (user_id, season_id),
            )
            row = await cur.fetchone()
        if row is None:
            return 0
        account_id = int(row[0])
        tick = await _current_tick(conn)
        legs = await _liquidate_account(
            conn,
            user_id=user_id,
            season_id=season_id,
            account_id=account_id,
            tick_index=tick,
            open_market_ids=await open_market_ids(conn, tick),
        )
        await _warn_margin_risk(
            conn,
            user_id=user_id,
            season_id=season_id,
            account_id=account_id,
            tick_index=tick,
        )
        return legs


async def sweep_undermargined(
    conn: AsyncConnection,
    tick_index: int,
    open_market_ids: set[int] | None = None,
) -> int:
    """Tick-time liquidation sweep. Runs inside apply_tick's transaction where
    every instrument is already locked; each account row is taken FOR UPDATE
    lazily on its first write/read inside the legs (instruments-then-accounts
    ordering holds because all instrument locks are already held). Returns
    the number of liquidation legs executed across all accounts.

    `open_market_ids` (R3) scopes legs to venues open this tick; an
    account left undermargined on closed-venue positions alone defers to
    that venue's reopen."""
    async with conn.cursor() as cur:
        await cur.execute(
            """
            SELECT DISTINCT a.user_id, a.season_id, a.id AS account_id
            FROM accounts a
            JOIN positions p
              ON p.user_id = a.user_id
             AND p.season_id IS NOT DISTINCT FROM a.season_id
            WHERE p.quantity < 0
            ORDER BY a.id
            """
        )
        candidates = await cur.fetchall()

    legs = 0
    for user_id, season_id, account_id in candidates:
        uid = int(user_id)
        sid = int(season_id) if season_id is not None else None
        legs += await _liquidate_account(
            conn,
            user_id=uid,
            season_id=sid,
            account_id=int(account_id),
            tick_index=tick_index,
            open_market_ids=open_market_ids,
        )
        await _warn_margin_risk(
            conn,
            user_id=uid,
            season_id=sid,
            account_id=int(account_id),
            tick_index=tick_index,
        )
    return legs


async def _warn_margin_risk(
    conn: AsyncConnection,
    *,
    user_id: int,
    season_id: int | None,
    account_id: int,
    tick_index: int | None,
) -> None:
    """One MARGIN_CALL DM per approach-episode. `accounts.margin_warned`
    latches on warn and clears on recovery, so hovering across the
    threshold doesn't DM every tick. Warns only in the approaching band
    -- an undermargined account gets liquidation DMs, not warnings."""
    cfg = await margin_config(conn)
    warn_ratio = cfg["margin.warn_ratio"]
    if warn_ratio <= 0:
        return
    health = await compute_health(conn, user_id, season_id)
    risky = (
        health.margined
        and not health.undermargined
        and Decimal(health.equity_minor)
        < warn_ratio * Decimal(health.maint_req_minor)
    )
    async with conn.cursor() as cur:
        if risky:
            # UPDATE-first: rowcount is the atomic claim on the episode.
            await cur.execute(
                "UPDATE accounts SET margin_warned = TRUE "
                "WHERE id = %s AND NOT margin_warned",
                (account_id,),
            )
            if cur.rowcount == 0:
                return
            await cur.execute(
                """
                INSERT INTO notifications (user_id, kind, payload)
                VALUES (%s, 'MARGIN_CALL', %s)
                """,
                (
                    user_id,
                    json.dumps(
                        {
                            "tick_index": tick_index,
                            "equity": health.equity_minor,
                            "maint": health.maint_req_minor,
                            "warn_ratio": float(warn_ratio),
                        }
                    ),
                ),
            )
        else:
            await cur.execute(
                "UPDATE accounts SET margin_warned = FALSE "
                "WHERE id = %s AND margin_warned",
                (account_id,),
            )


def effective_borrow_bps_per_tick(
    si_pct: Decimal, cfg: dict[str, Decimal]
) -> Decimal:
    """Current borrow rate for an instrument at short interest `si_pct`
    (a fraction of float). Mirrors the SQL in `accrue_borrow_fees` --
    keep the two formulas in sync: bps * (1 + k*(SI/max_SI)^2)."""
    max_si = max(cfg["margin.max_short_interest_pct"], Decimal("0.0001"))
    util = si_pct / max_si
    return cfg["margin.borrow_fee_bps_per_tick"] * (
        1 + cfg["margin.borrow_util_k"] * util * util
    )


async def sweep_recalls(
    conn: AsyncConnection,
    tick_index: int,
    open_market_ids: set[int] | None = None,
) -> int:
    """Borrow recalls on crowded shorts: when an instrument's short
    interest exceeds margin.recall_si_pct, every open short covers a
    pro-rata share of (SI - threshold) * margin.recall_fraction_per_tick
    shares. Recall legs are cash-capped (an account covers only what it
    can pay for) so the insurance-fund/ADL backstop in _liquidate_leg is
    unreachable; a cashless account keeps its short until the real
    liquidation sweep prices it. Bounded shorts are collateralized
    derivatives, not borrows -- never recalled. Returns recall legs run."""
    from stockbot.trading.service import FEE_BPS  # lazy: circular at top level

    cfg = await margin_config(conn)
    threshold = cfg["margin.recall_si_pct"]
    frac = cfg["margin.recall_fraction_per_tick"]
    if threshold <= 0 or frac <= 0:
        return 0

    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            SELECT id, short_interest_pct
            FROM instruments
            WHERE is_active AND short_interest_pct > %s
              AND (%s::int[] IS NULL OR market_id = ANY(%s))
            """,
            (
                threshold,
                sorted(open_market_ids) if open_market_ids is not None else None,
                sorted(open_market_ids) if open_market_ids is not None else None,
            ),
        )
        crowded = await cur.fetchall()

    legs = 0
    for inst in crowded:
        si_pct = Decimal(inst["short_interest_pct"])
        if si_pct <= 0:
            continue
        # Each short recalls this fraction of its size: the excess SI
        # share, times the per-tick recall pace.
        recall_frac = float((si_pct - threshold) / si_pct) * float(frac)

        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT p.id, p.user_id, p.season_id, p.instrument_id,
                       p.quantity, p.borrow_fees_accrued, p.dividends_accrued,
                       i.ticker, i.base_price, i.impact, i.liquidity, i.adv,
                       i.lambda_impact, i.max_impact, i.quoted_price,
                       i.maint_margin_pct, i.vol_state, i.flow_skew,
                       COALESCE(i.sigma_eff, i.sigma) AS sigma,
                       i.next_event_tick, i.last_halt_end_tick,
                       a.id AS account_id
                FROM positions p
                JOIN instruments i ON i.id = p.instrument_id
                JOIN accounts a
                  ON a.user_id = p.user_id
                 AND a.season_id IS NOT DISTINCT FROM p.season_id
                WHERE p.instrument_id = %s AND p.quantity < 0
                ORDER BY p.id
                FOR UPDATE OF p
                """,
                (int(inst["id"]),),
            )
            shorts = await cur.fetchall()

        for pos in shorts:
            qty = -int(pos["quantity"])
            recall_qty = math.ceil(qty * recall_frac)
            # Cash cap with a pessimistic per-share bound (mark at the
            # impact ceiling plus fee): the balance CHECK must never be
            # the thing that stops a recall leg. This is only a sizing
            # hint -- _liquidate_leg re-clamps close_qty against the
            # ACTUAL fill, which is what makes the fund backstop
            # unreachable (this bound misses the half-spread).
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT balance FROM accounts WHERE id = %s FOR UPDATE",
                    (int(pos["account_id"]),),
                )
                bal_row = await cur.fetchone()
            cash = int(bal_row[0]) if bal_row else 0
            per_share_ub = int(
                (
                    Decimal(pos["quoted_price"])
                    * (1 + Decimal(pos["max_impact"]))
                    * (1 + FEE_BPS / Decimal(10_000))
                    * 100
                ).to_integral_value(rounding=ROUND_CEILING)
            )
            affordable = cash // per_share_ub if per_share_ub > 0 else 0
            close_qty = int(min(recall_qty, affordable, qty))
            if close_qty <= 0:
                continue
            await _liquidate_leg(
                conn,
                user_id=int(pos["user_id"]),
                season_id=(
                    int(pos["season_id"]) if pos["season_id"] is not None else None
                ),
                account_id=int(pos["account_id"]),
                position=pos,
                close_qty=close_qty,
                equity_before=0,
                maint_before=0,
                tick_index=tick_index,
                penalty_bps=Decimal(0),
                kind="RECALL",
            )
            legs += 1
    return legs


async def accrue_borrow_fees(conn: AsyncConnection) -> None:
    """Accrue the per-tick borrow fee onto every short position. One UPDATE;
    settles to SINK when the position is covered or liquidated.

    Effective rate is utilization-scaled: bps * (1 + k*(SI/max_SI)^2), so a
    crowded short bleeds superlinearly -- runs right after
    `refresh_short_interest` in apply_tick, so i.short_interest_pct is fresh.
    """
    cfg = await margin_config(conn)
    async with conn.cursor() as cur:
        await cur.execute(
            """
            UPDATE positions p
            SET borrow_fees_accrued = borrow_fees_accrued
                  + (-p.quantity * i.quoted_price * 100 * %s
                     * (1 + %s * POWER(
                           i.short_interest_pct / GREATEST(%s, 0.0001), 2)) / 10000)
            FROM instruments i
            WHERE i.id = p.instrument_id AND p.quantity < 0
            """,
            (
                cfg["margin.borrow_fee_bps_per_tick"],
                cfg["margin.borrow_util_k"],
                cfg["margin.max_short_interest_pct"],
            ),
        )


async def refresh_short_interest(conn: AsyncConnection) -> None:
    """Update instruments.short_interest_pct from live positions."""
    async with conn.cursor() as cur:
        await cur.execute(
            """
            UPDATE instruments i
            SET short_interest_pct = COALESCE(
                (SELECT SUM(-p.quantity) * 1.0 / NULLIF(i.float_shares, 0)
                 FROM positions p
                 WHERE p.instrument_id = i.id AND p.quantity < 0),
                0)
            """
        )


async def list_liquidations(
    conn: AsyncConnection, user_id: int, season_id: int | None = None
) -> list[dict[str, Any]]:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            SELECT l.id, i.ticker, l.side, l.quantity_closed, l.fill_price,
                   l.notional_minor, l.penalty_minor, l.tick_index, l.created_at
            FROM liquidations l
            JOIN instruments i ON i.id = l.instrument_id
            WHERE l.user_id = %s AND l.season_id IS NOT DISTINCT FROM %s
            ORDER BY l.created_at DESC
            LIMIT 25
            """,
            (user_id, season_id),
        )
        return await cur.fetchall()
