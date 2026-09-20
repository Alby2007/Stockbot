from __future__ import annotations

import asyncio
import sys
from collections.abc import AsyncIterator

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
    """A connection to a clean test database, with a fresh transaction per test.

    Non-system tables are wiped and system accounts reset to a zero balance
    before each test so tests are independent and can run in any order.
    """
    settings = get_settings()
    connection = await AsyncConnection.connect(settings.test_database_url, autocommit=False)
    try:
        async with connection.cursor() as cur:
            await cur.execute("DELETE FROM ledger_entries")
            await cur.execute("DELETE FROM accounts WHERE kind = 'USER'")
            await cur.execute("DELETE FROM users")
            await cur.execute("UPDATE accounts SET balance = 0 WHERE kind = 'SYSTEM'")
        await connection.commit()
        yield connection
    finally:
        await connection.close()
