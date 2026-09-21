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
