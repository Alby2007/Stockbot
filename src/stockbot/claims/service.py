"""Daily faucet claims.

Deliberately thin, per the design doc: this should never be competitive with
trading well. Streak resets on any missed day; the bonus caps out well below
what a single good trade nets.
"""

from __future__ import annotations

from psycopg import AsyncConnection

from stockbot.accounts.service import bootstrap_user
from stockbot.claims.errors import AlreadyClaimedTodayError
from stockbot.ledger.service import get_system_account_id, post_transfer

BASE_CLAIM_MINOR = 200  # $2.00
STREAK_BONUS_PER_DAY_MINOR = 20  # $0.20 per consecutive day
MAX_STREAK_DAYS = 10  # bonus caps at day 10: $2.00 + 9 * $0.20 = $3.80


def claim_amount(streak: int) -> int:
    bonus_days = min(streak - 1, MAX_STREAK_DAYS - 1)
    return BASE_CLAIM_MINOR + bonus_days * STREAK_BONUS_PER_DAY_MINOR


async def claim_daily(conn: AsyncConnection, user_id: int) -> tuple[int, int]:
    """Claim today's faucet grant. Returns (amount_minor, streak).

    Raises `AlreadyClaimedTodayError` if this user already claimed today
    (server date, UTC). A streak continues if the previous claim was
    yesterday; any bigger gap resets it to 1.
    """
    async with conn.transaction():
        await bootstrap_user(conn, user_id)
        account_id_row = None
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT id FROM accounts WHERE kind = 'USER' AND user_id = %s FOR UPDATE",
                (user_id,),
            )
            account_id_row = await cur.fetchone()
        assert account_id_row is not None
        account_id = account_id_row[0]

        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT last_claim_date, streak FROM claims WHERE user_id = %s FOR UPDATE",
                (user_id,),
            )
            existing = await cur.fetchone()

            await cur.execute("SELECT CURRENT_DATE")
            today_row = await cur.fetchone()
            assert today_row is not None
            today = today_row[0]

        if existing is not None:
            last_claim_date, previous_streak = existing
            if last_claim_date == today:
                raise AlreadyClaimedTodayError(user_id)
            streak = previous_streak + 1 if (today - last_claim_date).days == 1 else 1
        else:
            streak = 1

        amount = claim_amount(streak)

        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO claims (user_id, last_claim_date, streak)
                VALUES (%s, %s, %s)
                ON CONFLICT (user_id) DO UPDATE SET last_claim_date = EXCLUDED.last_claim_date,
                                                     streak = EXCLUDED.streak
                """,
                (user_id, today, streak),
            )

        faucet_id = await get_system_account_id(conn, "FAUCET")
        await post_transfer(
            conn, from_account_id=faucet_id, to_account_id=account_id, amount=amount, reason="CLAIM"
        )

    return amount, streak
