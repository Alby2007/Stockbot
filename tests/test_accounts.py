"""bootstrap_user: exactly-once account creation and starting grant."""

from __future__ import annotations

from psycopg import AsyncConnection

from stockbot.accounts.service import (
    STARTING_GRANT,
    bootstrap_user,
    create_user_account,
)
from stockbot.ledger.service import get_balance


async def test_bootstrap_grants_exactly_once(conn: AsyncConnection) -> None:
    first = await bootstrap_user(conn, 9001)
    assert first.created
    assert await get_balance(conn, first.account_id) == STARTING_GRANT

    again = await bootstrap_user(conn, 9001)
    assert not again.created
    assert again.account_id == first.account_id
    assert await get_balance(conn, first.account_id) == STARTING_GRANT


async def test_create_user_account_reports_created(conn: AsyncConnection) -> None:
    """`created` comes from the INSERT's rowcount under the unique index --
    the reliable first-bootstrap signal (a pre-read SELECT would race)."""
    account_id, created = await create_user_account(conn, 9002)
    assert created

    again, created_again = await create_user_account(conn, 9002)
    assert again == account_id
    assert not created_again
