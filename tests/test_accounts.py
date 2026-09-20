from __future__ import annotations

from psycopg import AsyncConnection

from stockbot.accounts.service import STARTING_GRANT, bootstrap_user, create_user_account
from stockbot.ledger.service import get_balance


async def test_create_user_account_is_idempotent(conn: AsyncConnection) -> None:
    first = await create_user_account(conn, 555)
    second = await create_user_account(conn, 555)
    assert first == second


async def test_bootstrap_grants_starting_balance_once(conn: AsyncConnection) -> None:
    account_id = await bootstrap_user(conn, 777)
    assert await get_balance(conn, account_id) == STARTING_GRANT

    # Calling again must not re-grant.
    account_id_again = await bootstrap_user(conn, 777)
    assert account_id_again == account_id
    assert await get_balance(conn, account_id) == STARTING_GRANT
