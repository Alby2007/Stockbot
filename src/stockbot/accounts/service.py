"""User + account bootstrap."""

from __future__ import annotations

from psycopg import AsyncConnection

from stockbot.ledger.service import get_system_account_id, post_transfer

STARTING_GRANT = 10_000  # minor units; placeholder one-time starting balance


async def create_user_account(conn: AsyncConnection, user_id: int) -> int:
    """Idempotently ensure a user + zero-balance cash account exist. Returns the account id."""
    async with conn.transaction():
        async with conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO users (id) VALUES (%s) ON CONFLICT (id) DO NOTHING", (user_id,)
            )
            await cur.execute(
                """
                INSERT INTO accounts (kind, user_id, balance)
                VALUES ('USER', %s, 0)
                ON CONFLICT (user_id) WHERE kind = 'USER' DO NOTHING
                """,
                (user_id,),
            )
            await cur.execute(
                "SELECT id FROM accounts WHERE kind = 'USER' AND user_id = %s", (user_id,)
            )
            row = await cur.fetchone()
    assert row is not None
    return int(row[0])


async def bootstrap_user(conn: AsyncConnection, user_id: int) -> int:
    """Create a user's account if needed and grant the one-time starting balance.

    Safe to call more than once: the starting grant is only issued the first
    time the account is created.
    """
    async with conn.cursor() as cur:
        await cur.execute("SELECT 1 FROM users WHERE id = %s", (user_id,))
        already_existed = await cur.fetchone() is not None

    account_id = await create_user_account(conn, user_id)

    if not already_existed:
        faucet_id = await get_system_account_id(conn, "FAUCET")
        await post_transfer(
            conn,
            from_account_id=faucet_id,
            to_account_id=account_id,
            amount=STARTING_GRANT,
            reason="STARTING_GRANT",
        )

    return account_id
