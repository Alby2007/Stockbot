"""Admin tools: runtime instrument tuning and the ledger invariant audit.

Every tunable param lives in `instruments` and can be hot-adjusted here
without a deploy, per the design doc. `TUNABLE_PARAMS` is an allow-list --
the column name is interpolated into SQL, so anything not on this list is
rejected before it ever reaches a query.
"""

from __future__ import annotations

from dataclasses import dataclass

from psycopg import AsyncConnection

from stockbot.trading.errors import UnknownInstrumentError

TUNABLE_PARAMS = (
    "drift",
    "sigma",
    "beta",
    "gamma",
    "kappa",
    "fundamental_sigma",
    "liquidity",
    "lambda_impact",
    "tau_ticks",
    "max_impact",
    "short_knockout_pct",
    "init_margin_pct",
    "maint_margin_pct",
    "float_shares",
)


async def tune_instrument(conn: AsyncConnection, ticker: str, param: str, value: float) -> None:
    if param not in TUNABLE_PARAMS:
        raise ValueError(f"{param!r} is not a tunable parameter; choose from {TUNABLE_PARAMS}")
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
    system_balances: dict[str, int]

    @property
    def healthy(self) -> bool:
        return self.ledger_sum == 0 and self.negative_user_accounts == 0


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

        await cur.execute(
            "SELECT system_name, balance FROM accounts WHERE kind = 'SYSTEM' ORDER BY system_name"
        )
        system_balances = {name: int(balance) for name, balance in await cur.fetchall()}

    return LedgerAuditReport(
        ledger_sum=ledger_sum,
        negative_user_accounts=negative_user_accounts,
        system_balances=system_balances,
    )
