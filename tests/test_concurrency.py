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
import os
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from psycopg import AsyncConnection, conninfo, pq

from stockbot.accounts.service import STARTING_GRANT, bootstrap_user
from stockbot.config import get_settings
from stockbot.market.tick import _post_tick, apply_tick
from stockbot.migrate import run_migrations

_CROSS_CONN_USER = 987_654_321
_RACE_USER = 987_654_322


async def _real_conn(*, autocommit: bool = True) -> AsyncConnection:
    """A committed-mode connection. `autocommit=True` mirrors production
    service behavior; `autocommit=False` is the sharper test mode -- a
    bare-statement regression (the implicit-transaction poison) only
    fails there, since self-committing statements mask it."""
    return await AsyncConnection.connect(
        get_settings().test_database_url, autocommit=autocommit
    )


@pytest_asyncio.fixture
async def real_conn() -> AsyncIterator[AsyncConnection]:
    connection = await _real_conn(autocommit=False)
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
        res_a, res_b = await asyncio.gather(
            bootstrap_user(real_conn, _RACE_USER),
            bootstrap_user(other, _RACE_USER),
        )
    finally:
        await other.close()
    assert res_a.account_id == res_b.account_id
    # At most one winner even under the race (both False on re-runs where
    # the fixed user id already committed).
    assert not (res_a.created and res_b.created)

    async with real_conn.cursor() as cur:
        await cur.execute(
            """
            SELECT COUNT(*) FROM ledger_entries
            WHERE account_id = %s AND reason = 'STARTING_GRANT' AND amount > 0
            """,
            (res_a.account_id,),
        )
        assert (await cur.fetchone())[0] == 1


async def test_post_tick_leaves_connection_idle() -> None:
    """Regression for the _post_tick poison: a bare SELECT on the market's
    long-lived connection opened an implicit transaction that never
    committed, silently demoting every later apply_tick's transaction to
    a savepoint -- ticks 'succeeded' while writing nothing. The bug's
    signature is the connection being left INTRANS after post-tick work."""
    conn = await _real_conn(autocommit=False)
    try:
        await _post_tick(conn, 1)
        assert conn.pgconn.transaction_status == pq.TransactionStatus.IDLE
    finally:
        await conn.close()  # rolls back any leftover implicit tx


async def test_ticks_actually_commit_on_a_plain_connection() -> None:
    """End-to-end version of the same regression: two apply_tick calls on
    a plain (non-autocommit, no outer tx -- exactly the market service's)
    connection must be visible to a second connection. Runs on a
    disposable database because the ticks commit real state (instrument
    prices, candles) that can't be rolled back."""
    base = conninfo.conninfo_to_dict(get_settings().test_database_url)
    admin_url = conninfo.make_conninfo(**{**base, "dbname": "postgres"})
    scratch = f"stockbot_tickreg_{os.getpid()}"
    scratch_url = conninfo.make_conninfo(**{**base, "dbname": scratch})

    admin = await AsyncConnection.connect(admin_url, autocommit=True)
    try:
        try:
            await admin.execute(f'CREATE DATABASE "{scratch}"')
        except Exception as exc:
            pytest.skip(f"cannot create scratch DB ({exc}); needs CREATE privilege")
    finally:
        await admin.close()

    try:
        run_migrations(scratch_url)
        market_conn = await AsyncConnection.connect(scratch_url, autocommit=False)
        try:
            # Three ticks: a fresh DB starts at tick_index 0, and the poison
            # only opens on the first _post_tick (tick 1) -- under the bug,
            # tick 2 is the first one that silently fails to commit.
            for _ in range(3):
                await apply_tick(market_conn, "regression-seed")
        finally:
            await market_conn.close()

        reader = await AsyncConnection.connect(scratch_url, autocommit=True)
        try:
            async with reader.cursor() as cur:
                await cur.execute("SELECT MAX(tick_index) FROM market_ticks")
                row = await cur.fetchone()
        finally:
            await reader.close()
        assert row is not None and int(row[0]) >= 2, (
            "tick 2 did not commit -- a bare statement between ticks left "
            "the connection in a poisoned implicit transaction"
        )
    finally:
        admin = await AsyncConnection.connect(admin_url, autocommit=True)
        try:
            await admin.execute(
                f'DROP DATABASE IF EXISTS "{scratch}" WITH (FORCE)'
            )
        finally:
            await admin.close()
