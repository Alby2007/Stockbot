from __future__ import annotations

import psycopg
import pytest
from psycopg import AsyncConnection

from stockbot.accounts.service import bootstrap_user
from stockbot.admin.service import ledger_audit, tune_instrument
from stockbot.claims.service import claim_daily
from stockbot.trading.errors import UnknownInstrumentError


async def test_tune_instrument_updates_the_param(conn: AsyncConnection) -> None:
    async with conn.cursor() as cur:
        await cur.execute("SELECT ticker FROM instruments ORDER BY id LIMIT 1")
        (ticker,) = await cur.fetchone()

    await tune_instrument(conn, ticker, "sigma", 0.001)

    async with conn.cursor() as cur:
        await cur.execute("SELECT sigma FROM instruments WHERE ticker = %s", (ticker,))
        (sigma,) = await cur.fetchone()
    assert float(sigma) == 0.001


async def test_tune_rejects_non_tunable_param(conn: AsyncConnection) -> None:
    with pytest.raises(ValueError, match="not a tunable parameter"):
        await tune_instrument(conn, "NORT", "quoted_price", 999.0)


async def test_tune_rejects_unknown_ticker(conn: AsyncConnection) -> None:
    with pytest.raises(UnknownInstrumentError):
        await tune_instrument(conn, "NOPE", "sigma", 0.001)


async def test_tune_rejects_out_of_range_values(conn: AsyncConnection) -> None:
    """tau_ticks=0 would make exp(-dt/tau) raise on every tick; liquidity=0
    makes apply_trade_impact divide by zero on every trade. These must be
    refused at tune time, not discovered in the tick loop."""
    with pytest.raises(ValueError, match="tau_ticks"):
        await tune_instrument(conn, "NORT", "tau_ticks", 0)
    with pytest.raises(ValueError, match="liquidity"):
        await tune_instrument(conn, "NORT", "liquidity", 0)
    with pytest.raises(ValueError, match="sigma"):
        await tune_instrument(conn, "NORT", "sigma", -0.5)
    with pytest.raises(ValueError, match="init_margin_pct"):
        await tune_instrument(conn, "NORT", "init_margin_pct", 0)


async def test_tune_rejects_non_finite_values(conn: AsyncConnection) -> None:
    """NaN/Inf would slip past Postgres numeric comparisons (NaN > everything),
    so the Python-side gate is the real defense for the tuned path."""
    with pytest.raises(ValueError, match="finite"):
        await tune_instrument(conn, "NORT", "sigma", float("nan"))
    with pytest.raises(ValueError, match="finite"):
        await tune_instrument(conn, "NORT", "sigma", float("inf"))


async def test_param_bounds_are_enforced_by_the_schema(conn: AsyncConnection) -> None:
    """Backstop for writes that bypass tune_instrument entirely."""
    with pytest.raises(psycopg.errors.CheckViolation):
        async with conn.transaction():
            async with conn.cursor() as cur:
                await cur.execute("UPDATE instruments SET tau_ticks = 0")


async def test_ledger_audit_is_healthy_on_a_clean_ledger(conn: AsyncConnection) -> None:
    await bootstrap_user(conn, 4001)
    await claim_daily(conn, 4001)
    report = await ledger_audit(conn)
    assert report.healthy
    assert report.ledger_sum == 0
    assert report.negative_user_accounts == 0
    assert report.mismatched_accounts == []
    assert "FAUCET" in report.system_balances


async def test_ledger_audit_flags_accounts_whose_balance_drifted(conn: AsyncConnection) -> None:
    """accounts.balance is a cache over ledger_entries; a write that bypasses
    post_transfer must show up here even though the global sum stays 0."""
    account_id = await bootstrap_user(conn, 4002)
    await claim_daily(conn, 4002)
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE accounts SET balance = balance + 1 WHERE id = %s", (account_id,)
        )

    report = await ledger_audit(conn)
    assert not report.healthy
    assert account_id in report.mismatched_accounts
