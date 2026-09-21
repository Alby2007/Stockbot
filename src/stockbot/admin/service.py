"""Admin tools: runtime instrument tuning and the ledger invariant audit.

Every tunable param lives in `instruments` and can be hot-adjusted here
without a deploy, per the design doc. `PARAM_BOUNDS` doubles as the
allow-list -- the column name is interpolated into SQL, so anything not on
this list is rejected before it ever reaches a query. The bounds matter as
much as the list: an out-of-range value doesn't fail at tune time, it fails
inside every subsequent tick or trade (`tau_ticks = 0` -> `exp(-dt/0)` in
`step_instrument`, `liquidity = 0` -> division by zero in
`apply_trade_impact`). The same bounds are enforced at the schema level by
migration 0013's `instruments_engine_params_sane` CHECK.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from psycopg import AsyncConnection

from stockbot.ledger.service import (
    get_system_account_id,
    get_user_account_id,
    post_transfer,
)
from stockbot.trading.errors import UnknownInstrumentError

# Inclusive (min, max) bounds per tunable parameter, mirrored by the
# instruments_engine_params_sane CHECK constraint -- keep them in sync.
# Finite bounds also reject NaN/Infinity, which numeric comparisons treat
# as greater than everything rather than rejecting.
PARAM_BOUNDS: dict[str, tuple[float, float]] = {
    "drift": (-1.0, 1.0),
    "sigma": (0.0, 1.0),
    "beta": (-10.0, 10.0),
    "gamma": (-10.0, 10.0),
    "kappa": (0.0, 1.0),
    "fundamental_sigma": (0.0, 1.0),
    "liquidity": (0.01, 1e18),
    "lambda_impact": (0.0, 1e3),
    "tau_ticks": (0.01, 1e9),
    "max_impact": (0.0, 1.0),
    "short_knockout_pct": (0.0001, 0.9999),
    "init_margin_pct": (0.0001, 1.0),
    "maint_margin_pct": (0.0001, 1.0),
    "float_shares": (0.0, 9e18),
}

TUNABLE_PARAMS = tuple(PARAM_BOUNDS)


async def tune_instrument(conn: AsyncConnection, ticker: str, param: str, value: float) -> None:
    bounds = PARAM_BOUNDS.get(param)
    if bounds is None:
        raise ValueError(f"{param!r} is not a tunable parameter; choose from {TUNABLE_PARAMS}")
    if not math.isfinite(value):
        raise ValueError(f"{param} must be finite; got {value}")
    lo, hi = bounds
    if not lo <= value <= hi:
        raise ValueError(f"{param} must be within [{lo}, {hi}]; got {value}")
    ticker = ticker.upper()
    async with conn.transaction():
        async with conn.cursor() as cur:
            # `param` is validated against the allow-list above, so this
            # interpolation can't be used to inject arbitrary SQL.
            await cur.execute(
                f"UPDATE instruments SET {param} = %s WHERE ticker = %s",  # noqa: S608
                (value, ticker),
            )
            if cur.rowcount == 0:
                raise UnknownInstrumentError(ticker)


@dataclass(frozen=True)
class LedgerAuditReport:
    ledger_sum: int
    negative_user_accounts: int
    mismatched_accounts: list[int]
    system_balances: dict[str, int]

    @property
    def healthy(self) -> bool:
        return (
            self.ledger_sum == 0
            and self.negative_user_accounts == 0
            and not self.mismatched_accounts
        )


async def ledger_audit(conn: AsyncConnection) -> LedgerAuditReport:
    async with conn.cursor() as cur:
        await cur.execute("SELECT COALESCE(SUM(amount), 0) FROM ledger_entries")
        row = await cur.fetchone()
        assert row is not None
        ledger_sum = int(row[0])

        await cur.execute("SELECT COUNT(*) FROM accounts WHERE kind = 'USER' AND balance < 0")
        row = await cur.fetchone()
        assert row is not None
        negative_user_accounts = int(row[0])

        # The drift that can actually happen: accounts.balance is a
        # materialized cache over ledger_entries (post_transfer is the only
        # writer), so any divergence means a write bypassed the ledger --
        # which the two global checks above can't see.
        await cur.execute(
            """
            SELECT a.id
            FROM accounts a
            LEFT JOIN ledger_entries le ON le.account_id = a.id
            GROUP BY a.id
            HAVING a.balance <> COALESCE(SUM(le.amount), 0)
            ORDER BY a.id
            """
        )
        mismatched_accounts = [int(row[0]) for row in await cur.fetchall()]

        await cur.execute(
            "SELECT system_name, balance FROM accounts WHERE kind = 'SYSTEM' ORDER BY system_name"
        )
        system_balances = {name: int(balance) for name, balance in await cur.fetchall()}

    return LedgerAuditReport(
        ledger_sum=ledger_sum,
        negative_user_accounts=negative_user_accounts,
        mismatched_accounts=mismatched_accounts,
        system_balances=system_balances,
    )


# Allow-list + bounds for `config` keys, the same treatment PARAM_BOUNDS
# gives instrument columns. Config rows silently poison state at read
# time -- `session.open_ticks = 0` wedges every tick, a 0-divisor like
# `NULLIF(max_si, 0)` in the borrow-fee math fails *inside* a cover. The
# bounds are deliberately generous (these are ops knobs, not physics) but
# always finite and nonzero where a zero would divide.
CONFIG_BOUNDS: dict[str, tuple[float, float]] = {
    "audit.every_n_ticks": (1, 1e6),
    "cross.collar_pct": (0.0001, 1.0),
    "cross.trade_through_epsilon": (0.0, 1.0),
    "dividend.interval_ticks": (1, 1e9),
    "dividend.jitter_ticks": (0, 1e9),
    "dividend.max_yield": (0.0, 1.0),
    "dividend.min_yield": (0.0, 1.0),
    "dividend.payer_pct": (0.0, 100.0),  # percent of instruments, not a fraction
    "impact.participation_cap": (1e-6, 1.0),
    "margin.borrow_fee_bps_per_tick": (0.0, 1e6),
    "margin.borrow_util_k": (0.0, 1e6),
    "margin.liquidation_penalty_bps": (0.0, 1e6),
    "margin.liquidation_target_ratio": (0.0, 1e6),
    "margin.max_gross_leverage": (0.0, 1e6),
    "margin.max_short_interest_pct": (0.0, 1e18),
    "margin.squeeze_lambda_boost": (0.0, 1e6),
    "margin.squeeze_si_threshold": (0.0, 1e18),
    "order.max_fill_failures": (1, 1e6),
    "order.stop_cascade_max_iters": (1, 1e4),
    "orders.enabled": (0, 1),
    "session.closed_ticks": (0, 1440),
    "session.open_impact_reset": (0.0, 1.0),
    "session.open_ticks": (0, 1440),
    "session.phase_offset_ticks": (0, 1440),
    "shorts.enabled": (0, 1),
    "spread.base_bps": (0.0, 1e4),
    "spread.event_coeff": (0.0, 1e6),
    "spread.event_window_ticks": (0, 1e9),
    "spread.halt_coeff": (0.0, 1e6),
    "spread.halt_decay_ticks": (0, 1e9),
    "spread.inv_liquidity_coeff": (0.0, 1e18),
    "spread.liquidity_ref": (0.01, 1e18),
    "spread.sigma_coeff": (0.0, 1e6),
    "spread.sigma_ref": (1e-9, 1e6),
    "trading.enabled": (0, 1),
}

TUNABLE_CONFIG_KEYS = tuple(CONFIG_BOUNDS)


async def set_config(conn: AsyncConnection, key: str, value: float) -> None:
    bounds = CONFIG_BOUNDS.get(key)
    if bounds is None:
        raise ValueError(
            f"{key!r} is not a known config key; choose from {TUNABLE_CONFIG_KEYS}"
        )
    if not math.isfinite(value):
        raise ValueError(f"{key} must be finite; got {value}")
    lo, hi = bounds
    if not lo <= value <= hi:
        raise ValueError(f"{key} must be within [{lo}, {hi}]; got {value}")
    async with conn.transaction(), conn.cursor() as cur:
        await cur.execute("UPDATE config SET value = %s WHERE key = %s", (value, key))
        if cur.rowcount == 0:
            raise ValueError(f"config key {key!r} has no row; was the DB migrated?")


async def admin_adjust(
    conn: AsyncConnection,
    *,
    user_id: int,
    amount: int,
    memo: str,
    admin_id: int,
) -> None:
    """Ledger-respecting balance repair: a credit is a FAUCET->user
    transfer, a debit is user->SINK. Never UPDATE accounts.balance -- the
    ledger stays the only writer, so the adjustment is itself an
    explainable entry (`reason='ADMIN_ADJUST'`, memo carries the admin)."""
    if amount == 0:
        raise ValueError("adjustment amount must be nonzero")
    account_id = await get_user_account_id(conn, user_id)
    full_memo = f"admin {admin_id}: {memo}"
    if amount > 0:
        await post_transfer(
            conn,
            from_account_id=await get_system_account_id(conn, "FAUCET"),
            to_account_id=account_id,
            amount=amount,
            reason="ADMIN_ADJUST",
            memo=full_memo,
        )
    else:
        await post_transfer(
            conn,
            from_account_id=account_id,
            to_account_id=await get_system_account_id(conn, "SINK"),
            amount=-amount,
            reason="ADMIN_ADJUST",
            memo=full_memo,
        )


async def admin_cancel_order(conn: AsyncConnection, order_id: int) -> bool:
    """Force-cancel any OPEN order regardless of owner -- the zombie-order
    repair path. Returns False if the order isn't OPEN."""
    async with conn.transaction(), conn.cursor() as cur:
        await cur.execute(
            "UPDATE orders SET status = 'CANCELLED' WHERE id = %s AND status = 'OPEN'",
            (order_id,),
        )
        return cur.rowcount > 0


async def recalc_balances(conn: AsyncConnection) -> int:
    """Rebuild the accounts.balance cache from SUM(ledger_entries) and
    return how many rows drifted. Deterministic and self-verifying: the
    ledger is the source of truth, so recomputing can only restore, never
    corrupt -- and the USER/LEAGUE non-negativity CHECK means a rebuild
    that would go negative fails loudly instead of masking real debt."""
    async with conn.transaction(), conn.cursor() as cur:
        await cur.execute(
            """
            UPDATE accounts a
            SET balance = s.total
            FROM (
                SELECT account_id, SUM(amount) AS total
                FROM ledger_entries
                GROUP BY account_id
            ) s
            WHERE a.id = s.account_id AND a.balance <> s.total
            """
        )
        return cur.rowcount
