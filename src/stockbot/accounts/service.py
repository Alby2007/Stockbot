"""User + account bootstrap."""

from __future__ import annotations

import time
from dataclasses import dataclass

from psycopg import AsyncConnection

from stockbot.accounts.errors import UserDisabledError
from stockbot.ledger.service import get_system_account_id, post_transfer

STARTING_GRANT = 10_000  # minor units; placeholder one-time starting balance

_DISCORD_EPOCH_MS = 1_420_070_400_000  # 2015-01-01T00:00:00Z
DEFAULT_MIN_DISCORD_AGE_DAYS = 30


def discord_age_days(user_id: int, *, now_ms: int | None = None) -> float:
    """Discord account age from the snowflake's timestamp bits -- pure
    int math, no API call, no discord.py import. Every pre-2026 test id
    (3001, SYNTHETIC_USER_ID_BASE, ...) resolves to ~2015, so they're all
    eligible automatically; random ids above ~2**57 land in the FUTURE
    and stay grant-pending forever -- generate test snowflakes anchored
    at real time, not bare randranges."""
    created_ms = (int(user_id) >> 22) + _DISCORD_EPOCH_MS
    now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    return (now_ms - created_ms) / 86_400_000


async def min_discord_age_days(conn: AsyncConnection) -> int:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT value FROM config WHERE key = 'accounts.min_discord_age_days'"
        )
        row = await cur.fetchone()
    return int(row[0]) if row else DEFAULT_MIN_DISCORD_AGE_DAYS


@dataclass(frozen=True)
class BootstrapResult:
    account_id: int
    created: bool
    # H1: the grant is decoupled from creation. granted_now = this call
    # issued the grant; grant_pending = account exists but the snowflake
    # is younger than accounts.min_discord_age_days -- it self-heals on
    # the first bootstrap after aging, no cron needed.
    granted_now: bool = False
    grant_pending: bool = False
    # The threshold evaluated this call -- only meaningful when
    # grant_pending; lets the welcome copy quote the real configured age.
    min_age_days: int = DEFAULT_MIN_DISCORD_AGE_DAYS


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


async def bootstrap_user(
    conn: AsyncConnection, user_id: int, *, starting_grant: bool = True
) -> BootstrapResult:
    """Create a user's account if needed; grant the starting balance once
    the Discord snowflake is old enough.

    `starting_grant=False` (the NPC spawn path) stamps `grant_issued`
    without posting -- the anti-farm age check is snowflake-only and
    synthetic ids trivially satisfy it, so without the opt-out every
    bot would draw the faucet grant on top of NPC_STAKE.

    Safe to call more than once: `grant_issued` is claimed under the
    FOR UPDATE row lock before posting, so two concurrent pending-user
    bootstraps can't double-grant. The create + grant run in one
    transaction -- no bare statements before the first
    `conn.transaction()` (a bare statement would open an implicit
    transaction and silently demote every later block to a savepoint
    that never commits).

    Raises `UserDisabledError` when the user is suspended -- THE per-user
    enforcement point (H2): every user-initiated path goes through here,
    while market mechanics (fills, liquidation sweeps) never call this.

    Returns `BootstrapResult` -- `created` is how callers tell first-use
    from repeat (N3 welcome); `granted_now`/`grant_pending` pick between
    the funded and deferred welcome variants (H1).
    """
    async with conn.transaction():
        account_id, created = await create_user_account(conn, user_id)
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT grant_issued, disabled_at, disabled_reason FROM users "
                "WHERE id = %s FOR UPDATE",
                (user_id,),
            )
            row = await cur.fetchone()
            assert row is not None
            grant_issued, disabled_at, disabled_reason = row
            if disabled_at is not None:
                raise UserDisabledError(user_id, disabled_reason)

            granted_now = False
            grant_pending = False
            min_age = DEFAULT_MIN_DISCORD_AGE_DAYS
            if not grant_issued:
                min_age = await min_discord_age_days(conn)
                if not starting_grant:
                    # Burn the entitlement: the account is funded by a
                    # dedicated path (e.g. NPC_STAKE), and stamping
                    # grant_issued here keeps a later user-path bootstrap
                    # from drawing the grant anyway.
                    await cur.execute(
                        "UPDATE users SET grant_issued = TRUE WHERE id = %s",
                        (user_id,),
                    )
                elif discord_age_days(user_id) >= min_age:
                    await cur.execute(
                        "UPDATE users SET grant_issued = TRUE WHERE id = %s",
                        (user_id,),
                    )
                    faucet_id = await get_system_account_id(conn, "FAUCET")
                    await post_transfer(
                        conn,
                        from_account_id=faucet_id,
                        to_account_id=account_id,
                        amount=STARTING_GRANT,
                        reason="STARTING_GRANT",
                    )
                    granted_now = True
                else:
                    grant_pending = True
    return BootstrapResult(account_id, created, granted_now, grant_pending, min_age)
