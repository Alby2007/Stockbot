"""User + account bootstrap."""

from __future__ import annotations

from dataclasses import dataclass

from psycopg import AsyncConnection

from stockbot.ledger.service import get_system_account_id, post_transfer

STARTING_GRANT = 10_000  # minor units; placeholder one-time starting balance


@dataclass(frozen=True)
class BootstrapResult:
    account_id: int
    created: bool


async def create_user_account(conn: AsyncConnection, user_id: int) -> tuple[int, bool]:
    """Idempotently ensure a user + zero-balance cash account exist.

    Returns (account_id, created). `created` is True iff this call inserted
    the `users` row, derived from the INSERT's rowcount under the unique
    index -- two concurrent first-uses serialize on the conflict and only
    the winner sees `created`, so it's a safe "first bootstrap" signal.
    """
    async with conn.transaction():
        async with conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO users (id) VALUES (%s) ON CONFLICT (id) DO NOTHING",
                (user_id,),
            )
            created = cur.rowcount == 1
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
    return int(row[0]), created


async def bootstrap_user(conn: AsyncConnection, user_id: int) -> BootstrapResult:
    """Create a user's account if needed and grant the one-time starting balance.

    Safe to call more than once: the starting grant is only issued by the
    call that actually created the user row. The create + grant run in one
    transaction -- no bare statements before the first `conn.transaction()`
    (a bare statement would open an implicit transaction and silently
    demote every later block to a savepoint that never commits).

    Returns `BootstrapResult(account_id, created)` -- `created` is how
    callers tell first-use from repeat (N3: the welcome message).
    """
    async with conn.transaction():
        account_id, created = await create_user_account(conn, user_id)
        if created:
            faucet_id = await get_system_account_id(conn, "FAUCET")
            await post_transfer(
                conn,
                from_account_id=faucet_id,
                to_account_id=account_id,
                amount=STARTING_GRANT,
                reason="STARTING_GRANT",
            )
    return BootstrapResult(account_id, created)
