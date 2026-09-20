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

import math
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from psycopg import AsyncConnection
from psycopg.rows import dict_row

from stockbot.ledger.service import get_system_account_id, post_transfer
from stockbot.margin.errors import MarginSpendBlockedError
from stockbot.market import engine

MAX_LIQUIDATION_LEGS = 64


@dataclass(frozen=True)
class MarginHealth:
    account_id: int
    cash_minor: int
    positions_value_minor: int
    bshort_value_minor: int
    accrued_fees_minor: int
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
        return {key: Decimal(value) for key, value in await cur.fetchall()}


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
    bshort_value = int(Decimal(row["bshort_value"]).quantize(Decimal("1"), ROUND_HALF_UP))
    equity = cash + positions_value + bshort_value - accrued

    return MarginHealth(
        account_id=int(row["account_id"]),
        cash_minor=cash,
        positions_value_minor=positions_value,
        bshort_value_minor=bshort_value,
        accrued_fees_minor=accrued,
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
) -> None:
    async with conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO insurance_fund_flows (amount_minor, reason, liquidation_id, tick_index)
            VALUES (%s, %s, %s, %s)
            """,
            (amount_minor, reason, liquidation_id, tick_index),
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
) -> None:
    """Force-close `close_qty` of one position at the impact-adjusted price.

    For shorts (BUY leg) the cover cost is paid from cash, then the insurance
    fund, then implicitly by MARKET_MAKER (recorded as an ADL flow). For longs
    (SELL leg) MARKET_MAKER pays out proceeds. The penalty is taken from the
    account's cash when it can cover it.
    """
    # Lazy: trading.service imports this module, so a top-level import here
    # would be circular.
    from stockbot.trading.service import FEE_BPS, HALF_SPREAD_BPS

    instrument_id = int(position["instrument_id"])
    qty = int(position["quantity"])
    base_price = float(position["base_price"])
    signed = base_price * close_qty * (1 if qty < 0 else -1)
    fill_f, impact_after = engine.apply_trade_impact(
        base_price=base_price,
        impact_before=float(position["impact"]),
        signed_notional=signed,
        liquidity=float(position["liquidity"]),
        lambda_impact=float(position["lambda_impact"]),
        max_impact=float(position["max_impact"]),
        half_spread=float(HALF_SPREAD_BPS) / 10_000,
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
            "SELECT balance FROM accounts WHERE id = %s", (account_id,)
        )
        cash_row = await cur.fetchone()
        assert cash_row is not None
        cash = int(cash_row[0])

        fund_pays = 0
        residual = 0
        shortfall = 0
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
                    reason="LIQ_COVER",
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

        # Settle accrued borrow fees proportionally on the closed quantity.
        accrued = Decimal(position["borrow_fees_accrued"])
        settled_minor = 0
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

        new_qty = qty + close_qty if qty < 0 else qty - close_qty
        await cur.execute(
            """
            UPDATE positions
            SET quantity = %s,
                avg_cost = CASE WHEN %s = 0 THEN 0 ELSE avg_cost END,
                borrow_fees_accrued = CASE WHEN %s = 0 THEN 0 ELSE %s END,
                updated_at = now()
            WHERE id = %s
            """,
            (new_qty, new_qty, new_qty, accrued, position["id"]),
        )

        new_quoted = Decimal(str(round(base_price * math.exp(impact_after), 6)))
        await cur.execute(
            "UPDATE instruments SET impact = %s, quoted_price = %s WHERE id = %s",
            (impact_after, new_quoted, instrument_id),
        )

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
            await _record_fund_flow(conn, -residual, "ADL", liquidation_id, tick_index)


async def _liquidate_account(
    conn: AsyncConnection,
    *,
    user_id: int,
    season_id: int | None,
    account_id: int,
    tick_index: int | None,
) -> int:
    """Reduce an undermargined account until ratio >= target or it has no
    positions left. Returns the number of liquidation legs executed.

    Assumes the caller's transaction already holds locks on the account's
    position instruments (true inside apply_tick and in check_and_liquidate).
    """
    from stockbot.trading.service import FEE_BPS  # lazy: circular at top level

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
                       i.ticker, i.base_price, i.impact, i.liquidity,
                       i.lambda_impact, i.max_impact, i.quoted_price, i.maint_margin_pct
                FROM positions p
                JOIN instruments i ON i.id = p.instrument_id
                WHERE p.user_id = %s
                  AND p.season_id IS NOT DISTINCT FROM %s
                  AND p.quantity < 0
                ORDER BY -p.quantity * i.quoted_price * i.maint_margin_pct DESC
                LIMIT 1
                FOR UPDATE OF p
                """,
                (user_id, season_id),
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
                           i.ticker, i.base_price, i.impact, i.liquidity,
                           i.lambda_impact, i.max_impact, i.quoted_price,
                           i.maint_margin_pct
                    FROM positions p
                    JOIN instruments i ON i.id = p.instrument_id
                    WHERE p.user_id = %s
                      AND p.season_id IS NOT DISTINCT FROM %s
                      AND p.quantity > 0
                    ORDER BY p.quantity * i.quoted_price DESC
                    LIMIT 1
                    FOR UPDATE OF p
                    """,
                    (user_id, season_id),
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
                   i.ticker, i.base_price, i.impact, i.liquidity,
                   i.lambda_impact, i.max_impact, i.quoted_price, i.maint_margin_pct
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
                await _record_fund_flow(conn, -residual, "ADL", None, tick_index)

    return legs


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
        return await _liquidate_account(
            conn,
            user_id=user_id,
            season_id=season_id,
            account_id=account_id,
            tick_index=tick,
        )


async def sweep_undermargined(conn: AsyncConnection, tick_index: int) -> int:
    """Tick-time liquidation sweep. Runs inside apply_tick's transaction where
    every instrument is already locked; locks accounts afterwards. Returns the
    number of liquidation legs executed across all accounts."""
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
        legs += await _liquidate_account(
            conn,
            user_id=int(user_id),
            season_id=int(season_id) if season_id is not None else None,
            account_id=int(account_id),
            tick_index=tick_index,
        )
    return legs


async def accrue_borrow_fees(conn: AsyncConnection) -> None:
    """Accrue the per-tick borrow fee onto every short position. One UPDATE;
    settles to SINK when the position is covered or liquidated."""
    cfg = await margin_config(conn)
    async with conn.cursor() as cur:
        await cur.execute(
            """
            UPDATE positions p
            SET borrow_fees_accrued = borrow_fees_accrued
                  + (-p.quantity * i.quoted_price * 100 * %s / 10000)
            FROM instruments i
            WHERE i.id = p.instrument_id AND p.quantity < 0
            """,
            (cfg["margin.borrow_fee_bps_per_tick"],),
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
