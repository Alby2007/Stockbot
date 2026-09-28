"""Seasons + League: the soft-reset competitive track.

The persistent portfolio is never reset. A season is an opt-in league that
runs for a fixed tick range: entrants pay an entry fee (destroyed in SINK),
receive a fixed equal stake into a LEAGUE account, and trade it in
season-scoped positions. League wealth cannot leave the league -- the only
transfers touching a LEAGUE account are the initial stake, trade legs with
MARKET_MAKER, fees to SINK, and the final sweep back to SINK at close.

Scoring deliberately does not reward lottery play: daily equity snapshots
feed a Sharpe-like score, mean(daily log return) / stdev * sqrt(N), with
qualification floors on active days and trade count. Prize pool = the entry
fees collected; the same amount is re-minted from FAUCET as prizes, so the
season as a whole is faucet/sink neutral.

`on_tick` is called by the singleton market process inside `apply_tick`'s
transaction: it activates due seasons, writes day-boundary equity
snapshots, and closes finished seasons (scores, ranks, prizes, sweeps).
"""

from __future__ import annotations

import json
import math
import statistics
from dataclasses import dataclass
from typing import Any

from psycopg import AsyncConnection
from psycopg.errors import UniqueViolation
from psycopg.rows import dict_row

from stockbot.accounts.service import bootstrap_user
from stockbot.ledger.service import (
    get_balance,
    get_system_account_id,
    get_user_account_id,
    post_transfer,
)
from stockbot.margin import service as margin
from stockbot.market.engine import TICKS_PER_DAY
from stockbot.seasons.errors import (
    AlreadyEnteredError,
    NoOpenSeasonError,
    SandboxAlreadyOpenError,
    SeasonNotFoundError,
    SeasonNotOpenError,
)

SEASON_DAYS = 42  # 6 weeks
DEFAULT_ENTRY_FEE_MINOR = 1_000  # $10.00
DEFAULT_STAKE_MINOR = 100_000  # $1,000.00

# Qualification floors so a lucky one-trade YOLO can't win on raw return.
MIN_ACTIVE_DAYS = 20
MIN_TRADES = 10

# Prize pool (the collected entry fees) split across the top qualifiers.
PRIZE_SHARES = (0.50, 0.30, 0.20)


@dataclass(frozen=True)
class Season:
    id: int
    name: str
    status: str
    start_tick: int
    end_tick: int
    entry_fee_minor: int
    stake_minor: int
    prize_pool_minor: int
    sandbox_user_id: int | None = None


@dataclass(frozen=True)
class Standing:
    user_id: int
    equity_minor: int
    trades_count: int
    final_score: float | None
    final_rank: int | None
    prize_minor: int


def _season_from_row(row: dict[str, Any]) -> Season:
    return Season(
        id=int(row["id"]),
        name=str(row["name"]),
        status=str(row["status"]),
        start_tick=int(row["start_tick"]),
        end_tick=int(row["end_tick"]),
        entry_fee_minor=int(row["entry_fee_minor"]),
        stake_minor=int(row["stake_minor"]),
        prize_pool_minor=int(row["prize_pool_minor"]),
        sandbox_user_id=(
            int(row["sandbox_user_id"]) if row["sandbox_user_id"] is not None else None
        ),
    )


async def current_tick(conn: AsyncConnection) -> int:
    """Latest applied tick index; -1 if the market has never ticked."""
    async with conn.cursor() as cur:
        await cur.execute("SELECT COALESCE(MAX(tick_index), -1) FROM market_ticks")
        row = await cur.fetchone()
    assert row is not None
    return int(row[0])


async def create_season(
    conn: AsyncConnection,
    *,
    name: str,
    start_tick: int,
    end_tick: int,
    entry_fee_minor: int = DEFAULT_ENTRY_FEE_MINOR,
    stake_minor: int = DEFAULT_STAKE_MINOR,
) -> int:
    async with conn.transaction():
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO seasons (name, start_tick, end_tick, entry_fee_minor, stake_minor)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING id
                """,
                (name, start_tick, end_tick, entry_fee_minor, stake_minor),
            )
            row = await cur.fetchone()
    assert row is not None
    return int(row[0])


async def get_season(conn: AsyncConnection, season_id: int) -> Season | None:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute("SELECT * FROM seasons WHERE id = %s", (season_id,))
        row = await cur.fetchone()
    return _season_from_row(row) if row else None


async def get_open_season(conn: AsyncConnection) -> Season | None:
    """The season joinable right now: the ACTIVE one, else the next SCHEDULED."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            SELECT * FROM seasons
            WHERE status IN ('ACTIVE', 'SCHEDULED')
              AND sandbox_user_id IS NULL
            ORDER BY CASE status WHEN 'ACTIVE' THEN 0 ELSE 1 END, start_tick
            LIMIT 1
            """
        )
        row = await cur.fetchone()
    return _season_from_row(row) if row else None


async def get_latest_season(conn: AsyncConnection) -> Season | None:
    """The open season if any, else the most recently closed/scheduled one."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            SELECT * FROM seasons
            WHERE sandbox_user_id IS NULL
            ORDER BY CASE status WHEN 'ACTIVE' THEN 0 WHEN 'SCHEDULED' THEN 1 ELSE 2 END,
                     start_tick DESC
            LIMIT 1
            """
        )
        row = await cur.fetchone()
    return _season_from_row(row) if row else None


async def join_season(conn: AsyncConnection, user_id: int, season_id: int | None = None) -> int:
    """Enter a user into a season. Returns the league account id.

    One transaction: entry fee user->SINK, new LEAGUE account, stake
    FAUCET->league account, season_entries row. Rejoining is rejected.
    """
    async with conn.transaction():
        if season_id is None:
            season = await get_open_season(conn)
            if season is None:
                raise NoOpenSeasonError()
        else:
            season = await get_season(conn, season_id)
            if season is None:
                raise SeasonNotFoundError(season_id)
        if season.status == "CLOSED":
            raise SeasonNotOpenError(season.id, season.status)

        # Hold the season row FOR SHARE until commit: a racing
        # close_season's status UPDATE blocks on this lock, and a close
        # that already claimed the row is seen here on the re-read (plain
        # `season.status` above could have gone stale in between).
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT status FROM seasons WHERE id = %s FOR SHARE", (season.id,)
            )
            locked = await cur.fetchone()
            assert locked is not None
            if locked[0] == "CLOSED":
                raise SeasonNotOpenError(season.id, str(locked[0]))

        account_id = (await bootstrap_user(conn, user_id)).account_id

        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT 1 FROM season_entries WHERE season_id = %s AND user_id = %s",
                (season.id, user_id),
            )
            if await cur.fetchone() is not None:
                raise AlreadyEnteredError(season.id, user_id)

        tick = await current_tick(conn)

        async with conn.cursor() as cur:
            try:
                await cur.execute(
                    """
                    INSERT INTO accounts (kind, user_id, season_id, balance)
                    VALUES ('LEAGUE', %s, %s, 0)
                    RETURNING id
                    """,
                    (user_id, season.id),
                )
            except UniqueViolation:
                # Check-then-insert isn't enough: a concurrent join passes
                # the season_entries probe above, then trips the league-
                # account unique index here. Map it to the domain error.
                raise AlreadyEnteredError(season.id, user_id) from None
            row = await cur.fetchone()
            assert row is not None
            league_account_id = int(row[0])

        sink_id = await get_system_account_id(conn, "SINK")
        faucet_id = await get_system_account_id(conn, "FAUCET")
        if season.entry_fee_minor > 0:
            await margin.assert_spend_ok(conn, user_id, season.entry_fee_minor)
            await post_transfer(
                conn,
                from_account_id=account_id,
                to_account_id=sink_id,
                amount=season.entry_fee_minor,
                reason="LEAGUE_ENTRY_FEE",
                memo=season.name,
            )
        await post_transfer(
            conn,
            from_account_id=faucet_id,
            to_account_id=league_account_id,
            amount=season.stake_minor,
            reason="LEAGUE_STAKE",
            memo=season.name,
        )

        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO season_entries (season_id, user_id, account_id, joined_tick)
                VALUES (%s, %s, %s, %s)
                """,
                (season.id, user_id, league_account_id, max(tick, 0)),
            )
    return league_account_id


async def get_league_account_id(
    conn: AsyncConnection, user_id: int, season_id: int
) -> int | None:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT id FROM accounts WHERE kind = 'LEAGUE' AND user_id = %s AND season_id = %s",
            (user_id, season_id),
        )
        row = await cur.fetchone()
    return int(row[0]) if row else None


async def get_active_entry(
    conn: AsyncConnection, user_id: int, season_id: int | None = None
) -> tuple[int, int] | None:
    """Resolve the user's (season_id, league_account_id) in an ACTIVE season.

    With no season_id, picks the user's most recent active-season entry.
    Returns None if the user isn't entered in any active season.
    """
    async with conn.cursor() as cur:
        if season_id is None:
            await cur.execute(
                """
                SELECT e.season_id, e.account_id
                FROM season_entries e
                JOIN seasons s ON s.id = e.season_id
                WHERE e.user_id = %s AND s.status = 'ACTIVE'
                  AND s.sandbox_user_id IS NULL
                ORDER BY s.start_tick DESC
                LIMIT 1
                """,
                (user_id,),
            )
        else:
            await cur.execute(
                """
                SELECT e.season_id, e.account_id
                FROM season_entries e
                JOIN seasons s ON s.id = e.season_id
                WHERE e.user_id = %s AND e.season_id = %s AND s.status = 'ACTIVE'
                  AND s.sandbox_user_id IS NULL
                """,
                (user_id, season_id),
            )
        row = await cur.fetchone()
    return (int(row[0]), int(row[1])) if row else None


# Sandbox: a personal, resettable practice season. sandbox_user_id marks the
# season as private -- get_open_season/get_active_entry filter those rows out
# so a sandbox never becomes "the season" for league joins, standings, or the
# league flag's entry resolution; the sandbox flag resolves it explicitly via
# get_sandbox_entry. Close takes the sandbox branch of _close_season_claimed:
# no scoring, trophies, or result DMs, just the SINK sweep.
SANDBOX_END_TICK_OFFSET = 2_000_000_000  # effectively never auto-closes


async def _sandbox_stake_minor(conn: AsyncConnection) -> int:
    async with conn.cursor() as cur:
        await cur.execute("SELECT value FROM config WHERE key = 'sandbox.stake_minor'")
        row = await cur.fetchone()
    return int(row[0]) if row else 10_000


async def get_sandbox_entry(conn: AsyncConnection, user_id: int) -> tuple[int, int] | None:
    """The user's (season_id, league_account_id) in their ACTIVE sandbox."""
    async with conn.cursor() as cur:
        await cur.execute(
            """
            SELECT e.season_id, e.account_id
            FROM seasons s
            JOIN season_entries e ON e.season_id = s.id AND e.user_id = %s
            WHERE s.sandbox_user_id = %s AND s.status = 'ACTIVE'
            LIMIT 1
            """,
            (user_id, user_id),
        )
        row = await cur.fetchone()
    return (int(row[0]), int(row[1])) if row else None


async def open_sandbox(conn: AsyncConnection, user_id: int) -> int:
    """Create the user's sandbox season and join it. Returns the season id."""
    existing = await get_sandbox_entry(conn, user_id)
    if existing is not None:
        raise SandboxAlreadyOpenError(existing[0])
    tick = await current_tick(conn)
    stake_minor = await _sandbox_stake_minor(conn)
    async with conn.transaction():
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO seasons
                    (name, status, start_tick, end_tick, entry_fee_minor,
                     stake_minor, sandbox_user_id)
                VALUES (%s, 'ACTIVE', %s, %s, 0, %s, %s)
                RETURNING id
                """,
                (
                    f"Sandbox {user_id}",
                    max(tick, 0),
                    max(tick, 0) + SANDBOX_END_TICK_OFFSET,
                    stake_minor,
                    user_id,
                ),
            )
            row = await cur.fetchone()
            assert row is not None
            season_id = int(row[0])
    # Fee 0 skips the spend check inside join_season; the stake mints
    # FAUCET -> league account, which stays quarantined there.
    await join_season(conn, user_id, season_id)
    return season_id


async def reset_sandbox(conn: AsyncConnection, user_id: int) -> int:
    """Close the running sandbox and open a fresh one. Returns the new id."""
    existing = await get_sandbox_entry(conn, user_id)
    if existing is not None:
        await close_season(conn, existing[0])
    return await open_sandbox(conn, user_id)


# Reusable subquery: mark-to-market value of open bounded shorts, floored at
# 0 per position (collateral + Q*(entry - quoted), in minor units). The
# collateral already left the league account at open, so the position's
# liquidation value is what equity sees.
_SHORT_VALUE_SUBQUERY = """
    SELECT bs.season_id, bs.user_id,
           SUM(GREATEST(0, bs.collateral_minor
                 + CAST(bs.quantity * (bs.entry_price - i.quoted_price) * 100 AS BIGINT)))
               AS short_value_minor
    FROM bounded_shorts bs
    JOIN instruments i ON i.id = bs.instrument_id
    WHERE bs.status = 'OPEN' AND bs.season_id IS NOT NULL
    GROUP BY bs.season_id, bs.user_id
"""

# Open options mark-to-model per (season, user); the same join serves
# league equity and /sandbox status.
_OPTION_VALUE_SUBQUERY = """
    SELECT o.season_id, o.user_id, SUM(o.mark_minor * o.quantity) AS option_value_minor
    FROM option_positions o
    WHERE o.status = 'OPEN' AND o.season_id IS NOT NULL
    GROUP BY o.season_id, o.user_id
"""


async def league_equity_minor(conn: AsyncConnection, season_id: int, user_id: int) -> int:
    """League account cash + mark value of season-scoped positions and shorts."""
    async with conn.cursor() as cur:
        await cur.execute(
            f"""
            SELECT a.balance + COALESCE(pos.value_minor, 0)
                          + COALESCE(sh.short_value_minor, 0)
                          + COALESCE(opt.option_value_minor, 0)
            FROM season_entries e
            JOIN accounts a ON a.id = e.account_id
            LEFT JOIN (
                SELECT p.user_id,
                       CAST(SUM(p.quantity * i.quoted_price * 100
                                - p.borrow_fees_accrued
                                - p.dividends_accrued) AS BIGINT) AS value_minor
                FROM positions p
                JOIN instruments i ON i.id = p.instrument_id
                WHERE p.season_id = %s AND p.quantity <> 0
                GROUP BY p.user_id
            ) pos ON pos.user_id = e.user_id
            LEFT JOIN ({_SHORT_VALUE_SUBQUERY}) sh
                   ON sh.season_id = e.season_id AND sh.user_id = e.user_id
            LEFT JOIN ({_OPTION_VALUE_SUBQUERY}) opt
                   ON opt.season_id = e.season_id AND opt.user_id = e.user_id
            WHERE e.season_id = %s AND e.user_id = %s
            """,
            (season_id, season_id, user_id),
        )
        row = await cur.fetchone()
    return int(row[0]) if row else 0


def league_score(equities_minor: list[int]) -> float | None:
    """Sharpe-like score over daily equities: mean(log return)/stdev * sqrt(N).

    Returns None if there aren't enough snapshots to qualify. A perfectly
    flat series scores 0 -- never trading isn't a strategy worth rewarding.
    """
    if len(equities_minor) < MIN_ACTIVE_DAYS + 1:
        return None
    returns = [
        math.log(later / earlier)
        for earlier, later in zip(equities_minor, equities_minor[1:], strict=False)
        if earlier > 0 and later > 0
    ]
    if len(returns) < MIN_ACTIVE_DAYS:
        return None
    sd = statistics.stdev(returns)
    if sd == 0:
        return 0.0
    return statistics.fmean(returns) / sd * math.sqrt(len(returns))


async def on_tick(
    conn: AsyncConnection, tick_index: int, *, snapshot_interval_ticks: int = TICKS_PER_DAY
) -> None:
    """Season lifecycle maintenance, called once per tick inside apply_tick's
    transaction. Cheap when no season is live: one UPDATE no-op + one SELECT.
    """
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE seasons SET status = 'ACTIVE' WHERE status = 'SCHEDULED' AND start_tick <= %s",
            (tick_index,),
        )

        if tick_index % snapshot_interval_ticks == 0:
            day_index = tick_index // snapshot_interval_ticks
            await cur.execute(
                f"""
                INSERT INTO equity_snapshots
                    (season_id, user_id, day_index, tick_index, equity_minor)
                SELECT e.season_id, e.user_id, %s, %s,
                       a.balance + COALESCE(pos.value_minor, 0)
                             + COALESCE(sh.short_value_minor, 0)
                             + COALESCE(opt.option_value_minor, 0)
                FROM season_entries e
                JOIN seasons s ON s.id = e.season_id
                JOIN accounts a ON a.id = e.account_id
                LEFT JOIN (
                    SELECT p.season_id, p.user_id,
                           CAST(SUM(p.quantity * i.quoted_price * 100
                                    - p.borrow_fees_accrued
                                    - p.dividends_accrued) AS BIGINT)
                               AS value_minor
                    FROM positions p
                    JOIN instruments i ON i.id = p.instrument_id
                    WHERE p.season_id IS NOT NULL AND p.quantity <> 0
                    GROUP BY p.season_id, p.user_id
                ) pos ON pos.season_id = e.season_id AND pos.user_id = e.user_id
                LEFT JOIN ({_SHORT_VALUE_SUBQUERY}) sh
                       ON sh.season_id = e.season_id AND sh.user_id = e.user_id
                LEFT JOIN ({_OPTION_VALUE_SUBQUERY}) opt
                       ON opt.season_id = e.season_id AND opt.user_id = e.user_id
                WHERE s.status = 'ACTIVE'
                  AND s.sandbox_user_id IS NULL
                  AND s.start_tick <= %s AND %s <= s.end_tick
                ON CONFLICT DO NOTHING
                """,
                (day_index, tick_index, tick_index, tick_index),
            )

        await cur.execute(
            "SELECT id FROM seasons WHERE status = 'ACTIVE' AND end_tick <= %s",
            (tick_index,),
        )
        closing = [row[0] for row in await cur.fetchall()]

    for season_id in closing:
        await close_season(conn, season_id)


async def standings(conn: AsyncConnection, season_id: int) -> list[Standing]:
    """Live standings (current equity, score NULL) or final ranks once CLOSED."""
    season = await get_season(conn, season_id)
    if season is None:
        raise SeasonNotFoundError(season_id)

    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            f"""
            SELECT e.user_id, e.trades_count, e.final_score, e.final_rank, e.prize_minor,
                   a.balance + COALESCE(pos.value_minor, 0)
                         + COALESCE(sh.short_value_minor, 0)
                         + COALESCE(opt.option_value_minor, 0) AS equity_minor
            FROM season_entries e
            JOIN accounts a ON a.id = e.account_id
            LEFT JOIN (
                SELECT p.season_id, p.user_id,
                       CAST(SUM(p.quantity * i.quoted_price * 100
                                - p.borrow_fees_accrued
                                - p.dividends_accrued) AS BIGINT)
                           AS value_minor
                FROM positions p
                JOIN instruments i ON i.id = p.instrument_id
                WHERE p.season_id = %s AND p.quantity <> 0
                GROUP BY p.season_id, p.user_id
            ) pos ON pos.user_id = e.user_id
            LEFT JOIN ({_SHORT_VALUE_SUBQUERY}) sh
                   ON sh.season_id = e.season_id AND sh.user_id = e.user_id
            LEFT JOIN ({_OPTION_VALUE_SUBQUERY}) opt
                   ON opt.season_id = e.season_id AND opt.user_id = e.user_id
            WHERE e.season_id = %s
            ORDER BY COALESCE(e.final_rank, 2147483647),
                     a.balance + COALESCE(pos.value_minor, 0)
                           + COALESCE(sh.short_value_minor, 0)
                           + COALESCE(opt.option_value_minor, 0) DESC
            """,
            (season_id, season_id),
        )
        rows = await cur.fetchall()

    return [
        Standing(
            user_id=int(row["user_id"]),
            equity_minor=int(row["equity_minor"]),
            trades_count=int(row["trades_count"]),
            final_score=float(row["final_score"]) if row["final_score"] is not None else None,
            final_rank=int(row["final_rank"]) if row["final_rank"] is not None else None,
            prize_minor=int(row["prize_minor"]),
        )
        for row in rows
    ]


async def close_season(conn: AsyncConnection, season_id: int) -> None:
    """Score, rank, pay prizes, and sweep league balances back to SINK.

    One transaction (a savepoint when called from `on_tick` inside
    apply_tick -- without it, a standalone call's bare statements would sit
    in an implicit transaction that the pool rolls back at checkin). The
    status flip is claimed atomically up front: a racing closer blocks on
    the row lock, then sees CLOSED and returns instead of running the
    scoring/prize work a second time.
    """
    async with conn.transaction():
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE seasons SET status = 'CLOSED'
                WHERE id = %s AND status <> 'CLOSED'
                RETURNING id
                """,
                (season_id,),
            )
            if await cur.fetchone() is None:
                await cur.execute(
                    "SELECT 1 FROM seasons WHERE id = %s", (season_id,)
                )
                if await cur.fetchone() is None:
                    raise SeasonNotFoundError(season_id)
                return  # already CLOSED -- idempotent no-op
        await _close_season_claimed(conn, season_id)


async def _close_season_claimed(conn: AsyncConnection, season_id: int) -> None:
    """The scoring/payout/sweep work of close_season. Runs inside the
    caller's transaction with the seasons row already flipped to CLOSED."""
    season = await get_season(conn, season_id)
    assert season is not None

    tick = await current_tick(conn)
    day_index = max(tick, 0) // TICKS_PER_DAY

    async with conn.cursor(row_factory=dict_row) as dcur:
        await dcur.execute(
            "SELECT user_id, account_id, trades_count FROM season_entries WHERE season_id = %s",
            (season_id,),
        )
        entries = await dcur.fetchall()

    if season.sandbox_user_id is not None:
        # Sandbox reset: no scoring, trophies, standings, or result DMs --
        # just sweep the stake back to SINK and cancel resting orders.
        # Settle season options FIRST: their intrinsic payout must land in
        # the league account before the sweep, not post-close into a dead
        # account (the entry row persists, so a later expiry settle would
        # still resolve -- and strand the cash there forever).
        from stockbot.options.service import settle_season_options

        await settle_season_options(conn, season_id, max(tick, 0))
        sink_id = await get_system_account_id(conn, "SINK")
        for entry in entries:
            balance = await get_balance(conn, int(entry["account_id"]))
            if balance > 0:
                await post_transfer(
                    conn,
                    from_account_id=int(entry["account_id"]),
                    to_account_id=sink_id,
                    amount=balance,
                    reason="LEAGUE_RETURN",
                    memo=season.name,
                )
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE orders SET status = 'CANCELLED' "
                "WHERE season_id = %s AND status = 'OPEN'",
                (season_id,),
            )
        return

    # Final equity snapshot at close so the last partial day counts.
    for entry in entries:
        equity = await league_equity_minor(conn, season_id, int(entry["user_id"]))
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO equity_snapshots
                    (season_id, user_id, day_index, tick_index, equity_minor)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (season_id, user_id, day_index) DO UPDATE
                    SET equity_minor = EXCLUDED.equity_minor,
                        tick_index = EXCLUDED.tick_index
                """,
                (season_id, entry["user_id"], day_index, tick, equity),
            )

    # Score each entrant from their daily equity series.
    scores: dict[int, float | None] = {}
    for entry in entries:
        uid = int(entry["user_id"])
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT equity_minor FROM equity_snapshots
                WHERE season_id = %s AND user_id = %s
                ORDER BY day_index
                """,
                (season_id, uid),
            )
            equities = [int(row[0]) for row in await cur.fetchall()]
        scores[uid] = (
            league_score(equities)
            if int(entry["trades_count"]) >= MIN_TRADES
            else None
        )

    def _score(uid: int) -> float:
        score = scores[uid]
        return score if score is not None else float("-inf")

    qualified = sorted(
        (uid for uid, s in scores.items() if s is not None), key=_score, reverse=True
    )
    rank_by_user = {uid: rank for rank, uid in enumerate(qualified, start=1)}

    prize_pool = season.entry_fee_minor * len(entries)

    faucet_id = await get_system_account_id(conn, "FAUCET")
    sink_id = await get_system_account_id(conn, "SINK")

    for uid, rank in rank_by_user.items():
        prize = (
            int(prize_pool * PRIZE_SHARES[rank - 1]) if rank <= len(PRIZE_SHARES) else 0
        )
        main_account = await get_user_account_id(conn, uid)
        if prize > 0:
            await post_transfer(
                conn,
                from_account_id=faucet_id,
                to_account_id=main_account,
                amount=prize,
                reason="LEAGUE_PRIZE",
                memo=f"{season.name} rank {rank}",
            )
            # Permanent, non-purchasable trophy entitlement.
            item_key = f"trophy_s{season_id}_r{rank}"
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    INSERT INTO shop_items (key, name, description, kind, price_minor)
                    VALUES (%s, %s, %s, 'TROPHY', NULL)
                    ON CONFLICT (key) DO NOTHING
                    """,
                    (
                        item_key,
                        f"{season.name} — rank {rank}",
                        f"Trophy for finishing rank {rank} in {season.name}.",
                    ),
                )
                await cur.execute(
                    """
                    INSERT INTO entitlements (user_id, item_key)
                    VALUES (%s, %s)
                    ON CONFLICT (user_id, item_key) DO NOTHING
                    """,
                    (uid, item_key),
                )
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE season_entries
                SET final_score = %s, final_rank = %s, prize_minor = %s
                WHERE season_id = %s AND user_id = %s
                """,
                (scores[uid], rank, prize, season_id, uid),
            )
            await cur.execute(
                """
                INSERT INTO notifications (user_id, kind, payload)
                VALUES (%s, 'SEASON_RESULT', %s)
                """,
                (
                    uid,
                    json.dumps(
                        {
                            "tick_index": tick,
                            "season_name": season.name,
                            "rank": rank,
                            "score": scores[uid],
                            "prize": prize,
                        }
                    ),
                ),
            )

    # Unqualified entrants still get their final score recorded (NULL)
    # and a result DM -- "you didn't qualify" beats silence.
    for entry in entries:
        uid = int(entry["user_id"])
        if uid in rank_by_user:
            continue
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE season_entries SET final_score = %s
                WHERE season_id = %s AND user_id = %s
                """,
                (scores[uid], season_id, uid),
            )
            await cur.execute(
                """
                INSERT INTO notifications (user_id, kind, payload)
                VALUES (%s, 'SEASON_RESULT', %s)
                """,
                (
                    uid,
                    json.dumps(
                        {
                            "tick_index": tick,
                            "season_name": season.name,
                            "rank": None,
                            "score": None,
                            "prize": 0,
                        }
                    ),
                ),
            )

    # Season options settle at intrinsic NOW -- the equity snapshots above
    # already counted their model marks (which include residual time value),
    # and the intrinsic payout joins the league cash for the sweep. Waiting
    # for expiry would pay into an account the sweep already emptied.
    from stockbot.options.service import settle_season_options

    await settle_season_options(conn, season_id, max(tick, 0))

    # League wealth never leaves the league: sweep whatever is left to SINK.
    for entry in entries:
        league_account = int(entry["account_id"])
        balance = await get_balance(conn, league_account)
        if balance > 0:
            await post_transfer(
                conn,
                from_account_id=league_account,
                to_account_id=sink_id,
                amount=balance,
                reason="LEAGUE_RETURN",
                memo=season.name,
            )

    async with conn.cursor() as cur:
        # status was already flipped by the atomic claim in close_season.
        await cur.execute(
            "UPDATE seasons SET prize_pool_minor = %s WHERE id = %s",
            (prize_pool, season_id),
        )
        # League orders reference a dead season now -- cancel them.
        await cur.execute(
            "UPDATE orders SET status = 'CANCELLED' "
            "WHERE season_id = %s AND status = 'OPEN'",
            (season_id,),
        )
