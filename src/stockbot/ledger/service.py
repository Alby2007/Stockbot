"""Double-entry ledger operations.

Every transfer is exactly two postings to `ledger_entries` (a debit and a
credit) that sum to zero, written in one transaction alongside a cache-column
update on `accounts.balance`. `ledger_entries` itself is append-only (enforced
by a DB trigger); this module never issues UPDATE/DELETE against it.

`post_transfer` always locks the two involved accounts in ascending `id`
order before touching them, per the project's lock-ordering rule
(instruments before accounts, each sorted by id) to prevent deadlocks when
multiple transfers race.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import psycopg
from psycopg import AsyncConnection

from stockbot.ledger.errors import InsufficientFundsError, UnknownAccountError

SYSTEM_ACCOUNTS = ("FAUCET", "SINK", "MARKET_MAKER", "INSURANCE_FUND")


async def get_system_account_id(conn: AsyncConnection, name: str) -> int:
    if name not in SYSTEM_ACCOUNTS:
        raise UnknownAccountError(name)
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT id FROM accounts WHERE kind = 'SYSTEM' AND system_name = %s", (name,)
        )
        row = await cur.fetchone()
    if row is None:
        raise UnknownAccountError(name)
    return int(row[0])


async def get_user_account_id(conn: AsyncConnection, user_id: int) -> int:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT id FROM accounts WHERE kind = 'USER' AND user_id = %s", (user_id,)
        )
        row = await cur.fetchone()
    if row is None:
        raise UnknownAccountError(user_id)
    return int(row[0])


async def get_balance(conn: AsyncConnection, account_id: int) -> int:
    async with conn.cursor() as cur:
        await cur.execute("SELECT balance FROM accounts WHERE id = %s", (account_id,))
        row = await cur.fetchone()
    if row is None:
        raise UnknownAccountError(account_id)
    return int(row[0])


async def post_transfer(
    conn: AsyncConnection,
    *,
    from_account_id: int,
    to_account_id: int,
    amount: int,
    reason: str,
    memo: str | None = None,
) -> UUID:
    """Move `amount` (positive, minor units) from one account to another.

    Writes two ledger postings (-amount / +amount) that sum to zero and
    updates both accounts' balance caches, all in one transaction. Raises
    `InsufficientFundsError` if the debit would take a USER account negative;
    in that case nothing is committed.
    """
    if amount <= 0:
        raise ValueError("transfer amount must be positive")
    if from_account_id == to_account_id:
        raise ValueError("cannot transfer an account to itself")

    transfer_id = uuid4()
    lo, hi = sorted((from_account_id, to_account_id))

    try:
        async with conn.transaction():
            async with conn.cursor() as cur:
                # Lock ordering: always ascending by id, regardless of
                # debit/credit direction. Two statements rather than one
                # `ORDER BY id FOR UPDATE` -- whether FOR UPDATE locks follow
                # the sorted output order is undocumented, so sort in code.
                locked: set[int] = set()
                for account_id in (lo, hi):
                    await cur.execute(
                        "SELECT id FROM accounts WHERE id = %s FOR UPDATE", (account_id,)
                    )
                    row = await cur.fetchone()
                    if row is not None:
                        locked.add(row[0])
                for account_id in (from_account_id, to_account_id):
                    if account_id not in locked:
                        raise UnknownAccountError(account_id)

                await cur.execute(
                    """
                    INSERT INTO ledger_entries (transfer_id, account_id, amount, reason, memo)
                    VALUES (%s, %s, %s, %s, %s), (%s, %s, %s, %s, %s)
                    """,
                    (
                        transfer_id,
                        from_account_id,
                        -amount,
                        reason,
                        memo,
                        transfer_id,
                        to_account_id,
                        amount,
                        reason,
                        memo,
                    ),
                )
                await cur.execute(
                    "UPDATE accounts SET balance = balance - %s WHERE id = %s",
                    (amount, from_account_id),
                )
                await cur.execute(
                    "UPDATE accounts SET balance = balance + %s WHERE id = %s",
                    (amount, to_account_id),
                )
    except psycopg.errors.CheckViolation as exc:
        raise InsufficientFundsError(from_account_id) from exc

    return transfer_id


async def record_idempotency_key(conn: AsyncConnection, interaction_id: str) -> None:
    """Insert `interaction_id`; raise `DuplicateInteractionError` if seen.

    Every user-triggered money-moving path (trades, bounded shorts, shop
    buys) calls this first inside its transaction so a redelivered Discord
    interaction is a no-op instead of a double charge.
    """
    # Deferred import: trading.errors is downstream of this module
    # (trading.service imports ledger.service), so a top-level import would
    # be circular.
    from stockbot.trading.errors import DuplicateInteractionError

    async with conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO idempotency_keys (interaction_id) VALUES (%s) ON CONFLICT DO NOTHING",
            (interaction_id,),
        )
        if cur.rowcount == 0:
            raise DuplicateInteractionError(interaction_id)
