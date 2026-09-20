from __future__ import annotations

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


async def test_ledger_audit_is_healthy_on_a_clean_ledger(conn: AsyncConnection) -> None:
    await bootstrap_user(conn, 4001)
    await claim_daily(conn, 4001)
    report = await ledger_audit(conn)
    assert report.healthy
    assert report.ledger_sum == 0
    assert report.negative_user_accounts == 0
    assert "FAUCET" in report.system_balances
