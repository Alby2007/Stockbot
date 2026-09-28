"""Daily faucet claims.

Deliberately thin, per the design doc: this should never be competitive with
trading well. Streak resets on any missed day; the bonus caps out well below
what a single good trade nets.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from psycopg import AsyncConnection

from stockbot.accounts.service import (
    bootstrap_user,
    discord_age_days,
    min_discord_age_days,
)
from stockbot.claims.errors import (
    AccountTooYoungError,
    AlreadyClaimedTodayError,
    FirstClaimLockedError,
)
from stockbot.config import get_settings
from stockbot.feed import emit_feed
from stockbot.ledger.service import get_system_account_id, post_transfer
from stockbot.shop.service import owns_item, use_consumable

BASE_CLAIM_MINOR = 200  # $2.00
STREAK_BONUS_PER_DAY_MINOR = 20  # $0.20 per consecutive day
MAX_STREAK_DAYS = 10  # bonus caps at day 10: $2.00 + 9 * $0.20 = $3.80


def claim_amount(streak: int) -> int:
    bonus_days = min(streak - 1, MAX_STREAK_DAYS - 1)
    return BASE_CLAIM_MINOR + bonus_days * STREAK_BONUS_PER_DAY_MINOR


FIRST_CLAIM_DELAY = timedelta(hours=24)

# Wheel segments: (key, weight, payout as a multiple of BASE_CLAIM_MINOR).
# The jackpot segment's weight is REPLACED by the claim.jackpot_pct config
# knob at draw time; `u` is normalized over the summed weights, so
# jackpot_pct=0 cleanly disables the jackpot without redistributing it.
# EV at streak 1 = 1.43x base (~$2.86) at the default 2% jackpot.
WHEEL_SEGMENTS: tuple[tuple[str, float, float], ...] = (
    ("cold", 50.0, 0.75),
    ("even", 30.0, 1.25),
    ("warm", 12.0, 2.0),
    ("hot", 6.0, 4.0),
    ("jackpot", 2.0, 10.0),
)


@dataclass(frozen=True)
class WheelRoll:
    segment: str
    mult: float
    roll_minor: int


@dataclass(frozen=True)
class ClaimResult:
    amount_minor: int
    streak: int
    shield_used: bool
    # None when claim.wheel_enabled=0 -- the claim paid the flat formula.
    roll: WheelRoll | None = None


def _segment_for_u(u: float, jackpot_pct: float) -> tuple[str, float]:
    """Map u in [0,1) to (segment, multiplier). Pure -- boundary-testable."""
    total = sum(jackpot_pct if k == "jackpot" else w for k, w, _ in WHEEL_SEGMENTS)
    if total <= 0:
        return WHEEL_SEGMENTS[0][0], WHEEL_SEGMENTS[0][2]
    x = u * total
    acc = 0.0
    for key, weight, mult in WHEEL_SEGMENTS:
        acc += jackpot_pct if key == "jackpot" else weight
        if x < acc:
            return key, mult
    return WHEEL_SEGMENTS[-1][0], WHEEL_SEGMENTS[-1][2]


def _wheel_u(user_id: int, day: date, seed: str) -> float:
    """The raw draw: HMAC(seed, 'claim|{user_id}|{date}') -> u in [0,1)."""
    mac = hmac.new(
        seed.encode(),
        f"claim|{user_id}|{day.isoformat()}".encode(),
        hashlib.sha256,
    ).digest()
    return int.from_bytes(mac[:8], "big") / float(1 << 64)


def wheel_roll(user_id: int, day: date, seed: str, jackpot_pct: float) -> WheelRoll:
    """Deterministic wheel draw for a user's daily claim -- same pattern
    as engine.tick_seed. Same (user, day, seed) always rolls the same
    segment: a retried claim can't re-roll and sim runs reproduce exactly.
    """
    segment, mult = _segment_for_u(_wheel_u(user_id, day, seed), jackpot_pct)
    return WheelRoll(
        segment=segment, mult=mult, roll_minor=round(BASE_CLAIM_MINOR * mult)
    )


async def _wheel_config(conn: AsyncConnection) -> tuple[bool, float]:
    """(wheel_enabled, jackpot_pct) -- one read inside the claim txn."""
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT key, value FROM config "
            "WHERE key IN ('claim.wheel_enabled', 'claim.jackpot_pct')"
        )
        rows: dict[str, float] = dict(await cur.fetchall())
    return (
        float(rows.get("claim.wheel_enabled", 1.0)) != 0.0,
        float(rows.get("claim.jackpot_pct", 2.0)),
    )


async def claim_daily(
    conn: AsyncConnection,
    user_id: int,
    *,
    as_of_date: date | None = None,
    enforce_first_claim_delay: bool = True,
    wheel_seed: str | None = None,
) -> ClaimResult:
    """Claim today's faucet grant. Returns a `ClaimResult` -- amount,
    streak, shield usage, and the wheel roll (None when the wheel is
    disabled via `claim.wheel_enabled`).

    Raises `AlreadyClaimedTodayError` if this user already claimed today
    (server date, UTC). A streak continues if the previous claim was
    yesterday; any bigger gap resets it to 1.

    Two anti-farm gates live HERE, not just at the command layer (H1
    defense-in-depth): the Discord snowflake must be older than
    `accounts.min_discord_age_days` (`AccountTooYoungError`), and a
    brand-new bot account waits FIRST_CLAIM_DELAY before its first claim
    (`FirstClaimLockedError`, which carries the unlock instant).

    `as_of_date` overrides "today" -- the simulation harness passes
    simulated dates so a 90-simulated-day run exercises the real claim path
    instead of collapsing into the single real date the run happens on; it
    also passes `enforce_first_claim_delay=False` since its users'
    `users.created_at` is wall-clock now, not sim time.
    """
    async with conn.transaction():
        await bootstrap_user(conn, user_id)
        min_age = await min_discord_age_days(conn)
        if discord_age_days(user_id) < min_age:
            raise AccountTooYoungError(min_age)
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

            if enforce_first_claim_delay and existing is None:
                await cur.execute(
                    "SELECT created_at FROM users WHERE id = %s", (user_id,)
                )
                created_row = await cur.fetchone()
                assert created_row is not None  # bootstrap ran in this tx
                (created_at,) = created_row
                unlock_at = created_at + FIRST_CLAIM_DELAY
                if datetime.now(UTC) < unlock_at:
                    raise FirstClaimLockedError(unlock_at)

            if as_of_date is None:
                await cur.execute("SELECT CURRENT_DATE")
                today_row = await cur.fetchone()
                assert today_row is not None
                today = today_row[0]
            else:
                today = as_of_date

        shield_used = False
        if existing is not None:
            last_claim_date, previous_streak = existing
            if last_claim_date == today:
                raise AlreadyClaimedTodayError(user_id)
            gap = (today - last_claim_date).days
            if gap == 2 and await owns_item(conn, user_id, "streak_shield"):
                # Exactly one missed day: the shield is consumed and the
                # streak reads as continuous (+1), i.e. the missed day
                # was claimed. Longer gaps don't consume -- a shield
                # shouldn't resurrect a month-old streak.
                await use_consumable(conn, user_id, "streak_shield")
                shield_used = True
                streak = previous_streak + 1
            else:
                streak = previous_streak + 1 if gap == 1 else 1
        else:
            streak = 1

        wheel_enabled, jackpot_pct = await _wheel_config(conn)
        roll: WheelRoll | None = None
        if wheel_enabled:
            roll = wheel_roll(
                user_id,
                today,
                wheel_seed or get_settings().master_seed,
                jackpot_pct,
            )
            # Streak multiplies the wheel result along the existing streak
            # curve: claim_amount(streak)/BASE is 1.0x on day 1 rising to
            # the same 1.9x cap the flat formula had at day 10+.
            amount = round(roll.roll_minor * claim_amount(streak) / BASE_CLAIM_MINOR)
        else:
            amount = claim_amount(streak)

        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO claims (user_id, last_claim_date, streak,
                                    last_segment, last_amount_minor)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (user_id) DO UPDATE SET last_claim_date = EXCLUDED.last_claim_date,
                                                     streak = EXCLUDED.streak,
                                                     last_segment = EXCLUDED.last_segment,
                                                     last_amount_minor = EXCLUDED.last_amount_minor
                """,
                (user_id, today, streak, roll.segment if roll else None, amount),
            )

        faucet_id = await get_system_account_id(conn, "FAUCET")
        await post_transfer(
            conn, from_account_id=faucet_id, to_account_id=account_id, amount=amount, reason="CLAIM"
        )
        if roll is not None and roll.segment == "jackpot":
            # Public tape: the jackpot is the wheel's social proof.
            await emit_feed(
                conn,
                "JACKPOT",
                {"amount": amount, "streak": streak},
                user_id=user_id,
            )

    return ClaimResult(
        amount_minor=amount, streak=streak, shield_used=shield_used, roll=roll
    )
