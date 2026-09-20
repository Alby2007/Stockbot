from __future__ import annotations

import asyncio
import sys
from collections.abc import AsyncIterator

import psycopg
import pytest
import pytest_asyncio
from psycopg import AsyncConnection

from stockbot.config import get_settings
from stockbot.migrate import run_migrations

SYSTEM_ACCOUNTS = ("FAUCET", "SINK", "MARKET_MAKER", "INSURANCE_FUND")

# psycopg's async mode requires a selector-based event loop; Windows defaults
# asyncio to ProactorEventLoop, which it explicitly refuses to run under.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


@pytest.fixture(scope="session", autouse=True)
def _migrated_test_db() -> None:
    """Apply migrations to the test database once per test session.

    Requires a real Postgres reachable at TEST_DATABASE_URL (see .env.example).
    """
    run_migrations(get_settings().test_database_url)


@pytest_asyncio.fixture
async def conn() -> AsyncIterator[AsyncConnection]:
    """A connection wrapping the whole test in one transaction that is always
    rolled back at teardown, whether the test passed or failed.

    Every service function opens its own `async with conn.transaction():`
    block; because this fixture already has an outer transaction open, those
    nest as savepoints instead of top-level commits, so nothing here ever
    touches disk. That keeps tests independent (any order, no manual
    per-table cleanup) even though trading/tick tests mutate `instruments`,
    `positions`, etc. alongside the ledger tables.
    """
    settings = get_settings()
    connection = await AsyncConnection.connect(settings.test_database_url, autocommit=False)
    try:
        async with connection.transaction() as tx:
            yield connection
            raise psycopg.Rollback(tx)
    finally:
        await connection.close()
