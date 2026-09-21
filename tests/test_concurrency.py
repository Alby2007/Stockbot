"""Cross-connection and race-condition tests.

The main `conn` fixture wraps each test in a rolled-back transaction, so
it can never catch "wrote but never committed" or "two connections
racing" bugs -- the `bootstrap_user` bare-SELECT bug lived in exactly
that blind spot. These tests use real autocommit connections (each
service `conn.transaction()` commits for real, like production) against
fixed user ids; `bootstrap_user` is idempotent, so repeat runs only
re-verify the rows the first run committed.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest_asyncio
from psycopg import AsyncConnection

from stockbot.accounts.service import STARTING_GRANT, bootstrap_user
from stockbot.config import get_settings

_CROSS_CONN_USER = 987_654_321
_RACE_USER = 987_654_322


async def _real_conn() -> AsyncConnection:
    """A committed-mode connection: mirrors production, where every
    service-level transaction commits immediately."""
    return await AsyncConnection.connect(
        get_settings().test_database_url, autocommit=True
    )


@pytest_asyncio.fixture
async def real_conn() -> AsyncIterator[AsyncConnection]:
    connection = await _real_conn()
    try:
        yield connection
    finally:
        await connection.close()


async def test_bootstrap_visible_on_second_connection(
    real_conn: AsyncConnection,
) -> None:
    """Regression for the bare-SELECT bug: bootstrap on conn A must be
    fully committed -- a second connection sees the user, the account,
    and the grant balance. (Would have failed while bootstrap opened an
    implicit transaction and never committed.)"""
    await bootstrap_user(real_conn, _CROSS_CONN_USER)

    reader = await _real_conn()
    try:
        async with reader.cursor() as cur:
            await cur.execute("SELECT COUNT(*) FROM users WHERE id = %s", (_CROSS_CONN_USER,))
            assert (await cur.fetchone())[0] == 1
            await cur.execute(
                "SELECT balance FROM accounts WHERE kind = 'USER' AND user_id = %s",
                (_CROSS_CONN_USER,),
            )
            row = await cur.fetchone()
            assert row is not None
            # Balance is >= one grant: the sim may have claimed on this
            # account in earlier runs, but it can never lose the grant.
            assert int(row[0]) >= STARTING_GRANT
    finally:
        await reader.close()


async def test_concurrent_bootstrap_grants_once(real_conn: AsyncConnection) -> None:
    """Two connections racing the same first bootstrap must produce
    exactly one STARTING_GRANT transfer -- the ON CONFLICT rowcount
    decides who created the user, and only the winner grants."""
    other = await _real_conn()
    try:
        account_a, account_b = await asyncio.gather(
            bootstrap_user(real_conn, _RACE_USER),
            bootstrap_user(other, _RACE_USER),
        )
    finally:
        await other.close()
    assert account_a == account_b

    async with real_conn.cursor() as cur:
        await cur.execute(
            """
            SELECT COUNT(*) FROM ledger_entries
            WHERE account_id = %s AND reason = 'STARTING_GRANT' AND amount > 0
            """,
            (account_a,),
        )
        assert (await cur.fetchone())[0] == 1
